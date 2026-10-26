"""Existing-checkout connection and legacy migration never mutate repositories."""
import json
import time
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
        proxies = patch('devcontainer_setup.project_sync.restore.end_proxies_elsewhere', return_value=[])
        self.proxies = proxies.start(); self.addCleanup(proxies.stop)

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
        with patch.object(workspace, '_prerequisites'), patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), \
                patch.object(workspace, '_verify_roots', return_value=[self.generated]), \
                patch('devcontainer_setup.project_sync.replication.sync_project_data', return_value=dict(receipt={}, issues=[])) as sync, \
                patch.object(scratch, 'retarget') as retarget, patch.object(scratch, 'start'), \
                patch.object(workspace, '_ready', return_value={}), patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0) as opened, \
                patch('devcontainer_setup.project_sync.notes.configure_memory'):
            self.assertEqual(workspace.remote(self.project, self.state), 0)
        self.assertEqual(retarget.call_args.args[3], self.original)
        self.assertEqual(opened.call_args.args[2], self.original)
        sync.assert_not_called()
        value = read_json(self.state / 'workspace.json')
        self.assertEqual(value['remote_active_root'], self.original)
        self.assertIn(self.generated, value['verified_historical_roots'])
        self.assertTrue(list((self.state / 'workspace-history').glob('*.json')))

    def test_armed_step_runs_after_ssh_and_before_herdr_outside_the_lock(self):
        from devcontainer_setup.project_sync.state import locked
        self.registry()
        order = []
        def armed(primary):
            self.assertEqual(primary.root, self.root)
            # The consent prompt takes workspace-control.lock itself; it must be free here.
            with locked(self.state / 'workspace-control.lock'):
                order.append('armed')
        with patch.object(workspace, '_prerequisites'), patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), \
                patch.object(workspace, '_verify_roots', return_value=[self.generated]), \
                patch.object(scratch, 'retarget'), patch.object(scratch, 'start', side_effect=lambda *_: order.append('worker')), \
                patch.object(workspace, '_ready', return_value={}), \
                patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh', side_effect=lambda *_: order.append('ssh')), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', side_effect=lambda *_, **__: order.append('herdr') or 0):
            self.assertEqual(workspace.remote(self.project, self.state, armed=armed), 0)
        self.assertEqual(order, ['worker', 'ssh', 'armed', 'herdr'])

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

    def test_rebind_moves_to_the_named_codespace_and_starts_it(self):
        value = self.registry(remote_active_root=self.original)
        scratch._write(self.state / scratch.CONFIG, dict(version=1, identity=self.project.identity,
                         root=str(self.root), codespace='space', remote_root=self.original))
        before = (self.state / scratch.CONFIG).read_bytes()
        with patch('devcontainer_setup.project_sync.codespaces.Codespaces') as cls:
            service = cls.return_value; service._api.return_value = dict(name='new-space', state='Shutdown')
            service._matches_repository.return_value = True; service.roots.return_value = [self.original]
            self.assertEqual(workspace._choose_pairing(self.project, self.state, value, rebind='new-space'),
                             ('new-space', self.original))
            self.assertEqual(service._api.call_args.args[:2], ('GET', 'user/codespaces/new-space'))
            service.start_codespace.assert_called_once_with('new-space')
            # An explicit rebind is the trust-on-first-use boundary for the new host.
            service.roots.assert_called_once_with('new-space', accept_new=True)
        # retarget, not discovery, retires the old binding.
        self.assertEqual((self.state / scratch.CONFIG).read_bytes(), before)

    def test_rebind_to_a_foreign_or_invalid_codespace_is_refused_before_it_starts_anything(self):
        value = self.registry()
        scratch._write(self.state / scratch.CONFIG, dict(version=1, identity=self.project.identity,
                         root=str(self.root), codespace='space', remote_root=self.original))
        before = (self.state / scratch.CONFIG).read_bytes()
        for name, matches, message in (('new-space', False, 'No Codespace new-space for this repository'),
                                       ('../space', True, 'Invalid Codespace name'), ('', True, 'Invalid Codespace name')):
            with self.subTest(name=name), patch('devcontainer_setup.project_sync.codespaces.Codespaces') as cls:
                service = cls.return_value; service._api.return_value = dict(name=name, state='Shutdown')
                service._matches_repository.return_value = matches
                with self.assertRaisesRegex(ValueError, message):
                    workspace._choose_pairing(self.project, self.state, value, rebind=name)
                service.start_codespace.assert_not_called(); service.roots.assert_not_called()
        self.assertEqual((self.state / scratch.CONFIG).read_bytes(), before)

    def test_rebind_to_the_paired_codespace_keeps_its_saved_host_key_check(self):
        value = self.registry()
        scratch._write(self.state / scratch.CONFIG, dict(version=1, identity=self.project.identity,
                         root=str(self.root), codespace='space', remote_root=self.original))
        with patch('devcontainer_setup.project_sync.codespaces.Codespaces') as cls:
            service = cls.return_value; service._api.return_value = dict(name='space', state='Available')
            service._matches_repository.return_value = True; service.roots.return_value = [self.original]
            self.assertEqual(workspace._choose_pairing(self.project, self.state, value, rebind='space'), ('space', self.original))
            service.roots.assert_called_once_with('space', accept_new=False)

    def test_a_failed_retarget_leaves_the_registry_on_the_old_codespace(self):
        before = self.registry(verified_remote_historical_roots=['/workspaces/old-checkout'])
        scratch._write(self.state / scratch.CONFIG, dict(version=1, identity=self.project.identity,
                         root=str(self.root), codespace='space', remote_root=self.original))
        with patch.object(workspace, '_prerequisites'), \
                patch.object(workspace, '_choose_pairing', return_value=('new-space', self.original)), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), \
                patch.object(workspace, '_verify_roots', side_effect=lambda _c, _p, roots: list(roots)), \
                patch.object(scratch, 'retarget', side_effect=ValueError('Remote repository identity/checkout differs; old binding preserved')), \
                patch.object(scratch, 'start') as start:
            with self.assertRaisesRegex(ValueError, 'old binding preserved'):
                workspace.remote(self.project, self.state, rebind='new-space')
        start.assert_not_called()
        self.assertEqual(read_json(self.state / 'workspace.json'), before)

    def test_moving_to_another_codespace_drops_its_remote_history_and_keeps_local_history(self):
        recovered = self.base / 'recovered'
        shutil.copytree(self.root, recovered)
        # The binding, not the registry, names the Codespace being left.
        self.registry(local_active_root=str(recovered), verified_remote_historical_roots=['/workspaces/old-checkout'],
                      codespace=None)
        scratch._write(self.state / scratch.CONFIG, dict(version=1, identity=self.project.identity,
                         root=str(self.root), codespace='space', remote_root=self.original))
        verified, order = [], []
        self.proxies.side_effect = lambda codespace, *_: order.append('proxies:' + codespace) or []
        connection = self.connection(); connection.python.return_value = b'\n'
        with patch.object(workspace, '_prerequisites'), \
                patch.object(workspace, '_choose_pairing', return_value=('new-space', self.original)) as choose, \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=connection), \
                patch.object(workspace, '_verify_roots', side_effect=lambda _c, _p, roots: verified.append(list(roots)) or list(roots)), \
                patch.object(scratch, 'retarget', side_effect=lambda *_: order.append('retarget')) as retarget, \
                patch.object(scratch, 'start', side_effect=lambda *_: order.append('worker')), \
                patch.object(workspace, '_ready', return_value={}), \
                patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh', side_effect=lambda *_: order.append('ssh')), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0) as opened:
            self.assertEqual(workspace.remote(self.project, self.state, rebind='new-space'), 0)
        self.assertEqual(choose.call_args.kwargs['rebind'], 'new-space')
        # The worker repoints the alias; then any proxy still serving the old Codespace is ended
        # so Herdr reconnects through it (an older runner's proxy does not leave by itself, #51).
        self.assertEqual(order, ['retarget', 'worker', 'ssh', 'proxies:new-space'])
        # The new Codespace has no Herdr, a fact Herdr setup needs: only it installs Herdr there.
        self.assertEqual(opened.call_args.kwargs['remote_herdr'], '')
        # The old Codespace's checkouts do not exist on the new one, so they are not verified there.
        self.assertEqual(verified, [[self.original]])
        value = read_json(self.state / 'workspace.json')
        self.assertEqual(value['codespace'], 'new-space')
        self.assertEqual(value['verified_remote_historical_roots'], [])
        self.assertNotIn(self.generated, value['verified_historical_roots'])
        self.assertEqual(value['verified_local_historical_roots'], [str(recovered)])
        self.assertEqual(retarget.call_args.args[2:], ('new-space', self.original))

    def test_remote_herdr_reads_the_codespace_version_and_never_guesses(self):
        for answer, expected in ((b'herdr 0.9.4\n', 'herdr 0.9.4'), (b'\n', ''), (b'Welcome!\nherdr 0.9.4\n', None),
                                 (b'zsh: command not found\n', None), (b'herdr 0.9.4\nwarning: stale cache\n', None),
                                 (b'\xff\n', None), (None, None)):
            connection = self.connection(); connection.python.return_value = answer
            with self.subTest(answer=answer):
                self.assertEqual(workspace._remote_herdr(connection), expected)
        connection = self.connection(); connection.python.side_effect = ValueError('Remote Python/SSH timed out')
        self.assertIsNone(workspace._remote_herdr(connection))

    def test_herdr_probe_reports_the_version_on_path_or_in_local_bin(self):
        import stat, subprocess, sys
        home = self.base / 'probe-home'; (home / '.local/bin').mkdir(parents=True)
        def probe():
            return subprocess.run([sys.executable, '-I', '-B', '-c', workspace._HERDR_PROBE], capture_output=True, text=True,
                                  env={'PATH': '/usr/bin:/bin', 'HOME': str(home)}, timeout=60).stdout.strip()
        self.assertEqual(probe(), '')
        fake = home / '.local/bin/herdr'
        fake.write_text('#!/bin/sh\necho herdr 9.9.9-fixture\n'); fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(probe(), 'herdr 9.9.9-fixture')

    def test_verify_historical_root_uses_remote_canonical_identity(self):
        connection = self.connection()
        connection.python.return_value = json.dumps(dict(root=self.generated, identity=self.project.identity)).encode()
        self.assertEqual(workspace._verify_roots(connection, self.project, [self.generated]), [self.generated])
        connection.python.return_value = json.dumps(dict(root=self.generated, identity='github.com/foreign/repo')).encode()
        with self.assertRaisesRegex(ValueError, 'identity'):
            workspace._verify_roots(connection, self.project, [self.generated])

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
        self.assertIsNone(result['herdr_hooks'])
        self.assertIsNone(result['env_files'])
        self.assertIn('Herdr hooks: not checked on space; task work:cs checks them; skill unknown',
                      workspace.status_text(result))
        self.assertIn('Env files: not checked on space; task work:cs copies them',
                      workspace.status_text(result))

    def test_ready_requires_schema_two_receipt_not_old_worker_checkpoint(self):
        self.registry()
        with patch.object(scratch, 'status', return_value=dict(running=True, phase='syncing', checkpoint={'snapshot_id':'old'}, checkpoint_error=None)), \
                patch.object(workspace.time, 'monotonic', side_effect=[0, 1, 121]), patch.object(workspace.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'scratch|Scratch'):
                workspace._ready(self.project, self.state)

    def test_repeated_connection_retains_separate_verified_local_historical_roots(self):
        recovered = self.base / 'recovered'
        shutil.copytree(self.root, recovered)
        self.registry(local_active_root=str(recovered))
        for attempt in range(2):
            with patch.object(workspace, '_prerequisites'), patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), patch.object(workspace, '_verify_roots', return_value=[self.generated]) as verify, patch('devcontainer_setup.project_sync.replication.sync_project_data', return_value=dict(receipt={}, issues=[])), patch.object(scratch, 'retarget'), patch.object(scratch, 'start'), patch.object(workspace, '_ready', return_value={}), patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0), patch('devcontainer_setup.project_sync.notes.configure_memory'):
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
        with patch.object(workspace, '_prerequisites'), patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), patch.object(workspace, '_verify_roots', return_value=[self.generated]), patch('devcontainer_setup.project_sync.replication.sync_project_data', return_value=dict(receipt={}, issues=[])), patch.object(scratch, 'retarget'), patch.object(scratch, 'start'), patch.object(workspace, '_ready', return_value={}), patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0), patch('devcontainer_setup.project_sync.notes.configure_memory'):
            workspace.remote(self.project, self.state)
        archives = list((self.state / 'workspace-history').glob('*.json'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(read_json(archives[0]), expected)

    def test_transcript_failure_does_not_block_prepared_scratch_or_herdr(self):
        self.registry()
        with patch.object(workspace, '_prerequisites'), patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), \
                patch.object(workspace, '_verify_roots', return_value=[self.generated]), \
                patch('devcontainer_setup.project_sync.replication.sync_project_data', side_effect=ValueError('publication refused')) as exchange, \
                patch.object(scratch, 'retarget'), patch.object(scratch, 'start'), \
                patch.object(workspace, '_ready', return_value={}), patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0) as opened:
            self.assertEqual(workspace.remote(self.project, self.state), 0)
        exchange.assert_not_called()
        opened.assert_called_once()

    def test_ready_uses_current_scratch_binding_and_fresh_worker_not_transcript_receipt(self):
        binding = dict(version=1, root=str(self.root), identity=self.project.identity,
                       codespace='space', remote_root=self.original)
        scratch._write(self.state / scratch.CONFIG, binding)
        runtime = dict(running=True, phase='syncing', checkpoint_version=3, runtime_id=scratch._runtime_id(),
                       heartbeat_at=time.time(), prepared_binding=binding, connection_prepared=True,
                       conversations=dict(outcome='degraded'), conversation_error='publication refused')
        with patch.object(scratch, 'status', return_value=runtime), \
                patch.object(scratch, 'inspect_sync', return_value=dict(phase='synchronized')):
            workspace._ready(self.project, self.state, timeout=.01)
        for change, sync_phase in ((dict(heartbeat_at=0), 'synchronized'),
                                   (dict(prepared_binding=dict(binding, codespace='old')), 'synchronized'),
                                   (dict(runtime_id='old'), 'synchronized'), ({}, 'waiting')):
            with self.subTest(change=change, sync_phase=sync_phase), \
                    patch.object(scratch, 'status', return_value=dict(runtime, **change)), \
                    patch.object(scratch, 'inspect_sync', return_value=dict(phase=sync_phase)), \
                    patch.object(workspace.time, 'sleep'):
                with self.assertRaisesRegex(ValueError, 'scratch|Scratch'):
                    workspace._ready(self.project, self.state, timeout=.001)

    def test_local_prerequisite_failure_happens_before_codespace_selection_or_start(self):
        with patch('devcontainer_setup.project_sync.transport._check_gh', side_effect=ValueError('unsupported gh')), \
                patch.object(workspace, '_choose_pairing') as choose:
            with self.assertRaisesRegex(ValueError, 'gh'):
                workspace.remote(self.project, self.state)
        choose.assert_not_called()

    def test_mixed_legacy_history_preserves_local_scope_without_remote_identity_bypass(self):
        recovered = self.base / 'recovered'; shutil.copytree(self.root, recovered)
        self.registry(verified_historical_roots=[str(recovered), self.generated])
        def verify(connection, project, roots):
            self.assertNotIn(str(recovered), roots)
            return roots
        with patch.object(workspace, '_prerequisites'), \
                patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), \
                patch.object(workspace, '_verify_roots', side_effect=verify), \
                patch.object(scratch, 'retarget'), patch.object(scratch, 'start'), \
                patch.object(workspace, '_ready', return_value={}), \
                patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0):
            workspace.remote(self.project, self.state)
        result = read_json(self.state / 'workspace.json')
        self.assertIn(str(recovered), result['verified_local_historical_roots'])
        self.assertIn(self.generated, result['verified_remote_historical_roots'])

    def test_changed_verified_local_history_identity_refuses_connection(self):
        recovered = self.base / 'recovered'; shutil.copytree(self.root, recovered)
        subprocess.run(['git', '-C', str(recovered), 'remote', 'set-url', 'origin', 'https://github.com/foreign/repo'], check=True)
        original = self.registry(verified_local_historical_roots=[str(recovered)])
        with patch.object(workspace, '_prerequisites'), \
                patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), \
                patch.object(workspace, '_verify_roots', return_value=[self.generated]), \
                patch.object(scratch, 'retarget') as retarget, patch.object(scratch, 'start'), \
                patch.object(workspace, '_ready', return_value={}), \
                patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0):
            with self.assertRaisesRegex(ValueError, 'identity'):
                workspace.remote(self.project, self.state)
        retarget.assert_not_called()
        self.assertEqual(read_json(self.state / 'workspace.json'), original)

    def test_remote_prepares_herdr_hooks_for_the_chosen_codespace(self):
        self.registry()
        captured = {}

        def open_remote(*args, **kwargs):
            captured['prepared'] = kwargs['prepared']
            return 0

        with patch.object(workspace, '_prerequisites'), patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), \
                patch.object(workspace, '_verify_roots', return_value=[self.generated]), \
                patch.object(scratch, 'retarget'), patch.object(scratch, 'start'), \
                patch.object(workspace, '_ready', return_value={}), \
                patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', side_effect=open_remote), \
                patch('devcontainer_setup.project_sync.agent_hooks.ensure_on', return_value={}) as ensure_on, \
                patch('devcontainer_setup.project_sync.env_files.ensure_on', return_value={}):
            self.assertEqual(workspace.remote(self.project, self.state), 0)
            ensure_on.assert_not_called()
            captured['prepared']()
        ensure_on.assert_called_once_with('space', self.state)

    def test_remote_prepares_env_files_after_herdr_hooks(self):
        self.registry()
        captured = {}
        order = []

        def open_remote(*args, **kwargs):
            captured['prepared'] = kwargs['prepared']
            return 0

        def hooks(*args):
            order.append(('hooks', args))

        def env(*args):
            order.append(('env', args))

        with patch.object(workspace, '_prerequisites'), \
                patch.object(workspace, '_choose_pairing', return_value=('space', self.original)), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection()), \
                patch.object(workspace, '_verify_roots', return_value=[self.generated]), \
                patch.object(scratch, 'retarget'), patch.object(scratch, 'start'), \
                patch.object(workspace, '_ready', return_value={}), \
                patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', side_effect=open_remote), \
                patch('devcontainer_setup.project_sync.agent_hooks.ensure_on', side_effect=hooks), \
                patch('devcontainer_setup.project_sync.env_files.ensure_on', side_effect=env):
            self.assertEqual(workspace.remote(self.project, self.state), 0)
            self.assertEqual(order, [])
            captured['prepared']()
        self.assertEqual([item[0] for item in order], ['hooks', 'env'])
        self.assertEqual(order[0][1], ('space', self.state))
        self.assertEqual(order[1][1], ('space', self.state, self.root, self.original))

    def test_status_returns_herdr_hooks_without_a_remote_call(self):
        from devcontainer_setup.project_sync.state import atomic_write
        self.registry()
        receipt = {'codespace': 'space', 'checked_at': 1700000000.5,
                   'hooks': {'claude': 'current', 'codex': 'current', 'opencode': 'current'}, 'installed': []}
        atomic_write(self.state / 'herdr-hooks.json', json.dumps(receipt).encode() + b'\n')
        with patch.object(scratch, 'status', return_value=dict(running=False, phase='stopped')), \
                patch('devcontainer_setup.project_sync.codespaces.Codespaces', side_effect=AssertionError('offline')):
            result = workspace.status(self.project, self.state)
        self.assertEqual(result['herdr_hooks']['codespace'], 'space')
        self.assertEqual(result['herdr_hooks']['hooks']['opencode'], 'current')
        self.assertIn('Herdr hooks: claude, codex, opencode current on space', workspace.status_text(result))
        self.assertIn('; skill unknown (task work:cs installs it)', workspace.status_text(result))

    def test_status_returns_env_files_without_a_remote_call(self):
        from devcontainer_setup.project_sync.state import atomic_write
        self.registry()
        receipt = {'codespace': 'space', 'checked_at': 1700000000.5,
                   'files': {'.env': 'copied', 'web/.env': 'current'}}
        atomic_write(self.state / 'env-files.json', json.dumps(receipt).encode() + b'\n')
        with patch.object(scratch, 'status', return_value=dict(running=False, phase='stopped')), \
                patch('devcontainer_setup.project_sync.codespaces.Codespaces', side_effect=AssertionError('offline')):
            result = workspace.status(self.project, self.state)
        self.assertEqual(result['env_files']['files'], receipt['files'])
        text = workspace.status_text(result)
        self.assertIn('Env files: .env copied; web/.env current', text)
        self.assertLess(text.index('Herdr hooks:'), text.index('Env files:'))

    def test_retired_local_recovery_and_agent_launch_apis_absent(self):
        for name in ('local', 'local_project', '_recover'):
            self.assertFalse(hasattr(workspace, name))
