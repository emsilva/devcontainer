"""Conversation-only checkpoints retain verified transport and two-way publication."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from devcontainer_setup.project_sync import replication, rpc
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.state import discover_project, state_dir


class IsolatedPythonConnection:
    codespace = 'fixture-cs'

    def __init__(self, home):
        self.home = home
        home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'CODEX_', 'CLAUDE_'))}
        # A fixture token, so a failed verification's state read never reaches real gh.
        self.env.update(HOME=str(home), XDG_STATE_HOME=str(home / 'state'), GH_TOKEN='fixture-token',
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
        # The Codespace state read after a failed verification stays offline.
        self.reads, self.replies = [], []
        def upstream(url, token):
            self.reads.append((url, token))
            return self.replies.pop(0) if self.replies else (500, b'{}')
        offline = patch('devcontainer_setup.project_sync.transport._upstream', side_effect=upstream)
        offline.start()
        self.addCleanup(offline.stop)

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
        for extra in ({'files': {}}, {'settings': {}}, {'schema': 1},
                      {'conversations': dict(entries=['invalid'], issues=[])},
                      {'conversations': dict(entries=[], issues=['invalid'])}):
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

    def historical_checkout(self):
        legacy = self.base / 'legacy'
        subprocess.run(['git', 'clone', '-q', str(self.root), str(legacy)], check=True)
        subprocess.run(['git', '-C', str(legacy), 'remote', 'set-url', 'origin', self.project.remote], check=True)
        return legacy

    def checkout_receipt(self, root):
        def git(*args):
            return subprocess.check_output(['git', '--no-optional-locks', '-C', str(root), *args])
        return (git('rev-parse', 'HEAD'), (root / '.git/index').read_bytes(),
                git('diff', '--binary'), git('diff', '--cached', '--binary'), (root / 'source').read_bytes())

    def test_real_cycle_publishes_mixed_historical_and_current_claude_scope_without_echo(self):
        legacy = self.historical_checkout()
        remote_sid, remote_path = self.conversation(self.connection.env, text='MIXED', root=legacy)
        rows = [json.loads(row) for row in remote_path.read_bytes().splitlines()]
        rows[-1]['cwd'] = str(self.root)
        remote_path.write_bytes(b''.join(json.dumps(row).encode()+b'\n' for row in rows))
        local_sid, local_path = self.conversation(self.local_env, text='LOCAL')
        (self.root / 'source').write_text('staged'); self.git('add', 'source')
        (self.root / 'source').write_text('unstaged')
        before = self.receipt(), self.checkout_receipt(legacy)
        originals = local_path.read_bytes(), remote_path.read_bytes()
        result = replication.sync_project_data(self.connection, self.project, self.store.state, str(self.root),
                                               env=self.local_env, historical_roots=[str(legacy)])
        self.assertEqual(result['issues'], [])
        self.assertEqual([item['source_session_id'] for item in result['local_publication']['imported']], [remote_sid])
        self.assertEqual([item['source_session_id'] for item in result['remote_publication']['imported']], [local_sid])
        captures = [call['request'] for call in self.connection.calls if call['request']['operation']=='capture']
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0]['root'], str(self.root))
        self.assertEqual(captures[0]['values']['historical_roots'], [str(legacy)])
        again = replication.sync_project_data(self.connection, self.project, self.store.state, str(self.root),
                                              env=self.local_env, historical_roots=[str(legacy)])
        self.assertEqual(again['issues'], [])
        self.assertEqual(again['local_publication']['imported'], [])
        self.assertEqual(again['remote_publication']['imported'], [])
        self.assertEqual(before, (self.receipt(), self.checkout_receipt(legacy)))
        self.assertEqual(originals, (local_path.read_bytes(), remote_path.read_bytes()))

    def test_real_cycle_aggregate_receipt_status_counts_current_conversation_with_empty_history(self):
        from devcontainer_setup.project_sync import workspace
        legacy = self.historical_checkout()
        remote_sid, _ = self.conversation(self.connection.env)
        before = self.receipt(), self.checkout_receipt(legacy)
        result = replication.sync_project_data(self.connection, self.project, self.store.state, str(self.root),
                                               env=self.local_env, historical_roots=[str(legacy)])
        self.assertEqual(result['issues'], [])
        self.assertEqual([item['source_session_id'] for item in result['local_publication']['imported']], [remote_sid])
        with patch.object(workspace.scratch, 'status', return_value=dict(running=True, phase='syncing', checkpoint_version=3, runtime_id=workspace.scratch._runtime_id(), heartbeat_at=time.time(), conversations=result)), patch.object(workspace.scratch, 'inspect_sync', return_value=dict(phase='synchronized')):
            status = workspace.status(self.project, self.store.state)
        self.assertEqual(status['conversations']['directions']['codespace_to_desktop']['counts']['available'], 1)
        self.assertTrue(status['conversations']['healthy'])
        self.assertEqual(self.store.load(result['receipt']['snapshot_id'])['project']['root'], str(self.root))
        self.assertEqual(len(self.store.load(result['receipt']['snapshot_id'])['conversations']['entries']), 1)
        self.assertEqual(before, (self.receipt(), self.checkout_receipt(legacy)))

    def test_real_cycle_rejects_foreign_historical_root_and_preserves_previous_local_receipt(self):
        self.conversation(self.connection.env)
        result = replication.sync_project_data(self.connection, self.project, self.store.state, str(self.root), env=self.local_env)
        legacy = self.historical_checkout()
        subprocess.run(['git', '-C', str(legacy), 'remote', 'set-url', 'origin', 'https://github.com/foreign/repo'], check=True)
        before = self.receipt(), self.checkout_receipt(legacy)
        with self.assertRaises(ValueError):
            replication.sync_project_data(self.connection, self.project, self.store.state, str(self.root), env=self.local_env, historical_roots=[str(legacy)])
        self.assertEqual(self.store.latest('codespace'), result['receipt'])
        self.assertEqual(before, (self.receipt(), self.checkout_receipt(legacy)))

    def test_memory_setting_conflict_is_issue_but_does_not_stop_conversation_cycle(self):
        (self.root / '.claude').mkdir(); settings = self.root / '.claude/settings.local.json'
        settings.write_text('{"autoMemoryDirectory":"/owner/memory"}')
        self.conversation(self.connection.env)
        result = replication.sync_project_data(self.connection, self.project, self.store.state, str(self.root), env=self.local_env)
        self.assertTrue(result['issues'])
        self.assertIn('memory', str(result['issues']).lower())
        self.assertEqual(len(result['local_publication']['imported']), 1)
        self.assertEqual(settings.read_text(), '{"autoMemoryDirectory":"/owner/memory"}')

    def test_real_rpc_outbound_fault_preserves_inbound_custody_and_dirty_checkout(self):
        for failed_operation in ('publish', 'put_blobs'):
            with self.subTest(failed_operation=failed_operation):
                self.conversation(self.local_env, text='LOCAL-' + failed_operation)
                remote_sid, _ = self.conversation(self.connection.env, text='REMOTE-' + failed_operation)
                (self.root / 'source').write_text('staged'); self.git('add', 'source')
                (self.root / 'source').write_text('unstaged')
                before = self.receipt()
                actual = self.connection.python
                def refuse(code, payload=b'', timeout=60):
                    if payload and json.loads(payload)['request']['operation'] == failed_operation:
                        return b'{"ok":false,"error":"injected refusal"}'
                    return actual(code, payload, timeout)
                with patch.object(self.connection, 'python', side_effect=refuse):
                    result = replication.sync_project_data(self.connection, self.project, self.store.state,
                                                           str(self.root), env=self.local_env)
                inbound = result['directions']['codespace_to_desktop']
                outbound = result['directions']['desktop_to_codespace']
                self.assertEqual(result['outcome'], 'degraded')
                self.assertEqual(outbound['outcome'], 'failed')
                self.assertEqual(outbound['stage'], 'publication' if failed_operation == 'publish' else 'transfer')
                self.assertEqual(inbound['outcome'], 'complete')
                self.assertEqual(inbound['receipt'], self.store.latest('codespace'))
                self.assertIn(remote_sid, [row['source_session_id'] for row in inbound['publication']['imported']])
                self.assertGreater(inbound['counts']['available'], 0)
                self.assertEqual(outbound['counts']['available'], 0)
                self.assertEqual(before, self.receipt())

    def test_real_rpc_inbound_fault_preserves_outbound_publication_and_previous_receipt(self):
        self.conversation(self.connection.env)
        previous = replication.sync_project_data(self.connection, self.project, self.store.state,
                                                str(self.root), env=self.local_env)['receipt']
        local_sid, _ = self.conversation(self.local_env)
        (self.root / 'source').write_text('staged'); self.git('add', 'source')
        (self.root / 'source').write_text('unstaged'); before = self.receipt()
        actual = self.connection.python
        def refuse(code, payload=b'', timeout=60):
            if payload and json.loads(payload)['request']['operation'] == 'get_blobs':
                return b'{"ok":false,"error":"injected refusal"}'
            return actual(code, payload, timeout)
        self.conversation(self.connection.env, text='FRESH REMOTE')
        with patch.object(self.connection, 'python', side_effect=refuse):
            result = replication.sync_project_data(self.connection, self.project, self.store.state,
                                                  str(self.root), env=self.local_env)
        outbound = result['directions']['desktop_to_codespace']
        self.assertEqual(outbound['outcome'], 'complete')
        self.assertEqual(outbound['publication']['imported'][0]['source_session_id'], local_sid)
        self.assertEqual(outbound['receipt'], self.remote_store.latest('desktop'))
        self.assertEqual(result['directions']['codespace_to_desktop']['outcome'], 'failed')
        self.assertEqual(self.store.latest('codespace'), previous)
        self.assertEqual(before, self.receipt())

    def test_cycle_progress_is_fresh_and_receipt_bound_with_failed_publication_pending(self):
        self.conversation(self.connection.env)
        progress = []
        with patch('devcontainer_setup.project_sync.conversations.publish_conversations',
                   return_value=dict(imported=[], unchanged=[], issues=[dict(reason='publication refused')])):
            result = replication.sync_project_data(self.connection, self.project, self.store.state,
                                                  str(self.root), env=self.local_env, progress=progress.append)
        inbound = result['directions']['codespace_to_desktop']
        self.assertEqual(inbound['counts']['received'], 1)
        self.assertEqual(inbound['counts']['available'], 0)
        self.assertEqual(inbound['counts']['pending'], 1)
        self.assertEqual(result['outcome'], 'degraded')
        self.assertEqual(progress[0]['outcome'], 'pending')
        self.assertIsNone(progress[0]['completed_at'])
        self.assertIsNone(progress[0]['directions']['codespace_to_desktop']['receipt'])
        self.assertEqual(progress[-1], result)
        self.assertEqual(inbound['snapshot_id'], inbound['receipt']['snapshot_id'])
        self.assertTrue(all(step['cycle_id'] == result['cycle_id'] for step in progress))
        again = replication.sync_project_data(self.connection, self.project, self.store.state,
                                             str(self.root), env=self.local_env)
        self.assertNotEqual(result['cycle_id'], again['cycle_id'])
        self.assertEqual(again['directions']['codespace_to_desktop']['counts']['pending'], 0)
        self.assertEqual(again['directions']['codespace_to_desktop']['publication_issues'], [])

    def test_capture_issues_are_not_double_counted_as_publication_failures(self):
        sid, source = self.conversation(self.connection.env)
        source.write_bytes(source.read_bytes().replace(b'2.1.302', b'9.9.999'))
        result = replication.sync_project_data(self.connection, self.project, self.store.state,
                                              str(self.root), env=self.local_env)
        inbound = result['directions']['codespace_to_desktop']
        self.assertEqual(inbound['counts']['unsupported'], 1)
        self.assertEqual(len(inbound['capture_issues']), 1)
        self.assertEqual(inbound['publication_issues'], [])
        self.assertEqual(inbound['counts']['available'], 0)
        self.assertEqual(inbound['counts']['pending'], 0)
        self.assertEqual(result['outcome'], 'degraded')

    def test_status_does_not_reuse_previous_health_for_partial_stale_or_failed_cycle(self):
        import copy
        import time
        from devcontainer_setup.project_sync import workspace, scratch
        self.conversation(self.connection.env)
        result = replication.sync_project_data(self.connection, self.project, self.store.state,
                                              str(self.root), env=self.local_env)
        runtime = dict(running=True, phase='syncing', checkpoint_version=3, runtime_id=scratch._runtime_id(),
                       heartbeat_at=time.time(), conversations=result)
        for case in ('current', 'partial', 'stale', 'failed', 'wrong_receipt', 'old_worker', 'waiting_scratch'):
            changed = copy.deepcopy(runtime)
            cycle = changed['conversations']
            if case == 'partial': cycle.update(outcome='pending', completed_at=None)
            if case == 'stale': cycle['completed_at'] = 0
            if case == 'failed': cycle.update(outcome='degraded', issues=[dict(reason='failure')])
            if case == 'wrong_receipt': cycle['directions']['codespace_to_desktop']['snapshot_id'] = 'f' * 64
            if case == 'old_worker': changed['runtime_id'] = 'old'
            with self.subTest(case=case), patch.object(scratch, 'status', return_value=changed), \
                    patch.object(scratch, 'inspect_sync', return_value=dict(phase='waiting' if case == 'waiting_scratch' else 'synchronized')):
                status = workspace.status(self.project, self.store.state)['conversations']
            self.assertEqual(status['healthy'], case == 'current')
            if case == 'wrong_receipt':
                self.assertEqual(status['directions']['codespace_to_desktop']['counts']['received'], 0)
            self.assertNotIn('protected', status)
            self.assertEqual(status['retained_receipts']['codespace_to_desktop'], self.store.latest('codespace'))
            if case in ('stale', 'wrong_receipt', 'old_worker'):
                self.assertEqual(status['directions']['codespace_to_desktop']['counts']['available'], 0)
            if case == 'stale':
                self.assertTrue(status['directions']['codespace_to_desktop']['stale'])
                self.assertEqual(status['directions']['codespace_to_desktop']['counts']['pending'], 0)

    def test_authentication_loss_after_transfer_is_a_failed_connection(self):
        from devcontainer_setup.project_sync import workspace
        self.conversation(self.local_env)
        actual = self.connection.python
        lost = False
        def disconnect(code, payload=b'', timeout=60):
            nonlocal lost
            if payload and json.loads(payload)['request']['operation'] == 'publish':
                lost = True
            if lost:
                raise ValueError('SSH authentication failed')
            return actual(code, payload, timeout)
        progress = []
        with patch.object(self.connection, 'python', side_effect=disconnect):
            with self.assertRaisesRegex(ValueError, 'verification|authentication'):
                replication.sync_project_data(self.connection, self.project, self.store.state,
                                              str(self.root), env=self.local_env, progress=progress.append)
        self.assertEqual(progress[-1]['outcome'], 'failed')
        self.assertEqual(progress[-1]['stage'], 'verify')
        self.assertIsNotNone(progress[-1]['directions']['desktop_to_codespace']['receipt'])

    def failed_verification(self, *replies):
        """Run a cycle whose root verification fails; the Codespace reads answer `replies`."""
        self.replies[:] = replies
        with patch('devcontainer_setup.project_sync.workspace._verify_roots',
                   side_effect=ValueError('SSH connection failed')):
            with self.assertRaises(replication.ConnectionVerificationError) as caught:
                replication.sync_project_data(self.connection, self.project, self.store.state,
                                              str(self.root), env=self.local_env)
        return caught.exception

    @staticmethod
    def codespace(state):
        return 200, json.dumps(dict(name='fixture-cs', state=state)).encode()

    def test_verify_reports_shutdown_not_authentication(self):
        error = self.failed_verification(self.codespace('Shutdown'))
        self.assertEqual(str(error), 'Codespace is Shutdown; no start request was allowed.')
        self.assertEqual(error.state, 'Shutdown')
        # One read of this Codespace, with the token github_token(connection.env) hands over.
        self.assertEqual(self.reads, [('https://api.github.com/user/codespaces/fixture-cs', 'fixture-token')])
        self.assertNotIn('fixture-token', str(error))

    def test_verify_reports_unknown_state_when_read_fails(self):
        from devcontainer_setup.project_sync import transport
        unknown = 'Project connection verification failed; Codespace state unknown.'
        for case in ('http', 'name', 'token'):
            with self.subTest(case=case):
                self.reads.clear()
                if case == 'token':
                    with patch.object(transport, 'github_token',
                                      side_effect=transport.TokenTimeout('GitHub authentication timed out')):
                        error = self.failed_verification()
                    self.assertEqual(self.reads, [])
                else:
                    reply = (503, b'{}') if case == 'http' else (200, json.dumps(dict(name='other', state='Shutdown')).encode())
                    error = self.failed_verification(reply)
                    self.assertEqual(len(self.reads), 1)
                self.assertEqual(str(error), unknown)
                self.assertIsNone(error.state)

    def test_verify_keeps_identity_message_when_available(self):
        error = self.failed_verification(self.codespace('Available'))
        self.assertEqual(str(error), 'Project connection verification failed; identity/authentication must be valid')
        self.assertEqual(error.state, 'Available')

    def test_a_passing_verification_reads_no_state(self):
        self.conversation(self.local_env)
        result = replication.sync_project_data(self.connection, self.project, self.store.state,
                                               str(self.root), env=self.local_env)
        self.assertEqual(result['outcome'], 'complete')
        # A failed direction makes verify() run again after the exchange; passing, it reads nothing either.
        actual = self.connection.python
        def refuse_publish(code, payload=b'', timeout=60):
            if payload and json.loads(payload)['request']['operation'] == 'publish':
                raise ValueError('publication refused')
            return actual(code, payload, timeout)
        with patch.object(self.connection, 'python', side_effect=refuse_publish):
            again = replication.sync_project_data(self.connection, self.project, self.store.state,
                                                  str(self.root), env=self.local_env)
        self.assertEqual(again['directions']['desktop_to_codespace']['outcome'], 'failed')
        self.assertEqual(self.reads, [])

    def test_malformed_publication_result_does_not_suppress_opposite_direction(self):
        self.conversation(self.connection.env)
        actual = self.connection.python
        def malformed(code, payload=b'', timeout=60):
            if payload and json.loads(payload)['request']['operation'] == 'publish':
                return b'{"ok":true,"result":{"imported":["invalid"],"unchanged":[],"issues":[]}}'
            return actual(code, payload, timeout)
        with patch.object(self.connection, 'python', side_effect=malformed):
            result = replication.sync_project_data(self.connection, self.project, self.store.state,
                                                  str(self.root), env=self.local_env)
        self.assertEqual(result['directions']['desktop_to_codespace']['outcome'], 'failed')
        self.assertEqual(result['directions']['codespace_to_desktop']['outcome'], 'complete')
        self.assertEqual(result['directions']['codespace_to_desktop']['counts']['available'], 1)

    def test_malformed_outer_rpc_envelopes_retain_outbound_custody_and_advance_inbound(self):
        (self.root / 'source').write_text('staged'); self.git('add', 'source')
        (self.root / 'source').write_text('unstaged'); before = self.receipt()
        for envelope in ([], None, 42, 1.5, True, 'invalid'):
            with self.subTest(envelope=envelope):
                self.conversation(self.local_env, text='LOCAL-' + str(envelope))
                remote_sid, _ = self.conversation(self.connection.env, text='REMOTE-' + str(envelope))
                previous = self.store.latest('codespace')
                actual = self.connection.python
                def malformed(code, payload=b'', timeout=60):
                    if payload and json.loads(payload)['request']['operation'] == 'publish':
                        return json.dumps(envelope).encode()
                    return actual(code, payload, timeout)
                progress = []
                with patch.object(self.connection, 'python', side_effect=malformed):
                    result = replication.sync_project_data(self.connection, self.project, self.store.state,
                                                          str(self.root), env=self.local_env, progress=progress.append)
                outbound, inbound = (result['directions'][name] for name in ('desktop_to_codespace', 'codespace_to_desktop'))
                self.assertEqual(result['outcome'], 'degraded')
                self.assertEqual(progress[-1], result)
                self.assertEqual(outbound['outcome'], 'failed')
                self.assertEqual(outbound['stage'], 'publication')
                self.assertEqual(outbound['receipt'], self.remote_store.latest('desktop'))
                self.assertGreater(outbound['counts']['received'], 0)
                self.assertEqual(outbound['counts']['available'], 0)
                self.assertEqual(inbound['outcome'], 'complete')
                self.assertNotEqual(inbound['receipt'], previous)
                self.assertEqual(inbound['receipt'], self.store.latest('codespace'))
                self.assertIn(remote_sid, [row['source_session_id'] for row in inbound['publication']['imported']])
                self.assertEqual(before, self.receipt())

    def test_stage_timeout_interrupts_work_and_restores_signal_handler(self):
        import signal
        import threading
        from devcontainer_setup.project_sync import sync_cycle
        before = signal.getsignal(signal.SIGALRM)
        cycle = sync_cycle.begin(self.project, str(self.root))
        progress = []
        with patch.object(sync_cycle, 'STAGE_TIMEOUT', .02):
            with self.assertRaisesRegex(TimeoutError, 'timed out'):
                with sync_cycle.stage(cycle, 'publication', progress.append):
                    try:
                        threading.Event().wait(1)
                    except OSError:
                        pass  # Native per-session error handling must not swallow the stage deadline.
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))
        self.assertEqual(progress[0]['stage'], 'publication')
        self.assertLess(progress[0]['stage_deadline_at'] - progress[0]['stage_started_at'], .03)

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
