import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from devcontainer_setup.project_sync.state import Project
from devcontainer_setup.project_sync.codespaces import Codespaces

class CodespaceAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = Project(self.root, 'git@github.com:o/r.git', 'github.com/o/r', 'a'*24)
    def service(self):
        return Codespaces(self.project, self.root/'private')
    def test_start_waits_through_initial_shutdown_without_repeating_post(self):
        service = self.service()
        states = iter(('Shutdown', 'Shutdown', 'Starting', 'Available'))
        calls = []
        def api(method, route, env, **kwargs):
            calls.append(method)
            return {} if method == 'POST' else {'state': next(states)}
        with patch.object(service, '_api_env', return_value={}), \
                patch.object(service, '_api', side_effect=api), \
                patch('devcontainer_setup.project_sync.codespaces.time.sleep'):
            service.start_codespace('example')
        self.assertEqual(calls, ['POST', 'GET', 'GET', 'GET', 'GET'])

    def test_start_remaining_shutdown_times_out_with_last_state(self):
        service = self.service()
        with patch.object(service, '_api_env', return_value={}), \
                patch.object(service, '_api', return_value={'state': 'Shutdown'}) as api, \
                patch('devcontainer_setup.project_sync.codespaces.time.monotonic', side_effect=[0, 1, 2, 301]), \
                patch('devcontainer_setup.project_sync.codespaces.time.sleep'):
            with self.assertRaisesRegex(ValueError, 'timed out.*Shutdown'):
                service.start_codespace('example')
        self.assertEqual([call.args[0] for call in api.call_args_list], ['POST', 'GET'])

    def test_token_only_list_and_start_wait_until_available(self):
        service = self.service()
        responses = [
            Mock(returncode=0, stdout='[{"codespaces": []}]'),
            Mock(returncode=0, stdout='{}'),
            Mock(returncode=0, stdout='{"state": "Starting"}'),
            Mock(returncode=0, stdout='{"state": "Available"}'),
        ]
        with patch.dict('os.environ', {'GH_TOKEN': 'secret', 'GH_HOST': 'evil'}, clear=True), patch('devcontainer_setup.project_sync.codespaces.subprocess.run', side_effect=responses) as run, patch('subprocess.check_output', return_value='main\n'), patch('time.sleep'):
            self.assertEqual(service.spaces(), [])
            service.start_codespace('example')
        self.assertEqual([call.args[0][3] for call in run.call_args_list], ['GET', 'POST', 'GET', 'GET'])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['env']['GH_TOKEN'], 'secret')
            self.assertNotIn('GH_HOST', call.kwargs['env'])
            self.assertIn('--hostname', call.args[0])
            self.assertIn('github.com', call.args[0])

    def test_selected_account_used_for_discovery_and_start(self):
        service = self.service()
        calls = []
        def command(args, **kwargs):
            calls.append((args, kwargs))
            if args[1:3] == ['auth', 'token']:
                self.assertIn('alternate', args)
                self.assertNotIn('GITHUB_TOKEN', kwargs['env'])
                return Mock(returncode=0, stdout='selected-token\n')
            self.assertEqual(kwargs['env']['GH_TOKEN'], 'selected-token')
            return Mock(returncode=0, stdout='[]' if '--slurp' in args else '{"state":"Available"}')
        with patch.dict('os.environ', {'DEVCONTAINER_GH_USER': 'alternate', 'GITHUB_TOKEN': 'wrong'}, clear=True), patch('subprocess.run', side_effect=command), patch('subprocess.check_output', return_value='main\n'):
            service.spaces()
            service.start_codespace('example')
        self.assertEqual(sum(args[1:3] == ['auth', 'token'] for args, _ in calls), 2)

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

    def test_inventory_output_and_request_time_are_bounded(self):
        import subprocess
        service = self.service()
        with patch('devcontainer_setup.project_sync.codespaces.github_token', return_value='secret'), patch('subprocess.run', return_value=Mock(returncode=0, stdout='x' * (2 * 1024 * 1024 + 1))):
            with self.assertRaisesRegex(ValueError, 'exceeds limit'):
                service.spaces()
        with patch('devcontainer_setup.project_sync.codespaces.github_token', return_value='secret'), patch('subprocess.run', side_effect=subprocess.TimeoutExpired(['gh'], 30)):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                service.start_codespace('example')

    def test_start_terminal_failure_and_timeout(self):
        for state in ('Failed', 'Unavailable', 'Deleted'):
            with self.subTest(state=state), patch('devcontainer_setup.project_sync.codespaces.github_token', return_value='secret'), patch('subprocess.run', return_value=Mock(returncode=0, stdout=json.dumps({'state': state}))) as run:
                with self.assertRaisesRegex(ValueError, state):
                    self.service().start_codespace('example')
                self.assertEqual(sum('POST' in c.args[0] for c in run.call_args_list), 1)
        with patch('devcontainer_setup.project_sync.codespaces.github_token', return_value='secret'), patch('subprocess.run', return_value=Mock(returncode=0, stdout='{"state":"Starting"}')) as run, patch('time.monotonic', side_effect=[0, 301]):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                self.service().start_codespace('example')
            self.assertEqual(sum('POST' in c.args[0] for c in run.call_args_list), 1)
