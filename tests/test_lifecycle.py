import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devcontainer_setup.project_sync import lifecycle
from devcontainer_setup.project_sync.lifecycle import Reading

T0 = 1_000_000.0


def reading(state, pending=False, ok=True):
    return Reading(T0, ok, state, pending, None)


class Clock:
    def __init__(self):
        self.t = T0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeClient:
    """Replays readings (the last repeats) and POST statuses (the last repeats)."""

    def __init__(self, clock, readings, statuses=(200,)):
        self.clock, self.readings, self.statuses = clock, list(readings), list(statuses)
        self.post_times, self.reads = [], 0

    def read(self):
        row = self.readings[min(self.reads, len(self.readings) - 1)]
        self.reads += 1
        return row

    def post_start(self):
        self.post_times.append(self.clock.t)
        return self.statuses[min(len(self.post_times) - 1, len(self.statuses) - 1)]

    @property
    def posts(self):
        return len(self.post_times)


class StartTests(unittest.TestCase):
    def run_start(self, readings, statuses=(200,), **kwargs):
        clock = Clock()
        client = FakeClient(clock, readings, statuses)
        result = lifecycle.start(client, now=clock.now, sleep=clock.sleep, **kwargs)
        return client, clock, result

    def test_parse_time_accepts_z_and_offsets(self):
        self.assertEqual(lifecycle.parse_time('2026-10-21T14:34:47Z'),
                         lifecycle.parse_time('2026-10-21T11:34:47-03:00'))
        self.assertIsNone(lifecycle.parse_time(None))
        self.assertIsNone(lifecycle.parse_time('not a time'))

    def test_new_rid_is_utc_clock_time(self):
        self.assertEqual(lifecycle.new_rid(1_792_593_287.0), 'r-143447Z')

    def test_start_waits_for_shutdown_before_posting(self):
        states = ([reading('ShuttingDown')] * 3 + [reading('Shutdown', True), reading('Shutdown'),
                  reading('Queued'), reading('Available')])
        client, clock, result = self.run_start(states)
        self.assertEqual(client.posts, 1)
        self.assertEqual(result['outcome'], 'available')
        self.assertEqual(result['posted_at'], client.post_times[0])
        self.assertEqual(client.post_times[0], T0 + 40)  # reads 1-5, 10 s apart

    def test_already_available_never_posts(self):
        client, _, result = self.run_start([reading('Available')])
        self.assertEqual((client.posts, result['outcome'], result['posted_at']), (0, 'available', None))

    def test_409_waits_for_shutdown_and_posts_again(self):
        states = [reading('Shutdown'), reading('ShuttingDown'), reading('Shutdown'), reading('Available')]
        client, _, result = self.run_start(states, (409, 200))
        self.assertEqual(client.posts, 2)
        self.assertEqual(result['outcome'], 'available')

    def test_still_shutdown_two_minutes_after_accepted_post_reposts_once(self):
        client, clock, result = self.run_start([reading('Shutdown')] * 14 + [reading('Available')])
        self.assertEqual(client.posts, 2)
        self.assertGreaterEqual(client.post_times[1] - client.post_times[0], lifecycle.REPOST_AFTER)
        self.assertEqual(result['outcome'], 'available')
        # Never a third: a persistent Shutdown only waits out the budget.
        client, _, result = self.run_start([reading('Shutdown')])
        self.assertEqual((client.posts, result['outcome']), (2, 'timeout'))

    def test_posts_are_capped_per_rid(self):
        client, _, result = self.run_start([reading('Shutdown')], (503,))
        self.assertEqual(client.posts, lifecycle.MAX_POSTS)
        self.assertEqual((result['outcome'], result['status']), ('failed', 503))

    def test_other_4xx_stops_without_retry(self):
        client, _, result = self.run_start([reading('Shutdown')], (403,))
        self.assertEqual(client.posts, 1)
        self.assertEqual((result['outcome'], result['status']), ('failed', 403))

    def test_terminal_state_fails_without_posting(self):
        for state in ('Failed', 'Unavailable', 'Deleted', 'Archived'):
            client, _, result = self.run_start([reading(state)])
            self.assertEqual((client.posts, result['outcome'], result['last_state']), (0, 'failed', state))

    def test_terminal_state_after_post_fails_and_names_state(self):
        client, _, result = self.run_start([reading('Shutdown'), reading('Failed')])
        self.assertEqual((client.posts, result['outcome'], result['last_state']), (1, 'failed', 'Failed'))

    def test_unreadable_readings_wait_and_do_not_post(self):
        client, _, result = self.run_start([reading(None, None, ok=False)] * 2 + [reading('Available')])
        self.assertEqual((client.posts, result['outcome']), (0, 'available'))

    def test_available_wait_times_out_at_45_minutes(self):
        client, clock, result = self.run_start([reading('Shutdown'), reading('Queued')])
        self.assertEqual((client.posts, result['outcome'], result['last_state']), (1, 'timeout', 'Queued'))
        self.assertGreaterEqual(clock.t - client.post_times[0], lifecycle.AVAILABLE_WAIT)
        self.assertLess(clock.t - client.post_times[0], lifecycle.AVAILABLE_WAIT + 20)

    def test_shutdown_wait_times_out_at_20_minutes_without_posting(self):
        client, clock, result = self.run_start([reading('Shutdown', True)])
        self.assertEqual((client.posts, result['outcome']), (0, 'timeout'))
        self.assertGreaterEqual(clock.t - T0, lifecycle.SHUTDOWN_WAIT)

    def test_say_on_state_changes_and_tick_on_each_poll(self):
        said, ticks = [], []
        client, _, _ = self.run_start([reading('Shutdown'), reading('Shutdown'), reading('Available')],
                                      say=said.append, tick=lambda: ticks.append(1))
        self.assertEqual(said, ['Codespace state: Shutdown', 'Codespace state: Available'])
        self.assertEqual(len(ticks), 3)

    def test_ledger_gets_intent_before_and_result_after_each_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = lifecycle.Ledger(Path(tmp) / 'keeper-ledger.jsonl')
            clock = Clock()
            seen = []

            class Probe(FakeClient):
                def post_start(self):
                    seen.append([row['kind'] for row in ledger.rows()])
                    return super().post_start()
            client = Probe(clock, [reading('Shutdown'), reading('Available')])
            lifecycle.start(client, ledger=ledger, rid='r-000000Z', now=clock.now, sleep=clock.sleep)
            kinds = [row['kind'] for row in ledger.rows()]
            self.assertEqual(kinds, ['intent', 'result'])
            self.assertEqual(seen, [['intent']])
            self.assertNotIn('token', json.dumps(ledger.rows()))
            self.assertEqual(ledger.rows()[1]['status'], 200)
            self.assertEqual(oct(ledger.path.stat().st_mode & 0o777), '0o600')

    def test_unwritable_ledger_blocks_the_post(self):
        class Broken:
            def append(self, *args, **kwargs):
                raise OSError('read-only')
        clock = Clock()
        client = FakeClient(clock, [reading('Shutdown')])
        with self.assertRaises(OSError):
            lifecycle.start(client, ledger=Broken(), now=clock.now, sleep=clock.sleep)
        self.assertEqual(client.posts, 0)

    def test_ledger_skips_unparsable_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = lifecycle.Ledger(Path(tmp) / 'l.jsonl')
            ledger.append('intent', 'r-1')
            with ledger.path.open('a') as f:
                f.write('{broken\n[1]\n')
            ledger.append('result', 'r-1', status=200)
            self.assertEqual([row['kind'] for row in ledger.rows()], ['intent', 'result'])


class LifecycleAPITests(unittest.TestCase):
    NAME = 'space-1'

    def test_lifecycle_api_reads_and_posts_only_its_routes(self):
        calls = []
        body = json.dumps({'name': self.NAME, 'state': 'Shutdown', 'pending_operation': False,
                           'last_used_at': '2026-10-21T14:34:47Z'}).encode()

        def upstream(method, url, token):
            calls.append((method, url))
            return (200, body) if method == 'GET' else (202, b'{}')
        api = lifecycle.LifecycleAPI(self.NAME, upstream=upstream, token_reader=lambda: 't')
        row = api.read()
        self.assertEqual((row.ok, row.state, row.pending), (True, 'Shutdown', False))
        self.assertEqual(row.last_used_at, lifecycle.parse_time('2026-10-21T14:34:47Z'))
        self.assertEqual(api.post_start(), 202)
        self.assertEqual(api.post_stop(), 202)
        base = 'https://api.github.com/user/codespaces/' + self.NAME
        self.assertEqual(calls, [('GET', base), ('POST', base + '/start'), ('POST', base + '/stop')])

    def test_read_is_not_ok_on_bad_status_body_or_name(self):
        for status, body in ((500, b'{}'), (200, b'<html>'), (200, b'{"name":"other","state":"Available"}'),
                             (200, b'{"name":"space-1"}'), (200, b'[]')):
            api = lifecycle.LifecycleAPI(self.NAME, upstream=lambda m, u, t: (status, body), token_reader=lambda: 't')
            self.assertFalse(api.read().ok, (status, body))

    def test_network_failure_is_status_zero(self):
        def upstream(*args):
            raise OSError('down')
        api = lifecycle.LifecycleAPI(self.NAME, upstream=upstream, token_reader=lambda: 't')
        self.assertEqual(api.post_start(), 0)
        self.assertFalse(api.read().ok)

    def test_failing_token_reader_falls_back_to_the_last_good_token(self):
        tokens, used = iter(['good']), []

        def reader():
            try:
                return next(tokens)
            except StopIteration:
                raise ValueError('keychain locked') from None

        def upstream(method, url, token):
            used.append(token)
            return 200, json.dumps({'name': self.NAME, 'state': 'Available'}).encode()
        api = lifecycle.LifecycleAPI(self.NAME, upstream=upstream, token_reader=reader)
        self.assertTrue(api.read().ok)
        self.assertTrue(api.read().ok)
        self.assertEqual(used, ['good', 'good'])

    def test_no_token_ever_means_no_request(self):
        def reader():
            raise ValueError('no auth')
        api = lifecycle.LifecycleAPI(self.NAME, upstream=lambda *a: self.fail('no request'), token_reader=reader)
        self.assertFalse(api.read().ok)
        self.assertEqual(api.post_start(), 0)

    def test_default_reader_honors_the_selected_account(self):
        with patch.dict('os.environ', {'DEVCONTAINER_GH_USER': 'wrong', 'GH_TOKEN': ''}, clear=False), \
                patch('devcontainer_setup.project_sync.lifecycle.github_token', return_value='t') as reader:
            lifecycle.LifecycleAPI(self.NAME, 'alternate', upstream=lambda *a: (200, b'{}')).read()
        self.assertEqual(reader.call_args.args[0]['DEVCONTAINER_GH_USER'], 'alternate')

    def test_invalid_name_is_refused(self):
        with self.assertRaises(ValueError):
            lifecycle.LifecycleAPI('../x')


if __name__ == '__main__':
    unittest.main()
