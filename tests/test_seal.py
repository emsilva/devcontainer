"""Final-flush records are private attestations of a verified captured prefix."""
from dataclasses import replace
import importlib
import json
from pathlib import Path
import stat
import tempfile
import unittest

from devcontainer_setup.project_sync import target_scan, verdict
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.state import atomic_write
from tests.test_verdict import DIGEST, IDENTITY, NAME, NOW, example_scan, manifest


class SealTests(unittest.TestCase):
    def setUp(self):
        self.seal = importlib.import_module('devcontainer_setup.project_sync.seal')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state'
        self.store = Store(self.state)
        self.manifest = manifest(self.store)
        self.snapshot = self.store.save(self.manifest)
        self.options = dict(identity=IDENTITY, repository='example/project', gh_user='octo',
                            remote_root='/workspaces/project', operation='replace-1', cycle_id='cycle',
                            snapshot_id=self.snapshot, now=NOW)

    def write(self, scan=None, **options):
        scan = scan or example_scan()
        arguments = dict(self.options, evidence=self.evidence(scan))
        arguments.update(options)
        return self.seal.write(self.state, self.store, scan, **arguments)

    def evidence(self, base_scan=None, **updates):
        scan = base_scan or example_scan()
        return replace(verdict.Evidence(scan, True, (), scan.scratch, (), True, None, None, (), None, ()), **updates)

    def test_scratch_must_have_reached_the_mac_before_sealing(self):
        scan = example_scan()
        for changes in (dict(sync_running=False), dict(conflicts=('note.txt',)), dict(mac_scratch=None),
                        dict(mac_scratch=()), dict(mac_scratch=(('note.txt', 'c' * 64),)),
                        dict(scan=replace(scan, at=scan.at + 1))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.write(scan, evidence=self.evidence(scan, **changes))
        self.assertFalse((self.state / 'retired' / 'space.json').exists())

    def test_evidence_is_required_for_a_final_flush(self):
        with self.assertRaises(TypeError):
            self.seal.write(self.state, self.store, example_scan(), **self.options)

    def test_write_then_read_round_trips(self):
        path = self.write()
        self.assertEqual(path, self.state / 'retired' / 'space.json')
        record = self.seal.read(self.state, 'space')
        self.assertEqual(record.snapshot_id, self.snapshot)
        self.assertEqual(record.sessions, example_scan().sessions)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.write(operation='replace-2', now=NOW + 1)
        self.assertEqual(self.seal.read(self.state, 'space').operation, 'replace-2')
        self.assertIsNone(self.seal.read(self.state, 'missing'))
        self.write(replace(example_scan(), codespace='older'), now=NOW - 1)
        self.assertEqual(self.seal.names(self.state), ('older', 'space'))

    def test_incomplete_scan_tails_or_bad_snapshot_is_refused(self):
        for flag in ('git_complete', 'scratch_complete', 'sessions_complete', 'agents_complete'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                self.write(replace(example_scan(), **{flag: False}))
        for field in ('tails', 'mismatched'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.write(replace(example_scan(), **{field: (('codex', NAME),)}))
        with self.assertRaises(ValueError):
            self.write(snapshot_id='c' * 64)
        data = manifest(self.store, identity='github.com/other/project')
        with self.assertRaises(ValueError):
            self.write(snapshot_id=self.store.save(data))
        self.manifest['conversations']['issues'] = [dict(agent='codex', reason='unreadable')]
        with self.assertRaises(ValueError):
            self.write(snapshot_id=self.store.save(self.manifest))
        blob = self.store.root / 'objects' / DIGEST
        blob.unlink()
        with self.assertRaises(ValueError):
            self.write()
        self.assertFalse((self.state / 'retired' / 'space.json').exists())

    def test_a_scan_not_bound_to_the_snapshot_is_refused(self):
        unrelated = self.store.save(manifest(self.store, b'different\n'))
        with self.assertRaises(ValueError):
            self.write(snapshot_id=unrelated)
        for changes in (dict(cutoffs_sha256=None), dict(checked=()),
                        dict(checked=(('codex', NAME), ('codex', 'missing.jsonl')))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.write(replace(example_scan(), **changes))
        # Archived and ordinary files use identical agent/basename comparison keys.
        scan = replace(example_scan(), sessions=(('codex/archived_sessions/' + NAME, DIGEST),))
        self.write(scan)
        self.assertEqual(self.seal.read(self.state, 'space').sessions, scan.sessions)
        wrong_size = json.loads(json.dumps(self.manifest))
        wrong_size['conversations']['entries'][0]['files'][0]['size'] += 1
        cutoffs = {('codex', NAME): target_scan.Cutoff(11, DIGEST, True)}
        with self.assertRaises(ValueError):
            self.write(replace(scan, cutoffs_sha256=target_scan.cutoffs_digest(cutoffs)),
                       snapshot_id=self.store.save(wrong_size))
        malformed = manifest(self.store, b'different\n')
        malformed['conversations']['entries'][0]['files'][0]['sha256'] = DIGEST
        snapshot = self.store.save(malformed)
        (self.store.root / 'objects' / DIGEST).unlink()
        with self.assertRaises(ValueError):
            self.write(snapshot_id=snapshot)

    def test_malformed_or_foreign_seal_is_an_error_not_absent(self):
        path = self.write()
        good = json.loads(path.read_bytes())
        for changed in (dict(schema=2), dict(codespace='other'), dict(identity='github.com/other/project'),
                        dict(snapshot_id='bad'), dict(remote_root='../outside'), dict(sealed_at=float('nan')),
                        dict(sealed_at=1e100),
                        dict(scratch=[['../outside', DIGEST]]), dict(extra=True)):
            atomic_write(path, json.dumps(dict(good, **changed)).encode())
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'final-flush record for space cannot be read'):
                self.seal.read(self.state, 'space')
            self.assertEqual(self.seal.names(self.state), ())
        atomic_write(path, json.dumps(good).encode())
        atomic_write(self.state / 'workspace.json', json.dumps(dict(identity='github.com/other/project')).encode())
        with self.assertRaises(ValueError):
            self.seal.read(self.state, 'space')
