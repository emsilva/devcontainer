"""Regression tests for R1 UX, setup, error-recovery, sync-reliability, and doc improvements."""
from __future__ import annotations

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class Area1SetupImprovementsTests(unittest.TestCase):
    def test_check_gh_accepts_2_102_0_and_newer_and_guides_upgrade_when_older(self):
        from devcontainer_setup.project_sync import transport
        for stdout in ('gh version 2.102.0 (2026-01-01)\n',
                       'gh version 2.102.5 (2026-02-01)\n',
                       'gh version 2.110.0 (2026-06-01)\n',
                       'gh version 3.0.0 (2027-01-01)\n'):
            with self.subTest(stdout=stdout.strip()), \
                    patch.object(transport.subprocess, 'run', return_value=Mock(stdout=stdout)):
                transport._check_gh({})
        for bad in ('gh version 2.101.9 (2025-12-01)\n', 'gh version 1.99.0\n', 'unexpected\n'):
            with self.subTest(bad=bad.strip()), \
                    patch.object(transport.subprocess, 'run', return_value=Mock(stdout=bad)):
                with self.assertRaisesRegex(ValueError, '2.102.0 or newer.*brew upgrade gh'):
                    transport._check_gh({})

    def test_preflight_and_doctor_report_required_and_optional_fixes(self):
        from devcontainer_setup import __main__ as wizard
        with tempfile.TemporaryDirectory() as tmp:
            def fake_which(cmd):
                return '/usr/bin/' + cmd if cmd in ('git', 'task') else None
            with patch.object(wizard.shutil, 'which', side_effect=fake_which):
                checks = wizard.preflight(env={'HOME': tmp})
            by_name = {item['name']: item for item in checks}
            self.assertTrue(by_name['python3']['ok'])
            self.assertTrue(by_name['git']['ok'])
            self.assertTrue(by_name['task']['ok'])
            self.assertFalse(by_name['gh']['ok'])
            self.assertIn('brew install gh', by_name['gh']['fix'])
            self.assertFalse(by_name['herdr']['ok'])
            self.assertFalse(by_name['codespaces-ssh-key']['ok'])
            buf = io.StringIO()
            self.assertEqual(wizard.print_doctor(checks, stream=buf), 0)
            self.assertIn('[WARN   ] herdr:', buf.getvalue())

            with patch.object(wizard.shutil, 'which', return_value=None):
                missing = wizard.preflight(env={'HOME': tmp})
            buf2 = io.StringIO()
            self.assertEqual(wizard.print_doctor(missing, stream=buf2), 1)
            self.assertIn('[MISSING] task:', buf2.getvalue())

    def test_dotfiles_answer_skips_cleanly_on_blank_input(self):
        from devcontainer_setup import __main__ as wizard
        ui = Mock()
        ui.input.return_value = '   '
        prefs = {'dotfiles_repo': ''}
        self.assertFalse(wizard._dotfiles_answer(ui, prefs))
        self.assertEqual(prefs['dotfiles_repo'], '')

    def test_select_excludes_node_from_optional_runtime_multi_select(self):
        from devcontainer_setup import __main__ as wizard
        from devcontainer_setup.discovery import scan
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            subprocess.run(['git', '-C', str(project), 'init', '-q', '-b', 'main'], check=True)
            (project / 'package.json').write_text('{"engines": {"node": "24"}}\n')
            subprocess.run(['git', '-C', str(project), 'add', 'package.json'], check=True)
            found = scan(project)
            chosen = wizard.defaults(found, {'apply_dotfiles': False, 'dotfiles_repo': ''}, {})
            ui = Mock()
            multi_calls = []
            def fake_multi(prompt, options, selected):
                multi_calls.append((prompt, options, selected))
                return []
            ui.multi.side_effect = fake_multi
            ui.confirm.return_value = False
            result = wizard._select(ui, found, chosen, {'apply_dotfiles': False, 'dotfiles_repo': ''})
            self.assertEqual(multi_calls[0][1], ['ruby', 'go', 'rust'])
            self.assertNotIn('node', multi_calls[0][1])
            self.assertEqual(result['runtimes'].get('node'), '24')

    def test_setup_wizard_script_uses_cached_release_when_offline_for_readonly_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rev = 'a' * 40
            script = root / 'setup-wizard.sh'
            script.write_text((ROOT / 'templates/scripts/setup-wizard.sh').read_text().replace('__SOURCE_REF__', rev))
            script.chmod(0o755)
            cached = root / 'cache/devcontainer-setup/releases' / rev / 'scripts/setup.sh'
            cached.parent.mkdir(parents=True)
            cached.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
            cached.chmod(0o755)
            env = dict(os.environ, XDG_CACHE_HOME=str(root / 'cache'),
                       SETUP_DEVCONTAINER_REPO=str(root / 'missing-remote.git'))
            res = subprocess.run(['bash', str(script), '--check'], cwd=root, env=env,
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn('offline', res.stderr)
            self.assertIn('--check', res.stdout)

    def test_lost_create_reconciliation_adopts_single_new_codespace(self):
        from devcontainer_setup.codespaces import Codespaces
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            if args[1:3] == ['codespace', 'create']:
                return subprocess.CompletedProcess(args, 0, '', '')
            if args[1] == 'api' and args[2].startswith('user/codespaces?'):
                payload = [{'codespaces': [
                    {'name': 'adopted-cs-1', 'repository': {'full_name': 'owner/repo'},
                     'git_status': {'ref': 'main'}, 'machine': {'name': 'standardLinux32gb'},
                     'created_at': now_iso},
                ]}]
                return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
            return subprocess.CompletedProcess(args, 0, '', '')
        api = Codespaces(run=fake_run)
        adopted = api._reconcile_lost_create('owner/repo', 'main', 'standardLinux32gb', time.time())
        self.assertEqual(adopted, 'adopted-cs-1')

    def test_dirty_checkout_error_names_remediation_commands(self):
        from devcontainer_setup.codespaces import Codespaces
        api = Codespaces(run=lambda args, **kw: subprocess.CompletedProcess(args, 0, '', ''))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(api, 'get', return_value={
                    'name': 'cs-dirty', 'state': 'Available',
                    'repository': {'full_name': 'owner/repo'}, 'git_status': {'ref': 'main'},
                    'machine': {'name': 'm'}, 'idle_timeout_minutes': 30,
                    'retention_period_minutes': 10080, 'location': 'EastUs',
                }), \
                patch.object(api, 'wait'), \
                patch.object(api, '_read_ssh', side_effect=['main', ' M app.py']):
            with self.assertRaises(ValueError) as caught:
                api.launch('owner/repo', 'main', 'a' * 40, 'm', '30m', 'none', None,
                           Path(tmp) / 'launch.json', resume='cs-dirty')
        msg = str(caught.exception)
        self.assertIn('gh codespace ssh -c cs-dirty', msg)
        self.assertIn('--replace', msg)
        self.assertIn('task work:cs:delete -- --codespace cs-dirty', msg)


class Area2And3WorkflowRecoveryTests(unittest.TestCase):
    def test_ready_opens_herdr_with_warning_on_scratch_conflicts(self):
        from devcontainer_setup.project_sync import scratch, workspace
        binding = dict(version=1, root='/tmp/p', identity='github.com/o/r', codespace='cs', remote_root='/workspaces/r')
        runtime = dict(running=True, phase='syncing', checkpoint_version=3,
                       runtime_id=scratch._runtime_id(), heartbeat_at=time.time(),
                       prepared_binding=binding, connection_prepared=True)
        project = Mock(root=Path('/tmp/p'))
        out = io.StringIO()
        with patch.object(scratch, 'status', return_value=runtime), \
                patch.object(scratch, '_config', return_value=binding), \
                patch.object(scratch, 'inspect_sync', return_value=dict(phase='conflicts', conflicts=['memory/notes.md'])), \
                patch('sys.stdout', out):
            returned = workspace._ready(project, Path('/tmp/state'), timeout=5)
        self.assertEqual(returned, runtime)
        self.assertIn('memory/notes.md', out.getvalue())

    def test_overview_surfaces_last_restart_issue_scratch_conflicts_codespace_mac_and_more_actions(self):
        from devcontainer_setup.project_sync import overview, standing
        from devcontainer_setup.project_sync.probe import Observation, Remote, Space
        now = 1_760_000_000.0
        base = standing.Local(root=Path('/fixture/proj'), pairing='cs-1', armed=True, keeper='running',
                              worker='running', logs=True, last_restart_issue='no single lead',
                              scratch_conflicts=('memory/plan.md', 'tmp/notes.txt'))
        remote = Remote(
            account=Observation('ok', 'me', now, None),
            spaces=Observation('ok', (Space('cs-1', 'Available', now),), now, None),
            names=Observation('ok', frozenset({'cs-1'}), now, None),
            setup_prs=Observation('ok', (), now, None),
            upstream=Observation('ok', None, now, None),
        )
        frame = overview.view(base, remote, now=now)
        joined = '\n'.join(frame.lines)
        self.assertIn('last restart: no single lead', joined)
        self.assertIn('2 conflicts: memory/plan.md', joined)
        extras = overview.more_actions(base, frame.actions, now=now)
        extra_keys = {a.key for a in extras}
        self.assertTrue({'logs', 'disconnect', 'stop-worker', 'restart', 'hold', 'disarm'} <= extra_keys)

        in_cs = replace(base, in_codespace=True, mac_link_live=True, mac_stop_live=True)
        cs_frame = overview.view(in_cs, None, now=now)
        self.assertEqual(len(cs_frame.lines), 2)
        self.assertIn('connected (links & remote stop ready)', cs_frame.lines[1])

    def test_status_rows_show_conflict_paths_issues_and_http_401_403(self):
        from devcontainer_setup.project_sync import workspace
        now = 1_760_000_000.0
        for code, expected in ((401, 'HTTP 401'), (403, 'HTTP 403')):
            value = {
                'worker': {'running': True, 'phase': 'syncing'},
                'scratch': {'phase': 'conflicts', 'conflicts': ['memory/a.md', 'tmp/b.md']},
                'conversations': {
                    'outcome': 'degraded',
                    'directions': {
                        'desktop_to_codespace': {'counts': {'received': 0, 'available': 0, 'pending': 0, 'unsupported': 0}},
                        'codespace_to_desktop': {'counts': {'received': 1, 'available': 0, 'pending': 1, 'unsupported': 0}},
                    },
                    'issues': [{'agent': 'claude', 'session_id': '12345678-aaaa', 'reason': 'refused inbound'}],
                },
                'recovery': {
                    'arming': {'armed': True, 'gh_user': 'me', 'ports': []},
                    'keeper': {'running': True, 'pid': 99, 'heartbeat_age': 5, 'phase': 'waiting',
                               'reading': {'at': now, 'ok': False, 'http': code}},
                    'restarts': 0, 'now': now,
                },
            }
            text = workspace.status_text(value)
            self.assertIn('memory/a.md', text)
            self.assertIn('Issue (claude 12345678): refused inbound', text)
            self.assertIn(expected, text)

    def test_restore_returns_blocked_immediately_and_includes_restart_resume_hints(self):
        from devcontainer_setup.project_sync import restore
        sleeps = []
        herdr = Mock()
        herdr.agents.return_value = [{'name': 'lead-proj', 'agent_status': 'blocked', 'interactive_ready': True}]
        outcome = restore.resume(herdr, 'r-1', 'hello', now=lambda: 100.0, sleep=sleeps.append)
        self.assertEqual(outcome, ('blocked', 'lead-proj'))
        self.assertEqual(sleeps, [])
        herdr.prompt.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.scratch').mkdir()
            (root / '.scratch/restart-resume.json').write_text(json.dumps({
                'root_conversation_id': '422c1d4b-c5c0-417f-b6a4-5829eee90fcd',
                'services': [':19632', ':19680'],
            }))
            msg = restore.resume_text('r-1', 1000.0, 1060.0, root=root)
            self.assertIn('422c1d4b-c5c0-417f-b6a4-5829eee90fcd', msg)
            self.assertIn(':19632, :19680', msg)

    def test_signins_opencode_provider_is_configurable(self):
        from devcontainer_setup.project_sync import signins
        tool = next(t for t in signins.TOOLS if t.key == 'opencode')
        with patch.dict(os.environ, {'DEVCONTAINER_OPENCODE_PROVIDER': 'anthropic',
                                     'DEVCONTAINER_OPENCODE_METHOD': 'api-key'}, clear=False):
            self.assertEqual(signins._login_argv(tool), ('opencode', 'auth', 'login', 'anthropic', '--method', 'api-key'))
            self.assertEqual(signins._classify('opencode', 0, json.dumps([{'id': 'anthropic'}]), ''), ('signed-in', None))
            self.assertEqual(signins._classify('opencode', 0, json.dumps([{'id': 'openai'}]), ''), ('signed-out', None))

    def test_root_taskfile_includes_work_signin_and_setup_doctor(self):
        text = (ROOT / 'Taskfile.yml').read_text()
        self.assertIn('work:signin:', text)
        self.assertIn('setup:doctor:', text)

    def test_standing_refusals_reads_both_directions(self):
        from devcontainer_setup.project_sync import standing
        snap = 'a' * 64
        project = Mock(project_id='pid', identity='github.com/o/r', root=Path('/tmp/r'))
        binding = {'codespace': 'cs', 'remote_root': '/workspaces/r'}
        runtime = {
            'binding': binding,
            'conversations': {
                'schema': 3, 'completed_at': 100.0, 'project_id': 'pid', 'identity': 'github.com/o/r',
                'local_root': '/tmp/r', 'remote_root': '/workspaces/r',
                'directions': {
                    'desktop_to_codespace': {
                        'snapshot_id': snap,
                        'receipt': {'snapshot_id': snap, 'received_at': '2026-11-22T00:00:00+00:00'},
                        'publication_issues': [{'agent': 'codex', 'session_id': 'sess-out', 'reason': 'outbound refused'}],
                    },
                    'codespace_to_desktop': {
                        'snapshot_id': snap,
                        'receipt': {'snapshot_id': snap, 'received_at': '2026-11-22T00:00:00+00:00'},
                        'publication_issues': [{'agent': 'claude', 'session_id': 'sess-in', 'reason': 'inbound refused'}],
                    },
                },
            },
        }
        reader = standing._Reader()
        refs = standing._refusals(runtime, project, binding, reader)
        self.assertEqual([(r.agent, r.session_id, r.direction) for r in refs], [
            ('codex', 'sess-out', 'desktop_to_codespace'),
            ('claude', 'sess-in', 'codespace_to_desktop'),
        ])

    def test_keeper_distinguishes_http_401_and_403_errors(self):
        from devcontainer_setup.project_sync import keeper, lifecycle
        for http_code, expected in ((401, 'gh auth login'), (403, 'gh auth refresh -s codespace')):
            with self.subTest(http_code=http_code):
                fake_api = Mock()
                fake_api.name = 'cs-1'
                fake_api.read.return_value = lifecycle.Reading(100.0, False, None, None, None, http_code)
                obj = object.__new__(keeper.Keeper)
                obj.state = Path('/tmp/state')
                obj.project = 'proj'
                obj.api = fake_api
                obj.now = lambda: 100.0
                obj.done = False
                obj._deleted_read_at = None
                obj.runtime = {'phase': 'watching', 'down': {'count': 0, 'first_at': None}, 'notified': {}}
                obj._refusal = lambda value: None
                with patch.object(keeper.arming, 'read', return_value={'armed': True, 'codespace': 'cs-1'}):
                    obj._pass()
                self.assertIn(expected, obj.runtime['error'])


class Area4And5SyncAndDocsTests(unittest.TestCase):
    def test_matching_root_matches_subdirectories_and_rejects_siblings(self):
        from devcontainer_setup.project_sync import conversations
        roots = ['/workspaces/app', '/workspaces/app-wt']
        self.assertEqual(conversations._matching_root('/workspaces/app/packages/web', roots), '/workspaces/app')
        self.assertEqual(conversations._matching_root('/workspaces/app-wt/src', roots), '/workspaces/app-wt')
        self.assertIsNone(conversations._matching_root('/workspaces/app-other/src', roots))

    def test_backup_pruning_keeps_newest_max_backups(self):
        from devcontainer_setup.project_sync import scratch
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            bdir = state / 'backups'
            bdir.mkdir()
            for i in range(15):
                archive = bdir / f'{1000 + i:05d}-aaaa-local.tar.gz'
                archive.write_bytes(b'x')
                archive.with_suffix('.tar.gz.sha256').write_text('0\n')
                os.utime(archive, (1000 + i, 1000 + i))
            removed = scratch._prune_backups(state, keep=10)
            self.assertEqual(removed, 5)
            self.assertEqual(len(list(bdir.glob('*.tar.gz'))), 10)

    def test_docs_cover_mental_model_prerequisites_and_troubleshooting(self):
        readme = (ROOT / 'README.md').read_text()
        reference = (ROOT / 'docs/reference.md').read_text()
        continuity = (ROOT / 'docs/project-continuity.md').read_text()
        self.assertIn('## 5-minute mental model', readme)
        self.assertIn('## Mac prerequisites & bootstrap', readme)
        self.assertIn('gh auth refresh -s codespace', readme)
        self.assertIn('task work:cs:delete', readme)
        self.assertIn('task devcontainer:doctor', readme)
        self.assertIn('Python, Node, uv', readme)
        self.assertIn('Canonical catalog', reference)
        self.assertIn('Generated projects', reference)
        self.assertIn('HTTP 401', reference)
        self.assertIn('restart-resume.json', continuity)
        self.assertIn('DEVCONTAINER_OPENCODE_PROVIDER', continuity)


if __name__ == '__main__':
    unittest.main()
