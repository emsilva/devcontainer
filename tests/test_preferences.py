import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from devcontainer_setup import preferences
from devcontainer_setup.preferences import config_path, load_preferences, save_preferences


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

    def test_invalid_utf8_receives_config_location(self):
        path = config_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'\xff')
        with self.assertRaisesRegex(ValueError, 'config.json'):
            load_preferences(self.env)

    def test_unknown_preference_keys_survive_load_and_save(self):
        path = save_preferences({'dotfiles_repo': 'owner/dotfiles'}, self.env)
        original = json.loads(path.read_text())
        original['future_setting'] = {'enabled': True, 'values': [1, 'kept']}
        path.write_text(json.dumps(original))
        loaded = load_preferences(self.env)
        self.assertEqual(loaded, {key: value for key, value in original.items() if key != 'future_setting'})
        save_preferences(dict(loaded, apply_dotfiles=False), self.env)
        self.assertEqual(json.loads(path.read_text()), dict(original, apply_dotfiles=False))
        invalid = dict(original, apply_dotfiles='yes')
        path.write_text(json.dumps(invalid))
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'apply_dotfiles'):
            load_preferences(self.env)
        with self.assertRaisesRegex(ValueError, 'apply_dotfiles'):
            save_preferences({'dotfiles_repo': 'new/repo'}, self.env)
        self.assertEqual(path.read_bytes(), before)

    def test_open_after_create_defaults_to_none_and_validates(self):
        self.assertTrue(callable(getattr(preferences, 'load_open_after_create', None)), 'editor preference loader is missing')
        load = preferences.load_open_after_create
        save = preferences.save_open_after_create
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(load(self.env), 'none')
        self.assertEqual(stderr.getvalue(), '')
        self.assertFalse((self.root / 'config').exists())
        config = save_preferences({'dotfiles_repo': 'owner/dotfiles'}, self.env)
        before = config.read_bytes()
        for value in ('none', 'browser', 'vscode'):
            with self.subTest(valid=value):
                path = save(value, self.env)
                self.assertEqual(path, config.with_name('open-after-create.json'))
                self.assertEqual(json.loads(path.read_text()), {'open_after_create': value})
                self.assertEqual(load(self.env), value)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(config.read_bytes(), before)
        for content in (b'{', b'[]', b'{}', b'{"open_after_create":true}',
                        b'{"open_after_create":"editor"}', b'\xff'):
            with self.subTest(invalid=content):
                path.write_bytes(content)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(load(self.env), 'none')
                self.assertEqual(len(stderr.getvalue().splitlines()), 1)
                self.assertIn(str(path), stderr.getvalue())
                self.assertEqual(path.read_bytes(), content)
        for value in ('', 'editor', None, True, [], {}):
            with self.subTest(invalid_save=value):
                with self.assertRaises(ValueError):
                    save(value, self.env)
                self.assertEqual(path.read_bytes(), b'\xff')
        fallback = {'HOME': str(self.root), 'XDG_CONFIG_HOME': 'relative'}
        path = save('browser', fallback)
        self.assertEqual(path, self.root / '.config/devcontainer-setup/open-after-create.json')
        with patch.dict(os.environ, fallback, clear=True):
            self.assertEqual(load(), 'browser')
        self.assertEqual(config.read_bytes(), before)

    def test_open_after_create_replace_failure_preserves_bytes_and_cleans_temp(self):
        self.assertTrue(callable(getattr(preferences, 'save_open_after_create', None)), 'editor preference saver is missing')
        path = preferences.save_open_after_create('browser', self.env)
        before = path.read_bytes()
        with patch('devcontainer_setup.preferences.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                preferences.save_open_after_create('vscode', self.env)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
