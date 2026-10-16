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

    def test_launch_choices_work_before_local_branch_is_published(self):
        from devcontainer_setup.__main__ import _launch_options
        from devcontainer_setup.codespaces import Codespaces
        self.git('switch', '-c', 'docs/local-only')
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        before = self.git('rev-parse', 'HEAD')
        calls = []

        def github(args, **kwargs):
            calls.append(args)
            if args[1] != 'api':
                self.fail(f'Machine selection must not push or allocate: {args}')
            if args[2].endswith('/machines'):
                if any(arg.startswith('ref=') for arg in args):
                    return subprocess.CompletedProcess(args, 1, '', "The 'ref' provided was invalid (HTTP 400)")
                payload = {'machines': [{'name': 'standardLinux32gb', 'cpus': 4,
                            'memory_in_bytes': 16 * 1024**3, 'storage_in_bytes': 32 * 1024**3}]}
            else:
                self.assertEqual(args[2], 'user/codespaces?per_page=100')
                payload = [{'codespaces': []}]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')

        case = self
        class UI:
            def choose(self, title, options, selected=None):
                return 'Commit, push and open in browser' if title == 'After setup' else selected or options[0]

            def confirm(self, title, default=True):
                case.assertNotEqual(title, 'Continue with commit only?',
                                    'Unpublished branches must still offer Codespaces launch')
                return default

        options = _launch_options(UI(), Codespaces(run=github), self.root, 'docs/local-only', None)
        self.assertEqual(options['machine'], 'standardLinux32gb')
        self.assertEqual(options['client'], 'browser')
        self.assertEqual(options['idle_timeout'], '30m')
        self.assertFalse(any(arg.startswith('ref=') for call in calls for arg in call))
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_existing_taskfile_variants_keep_real_task_operations(self):
        import shutil
        from devcontainer_setup.__main__ import integrate_taskfile
        self.assertIsNotNone(shutil.which('task'), 'real go-task required')
        for name in ('Taskfile.yml', 'Taskfile.yaml', 'taskfile.yml', 'taskfile.yaml',
                     'Taskfile.dist.yml', 'Taskfile.dist.yaml', 'taskfile.dist.yml', 'taskfile.dist.yaml'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / name).write_text("version: '3'\ntasks:\n  up:\n    cmds: ['echo existing']\n  test:\n    cmds: ['echo tests']\n")
                files = {}
                integrate_taskfile(root, files)
                self.assertIn(name, files)
                for relative, data in files.items():
                    (root / relative).write_bytes(data)
                result = subprocess.run(['task', '--list-all'], cwd=root, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('up:', result.stdout)
                self.assertIn('test:', result.stdout)
                self.assertEqual([path.name for path in root.glob('*') if 'taskfile' in path.name.lower()], [name])

    def test_package_manager_pin_survives_defaults_and_renders(self):
        from devcontainer_setup.__main__ import defaults
        from devcontainer_setup.discovery import scan
        from devcontainer_setup.render import render
        (self.root / 'package.json').write_text('{"packageManager":"pnpm@9.15.4","engines":{"node":"24","pnpm":"9.15.4"}}')
        chosen = defaults(scan(self.root), {'agentic_tools': False, 'apply_dotfiles': False}, {})
        self.assertEqual(chosen['package_manager_versions'], {'node': '9.15.4'})
        config = json.loads(render(self.root, SOURCE, chosen, 'a' * 40)['.devcontainer/devcontainer.json'])
        self.assertEqual(config['features']['ghcr.io/devcontainers/features/node:1']['pnpmVersion'], '9.15.4')

    def test_manager_version_conflict_prompts_with_both_declarations(self):
        import contextlib
        import io
        from devcontainer_setup.__main__ import defaults, _select
        from devcontainer_setup.discovery import scan
        (self.root / 'package.json').write_text('{"packageManager":"pnpm@9.15.4"}')
        (self.root / 'nested').mkdir()
        (self.root / 'nested/package.json').write_text('{"packageManager":"pnpm@10.2.1"}')
        found = scan(self.root)
        preferences = {'agentic_tools': False, 'apply_dotfiles': False, 'dotfiles_repo': ''}
        class UI:
            prompts = []
            def multi(self, title, options, selected): return selected
            def confirm(self, title, default=True): return False
            def input(self, title, value=''):
                self.prompts.append(title)
                return '9.15.4'
        ui, output = UI(), io.StringIO()
        with contextlib.redirect_stdout(output):
            chosen = _select(ui, found, defaults(found, preferences, {}), preferences)
        self.assertTrue(any('pnpm' in prompt and 'version' in prompt for prompt in ui.prompts))
        self.assertIn('9.15.4', output.getvalue())
        self.assertIn('10.2.1', output.getvalue())
        self.assertEqual(chosen['package_manager_versions'], {'node': '9.15.4'})

    def test_fresh_taskfile_has_working_lifecycle_aliases(self):
        from devcontainer_setup.__main__ import integrate_taskfile
        files = {}
        integrate_taskfile(self.root, files)
        (self.root / 'Taskfile.yml').write_bytes(files['Taskfile.yml'])
        (self.root / '.devcontainer').mkdir()
        (self.root / '.devcontainer/Taskfile.yml').write_bytes((SOURCE / 'templates/Taskfile.yml').read_bytes())
        for operation in ('up', 'down', 'status', 'build'):
            result = subprocess.run(['task', '--dry', operation], cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_unresolved_manager_versions_cannot_silently_dry_run_default(self):
        (self.root / 'package.json').write_text('{"packageManager":"pnpm@9.15.4"}')
        (self.root / 'nested').mkdir()
        (self.root / 'nested/package.json').write_text('{"packageManager":"pnpm@10.2.1"}')
        result = self.cli('--dry-run')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('version', result.stderr)
        self.assertNotIn('12.11.2', result.stdout)

    def test_ambiguous_taskfiles_fail_without_changing_either(self):
        from devcontainer_setup.__main__ import integrate_taskfile
        for name in ('Taskfile.yml', 'Taskfile.yaml'):
            (self.root / name).write_text("version: '3'\n")
        files = {}
        with self.assertRaisesRegex(ValueError, 'Multiple supported Taskfiles'):
            integrate_taskfile(self.root, files)
        self.assertEqual(files, {})

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
