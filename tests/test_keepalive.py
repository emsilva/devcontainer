"""The K4 keep-alive child: exact argv, the bind check, respawn with backoff and stop by fact."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from devcontainer_setup.project_sync import arming, keepalive, keeper
from devcontainer_setup.project_sync.lifecycle import Reading
from devcontainer_setup.project_sync.state import atomic_write, process_identity, read_json

TOKEN = 'gho_sentinel_real_token'

# Answers `auth token` from a file; any other call records itself and holds, exits
# or holds with a grandchild, as the mode file says. A held child ends by itself.
FAKE_GH = '''#!{python}
import json, os, pathlib, subprocess, sys, time
base = pathlib.Path({base!r})
args = sys.argv[1:]
if args[:2] == ['auth', 'token']:
    with open(base / 'token-calls.jsonl', 'a') as out:
        out.write(json.dumps(args) + '\\n')
    token = base / 'token'
    if not token.exists():
        sys.exit(1)
    print(token.read_text().strip())
    sys.exit(0)
mode = (base / 'mode').read_text().strip() if (base / 'mode').exists() else 'hold'
row = {{'pid': os.getpid(), 'args': args, 'env': dict(os.environ)}}
if mode == 'grandchild':
    row['grandchild'] = subprocess.Popen(['sleep', '60']).pid
with open(base / 'children.jsonl', 'a') as out:
    out.write(json.dumps(row) + '\\n')
if mode == 'exit':
    sys.exit(3)
time.sleep(60)
'''

# Answers a holder for a port listed in the holders file ("port pid" lines), else finds none.
FAKE_LSOF = '''#!{python}
import json, pathlib, sys
base = pathlib.Path({base!r})
with open(base / 'lsof-calls.jsonl', 'a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
port = next((arg[len('-iTCP:'):] for arg in sys.argv[1:] if arg.startswith('-iTCP:')), None)
holders = base / 'holders'
found = dict(line.split() for line in holders.read_text().splitlines()) if holders.exists() else {{}}
if port not in found:
    sys.exit(1)
print(found[port])
'''


def gone(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(.05)
    return False


class Fakes(unittest.TestCase):
    """A temporary HOME with the paired key, and fake gh and lsof first on PATH."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home, self.state = self.base / 'home', self.base / 'state'
        (self.home / '.ssh').mkdir(parents=True)
        self.key = self.home / '.ssh/codespaces.auto'
        self.key.write_text('fixture key')
        tools = self.base / 'bin'
        tools.mkdir()
        for name, text in (('gh', FAKE_GH), ('lsof', FAKE_LSOF)):
            (tools / name).write_text(text.format(python=sys.executable, base=str(self.base)))
            (tools / name).chmod(0o700)
        (self.base / 'token').write_text(TOKEN)
        env = patch.dict(os.environ, {'HOME': str(self.home), 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
        env.start()
        self.addCleanup(env.stop)
        for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
            if name in os.environ:
                token = patch.dict(os.environ)
                token.start()
                self.addCleanup(token.stop)
                del os.environ[name]
        self.children = []
        self.addCleanup(self.reap_children)
        self.clock = 1000.0

    def reap_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(5)

    def spawn(self, *args, **kwargs):
        child = subprocess.Popen(*args, **kwargs)
        self.children.append(child)
        return child

    def mode(self, value):
        (self.base / 'mode').write_text(value)

    def rows(self, name='children.jsonl', count=0):
        # A child writes its row once it runs, which can be just after ensure() returns.
        deadline = time.monotonic() + 5
        while True:
            try:
                rows = [json.loads(line) for line in (self.base / name).read_text().splitlines()]
            except FileNotFoundError:
                rows = []
            if len(rows) >= count or time.monotonic() > deadline:
                return rows
            time.sleep(.02)

    def holder(self, ports=(), name='cs-1'):
        made = keepalive.KeepAlive(name, self.state, 'octo', list(ports), popen=self.spawn, now=lambda: self.clock)
        self.addCleanup(made.stop)
        return made


class ArgvTests(unittest.TestCase):
    def test_argv_is_exact(self):
        key = Path('/Users/me/.ssh/codespaces.auto')
        self.assertEqual(keepalive.argv('cs-1', key, [(13570, 3570), (18787, 8787)]), [
            'gh', 'codespace', 'ssh', '-c', 'cs-1', '--', '-i', '/Users/me/.ssh/codespaces.auto',
            '-o', 'ServerAliveInterval=30', '-o', 'ExitOnForwardFailure=yes',
            '-L', '127.0.0.1:13570:localhost:3570', '-L', '127.0.0.1:18787:localhost:8787',
            'exec sleep infinity'])
        self.assertEqual(keepalive.argv('cs-1', key, [])[-1], 'exec sleep infinity')
        with self.assertRaises(ValueError):
            keepalive.argv('../cs-1', key, [])


class FreePortTests(Fakes):
    def test_port_in_use_is_skipped_and_reported(self):
        # Ephemeral ports, never 13570 itself: the owner's own cs:ports may hold that one.
        with socket.socket() as held, socket.socket() as probe:
            held.bind(('127.0.0.1', 0))
            held.listen()
            busy = held.getsockname()[1]
            probe.bind(('127.0.0.1', 0))
            free = probe.getsockname()[1]
            probe.close()
            (self.base / 'holders').write_text(f'{busy} 4242\n')
            up, skipped = keepalive.free_ports([free - 10000, busy - 10000])
            self.assertEqual(up, [(free, free - 10000)])
            self.assertEqual(skipped, [(busy, 4242)])
            self.assertEqual(self.rows('lsof-calls.jsonl'), [['-nP', '-t', f'-iTCP:{port}', '-sTCP:LISTEN']
                                                             for port in (free, busy)])
            # lsof finds no holder (another user's listener, or it exited meanwhile): the bind decides.
            (self.base / 'holders').unlink()
            self.assertEqual(keepalive.free_ports([busy - 10000]), ([], [(busy, None)]))

    def test_a_port_lsof_names_is_skipped_even_when_it_binds(self):
        # R19: lsof sees a holder the 127.0.0.1 test bind cannot, such as another address's listener.
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            free = probe.getsockname()[1]
        (self.base / 'holders').write_text(f'{free} 5150\n')
        self.assertEqual(keepalive.free_ports([free - 10000]), ([], [(free, 5150)]))

    def test_skipped_port_does_not_stop_the_child(self):
        with socket.socket() as held:
            held.bind(('127.0.0.1', 0))
            held.listen()
            busy = held.getsockname()[1]
            record = self.holder([busy - 10000]).ensure(True)
        self.assertTrue(record['pid'])
        self.assertEqual(record['ports_up'], [])
        self.assertEqual(record['ports_skipped'], [[busy, None]])
        self.assertNotIn('-L', self.rows(count=1)[0]['args'])


class AnyListenerTests(unittest.TestCase):
    """R19 through free_ports itself, with the real lsof (local only), as the reviewer's probe_bind.py.

    On macOS ssh's -L bind on 127.0.0.1 succeeds beside a 0.0.0.0 or :: listener and then
    shadows that service for 127.0.0.1 clients, so such a port is skipped too.
    """

    def assert_skipped_by_us(self, family, address):
        with socket.socket(family) as held:
            if family == socket.AF_INET6:
                held.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            try:
                held.bind((address, 0))
            except OSError:
                self.skipTest(address + ' cannot be bound on this host')
            held.listen()
            port = held.getsockname()[1]
            self.assertEqual(keepalive.free_ports([port - 10000]), ([], [(port, os.getpid())]))

    def test_a_loopback_listener_is_skipped(self):
        self.assert_skipped_by_us(socket.AF_INET, '127.0.0.1')

    def test_a_wildcard_listener_is_skipped(self):
        self.assert_skipped_by_us(socket.AF_INET, '0.0.0.0')

    @unittest.skipUnless(socket.has_ipv6, 'requires IPv6')
    def test_a_dual_stack_listener_is_skipped(self):
        self.assert_skipped_by_us(socket.AF_INET6, '::')


class KeepAliveTests(Fakes):
    def test_child_env_has_capability_not_token(self):
        record = self.holder().ensure(True)
        row, = self.rows(count=1)
        self.assertEqual(row['pid'], record['pid'])
        self.assertEqual(row['args'], keepalive.argv('cs-1', self.key, [])[1:])
        env = row['env']
        capability = env['GH_TOKEN']
        self.assertNotEqual(capability, TOKEN)
        self.assertTrue(env['GITHUB_API_URL'].startswith('http://127.0.0.1:'))
        self.assertTrue(env['GITHUB_API_URL'].endswith('/' + capability + '/'))
        self.assertNotIn('GITHUB_TOKEN', env)
        # The real token is in no argv, no child env and no runtime record.
        self.assertNotIn(TOKEN, json.dumps(row))
        self.assertNotIn(TOKEN, json.dumps(record))
        self.assertEqual(self.rows('token-calls.jsonl'), [['auth', 'token', '--hostname', 'github.com', '--user', 'octo']])
        # The read-only adapter is open while the child runs: it refuses a start request.
        with self.assertRaises(HTTPError) as caught:
            urlopen(env['GITHUB_API_URL'] + 'user/codespaces/cs-1/start', data=b'', timeout=3)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(record['process_identity'], process_identity(record['pid']))

    def test_child_respawns_with_backoff_while_available(self):
        self.mode('exit')
        held = self.holder()
        self.assertTrue(held.ensure(True)['pid'])
        for number, wait in enumerate((2, 4, 8, 16, 32, 60, 60), 1):
            self.children[-1].wait(5)
            record = held.ensure(True)
            self.assertIsNone(record['pid'])
            self.assertEqual(record['error'], 'the connection exited with status 3')
            self.clock += wait - .5
            self.assertIsNone(held.ensure(True)['pid'])
            self.clock += .5
            record = held.ensure(True)
            self.assertTrue(record['pid'])
            self.assertEqual(record['restarts'], number)
            self.assertIsNone(record['error'])
        self.assertEqual(len(self.children), 8)
        # A child that held for a minute or more starts the backoff afresh.
        self.children[-1].wait(5)
        self.mode('hold')
        self.assertIsNone(held.ensure(True)['pid'])
        self.clock += 59.5
        self.assertIsNone(held.ensure(True)['pid'])
        self.clock += .5
        self.assertTrue(held.ensure(True)['pid'])
        self.clock += 61
        self.children[-1].kill()
        self.children[-1].wait(5)
        self.assertIsNone(held.ensure(True)['pid'])
        self.clock += 2
        self.assertTrue(held.ensure(True)['pid'])

    def test_tend_restarts_an_exited_child_between_readings(self):
        held = self.holder()
        first = held.ensure(True)['pid']
        self.assertIsNone(held.tend())  # nothing changed
        self.children[-1].kill()
        self.children[-1].wait(5)
        noticed = held.tend()
        self.assertIsNone(noticed['pid'])
        self.assertEqual(noticed['error'], 'the connection exited with status -9')
        self.assertIsNone(held.tend())
        self.clock += 2
        restarted = held.tend()
        self.assertNotIn(restarted['pid'], (None, first))
        self.assertEqual(len(self.children), 2)

    def test_child_stops_and_stays_stopped_when_not_available(self):
        held = self.holder()
        pid = held.ensure(True)['pid']
        record = held.ensure(False)
        self.assertIsNone(record['pid'])
        self.assertIsNotNone(self.children[0].poll())
        for _ in range(3):
            self.clock += 120
            self.assertIsNone(held.ensure(False)['pid'])
            self.assertIsNone(held.tend())
        self.assertEqual(len(self.children), 1)
        # Available again: a new child.
        self.assertNotIn(held.ensure(True)['pid'], (None, pid))

    def test_unknown_reading_leaves_a_running_child_and_spawns_nothing(self):
        held = self.holder()
        pid = held.ensure(True)['pid']
        self.assertEqual(held.ensure(None)['pid'], pid)
        self.assertIsNone(self.children[0].poll())
        self.children[0].kill()
        self.children[0].wait(5)
        for _ in range(3):
            self.clock += 120
            self.assertIsNone(held.ensure(None)['pid'])
            self.assertIsNone(held.tend())
        self.assertEqual(len(self.children), 1)

    def test_stop_ends_the_child_with_its_ssh_and_closes_the_adapter(self):
        self.mode('grandchild')
        held = self.holder()
        record = held.ensure(True)
        row, = self.rows(count=1)
        held.stop()
        self.assertIsNotNone(self.children[0].poll())
        # gh's ssh runs in the child's process group; it goes with gh.
        self.assertTrue(gone(row['grandchild']))
        with self.assertRaises(URLError):
            urlopen(row['env']['GITHUB_API_URL'] + 'user/codespaces/cs-1', timeout=3)
        self.assertIsNone(held.ensure(None)['pid'])
        held.stop()  # idempotent
        self.assertTrue(record['pid'])

    def test_missing_key_or_token_is_named_and_retried_with_backoff(self):
        self.key.unlink()
        held = self.holder()
        record = held.ensure(True)
        self.assertIsNone(record['pid'])
        self.assertIn('codespaces.auto', record['error'])
        self.key.write_text('fixture key')
        (self.base / 'token').unlink()
        self.clock += 2
        record = held.ensure(True)
        self.assertIsNone(record['pid'])
        self.assertIn('GitHub token for octo', record['error'])
        self.assertEqual(self.children, [])
        (self.base / 'token').write_text(TOKEN)
        self.assertIsNone(held.ensure(True)['pid'])  # still within its backoff
        self.clock += 4
        self.assertTrue(held.ensure(True)['pid'])

    def test_a_failed_token_read_falls_back_to_the_last_good_one(self):
        # Spec D10, as for the keeper's reads: held in memory only.
        self.mode('exit')
        held = self.holder()
        self.assertTrue(held.ensure(True)['pid'])
        (self.base / 'token').unlink()
        self.children[-1].wait(5)
        held.ensure(True)
        self.clock += 2
        self.assertTrue(held.ensure(True)['pid'])
        self.assertEqual(len(self.children), 2)


class Readings:
    """Stands in for LifecycleAPI: replays readings, the last one repeating."""

    def __init__(self, readings, name='cs-1'):
        self.name, self.readings, self.reads = name, readings, 0

    def read(self):
        self.reads += 1
        return self.readings[min(self.reads, len(self.readings)) - 1]


class KeeperDrivesTests(Fakes):
    def setUp(self):
        super().setUp()
        arming.arm(self.state, gh_user='octo', codespace='cs-1', consent='x', billable_owner='octo', ports=[],
                   now=time.time())
        atomic_write(self.state / 'scratch.json', json.dumps({'codespace': 'cs-1', 'root': str(self.base)}).encode())

    def test_repeat_available_readings_keep_the_same_child(self):
        # Spec 6.1 step 7: an adopted keeper keeps its child; another Available reading changes nothing.
        now = time.time()
        k = keeper.Keeper(self.state, api=Readings([Reading(now, True, 'Available', False, now - 60)]),
                          notifier=lambda *_: True, keepalive=self.holder(), now=lambda: now)
        records = []
        for _ in range(3):
            k.step()
            records.append(read_json(self.state / keeper.RUNTIME)['keepalive'])
        self.assertTrue(records[0]['pid'])
        self.assertEqual(records[1:], records[:1] * 2)
        self.assertEqual(len(self.children), 1)
        self.assertEqual(records[0]['process_identity'], process_identity(records[0]['pid']))

    def test_supervise_ends_its_child_on_the_way_out(self):
        # R14: the keeper's finally stops the child, so a stopped keeper bills nothing.
        now = time.time()
        api = Readings([Reading(now, True, 'Available', False, now - 60)])
        handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        self.addCleanup(lambda: [signal.signal(number, handler) for number, handler in handlers.items()])

        def shutdown():
            self.rows(count=1)  # once the child runs, as a SIGTERM would find it
            raise keeper._Shutdown()
        with patch.object(keeper.lifecycle, 'LifecycleAPI', return_value=api), \
                patch.object(keeper.notify, 'notify', return_value=True), \
                patch.object(keeper, '_caffeinate', return_value=None), \
                patch.object(keeper.Keeper, 'rest', side_effect=shutdown):
            keeper.supervise(self.base, self.state, 'n' * 48)
        row, = self.rows(count=1)
        self.assertTrue(gone(row['pid']))
        runtime = read_json(self.state / keeper.RUNTIME)
        self.assertEqual(runtime['phase'], 'stopped')
        self.assertIsNone(runtime['keepalive']['pid'])
        log = (self.state / keeper.LOG).read_text()
        self.assertIn(f'Keep-alive started (pid {row["pid"]})', log)
        self.assertIn(f'Keep-alive stopped (pid {row["pid"]})', log)


class LeftBehindTests(Fakes):
    def decoy(self, name='cs-1'):
        ready = self.base / 'decoy-ready'
        child = self.spawn([sys.executable, '-c', 'import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); '
                            'time.sleep(60)', str(ready), 'codespace', 'ssh', '-c', name, '--', '-i', str(self.key),
                            'exec sleep infinity'], start_new_session=True)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        return child, {'pid': child.pid, 'process_identity': process_identity(child.pid)}

    def test_a_child_left_behind_is_ended_by_fact(self):
        child, record = self.decoy()
        self.assertEqual(keepalive.end_left_behind(record, 'cs-1'), child.pid)
        self.assertIsNotNone(child.wait(5))
        self.assertIsNone(keepalive.end_left_behind(record, 'cs-1'))

    def test_a_recycled_pid_or_another_codespace_is_never_signalled(self):
        child, record = self.decoy()
        identity = record['process_identity']
        for changed, name in ((dict(record, process_identity='X' + identity[1:]), 'cs-1'),
                              (record, 'cs-10'), (record, 'cs'), (dict(record, process_identity=None), 'cs-1'),
                              ({'pid': child.pid}, 'cs-1'), ({'pid': 'x', 'process_identity': identity}, 'cs-1'),
                              (None, 'cs-1'), ({}, None)):
            with self.subTest(record=changed, name=name):
                self.assertIsNone(keepalive.end_left_behind(changed, name))
                time.sleep(.1)
                self.assertIsNone(child.poll())
        other, other_record = self.decoy('cs-2')
        self.assertIsNone(keepalive.end_left_behind(other_record, 'cs-1'))
        self.assertIsNone(other.poll())

    def test_a_child_that_ignores_sigterm_is_killed(self):
        ready = self.base / 'stubborn-ready'
        child = self.spawn([sys.executable, '-c', 'import pathlib, signal, sys, time; '
                            'signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(sys.argv[1]).touch(); '
                            'time.sleep(60)', str(ready), 'codespace', 'ssh', '-c', 'cs-1', '--', 'exec sleep infinity'],
                           start_new_session=True)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        record = {'pid': child.pid, 'process_identity': process_identity(child.pid)}
        with patch.object(keepalive, 'TERMINATE_WAIT', .5):
            self.assertEqual(keepalive.end_left_behind(record, 'cs-1'), child.pid)
        self.assertEqual(child.wait(5), -signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
