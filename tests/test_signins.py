import importlib
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
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


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.signins = importlib.import_module('devcontainer_setup.project_sync.signins')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name).resolve()
        from devcontainer_setup.project_sync.state import atomic_write
        atomic_write(self.state / 'scratch.json', b'{"codespace":"paired"}\n')

    def states(self, **changes):
        return {t.key: {'state': changes.get(t.key, 'signed-in'),
                        'identity': 'a@b.c' if t.key == 'claude' else None} for t in self.signins.TOOLS}

    def sequence(self, initial=None, *, follow=None, login=0, ready=None):
        with patch.object(self.signins, 'ready', return_value=ready), \
                patch.object(self.signins, 'probe', side_effect=[initial or self.states(), *list(follow or [])]) as probe, \
                patch.object(self.signins, 'login', side_effect=login if isinstance(login, list) else None,
                             return_value=login) as sign_in, contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.signins.run(self.state, self.state, 'paired')
        return code, out.getvalue(), sign_in, probe

    def test_not_connected_refuses_without_ssh(self):
        with patch.object(self.signins, 'watcher_live', return_value=False), \
                patch.object(self.signins.subprocess, 'run') as ssh:
            refusal = self.signins.ready(self.state, wait=0)
        self.assertEqual(refusal, 'Sign-in needs this project connected to its Codespace. Run task work:cs, then task work:signin:all.')
        ssh.assert_not_called()
        code, _, login, probe = self.sequence(ready=refusal)
        self.assertEqual(code, 1)
        login.assert_not_called()
        probe.assert_not_called()

    def test_stale_remote_heartbeat_refuses_after_wait(self):
        clock = [0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(self.signins, 'watcher_live', return_value=True), \
                patch.object(self.signins.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '40\n', '')) as ssh, \
                patch.object(self.signins.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.signins.time, 'sleep', side_effect=advance) as sleep:
            refusal = self.signins.ready(self.state, wait=2)
        self.assertEqual(refusal, "The Codespace is not reaching this Mac's link helper yet. Run task work:cs again, then task work:signin:all.")
        self.assertTrue(sleep.called)
        self.assertGreaterEqual(ssh.call_count, 2)
        code, _, login, _ = self.sequence(ready=refusal)
        self.assertEqual(code, 1)
        login.assert_not_called()

    def test_signs_in_only_signed_out_tools_in_order(self):
        code, out, login, probe = self.sequence(self.states(plaud='signed-out'), follow=[{'plaud': self.states()['plaud']}])
        self.assertEqual(code, 0)
        login.assert_called_once_with(self.state, self.signins.TOOLS[0])
        self.assertEqual(probe.call_args_list[1].args, (self.state, ['plaud']))
        self.assertIn('Signing in to Plaud. Approve the page that opens on this Mac.', out)
        self.assertIn('Plaud: signed in', out)
        self.assertIn('Claude: already signed in as a@b.c', out)
        self.assertIn('Signed in: Plaud, Codex, Claude, OpenCode, Grok.', out)

    def test_skips_signed_in_other_method_and_unknown(self):
        code, out, login, _ = self.sequence(self.states(codex='other-method', opencode='unknown', grok='absent'))
        self.assertEqual(code, 1)
        login.assert_not_called()
        self.assertIn('Codex: signed in another way; left as is', out)
        self.assertIn('OpenCode: could not check sign-in; skipped', out)
        self.assertIn('Grok: not installed; skipped', out)
        self.assertNotIn('Not signed in: OpenCode, Grok', out)  # Absent tools are not failures.

    def test_failed_login_continues_and_lists_retry(self):
        initial = self.states(plaud='signed-out', grok='signed-out')
        code, out, login, _ = self.sequence(initial, follow=[{'plaud': initial['plaud']}, {'grok': initial['grok']}], login=[1, 1])
        self.assertEqual(code, 1)
        self.assertEqual([c.args[1].key for c in login.call_args_list], ['plaud', 'grok'])
        self.assertIn('Grok: not signed in (the sign-in command exited with code 1)', out)
        self.assertIn('Not signed in: Plaud, Grok. Retry with task work:signin:all.', out)
        self.assertIn(' Enter the code shown here.', out)
        code, out, _, _ = self.sequence(self.states(grok='signed-out'), follow=[{'grok': initial['grok']}], login=1)
        self.assertIn('Not signed in: Grok. Retry with task work:signin:all.', out)

    def test_failed_login_names_what_happened(self):
        # The line follows the login's exit code, not a guess: ssh exits 255 when
        # the connection drops, and login() returns 124 at its own deadline.
        cases = [
            (255, 'unknown', 'Codex: not signed in (the connection to the Codespace dropped)'),
            (124, 'signed-out', 'Codex: not signed in (no approval within 10 minutes; '
                                'if a Mac notice said the callback port is busy, free it first)'),
            (0, 'signed-out', 'Codex: not signed in (the sign-in finished, but Codex still reports signed out)'),
            (0, 'unknown', 'Codex: not confirmed (could not check the sign-in afterwards)'),
            (255, 'signed-in', 'Codex: signed in'),
        ]
        for login, after, line in cases:
            with self.subTest(login=login, after=after):
                code, out, _, _ = self.sequence(self.states(codex='signed-out'),
                                                follow=[{'codex': {'state': after, 'identity': None}}], login=login)
                self.assertIn(line + '\n', out)
                self.assertEqual(code, 0 if after == 'signed-in' else 1)
                self.assertNotIn('callback port busy: see the Mac notice', out)

    def test_line_after_login_agrees_with_summary_for_other_states(self):
        # An API-key login exits 0 and probes as other-method; the summary counts
        # it as signed in, so the line must not say "still reports signed out".
        for after, line in (('other-method', 'Codex: signed in another way; left as is'),
                            ('absent', 'Codex: not installed; skipped')):
            with self.subTest(after=after):
                code, out, _, _ = self.sequence(self.states(codex='signed-out'),
                                                follow=[{'codex': {'state': after, 'identity': None}}], login=0)
                self.assertIn(line + '\n', out)
                self.assertEqual(code, 0)
        self.assertIn('Signed in: Plaud, Claude, OpenCode, Grok.', out)

    def test_deadline_line_counts_seconds_when_not_whole_minutes(self):
        code, out, _, _ = self.sequence(self.states(plaud='signed-out'),
                                        follow=[{'plaud': {'state': 'signed-out', 'identity': None}}], login=124)
        self.assertIn('Plaud: not signed in (no approval within 150 seconds; '
                      'if a Mac notice said the callback port is busy, free it first)\n', out)

    def test_cancel_stops_sequence_and_keeps_record(self):
        code, out, login, _ = self.sequence(self.states(plaud='signed-out', codex='signed-out', claude='signed-out'),
                follow=[{'plaud': self.states()['plaud']}], login=[0, 130])
        self.assertEqual(code, 130)
        self.assertEqual([c.args[1].key for c in login.call_args_list], ['plaud', 'codex'])
        self.assertIn('Sign-in cancelled during Codex.', out)
        self.assertIn('Signed in: Plaud', out)
        self.assertEqual(json.loads((self.state / 'signins.json').read_text())['tools']['plaud']['state'], 'signed-in')

    def test_deadline_terminates_login(self):
        spawn = subprocess.Popen
        children = []
        def child(*args, **kwargs):
            process = spawn(['sleep', '30'], **kwargs)
            children.append(process)
            return process
        try:
            with patch.object(self.signins.subprocess, 'Popen', side_effect=child):
                self.assertEqual(self.signins.login(self.state, replace(self.signins.TOOLS[0], deadline=1)), 124)
            self.assertIsNotNone(children[0].poll())
        finally:
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def test_login_argv(self):
        with patch.object(self.signins.subprocess, 'Popen') as spawn:
            spawn.return_value.wait.return_value = 0
            self.assertEqual(self.signins.login(self.state, self.signins.TOOLS[-1]), 0)
        spawn.assert_called_once_with(['ssh', '-F', str(self.state / 'ssh-config'), '-t', 'project-sync',
                                       'bash', '-lc', "'grok login --device-auth'"], start_new_session=False)

    def test_term_ends_current_login(self):
        self.assertTrue(callable(self.signins.run))
        driver = '''
import os, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from devcontainer_setup.project_sync import signins as s
state = Path(sys.argv[1])
spawn = subprocess.Popen
def child(*args, **kwargs):
    process = spawn(['sleep', '30'], **kwargs)
    (state / 'child.pid').write_text(str(process.pid))
    return process
s.ready = lambda *a, **k: None
s.probe = lambda state, keys: {k: {'state': 'signed-out', 'identity': None} for k in keys}
with patch.object(s.subprocess, 'Popen', side_effect=child):
    raise SystemExit(s.run(state, state, 'paired'))
'''
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=number):
                pidfile = self.state / 'child.pid'
                pidfile.unlink(missing_ok=True)
                parent = subprocess.Popen([sys.executable, '-B', '-c', driver, str(self.state)],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                child_pid = None
                try:
                    deadline = time.monotonic() + 5
                    while not pidfile.exists() and time.monotonic() < deadline and parent.poll() is None:
                        time.sleep(0.02)
                    self.assertTrue(pidfile.exists(), 'sequence never launched its fake child')
                    child_pid = int(pidfile.read_text())
                    parent.send_signal(number)
                    out, err = parent.communicate(timeout=10)
                    self.assertEqual(parent.returncode, 128 + number, err)
                    self.assertIn('Signed in:', out)
                    self.assertIn('Sign-in cancelled during Plaud.', out)
                    self.assertTrue((self.state / 'signins.json').exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                finally:
                    if parent.poll() is None:
                        parent.kill()
                    parent.communicate()
                    if child_pid is not None:
                        try:
                            os.kill(child_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_keyboard_interrupt_during_probe_keeps_record_and_restores_handlers(self):
        previous = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with patch.object(self.signins, 'ready', return_value=None), \
                patch.object(self.signins, 'probe', side_effect=KeyboardInterrupt), \
                patch.object(self.signins, 'login') as login, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.signins.run(self.state, self.state, 'paired'), 130)
        login.assert_not_called()
        self.assertIn('Signed in:', out.getvalue())
        self.assertTrue((self.state / 'signins.json').exists())
        self.assertEqual({n: signal.getsignal(n) for n in previous}, previous)

    def test_second_run_is_refused_while_locked(self):
        from devcontainer_setup.project_sync.state import locked
        with locked(self.state / 'signins.lock'):
            code, out, login, probe = self.sequence()
        self.assertEqual(code, 1)
        self.assertIn('Another sign-in is already running for this project.', out)
        login.assert_not_called()
        probe.assert_not_called()

    def test_readiness_is_rechecked_before_each_login(self):
        with patch.object(self.signins, 'ready', side_effect=[None, 'lost connection']), \
                patch.object(self.signins, 'probe', return_value=self.states(plaud='signed-out')), \
                patch.object(self.signins, 'login') as login, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.signins.run(self.state, self.state, 'paired'), 1)
        self.assertIn('lost connection', out.getvalue())
        login.assert_not_called()

    def test_signal_during_spawn_still_reaps_child(self):
        spawn, children = subprocess.Popen, []
        def child(*args, **kwargs):
            process = spawn(['sleep', '30'], **kwargs)
            children.append(process)
            signal.raise_signal(signal.SIGTERM)
            return process
        try:
            with patch.object(self.signins, 'ready', return_value=None), \
                    patch.object(self.signins, 'probe', return_value=self.states(plaud='signed-out')), \
                    patch.object(self.signins.subprocess, 'Popen', side_effect=child), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.signins.run(self.state, self.state, 'paired'), 143)
            self.assertIsNotNone(children[0].poll())
        finally:
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def test_signal_during_finalization_keeps_record_and_summary(self):
        for stage in ('write_record', '_summary'):
            with self.subTest(stage=stage):
                original = getattr(self.signins, stage)
                def interrupt(*args, **kwargs):
                    signal.raise_signal(signal.SIGTERM)
                    return original(*args, **kwargs)
                with patch.object(self.signins, 'ready', return_value=None), \
                        patch.object(self.signins, 'probe', return_value=self.states()), \
                        patch.object(self.signins, stage, side_effect=interrupt), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(self.signins.run(self.state, self.state, 'paired'), 143)
                self.assertTrue((self.state / 'signins.json').exists())
                self.assertIn('Signed in:', out.getvalue())

    def test_changed_binding_refuses_without_login(self):
        from devcontainer_setup.project_sync.state import atomic_write
        atomic_write(self.state / 'scratch.json', b'{"codespace":"replacement"}\n')
        code, out, login, probe = self.sequence()
        self.assertEqual(code, 1)
        self.assertIn('task work:cs', out)
        login.assert_not_called()
        probe.assert_not_called()
