"""Replace readers use private local records, never a runtime control lock."""
import importlib
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest

from devcontainer_setup.project_sync.state import atomic_write, read_json


def journal(**updates):
    value = dict(schema=1, operation='012345abcdef', old='old-space', new='new-space', account='octo',
                 repository='example/project', branch='main', commit='a' * 40, remote_root='/workspaces/project',
                 resources=dict(machine='standard', cpus=4, idle_timeout_minutes=30, billable_owner='octo'),
                 old_receipt=None, branch_receipt=None, request=None, phase='ready', at={'ready': 1000.0},
                 signins=None, check=None, moving=None, ended=None, ended_at=None)
    value.update(updates)
    return value


class ReplacingTests(unittest.TestCase):
    def setUp(self):
        name = 'devcontainer_setup.project_sync.replacing'
        self.assertIsNotNone(importlib.util.find_spec(name), 'replace journal reader is missing')
        self.r = importlib.import_module(name)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state'

    def save(self, path, value):
        atomic_write(self.state / path, json.dumps(value).encode())

    def test_round_trip_and_malformed(self):
        self.assertIsNone(self.r.read(self.state))
        value = journal(request=dict(at=999.0, before=['old-space'], retries=[1000.0]),
                        signins=dict(retry=['codex'], at=1001.0), check={'ok': True})
        self.r.write(self.state, value)
        self.assertEqual(self.r.read(self.state), value)
        self.assertEqual(stat.S_IMODE((self.state / 'replace.json').stat().st_mode), 0o600)
        for changes in (dict(phase='bad'), dict(schema=True), dict(schema=2), dict(commit='bad'),
                        dict(operation='../outside'), dict(account='bad\nlogin'), dict(repository='bad'),
                        dict(new='../outside'), dict(remote_root='../outside'), dict(moving=True),
                        dict(at={'ready': float('nan')}), dict(request={'at': 1, 'before': [], 'retries': [True]}),
                        dict(resources=dict(value['resources'], cpus=True))):
            with self.subTest(changes=changes):
                bad = dict(value, **changes)
                with self.assertRaisesRegex(ValueError, '^The replace record cannot be read$'):
                    self.r.write(self.state, bad)
                self.save('replace.json', bad)
                with self.assertRaisesRegex(ValueError, '^The replace record cannot be read$'):
                    self.r.read(self.state)
        self.save('replace.json', [])
        with self.assertRaises(ValueError):
            self.r.read(self.state)

    def test_archive_is_idempotent_and_hides_the_active_copy(self):
        value = journal()
        self.r.write(self.state, value)
        path = self.r.archive(self.state, value, 'abandoned', 1002.0)
        saved = path.read_bytes()
        self.assertEqual(read_json(path), dict(value, ended='abandoned', ended_at=1002.0))
        self.assertIsNone(self.r.read(self.state))
        self.save('replace.json', value)  # Crash after archive write, before active unlink.
        self.assertIsNone(self.r.read(self.state))
        self.assertEqual(self.r.archive(self.state, value, 'abandoned', 1003.0), path)
        self.assertEqual(path.read_bytes(), saved)
        self.assertFalse((self.state / 'replace.json').exists())
        self.assertEqual(value, journal(), 'archiving must not mutate the caller')

    def test_ended_by_fact_follows_either_deletion_journal(self):
        value = journal()
        for name in ('old-space', 'new-space'):
            for phase, expected in (('confirmed', None), ('delete-requested', None), ('deleted', name), ('cleaned', name)):
                self.save('deletions/' + name + '.json', dict(phase=phase))
                self.assertEqual(self.r.ended_by_fact(self.state, value), expected)
            (self.state / 'deletions' / (name + '.json')).unlink()
        self.assertIsNone(self.r.ended_by_fact(self.state, value))

    def test_unchecked_owner_cleanup_is_not_a_deletion_fact(self):
        for phase in ('deleted', 'cleaned'):
            self.save('deletions/new-space.json', dict(phase=phase, owner_checked=False))
            self.assertIsNone(self.r.ended_by_fact(self.state, journal()))
        self.save('deletions/new-space.json', dict(phase='deleted', owner_checked=True))
        self.assertEqual(self.r.ended_by_fact(self.state, journal()), 'new-space')

    def test_move_started_needs_this_operations_seal_and_a_file_naming_new(self):
        value = journal()
        files = [('scratch.json', {'codespace': 'new-space'}),
                 ('scratch-retarget.json', {'candidate': {'codespace': 'new-space'}}),
                 ('recovery-move.json', {'new': 'new-space'}),
                 ('workspace-pending.json', {'codespace': 'new-space'})]
        for filename, body in files:
            with self.subTest(filename=filename):
                (self.state / 'retired/old-space.json').unlink(missing_ok=True)
                self.save(filename, body)
                self.assertFalse(self.r.move_started(self.state, value))
                self.save('retired/old-space.json', {'operation': 'fedcba543210'})
                self.assertFalse(self.r.move_started(self.state, value))
                self.save('retired/old-space.json', {'operation': value['operation']})
                self.assertTrue(self.r.move_started(self.state, value))
                (self.state / filename).unlink()
        self.assertFalse(self.r.move_started(self.state, value))
        self.assertTrue(self.r.move_started(self.state, dict(value, moving=1001.0)))
        self.assertFalse(self.r.move_started(self.state, dict(value, new=None)))

    def test_naming_new_survives_an_abandon_until_new_is_deleted(self):
        value = journal()
        self.r.write(self.state, value)
        self.assertEqual(self.r.naming_new(self.state, 'new-space'), value)
        self.assertIsNone(self.r.naming_new(self.state, 'unnamed'))
        self.r.archive(self.state, value, 'abandoned', 1001.0)
        self.assertEqual(self.r.naming_new(self.state, 'new-space')['ended'], 'abandoned')
        self.save('deletions/old-space.json', {'phase': 'deleted'})
        self.assertIsNotNone(self.r.naming_new(self.state, 'new-space'))
        newer = journal(operation='fedcba543210', at={'ready': 1002.0})
        self.r.write(self.state, newer)
        self.assertEqual(self.r.naming_new(self.state, 'new-space'), newer)
        self.save('deletions/new-space.json', {'phase': 'cleaned'})
        self.assertIsNone(self.r.naming_new(self.state, 'new-space'))

    def test_receipt_must_match_operation_and_step(self):
        operation = journal()['operation']
        for step in ('cutover', 'check'):
            path = self.r.receipt_path(self.state, operation, step)
            self.assertEqual(path, self.state / 'replace-receipts' / (operation + '-' + step + '.json'))
            self.assertIsNone(self.r.read_receipt(path, operation, step))
            value = dict(schema=1, operation=operation, step=step, ok=True)
            atomic_write(path, json.dumps(value).encode())
            self.assertEqual(self.r.read_receipt(path, operation, step), value)
            for changes in (dict(schema=True), dict(schema=2), dict(operation='fedcba543210'), dict(step='wrong')):
                atomic_write(path, json.dumps(dict(value, **changes)).encode())
                self.assertIsNone(self.r.read_receipt(path, operation, step))
            atomic_write(path, b'{')
            self.assertIsNone(self.r.read_receipt(path, operation, step))
        for operation, step in (('../outside', 'check'), (operation, '../outside')):
            with self.assertRaises(ValueError):
                self.r.receipt_path(self.state, operation, step)
