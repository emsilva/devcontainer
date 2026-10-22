"""Exercise the distributed command entrypoint, including isolated execution."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from devcontainer_setup.project_sync.__main__ import parser

SOURCE = Path(__file__).resolve().parents[1]


class ArmFlowTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state'
        root = Path(self.temp.name).resolve() / 'proj'
        (root / '.devcontainer').mkdir(parents=True)
        (root / '.devcontainer/devcontainer.json').write_text('{"forwardPorts": [2222, 3570, 8787]}')
        self.project = mock.Mock(root=root)
        self.row = {'name': 'cs-1', 'billable_owner': {'login': 'octo'}}
        self.mock = mock
        self.args = mock.Mock(port=[], no_port=[])

    def arm(self, answer='arm', row='default', **flags):
        from devcontainer_setup.project_sync import __main__ as cli
        mock = self.mock
        self.args.port, self.args.no_port = flags.get('port', []), flags.get('no_port', [])
        row = self.row if row == 'default' else row
        with mock.patch('devcontainer_setup.project_sync.arming.account', return_value='octo'), \
                mock.patch('devcontainer_setup.project_sync.codespaces.Codespaces') as spaces, \
                mock.patch('builtins.input', side_effect=answer if isinstance(answer, BaseException) else [answer]) as ask:
            spaces.return_value.space.return_value = row
            result = cli._arm(self.project, self.state, self.args)
        return result, ask

    def test_confirmation_arms_with_default_ports_and_flags(self):
        from devcontainer_setup.project_sync import arming
        self.arm(port=[3571], no_port=[8787])
        value = arming.read(self.state)
        self.assertTrue(value['armed'])
        self.assertEqual(value['ports'], [3570, 3571])
        self.assertEqual(value['codespace'], 'cs-1')

    def test_wrong_answer_cancel_pairing_and_owner_refuse(self):
        from devcontainer_setup.project_sync import arming
        with self.assertRaisesRegex(ValueError, 'Not armed'):
            self.arm(answer='yes')
        with self.assertRaisesRegex(ValueError, 'Arming cancelled'):
            self.arm(answer=EOFError())
        with self.assertRaisesRegex(ValueError, 'Pair a Codespace'):
            self.arm(row=None)
        with self.assertRaisesRegex(ValueError, 'billing owner'):
            self.arm(row={'name': 'cs-1'})
        self.assertFalse(arming.read(self.state)['armed'])

    def test_already_armed_skips_prompt_and_updates_ports(self):
        from devcontainer_setup.project_sync import arming
        self.arm()
        result, ask = self.arm(answer='unused', port=[4000])
        ask.assert_not_called()
        self.assertEqual(arming.read(self.state)['ports'], [3570, 4000, 8787])

    def test_changed_codespace_asks_again(self):
        self.arm()
        _, ask = self.arm(row={'name': 'cs-2', 'billable_owner': {'login': 'octo'}})
        ask.assert_called_once()

    def armed_step(self, foreign=()):
        from devcontainer_setup.project_sync import __main__ as cli, keeper
        from devcontainer_setup.project_sync.state import atomic_write
        mock = self.mock
        atomic_write(self.state / 'scratch.json', json.dumps({'codespace': 'cs-1', 'root': str(self.project.root)}).encode())
        started = {'pid': 4121, 'nonce': 'n' * 48, 'phase': 'starting'}
        with mock.patch.object(keeper, 'foreign_processes', return_value=list(foreign)) as scan, \
                mock.patch.object(keeper, 'start', return_value=started) as start, \
                mock.patch.object(keeper, 'wait_ready', return_value=dict(started, phase='waiting')) as ready, \
                mock.patch('devcontainer_setup.project_sync.arming.account', return_value='octo'), \
                mock.patch('devcontainer_setup.project_sync.codespaces.Codespaces') as spaces, \
                mock.patch('builtins.input', return_value='arm') as ask, \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            spaces.return_value.space.return_value = self.row
            cli._armed(self.project, self.state, self.args)
        scan.assert_called_once_with(self.state, 'cs-1')
        return start, ready, ask, out.getvalue()

    def test_armed_step_refuses_a_foreign_gh_process_before_consent(self):
        from devcontainer_setup.project_sync import arming
        with self.assertRaisesRegex(ValueError, 'pid 404'):
            self.armed_step(foreign=[404])
        self.assertFalse(arming.read(self.state)['armed'])

    def test_armed_step_consents_starts_the_keeper_and_prints_status(self):
        from devcontainer_setup.project_sync import arming
        start, ready, ask, out = self.armed_step()
        ask.assert_called_once()
        self.assertTrue(arming.read(self.state)['armed'])
        start.assert_called_once_with(self.project.root, self.state)
        ready.assert_called_once_with(self.state, 'n' * 48)
        self.assertIn('Armed as octo', out)
        self.assertIn('Recovery     armed since', out)
        self.assertIn('Keeper ', out)

    def test_port_range(self):
        with self.assertRaises(SystemExit):
            parser().parse_args(['work', '--armed', '--port', '60000'])


class ProjectSyncCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'remote', 'add', 'origin',
                        'git@github.com:example/project.git'], check=True)
        self.state = Path(self.temp.name).resolve() / 'state'
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.state))

    def run_cli(self, *args):
        return subprocess.run(['bash', str(SOURCE / 'scripts/project-sync.sh'),
                               '--project', str(self.root), *args], env=self.env,
                              capture_output=True, text=True)

    def test_readonly_status_does_not_create_state_or_load_checkout_python(self):
        (self.root / 'sitecustomize.py').write_text('raise RuntimeError("checkout injection")\n')
        result = self.run_cli('scratch', 'status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['phase'], 'stopped')
        self.assertFalse(self.state.exists())

    def test_armed_requires_a_terminal(self):
        result = self.run_cli('work', '--armed')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Work entrypoints require a terminal', result.stderr)

    def test_hold_and_disarm_edit_arming_state(self):
        result = self.run_cli('work', '--hold', '09:30')
        self.assertEqual(result.returncode, 1)
        self.assertIn('not armed', result.stderr)
        files = list(self.state.rglob('arming.json'))
        self.assertEqual(files, [])
        result = self.run_cli('work', '--disarm')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hold_is_stored_for_an_armed_project(self):
        from devcontainer_setup.project_sync import arming
        from devcontainer_setup.project_sync.state import discover_project, state_dir
        state = state_dir(discover_project(self.root), self.env)
        arming.arm(state, gh_user='octo', codespace='c', consent='x', billable_owner='octo', ports=[], now=1.0)
        self.assertEqual(self.run_cli('work', '--hold', '09:30').returncode, 0)
        self.assertIsNotNone(arming.read(state)['hold_until'])
        self.assertEqual(self.run_cli('work', '--hold', 'off').returncode, 0)
        self.assertIsNone(arming.read(state)['hold_until'])
        self.assertEqual(self.run_cli('work', '--disarm').returncode, 0)
        self.assertFalse(arming.read(state)['armed'])

    def test_logs_without_logs_say_so_and_create_nothing(self):
        result = self.run_cli('work', '--logs')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No keeper or worker log yet', result.stdout)
        self.assertFalse(self.state.exists())

    def test_logs_merge_keeper_and_worker_lines_by_time(self):
        from devcontainer_setup.project_sync.state import atomic_write, discover_project, state_dir
        state = state_dir(discover_project(self.root), self.env)
        atomic_write(state / 'keeper.log', b'2026-10-22 01:00:00,000Z 11 Keeper started\n'
                                           b'2026-10-22 01:02:00,000Z 11 Codespace Shutdown\n')
        old = ''.join('Old worker line %d\n' % i for i in range(100))
        atomic_write(state / 'scratch.log', old.encode() + b'2026-10-22 01:01:00,000Z 22 Scratch connection established.\n')
        result = self.run_cli('work', '--logs')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 82)  # the last 80 worker lines and both keeper lines
        self.assertEqual(lines[-3:], ['keeper 2026-10-22 01:00:00,000Z 11 Keeper started',
                                      'worker 2026-10-22 01:01:00,000Z 22 Scratch connection established.',
                                      'keeper 2026-10-22 01:02:00,000Z 11 Codespace Shutdown'])
        self.assertEqual(lines[0], 'worker Old worker line 21')

    def test_stop_stops_the_worker_even_when_the_keeper_cannot_be_stopped(self):
        from unittest import mock
        from devcontainer_setup.project_sync import keeper, scratch
        from devcontainer_setup.project_sync.__main__ import main
        from devcontainer_setup.project_sync.state import atomic_write, discover_project, state_dir
        state = state_dir(discover_project(self.root), self.env)
        hung = mock.patch.object(keeper, 'stop', side_effect=ValueError('The keeper is still shutting down; '
                                                                         'inspect task work:status before retrying'))
        corrupt = mock.patch.object(keeper, 'RUNTIME', 'corrupt-keeper-runtime.json')
        atomic_write(state / 'corrupt-keeper-runtime.json', b'{not json')
        for name, failure in (('hung', hung), ('corrupt', corrupt)):
            with self.subTest(name), failure, \
                    mock.patch.dict(os.environ, {'XDG_STATE_HOME': self.env['XDG_STATE_HOME']}), \
                    mock.patch.object(scratch, 'stop', return_value={'running': False, 'phase': 'stopped'}) as worker_stop, \
                    mock.patch('sys.stdout', new_callable=io.StringIO) as out, \
                    mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                self.assertEqual(main(['--project', str(self.root), 'work', '--stop']), 1)
            worker_stop.assert_called_once_with(state)
            self.assertFalse(json.loads(out.getvalue())['running'])
            self.assertIn('the keeper was not', err.getvalue())

    def test_disarm_and_stop_also_stop_the_keeper(self):
        from unittest import mock
        from devcontainer_setup.project_sync import arming, keeper, scratch
        from devcontainer_setup.project_sync.__main__ import main
        from devcontainer_setup.project_sync.state import discover_project, state_dir
        state = state_dir(discover_project(self.root), self.env)
        arming.arm(state, gh_user='octo', codespace='c', consent='x', billable_owner='octo', ports=[], now=1.0)
        stopped = {'running': False, 'phase': 'stopped'}
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': self.env['XDG_STATE_HOME']}), \
                mock.patch.object(keeper, 'stop', return_value=stopped) as keeper_stop, \
                mock.patch.object(scratch, 'stop', return_value=stopped) as worker_stop, \
                mock.patch('sys.stdout'):
            self.assertEqual(main(['--project', str(self.root), 'work', '--disarm']), 0)
            keeper_stop.assert_called_once_with(state)
            self.assertFalse(arming.read(state)['armed'])
            self.assertEqual(main(['--project', str(self.root), 'work', '--stop']), 0)
            self.assertEqual(keeper_stop.call_count, 2)
            worker_stop.assert_called_once_with(state)

    def test_missing_pairing_has_actionable_error_without_traceback(self):
        result = self.run_cli('scratch', 'start')
        self.assertEqual(result.returncode, 1)
        self.assertIn('scratch:configure', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_old_handoff_surface_removed(self):
        result = self.run_cli('agent', 'recover')
        self.assertEqual(result.returncode, 2)
        self.assertIn('invalid choice', result.stderr)

    def test_agent_launchers_and_implicit_local_recovery_are_removed(self):
        for args in (('work',), ('work', '--agent', 'codex'),
                     ('work', '--agent', 'claude', '--resume')):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.state.exists())

    def test_work_status_is_offline_and_does_not_create_state(self):
        result = self.run_cli('work', '--status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Sync worker: stopped', result.stdout)
        self.assertIn('History:', result.stdout)
        self.assertLess(len(result.stdout.splitlines()), 9)
        detail = self.run_cli('work', '--status', '--json')
        self.assertEqual(detail.returncode, 0, detail.stderr)
        self.assertIsNone(json.loads(detail.stdout)['last_mac_checkpoint'])
        self.assertFalse(self.state.exists())

    def test_disconnect_is_project_scoped_idempotent_and_needs_no_terminal_or_pairing(self):
        from devcontainer_setup.project_sync.state import discover_project
        project = discover_project(self.root)
        alias, session = 'devcontainer-' + project.project_id, 'project-' + project.project_id
        catalog = self.root.parent / 'machines.json'
        calls = self.root.parent / 'calls.jsonl'
        rows = [
            dict(id='other-project', target='other-alias', session=session, enabled=True),
            dict(id='other-session', target=alias, session='default', enabled=True),
            dict(id='project-one', target=alias, session=session, enabled=True),
            dict(id='project-two', target=alias, session=session, enabled=True),
        ]
        catalog.write_text(json.dumps(rows))
        binary = self.root.parent / 'bin'
        binary.mkdir()
        (binary / 'herdr').write_text('#!' + sys.executable + '\n' + '''import json,os,pathlib,sys
catalog=pathlib.Path(os.environ['TEST_CATALOG'])
rows=json.loads(catalog.read_text())
args=sys.argv[1:]
with open(os.environ['TEST_CALLS'],'a') as stream:stream.write(json.dumps(args)+'\\n')
if args==['machine','list','--json']:print(json.dumps(rows))
elif len(args)==3 and args[:2]==['machine','disable']:
 for row in rows:
  if row['id']==args[2]:row['enabled']=False
 catalog.write_text(json.dumps(rows))
 print('Disabled machine')
else:sys.exit(12)
''')
        (binary / 'herdr').chmod(0o700)
        self.env.update(PATH=str(binary) + os.pathsep + os.environ['PATH'],
                        TEST_CATALOG=str(catalog), TEST_CALLS=str(calls))
        for _ in range(2):
            result = self.run_cli('work', '--disconnect')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('task work:cs', result.stdout)
        actual = json.loads(catalog.read_text())
        self.assertEqual([row['id'] for row in actual if row['enabled']], ['other-project', 'other-session'])
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(commands.count(['machine', 'disable', 'project-one']), 1)
        self.assertEqual(commands.count(['machine', 'disable', 'project-two']), 1)
        self.assertTrue(all(command[:2] in (['machine', 'list'], ['machine', 'disable']) for command in commands))
        self.assertFalse(self.state.exists())

    def test_disconnect_refuses_to_run_in_the_codespace(self):
        self.env['CODESPACES'] = 'true'
        result = self.run_cli('work', '--disconnect')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Mac', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(self.state.exists())

    def test_help_and_invalid_project_never_touch_normal_profiles(self):
        result = self.run_cli('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('scratch', result.stdout)
        (self.root / '.git/config').write_text('[core]\n repositoryformatversion=0\n')
        result = self.run_cli('work', '--status')
        self.assertEqual(result.returncode, 1)
        self.assertIn('origin', result.stderr)
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
