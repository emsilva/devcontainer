import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from devcontainer_setup.project_sync.state import Project
from devcontainer_setup.project_sync import scratch, handoff
try:
    from devcontainer_setup.project_sync.workflows import Workflows
except ImportError:
    Workflows = None

SID = '12345678-1234-4234-8234-123456789abc'
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = Project(self.root, 'git@github.com:o/r.git', 'github.com/o/r', 'a'*24)
    def service(self):
        self.assertIsNotNone(Workflows, 'workflow service must exist')
        return Workflows(self.project, self.root/'private')
    def test_preferences_private_and_validated(self):
        s = self.service(); self.assertEqual(s.preferences(), {})
        s.remember('codex', SID, 'mac')
        self.assertEqual(s.preferences()['session_id'], SID)
        self.assertEqual((s.state/'work.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError): s.remember('codex', '../bad', 'mac')
    def test_preferences_refuse_symlink(self):
        s=self.service(); s.state.mkdir(); (s.state/'work.json').symlink_to(self.root/'elsewhere')
        with self.assertRaises(OSError): s.preferences()
    def test_inventory_rpc_includes_project_catalog_and_ownership(self):
        endpoint=handoff.LocalEndpoint(self.project,self.root/'private',env={})
        with patch.object(handoff.sessions,'list_sessions',return_value=[dict(agent='codex',session_id=SID)]), patch.object(handoff.sessions,'_git',return_value={'head':'x'}), patch.object(handoff,'host_id',return_value='local'), patch.object(handoff.ownership,'owner_status',return_value={'status':'unknown'}):
            result=endpoint.call('inventory')
        self.assertEqual(len(result['sessions']),2); self.assertEqual(result['sessions'][0]['ownership']['status'],'unknown')
    def test_git_mismatch_and_dirty_block(self):
        s=self.service()
        for remote in ({'branch':'other','head':'1','status':''},{'branch':'main','head':'1','status':' M f'}):
            with self.assertRaisesRegex(ValueError,'Git.*manually'): s._check_git({'branch':'main','head':'1','status':''}, remote)
    def test_scratch_structured_conflicts_and_clean(self):
        self.assertTrue(hasattr(scratch,'_reconciliation'),'structured reconciliation must exist')
        self.assertEqual(scratch._reconciliation({'status':'watching','alpha':{'connected':True},'beta':{'connected':True},'conflicts':[]})['phase'],'synchronized')
        value=scratch._reconciliation({'status':'watching','conflicts':[{'root':'a\x1b[31m'}]})
        self.assertEqual(value['phase'],'conflicts'); self.assertNotIn('\x1b',str(value))
        self.assertNotEqual(scratch._reconciliation({'status':'syncing'})['phase'],'synchronized')
    def test_no_implicit_codespace_start(self):
        s=self.service()
        with patch('devcontainer_setup.project_sync.workflows.subprocess.run') as run:
            s.start_codespace = Mock()
            self.assertIsNone(s.binding()); s.start_codespace.assert_not_called(); run.assert_not_called()
    def test_spaces_filters_repo_and_branch(self):
        s=self.service()
        rows=[{'name':'good','repository':{'full_name':'o/r'},'git_status':{'ref':'main'}},{'name':'wrong','repository':{'full_name':'o/r'},'git_status':{'ref':'other'}},{'name':'alien','repository':{'full_name':'x/y'},'git_status':{'ref':'main'}}]
        with patch('devcontainer_setup.project_sync.workflows.subprocess.run',return_value=Mock(returncode=0,stdout=json.dumps([{'codespaces':rows}]))), patch.object(sessions_module(),'_git',return_value={'branch':'main'}):
            self.assertEqual([row['name'] for row in s.spaces()],['good'])
    def test_ambiguous_pair_requires_selection(self):
        s=self.service()
        with patch.object(s,'roots',return_value=['/workspaces/a','/workspaces/b']):
            with self.assertRaisesRegex(ValueError,'Choose'): s.pair('example')
    def test_same_host_reuses_custody_without_handoff(self):
        s=self.service(); s.is_remote=False
        inventory=dict(host='local',git={},sessions=[dict(agent='codex',session_id=SID,ownership={'status':'idle','host':'local'})])
        with patch.object(s,'inventory',return_value=inventory), patch.object(handoff,'handoff') as transfer, patch('devcontainer_setup.project_sync.workflows.agents.launch',return_value=0):
            self.assertEqual(s.continue_session('codex',SID,'mac'),0); transfer.assert_not_called()
    def test_active_writer_rejected(self):
        s=self.service(); s.is_remote=False
        inventory=dict(host='local',git={},sessions=[dict(agent='codex',session_id=SID,ownership={'status':'active','host':'local'})])
        with patch.object(s,'inventory',return_value=inventory), patch.object(handoff,'handoff') as transfer:
            with self.assertRaisesRegex(ValueError,'active or uncertain'): s.continue_session('codex',SID,'codespace')
            transfer.assert_not_called()
    def test_interactive_remote_shell_uses_real_generated_launcher(self):
        s=self.service()
        with patch.object(s,'binding',return_value={'codespace':'example','remote_root':"/workspaces/a'b"}), patch('devcontainer_setup.project_sync.workflows.CodespaceConnection') as connection, patch('devcontainer_setup.project_sync.workflows.subprocess.call',return_value=0) as call:
            s._launch_remote('codex',SID)
        args=call.call_args.args[0]
        self.assertIn('-tt',args); self.assertIn('project-sync.sh',args[-1]); self.assertIn("'\"'\"'",args[-1])
    def test_flush_refuses_dead_worker_even_if_phase_syncing(self):
        with patch.object(scratch,'_config',return_value={}),patch.object(scratch,'status',return_value={'phase':'syncing','running':False}):
            with self.assertRaisesRegex(ValueError,'not connected'): scratch.flush(self.root,self.root/'private')

    def test_switch_waits_for_new_worker_then_flushes(self):
        service = self.service()
        phases = [{'phase': 'connecting'}, {'phase': 'synchronized'}]
        with patch.object(service, 'sync_status', side_effect=phases) as status, \
                patch.object(scratch, 'flush', return_value={'phase': 'synchronized'}) as flush, \
                patch('devcontainer_setup.project_sync.workflows.time.sleep') as sleep:
            service.sync_ready()
        self.assertEqual(status.call_count, 2)
        sleep.assert_called_once()
        flush.assert_called_once_with(service.project.root, service.state)

    def test_switch_wait_refuses_conflicts_stopped_worker_and_deadline(self):
        for value, message in (({'phase': 'conflicts', 'conflicts': ['notes.md']}, 'notes.md'),
                               ({'phase': 'stopped'}, 'not running')):
            service = self.service()
            with patch.object(service, 'sync_status', return_value=value), \
                    patch.object(scratch, 'flush') as flush:
                with self.assertRaisesRegex(ValueError, message):
                    service.sync_ready()
                flush.assert_not_called()
        service = self.service()
        with patch.object(service, 'sync_status', return_value={'phase': 'connecting'}), \
                patch('devcontainer_setup.project_sync.workflows.time.monotonic', side_effect=[0, 91]), \
                patch.object(scratch, 'flush') as flush:
            with self.assertRaisesRegex(ValueError, 'timed out'):
                service.sync_ready()
            flush.assert_not_called()

    def test_remote_reuse_still_checks_git_and_sync(self):
        s=self.service(); s.is_remote=False
        local=dict(host='local',git={'branch':'main','head':'1','status':''},sessions=[])
        remote=dict(host='remote',git=local['git'],sessions=[dict(agent='codex',session_id=SID,ownership={'status':'idle','host':'remote'})])
        with patch.object(s,'inventory',side_effect=[local,remote]), patch.object(s,'sync_ready') as ready, patch.object(s,'_launch_remote',return_value=0), patch.object(handoff,'handoff') as transfer:
            s.continue_session('codex',SID,'codespace')
            ready.assert_called_once(); transfer.assert_not_called()

    def test_transfers_in_both_directions_before_launch(self):
        for destination in ('mac','codespace'):
            with self.subTest(destination=destination):
                s=self.service(); s.is_remote=False
                git=dict(branch='main',head='1',status='')
                row=lambda host: dict(agent='codex',session_id=SID,ownership={'status':'idle','host':host})
                local=dict(host='local',git=git,sessions=[row('local')] if destination=='codespace' else [])
                remote=dict(host='remote',git=git,sessions=[row('remote')] if destination=='mac' else [])
                with patch.object(s,'inventory',side_effect=[local,remote]), patch.object(s,'sync_ready') as ready, patch.object(s,'_launch_remote',return_value=0), patch.object(handoff,'handoff') as transfer, patch('devcontainer_setup.project_sync.workflows.agents.launch',return_value=0):
                    self.assertEqual(s.continue_session('codex',SID,destination),0)
                    self.assertEqual(transfer.call_args.args[3],destination); ready.assert_called_once()
    def test_flush_conflicts_preserve_session_and_refuse_flush(self):
        info={'identifier':'sync_owned','status':'watching','conflicts':[{'root':'notes.md'}]}
        with patch.object(scratch,'_config',return_value={}), patch.object(scratch,'status',return_value={'phase':'syncing','running':True}), patch.object(scratch,'_saved_session',return_value={'name':'owned'}), patch.object(scratch,'install_mutagen',return_value='mutagen'), patch.object(scratch,'_session_info',return_value=info), patch.object(scratch,'_mutagen') as command:
            with self.assertRaisesRegex(ValueError,'notes.md'): scratch.flush(self.root,self.root/'private')
            command.assert_not_called()
    def test_successful_flush_verifies_inventory_after_barrier(self):
        info={'identifier':'sync_owned','status':'watching','alpha':{'connected':True},'beta':{'connected':True}}
        with patch.object(scratch,'_config',return_value={}), patch.object(scratch,'status',return_value={'phase':'syncing','running':True}), patch.object(scratch,'_saved_session',return_value={'name':'owned'}), patch.object(scratch,'install_mutagen',return_value='mutagen'), patch.object(scratch,'_session_info',side_effect=[info,info]) as inventory, patch.object(scratch,'_mutagen') as command:
            self.assertEqual(scratch.flush(self.root,self.root/'private')['phase'],'synchronized')
            self.assertEqual(inventory.call_count,2); self.assertEqual(command.call_args.args[2:],('sync','flush','sync_owned'))

    def test_status_does_not_call_dead_worker_syncing(self):
        with patch.object(scratch,'status',return_value={'phase':'syncing','running':False}):
            self.assertEqual(scratch.inspect_sync(self.root,self.root/'private')['phase'],'stopped')

def sessions_module():
    from devcontainer_setup.project_sync import sessions
    return sessions

class CodespaceAPITests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    service = WorkflowTests.service
    def test_start_waits_through_initial_shutdown_without_repeating_post(self):
        service = self.service()
        states = iter(('Shutdown', 'Shutdown', 'Starting', 'Available'))
        calls = []
        def api(method, route, env, **kwargs):
            calls.append(method)
            return {} if method == 'POST' else {'state': next(states)}
        with patch.object(service, '_api_env', return_value={}), \
                patch.object(service, '_api', side_effect=api), \
                patch('devcontainer_setup.project_sync.workflows.time.sleep'):
            service.start_codespace('example')
        self.assertEqual(calls, ['POST', 'GET', 'GET', 'GET', 'GET'])

    def test_start_remaining_shutdown_times_out_with_last_state(self):
        service = self.service()
        with patch.object(service, '_api_env', return_value={}), \
                patch.object(service, '_api', return_value={'state': 'Shutdown'}) as api, \
                patch('devcontainer_setup.project_sync.workflows.time.monotonic', side_effect=[0, 1, 2, 301]), \
                patch('devcontainer_setup.project_sync.workflows.time.sleep'):
            with self.assertRaisesRegex(ValueError, 'timed out.*Shutdown'):
                service.start_codespace('example')
        self.assertEqual([call.args[0] for call in api.call_args_list], ['POST', 'GET'])

    def test_corrupt_preferences_are_actionable_and_preserved(self):
        service = self.service()
        service.state.mkdir()
        path = service.state / 'work.json'
        for value in ({}, [], {'agent': 'codex', 'session_id': '../bad', 'location': 'mac'}):
            raw = json.dumps(value).encode()
            path.write_bytes(raw)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'saved.*selection'):
                service.preferences()
            self.assertEqual(path.read_bytes(), raw)

    def test_token_only_list_and_start_wait_until_available(self):
        service = self.service()
        responses = [
            Mock(returncode=0, stdout='[{"codespaces": []}]'),
            Mock(returncode=0, stdout='{}'),
            Mock(returncode=0, stdout='{"state": "Starting"}'),
            Mock(returncode=0, stdout='{"state": "Available"}'),
        ]
        with patch.dict('os.environ', {'GH_TOKEN': 'secret', 'GH_HOST': 'evil'}, clear=True), patch('devcontainer_setup.project_sync.workflows.subprocess.run', side_effect=responses) as run, patch.object(sessions_module(), '_git', return_value={'branch': 'main'}), patch('time.sleep'):
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
        with patch.dict('os.environ', {'DEVCONTAINER_GH_USER': 'alternate', 'GITHUB_TOKEN': 'wrong'}, clear=True), patch('subprocess.run', side_effect=command), patch.object(sessions_module(), '_git', return_value={'branch': 'main'}):
            service.spaces()
            service.start_codespace('example')
        self.assertEqual(sum(args[1:3] == ['auth', 'token'] for args, _ in calls), 2)

    def test_paginated_match_and_bound_space_survives_branch_change(self):
        service = self.service()
        row = {'name': 'example', 'repository': {'full_name': 'o/r'}, 'git_status': {'ref': 'old'}}
        def command(args, **kwargs):
            self.assertIn('--paginate', args) if '--slurp' in args else None
            return Mock(returncode=0, stdout=json.dumps([{'codespaces': []}, {'codespaces': [row]}]) if '--slurp' in args else json.dumps(row))
        with patch('devcontainer_setup.project_sync.workflows.github_token', return_value='secret'), patch('subprocess.run', side_effect=command) as run, patch.object(sessions_module(), '_git', return_value={'branch': 'old'}), patch.object(service, 'binding', return_value={'codespace': 'example'}):
            self.assertEqual(service.spaces(), [row])
            with patch.object(sessions_module(), '_git', return_value={'branch': 'new'}):
                self.assertEqual(service.space(), row)
            self.assertTrue(all('POST' not in call.args[0] for call in run.call_args_list))
            row['repository']['full_name'] = 'alien/repo'
            with self.assertRaisesRegex(ValueError, 'repository'):
                service.space()

    def test_inventory_output_and_request_time_are_bounded(self):
        import subprocess
        service = self.service()
        with patch('devcontainer_setup.project_sync.workflows.github_token', return_value='secret'), patch('subprocess.run', return_value=Mock(returncode=0, stdout='x' * (2 * 1024 * 1024 + 1))):
            with self.assertRaisesRegex(ValueError, 'exceeds limit'):
                service.spaces()
        with patch('devcontainer_setup.project_sync.workflows.github_token', return_value='secret'), patch('subprocess.run', side_effect=subprocess.TimeoutExpired(['gh'], 30)):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                service.start_codespace('example')

    def test_start_terminal_failure_and_timeout(self):
        for state in ('Failed', 'Unavailable', 'Deleted'):
            with self.subTest(state=state), patch('devcontainer_setup.project_sync.workflows.github_token', return_value='secret'), patch('subprocess.run', return_value=Mock(returncode=0, stdout=json.dumps({'state': state}))) as run:
                with self.assertRaisesRegex(ValueError, state):
                    self.service().start_codespace('example')
                self.assertEqual(sum('POST' in c.args[0] for c in run.call_args_list), 1)
        with patch('devcontainer_setup.project_sync.workflows.github_token', return_value='secret'), patch('subprocess.run', return_value=Mock(returncode=0, stdout='{"state":"Starting"}')) as run, patch('time.monotonic', side_effect=[0, 301]):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                self.service().start_codespace('example')
            self.assertEqual(sum('POST' in c.args[0] for c in run.call_args_list), 1)
