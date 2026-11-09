"""Offline forget commands, outbound suppression and retained native custody."""
import contextlib
from datetime import datetime
import hashlib
import importlib
import io
import json
from pathlib import Path
import stat
import sys
import threading
import unittest
import uuid
from unittest.mock import patch

from devcontainer_setup.project_sync import __main__ as cli, conversations, replication, rpc, state
from tests.test_native_checkpoints import NativeFixture
from tests.test_replication import IsolatedPythonConnection
from devcontainer_setup.project_sync.checkpoint_store import Store


class ForgetTests(NativeFixture):
    def setUp(self):
        # Import per test to retain an individual missing-feature receipt.
        self.forget = importlib.import_module('devcontainer_setup.project_sync.forget')
        super().setUp()
        self.env.update(HOME=str(self.root / 'desktop-home'), XDG_STATE_HOME=str(self.root / 'desktop-state'))
        self.store = Store(self.root / 'local-state')
        self.connection = IsolatedPythonConnection(self.root / 'remote-home')
        # Native publication needs metadata RPC, never an installed agent or its account.
        binary = self.root / 'bin' / 'codex'
        binary.parent.mkdir()
        binary.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
home = Path(os.environ['CODEX_HOME']) / 'sessions'
selected = None
def history(sid, length=None):
    path = next(home.rglob('*' + sid + '.jsonl'))
    data = path.read_bytes()
    rows = [json.loads(line) for line in (data if length is None else data[:length]).splitlines()]
    base = rows[0]['payload'].get('history_base')
    return (history(base['thread_id'], base['end_byte_offset']) if base else []) + rows
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method, params = request['method'], request.get('params', {})
    result = {}
    if method == 'thread/resume':
        selected = params['threadId']
        result = {'thread': {'id': selected}}
    elif method == 'thread/list':
        result = {'data': [{'id': selected}], 'nextCursor': None}
    elif method == 'thread/turns/list':
        result = {'data': [{'id': row['payload']['turn_id'], 'status': 'completed'}
                           for row in history(params['threadId'])
                           if row['type'] == 'event_msg' and row['payload'].get('type') == 'task_complete'],
                  'nextCursor': None}
    elif method == 'thread/items/list':
        result = {'data': [], 'nextCursor': None}
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
''')
        binary.chmod(0o700)
        for env in (self.env, self.connection.env):
            env['PATH'] = str(binary.parent) + ':' + env.get('PATH', '')
        self.remote_store = Store(state.state_dir(self.project, self.connection.env))
        self.binding = dict(schema=1, codespace=self.connection.codespace, identity=self.project.identity,
                            root=str(self.project.root), remote_root=str(self.project.root))
        self.write('scratch.json', self.binding)
        with state.locked(self.store.state / 'workspace-control.lock', blocking=True):
            pass
        offline = patch.object(replication, 'read_codespace_state', return_value='Available')
        offline.start()
        self.addCleanup(offline.stop)

    def write(self, name, value):
        state.atomic_write(self.store.state / name, json.dumps(value).encode())

    def cycle(self, refused=frozenset()):
        actual = rpc.RemoteEndpoint.call

        def call(endpoint, operation, **values):
            result = actual(endpoint, operation, **values)
            if operation == 'publish':
                manifest = self.remote_store.load(values['snapshot_id'])
                entries = manifest['conversations']['entries']
                issues = [dict(agent=e['agent'], session_id=e['session_id'], reason='Fixture native format refused')
                          for e in entries if (e['agent'], e['session_id']) in refused]
                result = dict(result, issues=[*result['issues'], *issues])
                for field in ('imported', 'unchanged'):
                    result[field] = [e for e in result[field] if (e['agent'], e['source_session_id']) not in refused]
            return result

        with patch.object(rpc.RemoteEndpoint, 'call', autospec=True, side_effect=call):
            cycle = replication.sync_project_data(self.connection, self.project, self.store.state,
                                                   self.binding['remote_root'], env=self.env)
        self.write('scratch-runtime.json', dict(binding=self.binding, conversations=cycle))
        return cycle

    def prepare_refusals(self):
        native_id = self.sid
        native = self.claude()
        self.sid = other_id = str(uuid.uuid4())
        self.claude()
        relayed_id = str(uuid.uuid4())
        local_env = self.env
        try:
            self.env = self.connection.env
            remote = self.codex(relayed_id)
        finally:
            self.env = local_env
        self.cycle()
        pairs = frozenset({('claude', native_id), ('codex', relayed_id)})
        self.cycle(pairs)
        return pairs, other_id, native, remote

    def forget_pairs(self, pairs):
        return [self.forget.forget(self.project, self.store.state, agent=agent, session_id=sid, now=lambda: 1000.)
                for agent, sid in sorted(pairs)]

    def test_forgotten_conversation_leaves_the_next_outbound_snapshot(self):
        pairs, other_id, _, _ = self.prepare_refusals()
        messages = self.forget_pairs(pairs)
        cycle = self.cycle()
        row = cycle['directions']['desktop_to_codespace']
        manifest = self.store.load(row['snapshot_id'])
        manifest_pairs = {(e['agent'], e['session_id']) for e in manifest['conversations']['entries']}
        self.assertTrue(pairs.isdisjoint(manifest_pairs))
        self.assertIn(('claude', other_id), manifest_pairs)
        self.assertEqual(row['publication_issues'], [])
        self.assertEqual(cycle['outcome'], 'complete')
        self.assertTrue(all('on its own, and nothing is deleted. The next sync clears the warning.' in m for m in messages))
        self.assertEqual(self.forget.forgotten(self.store.state), pairs)
        record = state.read_json(self.store.state / self.forget.FILE)
        self.assertEqual(record['schema'], 1)
        self.assertEqual({r['codespace'] for r in record['conversations']}, {self.binding['codespace']})
        self.assertEqual({r['reason'] for r in record['conversations']}, {'Fixture native format refused'})
        self.assertEqual(stat.S_IMODE((self.store.state / self.forget.FILE).stat().st_mode), 0o600)

    def test_copies_of_the_forgotten_conversation_are_kept(self):
        pairs, _, native, remote = self.prepare_refusals()
        relays = sorted((self.store.root / 'relay').glob('*.json'))
        retained = [p for p in relays if (lambda e: (e['agent'], e['session_id']) in pairs)(state.read_json(p)['entry'])]
        self.assertTrue(retained)
        blobs = [self.store.root / 'objects' / digest for p in retained
                 for digest in replication.references(state.read_json(p)['entry'])]
        # Include the existing remote native copy of the desktop conversation.
        remote_native = list(Path(self.connection.env['CLAUDE_CONFIG_DIR']).rglob('*.jsonl'))
        self.assertTrue(remote_native)
        paths = [native, remote, *remote_native, *retained, *blobs]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        self.forget_pairs(pairs)
        self.write('checkpoint-collection.json', dict(retry_at=0))
        self.cycle()  # Includes normal checkpoint collection and the independent inbound path.
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_a_dependent_conversation_still_carries_its_ancestor(self):
        parent = self.codex()
        parent_id, child_id = self.sid, str(uuid.uuid4())
        self.codex(child_id, parent=(parent, parent.stat().st_size, len(parent.read_bytes().splitlines())), rows=[])
        self.cycle({('codex', parent_id)})
        self.forget_pairs({('codex', parent_id)})
        row = self.cycle()['directions']['desktop_to_codespace']
        entries = self.store.load(row['snapshot_id'])['conversations']['entries']
        self.assertEqual([e['session_id'] for e in entries], [child_id])
        self.assertEqual({f['session_id'] for f in entries[0]['files']}, {parent_id, child_id})
        conversations._preflight(self.project, self.store, entries[0])
        self.assertEqual(row['outcome'], 'complete')

    def test_unknown_conversation_is_refused_and_writes_nothing(self):
        self.claude()
        self.cycle()
        before = {p: p.read_bytes() for p in self.store.state.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'refused no claude conversation unknown in the last sync; nothing was forgotten'):
            self.forget.forget(self.project, self.store.state, agent='claude', session_id='unknown')
        self.assertFalse((self.store.state / self.forget.FILE).exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.state.rglob('*') if p.is_file()})

    def test_repeat_after_the_cycle_says_already_forgotten(self):
        self.claude()
        pair = {('claude', self.sid)}
        self.cycle(pair)
        self.forget_pairs(pair)
        self.cycle()
        path = self.store.state / self.forget.FILE
        before = path.read_bytes(), path.stat().st_mtime_ns
        date = datetime.fromtimestamp(1000.).strftime('%Y-%m-%d %H:%M')
        expected = f'claude conversation {self.sid} was already forgotten on {date}.'
        for rebind in (False, True):
            if rebind:
                self.binding = dict(self.binding, codespace='replacement-fixture')
                self.write('scratch.json', self.binding)
            self.assertEqual(self.forget.forget(self.project, self.store.state, agent='claude', session_id=self.sid), expected)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_rebind_during_forget_is_rechecked_under_the_lock(self):
        self.claude()
        self.cycle({('claude', self.sid)})
        waiting, errors = threading.Event(), []
        actual = self.forget.locked

        @contextlib.contextmanager
        def observed_lock(path, blocking=False):
            self.assertEqual(path, self.store.state / 'workspace-control.lock')
            self.assertTrue(blocking)
            waiting.set()
            with actual(path, blocking=blocking):
                yield

        def attempt():
            try:
                self.forget.forget(self.project, self.store.state, agent='claude', session_id=self.sid)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=attempt)
        try:
            with patch.object(self.forget, 'locked', observed_lock):
                with state.locked(self.store.state / 'workspace-control.lock', blocking=True):
                    worker.start()
                    self.assertTrue(waiting.wait(5))
                    self.binding = dict(self.binding, codespace='replacement-fixture')
                    self.write('scratch.json', self.binding)
        finally:
            if worker.ident is not None:
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertIn('replacement-fixture refused no', str(errors[0]))
        self.assertFalse((self.store.state / self.forget.FILE).exists())

    def test_repeat_uses_local_time_for_a_record_from_today(self):
        today = datetime.now().replace(hour=12, minute=34, second=0, microsecond=0)
        self.write(self.forget.FILE, dict(schema=1, conversations=[dict(
            agent='claude', session_id='x', codespace='fixture-cs', reason='refused', forgotten_at=today.timestamp())]))
        self.assertEqual(self.forget.forget(self.project, self.store.state, agent='claude', session_id='x'),
                         'claude conversation x was already forgotten on 12:34.')

    def test_unreadable_forget_file_fails_the_outbound_capture_with_its_message(self):
        state.atomic_write(self.store.state / 'conversation-forget.json', b'{broken')
        cycle = self.cycle()
        row = cycle['directions']['desktop_to_codespace']
        self.assertEqual(row['outcome'], 'failed')
        self.assertEqual(row['stage'], 'capture')
        self.assertIsNone(row['snapshot_id'])
        self.assertEqual(row['error'], 'conversation-forget.json cannot be read; move it aside to send every conversation again')
        self.assertEqual(cycle['directions']['codespace_to_desktop']['outcome'], 'complete')

    def test_cli_forget_runs_without_a_terminal(self):
        self.claude()
        self.cycle({('claude', self.sid)})
        with patch.object(cli, 'discover_project', return_value=self.project), \
                patch.object(cli, 'state_dir', return_value=self.store.state), \
                patch('sys.stdin.isatty', return_value=False), patch('sys.stdout.isatty', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = cli.main(['work', '--forget', '--agent', 'claude', '--session', self.sid])
        self.assertEqual(result, 0)
        self.assertIn(f'Forgot claude conversation {self.sid}', output.getvalue())
        self.assertIn(('claude', self.sid), self.forget.forgotten(self.store.state))

    def test_cli_agent_and_session_belong_only_to_forget(self):
        for args in (['work', '--status', '--agent', 'claude'], ['work', '--status', '--session', 'x'],
                     ['work', '--forget'], ['work', '--forget', '--agent', 'claude'],
                     ['work', '--forget', '--session', 'x']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()) as error, \
                    self.assertRaises(SystemExit) as exit_code:
                cli.main(args)
            self.assertEqual(exit_code.exception.code, 2)
            self.assertIn('--agent and --session go together with task work:forget', error.getvalue())

    def test_exclusion_drops_native_and_relay_issues_by_top_level_pair_only(self):
        pair = ('claude', 'refused')
        keep = dict(agent='claude', session_id='keep', files=[dict(session_id='refused')])
        drop = dict(agent=pair[0], session_id=pair[1])
        issues = [dict(drop, reason='refused'), dict(agent='claude', reason='global issue')]
        captured = dict(schema=1, entries=[drop, keep], issues=issues)
        with patch.object(replication, 'capture_conversations', return_value=captured), \
                patch.object(Store, 'relay_entries', return_value=([dict(drop, relayed=True)], issues)):
            manifest = self.store.load(replication.capture(self.project, self.store, relay=True, exclude={pair}))
        self.assertEqual(manifest['conversations']['entries'], [keep])
        self.assertEqual(manifest['conversations']['issues'], [issues[1], issues[1]])

    def test_malformed_forget_records_are_unreadable(self):
        valid = dict(agent='claude', session_id='x', codespace='fixture-cs', reason='refused', forgotten_at=1000.)
        for value in ({}, {'schema': 2, 'conversations': []}, {'schema': 1, 'conversations': {}},
                      {'schema': 1, 'conversations': [None]},
                      *({'schema': 1, 'conversations': [dict(valid, **change)]} for change in (
                          {'agent': '../claude'}, {'session_id': 'x\n'}, {'reason': None}, {'codespace': None},
                          {'forgotten_at': True}, {'forgotten_at': float('nan')}))):
            self.write('conversation-forget.json', value)
            with self.subTest(value=value), self.assertRaisesRegex(self.forget.Unreadable, 'cannot be read; move it aside'):
                self.forget.forgotten(self.store.state)

    def test_missing_pairing_and_invalid_ids_write_no_forget_record(self):
        (self.store.state / 'scratch.json').unlink()
        with self.assertRaisesRegex(ValueError, 'Pair a Codespace first: run task work:cs'):
            self.forget.forget(self.project, self.store.state, agent='claude', session_id='x')
        self.assertEqual(self.forget.forgotten(self.store.state), frozenset())
        for agent, sid in (('../claude', 'x'), ('claude', 'x\n')):
            with self.subTest(agent=agent, sid=sid), self.assertRaises(ValueError):
                self.forget.forget(self.project, self.store.state, agent=agent, session_id=sid)
        self.assertFalse((self.store.state / self.forget.FILE).exists())
