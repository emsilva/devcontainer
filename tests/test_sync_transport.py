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
            with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{}')):
                connection.python('print("{}")')
            self.assertEqual(stable.read_bytes(), b'existing validated binding')
            self.assertTrue((state / 'ssh-probe-config').is_file())

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
