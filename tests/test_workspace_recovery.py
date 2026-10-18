"""Existing-checkout connection and legacy migration never mutate repositories."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
from devcontainer_setup.project_sync import workspace, scratch
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.state import discover_project, read_json

class ExistingCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve(); self.root = self.base / 'desktop'; self.root.mkdir()
        self.git('init', '-q'); self.git('remote', 'add', 'origin', 'https://github.com/example/ordinary')
        self.project = discover_project(self.root); self.state = self.base / 'state'
        self.original = '/workspaces/ordinary'; self.generated = '/workspaces/.devcontainer-work/project/snapshot'
        (self.root / '.scratch').mkdir()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def registry(self, **updates):
        value = dict(schema=1, identity=self.project.identity, project_id=self.project.project_id,
                     local_root=str(self.root), local_active_root=str(self.root), active_host='codespace',
                     codespace='space', remote_root=self.original, remote_active_root=self.generated)
        value.update(updates); workspace._write(self.state, value); return value

    def connection(self):
        connection = Mock(); connection.__enter__ = Mock(return_value=connection); connection.__exit__ = Mock(return_value=None)
        return connection

    def test_remote_uses_verified_ordinary_checkout_and_never_capture_restore_or_git(self):
        self.registry()
        with patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), \
                patch.object(workspace, '_verify_roots', return_value=[self.generated]), \
                patch('devcontainer_setup.project_sync.replication.sync_project_data', return_value=dict(receipt={}, issues=[])) as sync, \
                patch.object(scratch, 'retarget') as retarget, patch.object(scratch, 'start'), \
                patch.object(workspace, '_ready'), patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0) as opened, \
                patch('devcontainer_setup.project_sync.notes.configure_memory'):
            self.assertEqual(workspace.remote(self.project, self.state), 0)
        self.assertEqual(retarget.call_args.args[3], self.original)
        self.assertEqual(opened.call_args.args[2], self.original)
        self.assertEqual(sync.call_args.kwargs['historical_roots'], [self.generated])
        value = read_json(self.state / 'workspace.json')
        self.assertEqual(value['remote_active_root'], self.original)
        self.assertIn(self.generated, value['verified_historical_roots'])
        self.assertTrue(list((self.state / 'workspace-history').glob('*.json')))

    def test_choose_pairing_discovers_original_when_registry_root_was_overwritten(self):
        value = self.registry(remote_root=self.generated)
        scratch._write(self.state / scratch.CONFIG, dict(version=1, identity=self.project.identity,
                         root=str(self.root), codespace='space', remote_root=self.generated))
        with patch('devcontainer_setup.project_sync.codespaces.Codespaces') as cls:
            service = cls.return_value
            service._api.return_value = dict(name='space', state='Available')
            service._matches_repository.return_value = True; service.roots.return_value = [self.original]
            self.assertEqual(workspace._choose_pairing(self.project, self.state, value), ('space', self.original))
            service.roots.assert_called_once_with('space', accept_new=False)
            service.pair.assert_not_called()
        self.assertEqual(read_json(self.state / scratch.CONFIG)['remote_root'], self.generated)

    def test_missing_or_ambiguous_original_preserves_binding_and_shows_choices(self):
        value = self.registry(remote_root=self.generated)
        scratch._write(self.state / scratch.CONFIG, dict(version=1, identity=self.project.identity,
                         root=str(self.root), codespace='space', remote_root=self.generated))
        before = (self.state / scratch.CONFIG).read_bytes()
        for roots in ([], ['/workspaces/one', '/workspaces/two']):
            with self.subTest(roots=roots), patch('devcontainer_setup.project_sync.codespaces.Codespaces') as cls:
                service = cls.return_value; service._api.return_value = dict(name='space', state='Available')
                service._matches_repository.return_value = True; service.roots.return_value = roots
                with self.assertRaisesRegex(ValueError, 'checkout.*' + ('one' if roots else 'found')):
                    workspace._choose_pairing(self.project, self.state, value)
            self.assertEqual((self.state / scratch.CONFIG).read_bytes(), before)

    def test_saved_ordinary_original_wins_over_other_candidates(self):
        value = self.registry()
        scratch._write(self.state / scratch.CONFIG, dict(identity=self.project.identity, codespace='space', remote_root=self.generated))
        with patch('devcontainer_setup.project_sync.codespaces.Codespaces') as cls:
            service = cls.return_value; service._api.return_value = dict(name='space', state='Available')
            service._matches_repository.return_value = True; service.roots.return_value = ['/workspaces/other', self.original]
            self.assertEqual(workspace._choose_pairing(self.project, self.state, value), ('space', self.original))

    def test_verify_historical_root_uses_remote_canonical_identity(self):
        connection = self.connection()
        connection.python.return_value = json.dumps(dict(root=self.generated, identity=self.project.identity)).encode()
        self.assertEqual(workspace._verify_roots(connection, self.project, [self.generated]), [self.generated])
        connection.python.return_value = json.dumps(dict(root=self.generated, identity='github.com/foreign/repo')).encode()
        self.assertEqual(workspace._verify_roots(connection, self.project, [self.generated]), [])

    def test_status_is_offline_and_legacy_checkpoint_never_counts_as_conversation_health(self):
        self.registry()
        store = Store(self.state)
        old = store.save(dict(schema=1, project=dict(identity=self.project.identity), files={}, conversations=dict(entries=[{}])))
        store.acknowledge(old, source='codespace')
        with patch.object(scratch, 'status', return_value=dict(running=False, phase='stopped')), \
                patch('devcontainer_setup.project_sync.codespaces.Codespaces', side_effect=AssertionError('offline')):
            result = workspace.status(self.project, self.state)
        self.assertIsNone(result['last_mac_checkpoint'])
        self.assertFalse(result['conversations']['healthy'])
        self.assertEqual(store.load(old)['schema'], 1)
        self.assertNotIn('recoverable', str(result))

    def test_ready_requires_schema_two_receipt_not_old_worker_checkpoint(self):
        self.registry()
        with patch.object(scratch, 'status', return_value=dict(running=True, phase='syncing', checkpoint={'snapshot_id':'old'}, checkpoint_error=None)), \
                patch.object(workspace.time, 'monotonic', side_effect=[0, 1, 121]), patch.object(workspace.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'conversation'):
                workspace._ready(self.project, self.state)

    def test_repeated_connection_retains_separate_verified_local_historical_roots(self):
        recovered = self.base / 'recovered'
        shutil.copytree(self.root, recovered)
        self.registry(local_active_root=str(recovered))
        for attempt in range(2):
            with patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), patch.object(workspace, '_verify_roots', return_value=[self.generated]) as verify, patch('devcontainer_setup.project_sync.replication.sync_project_data', return_value=dict(receipt={}, issues=[])), patch.object(scratch, 'retarget'), patch.object(scratch, 'start'), patch.object(workspace, '_ready'), patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0), patch('devcontainer_setup.project_sync.notes.configure_memory'):
                workspace.remote(self.project, self.state)
            value = read_json(self.state / 'workspace.json')
            self.assertIn(str(recovered), value['verified_local_historical_roots'])
            self.assertNotIn(str(recovered), verify.call_args.args[2])

    def test_stopped_or_waiting_worker_never_reports_healthy_conversations(self):
        self.registry()
        store = Store(self.state)
        manifest = dict(schema=2, project=dict(identity=self.project.identity, project_id=self.project.project_id), conversations=dict(entries=[], issues=[]), producer={})
        snapshot = store.save(manifest); store.acknowledge(snapshot, source='codespace')
        for running, phase in ((False, 'stopped'), (True, 'waiting')):
            with patch.object(scratch, 'status', return_value=dict(running=running, phase=phase, conversations=dict(schema=2, issues=[]))), patch.object(scratch, 'inspect_sync', return_value={}):
                self.assertFalse(workspace.status(self.project, self.state)['conversations']['healthy'])

    def test_status_survives_legacy_binding_root_mismatch_offline(self):
        self.registry(local_active_root=str(self.base / 'recovered'))
        with patch.object(scratch, 'status', return_value=dict(running=True, phase='syncing')), patch.object(scratch, 'inspect_sync', side_effect=ValueError('Scratch configuration does not match this repository/checkout')):
            result = workspace.status(self.project, self.state)
        self.assertIn('issue', str(result['scratch']).lower())

    def test_ready_rejects_stopped_runtime_even_with_previous_schema_two_receipt(self):
        store = Store(self.state)
        snapshot = store.save(dict(schema=2, project=dict(identity=self.project.identity, project_id=self.project.project_id), conversations=dict(entries=[], issues=[]), producer={}))
        store.acknowledge(snapshot, source='codespace')
        with patch.object(scratch, 'status', return_value=dict(running=False, phase='syncing', conversations=dict(schema=2))):
            with self.assertRaisesRegex(ValueError, 'stopped'):
                workspace._ready(self.project, self.state)

    def test_registry_archive_preserves_changed_saved_root_and_original_scope_lists(self):
        recovered = self.base / 'recovered'; shutil.copytree(self.root, recovered)
        original = self.registry(remote_root=self.generated, remote_active_root=self.original,
                                 verified_local_historical_roots=[str(recovered)], verified_historical_roots=[self.generated])
        expected = json.loads(json.dumps(original))
        with patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), patch.object(workspace, '_verify_roots', return_value=[self.generated]), patch('devcontainer_setup.project_sync.replication.sync_project_data', return_value=dict(receipt={}, issues=[])), patch.object(scratch, 'retarget'), patch.object(scratch, 'start'), patch.object(workspace, '_ready'), patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0), patch('devcontainer_setup.project_sync.notes.configure_memory'):
            workspace.remote(self.project, self.state)
        archives = list((self.state / 'workspace-history').glob('*.json'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(read_json(archives[0]), expected)

    def test_retired_local_recovery_and_agent_launch_apis_absent(self):
        for name in ('local', 'local_project', '_recover'):
            self.assertFalse(hasattr(workspace, name))
