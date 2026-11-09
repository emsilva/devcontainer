"""Offline printer/menu tests: every gh, Git and Gum executable is a fixture."""
from contextlib import ExitStack, redirect_stderr
from dataclasses import replace
import io
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

from devcontainer_setup.project_sync import __main__ as cli, overview, probe, standing, state, terminal


FAKE = r'''
import json, os, pathlib, sys, time
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
with open(os.environ['OVERVIEW_CALLS'], 'a') as log:
    log.write(json.dumps([tool, *args]) + '\n')
spec = json.loads(pathlib.Path(os.environ['OVERVIEW_FIXTURE']).read_text())
if tool == 'git':
    if 'ls-remote' in args:
        print('b' * 40 + '\trefs/heads/main')
    else:
        assert args[:2] == ['-C', os.environ['OVERVIEW_ROOT']]
        command = args[2:]
        if command == ['symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD']:
            print('refs/remotes/origin/main')
        elif command == ['symbolic-ref', '--quiet', '--short', 'HEAD']:
            print('main')
        elif command[:2] == ['reflog', 'show']:
            print('origin/main@{1794132000 +0000}')
        elif command[:2] == ['show-ref', '--verify']:
            pass
        elif command[:1] == ['rev-list']:
            print('0 0')
        elif command == ['remote', 'get-url', 'origin']:
            print('https://github.com/example/project.git')
        else:
            raise AssertionError('unexpected Git command: ' + repr(command))
elif tool == 'gum':
    print('Exit')
else:
    time.sleep(spec.get('sleep', 0))
    if spec.get('fail'):
        sys.exit(1)
    if args[:2] == ['auth', 'token']:
        print('fixture-only')
    else:
        assert args[:3] == ['api', '--hostname', 'github.com']
        target = args[3]
        if target == 'user':
            print(json.dumps({'login': 'fixture-user'}))
        elif target.startswith('user/codespaces?'):
            print(json.dumps(spec['names']))
        elif target.startswith('repos/example/project/pulls?'):
            print(json.dumps(spec['prs']))
        else:
            raise AssertionError('unexpected GitHub command: ' + target)
'''


def offline_environment(base, root):
    """Also used by CLI subprocess tests, without inheriting host auth or state."""
    base.mkdir(parents=True, exist_ok=True)
    binary = base / 'bin'
    binary.mkdir(exist_ok=True)
    env = dict(PATH=str(binary), HOME=str(base / 'home'), XDG_STATE_HOME=str(base / 'state'),
               XDG_CACHE_HOME=str(base / 'cache'), OVERVIEW_ROOT=str(root),
               SETUP_DEVCONTAINER_REPO='https://example.invalid/generator.git',
               OVERVIEW_FIXTURE=str(base / 'fixture.json'), OVERVIEW_CALLS=str(base / 'calls.jsonl'))
    for name in ('gh', 'git', 'gum'):
        script = binary / name
        script.write_text('#!' + sys.executable + '\n' + FAKE)
        script.chmod(0o700)
    for name, target in (('python3', sys.executable), ('bash', shutil.which('bash')),
                         ('dirname', shutil.which('dirname'))):
        (binary / name).symlink_to(target)
    spec = dict(names=[dict(total_count=1, codespaces=[dict(name='paired-cs', state='Available',
                repository=dict(full_name='example/project'))])], prs=[[]])
    Path(env['OVERVIEW_FIXTURE']).write_text(json.dumps(spec))
    return env


class Stream(io.StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_mode, path.stat().st_mtime_ns,
                                         path.read_bytes() if path.is_file() else None)
            for path in (root, *root.rglob('*'))}


class WorkOverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.env = offline_environment(self.base / 'fixture', self.root)
        for key in ('HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
            Path(self.env[key]).mkdir()
        self.local = standing.read(self.root, env=self.env)
        self.local.state.mkdir(parents=True, mode=0o700)
        state.atomic_write(self.local.state / 'scratch.json', b'{"codespace":"paired-cs"}\n')
        self.local = standing.read(self.root, env=self.env)
        self.out = Stream()
        self.call = Mock(side_effect=AssertionError('printing must never execute an action'))
        guard = patch.object(terminal, 'gum_binary', side_effect=AssertionError('no Gum download'))
        guard.start()
        self.addCleanup(guard.stop)

    def run_overview(self, *, tty=False, gum=None, **kwargs):
        self.assertTrue(callable(getattr(overview, 'run', None)), 'overview.run is missing')
        return overview.run(self.root, env=self.env, stdin=Stream(tty), stdout=self.out,
                            gum=gum, call=self.call, **kwargs)

    def spec(self, **changes):
        path = Path(self.env['OVERVIEW_FIXTURE'])
        value = json.loads(path.read_text())
        value.update(changes)
        path.write_text(json.dumps(value))

    def calls(self):
        path = Path(self.env['OVERVIEW_CALLS'])
        return [json.loads(line) for line in path.read_text().splitlines()]

    def assert_frame(self):
        frame, commands = self.out.getvalue().split('\n\n', 1)
        self.assertLessEqual(len(frame.splitlines()), 6)
        self.assertTrue(all(overview.cells(line) <= 78 for line in frame.splitlines()))
        self.assertIn('Next: ', commands)
        self.assertNotIn('\x1b', self.out.getvalue())

    def test_no_terminal_prints_state_and_next_lines_and_exits_0(self):
        for stdin_tty, stdout_tty in ((False, False), (True, False), (False, True)):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty):
                self.out = Stream(stdout_tty)
                self.assertTrue(callable(getattr(overview, 'run', None)), 'overview.run is missing')
                result = overview.run(self.root, env=self.env, stdin=Stream(stdin_tty),
                                      stdout=self.out, gum=None, call=self.call)
                self.assertEqual(result, 0)
                self.assert_frame()
                self.assertIn('Next: task work:cs\n', self.out.getvalue())
                self.assertEqual(self.out.getvalue().count('is available'), 1)
                self.assertNotIn('checking GitHub', self.out.getvalue())
        self.call.assert_not_called()

    def test_printing_starts_downloads_and_signs_in_nothing(self):
        # Cover every primary situation and the overlays consumed by Task 3.
        local = replace(self.local, generated=True, setup='installed', source_ref='a' * 40,
                        managed=frozenset(('work:cs', 'work:status', 'work:logs', 'work:forget')))
        cases = [(replace(local, setup=value), {}) for value in ('missing', 'unmanaged', 'unknown', 'partial')]
        cases += [(local, dict(prs=[[dict(number=n, head=dict(ref='setup/devcontainer',
                   repo=dict(full_name='example/project'))) for n in numbers]])) for numbers in ((7,), (7, 9))]
        cases += [(local, dict(names=[dict(total_count=1, codespaces=[dict(name='paired-cs', state=value,
                   repository=dict(full_name='example/project'))])])) for value in
                  ('Available', 'Shutdown', 'ShuttingDown', 'Starting', 'Provisioning', 'Queued', 'Rebuilding',
                   'Awaiting', 'Exporting', 'Updating', 'Failed', 'Unavailable', 'Archived', 'Deleted',
                   'Moved', 'Created', 'Unknown')]
        cases += [(replace(local, **changes), {}) for changes in (
            dict(pairing=None), dict(launch=standing.Launch('paired-cs', False)),
            dict(armed=True, keeper='stopped'), dict(keeper='stale'), dict(keeper='unknown'),
            dict(armed=True, keeper='running', hold_until=time.time() + 3600),
            dict(checkout=replace(local.checkout, relation='behind', behind=2)),
            dict(checkout=replace(local.checkout, relation='diverged')),
            dict(refusals=(standing.Refusal('claude', 'session-1', 'refused'),)))]
        cases += [(local, dict(names=[dict(total_count=0, codespaces=[])])),
                  (replace(local, gh_user=None), dict(names=[dict(total_count=0, codespaces=[])])),
                  (replace(local, deleted_at=1), dict(fail=True)),
                  (local, dict(fail=True)),
                  (replace(local, reading=('Available', 1)), dict(fail=True)),
                  (replace(local, pairing=None), dict(names=[dict(total_count=0, codespaces=[])])),
                  (local, dict(names=[dict(total_count=1, codespaces=[dict(name='paired-cs', state='Available',
                               repository=dict(full_name='example/other'))])])),
                  (local, dict(names=[dict(total_count=2, codespaces=[])]))]
        shutdown = dict(names=[dict(total_count=1, codespaces=[dict(name='paired-cs', state='Shutdown',
                                   repository=dict(full_name='example/project'))])])
        cases += [(replace(local, owner_stop_at=1), shutdown)]
        cases += [(replace(local, keeper='running', down_kind=kind, down_at=1), shutdown)
                  for kind in ('OWNER', 'LIFETIME', 'OTHER')]
        pristine = Path(self.env['OVERVIEW_FIXTURE']).read_text()
        forbidden = ('workspace.status', 'workspace.recovery', 'workspace._choose_pairing',
                     'codespaces.Codespaces.space', 'codespaces.Codespaces.spaces', 'lifecycle.LifecycleAPI',
                     'transport.github_token', 'arming.account', 'checkpoint_store.Store.latest',
                     'checkpoint_store.Store.load', 'checkpoint_store.Store.present',
                     'checkpoint_store.Store.relay_entries', 'state.locked')
        with ExitStack() as stack:
            for name in forbidden:
                stack.enter_context(patch('devcontainer_setup.project_sync.' + name,
                                         side_effect=AssertionError(name + ' is not a printing operation')))
            stack.enter_context(patch('devcontainer_setup.git_ops.checkout_update', side_effect=AssertionError))
            stack.enter_context(patch('devcontainer_setup.releases.fetch_release', side_effect=AssertionError))
            stack.enter_context(patch.object(terminal, 'Gum', side_effect=AssertionError('no headless Gum')))
            for index, (facts, remote) in enumerate(cases):
                with self.subTest(situation=index):
                    Path(self.env['OVERVIEW_FIXTURE']).write_text(pristine)
                    self.spec(**remote)
                    before = [snapshot(path) for path in (self.root, Path(self.env['XDG_STATE_HOME']),
                                                         Path(self.env['XDG_CACHE_HOME']))]
                    self.out = Stream()
                    with patch.object(standing, 'read', return_value=facts):
                        self.assertEqual(self.run_overview(), 0)
                    self.assert_frame()
                    self.assertEqual(before, [snapshot(path) for path in (self.root,
                                     Path(self.env['XDG_STATE_HOME']), Path(self.env['XDG_CACHE_HOME']))])
        for tool, *args in self.calls():
            if tool == 'git':
                if 'ls-remote' in args:
                    self.assertEqual(args, ['-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=',
                        'ls-remote', '--', 'https://example.invalid/generator.git',
                        'refs/heads/main', 'refs/tags/main', 'refs/tags/main^{}'])
                else:
                    self.assertEqual(args[:2], ['-C', str(self.root)])
                    self.assertIn(args[2:], [
                        ['symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD'],
                        ['symbolic-ref', '--quiet', '--short', 'HEAD'],
                        ['reflog', 'show', '-1', '--date=raw', '--format=%gD', 'refs/remotes/origin/main'],
                        ['show-ref', '--verify', '--quiet', 'refs/remotes/origin/main'],
                        ['rev-list', '--left-right', '--count', 'HEAD...refs/remotes/origin/main'],
                        ['remote', 'get-url', 'origin']])
            else:
                self.assertEqual(tool, 'gh')
                self.assertIn(args, [
                    ['auth', 'token', '--hostname', 'github.com'],
                    ['api', '--hostname', 'github.com', 'user'],
                    ['api', '--hostname', 'github.com', 'user/codespaces?per_page=100', '--paginate', '--slurp'],
                    ['api', '--hostname', 'github.com',
                     'repos/example/project/pulls?state=open&base=main&per_page=100', '--paginate', '--slurp']])
        self.call.assert_not_called()

    def test_failures_still_print_and_exit_0(self):
        for failure in ('forget', 'gh', 'git', 'state'):
            with self.subTest(failure=failure), ExitStack() as stack:
                self.out = Stream()
                if failure == 'forget':
                    state.atomic_write(self.local.state / 'conversation-forget.json', b'broken')
                elif failure in ('gh', 'git'):
                    stack.enter_context(patch('shutil.which', return_value=None))
                    script = Path(self.env['PATH']) / failure
                    original = script.read_bytes()
                    script.unlink()
                    stack.callback(script.chmod, 0o700)
                    stack.callback(script.write_bytes, original)
                else:
                    stack.enter_context(patch.object(state, 'state_dir', side_effect=ValueError('State is ambiguous')))
                self.assertEqual(self.run_overview(), 0)
                self.assert_frame()
                if failure == 'forget':
                    self.assertIn('conversation-forget.json cannot be read', self.out.getvalue())
                    (self.local.state / 'conversation-forget.json').unlink()
                if failure == 'state':
                    self.assertIn('State is ambiguous', self.out.getvalue())

    def test_deadline_holds_with_a_hung_github(self):
        self.spec(sleep=60)
        before = time.monotonic()
        self.assertEqual(self.run_overview(), 0)
        self.assertLess(time.monotonic() - before, 3)
        self.assertIn('within 2 s', self.out.getvalue())

    def test_terminal_redraws_the_first_frame(self):
        self.out = Stream(True)
        initial = overview.view(self.local, None).lines
        joined = probe.Probe.join

        def join(handle):
            self.assertEqual(self.out.getvalue(), '\n'.join(initial) + '\n')
            return joined(handle)

        with patch.object(probe.Probe, 'join', join):
            self.assertEqual(self.run_overview(tty=True), 0)
        self.assertIn(f'\x1b[{len(initial)}F\x1b[J', self.out.getvalue())
        self.assertIn('checking GitHub', self.out.getvalue())
        self.assertIn('is available', self.out.getvalue())

    def test_menu_runs_the_chosen_command_after_printing_it(self):
        self.out = Stream(True)

        class Menu:
            def choose(menu, title, options):
                self.assertEqual(title, 'What next?')
                self.assertEqual(options[-1], 'Exit')
                self.assertIn('(task work:cs)', options[0])
                self.assertIn('(task work:cs:armed)', options[1])
                return options[1]

        def call(argv, *, cwd):
            self.assertEqual(argv, ['task', 'work:cs:armed'])
            self.assertEqual(cwd, self.root)
            self.assertTrue(self.out.getvalue().endswith('Running: task work:cs:armed\n'))
            return 17

        self.call = Mock(side_effect=call)
        self.assertTrue(callable(getattr(overview, 'run', None)), 'overview.run is missing')
        with patch.object(terminal, 'Gum', return_value=Menu()) as gum, \
                patch.object(terminal, 'installed_gum', create=True, return_value='/fixture/gum') as installed:
            self.assertEqual(overview.run(self.root, env=self.env, stdin=Stream(True),
                                         stdout=self.out, call=self.call), 17)
        installed.assert_called_once_with()
        gum.assert_called_once_with('/fixture/gum')
        self.call.assert_called_once()

    def test_ctrl_c_or_esc_at_the_menu_runs_nothing(self):
        for choice in ('Exit', terminal.Cancelled(), KeyboardInterrupt()):
            with self.subTest(choice=type(choice).__name__):
                self.out = Stream(True)
                menu = Mock()
                if isinstance(choice, BaseException):
                    menu.choose.side_effect = choice
                else:
                    menu.choose.return_value = choice
                with patch.object(terminal, 'Gum', return_value=menu):
                    self.assertEqual(self.run_overview(tty=True, gum='/fixture/gum'), 0)
                self.assertTrue(self.out.getvalue().endswith('Nothing ran.\n'))
        # A real SIGINT terminating the fake Gum child must also be cancellation.
        binary = Path(self.env['PATH']) / 'gum'
        binary.write_text('#!' + sys.executable + '\nimport os, signal\n'
                          'signal.signal(signal.SIGINT, signal.SIG_DFL)\n'
                          'os.kill(os.getpid(), signal.SIGINT)\n')
        self.out = Stream(True)
        self.assertEqual(self.run_overview(tty=True, gum=str(binary)), 0)
        self.assertTrue(self.out.getvalue().endswith('Nothing ran.\n'))
        self.call.assert_not_called()

    def test_ctrl_c_during_the_probe_leaves_nothing_running(self):
        self.assertTrue(callable(getattr(overview, 'run', None)), 'overview.run is missing')
        self.spec(sleep=60)
        self.out = Stream(True)
        handles = []
        start = probe.start
        run = overview.run

        def tracked(*args, **kwargs):
            handle = start(*args, **kwargs)
            handles.append(handle)
            return handle

        def interrupted(handle):
            self.assertTrue(handle._children)
            raise KeyboardInterrupt()

        previous = signal.getsignal(signal.SIGINT)
        try:
            with patch.object(probe, 'start', side_effect=tracked), \
                    patch.object(probe.Probe, 'join', interrupted), \
                    patch.object(overview, 'run', side_effect=lambda root: run(root, env=self.env,
                                 stdin=Stream(True), stdout=self.out, gum=None, call=self.call)), \
                    patch.object(cli, 'discover_project', side_effect=AssertionError('overview dispatch is first')), \
                    redirect_stderr(io.StringIO()) as error:
                self.assertEqual(cli.main(['--project', str(self.root), 'work']), 130)
            self.assertEqual(error.getvalue(), 'Cancelled.\n')
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(len(handles), 1)
        for child in handles[0]._children:
            self.assertIsNotNone(child.process.returncode)
            with self.assertRaises(ProcessLookupError):
                os.kill(child.process.pid, 0)
        self.call.assert_not_called()

    def test_inside_a_codespace_names_the_stop_only(self):
        self.env['CODESPACES'] = 'true'
        with patch.object(probe, 'start', side_effect=AssertionError('no remote read inside a Codespace')):
            self.assertEqual(self.run_overview(), 0)
        self.assert_frame()
        self.assertIn('This is the Codespace;', self.out.getvalue())
        self.assertEqual([line for line in self.out.getvalue().splitlines() if line.startswith('Next:')],
                         ['Next: task work:cs:stop'])

    def test_without_gum_prints_next_lines_and_the_reason(self):
        self.out = Stream(True)
        with patch.object(terminal, 'Gum', side_effect=AssertionError('no Gum constructor')):
            self.assertEqual(self.run_overview(tty=True), 0)
        self.assertIn('Next: task work:cs\n', self.out.getvalue())
        self.assertTrue(self.out.getvalue().endswith(
            'Gum is not installed, so there is no menu; run one of the commands above.\n'))

    def test_forgotten_refusal_is_not_offered(self):
        state.atomic_write(self.local.state / 'conversation-forget.json', json.dumps(dict(schema=1,
            conversations=[dict(agent='claude', session_id='session-1', codespace='paired-cs',
                                reason='refused', forgotten_at=1)])).encode())
        local = replace(self.local, refusals=(standing.Refusal('claude', 'session-1', 'refused'),))
        with patch.object(standing, 'read', return_value=local):
            self.assertEqual(self.run_overview(), 0)
        self.assertNotIn('work:forget', self.out.getvalue())
        self.assertNotIn('refused 1 conversation', self.out.getvalue())


class InstalledGumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.cached = self.cache / 'devcontainer-setup/tools/gum' / terminal.GUM_VERSION / 'gum'

    def test_installed_gum_prefers_path_without_a_download(self):
        self.assertTrue(callable(getattr(terminal, 'installed_gum', None)), 'installed_gum is missing')
        with patch.object(terminal.shutil, 'which', return_value='/fixture/bin/gum'), \
                patch.object(terminal, 'gum_binary', side_effect=AssertionError):
            self.assertEqual(terminal.installed_gum(), '/fixture/bin/gum')

    def test_installed_gum_only_uses_an_executable_cached_binary(self):
        self.assertTrue(callable(getattr(terminal, 'installed_gum', None)), 'installed_gum is missing')
        with patch.object(terminal.shutil, 'which', return_value=None), \
                patch.object(terminal, 'cache_home', return_value=self.cache), \
                patch.object(terminal, 'gum_binary', side_effect=AssertionError):
            before = snapshot(self.cache)
            self.assertIsNone(terminal.installed_gum())
            self.assertEqual(snapshot(self.cache), before)
            self.cached.parent.mkdir(parents=True)
            self.cached.write_text('fixture')
            self.cached.chmod(0o600)
            self.assertIsNone(terminal.installed_gum())
            self.cached.chmod(0o700)
            self.assertEqual(terminal.installed_gum(), str(self.cached))

    def test_gum_accepts_an_explicit_binary_without_discovery(self):
        with patch.object(terminal, 'gum_binary', side_effect=AssertionError('no discovery')):
            self.assertEqual(terminal.Gum('/fixture/gum').binary, '/fixture/gum')
