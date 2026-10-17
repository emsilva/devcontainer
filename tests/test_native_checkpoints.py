"""Active native checkpoints preserve completed context without taking custody."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from devcontainer_setup.project_sync.state import discover_project


def lines(rows):
    return b''.join(json.dumps(row).encode() + b'\n' for row in rows)


class MemoryStore:
    def __init__(self):
        self.objects = {}
    def put(self, data):
        key = hashlib.sha256(data).hexdigest()
        self.objects[key] = data
        return key
    def read(self, key):
        return self.objects[key]


class NativeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        repo = self.root / 'project'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/example/native-checkpoint'], check=True)
        self.project = discover_project(repo)
        self.home = self.root / 'native'
        self.env = dict(os.environ, CODEX_HOME=str(self.home / 'codex'), CLAUDE_CONFIG_DIR=str(self.home / 'claude'))
        self.store = MemoryStore()
        self.api = importlib.import_module('devcontainer_setup.project_sync.conversations')
        self.sid = str(uuid.uuid4())
        self.turn = str(uuid.uuid4())

    def codex(self, sid=None, parent=None, rows=None):
        sid = sid or self.sid
        meta = dict(id=sid, cwd=str(self.project.root), runtime_workspace_roots=[str(self.project.root)], cli_version='0.163.0', source='cli')
        if parent:
            parentpath, offset, ordinal = parent
            meta['history_base'] = dict(thread_id=parentpath.name[-42:-6], end_byte_offset=offset, end_ordinal_exclusive=ordinal)
        base = [dict(type='session_meta', payload=meta)]
        if rows is None:
            rows = [dict(type='event_msg', payload=dict(type='task_started', turn_id=self.turn)), dict(type='response_item', payload=dict(type='message', role='assistant', content=[dict(type='output_text', text='ORCHID')])), dict(type='event_msg', payload=dict(type='task_complete', turn_id=self.turn))]
        start = parent[2] if parent else 0
        data = lines([dict(row, ordinal=start + i) for i, row in enumerate(base + rows)])
        path = Path(self.env['CODEX_HOME']) / 'sessions/2026/10/17' / f'rollout-2026-10-17T12-00-00-{sid}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def claude(self, rows=None):
        from devcontainer_setup.project_sync.sessions import claude_namespace
        path = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects' / claude_namespace(self.project.root) / (self.sid + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        if rows is None:
            rows = [dict(type='user', message=dict(role='user', content='What is the project word?'), uuid=str(uuid.uuid4())), dict(type='assistant', message=dict(role='assistant', content=[dict(type='text', text='ORCHID')], stop_reason='end_turn'), uuid=str(uuid.uuid4()))]
        path.write_bytes(lines([dict(row, sessionId=self.sid, cwd=str(self.project.root), version='2.1.302') for row in rows]))
        return path

    def capture(self):
        return self.api.capture_conversations(self.project, self.store, env=self.env)


class NativeCheckpointTests(NativeFixture):
    def test_completed_prefix_while_writer_appends_partial_next_turn(self):
        path = self.codex()
        completed = path.read_bytes()
        with path.open('ab') as writer:
            writer.write(lines([dict(type='event_msg', payload=dict(type='task_started', turn_id=str(uuid.uuid4())))]))
            writer.write(b'{"type":"response_item","pay')
            writer.flush()
            result = self.capture()
        self.assertEqual(result['issues'], [])
        self.assertEqual(len(result['entries']), 1)
        entry = result['entries'][0]
        self.assertEqual(self.store.read(entry['files'][0]['blob']), completed)
        self.assertEqual(entry['cutoff']['turn_id'], self.turn)
        self.assertEqual(entry['cutoff']['bytes'], len(completed))
        self.assertIn(b'"pay', path.read_bytes())

    def test_growth_during_stable_read_allowed_but_changed_prefix_refused(self):
        path = self.codex()
        original = path.read_bytes()
        pread = os.pread
        count = [0]
        def grow(fd, length, offset):
            value = pread(fd, length, offset)
            count[0] += 1
            if count[0] == 1:
                with path.open('ab') as output:
                    output.write(b'{"type":')
            return value
        with patch.object(self.api.os, 'pread', side_effect=grow):
            self.assertEqual(self.api._stable_prefix(path), original)
        count[0] = 0
        def change(fd, length, offset):
            value = pread(fd, length, offset)
            count[0] += 1
            if count[0] == 1:
                path.write_bytes(original.replace(b'ORCHID', b'VIOLET'))
            return value
        with patch.object(self.api.os, 'pread', side_effect=change), self.assertRaisesRegex(ValueError, 'changed|truncat'):
            self.api._stable_prefix(path)

    def test_fork_of_fork_closure_inherits_complete_turn_and_checks_ordinal(self):
        parent = self.codex()
        raw = parent.read_bytes()
        child_id = str(uuid.uuid4())
        child = self.codex(child_id, parent=(parent, len(raw), len(raw.splitlines())), rows=[])
        leaf_id = str(uuid.uuid4())
        leaf = self.codex(leaf_id, parent=(child, child.stat().st_size, len(raw.splitlines()) + len(child.read_bytes().splitlines())), rows=[])
        result = self.capture()
        entry = next(x for x in result['entries'] if x['session_id'] == leaf_id)
        self.assertEqual(len(entry['files']), 3)
        self.assertEqual(entry['cutoff']['turn_id'], self.turn)
        metadata = json.loads(leaf.read_bytes().splitlines()[0])
        metadata['payload']['history_base']['end_ordinal_exclusive'] = 999
        data = lines([metadata])
        leaf.write_bytes(data)
        result = self.capture()
        self.assertNotIn(leaf_id, [x['session_id'] for x in result['entries']])
        self.assertTrue(any('ordinal' in x['reason'] for x in result['issues']))

    def test_malformed_leading_metadata_is_an_issue_instead_of_stopping_backups(self):
        path = self.codex()
        path.write_bytes(b'[]\n' + b''.join(path.read_bytes().splitlines(keepends=True)[1:]))
        result = self.capture()
        self.assertEqual(result['entries'], [])
        self.assertEqual(len(result['issues']), 1)
        self.assertIn('metadata', result['issues'][0]['reason'])

    def test_global_memory_and_other_project_are_not_exported(self):
        self.codex(rows=[dict(type='event_msg', payload=dict(type='task_started', turn_id=self.turn)), dict(type='response_item', payload=dict(type='message', role='developer', content=[dict(type='input_text', text='<memory_summary>personal</memory_summary>')])), dict(type='event_msg', payload=dict(type='task_complete', turn_id=self.turn))])
        result = self.capture()
        self.assertEqual(result['entries'], [])
        self.assertTrue(any('memory' in item['reason'] for item in result['issues']))
        self.assertFalse(any(b'personal' in data for data in self.store.objects.values()))
        path = self.codex()
        path.write_bytes(path.read_bytes().replace(str(self.project.root).encode(), b'/another/project'))
        result = self.capture()
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['issues'], [])

    def test_incomplete_turn_and_unknown_record_are_explicit_issues(self):
        self.codex(rows=[dict(type='event_msg', payload=dict(type='task_started', turn_id=self.turn))])
        result = self.capture()
        self.assertEqual(result['entries'], [])
        self.assertIn('completed', result['issues'][0]['reason'])
        self.codex(rows=[dict(type='not-yet-supported', payload={}), dict(type='event_msg', payload=dict(type='task_started', turn_id=self.turn)), dict(type='event_msg', payload=dict(type='task_complete', turn_id=self.turn))])
        result = self.capture()
        self.assertIn('Unsupported', result['issues'][0]['reason'])

    def test_claude_complete_prefix_keeps_request_dependencies(self):
        path = self.claude()
        original = path.read_bytes()
        with path.open('ab') as output:
            output.write(lines([dict(type='user', sessionId=self.sid, cwd=str(self.project.root), message=dict(role='user', content='unfinished'))]))
        result = self.capture()
        self.assertEqual(result['issues'], [])
        self.assertEqual(self.store.read(result['entries'][0]['files'][0]['blob']), original)
        self.assertEqual(result['entries'][0]['agent'], 'claude')

    def test_claude_sidecars_refuse_until_atomic_dependency_closure_proven(self):
        path = self.claude()
        side = path.with_suffix('') / 'subagents/agent-A.jsonl'
        side.parent.mkdir(parents=True)
        side.write_bytes(path.read_bytes())
        result = self.capture()
        self.assertEqual(result['entries'], [])
        self.assertIn('subagent', result['issues'][0]['reason'])

    def test_capture_never_calls_native_writer_or_quiescence(self):
        self.codex(); self.claude()
        with patch('devcontainer_setup.project_sync.agents.CodexClient', side_effect=AssertionError('writer')), patch('devcontainer_setup.project_sync.sessions.assert_quiescent', side_effect=AssertionError('custody')):
            self.assertEqual(len(self.capture()['entries']), 2)


if __name__ == '__main__':
    unittest.main()

class NativeRestoreTests(NativeFixture):
    def restore(self, entry, **kwargs):
        return self.api.restore_conversation(self.project, self.store, entry, state=self.root / 'state', env=self.env, **kwargs)

    def test_codex_fork_indexes_only_new_imports_and_verifies_completed_history(self):
        path = self.codex()
        original = path.read_bytes()
        entry = self.capture()['entries'][0]
        calls = []
        child = str(uuid.uuid4())
        class Client:
            def __init__(inner, *args, **kwargs): pass
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def call(inner, method, params):
                calls.append((method, params))
                if method == 'thread/fork':
                    return dict(thread=dict(id=child, cwd=str(self.project.root), path=str(self.home / 'fork.jsonl')), runtimeWorkspaceRoots=[str(self.project.root)])
                return {}
        with patch.object(self.api.agents, 'CodexClient', Client), patch.object(self.api.agents, '_history', return_value=dict(turns=[dict(id=self.turn, status='completed')], items=[])):
            result = self.restore(entry)
        self.assertEqual(result['session_id'], child)
        self.assertFalse(any(method == 'thread/resume' for method, _ in calls))
        fork = next(params for method, params in calls if method == 'thread/fork')
        self.assertTrue(fork['deferGoalContinuation'])
        self.assertEqual(fork['lastTurnId'], self.turn)
        self.assertEqual(fork['path'], str(path))
        self.assertEqual(path.read_bytes(), original)

    def test_wrong_identity_digest_and_ancestor_offset_fail_before_native_writes(self):
        self.codex()
        entry = self.capture()['entries'][0]
        import copy
        for kind in ('identity', 'blob', 'scope'):
            candidate = copy.deepcopy(entry)
            if kind == 'identity': candidate['identity'] = 'github.com/wrong/project'
            elif kind == 'blob': candidate['files'][0]['blob'] = 'f' * 64
            else: candidate['source_root'] = '/unrelated'
            with self.subTest(kind=kind), patch.object(self.api.agents, 'CodexClient', side_effect=AssertionError('native write')), self.assertRaises(ValueError):
                self.restore(candidate)

    def test_codex_empty_inherited_history_refuses_fork(self):
        self.codex()
        entry = self.capture()['entries'][0]
        class Client:
            def __init__(inner, *args, **kwargs): pass
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
        with patch.object(self.api.agents, 'CodexClient', Client), patch.object(self.api.agents, '_history', return_value=dict(turns=[], items=[])), self.assertRaisesRegex(ValueError, 'history|turn'):
            self.restore(entry)

    def test_claude_restoration_uses_unique_parent_alias_preserving_existing_original(self):
        path = self.claude()
        original = path.read_bytes()
        entry = self.capture()['entries'][0]
        result = self.restore(entry)
        argv = result['argv']
        self.assertIn('--fork-session', argv)
        self.assertEqual(argv[argv.index('--session-id') + 1], result['session_id'])
        parent = argv[argv.index('--resume') + 1]
        self.assertNotEqual(parent, self.sid)
        alias = path.with_name(parent + '.jsonl')
        self.assertEqual(alias.read_bytes(), original)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(result['env']['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
        again = self.api.capture_conversations(self.project, self.store, state=self.root / 'state', env=self.env)
        self.assertEqual(again['issues'], [])
        self.assertEqual([x['session_id'] for x in again['entries']], [self.sid])


class NativeRetryTests(NativeFixture):
    def test_interrupted_owned_import_can_retry_without_resuming_foreign_original(self):
        self.codex()
        entry = self.capture()['entries'][0]
        destination_env = dict(self.env, CODEX_HOME=str(self.root / 'destination-native'))
        state = self.root / 'retry-state'
        calls = []
        failures = [True]
        child = str(uuid.uuid4())
        class Client:
            def __init__(inner, *args, **kwargs): pass
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def call(inner, method, params):
                calls.append((method, params))
                if method == 'thread/resume':
                    if failures[0]:
                        failures[0] = False
                        raise ValueError('fixture interruption after physical import')
                    return dict(thread=dict(id=params['threadId']))
                if method == 'thread/fork':
                    return dict(thread=dict(id=child, cwd=str(self.project.root)), runtimeWorkspaceRoots=[str(self.project.root)])
                return {}
        with patch.object(self.api.agents, 'CodexClient', Client), patch.object(self.api.agents, '_history', return_value=dict(turns=[dict(id=self.turn, status='completed')], items=[])):
            with self.assertRaisesRegex(ValueError, 'interruption'):
                self.api.restore_conversation(self.project, self.store, entry, state=state, env=destination_env)
            result = self.api.restore_conversation(self.project, self.store, entry, state=state, env=destination_env)
        self.assertEqual(result['session_id'], child)
        self.assertEqual(len([method for method, _ in calls if method == 'thread/resume']), 2)

class NativeLaunchTests(NativeFixture):
    def test_default_launch_starts_new_and_resume_can_select_acknowledged_checkpoint(self):
        from types import SimpleNamespace
        entry = dict(agent='codex', session_id=self.sid, title='Completed project turn')
        manifest = dict(project=dict(project_id=self.project.project_id, identity=self.project.identity), conversations=dict(entries=[entry]))
        class Store:
            def __init__(inner, state): pass
            def latest(inner, source): return dict(snapshot_id='fixture') if source == 'codespace' else None
            def load(inner, key): return manifest
        class Gum:
            def choose(inner, title, options): return options[0]
        state = self.root / 'launch-state'
        restored = dict(session_id=str(uuid.uuid4()), argv=['codex', 'resume', 'recovered-id'], env={})
        with patch('devcontainer_setup.project_sync.checkpoint_store.Store', Store), patch('devcontainer_setup.project_sync.terminal.Gum', Gum), patch.object(self.api, '_new_codex', return_value=self.sid) as new, patch.object(self.api, 'capture_conversations', return_value=dict(entries=[], issues=[])), patch.object(self.api, 'restore_conversation', return_value=restored) as restore, patch.object(self.api.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.api.launch(self.project, state, 'codex', env=self.env), 0)
            new.assert_called_once()
            restore.assert_not_called()
            self.assertIn(self.sid, run.call_args.args[0])
            self.assertEqual(self.api.launch(self.project, state, 'codex', resume=True, env=self.env), 0)
            restore.assert_called_once()
            self.assertEqual(run.call_args.args[0], restored['argv'])

    def test_fresh_claude_uses_project_notes_without_global_memory_or_profile_changes(self):
        from types import SimpleNamespace
        memory = self.project.root / '.scratch/memory'
        memory.mkdir(parents=True)
        (memory / 'project.md').write_text('The project marker is TULIP.')
        with patch.object(self.api.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.api.launch(self.project, self.root / 'launch-state', 'claude', env=self.env), 0)
        call = run.call_args
        self.assertIn('--session-id', call.args[0])
        self.assertNotIn('--resume', call.args[0])
        self.assertIn('TULIP', call.args[0][call.args[0].index('--append-system-prompt') + 1])
        self.assertEqual(call.kwargs['env']['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
        self.assertEqual(call.kwargs['cwd'], self.project.root)
