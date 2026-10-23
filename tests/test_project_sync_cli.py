"""Exercise the distributed command entrypoint, including isolated execution."""
import contextlib
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

    def armed_step(self, foreign=(), port=(), no_port=()):
        from devcontainer_setup.project_sync import __main__ as cli, keeper
        from devcontainer_setup.project_sync.state import atomic_write
        mock = self.mock
        self.args.port, self.args.no_port = list(port), list(no_port)
        atomic_write(self.state / 'scratch.json', json.dumps({'codespace': 'cs-1', 'root': str(self.project.root)}).encode())
        started = {'pid': 4121, 'nonce': 'n' * 48, 'phase': 'starting'}
        with mock.patch.object(keeper, 'foreign_processes', return_value=list(foreign)) as scan, \
                mock.patch.object(keeper, 'start', return_value=started) as start, \
                mock.patch.object(keeper, 'stop') as stop, \
                mock.patch.object(keeper, 'wait_ready', return_value=dict(started, phase='waiting')) as ready, \
                mock.patch('devcontainer_setup.project_sync.arming.account', return_value='octo'), \
                mock.patch('devcontainer_setup.project_sync.codespaces.Codespaces') as spaces, \
                mock.patch('builtins.input', return_value='arm') as ask, \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            spaces.return_value.space.return_value = self.row
            order = mock.Mock()
            order.attach_mock(stop, 'stop')
            order.attach_mock(start, 'start')
            cli._armed(self.project, self.state, self.args)
        scan.assert_called_once_with(self.state, 'cs-1')
        self.order = [name for name, *_ in order.mock_calls]
        return start, ready, ask, out.getvalue()

    def test_a_port_change_on_an_armed_project_restarts_the_keeper(self):
        # R20: the running keep-alive child carries the old ports; a new keeper starts one with the new list.
        from devcontainer_setup.project_sync import arming
        self.armed_step()
        self.assertEqual(self.order, ['start'])
        for unchanged in ((), (3570,)):
            with self.subTest(port=unchanged):
                # A repeat arm without a change is a no-op: the same keeper is adopted, with its child.
                self.armed_step(port=unchanged)
                self.assertEqual(self.order, ['start'])
        self.armed_step(port=[4000])
        self.assertEqual(self.order, ['stop', 'start'])
        self.assertEqual(arming.read(self.state)['ports'], [3570, 4000, 8787])
        self.armed_step(no_port=[4000])
        self.assertEqual(self.order, ['stop', 'start'])
        self.assertEqual(arming.read(self.state)['ports'], [3570, 8787])

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


T0 = 1_000_000.0


class OwnerAPI:
    """Replays readings; records each POST with the owner stop arming.json holds at that moment."""

    def __init__(self, state, readings, stop=202, start=200):
        self.state, self.readings, self.reads, self.calls = state, list(readings), 0, []
        self.stop, self.start, self.name = stop, start, 'cs-1'

    def read(self):
        item = self.readings[min(self.reads, len(self.readings) - 1)]
        self.reads += 1
        return item

    def post_stop(self):
        from devcontainer_setup.project_sync import arming
        self.calls.append(('stop', arming.read(self.state).get('owner_stop_at')))
        return self.stop

    def post_start(self):
        self.calls.append(('start', None))
        return self.start


class OwnerHerdr:
    def __init__(self):
        self.prompts, self.status = [], 'idle'

    def reachable(self):
        return True

    def agents(self):
        return [{'pane_id': 'p1'}, {'name': 'lead-proj', 'agent_status': self.status}]

    def prompt(self, name, text):
        self.prompts.append((name, text))
        self.status = 'working'
        return True

    def read(self, name, lines=80):
        return ''


class OwnerPowerTests(unittest.TestCase):
    """work:cs:stop and work:cs:restart with the owner present."""

    def setUp(self):
        from unittest import mock
        from devcontainer_setup.project_sync.state import atomic_write
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.state, root = base / 'state', base / 'proj'
        root.mkdir()
        atomic_write(self.state / 'scratch.json', json.dumps({'codespace': 'cs-1', 'root': str(root)}).encode())
        self.project = mock.Mock(root=root)
        self.clock = T0
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
            os.environ.pop(name, None)

    def advance(self, seconds):
        self.clock += seconds

    def arm(self):
        from devcontainer_setup.project_sync import arming
        arming.arm(self.state, gh_user='octo', codespace='cs-1', consent='x', billable_owner='octo', ports=[],
                   now=T0 - 7200)

    def power(self, api, restart):
        from devcontainer_setup.project_sync import __main__ as cli
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            cli._owner_power(self.project, self.state, restart=restart, api=api, now=lambda: self.clock,
                             sleep=self.advance)
        return out.getvalue()

    def test_codespace_stop_records_the_owner_stop_before_its_post(self):
        from devcontainer_setup.project_sync import arming
        from devcontainer_setup.project_sync.lifecycle import Reading
        api = OwnerAPI(self.state, [Reading(T0, True, 'Available', False, None)])
        out = self.power(api, restart=False)
        self.assertEqual(api.calls, [('stop', T0)])
        self.assertEqual(arming.read(self.state)['owner_stop_at'], T0)
        self.assertIn('stays stopped', out)
        # R33: every failed stop keeps the record, so the keeper never restarts what you asked to stop.
        for status in (403, 409):
            self.clock += 100
            with self.assertRaisesRegex(ValueError, rf'GitHub refused the stop request \(HTTP {status}\); it is '
                                                    'recorded as your stop, so the keeper will not restart it. '
                                                    'Check task work:status.'):
                self.power(OwnerAPI(self.state, [], stop=status), restart=False)
            self.assertEqual(arming.read(self.state)['owner_stop_at'], self.clock)
        self.clock = T0 + 300
        with self.assertRaisesRegex(ValueError, 'HTTP 0'):
            self.power(OwnerAPI(self.state, [], stop=0), restart=False)
        self.assertEqual(arming.read(self.state)['owner_stop_at'], T0 + 300)

    def test_a_refused_stop_during_the_12_hour_stop_is_never_restarted(self):
        # The reviewer's owner_4xx_undo: work:cs:stop while GitHub's own stop is under way, and
        # GitHub refuses the request (409) or its answer is lost (0). The keeper must not restart it.
        from devcontainer_setup.project_sync import keeper, lifecycle
        from devcontainer_setup.project_sync.lifecycle import Reading
        for status in (409, 403, 0):
            with self.subTest(status=status):
                self.setUp()
                self.arm()
                anchor = T0 - 3600
                state = {'now': 'Available'}

                class Codespace(OwnerAPI):
                    def read(api):
                        return Reading(self.clock, True, state['now'], False, anchor)
                api, notes = Codespace(self.state, [], stop=status), []
                k = keeper.Keeper(self.state, api=api, notifier=lambda title, text: notes.append(text) or True,
                                  herdr=OwnerHerdr(), now=lambda: self.clock, sleep=self.advance,
                                  end_proxies=lambda *args: [])
                k.step()
                self.clock = anchor + lifecycle.LIFETIME + 30
                state['now'] = 'ShuttingDown'
                k.step()
                self.advance(30)
                with self.assertRaises(ValueError):
                    self.power(api, restart=False)
                state['now'] = 'Shutdown'
                for _ in range(30):
                    self.advance(60)
                    k.step()
                self.assertEqual([call for call, _ in api.calls], ['stop'])
                self.assertEqual(lifecycle.Ledger(self.state / keeper.LEDGER).rows(), [])
                self.assertEqual(len(notes), 1)
                self.assertIn('was not restarted: you stopped it at', notes[0])

    def test_owner_commands_inside_a_codespace_say_to_run_them_on_the_mac(self):
        os.environ['CODESPACES'] = 'true'
        for restart, task in ((False, 'work:cs:stop'), (True, 'work:cs:restart')):
            with self.subTest(task=task):
                api = OwnerAPI(self.state, [])
                with self.assertRaisesRegex(ValueError, f'Run task {task} on your Mac'):
                    self.power(api, restart=restart)
                self.assertEqual(api.calls, [])

    def test_owner_commands_refuse_environment_tokens(self):
        for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
            for restart in (False, True):
                with self.subTest(name=name, restart=restart):
                    api = OwnerAPI(self.state, [])
                    os.environ[name] = 'ghp_leaked'
                    try:
                        with self.assertRaisesRegex(ValueError, 'Unset ' + name) as caught:
                            self.power(api, restart=restart)
                    finally:
                        del os.environ[name]
                    self.assertNotIn('ghp_leaked', str(caught.exception))
                    self.assertEqual(api.calls, [])
        self.assertFalse((self.state / 'arming.json').exists())

    def test_owner_commands_use_the_armed_account_else_the_active_one(self):
        from unittest import mock
        from devcontainer_setup.project_sync import __main__ as cli, lifecycle
        for armed, user in ((False, None), (True, 'octo')):
            with self.subTest(armed=armed):
                if armed:
                    self.arm()
                with mock.patch.object(lifecycle, 'LifecycleAPI', return_value=OwnerAPI(self.state, [])) as made, \
                        contextlib.redirect_stdout(io.StringIO()):
                    cli._owner_power(self.project, self.state, restart=False, now=lambda: self.clock)
                made.assert_called_once_with('cs-1', user)

    def test_planned_restart_skips_budget_and_resumes(self):
        from devcontainer_setup.project_sync import arming, keeper, lifecycle, restore
        from devcontainer_setup.project_sync.lifecycle import Reading
        self.arm()
        # One keeper restart two hours ago: the planned one must leave the budget at 1 of 2.
        lifecycle.Ledger(self.state / keeper.LEDGER, now=lambda: T0 - 7200).append('intent', 'r-000001Z', post=1)
        lifecycle.Ledger(self.state / keeper.LEDGER, now=lambda: T0 - 7190).append('result', 'r-000001Z', post=1,
                                                                                status=200)
        rows_before = lifecycle.Ledger(self.state / keeper.LEDGER).rows()
        api = OwnerAPI(self.state, [Reading(T0, True, 'Available', False, None),
                                    Reading(T0, True, 'ShuttingDown', False, None),
                                    Reading(T0, True, 'ShuttingDown', False, None),
                                    Reading(T0, True, 'Shutdown', False, None),
                                    Reading(T0, True, 'Starting', False, None),
                                    Reading(T0, True, 'Available', False, None)])
        out = self.power(api, restart=True)
        self.assertEqual(api.calls, [('stop', T0), ('start', None)])
        value = arming.read(self.state)
        pending = value['pending_restore']
        self.assertTrue(pending['rid'].startswith(lifecycle.OWNER_RID))
        self.assertEqual(pending['stopped_at'], T0)
        self.assertEqual(pending['back_at'], self.clock)
        self.assertIn(pending['rid'], out)
        self.assertIn('task work:logs', out)
        # Outside the budget: no ledger rows, and the gate answers as before.
        rows = lifecycle.Ledger(self.state / keeper.LEDGER).rows()
        self.assertEqual(rows, rows_before)
        self.assertEqual(lifecycle.restart_rids(rows, self.clock), ['r-000001Z'])
        self.assertIsNone(lifecycle.gate(arming=value, rows=rows, now=self.clock))
        # The keeper reads the stop as the owner's, then restores and resumes the planned rid once.
        herdr, notes = OwnerHerdr(), []
        k = keeper.Keeper(self.state, api=OwnerAPI(self.state, [Reading(self.clock + 30, True, 'Available', False,
                                                                        self.clock - 60)]),
                          notifier=lambda title, text: notes.append(text) or True, herdr=herdr,
                          now=lambda: self.clock, sleep=self.advance, end_proxies=lambda *args: [])
        for _ in range(3):
            self.advance(60)
            k.step()
        self.assertEqual(len(herdr.prompts), 1)
        self.assertEqual(herdr.prompts[0], ('lead-proj', restore.resume_text(pending['rid'], T0, pending['back_at'])))
        self.assertEqual(len(notes), 1)
        self.assertIn('back from your restart', notes[0])
        self.assertEqual(lifecycle.Ledger(self.state / keeper.LEDGER).rows(), rows_before)

    def test_unarmed_restart_prints_the_times_and_hands_nothing_on(self):
        from devcontainer_setup.project_sync import arming
        from devcontainer_setup.project_sync.lifecycle import Reading
        api = OwnerAPI(self.state, [Reading(T0, True, 'ShuttingDown', False, None),
                                    Reading(T0, True, 'Shutdown', False, None),
                                    Reading(T0, True, 'Available', False, None)])
        out = self.power(api, restart=True)
        self.assertEqual([call for call, _ in api.calls], ['stop', 'start'])
        self.assertIn('restarted (o-', out)
        self.assertIn(arming.format_time(T0), out)
        self.assertNotIn('pending_restore', arming.read(self.state))

    def test_a_failed_planned_start_names_its_outcome(self):
        from devcontainer_setup.project_sync.lifecycle import Reading
        api = OwnerAPI(self.state, [Reading(T0, True, 'Shutdown', False, None)], start=403)
        with self.assertRaisesRegex(ValueError, 'HTTP 403'):
            self.power(api, restart=True)


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
        # A ps that outlives its 5 s timeout while the keeper's ownership is checked.
        tools = self.root.parent / 'slow-ps'
        tools.mkdir()
        (tools / 'ps').write_text('#!/bin/sh\nexec sleep 30\n')
        (tools / 'ps').chmod(0o700)
        atomic_write(state / 'slow-keeper-runtime.json',
                     json.dumps({'pid': 4242, 'nonce': 'n' * 48, 'process_identity': 'x'}).encode())
        slow_ps = mock.patch.dict(os.environ, {'PATH': str(tools) + os.pathsep + os.environ['PATH']})
        slow_runtime = mock.patch.object(keeper, 'RUNTIME', 'slow-keeper-runtime.json')
        for name, failure in (('hung', hung), ('corrupt', corrupt), ('ps times out', slow_ps)):
            with self.subTest(name), failure, slow_runtime if name == 'ps times out' else contextlib.nullcontext(), \
                    mock.patch.dict(os.environ, {'XDG_STATE_HOME': self.env['XDG_STATE_HOME']}), \
                    mock.patch.object(scratch, 'stop', return_value={'running': False, 'phase': 'stopped'}) as worker_stop, \
                    mock.patch('sys.stdout', new_callable=io.StringIO) as out, \
                    mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                self.assertEqual(main(['--project', str(self.root), 'work', '--stop']), 1)
            worker_stop.assert_called_once_with(state)
            self.assertFalse(json.loads(out.getvalue())['running'])
            self.assertIn('the keeper was not', err.getvalue())
            self.assertNotIn('Traceback', err.getvalue())

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
