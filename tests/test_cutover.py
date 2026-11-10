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
        self.decide = self.stack.enter_context(patch.object(move, 'decide', side_effect=self.decide_move))
        self.finish = self.stack.enter_context(patch.object(move, 'finish_pending', side_effect=lambda *_: self.order.append('finish')))
        self.scanner = self.stack.enter_context(patch.object(target_scan, 'scan', side_effect=self.scan_old))
        self.gather = self.stack.enter_context(patch.object(self.c.deletion, 'gather', side_effect=self.gather_old))
        self.sealer = self.stack.enter_context(patch.object(seal, 'write', wraps=seal.write))
        self.actual_remote = workspace.remote
        self.remote = self.stack.enter_context(patch.object(workspace, 'remote', side_effect=self.move_remote))
        self.ask = Mock(side_effect=lambda _: self.order.append('ask') or '')

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

    def test_consent_is_decided_before_the_flush(self):
        decision = self.arm()
        original = self.sealer._mock_wraps
        def write(*args, **kwargs):
            self.order.append('seal')
            return original(*args, **kwargs)
        self.sealer.side_effect = write
        self.assertEqual(self.run_cutover()[0], 0)
        self.assertEqual(self.order, ['decide', 'scan', 'gather', 'seal', 'remote'])
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

    def test_agents_are_asked_never_killed(self):
        running = replace(self.scan, agents=(('claude', 2), ('codex', 1), ('herdr', 1)))
        for answer in ('', 'stop', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=type(answer).__name__ if isinstance(answer, BaseException) else answer):
                self.bind('old-space')
                self.scanner.side_effect = [running, replace(self.scan, agents=(('herdr', 1),))]
                self.ask.side_effect = answer if isinstance(answer, BaseException) else lambda _: answer
                self.gather.reset_mock()
                code, out = self.run_cutover()
                self.assertIn('On old-space: claude (2), codex (1) running. End them, then press Enter to check again, or type stop.', out)
                self.assertEqual(code, 0 if answer == '' else 1)
                self.assertEqual(self.gather.called, answer == '')
                self.connection.python.assert_not_called()  # Only the scripted scanner may use the connection.
                self.assertEqual(set(self.api.calls), {'read'})

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

    def test_changed_arming_after_decide_moves_nothing(self):
        self.arm()
        self.scanner.side_effect = [replace(self.scan, agents=(('claude', 1),)), self.scan]
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
                self.scanner.side_effect = [replace(self.scan, agents=(('claude', 1),)), self.scan]
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
        for leaf in (self.scanner, self.gather, self.sealer, self.factory):
            leaf.assert_called_once()
        self.assertTrue(read_json(self.receipt_path)['moved'])
        self.assertEqual(replacing.read(self.state)['moving'], 999.0)

    def test_a_retired_record_alone_flushes_again_before_moving(self):
        seal.write(self.state, self.store, self.scan, evidence=self.evidence, identity=self.project.identity,
                   repository='example/project', gh_user='octo', remote_root='/workspaces/project',
                   operation=self.operation, cycle_id='final-cycle', snapshot_id=self.snapshot, now=999.0)
        self.sealer.reset_mock()
        self.assertEqual(self.run_cutover()[0], 0)
        self.finish.assert_called_once()
        for leaf in (self.scanner, self.gather, self.sealer, self.factory):
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
        for name, value in (('scratch.json', {'codespace': 'new-space'}),
                            ('scratch-retarget.json', {'candidate': {'codespace': 'new-space'}}),
                            ('recovery-move.json', {'new': 'new-space'}),
                            ('workspace-pending.json', {'codespace': 'new-space'})):
            with self.subTest(name=name):
                self.bind('old-space')
                seal.write(self.state, self.store, self.scan, evidence=self.evidence, identity=self.project.identity,
                           repository='example/project', gh_user='octo', remote_root='/workspaces/project',
                           operation=self.operation, cycle_id='final-cycle', snapshot_id=self.snapshot, now=self.clock)
                self.sealer.reset_mock()
                if name == 'scratch.json':
                    self.bind('new-space')
                else:
                    self.write(name, value)
                self.remote.reset_mock()
                self.finish.reset_mock()
                self.assertEqual(self.run_cutover()[0], 0)
                self.finish.assert_called_once()
                self.remote.assert_called_once()
                self.assertIs(self.remote.call_args.kwargs['armed'], self.recovery)
                self.assertIs(self.remote.call_args.kwargs['enter'], False)
                for leaf in (self.scanner, self.gather, self.sealer, self.factory):
                    leaf.assert_not_called()
                self.assertTrue(read_json(self.receipt_path)['moved'])
                if name != 'scratch.json':
                    (self.state / name).unlink()

    def test_receipt_names_the_seal_and_herdr_result(self):
        for failure in (7, ValueError('Herdr workspace failed')):
            with self.subTest(failure=failure):
                self.bind('old-space')
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
                arming.write(self.state, dict(schema=1, armed=False))
                self.decide.side_effect = self.decide_move
                self.scanner.side_effect = ([replace(self.scan, agents=(('claude', 1),)), self.scan]
                                            if during == 'prompt' else [self.scan])
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
