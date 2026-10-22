"""No-start boundary: real gh is constrained by an authenticated adapter."""
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class TransportTests(unittest.TestCase):
    def api(self, states=None):
        transport = importlib.import_module('devcontainer_setup.project_sync.transport')
        self.calls = []
        states = iter(states or ['Shutdown'])
        def upstream(url, token):
            self.calls.append((url, token))
            return 200, json.dumps({'name': 'bound-name', 'state': next(states)}).encode()
        return transport.ReadOnlyAPI('bound-name', 'real-secret', upstream=upstream)

    def request(self, api, path, method='GET', token=None):
        request = Request(api.url + path, method=method,
                          headers={'Authorization': 'token ' + (token or api.child_token)})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def test_exact_route_auth_and_mutations_denied_locally(self):
        with self.api() as api:
            for path, method, token in [('/user/codespaces/other', 'GET', None),
                    ('/user/codespaces/bound-name/start', 'POST', None),
                    ('/user/codespaces/bound-name', 'GET', 'bad'),
                    ('/user/codespaces/bound-name?x=1', 'GET', None),
                    ('/user/codespaces/%62ound-name', 'GET', None)]:
                self.assertIn(self.request(api, path, method, token)[0], (403, 404, 405))
            self.assertEqual(self.calls, [])
            self.assertEqual(self.request(api, '/user/codespaces/bound-name?internal=true&refresh=true')[0], 200)
            self.assertEqual(self.calls, [('https://api.github.com/user/codespaces/bound-name?internal=true&refresh=true', 'real-secret')])
            self.assertNotEqual(api.child_token, 'real-secret')

    @unittest.skipUnless(shutil.which('gh'), 'requires actual gh offline compatibility gate')
    def test_actual_gh_shutdown_and_available_shutdown_race_never_post_upstream(self):
        for states in (['Shutdown'], ['Available', 'Shutdown']):
            with self.subTest(states=states), tempfile.TemporaryDirectory() as gh_home, self.api(states) as api:
                if states[0] == 'Available':
                    self.assertIn(b'Available', self.request(api, '/user/codespaces/bound-name')[1])
                env = api.child_env({'PATH': os.environ['PATH'], 'HOME': gh_home, 'GH_TOKEN': 'real-secret', 'GITHUB_TOKEN': 'other-secret', 'GH_DEBUG': 'api'})
                self.assertNotIn('real-secret', repr(env))
                self.assertNotIn('other-secret', repr(env))
                result = subprocess.run(['gh', 'codespace', 'ports', 'forward', '2222:0', '-c', 'bound-name'], env=env, capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.calls), len(states))
                self.assertTrue(all('/start' not in call[0] for call in self.calls))
                self.assertNotIn(b'real-secret', result.stdout + result.stderr)
                self.assertTrue(any(method == 'POST' for method, _ in api.denied))

    @unittest.skipUnless(shutil.which('gh'), 'requires actual gh offline compatibility gate')
    def test_actual_gh_keepalive_argv_never_posts_start(self):
        keepalive = importlib.import_module('devcontainer_setup.project_sync.keepalive')
        with tempfile.TemporaryDirectory() as gh_home, self.api(['Shutdown']) as api:
            key = Path(gh_home) / 'codespaces.auto'
            key.write_text('fixture key')
            # A dead proxy keeps any request gh makes beyond the loopback adapter on this Mac.
            env = api.child_env({'PATH': os.environ['PATH'], 'HOME': gh_home, 'GH_TOKEN': 'real-secret',
                                 'HTTPS_PROXY': 'http://127.0.0.1:9', 'HTTP_PROXY': 'http://127.0.0.1:9'})
            self.assertNotIn('real-secret', repr(env))
            result = subprocess.run(keepalive.argv('bound-name', key, [(13570, 3570)]), env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(self.calls)
            self.assertTrue(all('/start' not in call[0] for call in self.calls))
            self.assertTrue(any(method == 'POST' and path.endswith('/start') for method, path in api.denied))
            self.assertNotIn(b'real-secret', result.stdout + result.stderr)

    def test_mismatched_remote_name_is_refused(self):
        module = importlib.import_module('devcontainer_setup.project_sync.transport')
        with module.ReadOnlyAPI('bound-name', 'secret', upstream=lambda *_: (200, b'{"name":"other","state":"Available"}')) as api:
            self.assertEqual(self.request(api, '/user/codespaces/bound-name')[0], 502)

    def test_invalid_names_missing_key_and_gh_version_fail_before_network(self):
        module = importlib.import_module('devcontainer_setup.project_sync.transport')
        with self.assertRaises(ValueError):
            module.ReadOnlyAPI('../other', 'secret')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'SSH key'):
                with module.CodespaceConnection('bound-name', state=Path(tmp), env={'HOME': tmp, 'PATH': os.environ['PATH']}):
                    self.fail('must not connect without a key')

    def test_wrong_url_capability_is_denied_without_authorization(self):
        with self.api() as api:
            url = api.url.rsplit('/', 1)[0] + '/wrong/user/codespaces/bound-name'
            with self.assertRaises(HTTPError) as caught:
                urlopen(url, timeout=3)
            caught.exception.close()
            self.assertEqual(caught.exception.code, 403)
            self.assertEqual(self.calls, [])

    def test_identity_probe_never_rebinds_live_mutagen_configuration(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        module = importlib.import_module('devcontainer_setup.project_sync.transport')
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp).resolve()
            stable = state / 'ssh-config'
            stable.write_bytes(b'existing validated binding')
            connection = module.CodespaceConnection('bound-name', state=state)
            connection.port = 12345
            connection.key = state / 'key'
            with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{}')) as run:
                connection.python('print("{}")')
            self.assertEqual(stable.read_bytes(), b'existing validated binding')
            self.assertTrue(Path(run.call_args.args[0][2]).is_file())

    def test_remote_python_ignores_checkout_and_python_environment_before_bootstrap(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        module = importlib.import_module('devcontainer_setup.project_sync.transport')
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp).resolve()
            connection = module.CodespaceConnection('bound-name', state=state)
            connection.port = 12345
            connection.key = state / 'key'
            with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{}')) as run:
                connection.python('print("{}")')
            self.assertTrue(run.call_args.args[0][-1].startswith('python3 -I -B -c '))

    def test_github_token_timeout_is_actionable_and_private(self):
        from unittest.mock import patch
        from devcontainer_setup.project_sync import transport
        error = subprocess.TimeoutExpired(['gh', 'private-token-detail'], 20, output='secret-output')
        with patch.object(transport.subprocess, 'run', side_effect=error) as run:
            with self.assertRaisesRegex(ValueError, 'authentication timed out') as caught:
                transport.github_token({})
        self.assertNotIn('private-token-detail', str(caught.exception))
        self.assertNotIn('secret-output', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(run.call_count, 1)

    def test_gh_version_timeout_is_actionable_before_connection(self):
        from unittest.mock import patch
        from devcontainer_setup.project_sync import transport
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / '.ssh').mkdir()
            (home / '.ssh/codespaces.auto').touch()
            connection = transport.CodespaceConnection('bound-name', state=home / 'state', env={'HOME': tmp})
            with patch.object(transport.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private-command', 10)) as run:
                with self.assertRaisesRegex(ValueError, 'GitHub CLI check timed out'):
                    connection.__enter__()
            self.assertEqual(run.call_count, 1)
            self.assertIsNone(connection.api)
            self.assertIsNone(connection.child)

    def test_python_timeout_is_actionable_and_context_cleans_connection(self):
        from unittest.mock import patch, Mock, MagicMock
        from devcontainer_setup.project_sync import transport
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / '.ssh').mkdir()
            (home / '.ssh/codespaces.auto').touch()
            connection = transport.CodespaceConnection('bound-name', state=home / 'state', env={'HOME': tmp, 'GH_TOKEN': 'fixture'})
            api = MagicMock()
            child = Mock()
            child.poll.return_value = None
            version = Mock(stdout='gh version ' + transport.SUPPORTED_GH + ' (fixture)')
            timeout = subprocess.TimeoutExpired('private-python-code', 60, stderr=b'private-stderr')
            with patch.object(transport, 'ReadOnlyAPI', return_value=api), patch.object(transport.subprocess, 'Popen', return_value=child), patch.object(transport.socket, 'create_connection'), patch.object(transport.subprocess, 'run', side_effect=[version, timeout]) as run:
                with self.assertRaisesRegex(ValueError, 'Remote Python/SSH timed out') as caught:
                    with connection:
                        connection._ready = True  # This gate isolates the post-readiness RPC timeout.
                        connection.python('private-python-code')
            self.assertNotIn('private-', str(caught.exception))
            self.assertEqual(run.call_count, 2)
            child.terminate.assert_called_once()
            api.__exit__.assert_called_once()
            self.assertIsNone(connection.api)

    def test_failed_connection_entry_closes_read_only_api(self):
        from unittest.mock import patch, Mock, MagicMock
        from devcontainer_setup.project_sync import transport
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / '.ssh').mkdir()
            (home / '.ssh/codespaces.auto').touch()
            connection = transport.CodespaceConnection('bound-name', state=home / 'state', env={'HOME': tmp, 'GH_TOKEN': 'fixture'})
            api = MagicMock()
            version = Mock(stdout='gh version ' + transport.SUPPORTED_GH + ' (fixture)')
            with patch.object(transport, 'ReadOnlyAPI', return_value=api), patch.object(transport.subprocess, 'run', return_value=version), patch.object(transport.subprocess, 'Popen', side_effect=OSError('fixture forward unavailable')):
                with self.assertRaises(OSError):
                    connection.__enter__()
            api.__exit__.assert_called_once()
            self.assertIsNone(connection.api)


class ConnectionProbeOwnershipTests(unittest.TestCase):
    def setUp(self):
        from devcontainer_setup.project_sync import transport
        self.transport = transport
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve()
        self.first = self.connection(11111)
        self.second = self.connection(22222)

    def connection(self, port):
        connection = self.transport.CodespaceConnection('bound-name', state=self.state)
        connection.port = port
        connection.key = self.state / 'key'
        connection._ready = True
        self.addCleanup(connection.__exit__)
        return connection

    @staticmethod
    def success():
        return subprocess.CompletedProcess(['ssh'], 0, b'{}', b'')

    def test_sequential_connections_preserve_each_others_probe_config(self):
        from unittest.mock import patch
        with patch.object(self.transport.subprocess, 'run', return_value=self.success()) as run:
            self.first.python('first()')
            first_path = Path(run.call_args.args[0][2])
            first_config = first_path.read_bytes()
            self.second.python('second()')
            second_path = Path(run.call_args.args[0][2])
        self.assertEqual(first_path.read_bytes(), first_config)
        self.assertNotEqual(first_path, second_path)
        self.assertIn('  Port 11111\n', first_path.read_text())
        self.assertIn('  Port 22222\n', second_path.read_text())
        self.assertEqual(first_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(second_path.stat().st_mode & 0o777, 0o600)

    def test_nested_connection_cannot_retarget_in_flight_python(self):
        from unittest.mock import patch
        observed = []
        def run(args, **kwargs):
            path = Path(args[2])
            if not observed:
                observed.append(path)
                self.second.python('second()')
                self.assertIn('  Port 11111\n', path.read_text())
            else:
                observed.append(path)
                self.assertIn('  Port 22222\n', path.read_text())
            return self.success()
        with patch.object(self.transport.subprocess, 'run', side_effect=run):
            self.first.python('first()')
        self.assertEqual(len(observed), 2)
        self.assertNotEqual(*observed)

    def test_exit_removes_only_owned_probe_and_preserves_other_state(self):
        from unittest.mock import patch
        known_hosts = self.state / 'known_hosts'
        stable = self.state / 'ssh-config'
        known_hosts.write_bytes(b'known-host fixture')
        stable.write_bytes(b'validated binding fixture')
        with patch.object(self.transport.subprocess, 'run', return_value=self.success()) as run:
            self.first.python('first()')
            first_path = Path(run.call_args.args[0][2])
            self.second.python('second()')
            second_path = Path(run.call_args.args[0][2])
            self.first.__exit__()
            self.first.__exit__()  # Closing an already closed owner is safe.
            self.assertFalse(first_path.exists())
            self.assertTrue(second_path.is_file())
            self.second.python('still_connected()')
            self.assertIn('  Port 22222\n', second_path.read_text())
        self.assertEqual(known_hosts.read_bytes(), b'known-host fixture')
        self.assertEqual(stable.read_bytes(), b'validated binding fixture')
        self.second.__exit__()
        self.assertFalse(second_path.exists())

    def test_failed_reentry_removes_owned_probe_without_deleting_live_peer(self):
        from unittest.mock import MagicMock, Mock, patch
        home = self.state / 'home'
        (home / '.ssh').mkdir(parents=True)
        (home / '.ssh/codespaces.auto').touch()
        self.first.env.update(HOME=str(home), GH_TOKEN='fixture')
        with patch.object(self.transport.subprocess, 'run', return_value=self.success()) as run:
            self.first.python('first()')
            first_path = Path(run.call_args.args[0][2])
            self.second.python('second()')
            second_path = Path(run.call_args.args[0][2])
        api = MagicMock()
        version = Mock(stdout='gh version ' + self.transport.SUPPORTED_GH + ' (fixture)')
        with patch.object(self.transport, 'ReadOnlyAPI', return_value=api), \
                patch.object(self.transport.subprocess, 'run', return_value=version), \
                patch.object(self.transport.subprocess, 'Popen', side_effect=OSError('fixture forward unavailable')):
            with self.assertRaises(OSError):
                self.first.__enter__()
        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.is_file())
        self.assertIn('  Port 22222\n', second_path.read_text())
        api.__exit__.assert_called_once()


class SSHReadinessTests(unittest.TestCase):
    def setUp(self):
        from devcontainer_setup.project_sync import transport
        self.transport = transport
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.connection = transport.CodespaceConnection('bound-name', state=Path(temporary.name).resolve())
        self.connection.port = 12345
        self.connection.key = self.connection.state / 'key'

    @staticmethod
    def result(code=0, stdout=b'', stderr=b''):
        return subprocess.CompletedProcess(['ssh'], code, stdout, stderr)

    def test_open_forward_waits_for_ssh_then_sends_each_payload_once(self):
        from unittest.mock import patch
        responses = [self.result(255, stderr=b'Connection reset by peer'),
                     self.result(255, stderr=b'connect to host: Connection refused'),
                     self.result(), self.result(stdout=b'first'), self.result(stdout=b'second')]
        with patch.object(self.transport.subprocess, 'run', side_effect=responses) as run, \
                patch.object(self.transport.time, 'sleep'):
            self.assertEqual(self.connection.python('write_first()', b'first payload'), b'first')
            self.assertEqual(self.connection.python('write_second()', b'second payload'), b'second')
        calls = run.call_args_list
        self.assertEqual([c.args[0][-1] for c in calls[:3]], ['true'] * 3)
        self.assertEqual([c.kwargs['input'] for c in calls[3:]], [b'first payload', b'second payload'])
        self.assertEqual(len(calls), 5)

    def test_authentication_and_host_key_errors_are_immediate_and_private(self):
        from unittest.mock import patch
        cases = [(b'Permission denied (publickey). private-detail', 'authentication failed'),
                 (b'Host key verification failed. private-detail', 'host key verification failed'),
                 (b'REMOTE HOST IDENTIFICATION HAS CHANGED private-detail', 'host key verification failed')]
        for stderr, message in cases:
            with self.subTest(message=message), \
                    patch.object(self.transport.subprocess, 'run', return_value=self.result(255, stderr=stderr)) as run, \
                    patch.object(self.transport.time, 'sleep') as sleep:
                with self.assertRaisesRegex(ValueError, message) as caught:
                    self.connection.python('private_operation()', b'private payload')
                self.assertNotIn('private', str(caught.exception))
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][-1], 'true')
                sleep.assert_not_called()

    def test_ssh_readiness_timeout_is_bounded_without_running_operation(self):
        from unittest.mock import patch
        now = [0]
        def timeout(*args, **kwargs):
            now[0] = 46
            raise subprocess.TimeoutExpired('private command', kwargs['timeout'], stderr=b'private stderr')
        with patch.object(self.transport.subprocess, 'run', side_effect=timeout) as run, \
                patch.object(self.transport.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(self.transport.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'SSH.*ready.*45') as caught:
                self.connection.python('private_operation()', b'private payload')
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], 'true')
        self.assertLessEqual(run.call_args.kwargs['timeout'], 45)

    def test_connection_drop_after_remote_operation_is_never_retried(self):
        from unittest.mock import patch
        with patch.object(self.transport.subprocess, 'run', side_effect=[
                self.result(), self.result(255, stderr=b'Connection reset by peer')]) as run:
            with self.assertRaisesRegex(ValueError, 'SSH connection'):
                self.connection.python('write_data()', b'unique payload')
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs['input'], b'unique payload')

    def test_remote_python_exit_reports_command_failure_without_ssh_advice(self):
        from unittest.mock import patch
        with patch.object(self.transport.subprocess, 'run', side_effect=[
                self.result(), self.result(1, stderr=b'private traceback')]) as run:
            with self.assertRaisesRegex(ValueError, 'Remote Python command failed.*1') as caught:
                self.connection.python('raise ValueError("private")')
        self.assertNotIn('private', str(caught.exception))
        self.assertNotIn('sshd', str(caught.exception))
        self.assertEqual(run.call_count, 2)
