"""Durable content validation and acknowledgement ordering."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

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
