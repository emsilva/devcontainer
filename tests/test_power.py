"""Headless owner stops select and validate their target under the workspace lock."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from devcontainer_setup.project_sync import arming, lifecycle
from devcontainer_setup.project_sync.state import atomic_write, locked


class PowerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state'
        self.clock = 2000.0
        self.fake = mock.Mock()
        self.fake.post_stop.return_value = 202
        self.bind('old-cs')
        self.environment = mock.patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ('CODESPACES', 'GH_TOKEN', 'GITHUB_TOKEN'):
            os.environ.pop(name, None)

    def bind(self, name):
        atomic_write(self.state / 'scratch.json', json.dumps({'codespace': name}).encode())
        arming.write(self.state, {'schema': 1, 'armed': True, 'codespace': name, 'gh_user': 'octo'})

    def race(self, *, expected, api=None):
        from devcontainer_setup.project_sync import power
        attempted = threading.Event()
        self.error, self.result = None, None

        @contextlib.contextmanager
        def observed_lock(*args, **kwargs):
            attempted.set()
            with locked(*args, **kwargs) as fd:
                yield fd

        def stop():
            try:
                self.result = power.stop(self.state, expected=expected,
                                         until=self.clock + 60 if expected else None,
                                         api=api, now=lambda: self.clock)
            except Exception as error:
                self.error = error

        thread = threading.Thread(target=stop)
        with mock.patch.object(power, 'locked', side_effect=observed_lock):
            try:
                with locked(self.state / 'workspace-control.lock', blocking=True):
                    thread.start()
                    self.assertTrue(attempted.wait(5), 'stop never tried the workspace lock')
                    self.bind('new-cs')
            finally:
                thread.join(5)
        self.assertFalse(thread.is_alive(), 'stop thread did not finish')

    def test_stop_revalidates_the_target_under_the_lock(self):
        self.race(expected='old-cs', api=self.fake)
        self.assertIsInstance(self.error, ValueError)
        self.assertIn('names old-cs, but this project is paired with new-cs', str(self.error))
        self.assertNotIn('owner_stop_at', arming.read(self.state))
        self.fake.post_stop.assert_not_called()

    def test_cli_stop_stops_what_is_paired_when_it_gets_the_lock(self):
        with mock.patch.object(lifecycle, 'LifecycleAPI', return_value=self.fake) as made:
            self.race(expected=None)
        self.assertIsNone(self.error)
        made.assert_called_once_with('new-cs', 'octo')
        self.assertEqual(self.result.codespace, 'new-cs')
        self.assertTrue(self.result.armed)
        self.assertEqual(arming.read(self.state)['codespace'], 'new-cs')
        self.assertEqual(arming.read(self.state)['owner_stop_at'], self.clock)
        self.fake.post_stop.assert_called_once_with()

    def test_owner_stop_is_written_before_the_post(self):
        from devcontainer_setup.project_sync import power

        def post():
            self.assertEqual(arming.read(self.state)['owner_stop_at'], self.clock)
            # The POST is outside the workspace lock.
            with locked(self.state / 'workspace-control.lock'):
                pass
            return 202

        self.fake.post_stop.side_effect = post
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = power.stop(self.state, api=self.fake, now=lambda: self.clock)
        self.assertEqual(out.getvalue(), '')
        self.assertIs(result.api, self.fake)
        self.assertEqual(result.stopped_at, self.clock)
        self.assertEqual(result.message,
                         'Stopping old-cs; it stays stopped until task work:cs:armed starts it again.')
        with self.assertRaises(AttributeError):
            result.codespace = 'new-cs'

    def test_stop_expires_while_waiting_for_the_lock(self):
        from devcontainer_setup.project_sync import power
        before = (self.state / arming.ARMING).read_bytes()
        sleeps = []

        def advance(seconds):
            sleeps.append(seconds)
            self.clock += seconds

        with locked(self.state / 'workspace-control.lock', blocking=True):
            with self.assertRaisesRegex(ValueError, 'The stop request expired before it could run; nothing stopped'):
                power.stop(self.state, until=self.clock + 0.4, api=self.fake,
                           now=lambda: self.clock, sleep=advance)
        self.assertEqual(sleeps, [0.2, 0.2])
        self.assertEqual((self.state / arming.ARMING).read_bytes(), before)
        self.fake.post_stop.assert_not_called()

    def test_stop_checks_expiry_again_after_taking_the_lock(self):
        from devcontainer_setup.project_sync import power
        before = (self.state / arming.ARMING).read_bytes()
        deadline = self.clock + 60

        @contextlib.contextmanager
        def delayed_lock(*args, **kwargs):
            with locked(*args, **kwargs) as fd:
                self.clock = deadline
                yield fd

        with mock.patch.object(power, 'locked', side_effect=delayed_lock):
            with self.assertRaisesRegex(ValueError, 'The stop request expired before it could run; nothing stopped'):
                power.stop(self.state, until=deadline, api=self.fake, now=lambda: self.clock)
        self.assertEqual((self.state / arming.ARMING).read_bytes(), before)
        self.fake.post_stop.assert_not_called()

    def test_shutdown_cancels_a_lock_waiter(self):
        from devcontainer_setup.project_sync import power
        halt = threading.Event()
        before = (self.state / arming.ARMING).read_bytes()

        def sleep(seconds):
            self.assertEqual(seconds, 0.2)
            halt.set()

        with locked(self.state / 'workspace-control.lock', blocking=True):
            with self.assertRaisesRegex(ValueError, 'The Mac worker is shutting down; nothing stopped'):
                power.stop(self.state, until=self.clock + 60, halted=halt.is_set,
                           api=self.fake, now=lambda: self.clock, sleep=sleep)
        self.assertEqual((self.state / arming.ARMING).read_bytes(), before)
        self.fake.post_stop.assert_not_called()

    def test_shutdown_after_lock_acquisition_does_not_stop(self):
        from devcontainer_setup.project_sync import power
        halt = threading.Event()

        @contextlib.contextmanager
        def halting_lock(*args, **kwargs):
            with locked(*args, **kwargs) as fd:
                halt.set()
                yield fd

        with mock.patch.object(power, 'locked', side_effect=halting_lock):
            with self.assertRaisesRegex(ValueError, 'The Mac worker is shutting down; nothing stopped'):
                power.stop(self.state, until=self.clock + 60, halted=halt.is_set,
                           api=self.fake, now=lambda: self.clock)
        self.assertNotIn('owner_stop_at', arming.read(self.state))
        self.fake.post_stop.assert_not_called()

    def test_mismatched_arming_is_refused_without_a_write_or_post(self):
        from devcontainer_setup.project_sync import power
        arming.write(self.state, {'schema': 1, 'armed': True, 'codespace': 'other-cs'})
        before = (self.state / arming.ARMING).read_bytes()
        with self.assertRaisesRegex(ValueError, 'The paired Codespace is not the armed one'):
            power.stop(self.state, api=self.fake)
        self.assertEqual((self.state / arming.ARMING).read_bytes(), before)
        self.fake.post_stop.assert_not_called()
