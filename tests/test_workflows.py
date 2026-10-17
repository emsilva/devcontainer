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
        with patch('devcontainer_setup.project_sync.workflows.subprocess.run',return_value=Mock(returncode=0,stdout=json.dumps({'codespaces':rows}))), patch.object(sessions_module(),'_git',return_value={'branch':'main'}):
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
