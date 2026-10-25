"""Restore after a restart: stale Herdr proxies, the Herdr adapter, the lead and its resume."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync import restore

STATE = '/Users/me/.local/state/devcontainer-setup/projects/0123456789abcdef01234567'
PROXY = ("/usr/bin/python3 -I -B -c import sys;sys.path.insert(0,'/repo');from "
         'devcontainer_setup.project_sync.transport import herdr_stdio;'
         'raise SystemExit(herdr_stdio(sys.argv[1],sys.argv[2]))')


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
    """One lead whose status a script drives; prompt and read are recorded."""

    def __init__(self, clock, statuses=('idle',), shows_rid_after=None, working_after_prompt=False, agents=None):
        self.clock, self.statuses, self.reads = clock, list(statuses), 0
        self.shows_rid_after, self.working_after_prompt = shows_rid_after, working_after_prompt
        self.prompts, self.extra = [], agents or []

    def status(self):
        if self.working_after_prompt and self.prompts:
            return 'working'
        status = self.statuses[min(self.reads, len(self.statuses) - 1)]
        self.reads += 1
        return status

    def agents(self):
        return [{'name': 'lead-proj', 'agent_status': self.status()}] + self.extra

    def prompt(self, name, text):
        self.prompts.append((name, text, self.clock.t))
        return True

    def read(self, name, lines=80):
        if self.shows_rid_after is not None and len(self.prompts) >= self.shows_rid_after:
            return 'some screen\n> r-012820Z noted, bringing services up\n'
        return 'some screen without the id\n'


class ProxyTests(unittest.TestCase):
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

    def test_herdr_calls_drop_herdr_env(self):
        herdr = restore.Herdr('m-1')
        self.assertTrue(herdr.reachable())
        self.assertEqual([agent.get('name') for agent in herdr.agents()], ['lead-proj', None])
        self.assertTrue(herdr.prompt('lead-proj', 'Codespace restarted (r-012820Z)'))
        self.assertIn('r-012820Z', herdr.read('lead-proj'))
        calls = self.recorded()
        self.assertEqual([call['args'] for call in calls], [
            ['machine', 'status', 'm-1', '--json'],
            ['--machine', 'm-1', 'agent', 'list'],
            ['--machine', 'm-1', 'agent', 'prompt', 'lead-proj', 'Codespace restarted (r-012820Z)'],
            ['--machine', 'm-1', 'agent', 'read', 'lead-proj', '--source', 'recent-unwrapped', '--lines', '80']])
        for call in calls:
            self.assertFalse([key for key in call['env'] if key.startswith('HERDR_')], call['env'])
            # A restored agent can read idle before its UI is up; --wait then times out though delivered.
            self.assertNotIn('--wait', call['args'])

    def test_hanging_herdr_is_bounded_and_not_fatal(self):
        herdr = restore.Herdr('m-1', timeout=0.2)
        with patch.dict(os.environ, {'FAKE_HERDR_MODE': 'hang'}):
            started = time.monotonic()
            self.assertFalse(herdr.reachable())
            self.assertEqual(herdr.agents(), [])
            self.assertFalse(herdr.prompt('lead-proj', 'text'))
            self.assertEqual(herdr.read('lead-proj'), '')
            self.assertLess(time.monotonic() - started, 4)
        for mode in ('fail', 'garbage'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'FAKE_HERDR_MODE': mode}):
                self.assertFalse(herdr.reachable())
                self.assertEqual(herdr.agents(), [])
        with patch.dict(os.environ, {'FAKE_HERDR_MODE': 'fail'}):
            self.assertFalse(herdr.prompt('lead-proj', 'text'))
            self.assertEqual(herdr.read('lead-proj'), '')
        with patch.dict(os.environ, {'PATH': '/nonexistent'}):
            self.assertFalse(restore.Herdr('m-1').reachable())

    def test_odd_machine_ids_and_agent_names_never_reach_argv(self):
        for machine in ('', '-x', None, 'a b'):
            with self.subTest(machine=machine), self.assertRaisesRegex(ValueError, 'task work:cs'):
                restore.Herdr(machine)
        herdr = restore.Herdr('m-1')
        self.assertFalse(herdr.prompt('--wait', 'text'))
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
        by_screen = FakeHerdr(clock, ['idle'], shows_rid_after=1)
        self.assertEqual(self.resume(by_screen, clock), ('delivered', 'lead-proj'))
        self.assertEqual(len(by_screen.prompts), 1)

    def test_resume_resends_once_when_rid_absent(self):
        clock = Clock()
        herdr = FakeHerdr(clock, ['idle'], shows_rid_after=2)
        self.assertEqual(self.resume(herdr, clock), ('resent', 'lead-proj'))
        self.assertEqual(len(herdr.prompts), 2)
        self.assertEqual(herdr.prompts[1][2] - herdr.prompts[0][2], restore.PROOF_WAIT)
        clock = Clock()
        never = FakeHerdr(clock, ['idle'])
        self.assertEqual(self.resume(never, clock), ('undelivered', 'lead-proj'))
        self.assertEqual(len(never.prompts), 2)
        clock = Clock()
        # Not idle after the first prompt (blocked on a dialog): no second prompt.
        blocked = FakeHerdr(clock, ['idle', 'idle', 'idle', 'blocked'])
        self.assertEqual(self.resume(blocked, clock), ('undelivered', 'lead-proj'))
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
        herdr = Counting(clock, ['idle'])
        restore.wait_reachable(herdr, now=clock.now, sleep=clock.sleep, tick=lambda: log.append('tick'))
        self.assertEqual(restore.resume(herdr, 'r-012820Z', 'text', now=clock.now, sleep=clock.sleep,
                                        tick=lambda: log.append('tick')), ('undelivered', 'lead-proj'))
        self.assertGreater(log.count('call'), 10)
        self.assertEqual(log, ['call', 'tick'] * log.count('call'))

    def test_resume_without_exactly_one_lead_sends_nothing(self):
        clock = Clock()
        two = FakeHerdr(clock, agents=[{'name': 'lead-other', 'agent_status': 'idle'}])
        self.assertEqual(self.resume(two, clock), ('no-lead', None))
        self.assertEqual(two.prompts, [])


if __name__ == '__main__':
    unittest.main()
