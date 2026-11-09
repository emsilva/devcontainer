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


class CodespaceStopWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.base = self.home / '.local/state/devcontainer-links'
        self.base.mkdir(parents=True)
        self.clock = 2000.0
        self.hook = lambda: None

    def capable(self):
        path = self.base / 'stop-listening-v1'
        path.touch()
        os.utime(path, (self.clock, self.clock))

    def request(self):
        files = list((self.base / 'stops').glob('*.json'))
        self.assertEqual(len(files), 1)
        return files[0]

    def sleep(self, seconds):
        self.assertEqual(seconds, 0.5)
        self.clock += seconds
        self.hook()

    def run_writer(self):
        from devcontainer_setup.project_sync import power
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = power.request_from_codespace(home=self.home, env={'CODESPACE_NAME': 'fixture'},
                                                  now=lambda: self.clock, sleep=self.sleep, wait=1)
        return result, output.getvalue().strip()

    def test_writer_requires_the_stop_capability(self):
        (self.base / 'mac-listening').touch()
        for age in (None, 16):
            with self.subTest(age=age):
                if age is not None:
                    self.capable()
                    os.utime(self.base / 'stop-listening-v1', (self.clock - age,) * 2)
                result, text = self.run_writer()
                self.assertEqual(result, 1)
                self.assertIn('No Mac worker is connected to this Codespace, so nothing stops.', text)
                self.assertFalse((self.base / 'stops').exists())

    def answer(self, ok=True):
        path = self.request()
        path.unlink()  # The watcher claimed it before answering.
        atomic_write(path.with_suffix('.result'), json.dumps({'ok': ok, 'message': 'Recorded.'}).encode())
        self.hook = lambda: None

    def test_answer_is_printed_and_removed(self):
        self.capable()
        self.hook = self.answer
        self.assertEqual(self.run_writer(), (0, 'Your Mac recorded this as your stop. Recorded.'))
        self.assertEqual(list((self.base / 'stops').iterdir()), [])

    def test_refused_answer_is_printed_without_success_prefix(self):
        self.capable()
        self.hook = lambda: self.answer(False)
        self.assertEqual(self.run_writer(), (1, 'Recorded.'))
        self.assertEqual(list((self.base / 'stops').iterdir()), [])

    def test_unclaimed_request_is_removed_at_the_deadline(self):
        self.capable()
        result, text = self.run_writer()
        self.assertEqual(result, 1)
        self.assertIn('within 60 seconds, so nothing stops', text)
        self.assertEqual(list((self.base / 'stops').iterdir()), [])

    def test_claimed_without_answer_says_so(self):
        self.capable()
        def claim():
            self.request().rename(self.base / 'claimed.json')
            self.hook = lambda: None
        self.hook = claim
        result, text = self.run_writer()
        self.assertEqual(result, 1)
        self.assertIn('took the stop request but has not answered', text)
        self.assertNotIn('nothing stops', text)

    def test_cancel_races_the_claim_in_both_orders(self):
        self.capable()
        for claimed in (False, True):
            with self.subTest(claimed=claimed):
                def cancel():
                    if claimed:
                        self.request().rename(self.base / 'claimed.json')
                    raise KeyboardInterrupt
                self.hook = cancel
                result, text = self.run_writer()
                self.assertEqual(result, 130)
                self.assertEqual(text, 'Cancelled, but your Mac already took the stop request.' if claimed
                                 else 'Cancelled; nothing stops.')
                self.assertEqual(list((self.base / 'stops').iterdir()), [])

    def test_deadline_unlink_error_is_uncertain(self):
        self.capable()
        unlink = Path.unlink
        def denied(path, *args, **kwargs):
            if path.parent == self.base / 'stops' and path.suffix == '.json':
                raise PermissionError('fixture unlink denied')
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', denied):
            result, text = self.run_writer()
        self.assertEqual(result, 1)
        self.assertIn('took the stop request but has not answered', text)
        self.assertNotIn('nothing stops', text)

    def test_request_file_shape(self):
        self.capable()
        def inspect():
            path = self.request()
            value = json.loads(path.read_bytes())
            self.assertEqual(set(value), {'schema', 'kind', 'id', 'created_at', 'codespace'})
            self.assertEqual(value, dict(schema=1, kind='stop', id=path.stem, created_at=2000.0,
                                         codespace='fixture'))
            self.assertRegex(path.name, r'^[0-9]+-[0-9]+-[0-9a-f]{8}\.json$')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.answer()
        self.hook = inspect
        self.assertEqual(self.run_writer()[0], 0)


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
                         'Stopping old-cs; it stays stopped until task work:cs starts it again.')
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
