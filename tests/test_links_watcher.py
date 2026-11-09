"""Exercise the remote queue and the Mac watcher without network services."""
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from devcontainer_setup.project_sync import links
from tests import test_links as handler_tests
from tests.test_links import packet


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.remote = self.home / '.local/state/devcontainer-links'
        (self.remote / 'requests').mkdir(parents=True)
        self.state = self.home / 'state'
        self.root = self.home / 'project'
        (self.root / '.scratch').mkdir(parents=True)
        self.log = logging.getLogger('link-watcher-test')
        self.config = dict(version=1, identity='github.com/owner/project', codespace='fixture',
                           root=str(self.root), remote_root='/workspaces/project')

    def request(self, number=1, url=None, **changes):
        value = dict(created_at=time.time(), id=f'100-{number}-abcd')
        value.update(changes)
        return packet(**value) if url is None else packet(url, **value)

    def remote_child(self, *, signin=False):
        source = ('SIGNINS_ONLY=True\n' if signin else '') + links.REMOTE_WATCHER
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', source],
                                 env=dict(os.environ, HOME=str(self.home)), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.finish, child)
        return child

    def finish(self, child):
        if child.stdin is not None:
            child.stdin.close()
            child.stdin = None
        try:
            out, err = child.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=4)
            self.fail('remote watcher did not exit at stdin EOF')
        return out, err

    def line(self, child):
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(4), 'watcher emitted no request')
        return child.stdout.readline()

    def worker_log(self, dispatch):
        from devcontainer_setup.project_sync import scratch
        def installed(_):
            dispatch(logging.getLogger('project-sync-' + 'b' * 48))
            raise scratch._Shutdown()
        with mock.patch.object(scratch, '_config', return_value=self.config), \
                mock.patch.object(scratch, '_saved_session', return_value=None), \
                mock.patch.object(scratch, '_Notices'), \
                mock.patch.object(scratch, 'install_mutagen', side_effect=installed), \
                mock.patch.object(scratch.signal, 'signal'):
            scratch.supervise(self.root, self.state, 'b' * 48)
        return (self.state / 'scratch.log').read_text()

    def test_link_outcomes_reach_the_worker_log(self):
        helper = handler_tests.LinksTests('test_deadlines')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        handlers = list(links._LOG.handlers)
        settings = links._LOG.level, links._LOG.propagate
        attached = []
        levels = []
        def dispatch(logger):
            attached.extend(handler for handler in logger.handlers if handler in links._LOG.handlers)
            levels.append((links._LOG.level, links._LOG.propagate))
            with helper.forwards() as (forwards, _, _):
                self.assertEqual(helper.handle(links.parse_request(packet()), forwards), 'forwarded')
                busy = links.parse_request(packet(json.loads(packet())['url'].replace('8199', '1455')))
                with mock.patch.object(links, 'check_port', return_value=(False, 'server (pid 4242)')):
                    self.assertEqual(helper.handle(busy, forwards), 'busy')
        try:
            with mock.patch.object(logging.getLogger(), 'handle') as root_log:
                log = self.worker_log(dispatch)
            self.assertIn('Opened: plaud web.plaud.ai port 8199', log)
            self.assertIn('Busy: plaud web.plaud.ai port 1455', log)
            self.assertIn('server (pid 4242)', log)
            self.assertEqual(len(attached), 1)
            self.assertEqual(levels, [(logging.INFO, False)])
            self.assertEqual(log.count('Opened:'), 1)
            self.assertEqual(log.count('Busy:'), 1)
            root_log.assert_not_called()
            self.assertEqual(links._LOG.handlers, handlers)
            self.assertEqual((links._LOG.level, links._LOG.propagate), settings)
            self.assertIsNone(attached[0].stream)
            from devcontainer_setup.project_sync import scratch
            with mock.patch.object(scratch.project_state, 'process_identity',
                                   side_effect=ValueError('fixture setup failure')):
                with self.assertRaisesRegex(ValueError, 'fixture setup failure'):
                    self.worker_log(dispatch)
            self.assertEqual(links._LOG.handlers, handlers)
            self.assertEqual((links._LOG.level, links._LOG.propagate), settings)
            self.assertEqual(logging.getLogger('project-sync-' + 'b' * 48).handlers, [])
        finally:
            links._LOG.setLevel(settings[0])
            links._LOG.propagate = settings[1]

    def test_link_log_lines_never_hold_the_url(self):
        helper = handler_tests.LinksTests('test_deadlines')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        raw = packet(json.loads(packet())['url'] + '&code=SECRET&state=X')
        request = links.parse_request(raw)
        def dispatch(logger):
            for outcome in ('opened', 'busy', 'remote', 'forward', 'browser', 'expired',
                            'deadline', 'listener', 'exited'):
                with helper.forwards() as (forwards, _, _), contextlib.ExitStack() as stack:
                    if outcome == 'busy':
                        stack.enter_context(mock.patch.object(links, 'check_port',
                                                              return_value=(False, 'server (pid 4242)')))
                    if outcome == 'remote':
                        stack.enter_context(mock.patch.object(links, 'remote_listening', return_value=False))
                    if outcome == 'forward':
                        stack.enter_context(mock.patch.object(forwards, 'start', side_effect=ValueError(request.url)))
                    result = links.handle(request, helper.connection, forwards, project='project',
                                          now=lambda: 1000 if outcome == 'expired' else 101,
                                          opener=lambda _: outcome != 'browser', notifier=lambda *args: None)
                    expected = {'busy': 'busy', 'expired': 'expired', 'remote': 'rejected',
                                'forward': 'rejected', 'browser': 'rejected'}.get(outcome, 'forwarded')
                    self.assertEqual(result, expected)
                    if outcome in ('deadline', 'listener'):
                        with mock.patch.object(links, 'remote_listening', return_value=False):
                            forwards.tend(helper.connection, now=221 if outcome == 'deadline' else 102)
                    elif outcome == 'exited':
                        helper.children[-1].returncode = 0
                        forwards.active()
            invalid = packet(request.url, schema=2) + b'\n'
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c',
                                      'import os;os.write(1,' + repr(invalid) + ')'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True)
            watcher = links.Watcher(mock.Mock(), self.state, 'project', logger)
            try:
                with mock.patch.object(links.subprocess, 'Popen', return_value=child):
                    watcher._serve()
            finally:
                watcher.stop()
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=4)
            self.assertIsNotNone(child.returncode)
        log = self.worker_log(dispatch)
        for forbidden in (request.url, 'https://', 'SECRET', 'code=', 'state=', 'redirect_uri='):
            self.assertNotIn(forbidden, log)
        self.assertEqual(log.count('Opened:'), 4)
        for reason in ('deadline', 'remote stopped listening', 'callback forward exited'):
            self.assertEqual(log.count('(' + reason + ')'), 1)
        for outcome in ('Busy:', 'Rejected:', 'Forward start failed:',
                        'Browser could not open:', 'Expired:', 'Skipped invalid link request'):
            self.assertEqual(log.count(outcome), 1, outcome)

    def test_watcher_claims_once_and_streams_json(self):
        for number in range(20):
            (self.remote / 'requests' / f'{number}.json').write_bytes(self.request(number))
        first, second = self.remote_child(), self.remote_child()
        outputs = [self.finish(child) for child in (first, second)]
        values = [json.loads(line) for out, _ in outputs for line in out.splitlines()]
        self.assertEqual(len(values), 20)
        self.assertEqual({value['id'] for value in values}, {f'100-{n}-abcd' for n in range(20)})
        self.assertEqual(list((self.remote / 'requests').iterdir()), [])
        self.assertEqual(list((self.remote / 'claimed').iterdir()), [])
        self.assertTrue(all(not err for _, err in outputs))

    def stop_packet(self, **changes):
        value = dict(schema=1, kind='stop', id='100-1-abcdef01', created_at=time.time(), codespace='fixture')
        value.update(changes)
        stops = self.remote / 'stops'
        stops.mkdir(exist_ok=True)
        path = stops / (value['id'] + '.json')
        path.write_text(json.dumps(value))
        return path, value

    def test_watcher_streams_a_fresh_stop_request(self):
        path, value = self.stop_packet()
        result = path.with_suffix('.result')
        result.write_text('answer stays here')
        out, error = self.finish(self.remote_child())
        self.assertEqual([json.loads(line) for line in out.splitlines()], [value])
        self.assertFalse(path.exists())
        self.assertEqual(result.read_text(), 'answer stays here')
        self.assertEqual(list((self.remote / 'claimed').iterdir()), [])
        self.assertEqual(error, b'')
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_stale_stop_request_is_discarded(self):
        for index, stamp in enumerate((time.time() - 61, time.time() + 30, float('nan'))):
            self.stop_packet(id=f'100-{index}-abcdef01', created_at=stamp)
        out, error = self.finish(self.remote_child())
        self.assertEqual(out, b'')
        self.assertEqual(error.splitlines(), [b'Skipped stale or invalid stop request'] * 3)
        self.assertEqual(list((self.remote / 'stops').iterdir()), [])

    def test_invalid_stop_files_are_discarded_without_leaking_content(self):
        stops = self.remote / 'stops'
        path, _ = self.stop_packet(extra='PRIVATE-FIXTURE')
        target = self.home / 'untouched-stop'
        target.write_bytes(path.read_bytes())
        (stops / 'symlink.json').symlink_to(target)
        (stops / 'big.json').write_bytes(b'X' * 65537)
        os.mkfifo(stops / 'fifo.json')
        (stops / 'directory.json').mkdir()
        self.stop_packet(id='100-2-abcdef01', schema=True)
        self.stop_packet(id='100-3-abcdef01', codespace='../unsafe')
        out, error = self.finish(self.remote_child())
        self.assertEqual(out, b'')
        self.assertEqual(error.splitlines(), [b'Skipped stale or invalid stop request'] * 7)
        self.assertTrue(target.exists())
        self.assertEqual(list(stops.iterdir()), [])

    def test_signins_only_watcher_leaves_stop_requests(self):
        path, _ = self.stop_packet()
        out, error = self.finish(self.remote_child(signin=True))
        self.assertTrue(path.exists())
        self.assertFalse((self.remote / 'stop-listening-v1').exists())
        self.assertTrue((self.remote / 'mac-listening').exists())
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out, error), (b'', b''))

    def test_normal_watcher_advertises_the_stop_capability(self):
        capability = self.remote / 'stop-listening-v1'
        capability.touch()
        os.utime(capability, (1, 1))
        child = self.remote_child()
        deadline = time.monotonic() + 4
        while capability.stat().st_mtime == 1 and time.monotonic() < deadline:
            time.sleep(.02)
        stamp = capability.stat().st_mtime
        self.assertLess(abs(time.time() - stamp), 2)
        deadline = time.monotonic() + 7
        while capability.stat().st_mtime == stamp and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertGreater(capability.stat().st_mtime, stamp)
        self.assertEqual(self.finish(child), (b'', b''))
        self.assertEqual(capability.stat().st_mode & 0o777, 0o600)

    def test_watcher_skips_bad_request_files(self):
        requests = self.remote / 'requests'
        target = self.home / 'untouched'
        target.write_bytes(self.request(90))
        (requests / 'symlink.json').symlink_to(target)
        (requests / 'big.json').write_bytes(b'X' * 65537)
        (requests / 'invalid.json').write_bytes(b'not JSON SECRET-QUERY')
        (requests / 'schema.json').write_bytes(self.request(4, schema=2))
        os.mkfifo(requests / 'fifo.json')
        (requests / 'directory.json').mkdir()
        child = self.remote_child()
        deadline = time.monotonic() + 4
        while list(requests.iterdir()) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(list(requests.iterdir()), [])
        (requests / 'later.json').write_bytes(self.request(91))
        self.assertEqual(json.loads(self.line(child))['id'], '100-91-abcd')
        _, error = self.finish(child)
        self.assertEqual(len(error.splitlines()), 6)
        self.assertNotIn(b'SECRET-QUERY', error)
        self.assertEqual(target.read_bytes(), self.request(90, created_at=json.loads(target.read_bytes())['created_at']))

    def test_watcher_handles_long_request_filename(self):
        (self.remote / 'requests' / ('x' * 245 + '.json')).write_bytes(self.request(8))
        child = self.remote_child()
        out, error = self.finish(child)
        self.assertEqual(child.returncode, 0, error.decode())
        self.assertEqual([json.loads(line)['id'] for line in out.splitlines()], ['100-8-abcd'])

    def test_watcher_heartbeat_and_eof_exit(self):
        heartbeat = self.remote / 'mac-listening'
        heartbeat.touch()
        os.utime(heartbeat, (1, 1))
        child = self.remote_child()
        deadline = time.monotonic() + 4
        while heartbeat.stat().st_mtime == 1 and time.monotonic() < deadline:
            time.sleep(.02)
        stamp = heartbeat.stat().st_mtime
        self.assertLess(abs(time.time() - stamp), 2)
        deadline = time.monotonic() + 7
        while heartbeat.stat().st_mtime == stamp and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertGreater(heartbeat.stat().st_mtime, stamp)
        self.assertEqual(self.finish(child), (b'', b''))
        self.assertEqual(child.returncode, 0)
        self.assertEqual(self.remote.stat().st_mode & 0o777, 0o700)

    def test_thread_restarts_stream_with_backoff(self):
        clock = [0.0]
        connection = mock.Mock()
        connection.child.poll.return_value = None
        watcher = links.Watcher(connection, self.state, 'project', self.log,
                                now=lambda: clock[0])
        waits, calls = [], []
        def serve():
            calls.append(clock[0])
        def wait(delay):
            waits.append(delay)
            clock[0] += delay
            return len(waits) == 2
        with mock.patch.object(watcher, '_serve', side_effect=serve), \
                mock.patch.object(watcher._halt, 'wait', side_effect=wait):
            watcher.run()
        self.assertEqual(calls, [0, 2])
        self.assertEqual(waits, [2, 4])
        self.assertFalse((self.state / 'links-watcher.json').exists())

    def test_thread_survives_listener_probe_error_during_backoff(self):
        helper = handler_tests.LinksTests('test_deadlines')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        clock, calls = [0.0], []
        with helper.forwards() as (forwards, _, killed), \
                mock.patch.object(links, 'Forwards', return_value=forwards), \
                mock.patch.object(links, 'remote_listening', side_effect=[True, ValueError('transient probe')]):
            connection = mock.Mock()
            connection.child.poll.return_value = None
            watcher = links.Watcher(connection, helper.state, 'project', self.log, now=lambda: clock[0])
            def serve():
                calls.append(clock[0])
                if len(calls) == 1:
                    helper.handle(links.parse_request(packet()), forwards)
                else:
                    watcher._halt.set()
            def wait(delay):
                clock[0] += delay
                return watcher._halt.is_set()
            with mock.patch.object(watcher, '_serve', side_effect=serve), \
                    mock.patch.object(watcher._halt, 'wait', side_effect=wait):
                watcher.run()
            self.assertEqual(calls, [0, 2])
            self.assertEqual(forwards.active(), set())
            killed.assert_called_once_with(4200, signal.SIGTERM)

    def test_supervise_starts_and_stops_watcher_with_connection(self):
        from devcontainer_setup.project_sync import scratch
        started, watchers, events, observed = threading.Event(), [], [], []
        real_watcher = links.Watcher
        class Connection:
            child = mock.Mock(poll=lambda: None)
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def ssh_config(self, path):
                path.write_text('fixture configuration')
                events.append('config')
            def __exit__(self, *args):
                observed.append([watcher.is_alive() for watcher in watchers])
                events.append('connection-exit')
        def factory(*args):
            self.assertEqual(events, ['config', 'refresh'])
            watcher = real_watcher(*args)
            watchers.append(watcher)
            return watcher
        def serve(watcher):
            started.set()
            watcher._halt.wait(4)
        def interrupted(*args):
            observed.append(started.wait(3))
            observed.append(bool(watchers and watchers[0].is_alive()))
            raise scratch._Shutdown()
        with contextlib.ExitStack() as stack:
            for owner, name, replacement in (
                    (scratch, '_config', mock.Mock(return_value=self.config)),
                    (scratch, 'CodespaceConnection', Connection),
                    (scratch, 'install_mutagen', mock.Mock(return_value=Path('/fake/mutagen'))),
                    (scratch, '_remote', mock.Mock(return_value=json.dumps(dict(
                        identity=self.config['identity'], root=self.config['remote_root'])).encode())),
                    (scratch, '_session_info', mock.Mock(side_effect=interrupted)),
                    (scratch, '_pause_saved_session', mock.Mock()),
                    (scratch, '_mutagen', mock.Mock(return_value='')),
                    (scratch, '_Notices', mock.Mock()),
                    (real_watcher, '_serve', serve),
                    (links, 'Watcher', mock.Mock(side_effect=factory))):
                stack.enter_context(mock.patch.object(owner, name, replacement))
            stack.enter_context(mock.patch('devcontainer_setup.project_sync.ssh_hosts.refresh_ssh',
                                           side_effect=lambda state: events.append('refresh')))
            stack.enter_context(mock.patch.object(scratch.signal, 'signal'))
            try:
                scratch.supervise(self.root, self.state, 'a' * 48)
            finally:
                # Also reap a real thread when a negative control disables the hook's stop.
                for watcher in watchers:
                    watcher._halt.set()
                    if watcher.is_alive():
                        watcher.join(timeout=5)
        self.assertEqual(events, ['config', 'refresh', 'connection-exit'])
        self.assertEqual(observed, [True, True, [False]])
        self.assertTrue(started.is_set())
        self.assertFalse(watchers[0].is_alive())

    def test_watcher_live_requires_fresh_heartbeat_and_process_identity(self):
        from devcontainer_setup.project_sync.state import atomic_write
        value = dict(pid=os.getpid(), process_identity='owner', heartbeat_at=time.time())
        for age, identity, expected in ((0, 'owner', True), (16, 'owner', False), (0, 'reused', False)):
            value['heartbeat_at'] = time.time() - age
            atomic_write(self.state / 'links-watcher.json', json.dumps(value).encode())
            with mock.patch.object(links, 'process_identity', return_value=identity):
                self.assertEqual(links.watcher_live(self.state), expected)

    def test_signin_refuses_while_watcher_live(self):
        with mock.patch.object(links, 'watcher_live', return_value=True), \
                mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection') as connection, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(links.signin_main(self.root, self.state), 1)
        connection.assert_not_called()
        self.assertEqual(out.getvalue().strip(), 'The connected worker is already handling links.')

    def test_signin_refusal_preserves_worker_ssh_config(self):
        from devcontainer_setup.project_sync import scratch
        self.state.mkdir()
        path = self.state / 'ssh-config'
        path.write_bytes(b'worker port 40001\n')
        with mock.patch.object(links, 'watcher_live', side_effect=[False, True]), \
                mock.patch.object(scratch, '_config', return_value=self.config), \
                mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection') as factory, \
                contextlib.redirect_stdout(io.StringIO()):
            connection = factory.return_value.__enter__.return_value
            connection.ssh_config.side_effect = lambda dest: dest.write_bytes(b'manual port 40002\n')
            self.assertEqual(links.signin_main(self.root, self.state), 1)
            connection.ssh_config.assert_not_called()
        self.assertEqual(path.read_bytes(), b'worker port 40001\n')

    def test_signin_waits_for_ssh_before_publishing_config(self):
        # A forward restarted during the SSH wait moves to a new port; a config
        # published before that would point every ssh -L at the dead one.
        from devcontainer_setup.project_sync import scratch
        self.state.mkdir()
        with mock.patch.object(links, 'watcher_live', return_value=False), \
                mock.patch.object(scratch, '_config', return_value=self.config), \
                mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection') as factory:
            connection = factory.return_value.__enter__.return_value
            connection.python.return_value = b''
            self.assertEqual(links.signin_main(self.root, self.state), 0)
        names = [call[0] for call in connection.method_calls]
        self.assertIn('ensure_ready', names)
        self.assertLess(names.index('ensure_ready'), names.index('ssh_config'))

    def test_signin_restores_config_and_preserves_newer_worker_config(self):
        from devcontainer_setup.project_sync import scratch
        self.state.mkdir()
        path = self.state / 'ssh-config'
        for newer in (False, True):
            with self.subTest(newer=newer):
                path.write_bytes(b'worker port 40001\n')
                with mock.patch.object(links, 'watcher_live', return_value=False), \
                        mock.patch.object(scratch, '_config', return_value=self.config), \
                        mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection') as factory:
                    connection = factory.return_value.__enter__.return_value
                    connection.ssh_config.side_effect = lambda dest: dest.write_bytes(b'manual port 40002\n')
                    def snapshot(*args, **kwargs):
                        self.assertEqual(path.read_bytes(), b'manual port 40002\n')
                        if newer:
                            path.write_bytes(b'newer worker port 40003\n')
                        return b''
                    connection.python.side_effect = snapshot
                    self.assertEqual(links.signin_main(self.root, self.state), 0)
                expected = b'newer worker port 40003\n' if newer else b'worker port 40001\n'
                self.assertEqual(path.read_bytes(), expected)

    def test_signin_restores_config_when_interrupted_during_publication(self):
        from devcontainer_setup.project_sync import scratch
        self.state.mkdir()
        path = self.state / 'ssh-config'
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            for direct in (False, True):
                with self.subTest(signal=number, direct=direct):
                    path.write_bytes(b'worker port 40001\n')
                    with mock.patch.object(links, 'watcher_live', return_value=False), \
                            mock.patch.object(scratch, '_config', return_value=self.config), \
                            mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection') as factory:
                        connection = factory.return_value.__enter__.return_value
                        def interrupted(dest):
                            dest.write_bytes(b'manual port 40002\n')
                            if direct:
                                raise links._SigninStop(number)
                            signal.raise_signal(number)
                        connection.ssh_config.side_effect = interrupted
                        self.assertEqual(links.signin_main(self.root, self.state), 128 + number)
                    self.assertEqual(path.read_bytes(), b'worker port 40001\n')

    def test_expired_forward_stops_even_when_another_probe_fails(self):
        helper = handler_tests.LinksTests('test_deadlines')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with helper.forwards() as (forwards, _, killed):
            helper.handle(links.parse_request(packet('http://localhost:8192/')), forwards)
            helper.handle(links.parse_request(packet(json.loads(packet())['url'].replace('8199', '8193'))), forwards)
            with mock.patch.object(forwards, 'active', return_value=[8192, 8193]), \
                    mock.patch.object(links, 'remote_listening', side_effect=ValueError('transient')):
                with self.assertRaises(ValueError):
                    forwards.tend(helper.connection, now=1000)
            self.assertEqual(forwards.active(), {8192})
            killed.assert_called_once_with(4201, signal.SIGTERM)

    def test_stop_prevents_dispatch_of_other_buffered_requests(self):
        payload = self.request(1, 'https://first.example/') + b'\n' + self.request(2, 'https://second.example/') + b'\n'
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c',
                                  'import os,sys;os.write(1,' + repr(payload) + ');sys.stdin.buffer.read()'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
        watcher = links.Watcher(mock.Mock(), self.state, 'project', self.log)
        handled = []
        def handle(request, *args, **kwargs):
            handled.append(request.host)
            watcher._halt.set()
        try:
            with mock.patch.object(links.subprocess, 'Popen', return_value=child), \
                    mock.patch.object(links, 'handle', side_effect=handle):
                watcher._serve()
        finally:
            watcher.stop()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=4)
        self.assertEqual(handled, ['first.example'])
        self.assertIsNotNone(child.returncode)

    @contextlib.contextmanager
    def signin_fixture(self):
        from devcontainer_setup.project_sync import scratch
        helper = handler_tests.LinksTests('test_deadlines')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with helper.forwards() as (forwards, launched, killed), \
                mock.patch.object(links, 'Forwards', return_value=forwards), \
                mock.patch.object(links, 'watcher_live', return_value=False), \
                mock.patch.object(scratch, '_config', return_value=self.config), \
                mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection') as connection:
            yield helper, forwards, connection.return_value.__enter__.return_value, killed

    def test_signin_handles_only_signins_and_exits_when_done(self):
        with self.signin_fixture() as (helper, forwards, connection, killed):
            values = [self.request(1, 'https://github.com/login/device'),
                      self.request(2, 'http://localhost:5173/'), self.request(3)]
            batches, opened = [b'\n'.join(values), b''], []
            def remote(code, payload=b'', **kwargs):
                if code.startswith('SIGNINS_ONLY=True'):
                    return batches.pop(0)
                return b'1' if not opened else b'0'
            connection.python.side_effect = remote
            real_handle = links.handle
            def handle(request, *args, **kwargs):
                return real_handle(request, *args, **kwargs, opener=lambda url: opened.append(url) or True,
                                   notifier=lambda *args: None)
            with mock.patch.object(links, 'handle', side_effect=handle), mock.patch.object(links.time, 'sleep'):
                self.assertEqual(links.signin_main(self.root, self.state), 0)
            self.assertEqual(opened, [json.loads(values[2])['url']])
            self.assertEqual(forwards.active(), set())
            killed.assert_called_once_with(4200, signal.SIGTERM)

    def test_stop_ends_owned_forwards(self):
        for number in (None, signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number), self.signin_fixture() as (helper, forwards, connection, killed):
                path = helper.state / 'links-forwards.json'
                def foreign_record():
                    data = json.loads(path.read_text())
                    data['forwards']['9999'] = dict(pid=7777, process_identity='foreign',
                        owner='foreign', owner_pid=9999, owner_identity='foreign-owner')
                    helper.identities[9999] = 'foreign-owner'
                    path.write_text(json.dumps(data))
                handlers, calls = {}, [0]
                def install(sig, handler):
                    previous = handlers.get(sig, signal.SIG_DFL)
                    handlers[sig] = handler
                    return previous
                real_handle = links.handle
                def handle(request, *args, **kwargs):
                    result = real_handle(request, *args, **kwargs, opener=lambda url: True,
                                         notifier=lambda *args: None)
                    foreign_record()
                    return result
                def remote(code, payload=b'', **kwargs):
                    if code.startswith('SIGNINS_ONLY=True'):
                        calls[0] += 1
                        if calls[0] == 1:
                            return self.request()
                        handlers[number](number, None)
                    return b'1'
                if number is None:
                    request = links.parse_request(self.request())
                    helper.handle(request, forwards)
                    foreign_record()
                    watcher = links.Watcher(mock.Mock(), helper.state, 'project', self.log)
                    watcher.stop()
                else:
                    connection.python.side_effect = remote
                    with mock.patch.object(links, 'handle', side_effect=handle), \
                            mock.patch.object(links.signal, 'signal', side_effect=install), \
                            mock.patch.object(links.time, 'sleep'):
                        self.assertEqual(links.signin_main(self.root, self.state), 128 + number)
                self.assertEqual(forwards.active(), set())
                self.assertEqual(set(json.loads(path.read_text())['forwards']), {'9999'})
                killed.assert_called_once_with(4200, signal.SIGTERM)

    def test_remote_signin_snapshot_leaves_other_links_pending(self):
        requests = self.remote / 'requests'
        (requests / 'link.json').write_bytes(self.request(1, 'https://example.com/'))
        (requests / 'signin.json').write_bytes(self.request(2))
        (requests / 'expired.json').write_bytes(self.request(3, created_at=time.time() - 601))
        out, error = self.finish(self.remote_child(signin=True))
        self.assertEqual([json.loads(line)['id'] for line in out.splitlines()], ['100-2-abcd'])
        self.assertEqual([path.name for path in requests.iterdir()], ['link.json'])
        self.assertEqual(error, b'')


class StopWatcherTests(unittest.TestCase):
    def setUp(self):
        from devcontainer_setup.project_sync import arming, lifecycle, power
        from devcontainer_setup.project_sync.state import atomic_write
        self.power, self.arming = power, arming
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve()
        self.clock = time.time()
        self.connection = mock.Mock(codespace='fixture')
        self.connection.child.poll.return_value = None
        self.log = mock.Mock()
        self.watcher = links.Watcher(self.connection, self.state, 'project', self.log, now=lambda: self.clock)
        self.addCleanup(self.watcher.stop)
        atomic_write(self.state / 'scratch.json', json.dumps({'codespace': 'fixture'}).encode())
        arming.write(self.state, dict(schema=1, armed=True, codespace='fixture', gh_user='fixture-user'))
        self.fake = mock.Mock()
        self.fake.post_stop.return_value = 202
        patch = mock.patch.object(lifecycle, 'LifecycleAPI', return_value=self.fake)
        patch.start()
        self.addCleanup(patch.stop)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('CODESPACES', 'GH_TOKEN', 'GITHUB_TOKEN'):
            os.environ.pop(name, None)

    def packet(self, **changes):
        value = dict(schema=1, kind='stop', id='100-1-abcdef01', created_at=self.clock, codespace='fixture')
        value.update(changes)
        return value

    def join_handler(self):
        thread = getattr(self.watcher, '_stopping', None)
        if thread is not None:
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'stop handler did not finish')

    def answer(self):
        self.join_handler()
        self.connection.python.assert_called_once()
        call = self.connection.python.call_args
        self.assertEqual(call.kwargs['timeout'], 10)
        # Run the real answer publisher locally under an isolated HOME.
        result = subprocess.run([sys.executable, '-I', '-B', '-c', call.args[0]], input=call.args[1],
                                env=dict(os.environ, HOME=str(self.state)), capture_output=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        path = self.state / '.local/state/devcontainer-links/stops/100-1-abcdef01.result'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return json.loads(path.read_bytes())

    def disconnect_on_post(self, status=202):
        self.posted, self.delivered, self.stop_events = False, [], []

        def write(source, payload, *, timeout):
            if self.posted:
                raise OSError('fixture Codespace shut down after the stop POST')
            value = json.loads(payload)
            if value['ok']:
                record = self.arming.read(self.state)
                self.assertEqual(record['owner_stop_codespace'], 'fixture')
                self.assertIsInstance(record['owner_stop_at'], (int, float))
                from devcontainer_setup.project_sync.state import locked
                with locked(self.state / 'workspace-control.lock'):
                    pass
            result = subprocess.run([sys.executable, '-I', '-B', '-c', source], input=payload,
                                    env=dict(os.environ, HOME=str(self.state)), capture_output=True, timeout=4)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            path = self.state / '.local/state/devcontainer-links/stops' / (value['id'] + '.result')
            self.delivered.append(json.loads(path.read_bytes()))
            self.stop_events.append('answer')
            return result.stdout

        def post():
            self.posted = True
            self.stop_events.append('post')
            return status

        self.connection.python.side_effect = write
        self.fake.post_stop.side_effect = post

    def test_stop_answer_survives_codespace_shutdown_on_post(self):
        self.disconnect_on_post()
        self.watcher._stop_request(self.packet())
        self.join_handler()
        self.fake.post_stop.assert_called_once_with()
        self.connection.python.assert_called_once()
        self.assertEqual(self.stop_events, ['answer', 'post'])
        self.assertEqual(self.delivered, [{'ok': True, 'message':
                         'Stopping fixture; it stays stopped until task work:cs starts it again.'}])

    def test_unpaired_stop_is_refused_then_paired_retry_answers_before_post(self):
        from devcontainer_setup.project_sync.state import atomic_write
        (self.state / 'scratch.json').unlink()
        before = (self.state / self.arming.ARMING).read_bytes()
        self.disconnect_on_post()
        self.watcher._stop_request(self.packet())
        self.join_handler()
        self.fake.post_stop.assert_not_called()
        self.connection.python.assert_called_once()
        self.assertEqual(self.delivered, [{'ok': False, 'message': 'Pair a Codespace first: run task work:cs'}])
        self.assertEqual((self.state / self.arming.ARMING).read_bytes(), before)
        atomic_write(self.state / 'scratch.json', json.dumps({'codespace': 'fixture'}).encode())
        self.watcher._stop_request(self.packet(id='100-2-abcdef01'))
        self.join_handler()
        self.assertEqual(self.stop_events, ['answer', 'answer', 'post'])
        self.assertEqual([answer['ok'] for answer in self.delivered], [False, True])
        self.fake.post_stop.assert_called_once_with()
        self.assertEqual(self.connection.python.call_count, 2)

    def test_post_failure_is_logged_after_one_success_answer(self):
        for status, reason in ((403, 'GitHub refused the stop request (HTTP 403)'),
                               (503, 'The stop request did not complete (HTTP 503)')):
            with self.subTest(status=status):
                self.connection.python.reset_mock()
                self.fake.post_stop.reset_mock()
                self.log.info.reset_mock()
                self.disconnect_on_post(status)
                self.watcher._stop_request(self.packet())
                self.join_handler()
                self.assertEqual(self.stop_events, ['answer', 'post'])
                self.assertEqual(len(self.delivered), 1)
                self.assertTrue(self.delivered[0]['ok'])
                self.connection.python.assert_called_once()
                self.fake.post_stop.assert_called_once_with()
                self.assertIn(reason, str(self.log.info.call_args_list))
                self.assertNotIn('Could not write', str(self.log.info.call_args_list))
                self.assertIn('owner_stop_at', self.arming.read(self.state))

    def test_stop_request_goes_to_power_not_links(self):
        value = self.packet()
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c',
                                  'import json;print(' + repr(json.dumps(value)) + ',flush=True)'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        with mock.patch.object(links.subprocess, 'Popen', return_value=child), \
                mock.patch.object(links, 'handle') as handle, \
                mock.patch.object(self.power, 'stop', side_effect=lambda *args, **kwargs:
                                  kwargs['after_record'](mock.Mock(message='Stopped.'))) as stop:
            self.watcher._serve()
            self.join_handler()
            stop.assert_called_once_with(self.state, expected='fixture', until=value['created_at'] + 60,
                                         halted=self.watcher._halt.is_set, after_record=mock.ANY)
            handle.assert_not_called()
        self.assertEqual(self.answer(), {'ok': True, 'message': 'Stopped.'})

    def test_stop_request_for_another_codespace_is_refused(self):
        with mock.patch.object(self.power, 'stop') as stop:
            self.watcher._stop_request(self.packet(codespace='other-fixture'))
            self.join_handler()
            stop.assert_not_called()
        self.assertFalse(self.answer()['ok'])
        self.log.info.assert_called_once()

    def test_line_delivered_after_expiry_is_refused(self):
        self.clock = time.time()
        self.watcher._stop_request(self.packet(created_at=time.time() - 100))
        answer = self.answer()
        self.assertFalse(answer['ok'])
        self.assertIn('expired', answer['message'])
        self.assertNotIn('owner_stop_at', self.arming.read(self.state))
        self.fake.post_stop.assert_not_called()

    def test_request_from_the_future_is_refused(self):
        with mock.patch.object(self.power, 'stop') as stop:
            self.watcher._stop_request(self.packet(created_at=self.clock + 10))
            self.join_handler()
            stop.assert_not_called()
        self.assertFalse(self.answer()['ok'])

    def test_stop_expires_while_waiting_for_the_lock(self):
        from devcontainer_setup.project_sync.state import locked
        stamp = time.time() - 59.8
        self.clock = time.time()
        with locked(self.state / 'workspace-control.lock', blocking=True):
            self.watcher._stop_request(self.packet(created_at=stamp))
            self.join_handler()
        self.assertFalse(self.answer()['ok'])
        self.assertNotIn('owner_stop_at', self.arming.read(self.state))
        self.fake.post_stop.assert_not_called()

    def test_second_stop_while_one_runs_is_answered_busy(self):
        entered, release = threading.Event(), threading.Event()
        def stopping(*args, **kwargs):
            entered.set()
            if not release.wait(4):
                raise ValueError('fixture handler not released')
            kwargs['after_record'](mock.Mock(message='Stopped.'))
        with mock.patch.object(self.power, 'stop', side_effect=stopping) as stop:
            try:
                self.watcher._stop_request(self.packet(created_at=time.time()))
                self.assertTrue(entered.wait(2))
                self.watcher._stop_request(self.packet(id='100-2-abcdef01', created_at=time.time()))
                call = self.connection.python.call_args
                self.assertEqual(json.loads(call.args[1])['ok'], False)
                self.assertEqual(json.loads(call.args[1])['message'], 'A stop is already in progress.')
                stop.assert_called_once()
            finally:
                release.set()
                self.join_handler()

    def test_watcher_stop_joins_the_stop_handler(self):
        from devcontainer_setup.project_sync.state import locked
        attempted = threading.Event()
        @contextlib.contextmanager
        def observed(*args, **kwargs):
            attempted.set()
            with locked(*args, **kwargs) as lock:
                yield lock
        with locked(self.state / 'workspace-control.lock', blocking=True), \
                mock.patch.object(self.power, 'locked', side_effect=observed):
            self.clock = time.time()
            self.watcher._stop_request(self.packet())
            self.assertTrue(attempted.wait(3))
            self.watcher.stop()
            self.assertFalse(self.watcher._stopping.is_alive())
        self.assertNotIn('owner_stop_at', self.arming.read(self.state))
        self.fake.post_stop.assert_not_called()
        self.connection.python.assert_not_called()

    def test_failed_answer_is_logged_without_raising_or_leaking(self):
        self.connection.python.side_effect = ValueError('https://private.example/?token=PRIVATE-FIXTURE')
        self.watcher._stop_request(self.packet(created_at=time.time()))
        self.join_handler()
        self.assertTrue(self.connection.python.called)
        self.fake.post_stop.assert_called_once_with()
        self.assertNotIn('PRIVATE-FIXTURE', str(self.log.info.call_args_list))
        self.assertIn('answer', str(self.log.info.call_args_list).lower())


if __name__ == '__main__':
    unittest.main()
