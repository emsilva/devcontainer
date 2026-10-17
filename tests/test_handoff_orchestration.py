import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

from devcontainer_setup.project_sync import handoff as module
from devcontainer_setup.project_sync.state import discover_project


SID = '12345678-1234-4234-8234-123456789abc'


class Endpoint:
    def __init__(self, name, events, identity='github.com/a/b'):
        self.name, self.events, self.identity = name, events, identity
        self.root = '/mac/repo' if name == 'mac' else '/workspaces/repo'
        self.owner = {'status': 'unknown', 'agent': 'codex', 'session_id': SID}
        self.prepared = None
        self.fail_after = None
        self.mismatch = False

    def call(self, operation, **values):
        self.events.append((self.name, operation))
        if operation == 'describe':
            result = dict(host=self.name, root=self.root, identity=self.identity, project_id=hashlib.sha256(self.identity.encode()).hexdigest()[:24])
        elif operation == 'status':
            result = self.owner
        elif operation == 'backup':
            result = {'path': '/private/' + self.name + '-scratch.tar.gz'}
        elif operation == 'inspect':
            result = {'files': 1, 'generation': self.owner.get('generation', 0)}
        elif operation == 'source_prepare':
            if self.prepared is None:
                if self.owner.get('status') != 'idle' or self.owner.get('host') != self.name:
                    raise ValueError('Source owner is unknown or uncertain')
                transfer = dict(self.owner, schema=1, status='transferring', destination_host=values['destination_host'], nonce=str(uuid.uuid4()), generation=self.owner['generation'] + 1)
                self.owner = transfer
                self.prepared = dict(transfer=transfer, bundle={'source_host': self.name, 'generation': transfer['generation'], 'session_id': SID}, history={'turns': [1], 'items': [2]})
            result = self.prepared
        elif operation == 'target_prepare':
            if self.owner['status'] not in ('unknown', 'receiving', 'accepted'):
                raise ValueError('Target ownership is uncertain')
            self.owner = dict(values['transfer'], status='receiving')
            result = self.owner
        elif operation == 'import':
            result = dict(status='complete', bundle_id='digest', session_id=SID, agent='codex', source_host=values['bundle']['source_host'], generation=values['bundle']['generation'])
        elif operation == 'verify':
            if self.mismatch:
                raise ValueError('Native histories differ; receipts retained')
            result = {'verified': True}
        elif operation == 'accept':
            self.owner = dict(values['transfer'], status='accepted', bundle_id='digest')
            result = self.owner
        elif operation == 'complete':
            self.owner = dict(values['acknowledgment'], status='transferred')
            result = self.owner
        elif operation == 'activate':
            self.owner = dict(values['committed'], status='idle', host=self.name)
            result = self.owner
        else:
            raise AssertionError(operation)
        result = copy.deepcopy(result)
        if self.fail_after == operation:
            self.fail_after = None
            raise ValueError('Response lost after ' + operation)
        return result


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        subprocess.run(['git', 'init', '-q', str(self.root / 'repo')], check=True)
        subprocess.run(['git', '-C', str(self.root / 'repo'), 'remote', 'add', 'origin', 'git@github.com:a/b'], check=True)
        self.project = discover_project(self.root / 'repo')
        self.state = self.root / 'state'
        self.events = []
        self.local = Endpoint('mac', self.events)
        self.local.root = str(self.project.root)
        self.remote = Endpoint('codespace', self.events)
        self.config = dict(identity=self.project.identity, root=str(self.project.root), codespace='selected', remote_root=self.remote.root)
        self.patcher = mock.patch.object(module, '_config', return_value=self.config)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.local.owner = dict(schema=1, status='idle', agent='codex', session_id=SID, host='mac', generation=0)

    def run_handoff(self, direction='codespace'):
        return module.handoff(self.project, 'codex', SID, direction, self.state, local=self.local, remote=self.remote)

    def test_transfer_orders_backups_reservation_verification_and_release(self):
        result = self.run_handoff()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.local.owner['status'], 'transferred')
        self.assertEqual(self.remote.owner['status'], 'idle')
        names = [operation for _, operation in self.events]
        self.assertLess(names.index('backup'), names.index('source_prepare'))
        self.assertEqual(names.count('backup'), 2)
        for before, after in zip(('source_prepare', 'target_prepare', 'import', 'verify', 'accept', 'complete'), ('target_prepare', 'import', 'verify', 'accept', 'complete', 'activate')):
            self.assertLess(names.index(before), names.index(after))
        self.assertTrue(list(self.state.rglob('bundle.json')))
        before = len(self.events)
        self.assertEqual(self.run_handoff()['request_id'], result['request_id'])
        self.assertFalse(any(op in ('import', 'source_prepare', 'activate') for _, op in self.events[before:]))

    def test_response_loss_at_every_mutating_boundary_converges_same_nonce(self):
        for endpoint, operation in (('mac', 'source_prepare'), ('codespace', 'target_prepare'), ('codespace', 'import'), ('codespace', 'verify'), ('codespace', 'accept'), ('mac', 'complete'), ('codespace', 'activate')):
            with self.subTest(operation=operation):
                self.state = self.root / operation
                self.local.prepared = None
                self.local.owner = dict(schema=1, status='idle', agent='codex', session_id=SID, host='mac', generation=0)
                self.remote.owner = {'status': 'unknown'}
                target = self.local if endpoint == 'mac' else self.remote
                target.fail_after = operation
                with self.assertRaises(ValueError):
                    self.run_handoff()
                nonce = self.local.owner['nonce']
                result = self.run_handoff()
                self.assertEqual(result['nonce'], nonce)
                self.assertEqual(self.remote.owner['nonce'], nonce)
                self.assertEqual(self.remote.owner['status'], 'idle')

    def test_reverse_direction_uses_codespace_as_source(self):
        self.local.owner = {'status': 'unknown'}
        self.remote.owner = dict(schema=1, status='idle', agent='codex', session_id=SID, host='codespace', generation=0)
        result = self.run_handoff('mac')
        self.assertEqual(result['destination_host'], 'mac')
        self.assertEqual(self.remote.owner['status'], 'transferred')
        self.assertEqual(self.local.owner['status'], 'idle')

    def test_history_mismatch_preserves_pending_custody_and_retry_receipt(self):
        self.remote.mismatch = True
        with self.assertRaises(ValueError):
            self.run_handoff()
        self.assertEqual(self.local.owner['status'], 'transferring')
        self.assertEqual(self.remote.owner['status'], 'receiving')
        self.assertFalse(any(op == 'complete' for _, op in self.events))
        self.assertTrue(list(self.state.rglob('bundle.json')))
        self.remote.mismatch = False
        self.assertEqual(self.run_handoff()['status'], 'complete')

    def test_wrong_binding_refuses_before_backup_or_custody_change(self):
        self.remote.identity = 'github.com/other/repo'
        with self.assertRaises(ValueError):
            self.run_handoff()
        self.assertFalse(any(op in ('backup', 'source_prepare') for _, op in self.events))

    def test_unknown_owner_and_changed_pending_peer_refuse(self):
        self.local.owner = {'status': 'unknown'}
        with self.assertRaises(ValueError):
            self.run_handoff()
        self.assertEqual(self.remote.owner['status'], 'unknown')

    def test_completed_retry_refuses_changed_target_receipt(self):
        self.run_handoff()
        self.remote.owner = dict(self.remote.owner, generation=99, nonce=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, 'receipt|ownership'):
            self.run_handoff()

    def test_check_validates_export_without_ownership_writes_or_native_resume(self):
        result = module.check(self.project, 'codex', SID, 'codespace', self.state, local=self.local, remote=self.remote)
        self.assertEqual(result['status'], 'checked')
        self.assertTrue(any(op == 'inspect' for _, op in self.events))
        self.assertFalse(any(op in ('backup', 'source_prepare', 'target_prepare', 'verify') for _, op in self.events))
        self.assertFalse(self.state.exists())


class EndpointTests(unittest.TestCase):
    def setUp(self):
        HandoffTests.setUp(self)
        patcher = mock.patch.object(module, 'host_id', return_value='mac')
        patcher.start()
        self.addCleanup(patcher.stop)

    def reserved(self):
        return mock.patch.object(module.ownership, 'owner_status', return_value=dict(status='receiving', host='source', destination_host='mac', generation=1))

    def test_source_preflight_then_native_history_under_lease_before_export_generation(self):
        from devcontainer_setup.project_sync import ownership
        state = self.root / 'endpoint'
        with ownership.managed_lease(state, 'codex', SID, 'mac', new=True):
            pass
        endpoint = module.LocalEndpoint(self.project, state)
        events = []
        def export(*args, **kwargs):
            events.append(('export', kwargs['generation']))
            return dict(source_host='mac', generation=kwargs['generation'], session_id=SID, files=[])
        def history(*args, **kwargs):
            with self.assertRaisesRegex(ValueError, 'Session lease is active'):
                with ownership.managed_lease(state, 'codex', SID, 'mac'):
                    pass
            events.append(('history', 0))
            return {'turns': [], 'items': []}
        with mock.patch.object(module, 'host_id', return_value='mac'), mock.patch.object(module.sessions, 'export_snapshot', side_effect=export), mock.patch.object(module.agents, 'native_history', side_effect=history):
            result = endpoint.call('source_prepare', agent='codex', session_id=SID, request_id=str(uuid.uuid4()), destination_host='codespace')
        self.assertEqual(events[0], ('export', 0))
        self.assertLess(events.index(('history', 0)), events.index(('export', 1)))
        self.assertEqual(result['transfer']['generation'], result['bundle']['generation'])

    def test_codex_verification_compares_every_turn_and_item_ignoring_runtime_roots(self):
        endpoint = module.LocalEndpoint(self.project, self.state)
        expected = dict(turns=[{'id': 1}], items=[{'id': 2}], cwd='/source', runtime_workspace_roots=['/source'])
        actual = dict(expected, cwd=str(self.project.root), runtime_workspace_roots=[str(self.project.root)])
        with self.reserved(), mock.patch.object(module.sessions, '_preflight', return_value=[]), mock.patch.object(module.agents, 'native_history', return_value=actual):
            result = endpoint.call('verify', agent='codex', session_id=SID, bundle={'source_host': 'source', 'generation': 1}, history=expected)
            self.assertTrue(result['verified'])
        actual['items'] = [{'id': 2}, {'id': 3}]
        with self.reserved(), mock.patch.object(module.sessions, '_preflight', return_value=[]), mock.patch.object(module.agents, 'native_history', return_value=actual):
            with self.assertRaisesRegex(ValueError, 'histor'):
                endpoint.call('verify', agent='codex', session_id=SID, bundle={'source_host': 'source', 'generation': 1}, history=expected)

    def test_claude_verification_checks_whole_bytes_without_native_model_turn(self):
        path = self.root / 'claude.jsonl'
        path.write_bytes(b'complete historical bytes\n')
        endpoint = module.LocalEndpoint(self.project, self.state)
        plan = [dict(destination=path, data=path.read_bytes())]
        with self.reserved(), mock.patch.object(module.sessions, '_preflight', return_value=plan), mock.patch.object(module.agents, 'native_history') as native:
            self.assertTrue(endpoint.call('verify', agent='claude', session_id=SID, bundle={'source_host': 'source', 'generation': 1}, history=None)['verified'])
            path.write_bytes(path.read_bytes() + b'extra destination bytes\n')
            with self.assertRaisesRegex(ValueError, 'bytes'):
                endpoint.call('verify', agent='claude', session_id=SID, bundle={'source_host': 'source', 'generation': 1}, history=None)
            native.assert_not_called()

    def test_remote_rpc_runs_canonical_package_in_isolated_python_without_checkout_writes(self):
        import os
        import sys
        env = dict(os.environ, HOME=str(self.root / 'remote-home'))
        env.pop('XDG_STATE_HOME', None)
        class Connection:
            def python(inner, code, payload=b'', timeout=60):
                response = subprocess.run([sys.executable, '-c', code], input=payload, capture_output=True, env=env, timeout=timeout)
                if response.returncode:
                    raise AssertionError(response.stderr.decode())
                return response.stdout
        endpoint = module.RemoteEndpoint(Connection(), self.project.root, self.project.identity)
        result = endpoint.call('describe')
        self.assertEqual(result['identity'], self.project.identity)
        self.assertEqual(result['root'], str(self.project.root))
        self.assertTrue((self.root / 'remote-home/.local/state/devcontainer-setup/host.json').exists())
        self.assertEqual(list(self.project.root.iterdir()), [self.project.root / '.git'])
        self.assertFalse((self.root / 'remote-home/.codex').exists())

    def test_remote_errors_are_controlled_and_payload_is_bounded(self):
        connection = mock.Mock()
        connection.python.return_value = json.dumps({'ok': False, 'error': 'Remote import refused; inspect private handoff receipts.'}).encode()
        endpoint = module.RemoteEndpoint(connection, self.project.root, self.project.identity)
        with self.assertRaisesRegex(ValueError, 'inspect private'):
            endpoint.call('import', agent='codex', session_id=SID, bundle={})
        connection.python.reset_mock()
        with mock.patch.object(module, 'MAX_RPC_BYTES', 32):
            with self.assertRaises(ValueError):
                endpoint.call('describe')
        connection.python.assert_not_called()

    def test_mutating_target_rpc_requires_reserved_matching_custody(self):
        endpoint = module.LocalEndpoint(self.project, self.state)
        with mock.patch.object(module.sessions, 'import_snapshot') as importer, mock.patch.object(module.agents, 'native_history') as native:
            with self.assertRaisesRegex(ValueError, 'custody|reserv'):
                endpoint.call('import', agent='codex', session_id=SID, bundle={'source_host': 'mac', 'generation': 1})
            with self.assertRaisesRegex(ValueError, 'custody|reserv'):
                endpoint.call('verify', agent='codex', session_id=SID, bundle={'source_host': 'mac', 'generation': 1}, history={})
            importer.assert_not_called()
            native.assert_not_called()

    def test_malformed_remote_response_has_controlled_error(self):
        connection = mock.Mock()
        endpoint = module.RemoteEndpoint(connection, self.project.root, self.project.identity)
        for data in (b'[]', b'{"ok":true}', b'not json'):
            connection.python.return_value = data
            with self.subTest(data=data):
                with self.assertRaisesRegex(ValueError, 'Remote'):
                    endpoint.call('describe')

    def test_real_claude_snapshot_roundtrip_and_ownership_with_unchanged_native_profiles(self):
        from devcontainer_setup.project_sync import ownership, state
        remote_root = self.root / 'remote-repo'
        subprocess.run(['git', 'init', '-q', str(remote_root)], check=True)
        subprocess.run(['git', '-C', str(remote_root), 'remote', 'add', 'origin', 'git@github.com:a/b'], check=True)
        remote_project = discover_project(remote_root)
        self.config['remote_root'] = str(remote_root)
        local_env = {'HOME': str(self.root / 'mac-home')}
        remote_env = {'HOME': str(self.root / 'remote-home')}
        local_state, remote_state = self.root / 'real-local', self.root / 'real-remote'
        local_home = module.sessions.native_home('claude', local_env)
        remote_home = module.sessions.native_home('claude', remote_env)
        for project in (self.project, remote_project):
            (project.root / '.scratch').mkdir()
        source_path = local_home / 'projects' / module.sessions.claude_namespace(self.project.root) / (SID + '.jsonl')
        source_path.parent.mkdir(parents=True)
        original = json.dumps(dict(type='user', sessionId=SID, cwd=str(self.project.root), message={'role': 'user', 'content': 'literal /old/path preserved'})).encode() + b'\n'
        source_path.write_bytes(original)
        source_host = state.host_id(local_env)
        ownership.adopt_local(local_state, 'claude', SID, source_host, native_paths=[source_path])
        local = module.LocalEndpoint(self.project, local_state, env=local_env)
        remote = module.LocalEndpoint(remote_project, remote_state, env=remote_env)
        with mock.patch.object(module, 'host_id', side_effect=state.host_id), mock.patch.object(module, '_agent_version', return_value='2.1.118'), mock.patch.object(module.agents, 'native_history') as model:
            outgoing = module.handoff(self.project, 'claude', SID, 'codespace', local_state, local=local, remote=remote)
            target_path = remote_home / 'projects' / module.sessions.claude_namespace(remote_root) / (SID + '.jsonl')
            self.assertEqual(target_path.read_bytes(), original)
            extra = json.dumps(dict(type='assistant', sessionId=SID, cwd=str(remote_root), message={'role': 'assistant', 'content': 'remote continuation'})).encode() + b'\n'
            target_path.write_bytes(original + extra)
            incoming = module.handoff(self.project, 'claude', SID, 'mac', local_state, local=local, remote=remote)
            self.assertEqual(source_path.read_bytes(), original + extra)
            self.assertEqual(incoming['generation'], outgoing['generation'] + 1)
            self.assertEqual(ownership.owner_status(local_state, 'claude', SID)['status'], 'idle')
            self.assertEqual(ownership.owner_status(remote_state, 'claude', SID)['status'], 'transferred')
            model.assert_not_called()
        self.assertEqual(len(list((local_state / 'backups').glob('*.tar.gz'))), 2)
        self.assertEqual(len(list((remote_state / 'backups').glob('*.tar.gz'))), 2)
        for home in (Path(local_env['HOME']), Path(remote_env['HOME'])):
            self.assertFalse((home / '.claude/settings.json').exists())
            self.assertFalse((home / '.codex').exists())

    def test_remote_validation_categories_are_actionable_without_echoing_sensitive_errors(self):
        cases = (
            (ValueError('Unsupported codex native version: SECRET_TOKEN'), 'unsupported-native-format'),
            (ValueError('Unsupported compressed native transcript; SECRET_TOKEN'), 'unsupported-native-format'),
            (ValueError('Native transcript belongs to another project checkout SECRET_TOKEN'), 'project-scope'),
            (ValueError('Native global memory context prevents strict project-only export SECRET_TOKEN'), 'project-scope'),
            (ValueError('Session lease is active; SECRET_TOKEN'), 'writer-custody'),
            (ValueError('Native session has an open file / possible active writer: SECRET_TOKEN'), 'writer-custody'),
            (ValueError('Matching reserved destination custody is required SECRET_TOKEN'), 'writer-custody'),
            (ValueError('Native histories differ; SECRET_TOKEN'), 'history-mismatch'),
            (FileNotFoundError('SECRET_TOKEN missing executable'), 'missing-prerequisite'),
            (ValueError('Cannot establish writer quiescence: lsof is required SECRET_TOKEN'), 'missing-prerequisite'),
        )
        request = dict(root=str(self.project.root), identity=self.project.identity, operation='source_prepare', values={})
        for error, code in cases:
            with self.subTest(code=code), mock.patch.object(module.LocalEndpoint, 'call', side_effect=error):
                response = module._rpc_dispatch(request)
                self.assertFalse(response['ok'])
                self.assertEqual(response['error']['code'], code)
                self.assertNotIn('SECRET_TOKEN', json.dumps(response))
                connection = mock.Mock()
                connection.python.return_value = json.dumps(response).encode()
                endpoint = module.RemoteEndpoint(connection, self.project.root, self.project.identity)
                with self.assertRaises(ValueError) as caught:
                    endpoint.call('source_prepare')
                self.assertIn(code, str(caught.exception))
                self.assertNotIn('SECRET_TOKEN', str(caught.exception))

    def test_remote_error_envelope_unknown_code_or_arbitrary_message_fails_closed(self):
        connection = mock.Mock()
        endpoint = module.RemoteEndpoint(connection, self.project.root, self.project.identity)
        for error in ({'code': 'unknown', 'message': 'SECRET_TOKEN'}, {'code': 'history-mismatch', 'message': 'SECRET_TOKEN'}):
            connection.python.return_value = json.dumps({'ok': False, 'error': error}).encode()
            with self.assertRaises(ValueError) as caught:
                endpoint.call('import')
            self.assertNotIn('SECRET_TOKEN', str(caught.exception))

    def test_history_save_and_begin_transfer_exclude_an_interleaving_managed_launch(self):
        from devcontainer_setup.project_sync import ownership
        import threading
        state = self.root / 'atomic-source'
        with ownership.managed_lease(state, 'codex', SID, 'mac', new=True):
            pass
        endpoint = module.LocalEndpoint(self.project, state)
        real_save = module._save
        competing = []
        def contender():
            try:
                with ownership.managed_lease(state, 'codex', SID, 'mac'):
                    competing.append('acquired')
            except ValueError:
                competing.append('refused')
        def save(path, value, **kwargs):
            if Path(path) == state / 'handoffs/source' / f'codex-{SID}.json' and 'history' in value and 'result' not in value:
                thread = threading.Thread(target=contender)
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), 'Concurrent lease attempt did not resolve')
            return real_save(path, value, **kwargs)
        def export(*args, **kwargs):
            return dict(source_host='mac', generation=kwargs['generation'], session_id=SID, files=[])
        with mock.patch.object(module.sessions, 'export_snapshot', side_effect=export), mock.patch.object(module.agents, 'native_history', return_value={'turns': [], 'items': []}), mock.patch.object(module, '_save', side_effect=save):
            result = endpoint.call('source_prepare', agent='codex', session_id=SID, request_id=str(uuid.uuid4()), destination_host='codespace')
        self.assertEqual(competing, ['refused'])
        self.assertEqual(result['bundle']['generation'], result['transfer']['generation'])
