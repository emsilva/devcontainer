import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

try:
    from devcontainer_setup.preferences import config_path, load_preferences, save_preferences
except ImportError:
    config_path = lambda env=None: None
    load_preferences = lambda env=None: {}
    save_preferences = lambda values, env=None: None


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {'HOME': str(self.root), 'XDG_CONFIG_HOME': str(self.root / 'config')}

    def test_first_use_defaults_do_not_write_or_hardcode_dotfiles_owner(self):
        self.assertEqual(load_preferences(self.env), {'version': 1, 'apply_dotfiles': True, 'dotfiles_repo': '', 'agentic_tools': True})
        self.assertFalse((self.root / 'config').exists())

    def test_absolute_xdg_and_empty_relative_fallback(self):
        self.assertEqual(config_path(self.env), self.root / 'config/devcontainer-setup/config.json')
        for value in ('', 'relative/path', None):
            env = {'HOME': str(self.root)}
            if value is not None: env['XDG_CONFIG_HOME'] = value
            self.assertEqual(config_path(env), self.root / '.config/devcontainer-setup/config.json')

    def test_confirmed_preferences_shared_and_partial_updates_preserve_fields(self):
        saved = save_preferences({'dotfiles_repo': 'emsilva/dotfiles'}, self.env)
        self.assertEqual(saved, config_path(self.env))
        self.assertEqual(load_preferences(self.env)['dotfiles_repo'], 'emsilva/dotfiles')
        save_preferences({'agentic_tools': False}, self.env)
        self.assertEqual(load_preferences(self.env), {'version': 1, 'apply_dotfiles': True, 'dotfiles_repo': 'emsilva/dotfiles', 'agentic_tools': False})
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(saved.parent.stat().st_mode), 0o700)

    def test_malformed_values_are_actionable_and_never_overwritten(self):
        path = config_path(self.env)
        self.assertIsNotNone(path)
        path.parent.mkdir(parents=True)
        for content in ('{', '[]', '{"version":2}', '{"agentic_tools":"yes"}', '{"dotfiles_repo":42}'):
            path.write_text(content)
            with self.assertRaisesRegex(ValueError, 'config.json'):
                load_preferences(self.env)
            with self.assertRaises(ValueError):
                save_preferences({'agentic_tools': False}, self.env)
            self.assertEqual(path.read_text(), content)

    def test_invalid_input_and_tokens_not_persisted(self):
        for values in ({'version': True}, {'apply_dotfiles': 1}, {'dotfiles_repo': '\n'}, {'token': 'secret'}, ['bad']):
            with self.assertRaises(ValueError):
                save_preferences(values, self.env)
        self.assertFalse((self.root / 'config').exists())

    def test_atomic_replace_failure_preserves_existing_bytes_and_cleans_temp(self):
        path = save_preferences({'dotfiles_repo': 'owner/repo'}, self.env)
        self.assertIsNotNone(path)
        before = path.read_bytes()
        with patch('devcontainer_setup.preferences.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                save_preferences({'agentic_tools': False}, self.env)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_environ_is_used_when_not_explicitly_supplied(self):
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(config_path(), config_path(self.env))


if __name__ == '__main__':
    unittest.main()
