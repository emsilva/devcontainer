"""Conversation-only checkpoints retain verified transport and two-way publication."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from devcontainer_setup.project_sync import replication, rpc
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.state import discover_project, state_dir


class IsolatedPythonConnection:
    def __init__(self, home):
        self.home = home
        home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'CODEX_', 'CLAUDE_'))}
        self.env.update(HOME=str(home), XDG_STATE_HOME=str(home / 'state'),
                        CODEX_HOME=str(home / '.codex'), CLAUDE_CONFIG_DIR=str(home / '.claude'))
        self.calls = []

    def python(self, code, payload=b'', timeout=60):
        if payload:
            self.calls.append(json.loads(payload))
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], input=payload,
                                capture_output=True, env=self.env, cwd=self.home, timeout=timeout)
        if result.returncode:
            raise AssertionError(result.stderr.decode(errors='replace'))
        return result.stdout


class ReplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'repo'; self.root.mkdir()
        for args in [('init', '-q'), ('remote', 'add', 'origin', 'https://github.com/example/recovery.git'),
                     ('config', 'user.email', 'fixture@example.com'), ('config', 'user.name', 'Fixture')]:
            self.git(*args)
        (self.root / 'source').write_bytes(b'private source, never a checkpoint object')
        self.git('add', '.'); self.git('commit', '-qm', 'Initial')
        self.project = discover_project(self.root)
        self.store = Store(self.base / 'local-state')
        self.connection = IsolatedPythonConnection(self.base / 'remote-home')
        self.endpoint = rpc.RemoteEndpoint(self.connection, self.root, self.project.identity)
        self.remote_store = Store(state_dir(self.project, self.connection.env))
        self.local_env = dict(self.connection.env, HOME=str(self.base / 'local-home'),
                              CODEX_HOME=str(self.base / 'local-home/.codex'),
                              CLAUDE_CONFIG_DIR=str(self.base / 'local-home/.claude'))

    def git(self, *args):
        return subprocess.check_output(['git', '--no-optional-locks', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def receipt(self):
        return (self.git('rev-parse', 'HEAD'), (self.root / '.git/index').read_bytes(),
                self.git('diff', '--binary'), self.git('diff', '--cached', '--binary'),
                (self.root / 'source').read_bytes())

    def conversation(self, env, *, text='ORCHID', root=None):
        from devcontainer_setup.project_sync.sessions import claude_namespace
        sid = str(uuid.uuid4())
        target = Path(env['CLAUDE_CONFIG_DIR']) / 'projects' / claude_namespace(root or self.root) / (sid + '.jsonl')
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = [dict(type='user', uuid=str(uuid.uuid4()), message=dict(role='user', content='word?')),
                dict(type='assistant', uuid=str(uuid.uuid4()), message=dict(role='assistant', content=[dict(type='text', text=text)], stop_reason='end_turn'))]
        target.write_bytes(b''.join(json.dumps(dict(row, cwd=str(root or self.root), sessionId=sid, version='2.1.302')).encode()+b'\n' for row in rows))
        return sid, target

    def snapshot(self, *, count=1, size=32):
        entries = [dict(blob=self.store.put((str(i)+':').encode()+b'x'*size)) for i in range(count)]
        conversations = dict(schema=1, entries=entries, issues=[])
        with patch.object(replication, 'capture_conversations', return_value=conversations):
            return replication.capture(self.project, self.store)

    def test_capture_contains_only_conversations_and_never_reads_source_or_index(self):
        self.conversation(self.local_env)
        (self.root / 'source').write_text('staged'); self.git('add', 'source')
        (self.root / 'source').write_text('unstaged')
        before = self.receipt()
        snapshot = replication.capture(self.project, self.store, env=self.local_env)
        manifest = self.store.load(snapshot)
        self.assertEqual(manifest['schema'], 2)
        self.assertEqual(set(manifest), {'schema', 'project', 'conversations', 'producer'})
        self.assertEqual(len(manifest['conversations']['entries']), 1)
        self.assertNotIn(b'private source', b''.join(self.store.read(d) for d in replication.references(manifest)))
        self.assertEqual(before, self.receipt())
        (self.root / 'source').write_text('different code')
        self.assertEqual(replication.capture(self.project, self.store, env=self.local_env), snapshot)

    def test_schema_one_remains_readable_but_never_transferred(self):
        legacy = self.store.save(dict(schema=1, project=dict(identity=self.project.identity, project_id=self.project.project_id), files={}))
        before = self.store.load(legacy)
        with self.assertRaisesRegex(ValueError, 'schema|conversation'):
            replication.transfer(self.store, Store(self.base / 'dest'), legacy, source='desktop')
        self.assertEqual(self.store.load(legacy), before)

    def test_new_transfer_rejects_unknown_surface_and_wrong_project(self):
        snapshot = self.snapshot()
        manifest = self.store.load(snapshot)
        for extra in ({'files': {}}, {'settings': {}}, {'schema': 1}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                rpc.LocalEndpoint(self.project, self.base / 'rpc').call('save', manifest=dict(manifest, **extra))
        with self.assertRaisesRegex(ValueError, 'identity'):
            replication.transfer(self.store, Store(self.base / 'dest'), snapshot, source='desktop', identity='github.com/foreign/repo')

    def test_restore_operation_and_repository_replication_module_are_removed(self):
        self.assertNotIn('restore', rpc.OPERATIONS)
        self.assertFalse((Path(replication.__file__).parent / 'project_files.py').exists())
        for endpoint in (self.endpoint, rpc.LocalEndpoint(self.project, self.base / 'rpc')):
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                endpoint.call('restore', snapshot_id='0'*64, destination=str(self.root))

    def test_real_two_way_cycle_publishes_both_directions_and_retry_has_no_echo(self):
        local_sid, local_path = self.conversation(self.local_env, text='LOCAL')
        remote_sid, remote_path = self.conversation(self.connection.env, text='REMOTE')
        before = self.receipt(); originals = local_path.read_bytes(), remote_path.read_bytes()
        result = replication.sync_project_data(self.connection, self.project, self.store.state, str(self.root), env=self.local_env)
        self.assertEqual(result['local_publication']['issues'], [])
        self.assertEqual(result['remote_publication']['issues'], [])
        self.assertEqual(len(result['local_publication']['imported']), 1)
        self.assertEqual(result['local_publication']['imported'][0]['source_session_id'], remote_sid)
        self.assertEqual(result['remote_publication']['imported'][0]['source_session_id'], local_sid)
        self.assertEqual(self.store.latest('codespace'), result['receipt'])
        again = replication.sync_project_data(self.connection, self.project, self.store.state, str(self.root), env=self.local_env)
        self.assertEqual(again['local_publication']['imported'], [])
        self.assertEqual(again['remote_publication']['imported'], [])
        self.assertEqual(before, self.receipt())
        self.assertEqual(originals, (local_path.read_bytes(), remote_path.read_bytes()))
        self.assertFalse(any(c['request']['operation'] == 'restore' for c in self.connection.calls))
        self.assertTrue(all('project_files' not in c['modules'] for c in self.connection.calls))

    def test_memory_setting_conflict_is_issue_but_does_not_stop_conversation_cycle(self):
        (self.root / '.claude').mkdir(); settings = self.root / '.claude/settings.local.json'
        settings.write_text('{"autoMemoryDirectory":"/owner/memory"}')
        self.conversation(self.connection.env)
        result = replication.sync_project_data(self.connection, self.project, self.store.state, str(self.root), env=self.local_env)
        self.assertTrue(result['issues'])
        self.assertIn('memory', str(result['issues']).lower())
        self.assertEqual(len(result['local_publication']['imported']), 1)
        self.assertEqual(settings.read_text(), '{"autoMemoryDirectory":"/owner/memory"}')

    def test_batch_transport_and_retry_only_send_missing_verified_objects(self):
        snapshot = self.snapshot(count=100)
        remote = replication.RemoteStore(self.endpoint)
        replication.transfer(self.store, remote, snapshot, source='desktop', identity=self.project.identity)
        self.assertLessEqual(len(self.connection.calls), 5)
        self.assertIn('put_blobs', [c['request']['operation'] for c in self.connection.calls])
        self.connection.calls.clear()
        replication.transfer(self.store, remote, snapshot, source='desktop', identity=self.project.identity)
        self.assertNotIn('put_blobs', [c['request']['operation'] for c in self.connection.calls])
        self.connection.calls.clear()
        dest = Store(self.base / 'dest')
        replication.transfer(remote, dest, snapshot, source='codespace', identity=self.project.identity)
        self.assertEqual(dest.load(snapshot), self.store.load(snapshot))
        self.assertLessEqual(len(self.connection.calls), 5)

    def test_interrupted_transfer_keeps_previous_ack_and_retry_finishes(self):
        remote = replication.RemoteStore(self.endpoint)
        first = self.snapshot()
        previous = replication.transfer(self.store, remote, first, source='desktop')
        second = self.snapshot(count=3, size=700)
        actual, uploads = self.endpoint.call, []
        def disconnected(operation, **values):
            if operation == 'put_blobs':
                uploads.append(values)
                if len(uploads) == 2: raise OSError('disconnected')
            return actual(operation, **values)
        with patch.object(replication, 'BATCH_BYTES', 768), patch.object(self.endpoint, 'call', side_effect=disconnected):
            with self.assertRaises(OSError): replication.transfer(self.store, remote, second, source='desktop')
        self.assertEqual(self.remote_store.latest('desktop'), previous)
        self.assertEqual(self.remote_store.read(next(iter(uploads[0]['blobs']))), self.store.read(next(iter(uploads[0]['blobs']))))
        replication.transfer(self.store, remote, second, source='desktop')
        self.assertEqual(self.remote_store.latest('desktop')['snapshot_id'], second)

    def test_corrupt_remote_body_and_manifest_receipt_do_not_advance_ack(self):
        first = self.snapshot(); remote = replication.RemoteStore(self.endpoint)
        previous = replication.transfer(self.store, remote, first, source='desktop')
        actual = self.endpoint.call
        def tamper(operation, **values):
            if operation == 'get_blobs': return dict(blobs={d: base64.b64encode(b'corrupt').decode() for d in values['digests']})
            result = actual(operation, **values)
            if operation == 'save': result['snapshot_id'] = 'f'*64
            return result
        second = self.snapshot(count=2)
        with patch.object(self.endpoint, 'call', side_effect=tamper):
            with self.assertRaisesRegex(ValueError, 'digest'): replication.transfer(self.store, remote, second, source='desktop')
            with self.assertRaisesRegex(ValueError, 'digest'): replication.transfer(remote, Store(self.base/'dest'), first, source='codespace')
        self.assertEqual(self.remote_store.latest('desktop'), previous)

    def test_rpc_validates_entire_batch_before_writing_and_bounds_reads(self):
        endpoint = rpc.LocalEndpoint(self.project, self.base / 'rpc')
        digest = self.store.put(b'body')
        with self.assertRaises(ValueError): endpoint.call('put_blobs', blobs={digest: base64.b64encode(b'wrong').decode()})
        with self.assertRaises(FileNotFoundError): endpoint.store.read(digest)
        for digests in (['../escape'], [digest, digest], ['0'*64]*1025):
            with self.assertRaises(ValueError): endpoint.call('has_blobs', digests=digests)
        with patch.object(rpc, 'BATCH_BYTES', 2):
            with self.assertRaisesRegex(ValueError, 'bound|size'): endpoint.call('put_blobs', blobs={digest: base64.b64encode(b'body').decode()})
        foreign = rpc.RemoteEndpoint(self.connection, self.root, 'github.com/foreign/repo')
        with self.assertRaises(ValueError): foreign.call('capture')

    def test_old_registry_path_fields_cannot_expand_native_capture_scope(self):
        foreign = self.base / 'unrelated'; foreign.mkdir()
        self.conversation(self.local_env, root=foreign)
        from devcontainer_setup.project_sync import workspace
        workspace._write(self.store.state, dict(schema=1, identity=self.project.identity, project_id=self.project.project_id,
                         local_root=str(self.root), local_active_root=str(self.root), remote_root=str(foreign), remote_active_root=str(foreign)))
        captured = self.store.load(replication.capture(self.project, self.store, env=self.local_env))
        self.assertEqual(captured['conversations']['entries'], [])

    def test_typed_verified_historical_roots_remain_native_capture_scopes(self):
        legacy = self.base / 'historical'; legacy.mkdir()
        subprocess.run(['git', 'init', '-q', str(legacy)], check=True)
        subprocess.run(['git', '-C', str(legacy), 'remote', 'add', 'origin', self.project.remote], check=True)
        self.assertEqual(discover_project(legacy).identity, self.project.identity)
        self.conversation(self.local_env, root=legacy)
        from devcontainer_setup.project_sync import workspace
        workspace._write(self.store.state, dict(schema=1, identity=self.project.identity, project_id=self.project.project_id,
                         verified_local_historical_roots=[str(legacy)], verified_remote_historical_roots=[]))
        captured = self.store.load(replication.capture(self.project, self.store, env=self.local_env))
        self.assertEqual(len(captured['conversations']['entries']), 1)

    def test_remote_capture_accepts_only_git_verified_historical_roots(self):
        legacy = self.base / 'legacy'; subprocess.run(['git', 'clone', '-q', str(self.root), str(legacy)], check=True)
        subprocess.run(['git', '-C', str(legacy), 'remote', 'set-url', 'origin', self.project.remote], check=True)
        self.conversation(self.connection.env, root=legacy)
        snapshot = self.endpoint.call('capture', historical_roots=[str(legacy)])['snapshot_id']
        self.assertEqual(len(self.remote_store.load(snapshot)['conversations']['entries']), 1)
        subprocess.run(['git', '-C', str(legacy), 'remote', 'set-url', 'origin', 'https://github.com/foreign/repo'], check=True)
        with self.assertRaises(ValueError): self.endpoint.call('capture', historical_roots=[str(legacy)])
