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

    def test_recheckpointed_codex_child_carries_validated_ancestor_scope_union(self):
        parent = self.codex()
        raw = parent.read_bytes()
        destination = self.root / 'codex-next-checkout'
        subprocess.run(['git', 'init', '-q', str(destination)], check=True)
        subprocess.run(['git', '-C', str(destination), 'remote', 'add', 'origin', self.project.remote], check=True)
        dp = discover_project(destination)
        child_id, turn_id = str(uuid.uuid4()), str(uuid.uuid4())
        child = self.codex(child_id, parent=(parent, len(raw), len(raw.splitlines())), rows=[dict(type='event_msg', payload=dict(type='task_started', turn_id=turn_id)), dict(type='event_msg', payload=dict(type='task_complete', turn_id=turn_id))])
        child_rows = [json.loads(line) for line in child.read_bytes().splitlines()]
        child_rows[0]['payload']['cwd'] = str(destination)
        child_rows[0]['payload']['runtime_workspace_roots'] = [str(destination)]
        child.write_bytes(lines(child_rows))
        state = self.root / 'codex-next-state'
        receipt = state / 'conversation-imports/prior/receipt.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps(dict(schema=1, agent='codex', status='complete', identity=dp.identity, project_id=dp.project_id, destination_root=str(destination), files=[dict(destination=str(parent), size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), scope_roots=[str(self.project.root)])])))
        captured = self.api.capture_conversations(dp, self.store, state=state, env=self.env)
        entry = next(item for item in captured['entries'] if item['session_id'] == child_id)
        self.assertEqual(set(entry['scope_roots']), {str(destination), str(self.project.root)})
        self.api._preflight(dp, self.store, entry)

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

    def test_failed_put_is_an_issue_at_its_session_position(self):
        sid = str(uuid.uuid4())
        self.codex(sid)
        self.claude(rows=[dict(type='user', message=dict(role='user', content='unfinished'), uuid=str(uuid.uuid4()))])
        put = self.store.put
        def full(data):
            if sid.encode() in data:
                raise OSError('fixture store full')
            return put(data)
        self.store.put = full
        result = self.capture()
        self.assertEqual(result['entries'], [])
        self.assertEqual([(item['agent'], item['session_id'], item['reason']) for item in result['issues']],
                         [('codex', sid, 'fixture store full'), ('claude', self.sid, 'No validated completed Claude turn yet')])

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

    def test_claude_agent_tool_refuses_unproven_child_even_without_sidecars(self):
        self.claude(rows=[dict(type='assistant', message=dict(role='assistant', content=[dict(type='tool_use', name='Agent', id='tool-fixture', input={})], stop_reason='tool_use'), uuid=str(uuid.uuid4())), dict(type='assistant', message=dict(role='assistant', content=[dict(type='text', text='Finished parent')], stop_reason='end_turn'), uuid=str(uuid.uuid4()))])
        result = self.capture()
        self.assertEqual(result['entries'], [])
        self.assertIn('dependency', result['issues'][0]['reason'])

    def test_capture_never_calls_native_writer_or_quiescence(self):
        self.codex(); self.claude()
        with patch('devcontainer_setup.project_sync.agents.CodexClient', side_effect=AssertionError('writer')), patch('devcontainer_setup.project_sync.sessions.assert_quiescent', side_effect=AssertionError('custody')):
            self.assertEqual(len(self.capture()['entries']), 2)


if __name__ == '__main__':
    unittest.main()

class NativeRestoreTests(NativeFixture):
    def restore(self, entry, **kwargs):
        return self.api._publish_conversation(self.project, self.store, entry, state=self.root / 'state', env=self.env, **kwargs)

    def test_codex_new_envelope_retains_history_without_touching_original(self):
        path = self.codex(); original = path.read_bytes(); entry = self.capture()['entries'][0]
        calls = []
        class Client:
            def __init__(inner, *args, **kwargs): pass
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def call(inner, method, params):
                calls.append((method, params))
                if method == 'thread/resume': inner.publication_id = params['threadId']
                if method == 'thread/list': return dict(data=[dict(id=inner.publication_id)], nextCursor=None)
                return dict(thread=dict(id=params['threadId']))
        with patch.object(self.api.agents, 'CodexClient', Client), patch.object(self.api.agents, '_history', return_value=dict(turns=[dict(id=self.turn, status='completed')], items=[])):
            result = self.restore(entry)
        self.assertNotEqual(result['session_id'], self.sid)
        self.assertEqual([method for method, _ in calls], ['thread/resume', 'thread/list'])
        clone = next(p for p in self.home.rglob('*.jsonl') if p.name.endswith(result['session_id'] + '.jsonl'))
        self.assertEqual(clone.read_bytes().splitlines()[1:], original.splitlines()[1:])
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
            def call(inner, method, params): return dict(thread=dict(id=params['threadId']))
        with patch.object(self.api.agents, 'CodexClient', Client), patch.object(self.api.agents, '_history', return_value=dict(turns=[], items=[])), self.assertRaisesRegex(ValueError, 'history|turn'):
            self.restore(entry)



class ContinuedClaudeTests(NativeFixture):
    def test_native_string_blob_verifies_canonical_text_block_hash_and_request_digest(self):
        import copy
        def digest(value):return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        shape = dict(system=[dict(type='text', text='Project system prompt')], tools=[])
        message = dict(role='system', content='Inherited project environment', output_config=dict(effort='medium'))
        canonical = dict(message, content=[dict(type='text', text=message['content'])])
        request = dict(shape, messages=[copy.deepcopy(canonical)])
        request['messages'][0]['content'][0]['cache_control'] = dict(type='ephemeral')
        rows = [dict(type='user', uuid=str(uuid.uuid4()), message=dict(role='user', content='Project question')), dict(type='api-request-shape', shapeHash=digest(shape), shape=shape), dict(type='api-request-blob', hash=digest(canonical), message=message), dict(type='api-request', shapeHash=digest(shape), keep=0, tail=[digest(canonical)], params={}, cacheControl=[dict(path=['messages', 0, 'content', 0], value=dict(type='ephemeral'))], requestDigest=digest(request)), dict(type='assistant', uuid=str(uuid.uuid4()), message=dict(role='assistant', content=[dict(type='text', text='ORCHID')], stop_reason='end_turn'))]
        path = self.claude(rows=rows)
        original = path.read_bytes()
        captured = self.capture()
        self.assertEqual(captured['issues'], [])
        self.assertEqual(self.store.read(captured['entries'][0]['files'][0]['blob']), original)
        rows[2]['message']['output_config']['effort'] = 'high'
        self.claude(rows=rows)
        captured = self.capture()
        self.assertEqual(captured['entries'], [])
        self.assertIn('digest', captured['issues'][0]['reason'])

    def forked_fixture(self):
        import copy
        from devcontainer_setup.project_sync.sessions import claude_namespace
        source = self.project
        rows = [dict(type='user', uuid=str(uuid.uuid4()), promptId=str(uuid.uuid4()), message=dict(role='user', content='Say ORCHID')), dict(type='attachment', uuid=str(uuid.uuid4()), attachment=dict(type='environment', snapshot=dict(workingDirectory=str(source.root), additionalWorkingDirectories=[]))), dict(type='assistant', uuid=str(uuid.uuid4()), message=dict(role='assistant', content=[dict(type='text', text='ORCHID')], stop_reason='end_turn'))]
        parent = self.claude(rows=rows)
        entry = self.capture()['entries'][0]
        destination = self.root / 'fork-destination'
        subprocess.run(['git', 'init', '-q', str(destination)], check=True)
        subprocess.run(['git', '-C', str(destination), 'remote', 'add', 'origin', source.remote], check=True)
        dp = discover_project(destination)
        state = self.root / 'continued-state'
        child_id, alias_id = str(uuid.uuid4()), str(uuid.uuid4())
        result = dict(session_id=child_id, provenance=dict(agent='claude', source_session_id=self.sid, source_root=str(source.root), cutoff=entry['cutoff']))
        directory = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects' / claude_namespace(destination)
        directory.mkdir(parents=True)
        alias = directory / (alias_id + '.jsonl')
        alias.write_bytes(parent.read_bytes()); alias.chmod(0o400)
        info = alias.stat()
        receipt_path = state / 'conversation-imports/legacy/receipt.json'
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text(json.dumps(dict(schema=1, status='complete', agent='claude', project_id=dp.project_id, identity=dp.identity, source_session_id=self.sid, destination_root=str(destination), result=result, files=[dict(destination=str(alias), physical_alias=alias_id, source_session_id=self.sid, created=True, inode=[info.st_dev, info.st_ino], size=info.st_size, sha256=entry['files'][0]['sha256'], scope_roots=entry['scope_roots'])])))
        inherited = [json.loads(line) for line in alias.read_bytes().splitlines()]
        for row in inherited:
            row['sessionId'] = child_id
            row['cwd'] = str(destination)
            if row['type'] == 'user':row['promptId'] = str(uuid.uuid4())
        inherited.extend([dict(type='user', uuid=str(uuid.uuid4()), sessionId=child_id, cwd=str(destination), version='2.1.302', message=dict(role='user', content='Recall the word')), dict(type='assistant', uuid=str(uuid.uuid4()), sessionId=child_id, cwd=str(destination), version='2.1.302', message=dict(role='assistant', content=[dict(type='text', text='ORCHID')], stop_reason='end_turn'))])
        child = directory / (child_id + '.jsonl')
        child.write_bytes(lines(inherited))
        receipt = next((state / 'conversation-imports').glob('*/receipt.json'))
        return source, dp, state, result, alias, child, receipt

    def test_materialized_fork_keeps_historical_environment_and_can_export_again(self):
        source, dp, state, result, alias, child, receipt = self.forked_fixture()
        captured = self.api.capture_conversations(dp, self.store, state=state, env=self.env)
        self.assertEqual(captured['issues'], [])
        entry = next(item for item in captured['entries'] if item['session_id'] == result['session_id'])
        self.assertIn(str(source.root), entry['scope_roots'])
        self.assertEqual(self.store.read(entry['files'][0]['blob']), child.read_bytes())
        self.api._preflight(dp, self.store, entry)
        second = self.api._publish_conversation(dp, self.store, entry, state=self.root / 'second-recovery', env=self.env)
        self.assertNotEqual(second['session_id'], result['session_id'])

    def test_modified_alias_wrong_child_and_receipt_scope_mismatch_refuse(self):
        import copy
        source, dp, state, result, alias, child, receipt_path = self.forked_fixture()
        original_alias = alias.read_bytes()
        original_child = child.read_bytes()
        original_receipt = json.loads(receipt_path.read_bytes())
        for defect in ('alias', 'alias_digest', 'child_content', 'child_id', 'destination', 'source_id', 'unused_scope'):
            alias.chmod(0o600);alias.write_bytes(original_alias);alias.chmod(0o400)
            child.write_bytes(original_child)
            receipt = copy.deepcopy(original_receipt)
            if defect in ('alias', 'alias_digest'):
                alias.chmod(0o600);alias.write_bytes(original_alias.replace(b'ORCHID', b'VIOLET'));alias.chmod(0o400)
                if defect == 'alias_digest':receipt['files'][0]['sha256'] = hashlib.sha256(alias.read_bytes()).hexdigest()
            elif defect == 'child_content':
                child.write_bytes(original_child.replace(b'ORCHID', b'VIOLET', 1))
            elif defect == 'child_id':receipt['result']['session_id'] = str(uuid.uuid4())
            elif defect == 'destination':receipt['destination_root'] = str(self.root / 'foreign-checkout')
            elif defect == 'source_id':receipt['source_session_id'] = str(uuid.uuid4())
            else:
                receipt['files'][0]['scope_roots'].append('/foreign-unused-scope')
                child.write_bytes(original_child + lines([dict(type='assistant', uuid=str(uuid.uuid4()), sessionId=result['session_id'], cwd='/foreign-unused-scope', version='2.1.302', message=dict(role='assistant', content=[dict(type='text', text='foreign')], stop_reason='end_turn'))]))
            receipt_path.write_text(json.dumps(receipt))
            with self.subTest(defect=defect):
                captured = self.api.capture_conversations(dp, self.store, state=state, env=self.env)
                self.assertNotIn(result['session_id'], [item['session_id'] for item in captured['entries']])
                self.assertTrue(captured['issues'])


class NativePublicationTests(NativeFixture):
    def prepare(self):
        self.codex()
        entry = self.capture()['entries'][0]
        env = dict(self.env, CODEX_HOME=str(self.root / 'publication-native'))
        return entry, env, self.root / 'publication-state'

    def test_failed_publish_intent_writes_no_final_native_identity(self):
        entry, env, state = self.prepare()
        with patch.object(self.api, 'atomic_write', side_effect=OSError('fixture journal unavailable')):
            with self.assertRaisesRegex(OSError, 'journal unavailable'):
                self.api._publish_conversation(self.project, self.store, entry, state=state, env=env)
        self.assertEqual(list(Path(env['CODEX_HOME']).rglob('*.jsonl')), [])

    def test_interrupted_partial_stage_write_never_publishes_native_identity(self):
        entry, env, state = self.prepare()
        write = os.write
        count = [0]
        def partial(fd, data):
            count[0] += 1
            if count[0] == 1:
                return write(fd, data[:max(1, len(data) // 2)])
            raise OSError('fixture interrupted staged write')
        with patch.object(self.api.os, 'write', side_effect=partial), patch.object(self.api.agents, 'CodexClient', side_effect=AssertionError('native writer reached before staging fault')):
            with self.assertRaisesRegex(OSError, 'staged write'):
                self.api._publish_conversation(self.project, self.store, entry, state=state, env=env)
        self.assertEqual(list(Path(env['CODEX_HOME']).rglob('*.jsonl')), [])

    def test_foreign_final_created_during_publication_is_never_overwritten_or_resumed(self):
        entry, env, state = self.prepare()
        link = os.link
        foreign = b'foreign native history remains intact\n'
        def collision(source, target, **kwargs):
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=kwargs['dst_dir_fd'])
            try: os.write(fd, foreign)
            finally: os.close(fd)
            return link(source, target, **kwargs)
        with patch.object(self.api.os, 'link', side_effect=collision), self.assertRaises(FileExistsError):
            self.api._publish_conversation(self.project, self.store, entry, state=state, env=env)
        final = next(Path(env['CODEX_HOME']).rglob('*.jsonl'))
        with patch.object(self.api.agents, 'CodexClient', side_effect=AssertionError('foreign native identity resumed')), self.assertRaisesRegex(ValueError, 'ownership'):
            self.api._publish_conversation(self.project, self.store, entry, state=state, env=env)
        self.assertEqual(final.read_bytes(), foreign)

    def test_claude_retry_reuses_journaled_writable_import_after_complete_receipt_failure(self):
        path = self.claude()
        entry = self.capture()['entries'][0]
        state = self.root / 'claude-publish-state'
        write = self.api.atomic_write
        count = [0]
        def interrupt(path, data, *args, **kwargs):
            count[0] += 1
            if count[0] == 2:
                raise OSError('fixture complete receipt unavailable')
            return write(path, data, *args, **kwargs)
        with patch.object(self.api, 'atomic_write', side_effect=interrupt), self.assertRaisesRegex(OSError, 'complete receipt'):
            self.api._publish_conversation(self.project, self.store, entry, state=state, env=self.env)
        aliases = [candidate for candidate in path.parent.glob('*.jsonl') if candidate != path]
        self.assertEqual(len(aliases), 1)
        result = self.api._publish_conversation(self.project, self.store, entry, state=state, env=self.env)
        self.assertEqual(result['session_id'], aliases[0].stem)
        self.assertEqual(len(list(path.parent.glob('*.jsonl'))), 2)
        self.assertEqual([row['message'] for row in map(json.loads, aliases[0].read_bytes().splitlines())], [row['message'] for row in map(json.loads, path.read_bytes().splitlines())])
        self.assertEqual(aliases[0].stat().st_mode & 0o777, 0o600)

    def test_interrupted_after_publication_retries_only_owned_published_inode(self):
        entry, env, state = self.prepare()
        calls = []
        child = str(uuid.uuid4())
        class Client:
            def __init__(inner, *args, **kwargs): pass
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def call(inner, method, params):
                calls.append((method, params))
                if method == 'thread/resume': inner.publication_id = params['threadId']
                if method == 'thread/list': return dict(data=[dict(id=inner.publication_id)], nextCursor=None)
                if method == 'thread/resume':return dict(thread=dict(id=params['threadId']))
                if method == 'thread/fork':return dict(thread=dict(id=child, cwd=str(self.project.root)), runtimeWorkspaceRoots=[str(self.project.root)])
                return {}
        link = os.link
        def interrupted(*args, **kwargs):
            link(*args, **kwargs)
            raise OSError('fixture interruption after publication')
        with patch.object(self.api.os, 'link', side_effect=interrupted), patch.object(self.api.agents, 'CodexClient', Client), patch.object(self.api.agents, '_history', return_value=dict(turns=[dict(id=self.turn, status='completed')], items=[])):
            with self.assertRaisesRegex(OSError, 'after publication'):
                self.api._publish_conversation(self.project, self.store, entry, state=state, env=env)
        self.assertEqual(calls, [])
        with patch.object(self.api.agents, 'CodexClient', Client), patch.object(self.api.agents, '_history', return_value=dict(turns=[dict(id=self.turn, status='completed')], items=[])):
            result = self.api._publish_conversation(self.project, self.store, entry, state=state, env=env)
        self.assertNotEqual(result['session_id'], self.sid)
        self.assertEqual([params['threadId'] for method, params in calls if method == 'thread/resume'], [result['session_id']])
        finals = list(Path(env['CODEX_HOME']).rglob('*.jsonl'))
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].stat().st_nlink, 1)


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
                if method == 'thread/resume': inner.publication_id = params['threadId']
                if method == 'thread/list': return dict(data=[dict(id=inner.publication_id)], nextCursor=None)
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
                self.api._publish_conversation(self.project, self.store, entry, state=state, env=destination_env)
            result = self.api._publish_conversation(self.project, self.store, entry, state=state, env=destination_env)
        self.assertNotEqual(result['session_id'], self.sid)
        self.assertEqual(len([method for method, _ in calls if method == 'thread/resume']), 2)
