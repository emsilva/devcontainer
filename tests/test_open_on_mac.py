"""The Codespace opener uses an isolated queue and fake Linux process tree."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'templates/tools/open-on-mac'
URL = 'https://example.invalid/login?state=fixture'


class OpenOnMacTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.env = dict(os.environ, DEVCONTAINER_LINKS_STATE=str(self.state),
                        DEVCONTAINER_LINKS_PROC=str(self.proc))
        self.env.pop('BROWSER', None)

    def module(self):
        self.assertTrue(HELPER.is_file(), 'templates/tools/open-on-mac is missing')
        loader = importlib.machinery.SourceFileLoader('open_on_mac', str(HELPER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def run_helper(self, url=URL, path=HELPER, **env):
        self.assertTrue(HELPER.is_file(), 'templates/tools/open-on-mac is missing')
        # Startup hang guard; test_finishes_within_a_second pins the speed promise.
        return subprocess.run([sys.executable, str(path), url], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=30)

    def heartbeat(self, age=0):
        self.state.mkdir(exist_ok=True)
        heartbeat = self.state / 'mac-listening'
        heartbeat.touch()
        stamp = time.time() - age
        os.utime(heartbeat, (stamp, stamp))

    def process(self, pid, parent, argv, tty=None):
        process = self.proc / str(pid)
        (process / 'fd').mkdir(parents=True)
        # Fields 3 through 22, with a command containing spaces and parentheses.
        fields = ['S', str(parent), *(['0'] * 17), '987654']
        (process / 'stat').write_text(f'{pid} (a tricky (name)) ' + ' '.join(fields))
        (process / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in argv) + b'\0')
        for fd in (0, 1, 2):
            (process / 'fd' / str(fd)).symlink_to(tty if tty is not None and fd == 2 else '/dev/null')

    def request(self):
        files = list((self.state / 'requests').glob('*.json'))
        self.assertEqual(len(files), 1)
        return files[0], json.loads(files[0].read_text())

    def test_writes_one_request_mode_600_and_exits_0_when_listening(self):
        self.heartbeat()
        self.process(os.getpid(), 0, ['codex', 'login'])
        result = self.run_helper()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        path, request = self.request()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for directory in (self.state, self.state / 'requests', self.state / 'claimed'):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(set(request), {'schema', 'id', 'url', 'created_at', 'program', 'pid', 'start', 'tty'})
        self.assertEqual(request['schema'], 1)
        self.assertEqual(request['url'], URL)
        self.assertEqual(request['program'], 'codex')
        self.assertEqual(request['pid'], os.getpid())
        self.assertEqual(request['start'], '987654')
        self.assertIsNone(request['tty'])
        self.assertIsInstance(request['created_at'], float)
        self.assertLess(abs(time.time() - request['created_at']), 2)
        self.assertRegex(request['id'], r'^\d+-\d+-[0-9a-f]{4}$')
        self.assertEqual(path.stem, request['id'])
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])
        self.assertEqual(list((self.state / 'claimed').iterdir()), [])

    def no_mac(self, argv):
        module = self.module()
        self.heartbeat(age=20)
        tty = self.root / 'tty'
        tty.touch()
        self.process(101, 202, ['sh'])
        self.process(202, 0, argv, tty)
        real_readlink = os.readlink

        def readlink(path, *args, **kwargs):
            target = real_readlink(path, *args, **kwargs)
            return '/dev/pts/7' if str(target) == str(tty) else target

        with patch.object(module.os, 'getppid', return_value=101), \
                patch.object(module.os, 'readlink', side_effect=readlink):
            code = module.main([str(HELPER), URL], self.env)
        self.assertEqual(code, 1)
        _, request = self.request()
        self.assertEqual((request['pid'], request['start'], request['tty']), (202, '987654', '/dev/pts/7'))
        return tty.read_bytes(), request

    def test_exits_1_and_writes_link_to_tty_for_plaud_when_no_mac(self):
        output, request = self.no_mac(['node', '/home/vscode/.local/bin/plaud', 'login'])
        self.assertEqual(request['program'], 'plaud')
        self.assertEqual(output, ('\r\nOpen this link on your Mac:\n' + URL + '\r\n').encode())

    def test_never_writes_to_self_showing_programs(self):
        for program in ('claude', 'codex', 'opencode', 'gh', 'az'):
            with self.subTest(program=program):
                # Each requester gets its own isolated queue and proc tree.
                self.state = self.root / ('state-' + program)
                self.proc = self.root / ('proc-' + program)
                self.proc.mkdir()
                self.env.update(DEVCONTAINER_LINKS_STATE=str(self.state), DEVCONTAINER_LINKS_PROC=str(self.proc))
                output, request = self.no_mac([program, 'login'])
                self.assertEqual(request['program'], program)
                self.assertEqual(output, b'')

    def test_rejects_non_http_without_writing(self):
        for url in ('file:///etc/passwd', 'javascript:x'):
            with self.subTest(url=url):
                result = self.run_helper(url)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, '', ''))
                self.assertFalse(self.state.exists())

    def test_rejects_invalid_http_without_writing(self):
        for url in ('', 'https://', 'https://example.invalid:80/', 'http://localhost:70000/',
                    'https://example.invalid/' + 'é' * 4090):
            with self.subTest(url_length=len(url)):
                result = self.run_helper(url)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, '', ''))
                self.assertFalse(self.state.exists())

    def test_program_name_rules(self):
        module = self.module()
        for argv, expected in [(['node', '/x/plaud', 'login'], 'plaud'),
                               (['/opt/az/bin/python3', '-Im', 'azure.cli', 'login'], 'az'),
                               (['codex', 'login'], 'codex'), (['/usr/bin/gh', 'auth'], 'gh'),
                               (['bun', '/x/opencode'], 'opencode'), (['python', '/x/tool'], 'tool')]:
            with self.subTest(argv=argv):
                self.assertEqual(module.program_name(argv), expected)

    def test_xdg_open_defers_to_foreign_browser(self):
        stub = self.root / 'browser'
        output = self.root / 'browser-argument'
        stub.write_text('#!/bin/sh\nprintf "%s" "$1" > "$TEST_BROWSER_ARGUMENT"\n')
        stub.chmod(0o755)
        xdg = self.root / 'xdg-open'
        xdg.symlink_to(HELPER)
        result = self.run_helper(path=xdg, BROWSER=str(stub), TEST_BROWSER_ARGUMENT=str(output))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertEqual(output.read_text(), URL)
        self.assertFalse(self.state.exists())

    def test_finishes_within_a_second(self):
        self.heartbeat()
        requests = self.state / 'requests'
        requests.mkdir()
        for i in range(50):
            (requests / f'{i}.json').write_text('{}')
        started = time.monotonic()
        result = self.run_helper()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(list(requests.glob('*.json'))), 51)
