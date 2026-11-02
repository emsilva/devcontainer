import importlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.signins = importlib.import_module('devcontainer_setup.project_sync.signins')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name).resolve()

    def test_tools_order_and_argv(self):
        expected = [
            ('plaud', 'Plaud', ('plaud', 'me'), ('plaud', 'login'), 150),
            ('codex', 'Codex', ('codex', 'login', 'status'), ('codex', 'login'), 600),
            ('claude', 'Claude', ('claude', 'auth', 'status', '--json'),
             ('claude', 'auth', 'login', '--claudeai'), 600),
            ('opencode', 'OpenCode', ('opencode', 'auth', 'list', '--format', 'json'),
             ('opencode', 'auth', 'login', 'openai', '--method', 'chatgpt-token-sharing'), 600),
            ('grok', 'Grok', ('grok', 'models'), ('grok', 'login', '--device-auth'), 600),
        ]
        self.assertEqual([(t.key, t.label, t.status, t.login, t.deadline) for t in self.signins.TOOLS], expected)
        self.assertEqual([t.binary for t in self.signins.TOOLS], [row[0] for row in expected])
        self.assertNotIn('--email', self.signins.TOOLS[2].login)

    def test_classify_table(self):
        rows = [
            ('plaud', 0, '', '', 'signed-in', None),
            ('plaud', 2, '', '', 'signed-out', None),
            ('plaud', 3, '', '', 'unknown', None),
            ('codex', 0, '', 'Logged in using ChatGPT\n', 'signed-in', None),
            ('codex', 1, '', 'Not logged in\n', 'signed-out', None),
            ('codex', 2, '', 'Not logged in\n', 'unknown', None),
            ('claude', 0, '{"loggedIn":true,"authMethod":"claude.ai","email":"a@b.c"}', '', 'signed-in', 'a@b.c'),
            ('claude', 1, '{"loggedIn":false}', '', 'signed-out', None),
            ('claude', 0, '{"loggedIn":true,"authMethod":"api_key"}', '', 'other-method', None),
            ('claude', 0, 'invalid', '', 'unknown', None),
            ('claude', 0, '[]', '', 'unknown', None),
            ('opencode', 0, '[{"id":"openai"}]', '', 'signed-in', None),
            ('opencode', 0, '[]', '', 'signed-out', None),
            ('opencode', 1, '[]', '', 'unknown', None),
            ('opencode', 0, '{}', '', 'unknown', None),
            ('grok', 0, 'You are logged in with grok.com.\n', '', 'signed-in', None),
            ('grok', 0, 'You are not authenticated.\n', '', 'signed-out', None),
            ('grok', 0, 'something else', '', 'unknown', None),
        ]
        for key, code, out, err, state, identity in rows:
            with self.subTest(key=key, code=code, out=out):
                self.assertEqual(self.signins._classify(key, code, out, err), (state, identity))

    def test_codex_api_key_is_other_method(self):
        self.assertEqual(self.signins._classify('codex', 0, '', 'API key configured - sk-fake\n'),
                         ('other-method', None))

    def fake(self, name, source):
        path = self.state / name
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o700)

    def remote(self, keys, script=None):
        # An isolated fake-tool PATH cannot find an installed real auth CLI.
        argv = {t.key: t.status for t in self.signins.TOOLS if t.key in keys}
        return subprocess.run([sys.executable, '-I', '-B', '-c', script or self.signins.REMOTE_PROBE,
                               json.dumps(argv)], env=dict(os.environ, PATH=str(self.state)),
                              capture_output=True, text=True, timeout=10, check=True)

    def test_probe_script_runs_locally_with_fake_tools(self):
        self.fake('plaud', 'raise SystemExit(2)\n')
        self.fake('grok', 'print("You are logged in with grok.com.")\n')
        result = self.remote(['plaud', 'grok', 'codex'])
        self.assertEqual(json.loads(result.stdout), {
            'plaud': {'state': 'signed-out', 'identity': None},
            'grok': {'state': 'signed-in', 'identity': None},
            'codex': {'state': 'absent', 'identity': None},
        })
        self.assertEqual(result.stderr, '')

    def test_probe_timeout_is_unknown(self):
        self.fake('plaud', 'import time\ntime.sleep(2)\n')
        result = self.remote(['plaud'], self.signins.REMOTE_PROBE.replace('timeout=30', 'timeout=1'))
        self.assertEqual(json.loads(result.stdout)['plaud'], {'state': 'unknown', 'identity': None})

    def test_probe_ssh_failure_is_unknown(self):
        for answer in (subprocess.CompletedProcess([], 255, 'untrusted', 'error'),
                       subprocess.TimeoutExpired('ssh', 120), OSError('ssh unavailable')):
            with self.subTest(answer=type(answer).__name__), patch.object(self.signins.subprocess, 'run') as run:
                if isinstance(answer, BaseException):
                    run.side_effect = answer
                else:
                    run.return_value = answer
                self.assertEqual(self.signins.probe(self.state, ['plaud', 'grok']), {
                    key: {'state': 'unknown', 'identity': None} for key in ('plaud', 'grok')})

    def test_probe_argv(self):
        with patch.object(self.signins.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run:
            self.signins.probe(self.state, ['grok'])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:8], ['ssh', '-F', str(self.state / 'ssh-config'), '-T', 'project-sync', 'bash', '-lc', argv[7]])
        remote = shlex.split(argv[7])
        self.assertEqual(remote[0], 'python3 -I -B -c ' + shlex.quote(self.signins.REMOTE_PROBE)
                         + ' ' + shlex.quote(json.dumps({'grok': ['grok', 'models']})))
        self.assertEqual(run.call_args.kwargs['timeout'], 120)

    def test_record_holds_only_state_and_identity(self):
        self.signins.write_record(self.state, 'paired', {
            'claude': {'state': 'signed-in', 'identity': 'a@b.c', 'raw': 'http://discard'},
            'grok': {'state': 'signed-out', 'identity': 'http://discard'},
        }, 123.0)
        path = self.state / 'signins.json'
        self.assertEqual(json.loads(path.read_text()), {
            'schema': 1, 'codespace': 'paired', 'updated_at': 123.0, 'tools': {
                'claude': {'state': 'signed-in', 'checked_at': 123.0, 'identity': 'a@b.c'},
                'grok': {'state': 'signed-out', 'checked_at': 123.0},
            }})
        self.assertNotIn('http', path.read_text())
        self.assertNotRegex(path.read_text(), r'[A-Za-z0-9_-]{32,}')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_probe_rejects_malformed_output_and_drops_raw_fields(self):
        for output in ('garbage', '[]', '{"plaud":"signed-in"}', '{"plaud":{"state":"http://discard"}}'):
            with self.subTest(output=output), patch.object(self.signins.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, output, '')):
                self.assertEqual(self.signins.probe(self.state, ['plaud']),
                                 {'plaud': {'state': 'unknown', 'identity': None}})
        with patch.object(self.signins.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                '{"plaud":{"state":"signed-in","identity":"http://discard","raw":"discard"}}', '')):
            self.assertEqual(self.signins.probe(self.state, ['plaud']),
                             {'plaud': {'state': 'signed-in', 'identity': None}})
