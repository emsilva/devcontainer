import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]


class WizardCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(Path(self.tmp.name) / 'config'),
                        XDG_CACHE_HOME=str(Path(self.tmp.name) / 'cache'))
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test User')
        self.git('config', 'user.email', 'test@example.com')
        (self.root / 'package.json').write_text('{"engines":{"node":"24"}}\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Initial')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'devcontainer_setup', '--project', str(self.root), *args],
                              cwd=SOURCE, env=self.env, capture_output=True, text=True)

    def test_scan_reports_node_and_python_without_writing(self):
        result = self.cli('--scan')
        self.assertEqual(result.returncode, 0, result.stderr)
        scan = json.loads(result.stdout)
        self.assertEqual(scan['runtimes']['node']['version'], '24')
        self.assertTrue(scan['runtimes']['python']['selected'])
        self.assertFalse((self.root / '.devcontainer').exists())
        self.assertFalse(Path(self.env['XDG_CONFIG_HOME']).exists())

    def test_dry_run_shows_generated_diff_without_mutating_checkout(self):
        before = self.git('rev-parse', 'HEAD')
        result = self.cli('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('devcontainer.json', result.stdout)
        self.assertIn('python:1', result.stdout)
        self.assertNotIn('ruby:1', result.stdout)
        self.assertFalse((self.root / '.devcontainer').exists())
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)

    def test_noninteractive_mutation_is_refused(self):
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('terminal', result.stderr.lower())
        self.assertFalse((self.root / '.devcontainer').exists())

    def test_invalid_configuration_check_fails(self):
        result = self.cli('--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('devcontainer', result.stderr.lower())

    def test_upgrade_handoff_uses_fetched_immutable_runner_without_repeating_fetch(self):
        from devcontainer_setup.__main__ import upgrade_handoff
        release = Path(self.tmp.name) / ('b' * 40)
        with patch('devcontainer_setup.__main__.fetch_release', return_value=(release, 'b' * 40)) as fetch:
            with patch('devcontainer_setup.__main__.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(upgrade_handoff(self.root, 'a' * 40, 'existing-space'), 0)
        fetch.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['bash', str(release / 'scripts/setup.sh')])
        self.assertIn('--upgrade-loaded', command)
        self.assertIn('--resume', command)
        self.assertEqual(run.call_args.kwargs['env']['DEVCONTAINER_SETUP_REF'], 'b' * 40)

    def test_advanced_selection_can_override_runtime_versions_and_ports(self):
        from devcontainer_setup.__main__ import _select, defaults
        from devcontainer_setup.discovery import scan
        found = scan(self.root)
        preferences = {'apply_dotfiles': False, 'dotfiles_repo': '', 'agentic_tools': True}
        class UI:
            def multi(self, title, options, selected):
                return selected
            def confirm(self, title, default=True):
                return title == 'Advanced environment options?' or title == 'Include Claude Code and Codex?'
            def input(self, title, value=''):
                if title == 'node version':
                    return '26'
                if title.startswith('Forwarded ports'):
                    return '3570,8787'
                return value
        result = _select(UI(), found, defaults(found, preferences, {}), preferences)
        self.assertEqual(result['runtimes']['node'], '26')
        self.assertEqual(result['ports'], [3570, 8787])

    def test_new_dotfiles_repository_is_checked_without_shell_execution(self):
        from devcontainer_setup.__main__ import repository_access
        with patch('devcontainer_setup.__main__.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(repository_access('emsilva/dotfiles'))
        self.assertEqual(run.call_args.args[0], ['git', 'ls-remote', '--exit-code', '--',
                                               'https://github.com/emsilva/dotfiles.git', 'HEAD'])
        self.assertEqual(run.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')

    def test_dotfiles_https_url_cannot_persist_credentials(self):
        from devcontainer_setup.__main__ import dotfiles_repository
        with self.assertRaises(ValueError):
            dotfiles_repository('https://personal-token@github.com/owner/dotfiles.git')


if __name__ == '__main__':
    unittest.main()
