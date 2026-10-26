"""This package's own record of each Codespace start (#61)."""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from devcontainer_setup.project_sync import evidence, lifecycle


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state'
        self.state.mkdir()
        self.now = 1_700_000_000.0

    def saved(self):
        return json.loads((self.state / evidence.FILE).read_text())

    def test_a_recorded_start_is_usable_inside_the_window(self):
        # #61: a start inside [reading - 12h - window, reading] is this run's evidence.
        evidence.record_start(self.state, 'cs-1', self.now - 60, 'started')
        self.assertEqual(evidence.usable_start(self.state, 'cs-1', self.now), self.now - 60)
        saved = self.saved()['cs-1']
        self.assertEqual(saved, {'started_at': self.now - 60, 'source': 'started', 'stopped_at': None})
        # The lower edge is inclusive; a start after the reading is not.
        edge = self.now - lifecycle.LIFETIME - lifecycle.WINDOW
        evidence.record_start(self.state, 'cs-1', edge, 'created')
        self.assertEqual(evidence.usable_start(self.state, 'cs-1', self.now), edge)
        evidence.record_start(self.state, 'cs-1', self.now + 1, 'started')
        self.assertIsNone(evidence.usable_start(self.state, 'cs-1', self.now))

    def test_a_start_older_than_twelve_hours_and_the_window_is_not_usable(self):
        # #61: a start before reading - 12h - window belongs to a run GitHub has already ended.
        started = self.now - lifecycle.LIFETIME - lifecycle.WINDOW - 1
        evidence.record_start(self.state, 'cs-1', started, 'started')
        self.assertIsNone(evidence.usable_start(self.state, 'cs-1', self.now))

    def test_a_stop_seen_after_the_start_makes_it_unusable(self):
        # #61: a start recorded before a seen stop is never used for a later run.
        self.assertIsNone(evidence.record_stop(self.state, 'cs-1', self.now))
        self.assertFalse((self.state / evidence.FILE).exists())
        evidence.record_start(self.state, 'cs-1', self.now - 100, 'started')
        # A stop before the start is an older run; it does not expire this one.
        evidence.record_stop(self.state, 'cs-1', self.now - 200)
        self.assertIsNone(self.saved()['cs-1']['stopped_at'])
        self.assertEqual(evidence.usable_start(self.state, 'cs-1', self.now), self.now - 100)
        evidence.record_stop(self.state, 'cs-1', self.now - 10)
        evidence.record_stop(self.state, 'cs-1', self.now)  # a later sighting does not move the first-seen time
        self.assertEqual(self.saved()['cs-1']['stopped_at'], self.now - 10)
        self.assertIsNone(evidence.usable_start(self.state, 'cs-1', self.now))

    def test_a_newer_start_after_a_stop_is_usable_again(self):
        # #61: a new start replaces that Codespace's record and clears the stop.
        evidence.record_start(self.state, 'cs-1', self.now - 100, 'started')
        evidence.record_stop(self.state, 'cs-1', self.now - 40)
        evidence.record_start(self.state, 'cs-1', self.now - 10, 'keeper')
        self.assertEqual(evidence.usable_start(self.state, 'cs-1', self.now), self.now - 10)
        saved = self.saved()['cs-1']
        self.assertEqual(saved['source'], 'keeper')
        self.assertIsNone(saved['stopped_at'])

    def test_another_codespaces_record_is_ignored(self):
        # #61, Review Focus 1: a record for another Codespace is no evidence.
        evidence.record_start(self.state, 'cs-2', self.now - 30, 'started')
        self.assertIsNone(evidence.usable_start(self.state, 'cs-1', self.now))
        evidence.record_start(self.state, 'cs-1', self.now - 20, 'created')
        self.assertEqual(self.saved()['cs-2']['started_at'], self.now - 30)
        self.assertEqual(evidence.usable_start(self.state, 'cs-1', self.now), self.now - 20)

    def test_a_corrupt_evidence_file_reads_as_no_evidence(self):
        # #61, Review Focus 1: corrupt evidence is no evidence, and the next start rewrites a valid file.
        path = self.state / evidence.FILE
        path.write_bytes(b'{not json')
        self.assertIsNone(evidence.usable_start(self.state, 'cs-1', self.now))
        evidence.record_start(self.state, 'cs-1', self.now, 'started')
        self.assertEqual(self.saved()['cs-1'], {'started_at': self.now, 'source': 'started', 'stopped_at': None})
        self.assertEqual(evidence.usable_start(self.state, 'cs-1', self.now), self.now)

    def test_a_write_error_is_logged_and_never_raised(self):
        # #61, Review Focus 2: a state path the write can't reach is logged and never raised.
        blocked = Path(self.temp.name).resolve() / 'not-a-dir'
        blocked.write_text('owner data')
        with self.assertLogs(evidence._LOG, logging.INFO) as logs:
            self.assertIsNone(evidence.record_start(blocked, 'cs-1', self.now, 'started'))
            self.assertIsNone(evidence.record_stop(blocked, 'cs-1', self.now))
        self.assertTrue(any('Start evidence was not written' in line for line in logs.output))
        self.assertNotIn('owner data', '\n'.join(logs.output))
        self.assertEqual(blocked.read_text(), 'owner data')

    def test_the_file_is_private(self):
        # #61: start-evidence.json is private like the other state files.
        evidence.record_start(self.state, 'cs-1', self.now, 'started')
        self.assertEqual((self.state / evidence.FILE).stat().st_mode & 0o777, 0o600)
