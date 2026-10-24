"""Picker-ready checkpoint imports never need an interactive/model turn."""
import copy
import hashlib
import json
import os
from pathlib import Path
import uuid
from unittest.mock import patch

from test_native_checkpoints import NativeFixture, lines


class PublicationTests(NativeFixture):
    def publish(self, entry, *, env=None):
        manifest = dict(schema=2, project=dict(project_id=self.project.project_id, identity=self.project.identity), conversations=dict(schema=1, entries=[entry], issues=[]))
        self.store.load = lambda sid: manifest
        return self.api.publish_conversations(self.project, self.store, 'checkpoint', state=self.root / 'publication-state', env=env or self.env)

    def codex_ancestor_fixture(self):
        source = self.codex()
        leaf = str(uuid.uuid4())
        self.codex(leaf, parent=(source, source.stat().st_size, len(source.read_bytes().splitlines())), rows=[])
        entry = next(e for e in self.capture()['entries'] if e['session_id'] == leaf)
        env = dict(self.env, CODEX_HOME=str(self.root / 'retry-destination'))
        turns, resumes = [self.turn], []
        class Client:
            def __init__(inner, *args, **kwargs): pass
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def call(inner, method, params):
                if method == 'thread/resume':
                    inner.sid = params['threadId']
                    resumes.append(inner.sid)
                    return dict(thread=dict(id=inner.sid))
                if method == 'thread/list':
                    return dict(data=[dict(id=inner.sid)], nextCursor=None)
                return {}
        for mocked in (patch.object(self.api.agents, 'CodexClient', Client),
                       patch.object(self.api.agents, '_history', side_effect=lambda *args: dict(turns=[dict(id=t, status='completed') for t in turns], items=[]))):
            mocked.start(); self.addCleanup(mocked.stop)
        return source, leaf, entry, env, turns, resumes

    def advance_ancestor(self, source, leaf, turns, *, metadata_only=False):
        ordinal = len(source.read_bytes().splitlines())
        if metadata_only:
            rows = [dict(type='event_msg', payload=dict(type='thread_settings_applied', thread_settings=dict(cwd=str(self.project.root))))]
        else:
            turn = str(uuid.uuid4()); turns.append(turn)
            rows = [dict(type='event_msg', payload=dict(type='task_started', turn_id=turn)),
                    dict(type='event_msg', payload=dict(type='task_complete', turn_id=turn))]
        with source.open('ab') as out:
            out.write(lines([dict(row, ordinal=ordinal+i) for i, row in enumerate(rows)]))
        self.codex(leaf, parent=(source, source.stat().st_size, len(source.read_bytes().splitlines())), rows=[])
        return next(e for e in self.capture()['entries'] if e['session_id'] == leaf)

    def test_retry_never_appends_borrowed_ancestor_after_metadata_only_advance(self):
        source, leaf, entry, env, turns, _ = self.codex_ancestor_fixture()
        target = self.api._codex_target(Path(env['CODEX_HOME']), source.name)
        target.parent.mkdir(parents=True); target.write_bytes(source.read_bytes())
        before, inode = target.read_bytes(), target.stat().st_ino
        with patch.object(self.api.agents, '_history', side_effect=ValueError('fixture failed projection')):
            self.assertIn('failed projection', self.publish(entry, env=env)['issues'][0]['reason'])
        advanced = self.advance_ancestor(source, leaf, turns, metadata_only=True)
        self.assertEqual(self.api._revision(entry, *self.api._preflight(self.project, self.store, entry)[2:]),
                         self.api._revision(advanced, *self.api._preflight(self.project, self.store, advanced)[2:]))
        with patch.object(self.api, '_finish_append', wraps=self.api._finish_append) as append:
            retried = self.publish(advanced, env=env)
        self.assertTrue(retried['issues'])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_ino, inode)
        append.assert_not_called()

    def check_owned_append_retry(self, *, partial):
        source, leaf, entry, env, turns, resumes = self.codex_ancestor_fixture()
        self.assertEqual(self.publish(entry, env=env)['issues'], [])
        target = self.api._codex_target(Path(env['CODEX_HOME']), source.name)
        before, inode = target.read_bytes(), target.stat().st_ino
        advanced = self.advance_ancestor(source, leaf, turns)
        if partial:
            write = os.write
            progress = []
            def interrupt(fd, data):
                if os.fstat(fd).st_ino == inode:
                    if progress: raise OSError('fixture partial append')
                    progress.append(True)
                    return write(fd, data[:max(1, len(data)//2)])
                return write(fd, data)
            fault = patch.object(self.api.os, 'write', side_effect=interrupt)
        else:
            fault = patch.object(self.api, '_finish_append', side_effect=OSError('fixture append intent before write'))
        with fault:
            self.assertIn('fixture', self.publish(advanced, env=env)['issues'][0]['reason'])
        self.assertTrue(target.read_bytes().startswith(before))
        if partial:
            self.assertGreater(target.stat().st_size, len(before))
            self.assertLess(target.stat().st_size, source.stat().st_size)
        else:
            self.assertEqual(target.read_bytes(), before)
        retried = self.publish(advanced, env=env)
        self.assertEqual(retried['issues'], [])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.read_bytes(), source.read_bytes())
        count = len(list(Path(env['CODEX_HOME']).rglob('*.jsonl')))
        self.assertEqual(count, 3)
        self.assertEqual(len(resumes), 2)
        self.assertEqual(len(self.publish(advanced, env=env)['unchanged']), 1)
        self.assertEqual(len(list(Path(env['CODEX_HOME']).rglob('*.jsonl'))), count)

    def test_owned_ancestor_append_retries_after_intent_before_write(self):
        self.check_owned_append_retry(partial=False)

    def test_owned_ancestor_append_retries_after_partial_suffix(self):
        self.check_owned_append_retry(partial=True)

    def test_owned_ancestor_supports_multiple_successive_completed_advances(self):
        source, leaf, entry, env, turns, resumes = self.codex_ancestor_fixture()
        self.assertEqual(self.publish(entry, env=env)['issues'], [])
        target = self.api._codex_target(Path(env['CODEX_HOME']), source.name)
        first_prefix, inode = target.read_bytes(), target.stat().st_ino
        previous = first_prefix
        for _ in range(3):
            advanced = self.advance_ancestor(source, leaf, turns)
            result = self.publish(advanced, env=env)
            self.assertEqual(result['issues'], [])
            self.assertEqual(target.stat().st_ino, inode)
            self.assertTrue(target.read_bytes().startswith(previous))
            self.assertTrue(target.read_bytes().startswith(first_prefix))
            self.assertEqual(target.read_bytes(), source.read_bytes())
            previous = target.read_bytes()
            count = len(list(Path(env['CODEX_HOME']).rglob('*.jsonl')))
            calls = len(resumes)
            self.assertEqual(len(self.publish(advanced, env=env)['unchanged']), 1)
            self.assertEqual(len(list(Path(env['CODEX_HOME']).rglob('*.jsonl'))), count)
            self.assertEqual(len(resumes), calls)
        self.assertEqual(len(resumes), 4)

    def check_successive_append_interruption(self, *, advance_before_retry):
        source, leaf, entry, env, turns, resumes = self.codex_ancestor_fixture()
        self.assertEqual(self.publish(entry, env=env)['issues'], [])
        target = self.api._codex_target(Path(env['CODEX_HOME']), source.name)
        first_prefix, inode = target.read_bytes(), target.stat().st_ino
        first_advance = self.advance_ancestor(source, leaf, turns)
        self.assertEqual(self.publish(first_advance, env=env)['issues'], [])
        before = target.read_bytes()
        second_advance = self.advance_ancestor(source, leaf, turns)
        write, progressed = os.write, []
        def interrupt(fd, data):
            if os.fstat(fd).st_ino == inode:
                if progressed: raise OSError('fixture successive partial append')
                progressed.append(True)
                return write(fd, data[:max(1, len(data)//2)])
            return write(fd, data)
        with patch.object(self.api.os, 'write', side_effect=interrupt):
            result = self.publish(second_advance, env=env)
        self.assertIn('fixture successive partial append', result['issues'][0]['reason'])
        self.assertTrue(target.read_bytes().startswith(before))
        self.assertGreater(target.stat().st_size, len(before))
        self.assertLess(target.stat().st_size, source.stat().st_size)
        latest = self.advance_ancestor(source, leaf, turns) if advance_before_retry else second_advance
        result = self.publish(latest, env=env)
        self.assertEqual(result['issues'], [])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertTrue(target.read_bytes().startswith(first_prefix))
        self.assertTrue(target.read_bytes().startswith(before))
        self.assertEqual(target.read_bytes(), source.read_bytes())
        if not advance_before_retry:
            latest = self.advance_ancestor(source, leaf, turns)
            self.assertEqual(self.publish(latest, env=env)['issues'], [])
            self.assertEqual(target.stat().st_ino, inode)
            self.assertEqual(target.read_bytes(), source.read_bytes())
        calls = len(resumes)
        count = len(list(Path(env['CODEX_HOME']).rglob('*.jsonl')))
        self.assertEqual(len(self.publish(latest, env=env)['unchanged']), 1)
        self.assertEqual(len(resumes), calls)
        self.assertEqual(len(list(Path(env['CODEX_HOME']).rglob('*.jsonl'))), count)

    def test_second_owned_ancestor_advance_retries_after_partial_suffix(self):
        self.check_successive_append_interruption(advance_before_retry=False)

    def test_historical_partial_append_reconciles_before_newer_advance(self):
        self.check_successive_append_interruption(advance_before_retry=True)

    def test_diverged_new_checkpoint_never_finishes_historical_partial_append(self):
        source, leaf, entry, env, turns, _ = self.codex_ancestor_fixture()
        self.assertEqual(self.publish(entry, env=env)['issues'], [])
        target = self.api._codex_target(Path(env['CODEX_HOME']), source.name)
        inode = target.stat().st_ino
        advanced = self.advance_ancestor(source, leaf, turns)
        write, progressed = os.write, []
        def interrupt(fd, data):
            if os.fstat(fd).st_ino == inode:
                if progressed: raise OSError('fixture historical partial append')
                progressed.append(True)
                return write(fd, data[:max(1, len(data)//2)])
            return write(fd, data)
        with patch.object(self.api.os, 'write', side_effect=interrupt):
            self.assertTrue(self.publish(advanced, env=env)['issues'])
        before = target.read_bytes()
        self.advance_ancestor(source, leaf, turns)
        source.write_bytes(source.read_bytes().replace(b'ORCHID', b'VIOLET'))
        diverged = next(e for e in self.capture()['entries'] if e['session_id'] == leaf)
        self.assertTrue(self.publish(diverged, env=env)['issues'])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.read_bytes(), before)

    def test_completed_historical_append_requires_its_exact_final_prefix(self):
        source, leaf, entry, env, turns, _ = self.codex_ancestor_fixture()
        self.assertEqual(self.publish(entry, env=env)['issues'], [])
        target = self.api._codex_target(Path(env['CODEX_HOME']), source.name)
        before, inode = target.read_bytes(), target.stat().st_ino
        advanced = self.advance_ancestor(source, leaf, turns)
        self.assertEqual(self.publish(advanced, env=env)['issues'], [])
        final = target.read_bytes()
        for path in (self.root / 'publication-state').glob('conversation-imports/*/receipt.json'):
            receipt = json.loads(path.read_bytes())
            if receipt['source_checkpoint']['cutoff']['turn_id'] == advanced['cutoff']['turn_id']:
                # Reproduce the immutable completed receipt shape written by
                # the previous producer, which retained append_from on success.
                item = next(item for item in receipt['files'] if item.get('source_session_id') == self.sid)
                item.update(append_from=len(before), previous_sha256=hashlib.sha256(before).hexdigest())
                self.api.atomic_write(path, json.dumps(receipt).encode() + b'\n', mode=0o400)
        latest = self.advance_ancestor(source, leaf, turns)
        target.write_bytes(final[:-1])  # Post-completion truncation is not a pending append.
        truncated = target.read_bytes()
        self.assertTrue(self.publish(latest, env=env)['issues'])
        self.assertEqual(target.read_bytes(), truncated)
        self.assertEqual(target.stat().st_ino, inode)
        target.write_bytes(final)
        self.assertEqual(self.publish(latest, env=env)['issues'], [])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_codex_ancestor_publication_recovers_after_link_before_unlink(self):
        source, _, entry, env, _, resumes = self.codex_ancestor_fixture()
        original, source_inode = source.read_bytes(), source.stat().st_ino
        target = self.api._codex_target(Path(env['CODEX_HOME']), source.name)
        link = os.link
        def interrupt(*args, **kwargs):
            link(*args, **kwargs)
            raise OSError('fixture linked ancestor')
        with patch.object(self.api.os, 'link', side_effect=interrupt):
            self.assertIn('linked ancestor', self.publish(entry, env=env)['issues'][0]['reason'])
        inode = target.stat().st_ino
        self.assertEqual(target.stat().st_nlink, 2)
        retried = self.publish(entry, env=env)
        self.assertEqual(retried['issues'], [])
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(len(resumes), 1)
        self.assertEqual(len(self.publish(entry, env=env)['unchanged']), 1)
        self.assertEqual(len(list(Path(env['CODEX_HOME']).rglob('*.jsonl'))), 2)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(source.stat().st_ino, source_inode)

    def test_claude_superseding_recovers_after_link_before_unlink(self):
        source = self.claude(); entry = self.capture()['entries'][0]
        first = self.publish(entry)
        old = source.with_name(first['imported'][0]['session_id'] + '.jsonl')
        before, inode = old.read_bytes(), old.stat().st_ino
        with source.open('ab') as out:
            out.write(lines([dict(type='assistant', uuid=str(uuid.uuid4()), sessionId=self.sid, cwd=str(self.project.root), version='2.1.302', message=dict(role='assistant', content='Next answer', stop_reason='end_turn'))]))
        original, source_inode = source.read_bytes(), source.stat().st_ino
        advanced = next(e for e in self.capture()['entries'] if e['session_id'] == self.sid)
        link = os.link
        def interrupt(*args, **kwargs):
            linked = link(*args, **kwargs)
            if args[1] == 'archived.jsonl': raise OSError('fixture linked archive')
            return linked
        with patch.object(self.api.os, 'link', side_effect=interrupt):
            self.assertIn('linked archive', self.publish(advanced)['issues'][0]['reason'])
        self.assertEqual(old.stat().st_nlink, 2)
        archive = next((self.root / 'publication-state').glob('conversation-imports/*/archived.jsonl'))
        retried = self.publish(advanced)
        self.assertEqual(retried['issues'], [])
        self.assertEqual(len(retried['unchanged']), 1)
        self.assertFalse(old.exists())
        self.assertEqual(archive.stat().st_nlink, 1)
        self.assertEqual(archive.stat().st_ino, inode)
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual(len(list(source.parent.glob('*.jsonl'))), 2)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(source.stat().st_ino, source_inode)

    def test_foreign_second_link_never_authorizes_native_recovery(self):
        source, _, entry, env, _, _ = self.codex_ancestor_fixture()
        with patch.object(self.api.agents, '_history', side_effect=ValueError('fixture failed projection')):
            self.assertTrue(self.publish(entry, env=env)['issues'])
        target = self.api._codex_target(Path(env['CODEX_HOME']), source.name)
        foreign = self.root / 'foreign-hardlink'
        os.link(target, foreign)
        before, inode = target.read_bytes(), target.stat().st_ino
        retried = self.publish(entry, env=env)
        self.assertTrue(retried['issues'])
        self.assertTrue(foreign.exists())
        self.assertEqual(target.stat().st_nlink, 2)
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.read_bytes(), before)

    def test_foreign_archive_link_is_never_removed_during_claude_recovery(self):
        source = self.claude(); entry = self.capture()['entries'][0]
        first = self.publish(entry)
        old = source.with_name(first['imported'][0]['session_id'] + '.jsonl')
        before, inode = old.read_bytes(), old.stat().st_ino
        receipt = next((self.root / 'publication-state').glob('conversation-imports/*/receipt.json'))
        archive = receipt.with_name('archived.jsonl')
        archive.write_bytes(before)  # Identical bytes do not establish inode ownership.
        foreign = self.root / 'foreign-archive-link'
        os.link(archive, foreign)
        foreign_inode = archive.stat().st_ino
        with source.open('ab') as out:
            out.write(lines([dict(type='assistant', uuid=str(uuid.uuid4()), sessionId=self.sid, cwd=str(self.project.root), version='2.1.302', message=dict(role='assistant', content='Next', stop_reason='end_turn'))]))
        advanced = next(e for e in self.capture()['entries'] if e['session_id'] == self.sid)
        retried = self.publish(advanced)
        self.assertTrue(retried['issues'])
        self.assertEqual(archive.stat().st_ino, foreign_inode)
        self.assertEqual(archive.stat().st_nlink, 2)
        self.assertEqual(archive.read_bytes(), before)
        self.assertTrue(foreign.exists())
        self.assertEqual(old.stat().st_ino, inode)
        self.assertEqual(old.read_bytes(), before)

    def test_initial_claude_publication_recovers_link_before_unlink(self):
        source = self.claude(); entry = self.capture()['entries'][0]
        original, source_inode = source.read_bytes(), source.stat().st_ino
        link = os.link
        def interrupt(*args, **kwargs):
            link(*args, **kwargs)
            raise OSError('fixture initial Claude linked')
        with patch.object(self.api.os, 'link', side_effect=interrupt):
            self.assertIn('initial Claude linked', self.publish(entry)['issues'][0]['reason'])
        receipt_path = next((self.root / 'publication-state').glob('conversation-imports/*/receipt.json'))
        item = json.loads(receipt_path.read_bytes())['files'][0]
        target, stage = Path(item['destination']), Path(item['staged_path'])
        child, inode, before = target.stem, target.stat().st_ino, target.read_bytes()
        self.assertEqual(target.stat().st_nlink, 2)
        retried = self.publish(entry)
        self.assertEqual(retried['issues'], [])
        self.assertEqual(retried['imported'][0]['session_id'], child)
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertFalse(stage.exists())
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(len(self.publish(entry)['unchanged']), 1)
        self.assertEqual(len(list(source.parent.glob('*.jsonl'))), 2)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(source.stat().st_ino, source_inode)

    def test_initial_claude_retry_refuses_foreign_second_link(self):
        source = self.claude(); entry = self.capture()['entries'][0]
        original = source.read_bytes()
        write = self.api.atomic_write
        def interrupt(path, data, *args, **kwargs):
            if path.name == 'receipt.json' and json.loads(data).get('status') == 'complete':
                raise OSError('fixture complete receipt failed')
            return write(path, data, *args, **kwargs)
        with patch.object(self.api, 'atomic_write', side_effect=interrupt):
            self.assertTrue(self.publish(entry)['issues'])
        receipt_path = next((self.root / 'publication-state').glob('conversation-imports/*/receipt.json'))
        item = json.loads(receipt_path.read_bytes())['files'][0]
        target = Path(item['destination'])
        foreign = self.root / 'foreign-Claude-link'
        os.link(target, foreign)
        before, inode = target.read_bytes(), target.stat().st_ino
        self.assertTrue(self.publish(entry)['issues'])
        self.assertTrue(foreign.exists())
        self.assertEqual(target.stat().st_nlink, 2)
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(source.read_bytes(), original)

    def codex_interrupted_leaf_fixture(self):
        source, leaf, entry, env, _, resumes = self.codex_ancestor_fixture()
        originals = {p: p.read_bytes() for p in Path(self.env['CODEX_HOME']).rglob('*.jsonl')}
        with patch.object(self.api.agents, '_history', side_effect=ValueError('fixture projection interrupted')):
            self.assertIn('projection interrupted', self.publish(entry, env=env)['issues'][0]['reason'])
        receipt_path = next((self.root / 'publication-state').glob('conversation-imports/*/receipt.json'))
        receipt = json.loads(receipt_path.read_bytes())
        return entry, env, Path(receipt['fork_path']), resumes, originals

    def test_codex_retry_defers_open_selected_leaf_then_succeeds_after_close(self):
        entry, env, target, resumes, originals = self.codex_interrupted_leaf_fixture()
        before, inode, child = target.read_bytes(), target.stat().st_ino, resumes[-1]
        with target.open('ab'):
            with self.assertRaisesRegex(ValueError, 'open file'):
                self.api.sessions.assert_quiescent([target])
            with patch.object(self.api.agents, 'CodexClient', wraps=self.api.agents.CodexClient) as native:
                result = self.publish(entry, env=env)
            self.assertTrue(result['issues'])
            native.assert_not_called()
            self.assertEqual(resumes, [child])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_ino, inode)
        retried = self.publish(entry, env=env)
        self.assertEqual(retried['issues'], [])
        self.assertEqual(retried['imported'][0]['session_id'], child)
        self.assertEqual(len(self.publish(entry, env=env)['unchanged']), 1)
        self.assertEqual(resumes, [child, child])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_ino, inode)
        for path, original in originals.items(): self.assertEqual(path.read_bytes(), original)

    def check_codex_continued_leaf_retry(self, *, completed=False, partial=False):
        entry, env, target, resumes, originals = self.codex_interrupted_leaf_fixture()
        prefix, inode, child = target.read_bytes(), target.stat().st_ino, resumes[-1]
        turn = str(uuid.uuid4())
        rows = [dict(type='event_msg', payload=dict(type='task_started', turn_id=turn)),
                dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='User continuation')]))]
        if completed: rows.append(dict(type='event_msg', payload=dict(type='task_complete', turn_id=turn)))
        with target.open('ab') as out:
            out.write(b'{"type":' if partial else lines(rows))
        before = target.read_bytes()
        with patch.object(self.api.agents, 'CodexClient', wraps=self.api.agents.CodexClient) as native:
            result = self.publish(entry, env=env)
        self.assertTrue(result['issues'])
        native.assert_not_called()
        self.assertEqual(resumes, [child])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(target.read_bytes().startswith(prefix))
        self.assertEqual(len(list(Path(env['CODEX_HOME']).rglob('*.jsonl'))), 2)
        for path, original in originals.items(): self.assertEqual(path.read_bytes(), original)

    def test_codex_retry_preserves_completed_user_leaf_without_native_writer(self):
        self.check_codex_continued_leaf_retry(completed=True)

    def test_codex_retry_preserves_unfinished_user_leaf_without_native_writer(self):
        self.check_codex_continued_leaf_retry()

    def test_codex_retry_preserves_partial_user_leaf_without_native_writer(self):
        self.check_codex_continued_leaf_retry(partial=True)

    def test_codex_retry_allows_quiescent_metadata_only_projection_suffix(self):
        entry, env, target, resumes, _ = self.codex_interrupted_leaf_fixture()
        child = resumes[-1]
        with target.open('ab') as out:
            out.write(lines([dict(type='event_msg', payload=dict(type='thread_settings_applied', thread_settings=dict(cwd=str(self.project.root))))]))
        before, inode = target.read_bytes(), target.stat().st_ino
        retried = self.publish(entry, env=env)
        self.assertEqual(retried['issues'], [])
        self.assertEqual(retried['imported'][0]['session_id'], child)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(resumes, [child, child])

    def test_selected_codex_publication_recovers_link_before_unlink(self):
        _, _, entry, env, _, resumes = self.codex_ancestor_fixture()
        link = os.link
        def interrupt(source, target, **kwargs):
            result = link(source, target, **kwargs)
            if not target.endswith(self.sid + '.jsonl'):
                raise OSError('fixture selected Codex linked')
            return result
        with patch.object(self.api.os, 'link', side_effect=interrupt):
            self.assertIn('selected Codex linked', self.publish(entry, env=env)['issues'][0]['reason'])
        receipt_path = next((self.root / 'publication-state').glob('conversation-imports/*/receipt.json'))
        item = next(item for item in json.loads(receipt_path.read_bytes())['files'] if item.get('native_fork'))
        target, stage = Path(item['destination']), Path(item['staged_path'])
        before, inode, child = target.read_bytes(), target.stat().st_ino, item['session_id']
        self.assertEqual(target.stat().st_nlink, 2)
        self.assertEqual(self.publish(entry, env=env)['issues'], [])
        self.assertEqual(resumes, [child])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertFalse(stage.exists())
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(len(self.publish(entry, env=env)['unchanged']), 1)

    def test_selected_codex_retry_refuses_foreign_second_link(self):
        entry, env, target, resumes, _ = self.codex_interrupted_leaf_fixture()
        foreign = self.root / 'foreign-selected-Codex-link'
        os.link(target, foreign)
        before, inode, child = target.read_bytes(), target.stat().st_ino, resumes[-1]
        with patch.object(self.api.agents, 'CodexClient', wraps=self.api.agents.CodexClient) as native:
            result = self.publish(entry, env=env)
        self.assertTrue(result['issues'])
        native.assert_not_called()
        self.assertTrue(foreign.exists())
        self.assertEqual(target.stat().st_nlink, 2)
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(resumes, [child])

    def test_claude_picker_ready_standalone_and_semantic_idempotence(self):
        original = self.claude()
        entry = self.capture()['entries'][0]
        result = self.publish(entry)
        self.assertEqual(result['issues'], [])
        child = result['imported'][0]['session_id']
        target = original.with_name(child + '.jsonl')
        rows = [json.loads(line) for line in target.read_bytes().splitlines()]
        self.assertEqual([r['sessionId'] for r in rows], [child, child])
        self.assertEqual([r['message'] for r in rows], [r['message'] for r in map(json.loads, original.read_bytes().splitlines())])
        self.assertTrue(target.stat().st_mode & 0o200)
        entry['title'] = 'Different display title'; entry['updated_at'] += 100
        again = self.publish(entry)
        self.assertEqual(again['imported'], [])
        self.assertEqual(again['unchanged'][0]['session_id'], child)
        captured = self.api.capture_conversations(self.project, self.store, state=self.root / 'publication-state', env=self.env)
        self.assertEqual(captured['issues'], [])
        self.assertEqual([e['session_id'] for e in captured['entries']], [self.sid])

    def test_claude_completed_turn_advance_exports_child_then_imports_once(self):
        original = self.claude()
        entry = self.capture()['entries'][0]
        result = self.publish(entry)
        child = result['imported'][0]['session_id']
        target = original.with_name(child + '.jsonl')
        with target.open('ab') as out:
            out.write(lines([dict(type='user', uuid=str(uuid.uuid4()), sessionId=child, cwd=str(self.project.root), version='2.1.302', message=dict(role='user', content='Next')), dict(type='assistant', uuid=str(uuid.uuid4()), sessionId=child, cwd=str(self.project.root), version='2.1.302', message=dict(role='assistant', content='Next answer', stop_reason='end_turn'))]))
        captured = self.api.capture_conversations(self.project, self.store, state=self.root / 'publication-state', env=self.env)
        advanced = next(e for e in captured['entries'] if e['session_id'] == child)
        self.assertEqual(self.publish(advanced)['issues'], [])
        self.assertEqual(len(self.publish(advanced)['unchanged']), 1)

    def test_claude_environment_and_request_dependent_payloads_preserved(self):
        self.claude(rows=[dict(type='user',uuid=str(uuid.uuid4()),message=dict(role='user',content='Question')),dict(type='attachment',uuid=str(uuid.uuid4()),attachment=dict(type='environment',snapshot=dict(workingDirectory=str(self.project.root),additionalWorkingDirectories=[]))),dict(type='assistant',uuid=str(uuid.uuid4()),message=dict(role='assistant',content='Answer',stop_reason='end_turn'))])
        entry=self.capture()['entries'][0]
        result=self.publish(entry)
        self.assertEqual(result['issues'],[])
        self.api._preflight(self.project,self.store,entry)

    def test_history_matches_only_a_web_search_extension_to_web_search(self):
        history = dict(turns=[dict(id=self.turn, status='completed')], items=[dict(item=dict(type='webSearch', id='exec-1', query='orchid'))])
        def persisted(kind):
            return [self.turn], [dict(type='Extension', kind=kind, id='exec-1', query='orchid')]
        self.api._verify_history(history, persisted('web.search'))
        for kind in ('web.fetch', 'image.generation', None):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'history items do not match'):
                self.api._verify_history(history, persisted(kind))

    def test_wrong_manifest_identity_and_legacy_manifest_rejected_without_writes(self):
        self.store.load=lambda sid:dict(schema=1,files=[])
        with self.assertRaises(ValueError):self.api.publish_conversations(self.project,self.store,'bad',state=self.root/'state',env=self.env)
        self.store.load=lambda sid:dict(schema=2,project=dict(project_id='wrong',identity=self.project.identity),conversations=dict(entries=[]))
        with self.assertRaises(ValueError):self.api.publish_conversations(self.project,self.store,'bad',state=self.root/'state',env=self.env)

    def test_codex_publish_never_injects_or_resumes_completed_fork(self):
        self.codex();entry=self.capture()['entries'][0]
        env=dict(self.env,CODEX_HOME=str(self.root/'destination'))
        calls=[]
        class Client:
            def __init__(inner,*args,**kwargs):pass
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def call(inner,method,params):
                if method == 'thread/resume': inner.publication_id = params['threadId']
                if method == 'thread/list': return dict(data=[dict(id=inner.publication_id)], nextCursor=None)
                calls.append((method,params))
                return dict(thread=dict(id=params['threadId']))
        with patch.object(self.api.agents,'CodexClient',Client),patch.object(self.api.agents,'_history',return_value=dict(turns=[dict(id=self.turn,status='completed')],items=[])):
            result=self.publish(entry,env=env)
            again=self.publish(dict(entry,title='New title',updated_at=1234),env=env)
        self.assertEqual(result['issues'],[])
        self.assertEqual(len(again['unchanged']),1)
        child=result['imported'][0]['session_id']
        self.assertEqual([call for call in calls if call[0]=='thread/resume'],[('thread/resume',dict(threadId=child,**self.api.agents._runtime(self.project)))])
        self.assertNotEqual(child,self.sid)
        captured=self.api.capture_conversations(self.project,self.store,state=self.root/'publication-state',env=env)
        self.assertEqual(captured['entries'],[])
        self.assertEqual(captured['issues'],[])

    def test_codex_owned_staging_prefix_advances_without_rewriting_prior_bytes(self):
        path=self.codex();leaf=str(uuid.uuid4())
        child=self.codex(leaf,parent=(path,path.stat().st_size,len(path.read_bytes().splitlines())),rows=[])
        entry=next(e for e in self.capture()['entries'] if e['session_id']==leaf)
        env=dict(self.env,CODEX_HOME=str(self.root/'advance-destination'))
        class Client:
            def __init__(inner,*args,**kwargs):pass
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def call(inner,method,params):
                if method == 'thread/resume': inner.publication_id = params['threadId']
                if method == 'thread/list': return dict(data=[dict(id=inner.publication_id)], nextCursor=None)
                return dict(thread=dict(id=params.get('threadId')))
        turns=[self.turn]
        def history(*args):return dict(turns=[dict(id=turn,status='completed') for turn in turns],items=[])
        with patch.object(self.api.agents,'CodexClient',Client),patch.object(self.api.agents,'_history',side_effect=history):
            first=self.publish(entry,env=env)
            self.assertEqual(first['issues'],[])
            source=next(p for p in Path(env['CODEX_HOME']).rglob('*.jsonl') if p.name.endswith(self.sid+'.jsonl'))
            before=source.read_bytes();inode=source.stat().st_ino
            next_turn=str(uuid.uuid4());turns.append(next_turn)
            rows=[dict(type='event_msg',payload=dict(type='task_started',turn_id=next_turn)),dict(type='event_msg',payload=dict(type='task_complete',turn_id=next_turn))]
            with path.open('ab') as out:out.write(lines([dict(row,ordinal=len(before.splitlines())+i) for i,row in enumerate(rows)]))
            self.codex(leaf,parent=(path,path.stat().st_size,len(path.read_bytes().splitlines())),rows=[])
            advanced=next(e for e in self.capture()['entries'] if e['session_id']==leaf)
            second=self.publish(advanced,env=env)
        self.assertEqual(second['issues'],[])
        self.assertEqual(len(second['imported']),1)
        self.assertEqual(source.stat().st_ino,inode)
        self.assertTrue(source.read_bytes().startswith(before))
        self.assertEqual(source.read_bytes(),path.read_bytes())

    def test_claude_supersedes_only_unchanged_owned_predecessors(self):
        source=self.claude();entry=self.capture()['entries'][0]
        first=self.publish(entry);old=source.with_name(first['imported'][0]['session_id']+'.jsonl')
        user,answer=str(uuid.uuid4()),str(uuid.uuid4())
        with source.open('ab') as out:out.write(lines([dict(type='user',uuid=user,sessionId=self.sid,cwd=str(self.project.root),version='2.1.302',message=dict(role='user',content='Next')),dict(type='assistant',uuid=answer,sessionId=self.sid,cwd=str(self.project.root),version='2.1.302',message=dict(role='assistant',content='Answer',stop_reason='end_turn'))]))
        next_entry=next(e for e in self.capture()['entries'] if e['session_id']==self.sid)
        second=self.publish(next_entry)
        self.assertEqual(second['issues'],[])
        self.assertFalse(old.exists())
        self.assertEqual(len(list((self.root/'publication-state').glob('conversation-imports/*/archived.jsonl'))),1)
        self.assertEqual(len(self.publish(entry)['unchanged']),1)
        current=source.with_name(second['imported'][0]['session_id']+'.jsonl')
        with current.open('ab') as out:out.write(lines([dict(type='user',uuid=str(uuid.uuid4()),sessionId=current.stem,cwd=str(self.project.root),version='2.1.302',message=dict(role='user',content='Unsaved incomplete turn'))]))
        with source.open('ab') as out:out.write(lines([dict(type='assistant',uuid=str(uuid.uuid4()),sessionId=self.sid,cwd=str(self.project.root),version='2.1.302',message=dict(role='assistant',content='Another completed turn',stop_reason='end_turn'))]))
        final=next(e for e in self.capture()['entries'] if e['session_id']==self.sid)
        self.assertEqual(self.publish(final)['issues'],[])
        self.assertTrue(current.exists())

    def test_conflicting_codex_ancestor_identity_refuses_before_new_native_writes(self):
        parent=self.codex();child=str(uuid.uuid4())
        self.codex(child,parent=(parent,parent.stat().st_size,len(parent.read_bytes().splitlines())),rows=[])
        entry=next(e for e in self.capture()['entries'] if e['session_id']==child)
        env=dict(self.env,CODEX_HOME=str(self.root/'foreign-destination'))
        target=Path(env['CODEX_HOME'])/'sessions/2026/10/17'/parent.name
        target.parent.mkdir(parents=True);foreign=parent.read_bytes().replace(b'ORCHID',b'VIOLET');target.write_bytes(foreign)
        with patch.object(self.api.agents,'CodexClient',side_effect=AssertionError('foreign identity must not load')):
            result=self.publish(entry,env=env)
        self.assertEqual(result['imported'],[])
        self.assertIn('diverge',result['issues'][0]['reason'])
        self.assertEqual(target.read_bytes(),foreign)
        self.assertEqual(len(list(Path(env['CODEX_HOME']).rglob('*.jsonl'))),1)

    def test_hidden_native_codex_import_is_an_issue_not_picker_ready_success(self):
        self.codex();entry=self.capture()['entries'][0]
        env=dict(self.env,CODEX_HOME=str(self.root/'hidden-destination'))
        class Client:
            def __init__(inner,*args,**kwargs):pass
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def call(inner,method,params):
                if method == 'thread/resume': inner.publication_id = params['threadId']
                if method=='thread/list':return dict(data=[],nextCursor=None)
                return dict(thread=dict(id=params['threadId']))
        with patch.object(self.api.agents,'CodexClient',Client),patch.object(self.api.agents,'_history',return_value=dict(turns=[dict(id=self.turn,status='completed')],items=[])):
            result=self.publish(entry,env=env)
        self.assertEqual(result['imported'],[])
        self.assertIn('picker',result['issues'][0]['reason'])

    def test_both_agents_publish_into_another_checkout_of_same_origin(self):
        import subprocess
        from devcontainer_setup.project_sync.state import discover_project
        from devcontainer_setup.project_sync.sessions import claude_namespace
        codex=self.codex();claude=self.claude();originals={p:p.read_bytes() for p in (codex,claude)}
        entries=self.capture()['entries']
        root=self.root/'other-checkout'
        subprocess.run(['git','init','-q',str(root)],check=True)
        subprocess.run(['git','-C',str(root),'remote','add','origin',self.project.remote],check=True)
        project=discover_project(root)
        env=dict(self.env,CODEX_HOME=str(self.root/'other-codex'),CLAUDE_CONFIG_DIR=str(self.root/'other-claude'))
        self.store.load=lambda _:dict(schema=2,project=dict(project_id=project.project_id,identity=project.identity),conversations=dict(entries=entries,issues=[]))
        class Client:
            def __init__(inner,*args,**kwargs):pass
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def call(inner,method,params):
                if method=='thread/resume':inner.sid=params['threadId'];return dict(thread=dict(id=inner.sid))
                return dict(data=[dict(id=inner.sid)],nextCursor=None)
        with patch.object(self.api.agents,'CodexClient',Client),patch.object(self.api.agents,'_history',return_value=dict(turns=[dict(id=self.turn,status='completed')],items=[])):
            result=self.api.publish_conversations(project,self.store,'checkpoint',state=self.root/'cross-state',env=env)
        self.assertEqual(result['issues'],[])
        self.assertEqual(len(result['imported']),2)
        for imported in result['imported']:
            if imported['agent']=='codex':
                target=next(Path(env['CODEX_HOME']).rglob('*'+imported['session_id']+'.jsonl'))
                rows=[json.loads(row) for row in target.read_bytes().splitlines()]
                self.assertEqual(rows[0]['payload']['cwd'],str(root))
                self.assertEqual(target.read_bytes().splitlines()[1:],originals[codex].splitlines()[1:])
            else:
                target=Path(env['CLAUDE_CONFIG_DIR'])/'projects'/claude_namespace(root)/(imported['session_id']+'.jsonl')
                rows=[json.loads(row) for row in target.read_bytes().splitlines()]
                self.assertTrue(all(row.get('cwd')==str(root) for row in rows))
                self.assertEqual([row['message'] for row in rows],[row['message'] for row in map(json.loads,originals[claude].splitlines())])
        captured=self.api.capture_conversations(project,self.store,state=self.root/'cross-state',env=env)
        self.assertEqual(captured,dict(schema=1,entries=[],issues=[]))
        for path,original in originals.items():self.assertEqual(path.read_bytes(),original)

    def test_claude_session_ending_below_the_root_round_trips_between_checkouts(self):
        # A session's root is the checkout it worked in, not its last shell directory (#40).
        import subprocess
        from devcontainer_setup.project_sync.state import discover_project
        from devcontainer_setup.project_sync.sessions import claude_namespace
        def turn(cwd, sid, text):
            return [dict(type='user', sessionId=sid, cwd=cwd, version='2.1.302', uuid=str(uuid.uuid4()), message=dict(role='user', content=text)),
                    dict(type='assistant', sessionId=sid, cwd=cwd, version='2.1.302', uuid=str(uuid.uuid4()),
                         message=dict(role='assistant', content=[dict(type='text', text='Done')], stop_reason='end_turn'))]
        source = str(self.project.root)
        for below in ('web', '.claude/worktrees/feature'):
            with self.subTest(below=below):
                sid = str(uuid.uuid4())
                path = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects' / claude_namespace(self.project.root) / (sid + '.jsonl')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(lines(turn(source, sid, 'Start') + turn(source + '/' + below, sid, 'Work below')))
                entry = next(e for e in self.capture()['entries'] if e['session_id'] == sid)
                self.assertEqual(entry['source_root'], source)
                root = self.root / ('other-' + below.replace('/', '-'))
                subprocess.run(['git', 'init', '-q', str(root)], check=True)
                subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', self.project.remote], check=True)
                other = discover_project(root)
                env = dict(self.env, CODEX_HOME=str(root / '.codex-home'), CLAUDE_CONFIG_DIR=str(root / '.claude-home'))
                state = root / '.state'
                self.store.load = lambda _: dict(schema=2, project=dict(project_id=other.project_id, identity=other.identity), conversations=dict(entries=[entry], issues=[]))
                result = self.api.publish_conversations(other, self.store, 'checkpoint', state=state, env=env)
                self.assertEqual(result['issues'], [])
                self.assertEqual(len(result['imported']), 1)
                imported = result['imported'][0]['session_id']
                published = Path(env['CLAUDE_CONFIG_DIR']) / 'projects' / claude_namespace(root) / (imported + '.jsonl')
                with published.open('ab') as output:
                    output.write(lines(turn(str(root) + '/' + below, imported, 'Continue below')))
                captured = self.api.capture_conversations(other, self.store, state=state, env=env)
                self.assertEqual(captured['issues'], [])
                back = next(e for e in captured['entries'] if e['session_id'] == imported)
                self.assertEqual(back['source_root'], str(root))
                self.store.load = lambda _: dict(schema=2, project=dict(project_id=self.project.project_id, identity=self.project.identity), conversations=dict(entries=[back], issues=[]))
                result = self.api.publish_conversations(self.project, self.store, 'checkpoint', state=self.root / ('back-' + below.replace('/', '-')), env=self.env)
                self.assertEqual(result['issues'], [])
                self.assertEqual(len(result['imported']), 1)
