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


if __name__ == '__main__':
    unittest.main()
