"""Mac link handling without browser launches, SSH or callback requests."""
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote


def packet(url='https://web.plaud.ai/login?redirect_uri=http%3A%2F%2Flocalhost%3A8199%2Fauth%2Fcallback',
           **changes):
    value = dict(schema=1, id='100-42-abcd', url=url, created_at=100.0,
                 program='plaud', pid=42, start='1234', tty=None)
    value.update(changes)
    return json.dumps(value).encode()


class Child:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise AssertionError('child must be signalled before waiting')
        return self.returncode


class LinksTests(unittest.TestCase):
    def setUp(self):
        from devcontainer_setup.project_sync import links
        self.links = links
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state'
        self.connection = mock.Mock()
        self.connection.python.return_value = b'1'
        self.opened, self.notices, self.children, self.identities = [], [], [], {}

    @contextlib.contextmanager
    def forwards(self):
        m = self.links
        def popen(args, **kwargs):
            child = Child(4200 + len(self.children))
            self.children.append(child)
            self.identities[child.pid] = 'Mon Nov 2 00:00:00 2026 ' + ' '.join(args)
            return child
        def kill(pid, number):
            for child in self.children:
                if child.pid == pid:
                    child.returncode = -number
                    self.identities.pop(pid, None)
        def identity(pid):
            return 'owner-start owner-argv' if pid == os.getpid() else self.identities.get(pid)
        def run(args, **kwargs):
            if args[0] != 'lsof':
                raise AssertionError(args)
            return mock.Mock(returncode=0, stdout='COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n'
                             f'ssh {args[args.index("-p") + 1]} me 3u IPv4 0 0t0 TCP 127.0.0.1:8199 (LISTEN)\n')
        with mock.patch.object(m.subprocess, 'Popen', side_effect=popen) as launched, \
                mock.patch.object(m.subprocess, 'run', side_effect=run), \
                mock.patch.object(m, 'process_identity', side_effect=identity), \
                mock.patch.object(m.os, 'killpg', side_effect=kill) as killed, \
                mock.patch.object(m, 'check_port', return_value=(True, None)):
            forwards = m.Forwards(self.state, 'test-owner')
            try:
                yield forwards, launched, killed
            finally:
                for port in list(forwards.active()):
                    forwards.stop(port)

    def handle(self, request, forwards, **kwargs):
        return self.links.handle(request, self.connection, forwards, project='project-one',
                                 now=lambda: 101, opener=lambda url: self.opened.append(url) or True,
                                 notifier=lambda *args: self.notices.append(args), **kwargs)

    def test_parse_kinds_and_ports(self):
        for url, kind, port in (
                (json.loads(packet())['url'], 'signin', 8199),
                ('http://localhost:5173/', 'loopback', 5173),
                ('https://github.com/login/device', 'link', None),
                ('https://login.microsoftonline.com/auth?redirect_uri=http%3A%2F%2Flocalhost%3A38943', 'signin', 38943),
                ('https://auth.example/?redirect_uri=http%3A%2F%2F%5B%3A%3A1%5D%3A1455', 'signin', 1455),
                ('https://auth.example/?redirect_uri=HTTP%3A%2F%2Flocalhost%3A8199', 'signin', 8199),
                ('https://auth.example/?redirect_uri=http%3A%2F%2Flocalhost', 'link', None)):
            with self.subTest(url=url):
                request = self.links.parse_request(packet(url))
                self.assertEqual((request.kind, request.port), (kind, port))

    def test_parse_rejects(self):
        bad = [packet(url) for url in ('ftp://x/', 'file:///x', 'https://x/' + 'a' * 8183,
                                      'http://localhost:80/', 'https://x:70000/', 'https://x:abc/',
                                      'https://x/\nsecret', 'https://u:secret@x/',
                                      'https://x/?redirect_uri=http%3A%2F%2Flocalhost%3A80')]
        bad += [packet(schema=2), packet(created_at=float('nan')), packet(program='bad\nname'), b'{}', b'not json']
        for raw in bad:
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError) as caught:
                self.links.parse_request(raw)
            self.assertNotIn('secret', str(caught.exception))
            self.assertNotIn('https://', str(caught.exception))

    def test_non_loopback_redirect_opens_without_forward(self):
        for redirect in ('https://other.example:8199/callback', quote('http://localhost:8199/callback', safe='')):
            request = self.links.parse_request(packet('https://auth.example/?redirect_uri=' + quote(redirect, safe='')))
            with self.forwards() as (forwards, launched, _):
                self.assertEqual(self.handle(request, forwards), 'opened')
                self.assertIsNone(request.port)
                launched.assert_not_called()
                self.assertEqual(self.opened[-1], request.url)

    def test_deadlines(self):
        m = self.links
        self.assertEqual(m.deadline(m.parse_request(packet('https://github.com'))), 130)
        self.assertEqual(m.deadline(m.parse_request(packet())), 220)
        self.assertEqual(m.deadline(m.parse_request(packet(program='codex'))), 700)
        self.assertIsNone(m.deadline(m.parse_request(packet('http://localhost:5173/'))))
        with self.forwards() as (forwards, launched, _):
            request = m.parse_request(packet(created_at=-1000))
            self.assertEqual(self.handle(request, forwards), 'expired')
            self.assertEqual(self.opened, [])
            launched.assert_not_called()

    def test_busy_port_names_holder_and_remedy(self):
        with mock.patch.object(self.links.subprocess, 'run', return_value=mock.Mock(
                returncode=0, stdout='COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n'
                'plaud-mcp 4242 me 3u IPv4 0 0t0 TCP *:8199 (LISTEN)\n')):
            free, holder = self.links.check_port(8199)
        self.assertFalse(free)
        self.assertEqual(holder, 'plaud-mcp (pid 4242)')
        with self.forwards() as (forwards, launched, _), \
                mock.patch.object(self.links, 'check_port', return_value=(free, holder)):
            self.assertEqual(self.handle(self.links.parse_request(packet()), forwards), 'busy')
            launched.assert_not_called()
            text = str(self.notices)
            self.assertIn(holder, text)
            self.assertIn(self.links.REMEDIES['plaud'], text)
            self.assertEqual(self.opened, [])

    def test_wildcard_listener_counts_as_busy(self):
        for address in ('0.0.0.0', '::'):
            with self.subTest(address=address), mock.patch.object(self.links.socket, 'socket') as sock, \
                    mock.patch.object(self.links.subprocess, 'run', return_value=mock.Mock(
                        returncode=0, stdout=f'COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\nserver 4242 me 3u IPv6 0 0t0 TCP {address}:8199 (LISTEN)\n')):
                self.assertEqual(self.links.check_port(8199), (False, 'server (pid 4242)'))

    def test_forward_argv_and_proof(self):
        with self.forwards() as (forwards, launched, _):
            request = self.links.parse_request(packet())
            proof = []
            def run(args, **kwargs):
                self.assertEqual(args, ['lsof', '-a', '-p', '4200', '-nP', '-iTCP:8199', '-sTCP:LISTEN'])
                proof.append(True)
                return mock.Mock(returncode=0, stdout='ssh 4200 me LISTEN\n')
            with mock.patch.object(self.links.subprocess, 'run', side_effect=run):
                def opened(url):
                    self.assertTrue(proof)
                    return True
                self.assertEqual(self.links.handle(request, self.connection, forwards, project='p', now=lambda: 101,
                                                  opener=opened, notifier=lambda *args: None), 'forwarded')
            self.assertEqual(launched.call_args.args[0], [
                'ssh', '-F', str(self.state / 'ssh-config'), '-o', 'ClearAllForwardings=no',
                '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-N', '-L',
                '127.0.0.1:8199:127.0.0.1:8199', 'project-sync'])
            self.assertTrue(launched.call_args.kwargs['start_new_session'])
            record = json.loads((self.state / 'links-forwards.json').read_text())['forwards']['8199']
            self.assertEqual(record['pid'], 4200)
            self.assertEqual(record['process_identity'], self.identities[4200])
            self.assertEqual(record['owner'], 'test-owner')
            self.assertEqual(record['owner_pid'], os.getpid())
            self.assertEqual(record['owner_identity'], 'owner-start owner-argv')

    def test_forward_stops_when_remote_listener_goes(self):
        with self.forwards() as (forwards, _, killed), \
                mock.patch.object(self.links, 'remote_listening', side_effect=[True, True, False]):
            self.handle(self.links.parse_request(packet()), forwards)
            forwards.tend(self.connection, now=102)
            self.assertEqual(forwards.active(), {8199})
            forwards.tend(self.connection, now=104)
            self.assertEqual(forwards.active(), set())
            killed.assert_called_once_with(4200, signal.SIGTERM)
            self.assertEqual(json.loads((self.state / 'links-forwards.json').read_text())['forwards'], {})
            self.assertEqual(self.notices, [('project-one', 'plaud', 'web.plaud.ai', 'Port 8199')])

    def test_deadline_notifies_once_to_close_expired_page(self):
        with self.forwards() as (forwards, _, killed):
            self.handle(self.links.parse_request(packet()), forwards)
            self.notices.clear()
            with mock.patch.object(self.links, 'remote_listening', side_effect=AssertionError('expired port probed')):
                forwards.tend(self.connection, now=220)
                forwards.tend(self.connection, now=221)
            self.assertEqual(forwards.active(), set())
            killed.assert_called_once_with(4200, signal.SIGTERM)
            self.assertEqual(self.notices, [('project-one', 'plaud', 'web.plaud.ai',
                                            'Sign-in page expired; close its tab')])

    def test_forward_end_reason_is_logged(self):
        for reason in ('deadline', 'remote stopped listening', 'callback forward exited', 'stopped by owner'):
            with self.subTest(reason=reason), self.forwards() as (forwards, _, _), \
                    self.assertLogs(self.links._LOG, level='INFO') as logs:
                self.notices.clear()
                request = self.links.parse_request(packet())
                self.handle(request, forwards)
                if reason == 'callback forward exited':
                    self.children[-1].returncode = 0
                    forwards.active()
                elif reason == 'stopped by owner':
                    forwards.stop(request.port)
                    self.assertEqual(self.notices[-1][-1], 'Port 8199')
                    self.assertEqual(len(self.notices), 1)
                else:
                    with mock.patch.object(self.links, 'remote_listening', return_value=False):
                        forwards.tend(self.connection, now=221 if reason == 'deadline' else 102)
                forwards.tend(self.connection, now=1000)
                self.assertEqual(forwards.active(), set())
                ended = [line for line in logs.output if 'Callback forward ended:' in line]
                self.assertEqual(len(ended), 1)
                self.assertIn('plaud web.plaud.ai port 8199', ended[0])
                self.assertIn(reason, ended[0])

    def test_dead_forward_is_cleared_and_port_reusable(self):
        with self.forwards() as (forwards, launched, _):
            request = self.links.parse_request(packet())
            self.handle(request, forwards)
            self.children[0].returncode = 255
            self.identities.pop(4200)
            before = len(self.notices)
            self.assertEqual(forwards.active(), set())
            self.assertEqual(len(self.notices), before + 1)
            self.assertEqual(forwards.active(), set())
            self.assertEqual(len(self.notices), before + 1)
            self.assertEqual(json.loads((self.state / 'links-forwards.json').read_text())['forwards'], {})
            self.assertEqual(self.handle(request, forwards), 'forwarded')
            self.assertEqual(launched.call_count, 2)

    def test_end_left_behind_spares_live_owner(self):
        with self.forwards() as (forwards, _, killed):
            self.handle(self.links.parse_request(packet()), forwards)
            path = self.state / 'links-forwards.json'
            data = json.loads(path.read_text())
            record = data['forwards']['8199']
            record.update(owner='another', owner_pid=9999, owner_identity='live-owner')
            path.write_text(json.dumps(data))
            self.identities[9999] = 'live-owner'
            self.assertEqual(forwards.end_left_behind(), [])
            killed.assert_not_called()
            self.identities[9999] = 'reused-pid'
            self.assertEqual(forwards.end_left_behind(), [8199])
            killed.assert_called_once_with(4200, signal.SIGTERM)

    def test_second_request_same_port_reuses_forward(self):
        with self.forwards() as (forwards, launched, _):
            one = self.links.parse_request(packet())
            two = self.links.parse_request(packet(json.loads(packet())['url'].replace('8199', '1455')))
            self.handle(one, forwards)
            self.handle(one, forwards)
            self.handle(two, forwards)
            self.assertEqual(launched.call_count, 2)
            self.assertEqual(forwards.active(), {1455, 8199})
            self.assertEqual(len(self.opened), 3)

    def test_start_preserves_another_owners_live_record(self):
        with self.forwards() as (forwards, _, _):
            self.handle(self.links.parse_request(packet()), forwards)
            path = self.state / 'links-forwards.json'
            before = path.read_bytes()
            other = self.links.Forwards(self.state, 'other-owner')
            with self.assertRaises(ValueError):
                other.start(8199)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(forwards.active(), {8199})
            self.assertEqual(other.active(), set())

    def test_stop_reaps_when_group_exits_between_poll_and_signal(self):
        with self.forwards() as (forwards, _, _):
            self.handle(self.links.parse_request(packet()), forwards)
            child = self.children[0]
            def gone(*args):
                child.returncode = 0
                raise PermissionError
            with mock.patch.object(self.links.os, 'killpg', side_effect=gone):
                forwards.stop(8199)
            self.assertEqual(forwards.active(), set())
            self.assertEqual(json.loads((self.state / 'links-forwards.json').read_text())['forwards'], {})

    def test_busy_message_goes_to_tty_only_for_non_self_showing(self):
        for program in ('plaud', 'other', 'codex', 'claude', 'opencode', 'gh', 'az'):
            with self.subTest(program=program), self.forwards() as (forwards, _, _), \
                    mock.patch.object(self.links, 'check_port', return_value=(False, 'server (pid 4242)')):
                self.connection.reset_mock()
                request = self.links.parse_request(packet(program=program, tty='/dev/pts/7'))
                self.assertEqual(self.handle(request, forwards), 'busy')
                if program in ('plaud', 'other'):
                    payload = json.loads(self.connection.python.call_args.args[1])
                    self.assertEqual(payload['tty'], '/dev/pts/7')
                    self.assertIn('server (pid 4242)', payload['text'])
                    self.assertNotIn(request.url, payload['text'])
                else:
                    self.connection.python.assert_not_called()

    def test_logs_and_notices_never_contain_url(self):
        with self.forwards() as (forwards, _, _), self.assertLogs(self.links._LOG, level='INFO') as logs:
            request = self.links.parse_request(packet(json.loads(packet())['url'] + '&state=SECRET-QUERY'))
            self.handle(request, forwards)
            self.assertNotIn('SECRET-QUERY', str(logs.output) + str(self.notices))
            self.assertNotIn('redirect_uri=', str(logs.output) + str(self.notices))

    def test_remote_listening_reads_proc_without_contacting_callback(self):
        self.connection.python.return_value = b'1'
        self.assertTrue(self.links.remote_listening(self.connection, 8199))
        code, payload = self.connection.python.call_args.args
        self.assertIn('/proc/net/tcp', code)
        self.assertNotIn('socket', code)
        self.assertEqual(json.loads(payload)['port'], 8199)
        self.connection.python.return_value = b'0'
        self.assertFalse(self.links.remote_listening(self.connection, 8199))

    def test_open_and_notify_use_argv_and_ignore_notification_failures(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        url = 'https://example.com/?x=$(touch%20BAD)'
        self.assertTrue(self.links.open_url(url, run=run))
        self.assertEqual(run.call_args.args[0], ['/usr/bin/open', url])
        self.assertNotIn('shell', run.call_args.kwargs)
        with mock.patch.object(self.links.notify, 'notify') as mac, \
                mock.patch.object(self.links.subprocess, 'run', return_value=mock.Mock(returncode=0)) as herdr:
            self.links.notify_link('project-one', 'plaud', 'web.plaud.ai', 'Sign-in page expired; close its tab')
            mac.assert_called_once_with('project-one: plaud', 'web.plaud.ai — Sign-in page expired; close its tab')
            self.assertEqual(herdr.call_args_list[0].args[0], ['herdr', 'status'])
            self.assertEqual(herdr.call_args_list[1].args[0][:3], ['herdr', 'notification', 'show'])
        with mock.patch.object(self.links.notify, 'notify', side_effect=OSError), \
                mock.patch.object(self.links.subprocess, 'run', side_effect=OSError):
            self.links.notify_link('project-one', 'plaud', 'web.plaud.ai')


if __name__ == '__main__':
    unittest.main()
