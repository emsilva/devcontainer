import base64
import copy
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from devcontainer_setup.project_sync.state import discover_project


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.repo('source')
        self.dest = self.repo('destination')
        self.home = self.root / 'codex'
        self.target = self.root / 'target'
        self.state = self.root / 'state'
        self.sid = str(uuid.uuid4())
        self.api = importlib.import_module('devcontainer_setup.project_sync.sessions')

    def repo(self, name, origin='https://github.com/example/project'):
        path = self.root / name
        subprocess.run(['git', 'init', '-q', str(path)], check=True)
        subprocess.run(['git', '-C', str(path), 'remote', 'add', 'origin', origin], check=True)
        return discover_project(path)

    def rollout(self, sid=None, parent=None, home=None, cwd=None, extra=()):
        sid = sid or self.sid
        meta = dict(id=sid, cwd=str(cwd or self.project.root), cli_version='0.164.0', source='cli')
        if parent:
            meta['history_base'] = dict(thread_id=parent, end_byte_offset=0, end_ordinal_exclusive=0)
        records = [dict(type='session_meta', payload=meta), dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='Keep /old/project literal')]))]
        records.extend(extra)
        data = b''.join(json.dumps(row).encode() + b'\n' for row in records)
        path = (home or self.home) / 'sessions/2026/10/17' / f'rollout-2026-10-17T12-00-00-{sid}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def bundle(self, **kw):
        return self.api.export_snapshot(self.project, 'codex', self.sid, self.home, source_host='source-host', generation=1, **kw)

    def apply(self, bundle):
        return self.api.import_snapshot(self.dest, 'codex', self.sid, self.target, bundle, state=self.state)

    def test_exact_bytes_absent_identical_growth_local_ahead_and_divergence(self):
        path = self.rollout()
        original = path.read_bytes()
        bundle = self.bundle()
        self.assertEqual(self.apply(bundle)['status'], 'complete')
        target = self.target / path.relative_to(self.home)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.apply(bundle)['files'][0]['action'], 'identical')
        suffix = b'{"type":"event_msg","payload":{"type":"task_complete"}}\n'
        path.write_bytes(original + suffix)
        grown = self.bundle()
        self.assertEqual(self.apply(grown)['files'][0]['action'], 'append')
        self.assertEqual(target.read_bytes(), original + suffix)
        self.assertEqual(self.apply(bundle)['files'][0]['action'], 'local-ahead')
        path.write_bytes(original + suffix.replace(b'task_complete', b'other'))
        with self.assertRaisesRegex(ValueError, 'diverg'):
            self.apply(self.bundle())
        self.assertEqual(target.read_bytes(), original + suffix)

    def test_fork_closure_and_incremental_return_preserve_parent_offsets(self):
        parent = str(uuid.uuid4())
        self.rollout(parent)
        fork = self.rollout(parent=parent)
        bundle = self.bundle()
        self.assertEqual(len(bundle['files']), 2)
        self.apply(bundle)
        fork.write_bytes(fork.read_bytes() + b'{"type":"event_msg","payload":{"type":"task_complete"}}\n')
        self.apply(self.bundle())
        self.assertEqual((self.target / fork.relative_to(self.home)).read_bytes(), fork.read_bytes())

    def test_missing_ancestor_duplicate_id_and_compression_refused(self):
        path = self.rollout(parent=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, 'ancestor'):
            self.bundle()
        path.unlink()
        path = self.rollout()
        other = self.home / 'archived_sessions' / path.name
        other.parent.mkdir()
        other.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.bundle()
        other.unlink()
        path.rename(path.with_suffix('.jsonl.zst'))
        with self.assertRaisesRegex(ValueError, 'compress'):
            self.bundle()

    def test_truncation_unknown_record_scope_and_global_memory_refused(self):
        path = self.rollout()
        original = path.read_bytes()
        cases = [(original[:-1], 'complete'), (original + b'{"type":"new_native_record"}\n', 'Unsupported'), (original.replace(str(self.project.root).encode(), b'/elsewhere'), 'project'), (original + b'{"type":"response_item","payload":{"text":"<memory_summary>private global context</memory_summary>"}}\n', 'memory')]
        for data, message in cases:
            with self.subTest(message=message):
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, message):
                    self.bundle()

    def test_wrong_project_digest_path_and_full_preflight_no_writes(self):
        self.rollout()
        bundle = self.bundle()
        for mutate in ('project', 'digest', 'path', 'duplicate'):
            value = copy.deepcopy(bundle)
            if mutate == 'project':
                value['project']['identity'] = 'github.com/wrong/project'
            elif mutate == 'digest':
                value['files'][0]['sha256'] = '0' * 64
            elif mutate == 'path':
                value['files'][0]['path'] = '../auth.json'
            else:
                value['files'].append(value['files'][0])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.apply(value)
            self.assertFalse(self.target.exists())

    def test_interrupted_import_keeps_recovery_receipt_and_can_retry(self):
        self.rollout()
        bundle = self.bundle()
        actual = self.api.atomic_write
        def interrupt(path, data, *args, **kwargs):
            if Path(path).is_relative_to(self.target):
                raise OSError('synthetic interruption')
            return actual(path, data, *args, **kwargs)
        with patch.object(self.api, 'atomic_write', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'interruption'):
                self.apply(bundle)
        receipts = list(self.state.glob('imports/*/receipt.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['status'], 'prepared')
        self.assertEqual(self.apply(bundle)['status'], 'complete')

    def test_symlink_destination_and_open_foreign_writer_refused(self):
        path = self.rollout()
        bundle = self.bundle()
        self.target.mkdir()
        (self.target / 'sessions').symlink_to(self.home / 'sessions', target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.apply(bundle)
        (self.target / 'sessions').unlink()
        with path.open('ab'):
            with self.assertRaisesRegex(ValueError, 'writer|open'):
                self.bundle()

    def test_claude_parent_subagent_tasks_and_destination_namespace(self):
        encoded = self.api.claude_namespace(self.project.root)
        parent = self.home / 'projects' / encoded / f'{self.sid}.jsonl'
        parent.parent.mkdir(parents=True)
        record = dict(type='user', sessionId=self.sid, cwd=str(self.project.root), message=dict(role='user', content='fixture'))
        parent.write_text(json.dumps(record) + '\n')
        child = parent.with_suffix('') / 'subagents/agent-fixture.jsonl'
        child.parent.mkdir(parents=True)
        child.write_bytes(parent.read_bytes())
        task = self.home / 'tasks' / self.sid / '1.json'
        task.parent.mkdir(parents=True)
        task.write_text('{"id":"1","subject":"fixture","status":"pending"}')
        bundle = self.api.export_snapshot(self.project, 'claude', self.sid, self.home, source_host='source-host', generation=1, agent_version='2.1.313')
        self.assertEqual(len(bundle['files']), 3)
        self.api.import_snapshot(self.dest, 'claude', self.sid, self.target, bundle, state=self.state)
        mapped = self.target / 'projects' / self.api.claude_namespace(self.dest.root) / parent.name
        self.assertEqual(mapped.read_bytes(), parent.read_bytes())
        self.assertTrue((mapped.with_suffix('') / 'subagents/agent-fixture.jsonl').exists())
        task.write_text('{"id":"1","status":"completed"}')
        conflict = self.api.export_snapshot(self.project, 'claude', self.sid, self.home, source_host='source-host', generation=1, agent_version='2.1.313')
        with self.assertRaisesRegex(ValueError, 'diverg|task'):
            self.api.import_snapshot(self.dest, 'claude', self.sid, self.target, conflict, state=self.state)

    def test_imported_history_can_return_without_rewriting_original_cwd(self):
        path = self.rollout()
        self.apply(self.bundle())
        back = self.api.export_snapshot(self.dest, 'codex', self.sid, self.target, source_host='destination-host', generation=2, state=self.state)
        self.assertEqual(base64.b64decode(back['files'][0]['data']), path.read_bytes())
        self.api.import_snapshot(self.project, 'codex', self.sid, self.home, back, state=self.root / 'return-state')
        self.assertEqual(base64.b64decode(back['files'][0]['data']), path.read_bytes())

    def test_imported_sessions_list_in_destination_project(self):
        self.rollout()
        self.apply(self.bundle())
        listed = self.api.list_sessions(self.dest, 'codex', self.target, state=self.state)
        self.assertEqual([row['session_id'] for row in listed], [self.sid])

    def test_forged_snapshot_cannot_omit_dependency_or_add_unrelated_session(self):
        parent = str(uuid.uuid4())
        self.rollout(parent)
        self.rollout(parent=parent)
        bundle = self.bundle()
        missing = copy.deepcopy(bundle)
        missing['files'] = missing['files'][1:]
        with self.assertRaises(ValueError):
            self.apply(missing)
        unrelated = str(uuid.uuid4())
        self.rollout(unrelated)
        extra = self.api.export_snapshot(self.project, 'codex', unrelated, self.home, source_host='source-host', generation=1)
        bundle['files'].extend(extra['files'])
        with self.assertRaisesRegex(ValueError, 'closure|unrelated'):
            self.apply(bundle)
        self.assertFalse(self.target.exists())

    def test_modern_world_state_and_token_usage_records_preserved_exactly(self):
        path = self.rollout(extra=[dict(type='world_state', ordinal=4, payload=dict(full=True, state=dict(model='fixture'))), dict(type='token_usage_record', ordinal=11, payload=dict(thread_id=self.sid, usage=dict(input_tokens=1, output_tokens=1)))])
        self.apply(self.bundle())
        self.assertEqual((self.target / path.relative_to(self.home)).read_bytes(), path.read_bytes())

    def test_native_subagent_dependency_is_an_explicit_refusal(self):
        self.rollout(extra=[dict(type='response_item', payload=dict(type='function_call', name='spawn_agent', arguments='{}'))])
        with self.assertRaisesRegex(ValueError, 'subagent'):
            self.bundle()

    def test_malformed_native_payload_is_a_validation_error(self):
        self.rollout(extra=[dict(type='response_item', payload=['unsupported'])])
        with self.assertRaisesRegex(ValueError, 'payload'):
            self.bundle()

    def test_wrong_native_root_id_and_forged_subagent_manifest_refused(self):
        path = self.rollout()
        bundle = self.bundle()
        path.write_bytes(path.read_bytes().replace(self.sid.encode(), str(uuid.uuid4()).encode(), 1))
        with self.assertRaisesRegex(ValueError, 'ID|identity'):
            self.bundle()

    def test_forged_subagent_manifest_refused_before_writes(self):
        self.rollout()
        forged = self.bundle()
        entry = forged['files'][0]
        data = base64.b64decode(entry['data']) + b'{"type":"response_item","payload":{"type":"function_call","name":"functions.spawn_agent"}}\n'
        entry.update(data=base64.b64encode(data).decode(), size=len(data), sha256=hashlib.sha256(data).hexdigest())
        with self.assertRaisesRegex(ValueError, 'subagent'):
            self.apply(forged)
        self.assertFalse(self.target.exists())

    def test_claude_roundtrip_keeps_old_cwd_and_lists_new_destination_turns(self):
        directory = self.home / 'projects' / self.api.claude_namespace(self.project.root)
        directory.mkdir(parents=True)
        path = directory / (self.sid + '.jsonl')
        row = dict(type='user', sessionId=self.sid, cwd=str(self.project.root), message=dict(role='user', content='source'))
        path.write_text(json.dumps(row) + '\n')
        bundle = self.api.export_snapshot(self.project, 'claude', self.sid, self.home, source_host='a', generation=1, agent_version='2.1.302')
        self.api.import_snapshot(self.dest, 'claude', self.sid, self.target, bundle, state=self.state)
        target = self.target / 'projects' / self.api.claude_namespace(self.dest.root) / path.name
        row.update(cwd=str(self.dest.root), message=dict(role='user', content='destination'))
        target.write_bytes(target.read_bytes() + json.dumps(row).encode() + b'\n')
        listed = self.api.list_sessions(self.dest, 'claude', self.target, state=self.state)
        self.assertEqual([item['session_id'] for item in listed], [self.sid])
        back = self.api.export_snapshot(self.dest, 'claude', self.sid, self.target, source_host='b', generation=2, agent_version='2.1.302', state=self.state)
        self.api.import_snapshot(self.project, 'claude', self.sid, self.home, back, state=self.root / 'return')
        self.assertEqual(path.read_bytes(), target.read_bytes())
        again = self.api.export_snapshot(self.project, 'claude', self.sid, self.home, source_host='a', generation=3, agent_version='2.1.302', state=self.root / 'return')
        self.api.import_snapshot(self.dest, 'claude', self.sid, self.target, again, state=self.state)

    def test_version_gap_refused(self):
        path = self.rollout()
        path.write_bytes(path.read_bytes().replace(b'0.164.0', b'9.0.0'))
        with self.assertRaisesRegex(ValueError, 'version'):
            self.bundle()


if __name__ == '__main__':
    unittest.main()
