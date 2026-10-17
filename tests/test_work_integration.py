"""The daily launcher reaches real generated consumers and the setup lifecycle."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from devcontainer_setup.__main__ import defaults, integrate_continuity, integrate_taskfile
from devcontainer_setup.discovery import scan
from devcontainer_setup.render import render

SOURCE = Path(__file__).resolve().parents[1]


class WorkIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / 'project').resolve()
        self.root.mkdir()
        subprocess.run(['git', 'init', '-qb', 'main', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'remote', 'add', 'origin',
                        'https://github.com/example/project.git'], check=True)
        self.preferences = dict(apply_dotfiles=False, dotfiles_repo='', agentic_tools=True)
        self.chosen = defaults(scan(self.root), self.preferences, {})
        self.chosen['continuity'] = True

    def install(self, existing=None):
        if existing:
            (self.root / 'Taskfile.yml').write_text(existing)
        files = render(self.root, SOURCE, self.chosen, 'a' * 40)
        integrate_taskfile(self.root, files)
        integrate_continuity(self.root, files, True)
        for name, data in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return files

    def test_new_projects_recommend_sync_and_explicit_opt_out_survives_upgrade(self):
        self.assertIs(defaults(scan(self.root), self.preferences, {})['continuity'], True)
        result = defaults(scan(self.root), self.preferences,
                          {'selections': dict(self.chosen, continuity=False)})
        self.assertIs(result['continuity'], False)

    def test_generated_work_task_dispatches_and_nonterminal_is_actionable(self):
        self.install()
        listed = subprocess.run(['task', '--list-all', '--json'], cwd=self.root,
                                capture_output=True, text=True, check=True)
        names = {row['name'] for row in json.loads(listed.stdout)['tasks']}
        self.assertTrue({'work', 'devcontainer:work', 'up', 'down', 'status',
                         'scratch:start', 'agent:handoff'} <= names, sorted(names))
        result = subprocess.run(['task', 'work'], cwd=self.root,
                                env=dict(os.environ, XDG_STATE_HOME=str(self.root.parent / 'state')),
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('terminal', result.stderr.lower())
        self.assertNotIn('invalid choice', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.root.parent / 'state').exists())

    def test_four_shortcuts_dispatch_resume_flags_from_generated_tasks(self):
        self.install()
        for agent in ('codex', 'claude'):
            for suffix in ('', ':cs'):
                target = 'work:' + agent + suffix
                for prefix in ('', 'devcontainer:'):
                    with self.subTest(target=prefix+target):
                        result = subprocess.run(['task', '--dry', prefix+target, '--', '--resume'],
                                                cwd=self.root, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        command = result.stdout + result.stderr
                        self.assertIn('work --agent ' + agent, command)
                        self.assertIn('--resume', command)
                        self.assertEqual('--to codespace' in command, suffix == ':cs')
                result = subprocess.run(['task', target], cwd=self.root, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('terminal', result.stderr.lower())
                self.assertNotIn('unrecognized', result.stderr)

    def test_existing_shortcuts_are_preserved_while_namespaced_version_exists(self):
        self.install("version: '3'\ntasks:\n  work:codex:\n    cmds: ['echo owner-codex']\n")
        result = subprocess.run(['task', 'work:codex'], cwd=self.root, capture_output=True, text=True, check=True)
        self.assertIn('owner-codex', result.stdout)
        result = subprocess.run(['task', '--dry', 'devcontainer:work:codex'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('work --agent codex', result.stderr + result.stdout)

    def test_cli_dispatches_shortcut_and_requires_agent_for_resume_or_destination(self):
        from devcontainer_setup.project_sync.__main__ import parser, run
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
                patch('devcontainer_setup.project_sync.work.shortcut', return_value=0) as shortcut:
            args = parser().parse_args(['--project', str(self.root), 'work', '--agent', 'codex', '--to', 'codespace', '--resume'])
            self.assertEqual(run(args), 0)
        self.assertEqual(shortcut.call_args.kwargs, dict(agent='codex', destination='codespace', resume=True))
        for flags in (['--resume'], ['--to', 'codespace']):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, 'agent'):
                run(parser().parse_args(['--project', str(self.root), 'work', *flags]))

    def test_remote_new_launch_receipt_records_exact_id_and_refuses_repeated_request(self):
        from devcontainer_setup.project_sync.__main__ import parser, run
        from devcontainer_setup.project_sync.handoff import LocalEndpoint
        from devcontainer_setup.project_sync.state import discover_project
        request = '12345678-1234-4234-8234-123456789abc'
        sid = '22345678-1234-4234-8234-123456789abc'
        state = self.root.parent / 'private'
        args = parser().parse_args(['--project', str(self.root), 'agent', 'codex', '--new', '--launch-request', request])
        with patch('devcontainer_setup.project_sync.__main__.state_dir', return_value=state), \
                patch('devcontainer_setup.project_sync.__main__.host_id', return_value='host'), \
                patch('devcontainer_setup.project_sync.agents.create_codex_session', return_value=sid) as create, \
                patch('devcontainer_setup.project_sync.agents.launch', return_value=0) as launch:
            self.assertEqual(run(args), 0)
            self.assertEqual(LocalEndpoint(discover_project(self.root), state).call('launch_result', request_id=request),
                             dict(agent='codex', session_id=sid))
            with self.assertRaisesRegex(ValueError, 'already exists'):
                run(args)
        create.assert_called_once()
        launch.assert_called_once()
        self.assertEqual((state / 'work-launches' / (request + '.json')).stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, 'UUID'):
            LocalEndpoint(discover_project(self.root), state).call('launch_result', request_id='../bad')

    def test_existing_work_and_other_root_tasks_are_preserved(self):
        self.install("version: '3'\ntasks:\n  work:\n    cmds: ['echo original-work']\n"
                     "  up:\n    cmds: ['echo original-up']\n")
        for target in ('work', 'up'):
            result = subprocess.run(['task', target], cwd=self.root,
                                    capture_output=True, text=True, check=True)
            self.assertIn('original-' + target, result.stdout)
        result = subprocess.run(['task', '--dry', 'devcontainer:work'], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('project-sync.sh work', result.stderr + result.stdout)

    def test_cli_work_rejects_scripted_use_before_importing_menu(self):
        result = subprocess.run(['bash', str(SOURCE / 'scripts/project-sync.sh'),
                                 '--project', str(self.root), 'work'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('terminal', result.stderr.lower())

    def test_setup_pairs_selected_codespace_and_starts_worker(self):
        from devcontainer_setup.__main__ import setup_continuity
        service = MagicMock(is_remote=False)
        service.binding.return_value = None
        service.roots.return_value = ['/workspaces/project']
        with patch('devcontainer_setup.project_sync.workflows.Workflows', return_value=service):
            with contextlib.redirect_stdout(io.StringIO()):
                setup_continuity(self.root, MagicMock(), 'selected-space')
        service.pair.assert_called_once_with('selected-space', '/workspaces/project')
        service.ensure_sync.assert_called_once_with()

    def test_setup_asks_only_for_ambiguous_remote_checkouts(self):
        from devcontainer_setup.__main__ import setup_continuity
        service, ui = MagicMock(is_remote=False), MagicMock()
        service.binding.return_value = None
        service.roots.return_value = ['/workspaces/project', '/workspaces/alternate']
        ui.choose.return_value = '/workspaces/alternate'
        with patch('devcontainer_setup.project_sync.workflows.Workflows', return_value=service):
            with contextlib.redirect_stdout(io.StringIO()):
                setup_continuity(self.root, ui, 'selected-space')
        ui.choose.assert_called_once()
        service.pair.assert_called_once_with('selected-space', '/workspaces/alternate')

    def test_commit_only_setup_defers_pairing_without_github_calls(self):
        from devcontainer_setup.__main__ import setup_continuity
        output = io.StringIO()
        with patch('devcontainer_setup.project_sync.workflows.Workflows') as service:
            with contextlib.redirect_stdout(output):
                setup_continuity(self.root, MagicMock(), None)
        service.assert_not_called()
        self.assertIn('task work', output.getvalue())

    def test_failed_pairing_preserves_setup_and_offers_daily_entrypoint(self):
        from devcontainer_setup.__main__ import setup_continuity
        service = MagicMock(is_remote=False)
        service.binding.return_value = None
        service.roots.side_effect = ValueError('SSH temporarily unavailable')
        output = io.StringIO()
        with patch('devcontainer_setup.project_sync.workflows.Workflows', return_value=service):
            with contextlib.redirect_stderr(output):
                setup_continuity(self.root, MagicMock(), 'selected-space')
        self.assertIn('task work', output.getvalue())
        self.assertIn('preserved', output.getvalue())
        service.ensure_sync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
