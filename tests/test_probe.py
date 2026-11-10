"""Offline child-process tests: gh is always fake; Git only sees local fixtures."""
from contextlib import chdir
import errno
import hashlib
import importlib
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
from unittest.mock import Mock, patch

from devcontainer_setup.codespaces import Codespaces
from devcontainer_setup.releases import fetch_release


DEADLINE = 2.0
REAP_BOUND = 1.0


FAKE = r'''
import json, os, pathlib, signal, sys, time
spec = json.loads(pathlib.Path(os.environ['PROBE_FIXTURE']).read_text())
tool, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
key = 'git' if tool == 'git' else ('token' if args[:2] == ['auth', 'token'] else
      'names' if any(a.startswith('user/codespaces?') for a in args) else
      'prs' if any('/pulls?' in a for a in args) else 'account')
selected = {k: v for k, v in os.environ.items() if k.startswith('GIT_') or k in
            ('GH_PROMPT_DISABLED', 'GH_NO_UPDATE_NOTIFIER', 'SSH_ASKPASS')}
selected['GH_TOKEN_PRESENT'] = 'GH_TOKEN' in os.environ
selected['OTHER_TOKEN_PRESENT'] = any(k in os.environ for k in
    ('GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_DEBUG', 'GH_HOST'))
line = json.dumps({'tool': tool, 'args': args, 'env': selected, 'cwd': os.getcwd()}) + '\n'
fd = os.open(os.environ['PROBE_CALLS'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.write(fd, line.encode()); os.close(fd)
if tool == 'git' and spec.get('local_git'):
    # Refuse any real Git invocation that could address a non-fixture repository.
    root = pathlib.Path(os.environ['PROBE_ROOT']).resolve()
    if 'ls-remote' in args:
        url = args[args.index('--') + 1]
        path = pathlib.Path(url).resolve()
        assert path.is_relative_to(root) and path.is_dir(), 'nonlocal Git URL'
    else:
        assert args[:1] == ['-C'] and pathlib.Path(args[1]).resolve().is_relative_to(root)
    os.execv(os.environ['PROBE_REAL_GIT'], [os.environ['PROBE_REAL_GIT'], *args])
answer = spec[key]
if answer.get('fork'):
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if answer.get('close_pipes'):
            os.close(1); os.close(2)
        time.sleep(60)
        os._exit(0)
    # Publish atomically: the test waits for the file to exist, and write_text creates it empty first.
    target = pathlib.Path(os.environ['PROBE_PIDS'])
    partial = target.with_name(target.name + '.partial')
    partial.write_text(json.dumps([os.getpid(), child]))
    os.replace(partial, target)
    if answer.get('exit_leader'):
        sys.exit(0)
time.sleep(answer.get('sleep', 0))
if answer.get('stderr_bytes'):
    sys.stderr.write('x' * answer['stderr_bytes']); sys.stderr.flush()
if answer.get('bytes'):
    sys.stdout.write('x' * answer['bytes'])
else:
    sys.stdout.write(answer.get('raw', json.dumps(answer.get('value'))))
sys.exit(answer.get('code', 0))
'''


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file()}


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'calls.jsonl'
        self.pids = self.root / 'pids.json'
        self.specfile = self.root / 'fixture.json'
        self.real_git = shutil.which('git')
        self.env = {'PATH': str(self.bin), 'HOME': str(self.root / 'home'),
                    'XDG_CACHE_HOME': str(self.root / 'cache'),
                    'XDG_STATE_HOME': str(self.root / 'state'),
                    'PROBE_FIXTURE': str(self.specfile), 'PROBE_CALLS': str(self.calls),
                    'PROBE_PIDS': str(self.pids), 'PROBE_ROOT': str(self.root),
                    'PROBE_REAL_GIT': self.real_git, 'LC_ALL': 'C'}
        for name in ('gh', 'git'):
            script = self.bin / name
            script.write_text('#!' + sys.executable + '\n' + FAKE)
            script.chmod(0o755)
        self.spec = {'token': {'raw': 'fixture-only'}, 'account': {'value': {'login': 'reader'}},
                     'names': {'value': [{'total_count': 1, 'codespaces': [self.space()]}]},
                     'prs': {'value': [[self.pr(1)]]},
                     'git': {'raw': 'a' * 40 + '\trefs/heads/main\n'}}
        self.children = []
        self.addCleanup(self.clean_children)
        # Import in setUp so the initial RED run identifies every missing-feature test.
        self.probe = importlib.import_module('devcontainer_setup.project_sync.probe')

    @staticmethod
    def space(name='space-1', repository='owner/project'):
        return {'name': name, 'state': 'Available', 'last_used_at': '2026-11-01T12:30:00Z',
                'repository': {'full_name': repository}}

    @staticmethod
    def pr(number, ref='setup/devcontainer', repository='owner/project'):
        return {'number': number, 'head': {'ref': ref, 'repo': {'full_name': repository}}}

    def popen(self, *args, **kwargs):
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        child = subprocess.Popen(*args, **kwargs)
        self.children.append(child)
        return child

    def clean_children(self):
        for child in self.children:
            child.poll()
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for child in self.children:
            child.wait(timeout=3)
            for stream in (child.stdout, child.stderr):
                if stream and not stream.closed:
                    stream.close()

    def start(self, *, deadline=DEADLINE, generator=('https://example.invalid/generator.git', 'main'),
              want_prs=True, default='main', **kwargs):
        self.specfile.write_text(json.dumps(self.spec))
        request = self.probe.Request('owner/project', default, want_prs, generator)
        return self.probe.start(request, env=self.env, deadline=deadline, popen=self.popen, **kwargs)

    def calls_read(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def assert_dead(self):
        pids = json.loads(self.pids.read_text())
        limit = time.monotonic() + REAP_BOUND
        for pid in pids:
            while time.monotonic() < limit:
                result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                        capture_output=True, text=True, timeout=1)
                if result.returncode or not result.stdout.strip() or result.stdout.strip().startswith('Z'):
                    break
                time.sleep(0.01)
            else:
                self.fail(f'fixture child {pid} is still running')
        for child in self.children:
            self.assertIsNotNone(child.returncode)

    def wait_for_pids(self):
        until = time.monotonic() + DEADLINE
        while not self.pids.exists() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertTrue(self.pids.exists())

    def test_all_reads_finish_inside_the_deadline(self):
        before = time.monotonic()
        handle = self.start()
        self.assertLess(time.monotonic() - before, DEADLINE)
        remote = handle.join()
        self.assertLess(time.monotonic() - before, DEADLINE)
        for observation in (remote.account, remote.names, remote.spaces, remote.setup_prs, remote.upstream):
            self.assertEqual(observation.status, 'ok')
            self.assertIsInstance(observation.at, float)
            self.assertIsNone(observation.reason)
        self.assertEqual(remote.account.value, 'reader')
        self.assertEqual(remote.names.value, frozenset({'space-1'}))
        self.assertEqual(remote.spaces.value, (self.probe.Space('space-1', 'Available', 1793536200.0),))
        self.assertEqual(remote.setup_prs.value, (1,))
        self.assertEqual(remote.upstream.value, 'a' * 40)
        self.assertIs(handle.join(), remote)
        handle.cancel()

    def test_slow_read_is_unknown_and_the_rest_are_kept(self):
        self.spec['names']['sleep'] = 30
        deadline = DEADLINE
        before = time.monotonic()
        remote = self.start(deadline=deadline).join()
        self.assertEqual((remote.names.status, remote.names.reason), ('unknown', 'no answer within 2 s'))
        self.assertEqual(remote.account.status, 'ok')
        self.assertEqual(remote.upstream.status, 'ok')
        self.assertLess(time.monotonic() - before, deadline + REAP_BOUND)

    def test_grandchild_is_gone_after_the_deadline(self):
        self.spec['token'].update(fork=True, sleep=30)
        self.start(deadline=DEADLINE).join()
        self.assert_dead()

    def test_exited_leader_with_a_live_grandchild(self):
        self.spec['token'].update(fork=True, exit_leader=True)
        deadline = DEADLINE
        before = time.monotonic()
        self.start(deadline=deadline).join()
        self.assertLess(time.monotonic() - before, deadline + REAP_BOUND)
        self.assert_dead()

    def test_normal_completion_also_kills_a_leaders_group(self):
        self.spec['git'].update(fork=True, exit_leader=True, close_pipes=True)
        remote = self.start().join()
        self.assertEqual(remote.account.status, 'ok')
        self.assert_dead()

    def test_cancel_kills_and_reaps(self):
        self.spec['token'].update(fork=True, sleep=30)
        handle = self.start()
        self.wait_for_pids()
        handle.cancel()
        handle.cancel()
        self.assertEqual(handle.join().account.status, 'unknown')
        self.assert_dead()

    def test_interrupt_during_join_cleans_children(self):
        self.spec['token'].update(fork=True, sleep=30)
        # Fixture startup must not use up the deadline before the interruption.
        handle = self.start(clock=lambda: 0.0)
        self.wait_for_pids()
        with patch.object(handle, '_selector', wraps=handle._selector) as selector:
            selector.select.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                handle.join()
        self.assert_dead()

    def test_pipe_failure_is_failed_and_reaped(self):
        handle = self.start()
        with patch.object(self.probe.os, 'read', side_effect=OSError(errno.EIO, 'fixture pipe error')):
            remote = handle.join()
        self.assertEqual(remote.upstream.status, 'failed')
        self.assertEqual(remote.account.status, 'failed')
        for child in self.children:
            self.assertIsNotNone(child.returncode)
        self.assertIs(handle.join(), remote)

    def test_cleanup_failure_does_not_abandon_other_groups(self):
        self.spec['git']['sleep'] = 30
        self.spec['token'].update(fork=True, sleep=30)
        handle = self.start()
        self.wait_for_pids()
        original = os.killpg
        blocked = self.children[0].pid
        def killpg(group, sig):
            if group == blocked:
                raise PermissionError(errno.EPERM, 'fixture signal error')
            return original(group, sig)
        with patch.object(self.probe.os, 'killpg', side_effect=killpg):
            handle.cancel()
        self.assertIn(str(blocked), handle.join().upstream.reason)
        for child in self.children[1:]:
            self.assertIsNotNone(child.returncode)
        # The injected unkillable group is still owned by the fixture cleanup.
        self.clean_children()
        self.assert_dead()

    def test_only_reads_are_made(self):
        self.start().join()
        for call in self.calls_read():
            args = call['args']
            self.assertNotIn('fetch', args)
            self.assertNotIn('codespace', args)
            self.assertNotIn('login', args)
            self.assertNotIn('--method', args)
        self.assertFalse((self.root / 'state').exists())
        self.assertFalse((self.root / 'cache').exists())

    def test_child_environments_disable_prompts(self):
        self.env.update(GITHUB_TOKEN='fixture-only', GH_ENTERPRISE_TOKEN='fixture-only',
                        GH_DEBUG='api', GH_HOST='example.invalid', GIT_DIR='bad', GIT_CONFIG_COUNT='1',
                        GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='bad', SSH_ASKPASS='bad',
                        DEVCONTAINER_GH_USER='reader')
        self.start().join()
        for call in self.calls_read():
            env = call['env']
            if call['tool'] == 'gh':
                self.assertEqual(env['GH_PROMPT_DISABLED'], '1')
                self.assertEqual(env['GH_NO_UPDATE_NOTIFIER'], '1')
                self.assertFalse(env['OTHER_TOKEN_PRESENT'])
                self.assertEqual(env['GH_TOKEN_PRESENT'], call['args'][0] == 'api')
                self.assertFalse(any(k.startswith('GIT_') for k in env))
                if call['args'][:2] == ['auth', 'token']:
                    self.assertEqual(call['args'], ['auth', 'token', '--hostname', 'github.com', '--user', 'reader'])
            else:
                self.assertEqual({k: v for k, v in env.items() if k.startswith('GIT_')},
                                 {'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1',
                                  'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes'})
                self.assertNotIn('SSH_ASKPASS', env)
                self.assertEqual(call['cwd'], '/')
                self.assertEqual(call['args'][:5], ['-c', 'core.hooksPath=/dev/null', '-c',
                                                  'credential.helper=', 'ls-remote'])

    def test_list_completeness(self):
        cases = [([{'codespaces': [self.space(repository='owner/renamed')]}], True),
                 ([{'total_count': 101, 'codespaces': [self.space()]}], False),
                 ([{'total_count': 1, 'codespaces': [self.space()]},
                   {'total_count': 2, 'codespaces': []}], False),
                 ([{'codespaces': [{}]}], False), ([{'codespaces': [{'name': '\n'}]}], False),
                 ([{}], False), ([], False), ({'codespaces': []}, False),
                 ([{'codespaces': [self.space('space-' + str(i)) for i in range(100)]}], False),
                 ([{'codespaces': [self.space('space-' + str(i)) for i in range(100)]},
                   {'codespaces': []}], True),
                 ([{'total_count': 1, 'codespaces': [self.space(), self.space()]}], True),
                 ([{'codespaces': []}], True)]
        for value, complete in cases:
            with self.subTest(value=str(value)[:100]):
                self.spec['names'] = {'value': value}
                remote = self.start().join()
                self.assertEqual(remote.names.status, 'ok' if complete else 'failed')
                if not complete:
                    self.assertEqual(remote.names.reason, 'incomplete list')
                if value == cases[0][0]:
                    self.assertIn('space-1', remote.names.value)
                    self.assertEqual(remote.spaces.value, ())
        for answer, reason in (({'raw': 'not json'}, 'request failed'),
                               ({'code': 1}, 'request failed'),
                               ({'bytes': 3 * 1024 * 1024}, 'answer too large')):
            with self.subTest(answer=answer):
                self.spec['names'] = answer
                self.assertEqual(self.start().join().names.reason, reason)

    def test_missing_binaries_are_failed_observations(self):
        (self.bin / 'gh').unlink()
        (self.bin / 'git').unlink()
        remote = self.start().join()
        self.assertEqual((remote.account.status, remote.account.reason), ('failed', 'gh is not installed'))
        self.assertEqual((remote.upstream.status, remote.upstream.reason), ('failed', 'git is not installed'))

    def test_setup_pr_filter_matches_setup(self):
        pages = [[self.pr(1), self.pr(2, 'setup/devcontainer-0'), self.pr(3, 'setup/devcontainerx')],
                 [self.pr(4, 'setup/devcontainer-12'), self.pr(5, repository='fork/project'),
                  {'number': 6, 'head': {'ref': 'setup/devcontainer', 'repo': None}},
                  self.pr(7, repository='OWNER/PROJECT')]]
        self.spec['prs'] = {'value': pages}
        setup = Codespaces()
        with patch.object(setup, 'command', return_value=pages) as command:
            expected = tuple(pr['number'] for pr in setup.setup_prs('owner/project', 'release/one&two+x#y'))
        remote = self.start(default='release/one&two+x#y').join()
        self.assertEqual(remote.setup_prs.value, expected)
        query = command.call_args.args[1]
        self.assertTrue(any(query in call['args'] for call in self.calls_read()))

    def local_repository(self):
        self.spec['local_git'] = True
        self.specfile.write_text(json.dumps(self.spec))
        source = self.root / 'source'
        source.mkdir()
        def git(*args):
            return subprocess.check_output([str(self.bin / 'git'), '-C', str(source), *args],
                                           env=self.env, text=True, stderr=subprocess.PIPE).strip()
        git('init', '-b', 'main')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        git('config', 'commit.gpgsign', 'false')
        for name in ('scripts/setup.sh', 'devcontainer_setup/__main__.py'):
            path = source / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('# fixture\n')
        git('add', '.')
        git('commit', '-m', 'Fixture')
        git('tag', '-a', 'annotated', '-m', 'Fixture tag')
        git('tag', 'lightweight')
        # These config files are test-authored and contain no credentials.
        home = Path(self.env['HOME'])
        home.mkdir()
        helper = self.root / 'helper'
        helper.write_text('#!/bin/sh\necho called >> "' + str(self.root / 'helper-calls') + '"\n')
        helper.chmod(0o755)
        (home / '.gitconfig').write_text('[credential]\n helper = ' + str(helper) + '\n[core]\n askpass = ' + str(helper) + '\n')
        git('config', 'credential.helper', str(helper))
        git('config', 'core.askpass', str(helper))
        return source

    def test_upstream_tip_matches_fetch_release(self):
        # Check what is read; 2 s/reaping: test_all_reads_finish_inside_the_deadline, test_grandchild_is_gone_after_the_deadline, test_cancel_kills_and_reaps.
        source = self.local_repository()
        for ref in ('main', 'annotated', 'lightweight', 'refs/heads/main', 'refs/tags/annotated'):
            with self.subTest(ref=ref), patch.dict(os.environ, self.env, clear=True):
                expected = fetch_release(str(source), ref, cache=self.root / 'release-cache')[1]
                self.specfile.write_text(json.dumps(self.spec))
                before = snapshot(self.root)
                remote = self.start(generator=(str(source), ref), deadline=30.0).join()
                after = snapshot(self.root)
                # Only the fake's call receipt and unchanged test fixture may be touched.
                for path in ('calls.jsonl', 'fixture.json'):
                    before.pop(path, None); after.pop(path, None)
                self.assertEqual(after, before)
                self.assertEqual((remote.upstream.status, remote.upstream.value), ('ok', expected))
        self.assertEqual(self.start(generator=(str(source), 'missing'), deadline=30.0).join().upstream.status, 'failed')
        for ref in ('a' * 40, 'A' * 40):
            calls = len(self.calls_read())
            observation = self.start(generator=(str(source), ref), want_prs=False, deadline=30.0).join().upstream
            self.assertEqual((observation.status, observation.reason), ('unknown', 'pinned'))
            self.assertFalse(any(call['tool'] == 'git' for call in self.calls_read()[calls:]))
        with chdir(self.root):
            self.assertEqual(self.start(generator=('source', 'main'), deadline=30.0).join().upstream.status, 'ok')
        self.assertFalse((self.root / 'helper-calls').exists())

    def test_no_token_is_failed_without_api_calls(self):
        for answer in ({'raw': ''}, {'code': 1, 'raw': 'arbitrary error'}):
            self.spec['token'] = answer
            remote = self.start().join()
            for observation in (remote.account, remote.names, remote.spaces, remote.setup_prs):
                self.assertEqual((observation.status, observation.reason), ('failed', 'not signed in to GitHub'))
        self.assertFalse(any(call['args'][:1] == ['api'] for call in self.calls_read() if call['tool'] == 'gh'))

    def test_environment_token_bypasses_token_child(self):
        self.env['GH_TOKEN'] = 'fixture-only'
        remote = self.start(want_prs=False, generator=None).join()
        self.assertEqual(remote.account.status, 'ok')
        self.assertFalse(any(call['args'][:2] == ['auth', 'token'] for call in self.calls_read()))
        self.assertFalse(any('/pulls?' in str(call['args']) for call in self.calls_read()))
        self.assertEqual(remote.setup_prs.status, 'unknown')
        self.assertEqual(remote.upstream.status, 'unknown')

    def test_stderr_is_drained_without_being_exposed(self):
        self.spec['account']['stderr_bytes'] = 3 * 1024 * 1024
        remote = self.start().join()
        self.assertEqual(remote.account.status, 'ok')

    def test_generator_request_matches_setup_defaults_and_overrides(self):
        self.assertEqual(self.probe.generator_request({}), ('https://github.com/emsilva/devcontainer.git', 'main'))
        self.assertEqual(self.probe.generator_request({'SETUP_DEVCONTAINER_REPO': '/local/source',
                                                      'SETUP_DEVCONTAINER_REF': 'refs/heads/topic'}),
                         ('/local/source', 'refs/heads/topic'))


class CleanupRaceTests(unittest.TestCase):
    def setUp(self):
        self.probe = importlib.import_module('devcontainer_setup.project_sync.probe')
        self.killpg = self.enterContext(patch.object(self.probe.os, 'killpg'))

    def fake_probe(self):
        process = Mock(pid=12345, returncode=None)
        for name in ('stdout', 'stderr'):
            reader, writer = os.pipe()
            os.close(writer)
            stream = os.fdopen(reader, 'rb')
            self.addCleanup(stream.close)
            setattr(process, name, stream)
        process.poll.side_effect = lambda: process.returncode
        def reap(timeout):
            process.returncode = -signal.SIGKILL
            return process.returncode
        process.wait.side_effect = reap
        request = self.probe.Request('owner/project', 'main', False, None)
        handle = self.probe.start(request, env={}, popen=Mock(return_value=process))
        self.addCleanup(handle.cancel)
        return handle, process

    def test_oversized_answer_survives_an_exiting_child_cleanup(self):
        handle, process = self.fake_probe()
        def killpg(group, sig):
            self.assertEqual((group, sig), (process.pid, signal.SIGKILL))
            if self.killpg.call_count == 1:
                return  # The size limit kills the child; cleanup catches it exiting.
            if process.returncode is None:
                raise PermissionError(errno.EPERM, 'fixture exiting leader')
            raise ProcessLookupError(errno.ESRCH, 'fixture reaped leader')
        self.killpg.side_effect = killpg
        key = handle._selector.get_key(process.stdout)
        with patch.object(self.probe.os, 'read', return_value=b'x' * (self.probe.LIMIT + 1)):
            handle._read(key)
        remote = handle.join()
        self.assertEqual(remote.names.reason, 'answer too large')
        self.assertEqual(remote.spaces, remote.names)
        self.assertIsNotNone(process.returncode)

    def test_kill_retries_eperm_until_the_leader_is_reaped(self):
        handle, process = self.fake_probe()
        reap = process.wait.side_effect
        def wait(timeout):
            self.assertGreater(timeout, 0)
            self.assertLess(timeout, REAP_BOUND)
            if process.wait.call_count == 1:
                raise subprocess.TimeoutExpired('fixture', timeout)
            return reap(timeout)
        process.wait.side_effect = wait
        def killpg(group, sig):
            if process.returncode is None:
                raise PermissionError(errno.EPERM, 'fixture exiting leader')
            raise ProcessLookupError(errno.ESRCH, 'fixture reaped leader')
        self.killpg.side_effect = killpg
        self.probe.Probe._kill(handle._children[0])
        process.wait.side_effect = reap
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(self.killpg.call_count, 3)

    def test_reaped_leaders_zombie_group_is_not_a_cleanup_failure(self):
        handle, process = self.fake_probe()
        process.returncode = -signal.SIGKILL
        self.killpg.side_effect = PermissionError(errno.EPERM, 'fixture zombies')
        self.probe.Probe._kill(handle._children[0])
        process.wait.assert_not_called()

    def test_cleanup_failures_only_fill_unknown_observations(self):
        for failure in ('signal', 'reap'):
            with self.subTest(failure=failure):
                handle, process = self.fake_probe()
                known = {'account': self.probe.Observation('ok', 'reader', 1.0, None),
                         'names': self.probe.Observation('failed', None, 1.0, 'answer too large')}
                handle._observations.update(known)
                self.killpg.side_effect = (OSError(errno.EIO, 'fixture signal error')
                                           if failure == 'signal' else None)
                process.wait.side_effect = subprocess.TimeoutExpired('fixture', REAP_BOUND)
                handle.cancel()
                remote = handle.join()
                for key, observation in known.items():
                    self.assertIs(getattr(remote, key), observation)
                reason = (f'request failed; child {process.pid} could not be signalled' if failure == 'signal'
                          else f'request failed; child {process.pid} was not reaped')
                self.assertEqual((remote.spaces.status, remote.spaces.reason), ('failed', reason))
