"""The keeper: detached launch, adoption and stop by fact, and its watch in notify mode."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync import arming, keeper, lifecycle
from devcontainer_setup.project_sync.lifecycle import Reading
from devcontainer_setup.project_sync.state import atomic_write, process_identity, read_json

T0 = 1_000_000.0
LIFE = lifecycle.LIFETIME
AUTH_ERROR = 'GitHub authentication unavailable'


class FakeAPI:
    """Replays readings; an exception in the list is raised by read()."""

    def __init__(self, readings, name='cs-1'):
        self.name, self.readings, self.reads, self.posts = name, list(readings), 0, 0

    def read(self):
        item = self.readings[min(self.reads, len(self.readings) - 1)]
        self.reads += 1
        if isinstance(item, BaseException):
            raise item
        return item

    def post_start(self):
        self.posts += 1
        return 200

    post_stop = post_start


def up(at, last_used_at=None):
    return Reading(at, True, 'Available', False, last_used_at)


def down(at, state='Shutdown'):
    return Reading(at, True, state, False, None)


def failed(at):
    return Reading(at, False, None, None, None)


class KeeperStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state'
        arming.arm(self.state, gh_user='octo', codespace='cs-1', consent='x', billable_owner='octo',
                   ports=[], now=T0 - 10)
        self.bind('cs-1')
        self.clock = T0
        self.notes = []

    def bind(self, codespace):
        atomic_write(self.state / 'scratch.json',
                     json.dumps({'codespace': codespace, 'root': '/work/proj'}).encode())

    def keeper(self, readings):
        self.api = FakeAPI(readings)
        return keeper.Keeper(self.state, api=self.api, notifier=lambda title, text: self.notes.append(text) or True,
                             now=lambda: self.clock)

    def run_steps(self, k, times):
        for at in times:
            self.clock = at
            k.step()

    def runtime(self):
        return read_json(self.state / keeper.RUNTIME)

    def test_step_sets_api_anchor_then_observed_anchor(self):
        k = self.keeper([up(T0, last_used_at=T0 - 3600), down(T0 + 60), up(T0 + 120, last_used_at=T0 + 100)])
        self.run_steps(k, [T0])
        self.assertEqual(self.runtime()['anchor'], {'at': T0 - 3600, 'source': 'api'})
        self.run_steps(k, [T0 + 60])
        self.assertEqual(self.runtime()['anchor'], {'at': T0 - 3600, 'source': 'api'})
        self.run_steps(k, [T0 + 120])
        self.assertEqual(self.runtime()['anchor'], {'at': T0 + 120, 'source': 'observed'})
        self.assertEqual(self.runtime()['last_up_at'], T0 + 120)
        self.assertEqual(self.api.posts, 0)

    def test_stale_anchor_is_replaced_from_last_used_at(self):
        # A run that started over 12 h 15 min ago has ended; this one started later.
        k = self.keeper([up(T0, last_used_at=T0 - 600), up(T0 + LIFE + 1000, last_used_at=T0 + LIFE)])
        self.run_steps(k, [T0])
        self.assertEqual(self.runtime()['anchor']['at'], T0 - 600)
        self.run_steps(k, [T0 + LIFE + 1000])
        self.assertEqual(self.runtime()['anchor'], {'at': T0 + LIFE, 'source': 'api'})

    def test_three_down_readings_classify_once(self):
        stops = [T0 + 6 * 3600 + 60 * i for i in range(5)]
        k = self.keeper([up(T0, last_used_at=T0 - 60)] + [down(at) for at in stops])
        self.run_steps(k, [T0] + stops[:2])
        self.assertEqual(self.notes, [])
        with patch.object(lifecycle, 'classify', wraps=lifecycle.classify) as classify:
            self.run_steps(k, stops[2:])
        classify.assert_called_once()
        self.assertEqual(len(self.notes), 1)
        self.assertIn('for a reason other than the 12-hour limit; run task work:cs to start it', self.notes[0])
        self.assertIn(arming.format_time(stops[0]), self.notes[0])
        self.assertEqual(self.runtime()['down']['count'], 5)
        self.assertEqual(self.runtime()['down']['kind'], 'OTHER')

    def test_failed_reading_resets_down_count(self):
        times = [T0 + 6 * 3600 + 60 * i for i in range(6)]
        readings = [up(T0, last_used_at=T0 - 60), down(times[0]), down(times[1]), failed(times[2]),
                    down(times[3]), down(times[4]), down(times[5])]
        k = self.keeper(readings)
        self.run_steps(k, [T0] + times[:5])
        self.assertEqual(self.notes, [])
        self.assertEqual(self.runtime()['down'], {'count': 2, 'first_at': times[0]})
        self.run_steps(k, times[5:])
        self.assertEqual(len(self.notes), 1)
        # The stop is still keyed by its first non-Available reading.
        self.assertIn(arming.format_time(times[0]), self.notes[0])

    def test_token_failure_also_resets_down_count(self):
        times = [T0 + 6 * 3600 + 60 * i for i in range(4)]
        k = self.keeper([up(T0, last_used_at=T0 - 60), down(times[0]), down(times[1]),
                         ValueError(AUTH_ERROR + '; set GH_TOKEN or DEVCONTAINER_GH_USER.'), down(times[3])])
        self.run_steps(k, [T0] + times)
        self.assertEqual(self.notes, [])
        self.assertEqual(self.runtime()['down']['count'], 1)

    def test_lifetime_in_notify_mode_never_posts(self):
        anchor = T0 - 3600
        stops = [anchor + LIFE + 30 + 60 * i for i in range(4)]
        k = self.keeper([up(T0, last_used_at=anchor)] + [down(at, 'ShuttingDown') for at in stops[:1]]
                        + [down(at) for at in stops[1:]])
        self.run_steps(k, [T0] + stops)
        self.assertEqual(self.api.posts, 0)
        self.assertEqual(len(self.notes), 1)
        self.assertIn(('would restart proj (' + lifecycle.new_rid(stops[2]) + ')').lower(), self.notes[0].lower())
        self.assertEqual(self.runtime()['down']['kind'], 'LIFETIME')
        self.assertEqual(lifecycle.Ledger(self.state / keeper.LEDGER).rows(), [])

    def test_lifetime_names_the_refusing_gate(self):
        anchor = T0 - 3600
        arming.write(self.state, dict(arming.read(self.state), hold_until=anchor + LIFE + 3 * 3600))
        stops = [anchor + LIFE + 30 + 60 * i for i in range(3)]
        k = self.keeper([up(T0, last_used_at=anchor)] + [down(at) for at in stops])
        self.run_steps(k, [T0] + stops)
        self.assertEqual(self.api.posts, 0)
        self.assertEqual(len(self.notes), 1)
        self.assertIn('would not restart proj: on hold until', self.notes[0].lower())

    def test_owner_stop_is_logged_only(self):
        arming.write(self.state, dict(arming.read(self.state), owner_stop_at=T0 + 3600))
        stops = [T0 + 3700 + 60 * i for i in range(4)]
        k = self.keeper([up(T0, last_used_at=T0 - 60)] + [down(at) for at in stops])
        self.run_steps(k, [T0] + stops)
        self.assertEqual(self.notes, [])
        self.assertEqual(self.runtime()['down']['kind'], 'OWNER')

    def test_step_never_raises(self):
        for error, text in ((ValueError(AUTH_ERROR + '; set GH_TOKEN or DEVCONTAINER_GH_USER.'), AUTH_ERROR),
                            (RuntimeError('token ghp_secret in text'), 'RuntimeError')):
            with self.subTest(error=type(error).__name__):
                k = self.keeper([error])
                self.clock += 5
                k.step()
                runtime = self.runtime()
                self.assertEqual(runtime['heartbeat_at'], self.clock)
                self.assertEqual(runtime['phase'], 'waiting')
                self.assertIn(text, runtime['error'])
                self.assertNotIn('ghp_secret', json.dumps(runtime))
                self.assertEqual(k.delay, 2)
                k.step()
                self.assertEqual(k.delay, 4)

    def test_backoff_doubles_to_the_poll_and_resets_after_a_good_pass(self):
        k = self.keeper([RuntimeError()] * 7 + [up(T0, last_used_at=T0), RuntimeError()])
        delays = []
        for _ in range(9):
            k.step()
            delays.append(k.delay)
        self.assertEqual(delays, [2, 4, 8, 16, 32, 60, 60, keeper.POLL, 2])

    def test_paired_codespace_differing_from_the_armed_one_is_refused(self):
        self.bind('cs-2')
        k = self.keeper([up(T0, last_used_at=T0)])
        self.run_steps(k, [T0, T0 + 60])
        self.assertEqual(self.api.reads, 0)
        self.assertEqual(self.runtime()['phase'], 'refused')
        self.assertEqual(len(self.notes), 1)
        self.assertIn('task work:cs:armed', self.notes[0])

    def test_a_missing_pairing_is_refused_with_the_pairing_command(self):
        (self.state / 'scratch.json').unlink()
        k = self.keeper([up(T0, last_used_at=T0)])
        self.run_steps(k, [T0])
        self.assertEqual(self.api.reads, 0)
        runtime = self.runtime()
        self.assertEqual(runtime['phase'], 'refused')
        self.assertIn('not paired', runtime['error'])
        self.assertIn('run task work:cs', runtime['error'])
        self.assertNotIn('not the armed one', runtime['error'])
        self.assertIn('not paired', self.notes[0])

    def test_start_replaces_an_owned_keeper_whose_heartbeat_is_stale(self):
        from devcontainer_setup.project_sync.state import runtime_id
        owned = {'running': True, 'pid': 4121, 'nonce': 'n' * 48, 'process_identity': 'x',
                 'runtime_id': runtime_id(), 'codespace': 'cs-1', 'gh_user': 'octo'}
        launched = {'running': True, 'pid': 4122, 'nonce': 'm' * 48}
        for age, replaced in ((keeper.HEARTBEAT_STALE - 1, False), (keeper.HEARTBEAT_STALE + 1, True),
                              (None, True)):
            with self.subTest(age=age), \
                    patch.object(keeper, 'status', return_value=dict(owned, heartbeat_age=age)), \
                    patch.object(keeper, '_stop') as stop, \
                    patch.object(keeper, 'launch_detached', return_value=launched) as launch:
                result = keeper.start(self.state.parent, self.state)
            self.assertEqual(stop.called, replaced)
            self.assertEqual(launch.called, replaced)
            self.assertEqual(result['pid'], 4122 if replaced else 4121)

    def test_disarmed_keeper_ends_its_watch(self):
        arming.write(self.state, dict(arming.read(self.state), armed=False))
        k = self.keeper([up(T0, last_used_at=T0)])
        k.step()
        self.assertTrue(k.done)
        self.assertEqual(self.api.reads, 0)

    def test_replacement_keeper_keeps_the_watch_without_a_second_notification(self):
        stops = [T0 + 6 * 3600 + 60 * i for i in range(4)]
        k = self.keeper([up(T0, last_used_at=T0 - 60)] + [down(at) for at in stops[:3]])
        self.run_steps(k, [T0] + stops[:3])
        self.assertEqual(len(self.notes), 1)
        # An upgrade replaces the process within seconds; the stop is the same one.
        replacement = self.keeper([down(stops[3])])
        self.run_steps(replacement, stops[3:])
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.runtime()['anchor'], {'at': T0 - 60, 'source': 'api'})

    def test_a_stale_reload_drops_the_anchor_and_anchors_from_last_used_at(self):
        saved = {'codespace': 'cs-1', 'heartbeat_at': T0 - keeper.HEARTBEAT_STALE - 100,
                 'anchor': {'at': T0 - 1000, 'source': 'observed'}}
        atomic_write(self.state / keeper.RUNTIME, json.dumps(saved).encode())
        k = self.keeper([up(T0, last_used_at=T0 - 1100)])
        self.run_steps(k, [T0])
        self.assertEqual(self.runtime()['anchor'], {'at': T0 - 1100, 'source': 'api'})

    def test_a_start_the_keeper_never_saw_is_anchored_from_last_used_at(self):
        # Reviewer's case: anchor A, the keeper is gone, the Codespace is started
        # again at S = A + 6 h, then idles out at A + 12 h: not the 12-hour stop.
        a, s = T0, T0 + 6 * 3600
        k = self.keeper([up(a + 60, last_used_at=a)])
        self.run_steps(k, [a + 60])
        later = self.keeper([up(s + 60, last_used_at=s), up(s + 120, last_used_at=s)]
                            + [down(a + LIFE + 60 * i) for i in range(3)])
        self.run_steps(later, [s + 60, s + 120] + [a + LIFE + 60 * i for i in range(3)])
        self.assertEqual(self.runtime()['anchor'], {'at': s, 'source': 'api'})
        self.assertEqual(self.runtime()['down']['kind'], 'OTHER')
        self.assertEqual(len(self.notes), 1)
        self.assertIn('other than the 12-hour limit', self.notes[0])

    def stops_at(self, start):
        return [start + LIFE + 20 + 60 * i for i in range(3)]

    def test_a_fresh_replacement_keeps_its_anchor_through_a_no_op_start_request(self):
        # Observed start at A; a no-op start request at A + 2 h moves last_used_at but not
        # GitHub's clock; an upgrade replaces the keeper at A + 3 h, 65 s after its last reading.
        a = T0
        saved = {'codespace': 'cs-1', 'heartbeat_at': a + 3 * 3600 - 60, 'anchor': {'at': a, 'source': 'observed'},
                 'last_up_at': a + 3 * 3600 - 60, 'down': {'count': 0, 'first_at': None},
                 'last_ok': {'at': a + 3 * 3600 - 60, 'state': 'Available'}}
        atomic_write(self.state / keeper.RUNTIME, json.dumps(saved).encode())
        self.clock = a + 3 * 3600
        stops = self.stops_at(a)
        k = self.keeper([up(a + 3 * 3600 + 5, last_used_at=a + 2 * 3600)] + [down(at, 'ShuttingDown') for at in stops])
        self.run_steps(k, [a + 3 * 3600 + 5] + stops)
        self.assertEqual(self.runtime()['anchor'], {'at': a, 'source': 'observed'})
        self.assertEqual(self.runtime()['down']['kind'], 'LIFETIME')

    def test_one_keeper_reading_every_minute_ignores_a_no_op_start_request(self):
        a = T0
        k = self.keeper([down(a - 60), up(a, last_used_at=a - 30), up(a + 7140, last_used_at=a - 30),
                         up(a + 7200, last_used_at=a + 7190), up(a + 7260, last_used_at=a + 7190)]
                        + [down(at) for at in self.stops_at(a)])
        self.run_steps(k, [a - 60, a, a + 7140, a + 7200, a + 7260] + self.stops_at(a))
        self.assertEqual(self.runtime()['anchor'], {'at': a, 'source': 'observed'})
        self.assertEqual(self.runtime()['down']['kind'], 'LIFETIME')

    def test_a_reading_gap_that_could_hide_a_restart_anchors_from_last_used_at(self):
        # One keeper whose reads fail for hours while the Codespace stops and is started at S.
        a, s = T0, T0 + 6 * 3600
        readings = ([up(a + 60, last_used_at=a)] + [failed(a + 120 + 60 * i) for i in range(5)]
                    + [up(s + 60 + 60 * i, last_used_at=s) for i in range(3)]
                    + [down(a + LIFE + 60 * i) for i in range(3)])
        k = self.keeper(readings)
        self.run_steps(k, [r.at for r in readings])
        self.assertEqual(self.runtime()['anchor'], {'at': s, 'source': 'api'})
        self.assertEqual(self.runtime()['down']['kind'], 'OTHER')

    def test_a_fresh_replacement_still_sees_the_transition_it_was_in(self):
        saved = {'codespace': 'cs-1', 'heartbeat_at': T0 - 60, 'last_ok': {'at': T0 - 60, 'state': 'Shutdown'},
                 'down': {'count': 1, 'first_at': T0 - 60}}
        atomic_write(self.state / keeper.RUNTIME, json.dumps(saved).encode())
        k = self.keeper([up(T0, last_used_at=T0 - 30)])
        self.run_steps(k, [T0])
        self.assertEqual(self.runtime()['anchor'], {'at': T0, 'source': 'observed'})

    def test_a_stale_reload_starts_a_fresh_watch(self):
        saved = {'codespace': 'cs-1', 'heartbeat_at': T0 - keeper.HEARTBEAT_STALE - 1,
                 'down': {'count': 2, 'first_at': T0 - 300}, 'last_up_at': T0 - 1000}
        atomic_write(self.state / keeper.RUNTIME, json.dumps(saved).encode())
        k = self.keeper([down(T0)])
        self.run_steps(k, [T0])
        runtime = self.runtime()
        self.assertEqual(runtime['down'], {'count': 1, 'first_at': T0})
        self.assertNotIn('last_up_at', runtime)
        self.assertEqual(self.notes, [])

    def test_a_classification_that_raises_is_retried_on_the_next_readings(self):
        anchor = T0 - 3600
        stops = [anchor + LIFE + 30 + 60 * i for i in range(6)]
        k = self.keeper([up(T0, last_used_at=anchor)] + [down(at) for at in stops])
        with patch.object(lifecycle, 'gate', side_effect=[RuntimeError('ledger trouble'), None]):
            self.run_steps(k, [T0] + stops[:3])
            self.assertNotIn('kind', self.runtime()['down'])
            self.assertEqual(self.notes, [])
            self.run_steps(k, stops[3:])
        self.assertEqual(self.runtime()['down']['kind'], 'LIFETIME')
        self.assertEqual(len(self.notes), 1)
        self.assertIn('would restart', self.notes[0].lower())

    def test_a_malformed_saved_watch_starts_clean(self):
        saved = {'codespace': 'cs-1', 'heartbeat_at': T0, 'down': {'count': 2}, 'last_up_at': 'soon',
                 'anchor': {'source': 'api'}}
        atomic_write(self.state / keeper.RUNTIME, json.dumps(saved).encode())
        k = self.keeper([down(T0 + 60)])
        self.run_steps(k, [T0 + 60])
        runtime = self.runtime()
        self.assertEqual(runtime['phase'], 'watching')
        self.assertEqual(runtime['down'], {'count': 1, 'first_at': T0 + 60})
        self.assertNotIn('anchor', runtime)

    def test_watch_of_another_codespace_is_not_carried_over(self):
        k = self.keeper([up(T0, last_used_at=T0 - 60)])
        self.run_steps(k, [T0])
        arming.write(self.state, dict(arming.read(self.state), codespace='cs-2'))
        self.bind('cs-2')
        self.api = FakeAPI([up(T0 + 60, last_used_at=T0 + 30)], name='cs-2')
        other = keeper.Keeper(self.state, api=self.api, notifier=lambda *_: True, now=lambda: self.clock)
        self.clock = T0 + 60
        other.step()
        self.assertEqual(self.runtime()['anchor'], {'at': T0 + 30, 'source': 'api'})


class ForeignProcessTests(unittest.TestCase):
    def test_only_foreign_gh_processes_for_this_codespace_are_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp).resolve()
            atomic_write(state / keeper.RUNTIME, json.dumps({'keepalive': {'pid': 303}}).encode())
            listing = '\n'.join([
                '  101 gh codespace ports forward 2222:50123 -c cs-1',
                '  202 /opt/homebrew/bin/gh codespace ssh -c cs-1 --stdio -- -i /Users/me/.ssh/codespaces.auto',
                "  303 gh codespace ssh -c cs-1 -- -i key -o ServerAliveInterval=30 exec sleep infinity",
                '  404 gh codespace ssh -c cs-1 -- -N -L 13570:localhost:3570',
                '  505 gh codespace ssh -c cs-other -- -N -L 13570:localhost:3570',
                '  606 grep gh codespace cs-1',
                '  707 gh codespace ports forward 3570:3570 --codespace=cs-1',
                '  808 gh cs ssh -c cs-1 -- -N -L 13570:localhost:3570',
                '  909 /usr/local/bin/gh cs ports forward 3570:3570 -c cs-1',
                '  910 gh cs ports forward 2222:50124 -c cs-1',
                'garbage line',
            ])
            run = lambda *a, **k: subprocess.CompletedProcess(a, 0, listing + '\n', '')
            self.assertEqual(keeper.foreign_processes(state, 'cs-1', run=run), [404, 707, 808, 909])
            with self.assertRaisesRegex(ValueError, 'retry'):
                keeper.foreign_processes(state, 'cs-1', run=lambda *a, **k: subprocess.CompletedProcess(a, 1, '', ''))


FAKE_GH = '''#!{python}
import json, os, sys
with open({calls!r}, 'a') as out:
    out.write(json.dumps({{'args': sys.argv[1:], 'env': sorted(os.environ)}}) + '\\n')
sys.exit(1)
'''

FAKE_OSASCRIPT = '''#!{python}
import json, sys
with open({calls!r}, 'a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
'''


class KeeperProcessTests(unittest.TestCase):
    """The real detached `--keep` supervise, offline: the token read fails, so nothing reaches GitHub."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root, self.state = base / 'repo', base / 'state'
        self.root.mkdir()
        arming.arm(self.state, gh_user='octo', codespace='cs-1', consent='x', billable_owner='octo',
                   ports=[], now=time.time())
        atomic_write(self.state / 'scratch.json', json.dumps({'codespace': 'cs-1', 'root': str(self.root)}).encode())
        tools = base / 'bin'
        tools.mkdir()
        self.gh_calls, self.notes = base / 'gh-calls.jsonl', base / 'notes.jsonl'
        for name, text, calls in (('gh', FAKE_GH, self.gh_calls), ('osascript', FAKE_OSASCRIPT, self.notes)):
            (tools / name).write_text(text.format(python=sys.executable, calls=str(calls)))
            (tools / name).chmod(0o700)
        environment = patch.dict(os.environ, {'HOME': str(base), 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
        environment.start()
        self.addCleanup(environment.stop)
        # Cleanups run last-in first: the keeper stops before its state directory goes.
        self.addCleanup(keeper.stop, self.state)

    def keepers(self):
        listing = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout
        return [int(line.split()[0]) for line in listing.splitlines()
                if '--keep' in line and str(self.state) in line and '.keeper' in line]

    def gh_calls_made(self):
        try:
            return [json.loads(line) for line in self.gh_calls.read_text().splitlines()]
        except FileNotFoundError:
            return []

    def assert_watching_offline(self, nonce):
        ready = keeper.wait_ready(self.state, nonce, timeout=15)
        self.assertNotEqual(ready.get('phase'), 'starting', ready)
        self.assertEqual(ready['phase'], 'waiting')
        self.assertIn(AUTH_ERROR, ready['error'])
        calls = self.gh_calls_made()
        self.assertTrue(calls)
        # The only gh use is the token read; it failed, so LifecycleAPI never reached _upstream.
        for call in calls:
            self.assertEqual(call['args'], ['auth', 'token', '--hostname', 'github.com', '--user', 'octo'])
        self.assertNotIn('reading', read_json(self.state / keeper.RUNTIME))
        return ready

    def test_concurrent_arms_leave_one_keeper(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(keeper.start(self.root, self.state)))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(results), 2)
        self.assertEqual(len({(r['pid'], r['nonce']) for r in results}), 1)
        self.assertEqual(self.keepers(), [results[0]['pid']])
        self.assert_watching_offline(results[0]['nonce'])

    def test_repeat_start_adopts_same_keeper(self):
        first = keeper.start(self.root, self.state)
        self.assert_watching_offline(first['nonce'])
        again = keeper.start(self.root, self.state)
        self.assertEqual((again['pid'], again['nonce']), (first['pid'], first['nonce']))
        self.assertEqual(self.keepers(), [first['pid']])
        self.assertTrue(keeper.status(self.state)['running'])
        self.assertFalse(keeper.stop(self.state)['running'])
        self.assertFalse(keeper.stop(self.state)['running'])
        self.assertEqual(self.keepers(), [])
        log = (self.state / keeper.LOG).read_text().splitlines()
        self.assertTrue(log)
        for line in log:
            self.assertRegex(line, r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}Z ' + str(first['pid']) + ' ')

    def test_new_runtime_replaces_old_keeper(self):
        first = keeper.start(self.root, self.state)
        with patch.object(keeper, 'runtime_id', return_value='an upgraded package'):
            second = keeper.start(self.root, self.state)
        self.assertNotEqual(second['pid'], first['pid'])
        self.assertNotEqual(process_identity(first['pid']), first['process_identity'])
        self.assertEqual(self.keepers(), [second['pid']])
        self.assertEqual(keeper.start(self.root, self.state)['pid'], second['pid'])
        self.assert_watching_offline(second['nonce'])

    def test_stop_by_fact_ignores_a_reused_pid(self):
        nonce = 'a' * 48
        ready = self.state / 'decoy-ready'
        decoy = subprocess.Popen([sys.executable, '-c', 'import pathlib, sys, time; pathlib.Path(sys.argv[5]).touch(); '
                                  'time.sleep(60)', '--keep', 'project_sync.keeper', str(self.state), nonce, str(ready)])
        self.addCleanup(decoy.wait)
        self.addCleanup(decoy.kill)
        # Read its identity only once its Python code runs, after any interpreter re-exec.
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        identity = process_identity(decoy.pid)
        # Same pid, argv and nonce, but another start time: a reused pid.
        reused = {'pid': decoy.pid, 'nonce': nonce, 'process_identity': 'X' + identity[1:]}
        atomic_write(self.state / keeper.RUNTIME, json.dumps(reused).encode())
        self.assertFalse(keeper.status(self.state)['running'])
        keeper.stop(self.state)
        time.sleep(.2)
        self.assertIsNone(decoy.poll())
        # The exact record is owned, so the same stop ends it.
        atomic_write(self.state / keeper.RUNTIME, json.dumps(dict(reused, process_identity=identity)).encode())
        self.assertTrue(keeper.status(self.state)['running'])
        keeper.stop(self.state)
        self.assertIsNotNone(decoy.wait(timeout=5))

    def test_keeper_env_is_allow_listed(self):
        leaked = {'GH_TOKEN': 'leaked-token', 'GITHUB_TOKEN': 'leaked-token', 'HERDR_PANE_ID': 'pane-7',
                  'HERDR_ENV': '1', 'PYTHONPATH': '/nowhere', 'KEEPER_TEST_SENTINEL': 'x'}
        with patch.dict(os.environ, leaked):
            started = keeper.start(self.root, self.state)
        self.assert_watching_offline(started['nonce'])
        env = set(self.gh_calls_made()[0]['env'])
        self.assertTrue(env.isdisjoint(leaked), env & set(leaked))
        self.assertIn('DEVCONTAINER_GH_USER', env)
        allowed = set(keeper.ENV) | {'DEVCONTAINER_GH_USER', '__CF_USER_TEXT_ENCODING'}
        self.assertLessEqual(env, allowed)

    def test_a_second_keeper_on_a_held_lock_says_so_in_the_log(self):
        from devcontainer_setup.project_sync.state import locked
        with locked(self.state / 'keeper.lock'):
            with self.assertRaisesRegex(ValueError, 'task work:logs'):
                keeper.start(self.root, self.state)
        self.assertIn('Another keeper holds keeper.lock', (self.state / keeper.LOG).read_text())
        self.assertEqual(self.keepers(), [])

    def test_start_refuses_an_unarmed_project(self):
        arming.write(self.state, dict(arming.read(self.state), armed=False))
        with self.assertRaisesRegex(ValueError, 'task work:cs:armed'):
            keeper.start(self.root, self.state)
        self.assertEqual(self.keepers(), [])


if __name__ == '__main__':
    unittest.main()
