"""Restore after a restart: stale Herdr proxies, the Herdr adapter, the lead and its resume."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync import restore, ssh_hosts, state as project_state

STATE = '/Users/me/.local/state/devcontainer-setup/projects/0123456789abcdef01234567'
PROXY = ' '.join(shlex.split(ssh_hosts._proxy_command(Path(STATE), 'cs-1'))[:-2])


def etime(seconds):
    # As ps prints elapsed time: [[dd-]hh:]mm:ss.
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    clock = f'{hours:02d}:{rest // 60:02d}:{rest % 60:02d}' if days or hours else f'{rest // 60:02d}:{rest % 60:02d}'
    return f'{days}-{clock}' if days else clock


def listed(rows):
    return lambda args, **kwargs: subprocess.CompletedProcess(args, 0, '\n'.join(rows) + '\n', '')


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeHerdr:
    """One lead with scripted submissions, unsent input, readiness and activity."""

    def __init__(self, clock, statuses=('idle',), shows_rid_after=None, working_after_prompt=False, agents=None,
                 prompt_results=None, interactive_ready=None, active_after_enter=None, started_status='working'):
        self.clock, self.statuses, self.reads = clock, list(statuses), 0
        self.shows_rid_after = shows_rid_after
        self.prompt_results = list(prompt_results or ('started' if working_after_prompt else 'agent_prompt_stalled',))
        self.interactive_ready, self.active_after_enter = interactive_ready, active_after_enter
        self.started_status = started_status
        self.active, self.screen_reads, self.enters, self.waits = None, [], [], []
        self.prompts, self.extra = [], agents or []

    def status(self):
        if self.active:
            return self.active
        status = self.statuses[min(self.reads, len(self.statuses) - 1)]
        self.reads += 1
        return status

    def agents(self):
        ready = self.interactive_ready
        if isinstance(ready, tuple):
            ready = ready[min(self.reads, len(ready) - 1)]
        agent = {'name': 'lead-proj', 'agent_status': self.status()}
        if ready is not None:
            agent['interactive_ready'] = ready
        return [agent] + self.extra

    def prompt(self, name, text):
        self.prompts.append((name, text, self.clock.t))
        result = self.prompt_results[min(len(self.prompts) - 1, len(self.prompt_results) - 1)]
        if result == 'started':
            self.active = self.started_status
        return result

    def enter(self, name):
        self.enters.append(name)
        self.active = self.active_after_enter
        return True

    def wait_active(self, name):
        self.waits.append(name)
        return self.active in ('working', 'blocked')

    def read(self, name, lines=80):
        self.screen_reads.append((name, lines))
        if self.shows_rid_after is not None and len(self.prompts) >= self.shows_rid_after:
            return 'some screen\n> text r-012820Z\n'
        return 'some screen without the id\n'


class ProxyTests(unittest.TestCase):
    def test_end_target_proxies_ends_and_waits_for_only_that_codespace(self):
        clock, killed, reads = Clock(), [], []
        ours = f'101 00:20 {PROXY} cs-old {STATE}'
        others = [f'102 00:20 {PROXY} cs-new {STATE}', f'103 00:20 {PROXY} cs-old {STATE}-other']
        def run(args, **kwargs):
            reads.append(clock.t)
            return listed(([ours] if len(reads) < 3 else []) + others)(args, **kwargs)
        with patch.object(project_state, 'process_identity', return_value=f'start {PROXY} cs-old {STATE}'), \
                patch.object(restore.os, 'kill', side_effect=lambda pid, sig: killed.append(pid)):
            self.assertEqual(restore.end_target_proxies('cs-old', Path(STATE), run=run,
                             now=clock.now, sleep=clock.sleep), 'ended')
            self.assertEqual(restore.end_target_proxies('cs-old', Path(STATE), run=listed(others)), 'absent')
        self.assertEqual(killed, [101])
        self.assertGreaterEqual(len(reads), 3)
        self.assertGreater(clock.t, reads[0])

    def test_another_projects_proxy_with_a_suffix_state_path_is_not_ours(self):
        other = Path('/different project ' + STATE)
        canonical = ' '.join(shlex.split(ssh_hosts._proxy_command(other, 'cs-old')))
        stray = f'/usr/bin/herdr --label herdr_stdio cs-old {STATE}'
        for command in (canonical, stray, f'{PROXY} invalid/name {STATE}', f'{PROXY} cs-old {STATE} extra'):
            with self.subTest(command=command):
                self.assertIsNone(restore._served(command, Path(STATE)))
        self.assertEqual(restore._served(f'/moved/python -I -B -c ' + PROXY.split(' -c ', 1)[1]
                         + f' cs-old {STATE}', Path(STATE)), 'cs-old')

    def test_a_reused_pid_is_not_signalled(self):
        with patch.object(project_state, 'process_identity',
                          side_effect=[f'start {PROXY} cs-old {STATE}', 'reused']) as identity, \
                patch.object(restore.os, 'kill') as kill:
            replies = iter([[f'101 00:20 {PROXY} cs-old {STATE}'], []])
            self.assertEqual(restore.end_target_proxies('cs-old', Path(STATE),
                             run=lambda *a, **k: listed(next(replies))(*a, **k)), 'ended')
        kill.assert_not_called()
        self.assertEqual(identity.call_count, 2)

    def test_a_pid_reused_before_its_first_identity_read_is_not_signalled(self):
        replies = iter([[f'101 00:20 {PROXY} cs-old {STATE}'], []])
        with patch.object(project_state, 'process_identity', return_value='start /unrelated/program'), \
                patch.object(restore.os, 'kill') as kill:
            self.assertEqual(restore.end_target_proxies('cs-old', Path(STATE),
                             run=lambda *a, **k: listed(next(replies))(*a, **k)), 'ended')
        kill.assert_not_called()

    def test_a_surviving_or_unlisted_proxy_keeps_the_step_pending(self):
        clock = Clock()
        with patch.object(project_state, 'process_identity', return_value=f'start {PROXY} cs-old {STATE}'), patch.object(restore.os, 'kill'):
            with self.assertRaisesRegex(ValueError, 'a Herdr connection to cs-old is still open'):
                restore.end_target_proxies('cs-old', Path(STATE), wait=4, now=clock.now, sleep=clock.sleep,
                                          run=listed([f'101 00:20 {PROXY} cs-old {STATE}']))
        for failure in (subprocess.CompletedProcess([], 1, '', ''), OSError('ps unavailable')):
            with patch.object(restore.subprocess, 'run', side_effect=failure if isinstance(failure, Exception) else None,
                              return_value=failure) as run:
                with self.assertRaisesRegex(ValueError, 'the process list cannot be read'):
                    restore.end_target_proxies('cs-old', Path(STATE), run=run)

    def test_stale_proxies_match_only_this_codespace_and_older(self):
        # Start times come from elapsed time (R35): no local-time parse a DST fall-back hour could misdate.
        now, before = 1_000_000.0, 1_000_000.0 - 600
        rows = [f'  101 {etime(3 * 86400 + 5)} {PROXY} cs-1 {STATE}',
                f'  102 {etime(4000)} {PROXY} cs-1 {STATE}',
                f'  103 {etime(601)} {PROXY} cs-1 {STATE}',
                f'  202 {etime(599)} {PROXY} cs-1 {STATE}',
                f'  303 {etime(4000)} {PROXY} cs-2 {STATE}',
                f'  404 {etime(4000)} {PROXY} cs-1 {STATE}-other',
                f'  505 {etime(4000)} gh codespace ssh -c cs-1 --stdio -- -i key {STATE}',
                f'  606 1:2:3:4 {PROXY} cs-1 {STATE}',
                'garbage line']
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            return listed(rows)(args, **kwargs)
        self.assertEqual(restore.stale_proxies('cs-1', Path(STATE), before, run=run, now=lambda: now),
                         [101, 102, 103])
        args, kwargs = calls[0]
        self.assertEqual(args, ['ps', '-axo', 'pid=,etime=,command='])
        self.assertIn('timeout', kwargs)
        for failure in (lambda *a, **k: subprocess.CompletedProcess(a, 1, '', ''),
                        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('ps', 10))):
            self.assertEqual(restore.stale_proxies('cs-1', Path(STATE), before, run=failure), [])

    def test_a_proxy_age_is_measured_when_ps_returns(self):
        # ps reports each age as it runs, so the clock is read after it, not before.
        clock, before = [1_000_100.0], 1_000_000.0
        def run(args, **kwargs):
            clock[0] += 5  # a slow ps on a busy Mac
            return listed([f'  101 {etime(103)} {PROXY} cs-1 {STATE}'])(args, **kwargs)
        # Started at 1_000_105 - 103, after `before`: a proxy of the restarted Codespace, not a stale one.
        self.assertEqual(restore.stale_proxies('cs-1', Path(STATE), before, run=run, now=lambda: clock[0]), [])

    def test_proxies_elsewhere_are_this_projects_proxies_serving_another_codespace(self):
        rows = [f'  101 {etime(900)} {PROXY} cs-old {STATE}',
                f'  102 {etime(5)} {PROXY} cs-older {STATE}',
                f'  201 {etime(900)} {PROXY} cs-new {STATE}',
                f'  301 {etime(900)} {PROXY} cs-old {STATE}-other',
                f'  401 {etime(900)} gh codespace ssh -c cs-old --stdio -- -i key {STATE}',
                'garbage line']
        self.assertEqual(restore.proxies_elsewhere('cs-new', Path(STATE), run=listed(rows)), [101, 102])
        for failure in (lambda *a, **k: subprocess.CompletedProcess(a, 1, '', ''),
                        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('ps', 10))):
            self.assertIsNone(restore.proxies_elsewhere('cs-new', Path(STATE), run=failure))
            self.assertIsNone(restore.end_proxies_elsewhere('cs-new', Path(STATE), run=failure))

    def test_end_proxies_elsewhere_signals_only_the_proxies_it_lists_for_another_codespace(self):
        def decoy():
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)
            return child
        old, current = decoy(), decoy()
        rows = [f'{old.pid} {etime(900)} {PROXY} cs-old {STATE}', f'{current.pid} {etime(900)} {PROXY} cs-new {STATE}']
        self.assertEqual(restore.end_proxies_elsewhere('cs-new', Path(STATE), run=listed(rows)), [old.pid])
        self.assertEqual(old.wait(timeout=5), -15)
        time.sleep(.2)
        self.assertIsNone(current.poll())

    def test_elapsed_time_parses_every_ps_shape(self):
        for text, seconds in (('00:05', 5), ('59:59', 3599), ('01:00:00', 3600), ('23:59:59', 86399),
                              ('1-00:00:00', 86400), ('12-03:04:05', 12 * 86400 + 3 * 3600 + 245)):
            with self.subTest(text=text):
                self.assertEqual(restore._elapsed(text), seconds)
                self.assertEqual(etime(seconds), text)
        for text in ('', '5', '1:2:3:4', 'a-00:01', '00:x1', '-00:01'):
            with self.subTest(text=text):
                self.assertIsNone(restore._elapsed(text))

    def test_end_stale_proxies_signals_only_the_matching_processes_it_lists(self):
        # Fake ps output naming only processes this test started; nothing else can be signalled.
        def decoy():
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)
            return child
        stale, other_codespace, newer = decoy(), decoy(), decoy()
        before = time.time() - 300
        rows = [f'{stale.pid} {etime(900)} {PROXY} cs-1 {STATE}',
                f'{other_codespace.pid} {etime(900)} {PROXY} cs-2 {STATE}',
                f'{newer.pid} {etime(60)} {PROXY} cs-1 {STATE}']
        self.assertEqual(restore.end_stale_proxies('cs-1', Path(STATE), before, run=listed(rows)), [stale.pid])
        self.assertEqual(stale.wait(timeout=5), -15)
        time.sleep(.2)
        self.assertIsNone(other_codespace.poll())
        self.assertIsNone(newer.poll())


FAKE_HERDR = '''#!{python}
import json, os, sys, time
args = sys.argv[1:]
with open({calls!r}, 'a') as out:
    out.write(json.dumps({{'args': args, 'env': sorted(os.environ)}}) + '\\n')
mode = os.environ.get('FAKE_HERDR_MODE', 'ok')
if mode == 'hang':
    time.sleep(5)
if mode == 'fail':
    sys.exit(1)
if mode == 'error':
    print(json.dumps({{'error': {{'code': os.environ['FAKE_HERDR_ERROR']}}, 'id': 'cli:agent:prompt'}}), file=sys.stderr)
    sys.exit(1)
if mode == 'bad-error':
    print('not json', file=sys.stderr)
    sys.exit(1)
if mode == 'odd-error':
    print(json.dumps({{'error': []}}), file=sys.stderr)
    sys.exit(1)
if mode == 'garbage':
    print('not json')
elif args[:2] == ['machine', 'status']:
    print(json.dumps([{{'id': args[2], 'status': 'reachable'}}]))
elif args[2:4] == ['agent', 'list']:
    print(json.dumps({{'result': {{'agents': [{{'name': 'lead-proj', 'agent_status': 'idle'}}, {{'pane_id': 'p2'}}]}}}}))
elif args[2:4] == ['agent', 'read']:
    print('screen text r-012820Z')
'''


class HerdrTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        tools = base / 'bin'
        tools.mkdir()
        self.calls = base / 'herdr-calls.jsonl'
        (tools / 'herdr').write_text(FAKE_HERDR.format(python=sys.executable, calls=str(self.calls)))
        (tools / 'herdr').chmod(0o700)
        environment = patch.dict(os.environ, {'HOME': str(base), 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                                              'HERDR_ENV': '1', 'HERDR_PANE_ID': 'pane-7', 'HERDR_SOCKET': '/x'})
        environment.start()
        self.addCleanup(environment.stop)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_prompt_waits_for_activity_with_a_longer_subprocess_timeout(self):
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, '{"result":{"type":"ok"}}', '')
        self.assertEqual(restore.Herdr('m-1', run=run).prompt('lead-proj', 'resume'), 'started')
        args, kwargs = calls[0]
        self.assertEqual(args, ['herdr', '--machine', 'm-1', 'agent', 'prompt', 'lead-proj', 'resume',
                                '--wait', '--until', 'working', '--until', 'blocked', '--timeout', '30000'])
        self.assertEqual(kwargs['timeout'], 60)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)

    def test_enter_and_wait_active_use_machine_and_drop_herdr_env(self):
        herdr = restore.Herdr('m-1')
        self.assertTrue(callable(getattr(herdr, 'enter', None)), 'Herdr.enter is missing')
        self.assertTrue(callable(getattr(herdr, 'wait_active', None)), 'Herdr.wait_active is missing')
        self.assertTrue(herdr.enter('lead-proj'))
        self.assertTrue(herdr.wait_active('lead-proj'))
        calls = self.recorded()
        self.assertEqual([call['args'] for call in calls], [
            ['--machine', 'm-1', 'agent', 'send-keys', 'lead-proj', 'enter'],
            ['--machine', 'm-1', 'agent', 'wait', 'lead-proj', '--until', 'working', '--until', 'blocked',
             '--timeout', '30000']])
        for call in calls:
            self.assertFalse([key for key in call['env'] if key.startswith('HERDR_')])
        with patch.dict(os.environ, {'FAKE_HERDR_MODE': 'fail'}):
            self.assertFalse(herdr.enter('lead-proj'))
            self.assertFalse(herdr.wait_active('lead-proj'))

    def test_prompt_preserves_stderr_error_codes(self):
        herdr = restore.Herdr('m-1')
        for code in ('agent_prompt_stalled', 'timeout', 'agent_not_found', 'agent_blocked'):
            with self.subTest(code=code), patch.dict(os.environ, {'FAKE_HERDR_MODE': 'error',
                                                                  'FAKE_HERDR_ERROR': code}):
                self.assertEqual(herdr.prompt('lead-proj', 'resume'), code)
                self.assertFalse(herdr.reachable())
                self.assertEqual(herdr.agents(), [])
                self.assertEqual(herdr.read('lead-proj'), '')
        for mode in ('fail', 'bad-error', 'odd-error'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'FAKE_HERDR_MODE': mode}):
                self.assertEqual(herdr.prompt('lead-proj', 'resume'), 'unknown')

    def test_prompt_reports_unavailable_on_hang_or_oserror(self):
        with patch.dict(os.environ, {'FAKE_HERDR_MODE': 'hang'}):
            self.assertEqual(restore.Herdr('m-1', timeout=.2).prompt('lead-proj', 'resume'), 'unavailable')
        with patch.dict(os.environ, {'PATH': '/nonexistent'}):
            self.assertEqual(restore.Herdr('m-1').prompt('lead-proj', 'resume'), 'unavailable')

    def test_input_guards_refuse_names_and_nul_without_spawning(self):
        herdr = restore.Herdr('m-1')
        for name in ('--wait', 'Lead X', '', None, 'lead\0x', 'x' * 33):
            with self.subTest(name=name):
                self.assertEqual(herdr.prompt(name, 'resume'), 'refused')
                self.assertFalse(herdr.enter(name))
                self.assertFalse(herdr.wait_active(name))
        self.assertEqual(herdr.prompt('lead-proj', 'resume\0text'), 'refused')
        self.assertFalse(self.calls.exists())

    def test_herdr_calls_drop_herdr_env(self):
        herdr = restore.Herdr('m-1')
        self.assertTrue(herdr.reachable())
        self.assertEqual([agent.get('name') for agent in herdr.agents()], ['lead-proj', None])
        self.assertEqual(herdr.prompt('lead-proj', 'Codespace restarted (r-012820Z)'), 'started')
        self.assertIn('r-012820Z', herdr.read('lead-proj'))
        calls = self.recorded()
        self.assertEqual([call['args'] for call in calls], [
            ['machine', 'status', 'm-1', '--json'],
            ['--machine', 'm-1', 'agent', 'list'],
            ['--machine', 'm-1', 'agent', 'prompt', 'lead-proj', 'Codespace restarted (r-012820Z)',
             '--wait', '--until', 'working', '--until', 'blocked', '--timeout', '30000'],
            ['--machine', 'm-1', 'agent', 'read', 'lead-proj', '--source', 'recent-unwrapped', '--lines', '80']])
        for call in calls:
            self.assertFalse([key for key in call['env'] if key.startswith('HERDR_')], call['env'])

    def test_hanging_herdr_is_bounded_and_not_fatal(self):
        herdr = restore.Herdr('m-1', timeout=0.2)
        with patch.dict(os.environ, {'FAKE_HERDR_MODE': 'hang'}):
            started = time.monotonic()
            self.assertFalse(herdr.reachable())
            self.assertEqual(herdr.agents(), [])
            self.assertEqual(herdr.prompt('lead-proj', 'text'), 'unavailable')
            self.assertEqual(herdr.read('lead-proj'), '')
            self.assertLess(time.monotonic() - started, 4)
        for mode in ('fail', 'garbage'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'FAKE_HERDR_MODE': mode}):
                self.assertFalse(herdr.reachable())
                self.assertEqual(herdr.agents(), [])
        with patch.dict(os.environ, {'FAKE_HERDR_MODE': 'fail'}):
            self.assertEqual(herdr.prompt('lead-proj', 'text'), 'unknown')
            self.assertEqual(herdr.read('lead-proj'), '')
        with patch.dict(os.environ, {'PATH': '/nonexistent'}):
            self.assertFalse(restore.Herdr('m-1').reachable())

    def test_odd_machine_ids_and_agent_names_never_reach_argv(self):
        for machine in ('', '-x', None, 'a b'):
            with self.subTest(machine=machine), self.assertRaisesRegex(ValueError, 'task work:cs'):
                restore.Herdr(machine)
        herdr = restore.Herdr('m-1')
        self.assertEqual(herdr.prompt('--wait', 'text'), 'refused')
        self.assertEqual(herdr.read('Lead X'), '')
        self.assertFalse(self.calls.exists())

    def test_reachable_treats_any_other_status_as_not_yet(self):
        # Measured 2026-10-22: 'error' ("remote Herdr server is stopped or incompatible") right after
        # Available, then 'reachable' 28 s later with no action from us.
        answers = [[{'status': 'error', 'message': 'remote Herdr server is stopped or incompatible'}]] * 3
        answers += [[{'status': 'reachable'}]]
        commands = []

        def run(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps(answers[min(len(commands), len(answers)) - 1]), '')
        clock = Clock()
        at = restore.wait_reachable(restore.Herdr('m-1', run=run), now=clock.now, sleep=clock.sleep)
        self.assertEqual(at, 1_000_000.0 + 3 * restore.POLL)
        self.assertEqual(commands, [['herdr', 'machine', 'status', 'm-1', '--json']] * 4)
        never = Clock()
        unreachable = restore.Herdr('m-1', run=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, '[]', ''))
        self.assertIsNone(restore.wait_reachable(unreachable, now=never.now, sleep=never.sleep))
        self.assertEqual(never.t - 1_000_000.0, restore.REACH_WAIT)


class LeadTests(unittest.TestCase):
    def test_lead_requires_exactly_one(self):
        lead = {'name': 'lead-consult-sm-mainstage', 'agent_status': 'idle'}
        # Measured: the real lead beside an unnamed agent.
        self.assertEqual(restore.lead([{'pane_id': 'p1'}, {'name': None}, lead, {'name': 'worker-xhigh-1a2b'}]), lead)
        self.assertIsNone(restore.lead([]))
        self.assertIsNone(restore.lead([{'name': 'worker'}, {'pane_id': 'p1'}]))
        self.assertIsNone(restore.lead([lead, {'name': 'lead-other'}]))

    def test_resume_text_is_verbatim(self):
        stopped = 1_792_632_500.0  # 2026-10-22 01:28:20Z
        text = restore.resume_text('r-012820Z', stopped, stopped + 180)
        # R34: spec 6.6 exactly, with a period after "work".
        self.assertEqual(text, 'Codespace restarted, we are back. You need to bring the services that you require '
                               "up and continue with the work. (Automatic restart r-012820Z: GitHub's 12-hour stop at "
                               '01:28Z, back at 01:31Z. Restored agents came back with --resume and without their '
                               "launch flags, so check each worker's model, effort and sandbox before dispatching "
                               'to it.)')
        planned = restore.resume_text('o-012820Z', stopped, stopped + 180)
        self.assertTrue(planned.startswith(restore.SENTENCE + '. (Planned restart o-012820Z: stopped at 01:28Z, '
                                                              'back at 01:31Z. Restored agents'))
        self.assertNotIn('12-hour', planned)


class ResumeTests(unittest.TestCase):
    def resume(self, herdr, clock):
        return restore.resume(herdr, 'r-012820Z', 'text r-012820Z', now=clock.now, sleep=clock.sleep)

    def test_rid_on_screen_without_activity_is_not_delivered(self):
        clock = Clock()
        herdr = FakeHerdr(clock, shows_rid_after=1)
        self.assertEqual(self.resume(herdr, clock), ('undelivered', 'lead-proj'))
        self.assertEqual(herdr.enters, ['lead-proj'])
        self.assertEqual(len(herdr.prompts), 1)
        self.assertEqual(herdr.waits, ['lead-proj'])
        self.assertEqual(len(herdr.screen_reads), 1)

    def test_unsent_text_gets_one_enter_then_counts_as_entered(self):
        for status, outcome in (('working', 'entered'), ('blocked', 'blocked')):
            with self.subTest(status=status):
                clock = Clock()
                herdr = FakeHerdr(clock, shows_rid_after=1, active_after_enter=status)
                self.assertEqual(self.resume(herdr, clock), (outcome, 'lead-proj'))
                self.assertEqual(len(herdr.prompts), 1)
                self.assertEqual(herdr.enters, ['lead-proj'])
                self.assertEqual(herdr.waits, ['lead-proj'])
                self.assertEqual(len(herdr.screen_reads), 1)
        clock = Clock()
        refused = FakeHerdr(clock, shows_rid_after=1)
        with patch.object(refused, 'enter', return_value=False), patch.object(refused, 'wait_active') as wait:
            self.assertEqual(self.resume(refused, clock), ('undelivered', 'lead-proj'))
        wait.assert_not_called()

    def test_lost_text_is_prompted_once_more(self):
        for second, status, outcome in (('started', 'working', 'resent'), ('started', 'blocked', 'blocked'),
                                        ('agent_prompt_stalled', 'idle', 'undelivered')):
            with self.subTest(second=second, status=status):
                clock = Clock()
                herdr = FakeHerdr(clock, prompt_results=('agent_prompt_stalled', second), started_status=status)
                self.assertEqual(self.resume(herdr, clock), (outcome, 'lead-proj'))
                self.assertEqual(len(herdr.prompts), 2)
                self.assertEqual(herdr.enters, [])
                self.assertEqual(herdr.waits, [])
                self.assertEqual(len(herdr.screen_reads), 1)

    def test_no_input_after_a_stall_unless_ready(self):
        for status, outcome in (('blocked', 'blocked'), ('unknown', 'undelivered'), (None, 'undelivered'),
                                ('working', 'delivered')):
            with self.subTest(status=status):
                clock = Clock()
                herdr = FakeHerdr(clock, statuses=('idle', 'idle', 'idle', status), shows_rid_after=1)
                self.assertEqual(self.resume(herdr, clock), (outcome, 'lead-proj'))
                self.assertEqual(len(herdr.prompts), 1)
                self.assertEqual(herdr.enters, [])
                self.assertEqual(herdr.waits, [])
                self.assertEqual(herdr.screen_reads, [])

    def test_started_prompt_is_delivered_without_reading_the_screen(self):
        for status, outcome in (('working', 'delivered'), ('blocked', 'blocked'), ('idle', 'delivered'),
                                (None, 'delivered')):
            with self.subTest(status=status):
                clock = Clock()
                herdr = FakeHerdr(clock, working_after_prompt=True, started_status=status)
                with patch.object(herdr, 'agents', wraps=herdr.agents) as agents:
                    self.assertEqual(self.resume(herdr, clock), (outcome, 'lead-proj'))
                self.assertEqual(agents.call_count, 4)  # Find, settle twice, then read status once.
                self.assertEqual(herdr.screen_reads, [])
                self.assertEqual(herdr.enters, [])
                self.assertEqual(len(herdr.prompts), 1)

    def test_settled_waits_for_interactive_ready(self):
        clock = Clock()
        never = FakeHerdr(clock, working_after_prompt=True, interactive_ready=False)
        self.assertEqual(self.resume(never, clock), ('busy', 'lead-proj'))
        self.assertEqual(never.prompts, [])
        self.assertEqual(clock.t - 1_000_000.0, restore.IDLE_WAIT)
        for ready, delay in ((None, 1), ((False, False, True, True), 2)):
            with self.subTest(ready=ready):
                clock = Clock()
                herdr = FakeHerdr(clock, working_after_prompt=True, interactive_ready=ready)
                self.assertEqual(self.resume(herdr, clock), ('delivered', 'lead-proj'))
                self.assertEqual(herdr.prompts[0][2] - 1_000_000.0, delay * restore.SETTLE)

    def test_resume_names_the_lead_it_prompted(self):
        # The keeper's notice names the lead from this answer, with no Herdr call of its own.
        clock = Clock()
        self.assertEqual(self.resume(FakeHerdr(clock, ['idle'], working_after_prompt=True), clock),
                         ('delivered', 'lead-proj'))

    def test_resume_delivers_once_the_lead_is_idle_on_two_reads(self):
        clock = Clock()
        # The first read finds the lead; then idle at 0, working at 10, idle at 20 and 30.
        herdr = FakeHerdr(clock, ['idle', 'idle', 'working', 'idle', 'idle'], working_after_prompt=True)
        self.assertEqual(self.resume(herdr, clock), ('delivered', 'lead-proj'))
        self.assertEqual(len(herdr.prompts), 1)
        # The prompt follows the second idle read in a row.
        self.assertEqual(herdr.prompts[0][2], 1_000_000.0 + 3 * restore.SETTLE)
        clock = Clock()
        by_screen = FakeHerdr(clock, ['idle'], shows_rid_after=1, active_after_enter='working')
        self.assertEqual(self.resume(by_screen, clock), ('entered', 'lead-proj'))
        self.assertEqual(len(by_screen.prompts), 1)
        self.assertEqual(by_screen.enters, ['lead-proj'])

    def test_resume_resends_once_when_rid_absent(self):
        clock = Clock()
        herdr = FakeHerdr(clock, ['idle'], shows_rid_after=2,
                          prompt_results=('agent_prompt_stalled', 'started'))
        self.assertEqual(self.resume(herdr, clock), ('resent', 'lead-proj'))
        self.assertEqual(len(herdr.prompts), 2)
        self.assertEqual(herdr.prompts[1][2], herdr.prompts[0][2])
        clock = Clock()
        never = FakeHerdr(clock, ['idle'])
        self.assertEqual(self.resume(never, clock), ('undelivered', 'lead-proj'))
        self.assertEqual(len(never.prompts), 2)
        clock = Clock()
        # Not idle after the first prompt (blocked on a dialog): no second prompt.
        blocked = FakeHerdr(clock, ['idle', 'idle', 'idle', 'blocked'])
        self.assertEqual(self.resume(blocked, clock), ('blocked', 'lead-proj'))
        self.assertEqual(len(blocked.prompts), 1)

    def test_resume_waits_for_idle_then_gives_up_busy(self):
        clock = Clock()
        herdr = FakeHerdr(clock, ['working'])
        self.assertEqual(self.resume(herdr, clock), ('busy', 'lead-proj'))
        self.assertEqual(herdr.prompts, [])
        self.assertGreaterEqual(clock.t - 1_000_000.0, restore.IDLE_WAIT)
        self.assertLess(clock.t - 1_000_000.0, restore.IDLE_WAIT + 2 * restore.SETTLE)

    def test_every_herdr_call_is_followed_by_a_tick(self):
        # Each call may take its whole 60 s timeout; the keeper's heartbeat follows each one.
        clock, log = Clock(), []

        class Counting(FakeHerdr):
            def reachable(self):
                log.append('call')
                return True

            def agents(self):
                log.append('call')
                return super().agents()

            def prompt(self, name, text):
                log.append('call')
                return super().prompt(name, text)

            def read(self, name, lines=80):
                log.append('call')
                return super().read(name, lines)

            def enter(self, name):
                log.append('call')
                return super().enter(name)

            def wait_active(self, name):
                log.append('call')
                return super().wait_active(name)
        for shows_rid in (None, 1):
            with self.subTest(shows_rid=shows_rid):
                log.clear()
                herdr = Counting(clock, ['done'], shows_rid_after=shows_rid)
                restore.wait_reachable(herdr, now=clock.now, sleep=clock.sleep, tick=lambda: log.append('tick'))
                self.assertEqual(restore.resume(herdr, 'r-012820Z', 'text', now=clock.now, sleep=clock.sleep,
                                                tick=lambda: log.append('tick')), ('undelivered', 'lead-proj'))
                self.assertEqual(log, ['call', 'tick'] * log.count('call'))
                if shows_rid:
                    self.assertEqual(herdr.enters, ['lead-proj'])
                    self.assertEqual(herdr.waits, ['lead-proj'])
                else:
                    self.assertEqual(len(herdr.prompts), 2)

    def test_resume_without_exactly_one_lead_sends_nothing(self):
        clock = Clock()
        two = FakeHerdr(clock, agents=[{'name': 'lead-other', 'agent_status': 'idle'}])
        self.assertEqual(self.resume(two, clock), ('no-lead', None))
        self.assertEqual(two.prompts, [])


if __name__ == '__main__':
    unittest.main()
