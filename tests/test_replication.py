import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync import replication
from devcontainer_setup.project_sync.state import discover_project


class ReplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'repo'
        self.root.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '--no-optional-locks', '-c', 'diff.autoRefreshIndex=false', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)
        git('init', '-b', 'main')
        git('config', 'user.email', 'test@example.com')
        git('config', 'user.name', 'Test')
        git('remote', 'add', 'origin', 'https://github.com/example/recovery.git')
        (self.root / 'code.txt').write_text('committed\n')
        git('add', '.')
        git('commit', '-m', 'initial')
        self.project = discover_project(self.root)

    def test_verified_transfer_and_offline_restore(self):
        from devcontainer_setup.project_sync.checkpoint_store import Store
        from devcontainer_setup.project_sync.project_files import restore_files
        source, destination = Store(self.base / 'remote-state'), Store(self.base / 'mac-state')
        (self.root / 'code.txt').write_text('uncommitted\n')
        with patch.object(replication, 'capture_conversations', return_value={'entries': [], 'issues': []}):
            snapshot = replication.capture(self.project, source)
        receipt = replication.transfer(source, destination, snapshot, source='codespace')
        self.assertEqual(receipt['snapshot_id'], snapshot)
        recovered = restore_files(destination, destination.load(snapshot)['files'], self.base / 'recovered')
        self.assertEqual((recovered.root / 'code.txt').read_text(), 'uncommitted\n')
        self.assertEqual(destination.latest('codespace'), receipt)

    def test_interrupted_transfer_does_not_acknowledge(self):
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source, destination = Store(self.base / 'remote-state'), Store(self.base / 'mac-state')
        with patch.object(replication, 'capture_conversations', return_value={'entries': [], 'issues': []}):
            snapshot = replication.capture(self.project, source)
        with patch.object(source, 'read', side_effect=ValueError('disconnected')):
            with self.assertRaises(ValueError):
                replication.transfer(source, destination, snapshot, source='codespace')
        self.assertIsNone(destination.latest('codespace'))

    def test_manifest_digest_and_identity_are_checked_before_ack(self):
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source, destination = Store(self.base / 'remote-state'), Store(self.base / 'mac-state')
        with patch.object(replication, 'capture_conversations', return_value={'entries': [], 'issues': []}):
            snapshot = replication.capture(self.project, source)
        with self.assertRaisesRegex(ValueError, 'identity'):
            replication.transfer(source, destination, snapshot, source='codespace', identity='github.com/wrong/repo')
        self.assertIsNone(destination.latest('codespace'))

    def test_unknown_rpc_operation_and_cross_project_refused(self):
        from devcontainer_setup.project_sync.rpc import LocalEndpoint
        endpoint = LocalEndpoint(self.project, self.base / 'state')
        with self.assertRaises(ValueError):
            endpoint.call('shell', command='rm')
        with self.assertRaises(ValueError):
            endpoint.call('restore', snapshot_id='0' * 64, destination=str(self.root))


    def test_unchanged_capture_reuses_manifest_and_changed_content_gets_new_snapshot(self):
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'remote-state')
        with patch.object(replication, 'capture_conversations', return_value={'entries': [], 'issues': []}):
            first = replication.capture(self.project, source)
            second = replication.capture(self.project, source)
            self.assertEqual(second, first, 'Wall-clock capture time must not defeat immutable manifest deduplication')
            (self.root / 'code.txt').write_text('actual content change')
            third = replication.capture(self.project, source)
        self.assertNotEqual(third, first)


class IsolatedPythonConnection:
    """Execute the real canonical bootstrap in a disposable remote environment."""
    def __init__(self, home):
        import os
        self.home = home
        self.home.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('GIT_', 'CODEX_', 'CLAUDE_'))}
        self.env.update(HOME=str(home), XDG_STATE_HOME=str(home / 'state'),
                        CODEX_HOME=str(home / '.codex'), CLAUDE_CONFIG_DIR=str(home / '.claude'))
        self.calls = []

    def python(self, code, payload=b'', timeout=60):
        import sys
        self.calls.append(json.loads(payload))
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], input=payload,
                                capture_output=True, env=self.env, cwd=self.home, timeout=timeout)
        if result.returncode:
            raise AssertionError('Canonical bootstrap failed: ' + result.stderr.decode(errors='replace'))
        return result.stdout


class CanonicalRPCRecoveryTests(unittest.TestCase):
    def setUp(self):
        ReplicationTests.setUp(self)
        from devcontainer_setup.project_sync.rpc import RemoteEndpoint
        from devcontainer_setup.project_sync.state import state_dir
        from devcontainer_setup.project_sync.checkpoint_store import Store
        self.connection = IsolatedPythonConnection(self.base / 'remote-home')
        self.endpoint = RemoteEndpoint(self.connection, self.root, self.project.identity)
        self.remote_state = state_dir(self.project, self.connection.env)
        self.remote_store = Store(self.remote_state)
        self.mac_store = Store(self.base / 'mac-state')

    def capture_local(self, store):
        with patch.object(replication, 'capture_conversations', return_value={'entries': [], 'issues': []}):
            return replication.capture(self.project, store)

    def source_receipt(self):
        def git(*args):
            return subprocess.check_output(['git', '--no-optional-locks', '-c', 'diff.autoRefreshIndex=false', '-C', str(self.root), *args])
        return (git('rev-parse', 'HEAD'), (self.root / '.git/index').read_bytes(),
                git('ls-files', '--stage', '-z'), git('diff', '--binary'),
                git('diff', '--cached', '--binary'), (self.root / 'code.txt').read_bytes())

    def test_canonical_bootstrap_captures_actual_files_and_pull_restores_offline(self):
        from devcontainer_setup.project_sync.project_files import restore_files
        (self.root / 'code.txt').write_bytes(b'\x00staged\xff')
        subprocess.run(['git', '--no-optional-locks', '-c', 'diff.autoRefreshIndex=false', '-C', str(self.root), 'add', 'code.txt'], check=True)
        (self.root / 'code.txt').write_bytes(b'\x00current\xfe')
        (self.root / 'untracked').write_text('untracked source')
        before = self.source_receipt()
        remote = replication.RemoteStore(self.endpoint)
        snapshot = self.endpoint.call('capture')['snapshot_id']
        receipt = replication.transfer(remote, self.mac_store, snapshot, source='codespace',
                                       identity=self.project.identity)
        self.assertEqual(self.mac_store.latest('codespace'), receipt)
        destination = self.base / 'offline-recovery'
        recovered = restore_files(self.mac_store, self.mac_store.load(snapshot)['files'], destination)
        self.assertEqual((recovered.root / 'code.txt').read_bytes(), b'\x00current\xfe')
        self.assertEqual(subprocess.check_output(['git', '--no-optional-locks', '-c', 'diff.autoRefreshIndex=false', '-C', str(destination), 'show', ':code.txt']), b'\x00staged\xff')
        self.assertEqual((destination / 'untracked').read_text(), 'untracked source')
        self.assertEqual(self.source_receipt(), before)
        self.assertTrue(self.connection.calls)
        self.assertTrue(all({'state', 'checkpoint_store', 'project_files', 'rpc'} <= set(call['modules'])
                            for call in self.connection.calls))
        self.assertTrue(all(call['request']['identity'] == self.project.identity for call in self.connection.calls))

    def test_canonical_remote_store_push_is_verified_and_acknowledged_after_save(self):
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'desktop-state')
        snapshot = self.capture_local(source)
        remote = replication.RemoteStore(self.endpoint)
        receipt = replication.transfer(source, remote, snapshot, source='desktop', identity=self.project.identity)
        self.assertEqual(self.remote_store.latest('desktop'), receipt)
        self.assertEqual(self.remote_store.load(snapshot), source.load(snapshot))
        operations = [call['request']['operation'] for call in self.connection.calls]
        self.assertLess(operations.index('save'), operations.index('acknowledge'))
        count = operations.count('put_blobs')
        self.assertGreater(count, 0)
        replication.transfer(source, remote, snapshot, source='desktop', identity=self.project.identity)
        self.assertEqual(sum(call['request']['operation'] == 'put_blobs' for call in self.connection.calls), count)

    def test_interrupted_real_remote_transfer_preserves_previous_latest(self):
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'desktop-state')
        first = self.capture_local(source)
        remote = replication.RemoteStore(self.endpoint)
        old_receipt = replication.transfer(source, remote, first, source='desktop', identity=self.project.identity)
        (self.root / 'code.txt').write_bytes(b'new not yet acknowledged data')
        (self.root / 'new-file').write_bytes(b'a' * 700)
        (self.root / 'another-file').write_bytes(b'b' * 700)
        second = self.capture_local(source)
        actual_call = self.endpoint.call
        uploads = []
        def disconnected(operation, **values):
            if operation == 'put_blobs':
                uploads.append(values)
                if len(uploads) == 2:
                    raise OSError('connection severed between blob writes')
            return actual_call(operation, **values)
        with patch.object(replication, 'BATCH_BYTES', 768, create=True), patch.object(self.endpoint, 'call', side_effect=disconnected):
            with self.assertRaises(OSError):
                replication.transfer(source, remote, second, source='desktop', identity=self.project.identity)
        self.assertEqual(len(uploads), 2, 'Proof must interrupt after at least one durable batch write')
        first_uploaded = next(iter(uploads[0]['blobs']))
        self.assertEqual(self.remote_store.read(first_uploaded), source.read(first_uploaded))
        self.assertEqual(self.remote_store.latest('desktop'), old_receipt)
        self.assertEqual(self.remote_store.load(first), source.load(first))
        with self.assertRaises(FileNotFoundError):
            self.remote_store.load(second)

    def test_remote_blob_digest_and_manifest_digest_tampering_refuse_latest_replacement(self):
        import base64
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'desktop-state')
        first = self.capture_local(source)
        remote = replication.RemoteStore(self.endpoint)
        previous = replication.transfer(source, remote, first, source='desktop', identity=self.project.identity)
        manifest = source.load(first)
        digest = next(replication.references(manifest))
        real = self.endpoint.call
        def tamper_blob(operation, **values):
            if operation == 'blob':
                return {'data': base64.b64encode(b'wrong bytes for claimed digest').decode()}
            return real(operation, **values)
        with patch.object(self.endpoint, 'call', side_effect=tamper_blob):
            with self.assertRaisesRegex(ValueError, 'digest'):
                remote.read(digest)
        (self.root / 'code.txt').write_text('second snapshot')
        second = self.capture_local(source)
        def tamper_manifest(operation, **values):
            result = real(operation, **values)
            if operation == 'save':
                return {'snapshot_id': 'f' * 64}
            return result
        with patch.object(self.endpoint, 'call', side_effect=tamper_manifest):
            with self.assertRaisesRegex(ValueError, 'manifest digest'):
                replication.transfer(source, remote, second, source='desktop', identity=self.project.identity)
        self.assertEqual(self.remote_store.latest('desktop'), previous)

    def test_canonical_rpc_rejects_bad_digest_and_cross_project_binding(self):
        from devcontainer_setup.project_sync.rpc import RemoteEndpoint
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'desktop-state')
        snapshot = self.capture_local(source)
        manifest = source.load(snapshot)
        for bad in ('../private', 'a' * 63, 'A' * 64):
            with self.subTest(digest=bad), self.assertRaises(ValueError):
                self.endpoint.call('blob', digest=bad)
        foreign = RemoteEndpoint(self.connection, self.root, 'github.com/foreign/project')
        with self.assertRaises(ValueError):
            foreign.call('capture')
        manifest['project']['identity'] = 'github.com/foreign/project'
        with self.assertRaises(ValueError):
            self.endpoint.call('save', manifest=manifest)
        self.assertIsNone(self.remote_store.latest('desktop'))
        self.assertIsNone(self.remote_store.latest('codespace'))

    def test_canonical_rpc_refuses_foreign_file_project_inside_valid_outer_manifest(self):
        import hashlib
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'desktop-state')
        first = self.capture_local(source)
        remote = replication.RemoteStore(self.endpoint)
        previous = replication.transfer(source, remote, first, source='desktop', identity=self.project.identity)
        manifest = source.load(first)
        foreign = manifest['files']['project']
        foreign.update(remote='https://github.com/foreign/other.git', identity='github.com/foreign/other')
        foreign['project_id'] = hashlib.sha256(foreign['identity'].encode()).hexdigest()[:24]
        with self.assertRaisesRegex(ValueError, 'refused|identity|project'):
            self.endpoint.call('save', manifest=manifest)
        self.assertEqual(self.remote_store.latest('desktop'), previous)

    def test_interrupted_remote_pull_preserves_mac_acknowledgement(self):
        remote = replication.RemoteStore(self.endpoint)
        first = self.endpoint.call('capture')['snapshot_id']
        previous = replication.transfer(remote, self.mac_store, first, source='codespace',
                                        identity=self.project.identity)
        (self.root / 'code.txt').write_bytes(b'not fully pulled current file')
        (self.root / 'second-new-file').write_bytes(b'a' * 700)
        (self.root / 'third-new-file').write_bytes(b'b' * 700)
        second = self.endpoint.call('capture')['snapshot_id']
        real = self.endpoint.call
        reads = []
        def disconnect(operation, **values):
            if operation == 'get_blobs':
                reads.append(values['digests'])
                if len(reads) == 2:
                    raise OSError('Codespace disappeared mid-pull')
            return real(operation, **values)
        with patch.object(replication, 'BATCH_BYTES', 768, create=True), patch.object(self.endpoint, 'call', side_effect=disconnect):
            with self.assertRaises(OSError):
                replication.transfer(remote, self.mac_store, second, source='codespace',
                                     identity=self.project.identity)
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.mac_store.read(reads[0][0]), self.remote_store.read(reads[0][0]))
        self.assertEqual(self.mac_store.latest('codespace'), previous)
        self.assertEqual(self.mac_store.load(first)['project']['identity'], self.project.identity)
        with self.assertRaises(FileNotFoundError):
            self.mac_store.load(second)

    def test_batch_transfer_100_files_uses_few_canonical_rpc_calls(self):
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'desktop-state')
        for number in range(100):
            (self.root / f'file-{number:03}').write_text(f'unique content {number}\n')
        snapshot = self.capture_local(source)
        real = self.connection.python
        def bounded_calls(*args, **kwargs):
            self.assertLess(len(self.connection.calls), 5, '100-file transfer must use at most five RPCs')
            return real(*args, **kwargs)
        remote = replication.RemoteStore(self.endpoint)
        with patch.object(self.connection, 'python', side_effect=bounded_calls):
            replication.transfer(source, remote, snapshot, source='desktop', identity=self.project.identity)
        push = [call['request']['operation'] for call in self.connection.calls]
        self.assertIn('put_blobs', push)
        self.assertNotIn('put_blob', push)
        self.connection.calls.clear()
        with patch.object(self.connection, 'python', side_effect=bounded_calls):
            replication.transfer(remote, self.mac_store, snapshot, source='codespace', identity=self.project.identity)
        pull = [call['request']['operation'] for call in self.connection.calls]
        self.assertIn('get_blobs', pull)
        self.assertNotIn('blob', pull)
        self.assertEqual(self.mac_store.load(snapshot), source.load(snapshot))
        self.connection.calls.clear()
        replication.transfer(source, remote, snapshot, source='desktop', identity=self.project.identity)
        self.assertLessEqual(len(self.connection.calls), 3)
        self.assertNotIn('put_blobs', [call['request']['operation'] for call in self.connection.calls])

    def test_batch_rpc_rejects_invalid_digest_corruption_and_oversized_batches(self):
        import base64
        import hashlib
        from devcontainer_setup.project_sync import rpc
        endpoint = rpc.LocalEndpoint(self.project, self.remote_state)
        body = b'valid body'
        digest = hashlib.sha256(body).hexdigest()
        with self.assertRaisesRegex(ValueError, 'digest'):
            endpoint.call('put_blobs', blobs={digest: base64.b64encode(b'wrong body').decode()})
        with self.assertRaises(FileNotFoundError):
            self.remote_store.read(digest)
        for operation in ('has_blobs', 'get_blobs'):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                endpoint.call(operation, digests=['../escape'])
        with patch.object(rpc, 'BATCH_BYTES', 4, create=True):
            with self.assertRaisesRegex(ValueError, 'batch|bound|size'):
                endpoint.call('put_blobs', blobs={digest: base64.b64encode(body).decode()})
        self.remote_store.put(body)
        with patch.object(rpc, 'BATCH_BYTES', 4, create=True):
            with self.assertRaisesRegex(ValueError, 'batch|bound|size'):
                endpoint.call('get_blobs', digests=[digest])
        obj = self.remote_state / 'checkpoints/objects' / digest
        obj.chmod(0o600)
        obj.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'hash|digest|corrupt'):
            endpoint.call('has_blobs', digests=[digest])
        self.assertIsNone(self.remote_store.latest('desktop'))

    def test_transfer_rejects_tampered_batch_body_before_acknowledgement(self):
        import base64
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'desktop-state')
        snapshot = self.capture_local(source)
        remote = replication.RemoteStore(self.endpoint)
        replication.transfer(source, remote, snapshot, source='desktop', identity=self.project.identity)
        real = self.endpoint.call
        def tampered(operation, **values):
            result = real(operation, **values)
            if operation == 'get_blobs':
                digest = next(iter(result['blobs']))
                result['blobs'][digest] = base64.b64encode(b'changed in transit').decode()
            return result
        with patch.object(self.endpoint, 'call', side_effect=tampered):
            with self.assertRaisesRegex(ValueError, 'digest'):
                replication.transfer(remote, self.mac_store, snapshot, source='codespace', identity=self.project.identity)
        self.assertIsNone(self.mac_store.latest('codespace'))

    def test_large_single_object_uses_compatible_bounded_rpc(self):
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'desktop-state')
        (self.root / 'large-for-batch').write_bytes(b'x' * 2048)
        snapshot = self.capture_local(source)
        remote = replication.RemoteStore(self.endpoint)
        with patch.object(replication, 'BATCH_BYTES', 1024, create=True):
            replication.transfer(source, remote, snapshot, source='desktop', identity=self.project.identity)
            replication.transfer(remote, self.mac_store, snapshot, source='codespace', identity=self.project.identity)
        operations = [call['request']['operation'] for call in self.connection.calls]
        self.assertIn('put_blob', operations)
        self.assertIn('blob', operations)
        self.assertEqual(self.mac_store.load(snapshot), source.load(snapshot))

    def test_bad_batch_receipt_refuses_new_acknowledgement_after_durable_upload(self):
        from devcontainer_setup.project_sync.checkpoint_store import Store
        source = Store(self.base / 'desktop-state')
        first = self.capture_local(source)
        remote = replication.RemoteStore(self.endpoint)
        previous = replication.transfer(source, remote, first, source='desktop', identity=self.project.identity)
        (self.root / 'code.txt').write_bytes(b'next generation')
        second = self.capture_local(source)
        actual = self.endpoint.call
        uploaded = []
        def wrong_receipt(operation, **values):
            result = actual(operation, **values)
            if operation == 'put_blobs':
                uploaded.extend(values['blobs'])
                return {'digests': ['f' * 64]}
            return result
        with patch.object(self.endpoint, 'call', side_effect=wrong_receipt):
            with self.assertRaisesRegex(ValueError, 'digest'):
                replication.transfer(source, remote, second, source='desktop', identity=self.project.identity)
        self.assertTrue(uploaded)
        self.assertEqual(self.remote_store.read(uploaded[0]), source.read(uploaded[0]))
        self.assertEqual(self.remote_store.latest('desktop'), previous)

    def test_batch_validation_precedes_any_write_and_caps_reference_count(self):
        import base64
        import hashlib
        from devcontainer_setup.project_sync import rpc
        endpoint = rpc.LocalEndpoint(self.project, self.remote_state)
        body = b'valid first object'
        digest = hashlib.sha256(body).hexdigest()
        with self.assertRaisesRegex(ValueError, 'digest'):
            endpoint.call('put_blobs', blobs={digest: base64.b64encode(body).decode(),
                                             'f' * 64: base64.b64encode(b'invalid second object').decode()})
        with self.assertRaises(FileNotFoundError):
            self.remote_store.read(digest)
        for operation in ('has_blobs', 'get_blobs'):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'bound'):
                endpoint.call(operation, digests=[digest] * (rpc.BATCH_ITEMS + 1))
        with patch.object(rpc, 'SINGLE_BYTES', 4):
            with self.assertRaisesRegex(ValueError, 'bound'):
                endpoint.call('put_blob', data=base64.b64encode(body).decode())
