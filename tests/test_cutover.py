"""Replacement cutover uses local records and scripted remote evidence only."""
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from devcontainer_setup.project_sync import arming, lifecycle, move, replacing, scratch, seal, sync_cycle, target_scan, verdict, workspace
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.state import Project, atomic_write, read_json
from tests.test_deletion import FakeAPI
from tests.test_replacing import journal


class CutoverTests(unittest.TestCase):
    def setUp(self):
        name = 'devcontainer_setup.project_sync.cutover'
        self.assertIsNotNone(importlib.util.find_spec(name), 'cutover module is missing')
        self.c = importlib.import_module(name)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root, self.state = self.base / 'project', self.base / 'state'
        (self.root / '.scratch').mkdir(parents=True)
        identity = 'github.com/example/project'
        self.project = Project(self.root, 'https://' + identity, identity, hashlib.sha256(identity.encode()).hexdigest()[:24])
        self.clock, self.order = 1000.0, []
        self.operation = '012345abcdef'
        self.receipt_path = self.state / 'receipt.json'
        self.bind('old-space')
        self.write('workspace.json', dict(schema=1, identity=identity, project_id=self.project.project_id,
                   local_root=str(self.root), local_active_root=str(self.root), active_host='codespace',
                   codespace='old-space', remote_root='/workspaces/project'))
        self.journal = journal()
        replacing.write(self.state, self.journal)
        self.store = Store(self.state)
        manifest = dict(schema=2, project=dict(identity=identity, project_id=self.project.project_id, root='/workspaces/project'),
                        conversations=dict(schema=1, entries=[], issues=[]),
                        producer=dict(format='devcontainer-project-conversations-v2'))
        self.snapshot = self.store.save(manifest)
        receipt = self.store.acknowledge(self.snapshot, source='codespace')
        inbound = sync_cycle.direction()
        inbound.update(outcome='complete', snapshot_id=self.snapshot, receipt=receipt)
        cycle = dict(schema=3, cycle_id='final-cycle', outcome='complete', started_at=999.0, completed_at=1000.0,
                     issues=[], directions=dict(codespace_to_desktop=inbound))
        self.scan = target_scan.Scan('old-space', self.clock, (), True, (), True, (), True,
                                    target_scan.cutoffs_digest({}), (), (), (), (), True, ())
        self.evidence = verdict.Evidence(self.scan, True, (), (), (), True, cycle, receipt, (), None, ())
        self.api, self.factory = FakeAPI(states=('Available',)), Mock()
        self.factory.return_value = self.api
        self.connection = Mock()
        self.connection.__enter__ = Mock(return_value=self.connection)
        self.connection.__exit__ = Mock(return_value=False)
        self.connect = Mock(return_value=self.connection)
        self.recovery = Mock()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(arming, 'account', return_value='octo'))
        self.runtime = dict(running=True, phase='syncing', checkpoint_version=sync_cycle.SCHEMA, runtime_id=scratch._runtime_id(),
                            heartbeat_at=time.time())
        self.status = self.stack.enter_context(patch.object(scratch, 'status', side_effect=lambda *_: dict(self.runtime)))
        self.stop = self.stack.enter_context(patch.object(scratch, 'stop', side_effect=self.stop_old))
        self.decide = self.stack.enter_context(patch.object(move, 'decide', side_effect=self.decide_move))
        self.actual_finish = move.finish_pending
        self.finish = self.stack.enter_context(patch.object(move, 'finish_pending', side_effect=lambda *_: self.order.append('finish')))
        self.scanner = self.stack.enter_context(patch.object(target_scan, 'scan', side_effect=self.scan_old))
        self.gather = self.stack.enter_context(patch.object(self.c.deletion, 'gather', side_effect=self.gather_old))
        self.sealer = self.stack.enter_context(patch.object(seal, 'write', wraps=seal.write))
        self.actual_remote = workspace.remote
        self.remote = self.stack.enter_context(patch.object(workspace, 'remote', side_effect=self.move_remote))
        self.ask = Mock(side_effect=lambda _: self.order.append('ask') or 'stop')

    def write(self, path, value):
        atomic_write(self.state / path, json.dumps(value).encode())

    def bind(self, name):
        self.write('scratch.json', dict(version=1, identity=self.project.identity, root=str(self.root),
                                        codespace=name, remote_root='/workspaces/project'))

    def decide_move(self, *args, **kwargs):
        self.order.append('decide')
        return None

    def scan_old(self, *args, **kwargs):
        self.order.append('scan')
        return self.scan

    def gather_old(self, *args, **kwargs):
        self.order.append('gather')
        return self.evidence

    def stop_old(self, *args):
        self.order.append('stop')
        self.runtime.update(running=False, phase='stopped')
        return dict(self.runtime)

    def move_remote(self, *args, **kwargs):
        self.order.append('remote')
        if 'expected_old' in kwargs and read_json(self.state / 'scratch.json')['codespace'] != kwargs['expected_old']:
            raise ValueError('old-space is no longer the paired Codespace; nothing moved.')
        if kwargs.get('move') is not None:
            move.check_arming(self.state, kwargs['move'])
        if kwargs.get('rebind'):
            self.bind(kwargs['rebind'])
        self.runtime.update(runtime_id=scratch._runtime_id(), running=True, phase='syncing',
                            checkpoint_version=sync_cycle.SCHEMA, heartbeat_at=time.time())
        return 0

    def run_cutover(self, **options):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.c.run(self.project, self.state, operation=self.operation, old='old-space', new='new-space',
                              receipt=self.receipt_path, recovery=self.recovery, ask=self.ask, tty=lambda: True,
                              api_factory=self.factory, connect=self.connect, now=lambda: self.clock,
                              sleep=self.advance, **options)
        return code, out.getvalue()

    def advance(self, seconds):
        self.clock += seconds

    def arm(self):
        arming.arm(self.state, gh_user='octo', codespace='old-space', billable_owner='octo',
                   consent=arming.consent_text(self.root.name, 'old-space', 'octo'), ports=[], now=1.0)
        raw = (self.state / arming.ARMING).read_bytes()
        decision = move.Decision('old-space', 'new-space', 'octo', 'octo', True, 'a' * 64,
                                 hashlib.sha256(raw).hexdigest(), ())
        self.decide.side_effect = lambda *a, **k: self.order.append('decide') or decision
        return decision

    def real_move(self, verify=None):
        """Keep publication real; fake remote IO and detached worker/keeper starts."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.finish.side_effect = lambda *a: self.order.append('finish') or self.actual_finish(*a)
        self.remote.side_effect = lambda *a, **k: self.order.append('remote') or self.actual_remote(*a, **k)
        def choose(project, state, value, rebind=None, **options):
            return rebind or read_json(self.state / 'scratch.json')['codespace'], '/workspaces/project'
        def start(*args):
            self.order.append('start:' + read_json(self.state / 'scratch.json')['codespace'])
            self.runtime.update(running=True, phase='syncing', runtime_id=scratch._runtime_id(),
                                checkpoint_version=sync_cycle.SCHEMA, heartbeat_at=time.time())
        for item in (patch.object(workspace, '_prerequisites'),
                     patch.object(workspace, '_project', return_value=self.project),
                     patch.object(workspace, '_choose_pairing', side_effect=choose),
                     patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection),
                     patch.object(workspace, '_verify_roots', return_value=[]),
                     patch.object(workspace, '_remote_herdr', return_value='herdr fixture'),
                     patch.object(workspace, '_remember_account'),
                     patch.object(scratch, 'discover_project', return_value=self.project),
                     patch.object(scratch, '_verify_destination', side_effect=verify),
                     patch.object(scratch, 'start', side_effect=start),
                     patch.object(workspace, '_ready', return_value={}),
                     patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'),
                     patch('devcontainer_setup.project_sync.restore.end_proxies_elsewhere', return_value=[]),
                     patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0),
                     patch('devcontainer_setup.project_sync.keeper.stop', side_effect=lambda *_: self.order.append('keeper-stop'))):
            stack.enter_context(item)
        return stack

    def assert_failed_move_flushes_again(self, *, armed=False, retarget_intent=False, worker_current=False):
        if armed:
            self.arm()
        actual_write = scratch._write
        def fail_after_intent(path, value):
            actual_write(path, value)
            if path == self.state / 'scratch-retarget.json':
                raise ValueError('fixture failed after retarget intent')
        verify = None if retarget_intent else Mock(side_effect=ValueError('fixture NEW unreachable'))
        with self.real_move(verify), patch.object(scratch, '_write', side_effect=fail_after_intent):
            code, out = self.run_cutover()
        self.assertEqual(code, 1, out)
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'old-space')
        self.assertEqual(read_json(self.state / workspace.PENDING)['codespace'], 'new-space')
        self.assertEqual((self.state / move.INTENT).exists(), armed)
        self.assertEqual((self.state / 'scratch-retarget.json').exists(), retarget_intent)
        self.assertEqual('keeper-stop' in self.order, armed)
        self.assertFalse(self.runtime['running'])
        self.assertEqual(seal.read(self.state, 'old-space').sealed_at, self.clock)
        replacing.write(self.state, dict(self.journal, moving=self.clock))

        # OLD can acquire new work after any failed move, including after scratch:start.
        self.runtime.update(running=worker_current, phase='syncing' if worker_current else 'stopped')
        self.clock += 86400
        self.scan = replace(self.scan, at=self.clock, scratch=(('notes/late.md', 'f' * 64),))
        self.evidence = replace(self.evidence, scan=self.scan, mac_scratch=self.scan.scratch)
        scans = iter((replace(self.scan, agents=(('claude', 1),)), self.scan, self.scan))
        def scan(*args, **kwargs):
            self.order.append('scan')
            return next(scans)
        self.scanner.side_effect = scan
        self.ask.side_effect = lambda _: self.order.append('ask') or ''
        self.recovery.side_effect = lambda *_: self.order.append('recovery:' + arming.read(self.state)['codespace'])
        self.order.clear()
        self.api.calls.clear()
        for leaf in (self.factory, self.scanner, self.gather, self.sealer, self.ask, self.remote, self.recovery):
            leaf.reset_mock()
        with self.real_move():
            code, out = self.run_cutover()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.api.calls, ['read'], 'rerun must read OLD Available once')
        self.factory.assert_called_once_with('old-space', gh_user='octo')
        self.assertEqual(self.scanner.call_count, 3)
        self.ask.assert_called_once_with('> ')
        self.assertIn('On old-space: claude (1) running.', out)
        self.gather.assert_called_once()
        self.sealer.assert_called_once()
        self.assertEqual(seal.read(self.state, 'old-space').sealed_at, self.clock)
        self.assertEqual(seal.read(self.state, 'old-space').scratch, self.scan.scratch)
        self.assertLess(self.order.index('ask'), self.order.index('gather'))
        if worker_current:
            self.remote.assert_called_once()
            self.assertNotIn('start:old-space', self.order)
            self.assertEqual(self.remote.call_args.kwargs['rebind'], 'new-space')
        else:
            self.assertEqual(self.remote.call_count, 2)
            self.assertEqual(self.remote.call_args_list[0].kwargs,
                             dict(armed=self.recovery if armed else None, enter=False,
                                  expected_old='old-space', start=False))
            self.assertLess(self.order.index('start:old-space'), self.order.index('scan'))
        if armed:
            self.recovery.assert_any_call(self.project)
            self.assertEqual(self.order.count('recovery:old-space'), 1)
            self.assertLess(self.order.index('recovery:old-space'), self.order.index('scan'))
            self.assertLess(self.order.index('recovery:old-space'), self.order.index('gather'))
            self.assertEqual(self.order.count('recovery:new-space'), 1)
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'new-space')
        for name in ('scratch-retarget.json', move.INTENT, workspace.PENDING):
            self.assertFalse((self.state / name).exists(), name)
        receipt = read_json(self.receipt_path)
        self.assertTrue(receipt['moved'])
        self.assertEqual(receipt['sealed_at'], self.clock)
        self.assertEqual(replacing.read(self.state)['moving'], 1000.0)

    def test_unreachable_new_flushes_again_before_moving(self):
        self.assert_failed_move_flushes_again()

    def test_armed_failed_move_recovers_old_before_a_fresh_flush(self):
        self.assert_failed_move_flushes_again(armed=True)

    def test_failed_retarget_intent_flushes_again_before_moving(self):
        self.assert_failed_move_flushes_again(retarget_intent=True)

    def test_current_old_worker_flushes_again_under_a_pending_registry(self):
        self.assert_failed_move_flushes_again(worker_current=True)

    def test_consent_is_decided_before_the_flush(self):
        decision = self.arm()
        original = self.sealer._mock_wraps
        def write(*args, **kwargs):
            self.order.append('seal')
            return original(*args, **kwargs)
        self.sealer.side_effect = write
        self.assertEqual(self.run_cutover()[0], 0)
        self.assertEqual(self.order, ['decide', 'scan', 'gather', 'stop', 'scan', 'seal', 'remote'])
        self.remote.assert_called_once_with(self.project, self.state, rebind='new-space', move=decision,
                                            armed=self.recovery, enter=False, expected_old='old-space')
        self.assertEqual(self.api.calls, ['read'])

    def test_decline_moves_nothing_and_writes_no_seal(self):
        self.decide.side_effect = ValueError('Move cancelled; nothing moved.')
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn('Nothing moved.', out)
        for leaf in (self.gather, self.sealer, self.remote):
            leaf.assert_not_called()
        self.assertFalse(read_json(self.receipt_path)['moved'])

    def test_final_flush_uses_singular_for_one_file_and_conversation(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                self.bind('old-space')
                (self.state / 'retired/old-space.json').unlink(missing_ok=True)
                manifest = self.store.load(self.snapshot)
                digest = self.store.put(b'completed\n')
                entries = []
                for index in range(count):
                    sid = f'{index + 1:08d}-1111-4111-8111-111111111111'
                    entries.append(dict(agent='codex', session_id=sid, files=[
                        dict(session_id=sid, path='rollout-' + sid + '.jsonl',
                             size=len(b'completed\n'), sha256=digest, blob=digest)]))
                manifest['conversations']['entries'] = entries
                self.snapshot = self.store.save(manifest)
                receipt = self.store.acknowledge(self.snapshot, source='codespace')
                inbound = sync_cycle.direction()
                inbound.update(outcome='complete', snapshot_id=self.snapshot, receipt=receipt)
                cycle = dict(self.evidence.cycle, directions=dict(codespace_to_desktop=inbound))
                cutoffs = verdict.cutoffs(manifest)
                files = tuple((f'note-{index}.md', digest) for index in range(count))
                sessions = tuple(sorted(('codex/sessions/' + name, cutoff.sha256)
                                        for (_, name), cutoff in cutoffs.items()))
                self.scan = replace(self.scan, scratch=files, sessions=sessions,
                                    checked=tuple(sorted(cutoffs)), cutoffs_sha256=target_scan.cutoffs_digest(cutoffs))
                self.evidence = replace(self.evidence, scan=self.scan, mac_scratch=files, cycle=cycle,
                                        receipt=receipt, sessions_by_file=verdict.sessions_by_file(manifest))
                code, out = self.run_cutover()
                self.assertEqual(code, 0, out)
                when = self.c.overview._when(self.clock, self.clock)
                noun = 'file' if count == 1 else 'files'
                conversation = 'conversation' if count == 1 else 'conversations'
                self.assertIn(f'Final flush of old-space recorded at {when} '
                              f'({count} {noun}, {count} {conversation}).', out)
                self.assertEqual(seal.read(self.state, 'old-space').scratch, files)

    def test_agents_are_asked_never_killed(self):
        running = replace(self.scan, agents=(('claude', 2), ('codex', 1), ('herdr', 1)))
        for answer in ('', 'stop', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=type(answer).__name__ if isinstance(answer, BaseException) else answer):
                self.bind('old-space')
                self.scanner.side_effect = [running, replace(self.scan, agents=(('herdr', 1),)), self.scan]
                self.ask.side_effect = answer if isinstance(answer, BaseException) else lambda _: answer
                self.gather.reset_mock()
                code, out = self.run_cutover()
                self.assertIn('On old-space: claude (2), codex (1) running. End them, then press Enter to check again, or type stop.', out)
                self.assertEqual(code, 0 if answer == '' else 1)
                self.assertEqual(self.gather.called, answer == '')
                self.connection.python.assert_not_called()  # Only the scripted scanner may use the connection.
                self.assertEqual(set(self.api.calls), {'read'})

    def service_process(self, pid, start, argv):
        path = self.base / 'proc' / str(pid)
        path.mkdir(parents=True)
        (path / 'stat').write_text(f'{pid} (fixture with ) parentheses) S 0 ' + '0 ' * 17 + str(start))
        (path / 'cmdline').write_bytes(argv)
        return path

    def execute_service_program(self, code, payload=b'', timeout=60):
        self.assertGreaterEqual(timeout, 10 * len(json.loads(payload)['services']))
        proc, output = self.base / 'proc', io.StringIO()
        with patch('pathlib.Path', side_effect=lambda value: proc if value == '/proc' else Path(value)), \
                patch('os.kill', side_effect=self.service_signal), \
                patch('time.monotonic', side_effect=lambda: self.clock), \
                patch('time.sleep', side_effect=self.advance), \
                patch('sys.stdin', io.StringIO(payload.decode())), redirect_stdout(output):
            exec(compile(code, '<end-services-fixture>', 'exec'), {})
        return output.getvalue().encode()

    def test_background_services_prompt_end_matches_identity_then_rescans(self):
        services = tuple(('codex app-server', 100001 + i, 11 + i) for i in range(5)) + (
            ('opencode serve', 100006, 17),)
        self.service_process(100001, 11, b'codex\0app-server\0')
        self.service_process(100002, 99, b'codex\0app-server\0')  # Reused PID.
        self.service_process(100003, 13, b'codex\0')  # Now a session.
        self.service_process(100005, 15, b'opencode\0serve\0--service\0')  # Different service.
        self.service_process(100006, 17, b'opencode\0serve\0--service\0')
        unlisted = self.service_process(100007, 18, b'codex\0app-server\0')
        signals = []
        def end(pid, sig):
            signals.append((pid, sig))
            path = self.base / 'proc' / str(pid)
            (path / 'stat').unlink(); (path / 'cmdline').unlink(); path.rmdir()
        self.service_signal = end
        self.connection.python.side_effect = self.execute_service_program
        self.scanner.side_effect = [replace(self.scan, services=services), self.scan, self.scan]
        self.ask.side_effect = ['end']
        code, out = self.run_cutover()
        self.assertEqual(code, 0)
        self.assertIn('On old-space: background services running: codex app-server (5), opencode serve (1). '
                      'Type end to end them, press Enter to check again, or type stop.', out)
        self.assertIn('Ended codex app-server (1), opencode serve (1) on old-space.', out)
        self.assertEqual(signals, [(100001, signal.SIGTERM), (100006, signal.SIGTERM)])
        self.assertTrue(unlisted.exists())
        self.assertEqual(json.loads(self.connection.python.call_args.kwargs['payload'])['services'],
                         [list(row) for row in services])
        self.assertEqual(self.scanner.call_count, 3)
        self.gather.assert_called_once()

    def test_sessions_take_priority_over_services_and_end_never_signals_them(self):
        running = replace(self.scan, agents=(('codex', 1),), services=(('codex app-server', 100001, 1),))
        self.scanner.side_effect = [running, running]
        self.ask.side_effect = ['end', 'stop']
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertEqual(out.count('On old-space: codex (1) running. End them, then press Enter to check again, or type stop.'), 2)
        self.assertNotIn('background services running', out)
        self.connection.python.assert_not_called()
        self.gather.assert_not_called()

    def test_background_services_enter_rechecks_without_signalling(self):
        self.scanner.side_effect = [replace(self.scan, services=(('opencode serve', 100001, 1),)), self.scan, self.scan]
        self.ask.side_effect = ['']
        code, out = self.run_cutover()
        self.assertEqual(code, 0)
        self.assertIn('background services running: opencode serve (1). Type end to end them', out)
        self.assertNotIn('Ended ', out)
        self.connection.python.assert_not_called()
        self.assertEqual(self.scanner.call_count, 3)

    def test_background_services_stop_moves_nothing(self):
        self.scanner.side_effect = [replace(self.scan, services=(('codex app-server', 100001, 1),))]
        self.ask.side_effect = ['stop']
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn('Nothing moved.', out)
        for leaf in (self.connection.python, self.gather, self.stop, self.sealer, self.remote):
            leaf.assert_not_called()

    def test_background_service_end_waits_ten_seconds_without_escalating(self):
        service = ('codex app-server', 100001, 1)
        self.service_process(100001, 1, b'codex\0app-server\0')
        self.service_signal = Mock()
        self.connection.python.side_effect = self.execute_service_program
        self.scanner.side_effect = [replace(self.scan, services=(service,)), replace(self.scan, services=(service,))]
        self.ask.side_effect = ['end', 'stop']
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.service_signal.assert_called_once_with(100001, signal.SIGTERM)
        self.assertEqual(self.clock, 1010.0)
        self.assertEqual(out.count('background services running'), 2)
        self.assertNotIn('Ended ', out)
        self.sealer.assert_not_called()

    def test_service_timeout_reports_verified_survivors_but_skips_reused_pids(self):
        for index, (actual_start, timed_out) in enumerate(((1, True), (99, False))):
            with self.subTest(reused=not timed_out):
                pid = 110001 + index
                service = ('codex app-server', pid, 1)
                self.service_process(pid, actual_start, b'codex\0app-server\0')
                self.service_signal = Mock()
                self.connection.python.side_effect = self.execute_service_program
                running = replace(self.scan, services=(service,))
                self.scanner.side_effect = [running, running]
                self.ask.side_effect = ['end', 'stop']
                code, out = self.run_cutover()
                self.assertEqual(code, 1)
                message = 'Did not end within 10 seconds on old-space: codex app-server (1).'
                self.assertEqual(message in out, timed_out)
                self.assertNotIn('Ended ', out)
                self.assertNotIn('Started again', out)
                if timed_out:
                    self.service_signal.assert_called_once_with(pid, signal.SIGTERM)
                else:
                    self.service_signal.assert_not_called()

    def test_ended_service_restarting_under_a_new_pid_is_named_once(self):
        ended = ('codex app-server', 120001, 1)
        survivor = ('codex app-server', 120002, 2)
        restarted = ('codex app-server', 120003, 3)
        self.service_process(120001, 1, b'codex\0app-server\0')
        self.service_process(120002, 2, b'codex\0app-server\0')
        def end(pid, sig):
            self.assertEqual(sig, signal.SIGTERM)
            if pid == ended[1]:
                path = self.base / 'proc' / str(pid)
                (path / 'stat').unlink()
                (path / 'cmdline').unlink()
                path.rmdir()
        self.service_signal = Mock(side_effect=end)
        self.connection.python.side_effect = self.execute_service_program
        original = replace(self.scan, services=(ended, survivor))
        after = replace(self.scan, services=(survivor, restarted, ('opencode serve', 120004, 4)))
        self.scanner.side_effect = [original, after, after]
        self.ask.side_effect = ['end', '', 'stop']
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        message = ('Started again on old-space: codex app-server (1). '
                   'Something restarts it; close that, then press Enter.')
        self.assertEqual(out.count(message), 1)
        self.assertIn('Ended codex app-server (1) on old-space.', out)
        self.assertIn('Did not end within 10 seconds on old-space: codex app-server (1).', out)
        lines = out.splitlines()
        self.assertTrue(lines[lines.index(message) + 1].startswith('On old-space: background services running:'))
        self.assertEqual(self.service_signal.call_args_list,
                         [unittest.mock.call(120001, signal.SIGTERM), unittest.mock.call(120002, signal.SIGTERM)])
        self.sealer.assert_not_called()

    def test_background_service_end_refuses_an_unlisted_result(self):
        self.scanner.side_effect = [replace(self.scan, services=(('codex app-server', 100001, 1),))]
        self.connection.python.return_value = b'{"ended":[["codex app-server",100002,1]],"did_not_end":[]}'
        self.ask.side_effect = ['end']
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertNotIn('Ended ', out)
        self.gather.assert_not_called()

    def test_a_service_appearing_during_flush_returns_to_the_gate(self):
        services = (('codex app-server', 100001, 1),)
        self.gather.side_effect = [replace(self.evidence, scan=replace(self.scan, services=services)), self.evidence]
        self.scanner.side_effect = [self.scan, replace(self.scan, services=services), self.scan, self.scan]
        self.connection.python.return_value = b'{"ended":[["codex app-server",100001,1]],"did_not_end":[]}'
        self.ask.side_effect = ['end']
        self.assertEqual(self.run_cutover()[0], 0)
        self.assertEqual(self.gather.call_count, 2)
        self.connection.python.assert_called_once()
        self.sealer.assert_called_once()

    def test_service_change_after_stop_refuses_the_move(self):
        self.scanner.side_effect = [self.scan, replace(self.scan, services=(('opencode serve', 100001, 1),))]
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn('old-space changed while its final flush was recorded; nothing moved.', out)
        self.sealer.assert_not_called()
        self.remote.assert_not_called()

    def test_flush_requires_scratch_history_and_agents_safe(self):
        changes = [dict(steady=False), dict(mac_scratch=None), dict(conflicts=('conflict',)),
                   dict(cycle=None), dict(scan=replace(self.scan, sessions_complete=False)),
                   dict(scan=replace(self.scan, mismatched=(('codex', 'session.jsonl'),))),
                   dict(scan=replace(self.scan, agents=(('codex', 1),))),
                   dict(scan=replace(self.scan, agents_complete=False))]
        good = self.evidence
        for change in changes:
            with self.subTest(change=change):
                self.evidence = replace(good, **change)
                code, out = self.run_cutover()
                self.assertEqual(code, 1)
                self.assertIn("old-space's final flush is not complete; nothing moved:", out)
                self.sealer.assert_not_called()
                self.remote.assert_not_called()
        # Git's UNSAFE judgment alone is deliberately outside the flush gate.
        categories = verdict.judge(verdict.resolve(self.project, self.state, 'old-space'), good, now=self.clock)
        self.evidence = good
        with patch.object(verdict, 'judge', return_value=(replace(categories[0], verdict=verdict.UNSAFE), *categories[1:])):
            self.assertEqual(self.run_cutover()[0], 0)

    def test_no_prompt_between_seal_and_retarget(self):
        self.arm()
        def after_seal(*args, **kwargs):
            self.order.append('seal')
            self.ask.side_effect = AssertionError('prompt after final flush')
            return self.sealer._mock_wraps(*args, **kwargs)
        self.sealer.side_effect = after_seal
        self.assertEqual(self.run_cutover()[0], 0)
        self.assertEqual(self.order[-2:], ['seal', 'remote'])

    def test_a_changed_or_incomplete_post_stop_scan_refuses_the_move(self):
        for changed in (replace(self.scan, scratch=(('late.md', 'a' * 64),)),
                        replace(self.scan, sessions=(('claude/late.jsonl', 'b' * 64),)),
                        replace(self.scan, agents=(('codex', 1),)),
                        replace(self.scan, git_complete=False)):
            with self.subTest(changed=changed):
                self.bind('old-space')
                self.runtime.update(running=True, phase='syncing')
                self.scanner.side_effect = [self.scan, changed]
                for leaf in (self.stop, self.sealer, self.remote):
                    leaf.reset_mock()
                code, out = self.run_cutover()
                self.assertEqual(code, 1)
                self.assertIn('old-space changed while its final flush was recorded; nothing moved.', out)
                self.assertTrue(out.endswith('Sync with old-space is paused. Run the replace again, or task work:cs to resume it.\n'))
                self.stop.assert_called_once_with(self.state)
                self.sealer.assert_not_called()
                self.remote.assert_not_called()
                self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'old-space')
                self.assertFalse(read_json(self.receipt_path)['moved'])

    def test_a_paused_flush_reconnects_old_before_retrying(self):
        replacing.write(self.state, dict(self.journal, moving=999.0))
        self.scanner.side_effect = [self.scan, replace(self.scan, agents=(('codex', 1),))]
        self.assertEqual(self.run_cutover()[0], 1)
        self.assertFalse(self.runtime['running'])
        self.order.clear()
        self.scanner.side_effect = self.scan_old
        self.gather.reset_mock()
        self.sealer.reset_mock()
        self.remote.reset_mock()
        self.clock += 1
        self.assertEqual(self.run_cutover()[0], 0)
        self.assertEqual(self.order[:4], ['finish', 'finish', 'remote', 'decide'])
        self.assertEqual(self.remote.call_args_list[0].kwargs,
                         dict(armed=None, enter=False, expected_old='old-space', start=False))
        self.gather.assert_called_once()
        self.sealer.assert_called_once()
        self.assertEqual(seal.read(self.state, 'old-space').sealed_at, self.clock)
        self.assertEqual(replacing.read(self.state)['moving'], 999.0)

    def test_every_failure_after_the_stop_explains_how_to_resume(self):
        for leaf in ('scan', 'seal', 'remote'):
            with self.subTest(leaf=leaf):
                self.bind('old-space')
                (self.state / 'retired/old-space.json').unlink(missing_ok=True)
                self.runtime.update(running=True, phase='syncing')
                self.scanner.side_effect = ([self.scan, ValueError('fixture scan failed')]
                                            if leaf == 'scan' else self.scan_old)
                self.sealer.side_effect = ValueError('fixture seal failed') if leaf == 'seal' else None
                self.remote.side_effect = ValueError('fixture move failed') if leaf == 'remote' else self.move_remote
                code, out = self.run_cutover()
                self.assertEqual(code, 1)
                self.assertIn('Nothing moved.\nSync with old-space is paused. Run the replace again, or task work:cs to resume it.\n', out)
                self.assertFalse(read_json(self.receipt_path)['moved'])

    def test_failed_pause_is_recovered_before_the_final_scan(self):
        def stop(*args):
            result = self.stop_old(*args)
            if self.stop.call_count == 1:
                result['phase'] = 'cleanup_required'
            return result
        self.stop.side_effect = stop
        def scan(*args, **kwargs):
            if self.stop.called:
                self.assertEqual(self.stop.call_count, 2)
            return self.scan_old(*args, **kwargs)
        self.scanner.side_effect = scan
        self.assertEqual(self.run_cutover()[0], 0)
        self.assertEqual(self.stop.call_count, 2)
        self.assertEqual(self.scanner.call_count, 2)

    def test_a_stop_timeout_keeps_the_resume_guidance_and_does_not_move(self):
        message = 'Scratch supervisor is still shutting down; inspect status before retrying.'
        self.stop.side_effect = ValueError(message)
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [message, 'Nothing moved.',
                         'Sync with old-space is paused. Run the replace again, or task work:cs to resume it.'])
        self.stop.assert_called_once_with(self.state)
        self.scanner.assert_called_once()
        self.sealer.assert_not_called()
        self.remote.assert_not_called()
        self.assertIsNone(seal.read(self.state, 'old-space'))
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'old-space')
        self.assertFalse(read_json(self.receipt_path)['moved'])

    def test_a_dead_syncing_worker_is_recovered_before_the_final_scan(self):
        def stop(*args):
            result = self.stop_old(*args)
            if self.stop.call_count == 1:
                result['phase'] = 'syncing'
            return result
        self.stop.side_effect = stop
        code, out = self.run_cutover()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stop.call_count, 2)
        self.assertEqual(self.order, ['decide', 'scan', 'gather', 'stop', 'stop', 'scan', 'remote'])
        self.assertEqual(self.scanner.call_count, 2)
        self.sealer.assert_called_once()
        self.remote.assert_called_once()
        self.assertIsNotNone(seal.read(self.state, 'old-space'))
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'new-space')
        self.assertTrue(read_json(self.receipt_path)['moved'])

    def assert_unclean_stop_refuses(self, calls):
        code, out = self.run_cutover()
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines(), ['Sync with old-space did not stop cleanly; nothing moved.',
                         'Sync with old-space is paused. Run the replace again, or task work:cs to resume it.'])
        self.assertEqual(self.stop.call_count, calls)
        self.scanner.assert_called_once()
        self.sealer.assert_not_called()
        self.remote.assert_not_called()
        self.assertIsNone(seal.read(self.state, 'old-space'))
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'old-space')
        self.assertFalse(read_json(self.receipt_path)['moved'])

    def test_repeated_cleanup_required_does_not_scan_seal_or_move(self):
        self.stop.side_effect = [dict(running=False, phase='cleanup_required'),
                                 dict(running=False, phase='cleanup_required')]
        self.assert_unclean_stop_refuses(2)

    def test_stop_recovery_still_waiting_does_not_scan_seal_or_move(self):
        self.stop.side_effect = [dict(running=False, phase='cleanup_required'),
                                 dict(running=False, phase='waiting')]
        self.assert_unclean_stop_refuses(2)

    def test_a_running_stop_result_does_not_scan_seal_or_move(self):
        self.stop.side_effect = [dict(running=True, phase='stopped')]
        self.assert_unclean_stop_refuses(1)

    def test_real_remote_and_retarget_keep_old_sync_stopped_until_new_is_bound(self):
        self.remote.side_effect = self.actual_remote
        actual_retarget = scratch.retarget
        def retarget(*args):
            self.assertFalse(self.runtime['running'])
            self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'old-space')
            self.order.append('retarget')
            result = actual_retarget(*args)
            self.order.append('bound-new')
            return result
        def start(*args):
            self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'new-space')
            self.order.append('start-new')
            self.runtime.update(running=True, phase='syncing')
        def write(*args, **kwargs):
            self.assertFalse(self.runtime['running'])
            self.assertEqual(self.scanner.call_count, 2)
            self.order.append('seal')
            return self.sealer._mock_wraps(*args, **kwargs)
        self.sealer.side_effect = write
        with patch.object(workspace, '_prerequisites'), \
                patch.object(workspace, '_project', return_value=self.project), \
                patch.object(workspace, '_choose_pairing', return_value=('new-space', '/workspaces/project')), \
                patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection), \
                patch.object(workspace, '_verify_roots', return_value=[]), \
                patch.object(workspace, '_remote_herdr', return_value='herdr fixture'), \
                patch.object(scratch, 'discover_project', return_value=self.project), \
                patch.object(scratch, '_verify_destination'), \
                patch.object(scratch, 'retarget', side_effect=retarget), \
                patch.object(scratch, 'start', side_effect=start) as started, \
                patch.object(workspace, '_ready', return_value={}), \
                patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                patch('devcontainer_setup.project_sync.restore.end_proxies_elsewhere', return_value=[]), \
                patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0):
            self.assertEqual(self.run_cutover()[0], 0)
        self.assertEqual(self.order[:6], ['decide', 'scan', 'gather', 'stop', 'scan', 'seal'])
        self.assertLess(self.order.index('seal'), self.order.index('retarget'))
        self.assertLess(self.order.index('bound-new'), self.order.index('start-new'))
        started.assert_called_once_with(self.root, self.state)

    def test_failed_stop_recovery_keeps_the_resume_guidance_and_does_not_move(self):
        def stop(*args):
            result = self.stop_old(*args)
            if self.stop.call_count == 2:
                raise ValueError('fixture daemon stop failed')
            return dict(result, phase='cleanup_required')
        self.stop.side_effect = stop
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertTrue(out.endswith('Sync with old-space is paused. Run the replace again, or task work:cs to resume it.\n'))
        self.assertEqual(self.scanner.call_count, 1)
        self.sealer.assert_not_called()
        self.remote.assert_not_called()

    def test_changed_arming_after_decide_moves_nothing(self):
        self.arm()
        self.scanner.side_effect = [replace(self.scan, agents=(('claude', 1),)), self.scan, self.scan]
        def answer(_):
            arming.write(self.state, dict(arming.read(self.state), hold_until=2000))
            return ''
        self.ask.side_effect = answer
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn(move.CHANGED, out)
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'old-space')

    def test_a_rebind_during_the_prompt_is_never_overwritten(self):
        for armed in (False, True):
            with self.subTest(armed=armed):
                self.bind('old-space')
                if armed:
                    self.arm()
                self.scanner.side_effect = [replace(self.scan, agents=(('claude', 1),)), self.scan, self.scan]
                self.ask.side_effect = lambda _: self.bind('other-space') or ''
                code, out = self.run_cutover()
                self.assertEqual(code, 1)
                self.assertIn('old-space is no longer the paired Codespace; nothing moved.', out)
                self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'other-space')

    def test_a_stale_or_missing_old_worker_is_reconnected_first(self):
        for runtime in ({}, dict(self.runtime, runtime_id='prior-runtime')):
            with self.subTest(runtime=runtime):
                self.bind('old-space')
                (self.state / 'retired/old-space.json').unlink(missing_ok=True)
                self.remote.reset_mock()
                self.order.clear()
                self.runtime = runtime
                self.assertEqual(self.run_cutover()[0], 0)
                self.assertEqual(self.order[:3], ['finish', 'remote', 'decide'])
                self.assertEqual(self.remote.call_args_list[0].kwargs,
                                 dict(armed=None, enter=False, expected_old='old-space', start=False))

    def test_old_stopping_between_reads_is_not_started(self):
        from devcontainer_setup.project_sync.codespaces import Codespaces
        self.runtime = {}
        self.remote.side_effect = self.actual_remote
        with patch.object(workspace, '_prerequisites'), patch.object(workspace, '_project', return_value=self.project), \
                patch.object(Codespaces, '_api_env', return_value={}), \
                patch.object(Codespaces, '_api', return_value=dict(name='old-space', state='Shutdown', repository=dict(full_name='example/project'))), \
                patch.object(Codespaces, 'start_codespace', side_effect=AssertionError('start POST')) as start:
            code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn('old-space is Shutdown. Start it with task work:cs:restart, then run the replace again.', out)
        start.assert_not_called()
        self.assertEqual(self.api.calls, ['read'])

    def test_the_latch_alone_flushes_again_before_moving(self):
        replacing.write(self.state, dict(self.journal, moving=999.0))
        self.assertEqual(self.run_cutover()[0], 0)
        self.finish.assert_called_once()
        self.assertEqual(self.scanner.call_count, 2)  # Agent scan, then the post-stop fingerprint.
        for leaf in (self.gather, self.sealer, self.factory):
            leaf.assert_called_once()
        self.assertTrue(read_json(self.receipt_path)['moved'])
        self.assertEqual(replacing.read(self.state)['moving'], 999.0)

    def test_a_replayed_move_never_overwrites_a_third_pairing(self):
        actual_binding = self.c._binding
        for when in ('before-replay', 'after-finish-race', 'ordinary-path', 'after-decide'):
            with self.subTest(when=when):
                replacing.write(self.state, dict(self.journal, moving=None if when == 'ordinary-path' else 999.0))
                (self.state / workspace.PENDING).unlink(missing_ok=True)
                if when != 'ordinary-path':
                    self.write(workspace.PENDING, dict(codespace='new-space'))
                self.bind('old-space' if when == 'after-decide' else 'other-space')
                def decide(*args, **kwargs):
                    self.bind('other-space')
                    return None
                self.decide.side_effect = decide
                for leaf in (self.finish, self.decide, self.remote, self.factory):
                    leaf.reset_mock()
                after_finish = 0
                def binding(state):
                    nonlocal after_finish
                    value = actual_binding(state)
                    if self.finish.called:
                        after_finish += 1
                        if when == 'after-finish-race' and after_finish == 2:
                            # Even if another writer restores OLD immediately, the
                            # third binding just observed must end this attempt.
                            self.bind('old-space')
                    return value
                with patch.object(self.c, '_binding', side_effect=binding):
                    code, out = self.run_cutover()
                self.assertEqual(code, 1)
                self.assertIn('old-space is no longer the paired Codespace; nothing moved.', out)
                self.assertEqual(read_json(self.state / 'scratch.json')['codespace'],
                                 'old-space' if when == 'after-finish-race' else 'other-space')
                self.assertFalse(read_json(self.receipt_path)['moved'])
                self.remote.assert_not_called()
                if when != 'after-decide':
                    self.decide.assert_not_called()
                    self.factory.assert_not_called()
                else:
                    self.decide.assert_called_once()
                    self.factory.assert_called_once()
                for leaf in (self.scanner, self.gather, self.sealer):
                    leaf.assert_not_called()

    def test_mixed_case_origin_is_admitted_before_consent(self):
        self.project = replace(self.project, remote='https://github.com/ExAmPlE/PrOjEcT.git')
        replacing.write(self.state, dict(self.journal, repository='EXAMPLE/PROJECT'))
        self.decide.side_effect = ValueError('Move cancelled; nothing moved.')
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn('Move cancelled; nothing moved.', out)
        self.factory.assert_called_once()
        self.decide.assert_called_once()
        self.sealer.assert_not_called()
        self.remote.assert_not_called()

    def test_mixed_case_origin_refuses_a_foreign_journal(self):
        self.project = replace(self.project, remote='https://github.com/ExAmPlE/PrOjEcT.git')
        replacing.write(self.state, dict(self.journal, repository='OTHER/PROJECT'))
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn('This replace record does not match; run the replace again.', out)
        self.assertFalse(self.receipt_path.exists())
        for leaf in (self.factory, self.decide, self.scanner, self.gather, self.sealer, self.remote):
            leaf.assert_not_called()

    def test_a_retired_record_alone_flushes_again_before_moving(self):
        seal.write(self.state, self.store, self.scan, evidence=self.evidence, identity=self.project.identity,
                   repository='example/project', gh_user='octo', remote_root='/workspaces/project',
                   operation=self.operation, cycle_id='final-cycle', snapshot_id=self.snapshot, now=999.0)
        self.sealer.reset_mock()
        self.assertEqual(self.run_cutover()[0], 0)
        self.finish.assert_called_once()
        self.assertEqual(self.scanner.call_count, 2)
        for leaf in (self.gather, self.sealer, self.factory):
            leaf.assert_called_once()
        self.assertTrue(read_json(self.receipt_path)['moved'])
        self.assertEqual(seal.read(self.state, 'old-space').sealed_at, self.clock)

    def test_stopped_old_is_not_started(self):
        for state in ('Shutdown', 'Starting', None):
            with self.subTest(state=state):
                self.api.states = [state]
                code, out = self.run_cutover()
                self.assertEqual(code, 1)
                self.assertIn('Start it with task work:cs:restart, then run the replace again.', out)
                self.assertEqual(set(self.api.calls), {'read'})
                self.remote.assert_not_called()

    def test_a_crash_inside_the_move_finishes_forward(self):
        self.arm()
        seal.write(self.state, self.store, self.scan, evidence=self.evidence, identity=self.project.identity,
                   repository='example/project', gh_user='octo', remote_root='/workspaces/project',
                   operation=self.operation, cycle_id='final-cycle', snapshot_id=self.snapshot, now=self.clock)
        self.sealer.reset_mock()
        self.bind('new-space')
        self.assertEqual(self.run_cutover()[0], 0)
        self.finish.assert_called_once()
        self.remote.assert_called_once()
        self.assertIs(self.remote.call_args.kwargs['armed'], self.recovery)
        self.assertIs(self.remote.call_args.kwargs['enter'], False)
        for leaf in (self.scanner, self.gather, self.sealer, self.factory):
            leaf.assert_not_called()
        self.assertTrue(read_json(self.receipt_path)['moved'])

    def test_a_finished_move_never_prints_the_paused_line(self):
        code, out = self.run_cutover()
        self.assertEqual(code, 0, out)
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'new-space')
        self.assertTrue(read_json(self.receipt_path)['moved'])
        self.assertNotIn('Sync with old-space is paused.', out)

    def test_receipt_names_the_seal_and_herdr_result(self):
        for failure in (7, ValueError('Herdr workspace failed')):
            with self.subTest(failure=failure):
                self.bind('old-space')
                self.runtime.update(running=True, phase='syncing')
                (self.state / 'retired/old-space.json').unlink(missing_ok=True)
                def remote(*a, **k):
                    self.bind('new-space')
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                self.remote.side_effect = remote
                code, out = self.run_cutover()
                self.assertEqual(code, 0)
                self.assertIn('Moved sync to new-space; Herdr could not connect it. The replace checks it again.', out)
                self.assertEqual(read_json(self.receipt_path), dict(schema=1, operation=self.operation, step='cutover',
                                 old='old-space', new='new-space', moved=True, sealed_at=self.clock,
                                 cycle_id='final-cycle', snapshot_id=self.snapshot,
                                 herdr=1 if isinstance(failure, Exception) else failure, at=self.clock))
                self.assertEqual(self.receipt_path.stat().st_mode & 0o777, 0o600)

    def test_mismatched_journal_refuses(self):
        for change in (dict(operation='abcdef012345'), dict(old='other-space'), dict(new='other-space'),
                       dict(phase='created', at={'created': 1}), dict(ended='completed', ended_at=1)):
            with self.subTest(change=change):
                replacing.write(self.state, dict(self.journal, **change))
                code, out = self.run_cutover()
                self.assertEqual(code, 1)
                self.assertIn('This replace record does not match; run the replace again.', out)
                self.assertFalse(self.receipt_path.exists())
                self.factory.assert_not_called()

    def test_host_refusals_are_observational(self):
        for env in ({'CODESPACES': 'true'}, {'GH_TOKEN': 'fixture'}, {'GITHUB_TOKEN': 'fixture'}):
            with self.subTest(keys=list(env)), patch.dict(os.environ, env):
                self.assertEqual(self.run_cutover()[0], 1)
                self.assertFalse(self.receipt_path.exists())
                self.factory.assert_not_called()
        with patch.object(self.c.sys.stdin, 'isatty', return_value=False):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(self.c.run(self.project, self.state, operation=self.operation, old='old-space',
                                 new='new-space', receipt=self.receipt_path, recovery=self.recovery), 1)

    def test_worker_timeout_and_unreadable_agents_write_refusal_receipts(self):
        self.runtime = {}
        self.remote.side_effect = lambda *a, **k: 0
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn('Replace stopped at flush and move: sync with old-space did not start.', out)
        self.assertEqual(self.clock, 1120)
        self.assertFalse(read_json(self.receipt_path)['moved'])
        self.runtime = dict(running=True, phase='syncing', checkpoint_version=sync_cycle.SCHEMA,
                            runtime_id=scratch._runtime_id(), heartbeat_at=time.time())
        self.scanner.side_effect = [replace(self.scan, agents_complete=False)]
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn('Unknown: could not read processes on old-space', out)
        self.gather.assert_not_called()

    def test_unreadable_seal_still_writes_a_refusal_receipt(self):
        self.write('retired/old-space.json', dict(schema=2))
        code, out = self.run_cutover()
        self.assertEqual(code, 1)
        self.assertIn('cannot be read', out)
        value = read_json(self.receipt_path)
        self.assertFalse(value['moved'])
        self.assertIsNone(value['sealed_at'])
        self.remote.assert_not_called()

    def test_old_reconnect_is_not_new_herdr_evidence(self):
        self.runtime = {}
        self.decide.side_effect = ValueError('Move cancelled; nothing moved.')
        self.assertEqual(self.run_cutover()[0], 1)
        self.assertIsNone(read_json(self.receipt_path)['herdr'])

    def test_arming_an_unarmed_old_during_the_wait_refuses(self):
        for during in ('prompt', 'gather'):
            with self.subTest(during=during):
                self.bind('old-space')
                self.runtime.update(running=True, phase='syncing')
                arming.write(self.state, dict(schema=1, armed=False))
                self.decide.side_effect = self.decide_move
                self.scanner.side_effect = ([replace(self.scan, agents=(('claude', 1),)), self.scan, self.scan]
                                            if during == 'prompt' else [self.scan, self.scan])
                def answer(_):
                    self.arm()
                    return ''
                self.ask.side_effect = answer
                def gather(*args, **kwargs):
                    if during == 'gather':
                        self.arm()
                    return self.gather_old(*args, **kwargs)
                self.gather.side_effect = gather
                self.remote.side_effect = self.actual_remote
                def retarget(*args):
                    self.bind(args[2])
                with patch.object(workspace, '_prerequisites'), \
                        patch.object(workspace, '_project', return_value=self.project), \
                        patch.object(workspace, '_choose_pairing', return_value=('new-space', '/workspaces/project')) as choose, \
                        patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=self.connection), \
                        patch.object(workspace, '_verify_roots', return_value=[]), \
                        patch.object(workspace, '_remote_herdr', return_value='herdr fixture'), \
                        patch.object(scratch, 'retarget', side_effect=retarget) as retargeted, \
                        patch.object(scratch, 'start'), patch.object(workspace, '_ready', return_value={}), \
                        patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                        patch('devcontainer_setup.project_sync.restore.end_proxies_elsewhere', return_value=[]), \
                        patch('devcontainer_setup.project_sync.herdr.open_remote', return_value=0):
                    code, out = self.run_cutover()
                    self.assertEqual(code, 1, out)
                    self.assertIn(move.CHANGED, out)
                    retargeted.assert_not_called()
                    self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'old-space')
                    self.assertEqual(arming.read(self.state)['codespace'], 'old-space')
                    # An armed same-target reconnect requires no move decision.
                    choose.return_value = ('old-space', '/workspaces/project')
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(self.actual_remote(self.project, self.state, rebind='old-space', enter=False), 0)

    def test_consent_after_old_reconnect_resumes_new_recovery(self):
        self.runtime = {}
        def remote(*args, **kwargs):
            if kwargs.get('rebind') is None:
                self.arm()
            return self.move_remote(*args, **kwargs)
        self.remote.side_effect = remote
        self.assertEqual(self.run_cutover()[0], 0)
        self.assertIsNotNone(self.remote.call_args.kwargs['move'])
        self.assertIs(self.remote.call_args.kwargs['armed'], self.recovery)

    def test_finish_forward_consent_resumes_new_recovery(self):
        replacing.write(self.state, dict(self.journal, moving=999.0))
        self.decide.side_effect = lambda *a, **k: self.arm()
        self.assertEqual(self.run_cutover()[0], 0)
        self.assertIsNotNone(self.remote.call_args.kwargs['move'])
        self.assertIs(self.remote.call_args.kwargs['armed'], self.recovery)
