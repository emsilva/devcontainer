"""Native SSH proxy byte streams, child cleanup, and read-only capability."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from devcontainer_setup.project_sync import transport
from devcontainer_setup.project_sync import ssh_hosts
from devcontainer_setup.project_sync.state import atomic_write


class SSHProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'home with spaces'
        self.home.mkdir()
        self.state = self.home / 'state'
        atomic_write(self.state / 'scratch.json', b'{"codespace":"fixture"}')
        (self.home / '.ssh').mkdir(mode=0o700)
        (self.home / '.ssh/codespaces.auto').write_text('fixture key')
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        gh.write_text('#!' + sys.executable + '\n' + '''import json,os,pathlib,sys
s=pathlib.Path(os.environ["TEST_STATE"])
if sys.argv[1:]==["--version"]:
 print("gh version "+os.environ.get("TEST_GH_VERSION","2.102.0")+" (fixture)")
else:
 (s/"child.json").write_text(json.dumps({"pid":os.getpid(),"args":sys.argv[1:],"private_token_leaked":os.environ.get("GH_TOKEN")=="real-private-token","api":os.environ.get("GITHUB_API_URL","").startswith("http://127.0.0.1:")}))
 if os.environ.get("TEST_MODE")=="fail":print("private-error-detail",file=sys.stderr);sys.exit(2)
 while True:
  data=os.read(0,4096)
  if not data:break
  os.write(1,data)
''')
        gh.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        GH_TOKEN='real-private-token', TEST_STATE=str(self.state))

    def command(self):
        self.assertTrue(callable(getattr(transport, 'herdr_stdio', None)), 'Herdr needs a native read-only SSH proxy')
        root = Path(__file__).resolve().parents[1]
        code = ('import sys;sys.path.insert(0,' + repr(str(root)) + ');'
                'from devcontainer_setup.project_sync.transport import herdr_stdio;'
                'raise SystemExit(herdr_stdio("fixture",sys.argv[1]))')
        return [sys.executable, '-I', '-B', '-c', code, str(self.state)]

    def test_eof_preserves_binary_stdio_and_reaps_native_child(self):
        payload = b'SSH-2.0-fixture\r\n' + bytes(range(256)) * 20
        result = subprocess.run(self.command(), env=self.env, input=payload, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b'')
        child = json.loads((self.state / 'child.json').read_text())
        self.assertEqual(child['args'], ['codespace', 'ssh', '-c', 'fixture', '--stdio', '--', '-i', str(self.home / '.ssh/codespaces.auto')])
        self.assertFalse(child['private_token_leaked'])
        self.assertTrue(child['api'])
        with self.assertRaises(ProcessLookupError): os.kill(child['pid'], 0)

    def test_termination_reaps_native_child_without_logging_private_errors(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                record = self.state / 'child.json'
                record.unlink(missing_ok=True)
                process = subprocess.Popen(self.command(), env=self.env, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while not record.exists() and time.monotonic() < deadline: time.sleep(.02)
                    self.assertTrue(record.exists())
                    child = json.loads(record.read_text())
                    process.send_signal(sig)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + sig)
                    self.assertEqual(stdout, b'')
                    self.assertNotIn(b'real-private-token', stderr)
                    with self.assertRaises(ProcessLookupError): os.kill(child['pid'], 0)
                finally:
                    if process.poll() is None: process.kill(); process.communicate()

    def test_unsupported_cli_never_opens_native_ssh(self):
        result = subprocess.run(self.command(), env=dict(self.env, TEST_GH_VERSION='0.0.0'),
                                input=b'', capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / 'child.json').exists())
        self.assertEqual(result.stdout, b'')

    def test_native_failure_keeps_stdout_clean_and_stderr_private(self):
        result = subprocess.run(self.command(), env=dict(self.env, TEST_MODE='fail'),
                                input=b'', capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(b'private-error-detail', result.stderr)

    def test_actual_openssh_proxy_command_keeps_shell_arguments_and_spaces(self):
        connection = transport.CodespaceConnection('fixture', state=self.state, env=self.env)
        connection.key, connection.port = self.home / '.ssh/codespaces.auto', 31000
        connection.ssh_config(self.state / 'ssh-config')
        config = self.state / 'alias-config'
        with mock.patch.dict(os.environ, self.env, clear=True):
            config.write_bytes(ssh_hosts._alias_config(self.state, 'fixture'))
        resolved = subprocess.check_output(['ssh', '-G', '-F', str(config), 'fixture'], text=True)
        proxy = next(line.removeprefix('proxycommand ') for line in resolved.splitlines()
                     if line.startswith('proxycommand '))
        payload = b'SSH-2.0-spaces\r\n\x00\xff'
        result = subprocess.run(['/bin/sh', '-c', 'exec ' + proxy], env=self.env,
                                input=payload, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, payload)

    @unittest.skipUnless(shutil.which('gh'), 'requires native gh offline compatibility gate')
    def test_real_native_ssh_cannot_start_a_stopped_codespace(self):
        self.assertTrue(callable(getattr(transport, 'herdr_stdio', None)))
        calls, adapters = [], []
        original_api = transport.ReadOnlyAPI
        def upstream(url, token):
            calls.append(url)
            return 200, b'{"name":"fixture","state":"Shutdown"}'
        def api(name, token):
            adapter = original_api(name, token, upstream=upstream)
            adapters.append(adapter)
            return adapter
        with mock.patch.dict(os.environ, dict(self.env, PATH=os.environ['PATH']), clear=True), \
                mock.patch.object(transport, 'ReadOnlyAPI', side_effect=api):
            self.assertNotEqual(transport.herdr_stdio('fixture', self.state), 0)
        self.assertTrue(calls)
        self.assertFalse(any('/start' in url for url in calls))
        self.assertTrue(any(method == 'POST' for method, _ in adapters[0].denied))
