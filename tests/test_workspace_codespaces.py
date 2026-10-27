import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from devcontainer_setup.project_sync.state import Project
from devcontainer_setup.project_sync import lifecycle
from devcontainer_setup.project_sync.codespaces import Codespaces
from devcontainer_setup.project_sync.lifecycle import Reading

class CodespaceAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = Project(self.root, 'git@github.com:o/r.git', 'github.com/o/r', 'a'*24)
    def service(self):
        return Codespaces(self.project, self.root/'private')
    def start(self, readings, statuses=(200,)):
        """Run start_codespace against a scripted client on a fake clock; returns (client, error)."""
        clock = {'t': 1_000_000.0}
        class Client:
            posts, reads = [], 0
            def read(self):
                row = readings[min(self.reads, len(readings) - 1)]
                self.reads += 1
                return Reading(clock['t'], True, row[0], row[1] if len(row) > 1 else False, None)
            def post_start(self):
                self.posts.append(clock['t'])
                return statuses[min(len(self.posts) - 1, len(statuses) - 1)]
        client = Client()
        client.posts = []
        def sleep(seconds):
            clock['t'] += seconds
        real_start = lifecycle.start
        def start(api, **kwargs):
            return real_start(api, now=lambda: clock['t'], sleep=sleep, **kwargs)
        with patch('devcontainer_setup.project_sync.codespaces.lifecycle.LifecycleAPI', return_value=client), \
                patch('devcontainer_setup.project_sync.codespaces.lifecycle.start', side_effect=start):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.service().start_codespace('example')
            except ValueError as error:
                return client, error
        return client, None

    def test_start_during_shutting_down_posts_only_after_shutdown(self):
        client, error = self.start([('ShuttingDown',), ('ShuttingDown',), ('Shutdown', True), ('Shutdown',),
                                    ('Starting',), ('Available',)])
        self.assertIsNone(error)
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.reads, 6)

    def test_start_409_waits_and_posts_again_instead_of_blaming_authentication(self):
        client, error = self.start([('Shutdown',), ('ShuttingDown',), ('Shutdown',), ('Shutdown',), ('Available',)], (409, 200))
        self.assertIsNone(error)
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(client.posts[1] - client.posts[0], lifecycle.RETRY_GAP)

    def test_start_already_available_does_not_post(self):
        client, error = self.start([('Available',)])
        self.assertIsNone(error)
        self.assertEqual(client.posts, [])

    def test_start_remaining_shutdown_times_out_with_last_state(self):
        client, error = self.start([('Shutdown',)])
        self.assertRegex(str(error), 'timed out.*Shutdown')
        self.assertEqual(len(client.posts), 2)

    def test_start_refused_names_status_without_blaming_a_token(self):
        client, error = self.start([('Shutdown',)], (403,))
        self.assertRegex(str(error), 'HTTP 403')
        self.assertEqual(len(client.posts), 1)

    def test_start_without_any_token_fails_at_once(self):
        with patch('devcontainer_setup.project_sync.lifecycle.github_token',
                   side_effect=ValueError('GitHub authentication unavailable; set GH_TOKEN or DEVCONTAINER_GH_USER.')), \
                patch('devcontainer_setup.project_sync.lifecycle.time.sleep', side_effect=AssertionError('no wait')):
            with self.assertRaisesRegex(ValueError, 'authentication unavailable'):
                self.service().start_codespace('example')

    def test_token_only_spaces_strips_host_and_pins_hostname(self):
        service = self.service()
        with patch.dict('os.environ', {'GH_TOKEN': 'secret', 'GH_HOST': 'evil'}, clear=True), \
                patch('devcontainer_setup.project_sync.codespaces.subprocess.run',
                      return_value=Mock(returncode=0, stdout='[{"codespaces": []}]')) as run, \
                patch('subprocess.check_output', return_value='main\n'):
            self.assertEqual(service.spaces(), [])
        call = run.call_args
        self.assertEqual(call.kwargs['env']['GH_TOKEN'], 'secret')
        self.assertNotIn('GH_HOST', call.kwargs['env'])
        self.assertIn('--hostname', call.args[0])
        self.assertIn('github.com', call.args[0])

    def test_discovery_uses_the_selected_account(self):
        service = self.service()
        calls = []
        def command(args, **kwargs):
            calls.append((args, kwargs))
            if args[1:3] == ['auth', 'token']:
                self.assertIn('alternate', args)
                self.assertNotIn('GITHUB_TOKEN', kwargs['env'])
                return Mock(returncode=0, stdout='selected-token\n')
            self.assertEqual(kwargs['env']['GH_TOKEN'], 'selected-token')
            return Mock(returncode=0, stdout='[]')
        with patch.dict('os.environ', {'DEVCONTAINER_GH_USER': 'alternate', 'GITHUB_TOKEN': 'wrong'}, clear=True), patch('subprocess.run', side_effect=command), patch('subprocess.check_output', return_value='main\n'):
            service.spaces()
        self.assertEqual(sum(args[1:3] == ['auth', 'token'] for args, _ in calls), 1)

    def test_paginated_match_and_bound_space_survives_branch_change(self):
        service = self.service()
        row = {'name': 'example', 'repository': {'full_name': 'o/r'}, 'git_status': {'ref': 'old'}}
        def command(args, **kwargs):
            self.assertIn('--paginate', args) if '--slurp' in args else None
            return Mock(returncode=0, stdout=json.dumps([{'codespaces': []}, {'codespaces': [row]}]) if '--slurp' in args else json.dumps(row))
        with patch('devcontainer_setup.project_sync.codespaces.github_token', return_value='secret'), patch('subprocess.run', side_effect=command) as run, patch('subprocess.check_output', return_value='old\n'), patch.object(service, 'binding', return_value={'codespace': 'example'}):
            self.assertEqual(service.spaces(), [row])
            with patch('subprocess.check_output', return_value='new\n'):
                self.assertEqual(service.space(), row)
            self.assertTrue(all('POST' not in call.args[0] for call in run.call_args_list))
            row['repository']['full_name'] = 'alien/repo'
            with self.assertRaisesRegex(ValueError, 'repository'):
                service.space()

    def test_repository_inventory_does_not_filter_or_query_local_branch(self):
        service = self.service()
        rows = [dict(name='space', repository={'full_name':'o/r'}, git_status={'ref':'other-branch'})]
        with patch.object(service, '_api_env', return_value={}), patch.object(service, '_api', return_value=[{'codespaces':rows}]), patch('subprocess.check_output', side_effect=AssertionError('branch must be manual')):
            self.assertEqual(service.spaces(), rows)

    def test_generated_worktrees_are_excluded_from_remote_checkout_inventory(self):
        service = self.service()
        connection = Mock(); connection.__enter__ = Mock(return_value=connection); connection.__exit__ = Mock(return_value=None)
        connection.python.return_value = json.dumps([['/workspaces/ordinary','https://github.com/o/r'], ['/workspaces/.devcontainer-work/p/s','https://github.com/o/r']]).encode()
        with patch('devcontainer_setup.project_sync.codespaces.CodespaceConnection', return_value=connection):
            self.assertEqual(service.roots('space'), ['/workspaces/ordinary'])

    def test_inventory_output_and_request_time_are_bounded(self):
        import subprocess
        service = self.service()
        with patch('devcontainer_setup.project_sync.codespaces.github_token', return_value='secret'), patch('subprocess.run', return_value=Mock(returncode=0, stdout='x' * (2 * 1024 * 1024 + 1))):
            with self.assertRaisesRegex(ValueError, 'exceeds limit'):
                service.spaces()
        with patch('devcontainer_setup.project_sync.codespaces.github_token', return_value='secret'), patch('subprocess.run', side_effect=subprocess.TimeoutExpired(['gh'], 30)):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                service.spaces()

    def test_start_terminal_states_fail_without_posting(self):
        for state in ('Failed', 'Unavailable', 'Deleted', 'Archived'):
            with self.subTest(state=state):
                client, error = self.start([(state,)])
                self.assertRegex(str(error), state)
                self.assertEqual(client.posts, [])

    def test_start_terminal_state_after_post_fails_once(self):
        client, error = self.start([('Shutdown',), ('Failed',)])
        self.assertRegex(str(error), 'Failed')
        self.assertEqual(len(client.posts), 1)


class RecoveryStatusTests(unittest.TestCase):
    def test_status_names_a_deleted_codespace_for_an_armed_project(self):
        # #60: a keeper-confirmed deletion names the rebind and disarm choices.
        lines = self.lines(phase='deleted', caffeinate=None,
                           reading={'at': self.now, 'ok': True, 'state': 'Deleted', 'http': 404})
        self.assertIn('Codespace    cs-1 no longer exists on GitHub (deleted). Pair another with '
                      'task work:cs:armed -- --rebind <name>, or run task work:disarm.', lines)

    def test_status_names_a_deleted_codespace_for_an_unarmed_project(self):
        # #60: unarming changes the rebind command on the retained keeper reading.
        from devcontainer_setup.project_sync import arming
        arming.write(self.state, dict(arming.read(self.state), armed=False))
        lines = self.lines(phase='deleted', caffeinate=None,
                           reading={'at': self.now, 'ok': True, 'state': 'Deleted', 'http': 404})
        self.assertIn('Codespace    cs-1 no longer exists on GitHub (deleted). Pair another with '
                      'task work:cs -- --rebind <name>.', lines)

    def setUp(self):
        import time
        from devcontainer_setup.project_sync import arming
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name).resolve() / 'state'
        self.now = time.time()
        arming.arm(self.state, gh_user='octo', codespace='cs-1', consent='x', billable_owner='octo',
                   ports=[3570, 8787], now=self.now - 7200)

    def lines(self, running=True, **runtime):
        from devcontainer_setup.project_sync import keeper, workspace
        from devcontainer_setup.project_sync.state import atomic_write
        value = dict({'pid': 4121, 'nonce': 'n', 'codespace': 'cs-1', 'phase': 'watching', 'heartbeat_at': self.now - 12,
                      'caffeinate': 4122, 'reading': {'at': self.now - 12, 'ok': True, 'state': 'Available',
                                                      'pending': False}}, **runtime)
        atomic_write(self.state / keeper.RUNTIME, json.dumps(value).encode())
        with patch.object(keeper, '_owned', return_value=running):
            return workspace.recovery_lines(workspace.recovery(self.state))

    def test_status_block_shows_due_time_in_both_zones(self):
        from devcontainer_setup.project_sync import arming
        anchor = self.now - 3600
        lines = self.lines(anchor={'at': anchor, 'source': 'observed'})
        self.assertEqual([line.split()[0] for line in lines], ['Recovery', 'Keeper', 'Codespace', 'Keep-alive', 'Last'])
        self.assertIn('armed since ' + arming.format_time(self.now - 7200) + ' as octo, no hold', lines[0])
        self.assertIn('restarts 0 of 2 in 24 h', lines[0])
        # The heartbeat is 12 s old at setUp; a loaded machine may add a second or two.
        self.assertRegex(lines[1], r'running \(pid 4121\), heartbeat 1[2-5] s, caffeinate held')
        expected = ('Available; 12-hour stop expected ~' + arming.short_time(anchor + lifecycle.LIFETIME)
                    + ', counted from its last start at ' + arming.short_time(anchor))
        self.assertRegex(arming.short_time(anchor), r'^\d\d:\d\d$')
        self.assertIn(expected, lines[2])
        self.assertNotIn('anchor', lines[2])
        self.assertIn('13570 18787', lines[3])
        self.assertTrue(lines[4].startswith('Last restart'))

    def test_status_shows_each_kept_start_as_an_alternative(self):
        # #61: each kept start is an alternative 12-hour stop, earliest first.
        from devcontainer_setup.project_sync import arming
        at, also = self.now - 3600, self.now - 1800
        lines = self.lines(anchor={'at': at, 'source': 'evidence', 'also': [also]})
        expected = ('Codespace    Available; 12-hour stop expected ~' + arming.short_time(at + lifecycle.LIFETIME)
                    + ' (or ~' + arming.short_time(also + lifecycle.LIFETIME) + ')'
                    + ', counted from its last start at ' + arming.short_time(at))
        self.assertIn(expected, lines[2])

    def test_an_active_restart_is_named_and_only_a_restarting_stop_stops_pointing_at_work_cs(self):
        shutdown = {'at': self.now - 180, 'ok': True, 'state': 'Shutdown', 'pending': False}
        down = {'count': 3, 'first_at': self.now - 300, 'kind': 'LIFETIME'}
        outcome = {'rid': 'r-010203Z', 'at': self.now - 100, 'stopped_at': self.now - 300, 'outcome': 'restarting'}
        for phase in ('restarting', 'restoring', 'resuming'):
            with self.subTest(phase=phase):
                # _restart rewrites the reading to Available before it restores and resumes.
                reading = shutdown if phase == 'restarting' else dict(shutdown, state='Available')
                lines = self.lines(phase=phase, reading=reading, down=down, last_restart=outcome,
                                   anchor={'at': self.now - 60, 'source': 'keeper'})
                self.assertRegex(lines[1], r'caffeinate held; ' + phase + ' r-010203Z$')
                if phase == 'restarting':
                    self.assertIn("(GitHub's 12-hour stop); the keeper is restarting it (r-010203Z), "
                                  'follow task work:logs', lines[2])
                    self.assertNotIn('work:cs', lines[2])
                else:
                    self.assertTrue(lines[2].startswith('Codespace    Available; 12-hour stop expected ~'), lines[2])

    def test_keep_alive_line_shows_skipped_ports_and_what_to_run(self):
        holding = self.lines(keepalive={'pid': 5402, 'ports_up': [[13570, 3570]],
                                        'ports_skipped': [[18787, 812], [13571, None]]})
        self.assertEqual(holding[3], 'Keep-alive   holding (pid 5402); ports 13570; 18787 in use by pid 812, '
                                     '13571 in use')
        failing = self.lines(keepalive={'pid': None, 'error': 'the connection exited with status 255'})
        self.assertEqual(failing[3], 'Keep-alive   not holding (the connection exited with status 255); '
                                     'ports 13570 18787; run task work:logs for the reason')
        unknown = self.lines(reading={'at': self.now, 'ok': False, 'state': None, 'pending': None})
        self.assertEqual(unknown[3], 'Keep-alive   not holding; ports 13570 18787; run task work:logs for the reason')
        down = self.lines(reading={'at': self.now, 'ok': True, 'state': 'Shutdown', 'pending': False},
                          keepalive={'pid': None, 'error': None})
        self.assertEqual(down[3], 'Keep-alive   not holding while the Codespace is Shutdown; ports 13570 18787')
        stopped = self.lines(running=False, keepalive={'pid': 5402, 'ports_up': [[13570, 3570]]})
        self.assertEqual(stopped[3], 'Keep-alive   not holding; ports 13570 18787; run task work:cs:armed')

    def test_lines_that_are_not_fine_name_the_command(self):
        stopped = self.lines(running=False)
        self.assertIn('not running; run task work:cs:armed', stopped[1])
        stale = self.lines(heartbeat_at=self.now - 400)
        self.assertIn('task work:cs:armed', stale[1])
        other = self.lines(reading={'at': self.now, 'ok': True, 'state': 'Shutdown', 'pending': False},
                           down={'count': 4, 'first_at': self.now - 240, 'kind': 'OTHER'})
        self.assertIn('run task work:cs to start it', other[2])
        unreadable = self.lines(reading={'at': self.now, 'ok': False, 'state': None, 'pending': None})
        self.assertIn('gh auth status', unreadable[2])

    def test_a_waiting_keeper_without_a_reading_names_gh_auth_status(self):
        lines = self.lines(phase='waiting', error='ValueError: GitHub authentication unavailable for octo',
                           reading=None)
        self.assertIn('not read yet', lines[2])
        self.assertIn('gh auth status --hostname github.com', lines[2])

    def test_an_unreadable_arming_file_is_named_and_the_keeper_still_shown(self):
        from devcontainer_setup.project_sync.state import atomic_write
        atomic_write(self.state / 'arming.json', b'{"schema": 2}')
        lines = self.lines()
        self.assertEqual(lines[0], 'Recovery     arming.json cannot be read (Unsupported arming.json schema); move '
                         + str(self.state / 'arming.json') + ' aside, then run task work:cs:armed to arm again')
        self.assertEqual([line.split()[0] for line in lines], ['Recovery', 'Keeper', 'Codespace', 'Keep-alive', 'Last'])

    def test_the_gh_auth_hint_needs_an_auth_error(self):
        other = self.lines(phase='waiting', error='RuntimeError: unexpected error', reading=None)
        self.assertEqual(other[2], 'Codespace    not read yet')
        locked = self.lines(phase='waiting', reading=None, error='ValueError: Reading the GitHub token for octo '
                            'timed out; unlock the Mac and its login keychain, then retry.')
        self.assertIn('unlock the Mac', locked[2])
        self.assertNotIn('gh auth status', locked[2])

    def test_unarmed_project_gets_one_line(self):
        from devcontainer_setup.project_sync import arming
        arming.write(self.state, dict(arming.read(self.state), armed=False))
        lines = self.lines(running=False)
        self.assertEqual(len(lines), 1)
        self.assertIn('not armed', lines[0])
