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
