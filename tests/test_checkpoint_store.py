"""Durable content validation and acknowledgement ordering."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from devcontainer_setup.project_sync.state import atomic_write, read_json

try:
    from devcontainer_setup.project_sync import checkpoint_store
except ImportError:
    checkpoint_store = None


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(checkpoint_store, "Checkpoint storage has not been implemented")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'devcontainer-setup/projects' / ('a' * 24)
        self.store = checkpoint_store.Store(self.path)

    def age(self, family, digest, days):
        path = self.path / 'checkpoints' / family / digest
        when = path.stat().st_mtime - days * 86400
        os.utime(path, (when, when))

    def conversation(self, body, *, sid='session', agent='claude'):
        # Codex dependencies can precede the head in the files list.
        return dict(agent=agent, session_id=sid, source_root='/workspaces/project',
                    cutoff=dict(bytes=len(body)), files=[
                        dict(session_id='ancestor', path='ancestor.jsonl', size=len(b'ancestor context'),
                             sha256=self.store.put(b'ancestor context'), blob=self.store.put(b'ancestor context')),
                        dict(session_id=sid, path=sid + '.jsonl', size=len(body),
                             sha256=self.store.put(body), blob=self.store.put(body))])

    def test_collect_keeps_what_a_seal_names(self):
        snapshots = []
        for name in ('one', 'two'):
            blob = self.store.put(name.encode())
            snapshot = self.store.save({'files': [{'blob': blob}]})
            snapshots.append(snapshot)
            self.age('manifests', snapshot, 10)
            self.age('objects', blob, 10)
            atomic_write(self.path / 'retired' / (name + '.json'),
                         json.dumps({'snapshot_id': snapshot}).encode())
        self.assertEqual(self.store.collect(apply=True)['manifests'], 0)
        for snapshot in snapshots:
            self.assertIsNotNone(self.store.load(snapshot))

    def test_collect_deletes_nothing_when_a_seal_cannot_be_read(self):
        blob = self.store.put(b'would be collected')
        snapshot = self.store.save({'files': [{'blob': blob}]})
        self.age('manifests', snapshot, 10)
        self.age('objects', blob, 10)
        for body in (b'{', b'{}', b'{"snapshot_id": "bad"}', b'{"snapshot_id": []}'):
            with self.subTest(body=body):
                atomic_write(self.path / 'retired/old-space.json', body)
                with self.assertRaises(ValueError):
                    self.store.collect(apply=True)
                self.assertEqual(self.store.read(blob), b'would be collected')
                self.assertIsNotNone(self.store.load(snapshot))

    def test_a_changed_ancestor_is_never_retired(self):
        earlier = self.conversation(b'head', agent='codex')
        changed = copy.deepcopy(earlier)
        body = b'changed ancestor'
        changed['files'][0].update(size=len(body), sha256=self.store.put(body), blob=self.store.put(body))
        self.receive(earlier)
        self.receive(changed)
        self.assertEqual(len(self.relay_records()), 2)
        self.assertEqual(self.store.relay_entries(), ([dict(e, relayed=True) for e in (earlier, changed)], []))

    def test_collect_deletes_nothing_when_seals_cannot_be_listed(self):
        blob = self.store.put(b'sealed data')
        snapshot = self.store.save({'files': [{'blob': blob}]})
        self.age('manifests', snapshot, 10)
        self.age('objects', blob, 10)
        real = checkpoint_store.os.scandir
        def scandir(path):
            if Path(path) == self.path / 'retired':
                raise PermissionError('fixture seals cannot be listed')
            return real(path)
        with mock.patch.object(checkpoint_store.os, 'scandir', side_effect=scandir):
            with self.assertRaisesRegex(ValueError, 'final-flush record cannot be read'):
                self.store.collect(apply=True)
        self.assertEqual(self.store.read(blob), b'sealed data')
        self.assertIsNotNone(self.store.load(snapshot))

    def test_inconsistent_file_metadata_cannot_prove_retirement(self):
        earlier, later = self.conversation(b'AB'), self.conversation(b'AC')
        earlier['files'][1].update(size=1, sha256=hashlib.sha256(b'A').hexdigest())
        self.receive(earlier)
        self.receive(later)
        self.assertEqual(len(self.relay_records()), 2)

    def test_an_identical_retry_retires_a_crash_left_prefix(self):
        earlier, later = self.conversation(b'A'), self.conversation(b'AB')
        self.receive(earlier)
        snapshot = self.store.save(dict(conversations=dict(entries=[later])))
        with mock.patch.object(checkpoint_store.os, 'unlink', side_effect=OSError('crash before retirement')):
            with self.assertRaisesRegex(OSError, 'crash before retirement'):
                self.store.acknowledge(snapshot, source='codespace')
        self.assertEqual(len(self.relay_records()), 2)
        self.store.acknowledge(snapshot, source='codespace')
        self.assertEqual(len(self.relay_records()), 1)
        self.assertEqual(self.store.relay_entries(), ([dict(later, relayed=True)], []))

    def receive(self, *entries, source='codespace'):
        snapshot = self.store.save(dict(schema=2, conversations=dict(entries=list(entries), issues=[])))
        self.store.acknowledge(snapshot, source=source)
        return snapshot

    def relay_records(self):
        return sorted((self.path / 'checkpoints/relay').glob('*.json'))

    def test_codespace_acknowledge_records_each_entry(self):
        entries = [self.conversation(b'A', sid='one'), self.conversation(b'B', sid='two', agent='codex')]
        self.receive(*entries)
        records = self.relay_records()
        self.assertEqual(len(records), 2)
        for path in records:
            record = read_json(path)
            canonical = json.dumps(record['entry'], sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=True, allow_nan=False).encode()
            self.assertEqual(path.name, hashlib.sha256(canonical).hexdigest() + '.json')
            self.assertEqual(set(record), {'schema', 'entry', 'received_at'})
            self.assertEqual(record['schema'], 1)
            self.assertTrue(record['received_at'].endswith('+00:00'))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.store.relay_entries(), ([dict(entry, relayed=True) for entry in entries], []))
        # Callers cannot add relay metadata to the retained entry itself.
        self.assertTrue(all('relayed' not in read_json(path)['entry'] for path in records))

    def test_desktop_acknowledge_records_nothing(self):
        self.receive(self.conversation(b'A', sid='one'), self.conversation(b'B', sid='two'), source='desktop')
        self.assertEqual(self.store.relay_entries(), ([], []))
        self.assertEqual(self.relay_records(), [])

    def test_appended_revision_retires_the_earlier_record(self):
        earlier, later = self.conversation(b'A'), self.conversation(b'AB')
        self.receive(earlier)
        self.receive(later)
        self.assertEqual(len(self.relay_records()), 1)
        self.assertEqual(self.store.relay_entries(), ([dict(later, relayed=True)], []))

    def test_older_revision_arriving_later_is_not_recorded(self):
        later, earlier = self.conversation(b'AB'), self.conversation(b'A')
        self.receive(later)
        self.receive(earlier)
        self.assertEqual(len(self.relay_records()), 1)
        self.assertEqual(self.store.relay_entries(), ([dict(later, relayed=True)], []))

    def test_diverging_revisions_of_one_session_are_both_kept(self):
        entries = [self.conversation(b'AB'), self.conversation(b'AC')]
        for entry in entries:
            self.receive(entry)
        self.assertEqual(len(self.relay_records()), 2)
        self.assertEqual(self.store.relay_entries(), ([dict(entry, relayed=True) for entry in entries], []))

    def test_collect_keeps_record_blobs_after_keep_days(self):
        entry = self.conversation(b'only retained in the relay')
        snapshot = self.receive(entry)
        self.receive()  # Replaces the only receipt that named the old manifest.
        self.age('manifests', snapshot, checkpoint_store.KEEP_DAYS + 2)
        for item in entry['files']:
            self.age('objects', item['blob'], checkpoint_store.KEEP_DAYS + 2)
        for path in self.relay_records():
            when = path.stat().st_mtime - (checkpoint_store.KEEP_DAYS + 2) * 86400
            os.utime(path, (when, when))
        loose = self.store.put(b'unreferenced')
        self.age('objects', loose, checkpoint_store.GRACE_SECONDS / 86400 + 1)
        collected = self.store.collect(apply=True)
        self.assertEqual((collected['manifests'], collected['objects']), (1, 1))
        self.assertEqual([self.store.read(item['blob']) for item in entry['files']],
                         [b'ancestor context', b'only retained in the relay'])
        self.assertEqual(self.store.relay_entries(), ([dict(entry, relayed=True)], []))

    def test_seeding_uses_current_receipt_and_unsuperseded_journals(self):
        received = self.conversation(b'received', sid='received')
        snapshot = self.store.save(dict(conversations=dict(entries=[received])))
        atomic_write(self.path / 'checkpoints/receipts/codespace.json', json.dumps(dict(
            snapshot_id=snapshot, received_at='2026-10-30T00:00:00+00:00')).encode())
        imported = self.conversation(b'imported', sid='imported')
        for key, status, superseded in (('b', 'complete', False), ('c', 'complete', True),
                                        ('d', 'importing', False)):
            entry = imported if key == 'b' else self.conversation(key.encode(), sid=key)
            journal = self.path / 'conversation-imports' / (key * 64)
            atomic_write(journal / 'receipt.json', json.dumps(dict(
                status=status, source_checkpoint=entry)).encode())
            if superseded:
                atomic_write(journal / 'superseded.json', b'{}')
        self.assertEqual(self.store.relay_entries(),
                         ([dict(received, relayed=True), dict(imported, relayed=True)], []))
        marker = self.path / 'checkpoints/relay/seeded'
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        late = self.conversation(b'late journal', sid='late')
        atomic_write(self.path / 'conversation-imports' / ('e' * 64) / 'receipt.json',
                     json.dumps(dict(status='complete', source_checkpoint=late)).encode())
        self.assertEqual(self.store.relay_entries(),
                         ([dict(received, relayed=True), dict(imported, relayed=True)], []))

    def test_unreadable_record_is_an_issue_not_a_failure(self):
        missing = self.conversation(b'missing', sid='missing')
        corrupt = self.conversation(b'corrupt dependency', sid='corrupt')
        corrupt['files'][0]['blob'] = self.store.put(b'unique dependency')
        healthy = self.conversation(b'healthy', sid='healthy')
        self.receive(missing, corrupt, healthy)
        (self.path / 'checkpoints/objects' / missing['files'][1]['blob']).unlink()
        path = self.path / 'checkpoints/objects' / corrupt['files'][0]['blob']
        path.chmod(0o600)
        path.write_bytes(b'corrupt')
        self.assertEqual(self.store.relay_entries(), ([dict(healthy, relayed=True)], [
            dict(agent='claude', session_id=sid, reason='Relayed conversation checkpoint is unreadable')
            for sid in ('missing', 'corrupt')]))

    def test_records_survive_an_interrupted_receipt_and_retry(self):
        first, second = self.conversation(b'first', sid='first'), self.conversation(b'second', sid='second')
        self.receive(first)
        previous = self.store.latest('codespace')
        snapshot = self.store.save(dict(conversations=dict(entries=[second])))
        real = checkpoint_store.atomic_write
        def interrupted(path, data, *args, **kwargs):
            if path.name == 'codespace.json':
                raise OSError('interrupted receipt')
            return real(path, data, *args, **kwargs)
        with mock.patch.object(checkpoint_store, 'atomic_write', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'interrupted receipt'):
                self.store.acknowledge(snapshot, source='codespace')
        self.assertEqual(self.store.latest('codespace'), previous)
        self.assertEqual(self.store.relay_entries(),
                         ([dict(first, relayed=True), dict(second, relayed=True)], []))
        self.store.acknowledge(snapshot, source='codespace')
        self.assertEqual(len(self.relay_records()), 2)

    def test_first_acknowledgement_seeds_the_receipt_before_replacing_it(self):
        old = self.conversation(b'before upgrade')
        snapshot = self.store.save(dict(conversations=dict(entries=[old])))
        atomic_write(self.path / 'checkpoints/receipts/codespace.json', json.dumps(dict(
            snapshot_id=snapshot, received_at='2026-10-30T00:00:00+00:00')).encode())
        self.receive()
        self.assertEqual(self.store.relay_entries(), ([dict(old, relayed=True)], []))

    def test_matching_session_ids_from_different_agents_are_both_kept(self):
        entries = [self.conversation(b'AB'), self.conversation(b'A', agent='codex')]
        for entry in entries:
            self.receive(entry)
        self.assertEqual(len(self.relay_records()), 2)
        self.assertEqual(self.store.relay_entries(), ([dict(entry, relayed=True) for entry in entries], []))

    def test_unreadable_head_cannot_prove_retirement(self):
        earlier, later = self.conversation(b'A'), self.conversation(b'AB')
        self.receive(earlier)
        (self.path / 'checkpoints/objects' / earlier['files'][1]['blob']).unlink()
        self.receive(later)
        self.assertEqual(len(self.relay_records()), 2)
        self.assertEqual(self.store.relay_entries(), ([dict(later, relayed=True)], [dict(
            agent='claude', session_id='session', reason='Relayed conversation checkpoint is unreadable')]))

    def test_unreadable_seed_sources_never_block_acknowledge(self):
        base = self.path
        cases = [
            ('missing_manifest', json.dumps(dict(snapshot_id='e' * 64)).encode(), b'{'),
            ('corrupt_receipt', b'{', b'[]'),
            ('missing_receipt_id', b'{}', b'null'),
            ('invalid_receipt_id', b'{"snapshot_id": []}', b'{'),
            ('corrupt_marker', json.dumps(dict(snapshot_id='e' * 64)).encode(), b'{'),
        ]
        for name, receipt, journal in cases:
            with self.subTest(source=name):
                self.path = base / name
                self.store = checkpoint_store.Store(self.path)
                atomic_write(self.path / 'checkpoints/receipts/codespace.json', receipt)
                if name == 'corrupt_marker':
                    atomic_write(self.path / 'checkpoints/relay/seeded', b'{')
                atomic_write(self.path / 'conversation-imports' / ('b' * 64) / 'receipt.json', journal)
                seeded = self.conversation(b'valid journal', sid='seeded')
                atomic_write(self.path / 'conversation-imports' / ('c' * 64) / 'receipt.json',
                             json.dumps(dict(status='complete', source_checkpoint=seeded)).encode())
                new = self.conversation(b'new receipt', sid='new')
                snapshot = self.receive(new)
                self.assertEqual(self.store.latest('codespace')['snapshot_id'], snapshot)
                self.assertEqual(self.store.relay_entries(),
                                 ([dict(seeded, relayed=True), dict(new, relayed=True)], []))
                self.assertEqual(stat.S_IMODE((self.path / 'checkpoints/relay/seeded').stat().st_mode), 0o600)

    def test_corrupt_record_file_is_an_issue_and_acknowledge_still_works(self):
        base = self.path
        for index, corrupt in enumerate((b'{', b'{}', b'{"schema": 1, "entry": [], "received_at": null}',
                                         b'{"schema": 1, "entry": {}, "received_at": 0}',
                                         b'{"schema": 1, "entry": {}, "received_at": "2026-11-02T00:00:00+00:00"}')):
            with self.subTest(record=corrupt):
                self.path = base / str(index)
                self.store = checkpoint_store.Store(self.path)
                healthy = self.conversation(b'healthy', sid='healthy')
                self.receive(healthy)
                digest = hashlib.sha256(b'{}').hexdigest() if index == 3 else 'b' * 64
                path = self.path / 'checkpoints/relay' / (digest + '.json')
                atomic_write(path, corrupt)
                issue = dict(agent=None, session_id=None,
                             reason='Relayed conversation checkpoint is unreadable')
                self.assertEqual(self.store.relay_entries(), ([dict(healthy, relayed=True)], [issue]))
                new = self.conversation(b'new', sid='new')
                snapshot = self.receive(new)
                self.assertEqual(self.store.latest('codespace')['snapshot_id'], snapshot)
                self.assertEqual(self.store.relay_entries(),
                                 ([dict(healthy, relayed=True), dict(new, relayed=True)], [issue]))
                self.assertEqual(path.read_bytes(), corrupt)
                # Collection cannot safely remove objects when a root is unreadable.
                loose = self.store.put(b'loose')
                self.age('objects', loose, checkpoint_store.KEEP_DAYS + 2)
                with self.assertRaises((OSError, ValueError, KeyError, TypeError)):
                    self.store.collect(apply=True)
                self.assertEqual(self.store.read(loose), b'loose')

    def test_identical_record_retry_does_not_read_heads_again(self):
        entry = self.conversation(b'head of an unchanged conversation')
        snapshot = self.receive(entry)
        record = self.relay_records()[0]
        original = record.read_bytes()
        with mock.patch.object(self.store, 'read', wraps=self.store.read) as read:
            self.store.acknowledge(snapshot, source='codespace')
        # Acknowledgement still verifies each unique manifest blob once.
        self.assertEqual(read.call_count, len({item['blob'] for item in entry['files']}))
        self.assertEqual(record.read_bytes(), original)

    def test_collect_keeps_named_and_recent_manifests_and_what_they_reference(self):
        old, recent, named, loose, fresh = (self.store.put(name.encode()) for name in ('old', 'recent', 'named', 'loose', 'fresh'))
        stale = self.store.save({'files': [{'blob': old}]})
        kept = self.store.save({'files': [{'blob': recent}]})
        received = self.store.save({'files': [{'blob': named}]})
        self.store.acknowledge(received, source='codespace')
        for family, digest in (('manifests', stale), ('manifests', received), ('objects', old),
                               ('objects', named), ('objects', loose)):
            self.age(family, digest, 8)
        with mock.patch.object(self.store, 'read', side_effect=AssertionError('collect reads no blob bodies')):
            planned = self.store.collect()
        self.assertEqual((planned['manifests'], planned['objects'], planned['applied']), (1, 2, False))
        self.assertEqual(self.store.read(old), b'old')  # A dry run deletes nothing.
        done = self.store.collect(apply=True)
        self.assertEqual((done['manifests'], done['objects'], done['bytes']), (1, 2, len(b'old') + len(b'loose')))
        for digest in (old, loose):
            with self.assertRaises(FileNotFoundError):
                self.store.read(digest)
        with self.assertRaises(FileNotFoundError):
            self.store.load(stale)
        for digest, body in ((recent, b'recent'), (named, b'named'), (fresh, b'fresh')):
            self.assertEqual(self.store.read(digest), body)
        self.assertEqual(self.store.load(kept), {'files': [{'blob': recent}]})
        self.assertEqual(self.store.latest('codespace')['snapshot_id'], received)

    def test_putting_existing_content_again_restarts_its_grace(self):
        # A capture re-puts unchanged sessions before it saves their manifest.
        digest = self.store.put(b'unchanged session')
        self.age('objects', digest, 8)
        self.store.put(b'unchanged session')
        self.assertEqual(self.store.collect(apply=True)['objects'], 0)
        self.assertEqual(self.store.read(digest), b'unchanged session')

    def test_finding_content_present_restarts_its_grace(self):
        # A transfer skips blobs the destination has, then saves a manifest naming them.
        digest = self.store.put(b'already there')
        self.age('objects', digest, 8)
        self.assertEqual(self.store.present(digest), len(b'already there'))
        self.assertIsNone(self.store.present('c' * 64))
        self.assertEqual(self.store.collect(apply=True)['objects'], 0)

    def test_collect_spares_blobs_an_import_receipt_names(self):
        source = self.store.put(b'published source')
        self.age('objects', source, 8)
        from devcontainer_setup.project_sync.state import atomic_write
        atomic_write(self.path / 'conversation-imports' / ('b' * 64) / 'receipt.json',
                     json.dumps({'files': [{'sha256': source, 'destination': '/x'}]}).encode())
        self.assertEqual(self.store.collect(apply=True)['objects'], 0)
        self.assertEqual(self.store.read(source), b'published source')

    def test_collect_deletes_nothing_when_a_kept_manifest_cannot_be_read(self):
        body = self.store.put(b'body')
        loose = self.store.put(b'loose')
        self.age('objects', loose, 8)
        snapshot = self.store.save({'files': [{'blob': body}]})
        path = self.path / 'checkpoints/manifests' / snapshot
        path.chmod(0o600)
        path.write_bytes(b'{"corrupt": true}')
        with self.assertRaises(ValueError):
            self.store.collect(apply=True)
        self.assertEqual(self.store.read(loose), b'loose')

    def test_content_addressed_private_immutable_deduplicated_objects(self):
        body = b'\x00private binary\xff'
        digest = self.store.put(body)
        self.assertEqual(digest, hashlib.sha256(body).hexdigest())
        objects = list((self.path / 'checkpoints/objects').rglob(digest))
        self.assertEqual(len(objects), 1)
        original = objects[0].stat()
        self.assertEqual(self.store.put(body), digest)
        self.assertEqual(objects[0].stat().st_ino, original.st_ino)
        self.assertEqual(stat.S_IMODE(original.st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(objects[0].parent.stat().st_mode), 0o700)
        self.assertEqual(self.store.read(digest), body)

    def test_manifest_is_canonical_and_save_does_not_acknowledge(self):
        digest = self.store.put(b'payload')
        manifest = {'z': [{'blob': digest}], 'a': {'nested': {'blob': digest}}}
        snapshot = self.store.save(manifest)
        canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
        self.assertEqual(snapshot, hashlib.sha256(canonical).hexdigest())
        self.assertEqual(self.store.save({'a': manifest['a'], 'z': manifest['z']}), snapshot)
        self.assertEqual(self.store.load(snapshot), manifest)
        self.assertIsNone(self.store.latest('codespace'))
        receipt = self.store.acknowledge(snapshot, source='codespace')
        self.assertEqual(receipt['snapshot_id'], snapshot)
        self.assertTrue(receipt['received_at'])
        self.assertEqual(self.store.latest('codespace'), receipt)
        self.assertIsNone(self.store.latest('local'))
        self.assertEqual(stat.S_IMODE((self.path / 'checkpoints/receipts/codespace.json').stat().st_mode), 0o600)

    def test_missing_nested_blob_refuses_save_and_receipt(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.store.save({'entries': [{'any': {'blob': 'b' * 64}}]})
        self.assertIsNone(self.store.latest('desktop'))

    def test_corrupt_truncated_blob_and_manifest_refuse_acknowledgement(self):
        digest = self.store.put(b'body')
        snapshot = self.store.save({'blob': digest})
        path = next((self.path / 'checkpoints/objects').rglob(digest))
        path.chmod(0o600)
        path.write_bytes(b'bod')
        with self.assertRaisesRegex(ValueError, 'hash|digest|corrupt'):
            self.store.read(digest)
        with self.assertRaises(ValueError):
            self.store.acknowledge(snapshot, source='local')
        self.assertIsNone(self.store.latest('local'))
        path.write_bytes(b'body')
        path.chmod(0o400)
        manifest_path = next((self.path / 'checkpoints/manifests').rglob(snapshot))
        manifest_path.chmod(0o600)
        manifest_path.write_bytes(b'{')
        with self.assertRaises(ValueError):
            self.store.load(snapshot)

    def test_failed_durable_receipt_leaves_previous_acknowledgement(self):
        first = self.store.save({'generation': 1})
        old = self.store.acknowledge(first, source='codespace')
        second = self.store.save({'generation': 2})
        real = checkpoint_store.atomic_write
        def interrupted(path, data, *args, **kwargs):
            if path.name == 'codespace.json':
                raise OSError('simulated interruption before replace')
            return real(path, data, *args, **kwargs)
        with mock.patch.object(checkpoint_store, 'atomic_write', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.acknowledge(second, source='codespace')
        self.assertEqual(self.store.latest('codespace'), old)

    def test_invalid_identifiers_and_sources_refuse_traversal(self):
        for digest in ('../escape', 'A' * 64, 'a' * 63, ''):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                self.store.read(digest)
        for source in ('../escape', 'remote', ''):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.store.latest(source)
        with self.assertRaises(ValueError):
            self.store.save({'blob': '../escape'})

    def test_store_symlink_or_hardlink_cannot_redirect_private_io(self):
        outside = Path(self.tmp.name).resolve() / 'outside'
        outside.mkdir()
        self.path.mkdir(parents=True)
        (self.path / 'checkpoints').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.store.put(b'never outside')
        self.assertEqual(list(outside.iterdir()), [])
        (self.path / 'checkpoints').unlink()
        digest = self.store.put(b'private')
        obj = next((self.path / 'checkpoints/objects').rglob(digest))
        os.link(obj, outside / 'linked')
        with self.assertRaises(ValueError):
            self.store.read(digest)

    def test_existing_corrupt_object_is_never_silently_repaired(self):
        digest = self.store.put(b'original')
        obj = next((self.path / 'checkpoints/objects').rglob(digest))
        obj.chmod(0o600)
        obj.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.store.put(b'original')
        self.assertEqual(obj.read_bytes(), b'corrupt')
