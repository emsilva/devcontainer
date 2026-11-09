"""Checked deletion with local evidence, scripted APIs and counted mutation leaves."""
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from devcontainer_setup.project_sync import arming, lifecycle, pushed, seal, standing, sync_cycle, target_scan
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.state import Project, atomic_write, locked, read_json


IDENTITY = 'github.com/example/project'
PID = hashlib.sha256(IDENTITY.encode()).hexdigest()[:24]
REMOTE = '/workspaces/project'


class FakeAPI:
    def __init__(self, states=('Available', 'Available', 'Shutdown'), *, status=204, gone=True):
        self.states, self.status, self.gone = list(states), status, gone
        self.calls = []
        self.repository = 'example/project'

    def read(self):
        self.calls.append('read')
        row = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if callable(row):
            return row()
        if isinstance(row, lifecycle.Reading):
            return row
        return lifecycle.Reading(1000, row is not None, row, False, None, 200,
                                 self.repository if row != 'Deleted' else None)

    def post_stop(self):
        self.calls.append('post_stop')
        return 202

    def post_start(self):
        self.calls.append('post_start')
        return 202

    def delete(self):
        self.calls.append('delete')
        return self.status

    def confirm_deleted(self):
        self.calls.append('confirm_deleted')
        return self.gone


class DeletionTests(unittest.TestCase):
    def setUp(self):
        module = 'devcontainer_setup.project_sync.deletion'
        self.assertIsNotNone(importlib.util.find_spec(module), 'checked-delete module is missing')
        self.d = importlib.import_module(module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root, self.state = self.base / 'project', self.base / 'state'
        (self.root / '.scratch').mkdir(parents=True)
        self.project = Project(self.root, 'https://' + IDENTITY, IDENTITY, PID)
        self.local = standing.Local(self.root, repository='example/project')
        self.clock = 1000.0
        self.worker_running = True
        self.bind()
        self.api = FakeAPI()
        self.scan = target_scan.Scan('space', self.clock, (), True, (), True, (), True,
                                    None, (), (), (), (), True, ())
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mocks = {}
        for name, value in [('standing.read', self.local), ('arming.account', 'octo'),
                            ('target_scan.scan', self.scan), ('pushed.prove', ()),
                            ('scratch.status', {'running': True}),
                            ('scratch.inspect_sync', {'phase': 'synchronized', 'conflicts': []}),
                            ('keeper.stop', {'running': False}), ('scratch.stop', {'running': False}),
                            ('keeper.status', {'running': False}),
                            ('restore.end_target_proxies', 'absent'), ('ssh_hosts.remove', 'removed'),
                            ('herdr.remove_profile', 'removed'), ('scratch.terminate_retired', 'terminated')]:
            self.mocks[name] = self.stack.enter_context(patch('devcontainer_setup.project_sync.' + name,
                                                             return_value=value))
        self.mocks['scratch.stop'].side_effect = self.stop_worker
        self.connect = Mock()
        self.connect.return_value.__enter__ = Mock(return_value=object())
        self.connect.return_value.__exit__ = Mock(return_value=False)
        self.factory = Mock(return_value=self.api)
        self.install_capture()
        self.mocks['scratch.status'].side_effect = self.worker_runtime
        self.extra_mutations = {}
        for module, name in ((self.d.power, 'stop'), (arming, 'record_owner_stop'),
                             (self.d.workspace, 'retire'), (arming, 'write')):
            self.extra_mutations[name] = self.stack.enter_context(patch.object(module, name, wraps=getattr(module, name)))
        from devcontainer_setup.project_sync import conversations, replication
        for module, name in ((conversations, 'capture_conversations'), (conversations, 'publish_conversations'),
                             (replication, 'capture'), (Store, 'acknowledge'), (replication.RemoteStore, 'acknowledge')):
            self.stack.enter_context(patch.object(module, name,
                side_effect=AssertionError('deletion must never capture, acknowledge or publish conversations')))

    def stop_worker(self, *_):
        self.worker_running = False
        return {'running': False}

    def worker_runtime(self, *_):
        cycle, _ = self.current_cycle()
        runtime = dict(running=self.worker_running, phase='syncing', checkpoint_version=sync_cycle.SCHEMA,
                       runtime_id=self.d.scratch._runtime_id(), heartbeat_at=time.time(), conversations=cycle)
        self.write(self.state / self.d.scratch.RUNTIME, runtime)
        return read_json(self.state / self.d.scratch.RUNTIME)

    def bind(self, name='space'):
        self.write(self.state / 'scratch.json', dict(version=1, identity=IDENTITY, root=str(self.root),
                                                   codespace=name, remote_root=REMOTE))
        self.write(self.state / 'workspace.json', dict(schema=1, project_id=PID, identity=IDENTITY,
                   local_root=str(self.root), local_active_root=str(self.root), active_host='codespace',
                   codespace=name, remote_root=REMOTE))

    def write(self, path, value):
        atomic_write(path, json.dumps(value).encode())

    def install_capture(self):
        store = Store(self.state)
        manifest = dict(schema=2, project=dict(identity=IDENTITY, project_id=PID, root=REMOTE),
                        conversations=dict(schema=1, entries=[], issues=[]),
                        producer=dict(format='devcontainer-project-conversations-v2'))
        snapshot = store.save(manifest)
        self.receipt = store.acknowledge(snapshot, source='codespace')

    def current_cycle(self, *args):
        inbound = sync_cycle.direction()
        inbound.update(outcome='complete', snapshot_id=self.receipt['snapshot_id'], receipt=self.receipt)
        return dict(schema=3, cycle_id='cycle', outcome='complete', started_at=self.clock,
                    completed_at=self.clock, stage='complete', issues=[], identity=IDENTITY, project_id=PID,
                    local_root=str(self.root), remote_root=REMOTE,
                    directions=dict(codespace_to_desktop=inbound)), True

    def advance(self, seconds):
        self.clock += seconds

    def run_delete(self, *, check=False, as_json=False, start=False, name=None, tty=True, answer='delete', env=None):
        for leaf in self.extra_mutations.values():
            leaf.reset_mock()
        ask = Mock(side_effect=answer if isinstance(answer, BaseException) or callable(answer) else lambda _: answer)
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.d.run(self.project, self.state, codespace=name, check=check, as_json=as_json, start=start,
                              env={} if env is None else env, ask=ask, tty=lambda: tty,
                              api_factory=self.factory, connect=self.connect,
                              now=lambda: self.clock, sleep=self.advance)
        return code, output.getvalue(), ask

    def mutations(self):
        return [call for call in self.api.calls if call in ('post_stop', 'post_start', 'delete')] + [
            name for name in ('keeper.stop', 'scratch.stop', 'restore.end_target_proxies', 'ssh_hosts.remove',
                             'herdr.remove_profile', 'scratch.terminate_retired') if self.mocks[name].called] + [
            name for name, leaf in self.extra_mutations.items() if leaf.called]

    def journal(self, phase='deleted', kind='paired', cleanup=None, **updates):
        value = dict(schema=1, codespace='space', kind=kind, account='octo', repository='example/project',
                     fingerprint=target_scan.fingerprint(self.scan), phase=phase, at={phase: self.clock},
                     archive=None, cleanup={} if cleanup is None else cleanup)
        value.update(updates)
        self.write(self.state / 'deletions/space.json', value)
        return value

    def retired(self, name='space'):
        self.bind('new-space')
        record = seal.Seal(name, IDENTITY, 'example/project', 'octo', REMOTE, 'replace-1', self.clock,
                           'cycle', self.receipt['snapshot_id'], (), ())
        self.write(self.state / 'retired' / (name + '.json'), dict(schema=1, **record.__dict__))
        archive = self.state / 'retired-bindings/100-abcdef12'
        self.write(archive / 'scratch.json', dict(identity=IDENTITY, codespace=name, remote_root=REMOTE))
        self.write(archive / 'scratch-session.json', {'name': 'archived-session'})
        return archive

    def test_check_json_makes_no_destructive_call(self):
        code, output, ask = self.run_delete(check=True, as_json=True, tty=False)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)['deletable'])
        ask.assert_not_called()
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.state / 'deletions').exists())

    def test_check_json_prints_one_object_for_every_outcome(self):
        for states, outcome in [(('Available',), 'checked'), (('Shutdown',), 'unreachable'),
                                (('Queued',), 'unreachable'), ((None,), 'unreachable'),
                                (('Deleted',), 'cleanup-pending')]:
            with self.subTest(states=states):
                self.api.states = list(states)
                code, output, _ = self.run_delete(check=True, as_json=True)
                self.assertEqual(json.loads(output)['outcome'], outcome)
                self.assertEqual(code, 0 if outcome == 'checked' else 1)
        self.api.repository = 'example/elsewhere'
        self.api.states = ['Available']
        self.assertEqual(json.loads(self.run_delete(check=True, as_json=True)[1])['outcome'], 'refused')
        self.api.repository = 'example/project'
        arming.arm(self.state, gh_user='octo', codespace='space', consent='x', billable_owner='octo', ports=[], now=1)
        self.mocks['arming.account'].return_value = 'other'
        self.assertEqual(json.loads(self.run_delete(check=True, as_json=True)[1])['outcome'], 'refused')
        self.mocks['arming.account'].return_value = 'octo'
        self.journal('cleaned')
        self.assertEqual(json.loads(self.run_delete(check=True, as_json=True)[1])['outcome'], 'cleaned')
        (self.state / 'deletions/space.json').unlink()
        (self.state / 'scratch.json').unlink()
        self.api.states = ['Deleted']
        self.assertEqual(json.loads(self.run_delete(name='space', check=True, as_json=True)[1])['outcome'],
                         'already-deleted')
        self.api.states = ['Available']
        self.assertEqual(json.loads(self.run_delete(name='space', check=True, as_json=True)[1])['outcome'],
                         'unreachable')
        self.assertEqual(self.mutations(), [])

    def test_check_never_cleans_up_at_any_phase(self):
        with patch.object(self.d.power, 'stop') as stop, patch.object(arming, 'record_owner_stop') as owner, \
                patch.object(self.d.workspace, 'retire') as retire, patch.object(arming, 'write') as write:
            for phase in self.d.PHASES:
                for check, tty in ((True, True), (False, False)):
                    with self.subTest(phase=phase, check=check):
                        self.journal(phase)
                        self.api.states = ['Deleted']
                        before = (self.state / 'deletions/space.json').read_bytes()
                        self.run_delete(check=check, tty=tty)
                        self.assertEqual(self.mutations(), [])
                        self.assertEqual((self.state / 'deletions/space.json').read_bytes(), before)
            for leaf in (stop, owner, retire, write):
                leaf.assert_not_called()
        (self.state / 'deletions/space.json').unlink()
        code, _, _ = self.run_delete(answer=EOFError())
        self.assertEqual(code, 1)
        self.assertEqual(self.mutations(), [])
        self.journal('confirmed')
        code, _, ask = self.run_delete(answer=EOFError())
        self.assertEqual(code, 0)
        ask.assert_not_called()

    def test_unsafe_never_asks_and_never_deletes(self):
        tree = target_scan.Worktree(REMOTE + '-other', 'a' * 40, 'main', 1, ('work.txt',), True)
        repo = target_scan.Repository(REMOTE, IDENTITY, (), 0, (tree,), False, True)
        for changed in (repo, replace(repo, stashes=1, worktrees=())):
            self.mocks['target_scan.scan'].return_value = replace(self.scan, repositories=(changed,))
            self.mocks['pushed.prove'].return_value = (pushed.Proof(REMOTE, 0, (), (), False),)
            code, _, ask = self.run_delete()
            self.assertEqual(code, 1)
            ask.assert_not_called()
            self.assertEqual(self.mutations(), [])

    def test_only_the_exact_word_proceeds(self):
        for answer in ('Delete', 'yes', 'delete it', '', EOFError()):
            self.api.states = ['Available']
            code, output, _ = self.run_delete(answer=answer)
            self.assertEqual(code, 1)
            self.assertIn('Nothing was deleted.', output)
            self.assertEqual(self.mutations(), [])
        self.api.states = ['Available', 'Available', 'Shutdown']
        code, output, ask = self.run_delete(tty=False)
        self.assertEqual(code, 1)
        self.assertIn('deleting needs a terminal', output)
        ask.assert_not_called()
        self.assertEqual(self.mutations(), [])
        self.api.states = ['Available', 'Available', 'Shutdown']
        code, output, _ = self.run_delete(answer=' delete ')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.api.calls.count('delete'), 1)

    def test_a_turn_written_just_before_the_check_needs_a_later_cycle(self):
        data = b'completed conversation\n'
        store = Store(self.state)
        digest = store.put(data)
        name, session = 'rollout-session.jsonl', 'session'
        manifest = dict(schema=2, project=dict(identity=IDENTITY, project_id=PID, root=REMOTE),
                        conversations=dict(schema=1, entries=[dict(agent='codex', session_id=session,
                            files=[dict(path=name, session_id=session, size=len(data), sha256=digest, blob=digest)])],
                                           issues=[]), producer=dict(format='devcontainer-project-conversations-v2'))
        snapshot = store.save(manifest)
        old_receipt = dict(self.receipt)
        cutoff = target_scan.Cutoff(len(data), digest, True)
        # The new session is discovered during scan one, after the earlier empty capture.
        self.scan = replace(self.scan, sessions=(('codex/sessions/' + name, digest),), checked=(('codex', name),),
                            cutoffs_sha256=target_scan.cutoffs_digest({('codex', name): cutoff}))
        seen = []
        def scope(*args):
            seen.append(self.clock)
            if len(seen) > 1:
                self.receipt = dict(old_receipt, snapshot_id=snapshot)
                self.write(self.state / 'checkpoints/receipts/codespace.json', self.receipt)
            cycle, current = self.current_cycle()
            cycle['started_at'] = 999 if len(seen) == 1 else self.clock
            return cycle, current
        def first_scan(*args, **kwargs):
            self.clock += 3
            return self.scan
        self.mocks['target_scan.scan'].side_effect = first_scan
        with patch.object(self.d.workspace, 'cycle_scope', side_effect=scope):
            evidence = self.d.gather(self.project, self.state, self.d.verdict.resolve(self.project, self.state, None),
                                     object(), now=lambda: self.clock, sleep=self.advance)
        self.assertGreaterEqual(len(seen), 2)
        self.assertGreater(evidence.cycle['started_at'], 1003)
        self.assertEqual(self.mocks['target_scan.scan'].call_args.kwargs['cutoffs'], {('codex', name): cutoff})
        self.assertEqual(evidence.sessions_by_file, (('codex', name, session),))
        self.assertTrue(all(row.verdict == 'safe' for row in self.d.verdict.judge(
            self.d.verdict.resolve(self.project, self.state, None), evidence, now=self.clock)))
        # Keeping only the older capture never makes that same stable session SAFE.
        self.receipt = old_receipt
        self.write(self.state / 'checkpoints/receipts/codespace.json', old_receipt)
        def old_cycle(*args):
            cycle, current = self.current_cycle()
            return dict(cycle, started_at=999), current
        with patch.object(self.d.workspace, 'cycle_scope', side_effect=old_cycle):
            stale = self.d.gather(self.project, self.state, self.d.verdict.resolve(self.project, self.state, None),
                                  object(), now=lambda: self.clock, sleep=lambda _: self.advance(360))
        history = self.d.verdict.judge(self.d.verdict.resolve(self.project, self.state, None), stale, now=self.clock)[2]
        self.assertEqual(history.verdict, 'unknown')
        self.mocks['target_scan.scan'].side_effect = [self.scan, replace(self.scan, agents=(('codex', 1),))]
        code, output, _ = self.run_delete(check=True, as_json=True)
        self.assertEqual(code, 1)
        self.assertTrue(all(row['verdict'] == 'unknown' for row in json.loads(output)['categories']))

    def test_a_change_during_the_prompt_voids_the_verdict(self):
        tree = target_scan.Worktree(REMOTE, 'a' * 40, 'main', 0, (), True)
        repo = target_scan.Repository(REMOTE, IDENTITY, (('main', 'a' * 40),), 0, (tree,), False, True)
        cases = ('files', 'proof', 'receipt', 'account', 'pairing', 'state', 'repository', 'connection')
        for case in cases:
            with self.subTest(case=case):
                self.bind()
                self.api.states, self.api.repository = ['Available', 'Available', 'Shutdown'], 'example/project'
                self.mocks['target_scan.scan'].side_effect = None
                self.mocks['target_scan.scan'].return_value = replace(self.scan, repositories=(repo,))
                self.mocks['pushed.prove'].return_value = (pushed.Proof(REMOTE, 1, (), (), False),)
                self.mocks['arming.account'].side_effect = None
                def change(_):
                    if case == 'files':
                        self.mocks['target_scan.scan'].return_value = replace(self.scan, scratch=(('late', 'b'*64),))
                    elif case == 'proof':
                        self.mocks['pushed.prove'].return_value = (pushed.Proof(REMOTE, 0,
                                                                       (('main', 'a' * 40),), (), False),)
                    elif case == 'receipt':
                        self.d.workspace._conversation_receipt = Mock(return_value=(None, {}))
                    elif case == 'account':
                        self.mocks['arming.account'].return_value = 'other'
                    elif case == 'pairing':
                        self.bind('new-space')
                    elif case == 'repository':
                        self.api.repository = 'example/other'
                    elif case == 'connection':
                        self.mocks['target_scan.scan'].return_value = replace(self.scan, agents_complete=False)
                    else:
                        self.api.states = ['Shutdown']
                    return 'delete'
                with patch.object(self.d.workspace, '_conversation_receipt', wraps=self.d.workspace._conversation_receipt):
                    code, output, _ = self.run_delete(answer=change)
                self.assertEqual(code, 1, output)
                self.assertIn('changed since the check', output)
                self.assertEqual(self.mutations(), [])
                self.mocks['arming.account'].return_value = 'octo'

    def test_paired_stop_is_recorded_before_the_post(self):
        observed = []
        original = self.api.post_stop
        def post():
            observed.append(read_json(self.state / 'arming.json')['owner_stop_codespace'])
            self.assertEqual(read_json(self.state / 'deletions/space.json')['phase'], 'stop-recorded')
            return original()
        self.api.post_stop = post
        # Neither a pending operation nor unknown/wrong repository evidence permits DELETE.
        for last in (lifecycle.Reading(1000, True, 'Shutdown', True, None, 200, 'example/project'),
                     lifecycle.Reading(1000, True, 'Shutdown', False, None, 200, 'example/other'),
                     lifecycle.Reading(1000, False, None, None, None), 'ShuttingDown'):
            self.api.states, self.api.calls = ['Available', 'Available', last], []
            code, output, _ = self.run_delete()
            self.assertEqual(code, 1)
            self.assertIn('has not reached Shutdown', output)
            self.assertNotIn('delete', self.api.calls)
            self.mocks['keeper.stop'].assert_not_called()
        self.api.states, self.api.calls = ['Available', 'Available', 'Shutdown'], []
        code, output, _ = self.run_delete()
        self.assertEqual(code, 0, output)
        self.assertEqual(observed, ['space'] * 5)
        self.assertLess(self.api.calls.index('post_stop'), self.api.calls.index('delete'))

    def test_retired_delete_keeps_new_recovery_working(self):
        self.retired()
        arming.arm(self.state, gh_user='octo', codespace='new-space', consent='x', billable_owner='octo',
                   ports=[], now=999990)
        for name in ('herdr-ssh.json', 'herdr-machine.json'):
            self.write(self.state / name, {'codespace': 'new-space'})
        paths = [self.state / name for name in ('scratch.json', 'workspace.json', 'arming.json',
                                                'herdr-ssh.json', 'herdr-machine.json')]
        before = [path.read_bytes() for path in paths]
        code, output, _ = self.run_delete(name='space')
        self.assertEqual(code, 0, output)
        self.assertEqual([path.read_bytes() for path in paths], before)
        from tests.test_keeper import FakeAPI as KeeperAPI, FakeHerdr, FakeKeepAlive, T0, up, down
        from devcontainer_setup.project_sync import keeper
        anchor = T0 - 3600
        stops = [anchor + lifecycle.LIFETIME + 30 + 60 * i for i in range(3)]
        readings = [up(T0, last_used_at=anchor), down(stops[0], 'ShuttingDown')]
        readings += [down(at) for at in stops[1:]] + [down(T0 + lifecycle.LIFETIME),
                      up(T0 + lifecycle.LIFETIME + 60, last_used_at=T0 + lifecycle.LIFETIME)]
        api = KeeperAPI(readings, name='new-space')
        k = keeper.Keeper(self.state, api=api, notifier=lambda *a: True, now=lambda: self.clock,
                          sleep=self.advance, keepalive=FakeKeepAlive(), herdr=FakeHerdr(), end_proxies=lambda *a: [])
        for at in [T0] + stops:
            self.clock = at
            k.step()
        self.assertEqual(api.posts, 1)
        self.assertEqual(read_json(self.state / keeper.RUNTIME)['last_restart']['resume'], 'delivered')

    def test_404_alone_is_not_deleted(self):
        self.api.status, self.api.gone = 404, None
        code, output, _ = self.run_delete()
        self.assertEqual(code, 1)
        self.assertIn('does not show space as gone yet', output)
        self.mocks['keeper.stop'].assert_not_called()
        self.assertEqual(read_json(self.state / 'deletions/space.json')['phase'], 'delete-requested')

    def test_lost_delete_reply_is_confirmed_by_inventory(self):
        self.api.status = 0
        with patch.object(self.api, 'confirm_deleted', side_effect=ValueError('fixture auth unavailable')):
            code, output, _ = self.run_delete()
        self.assertEqual(code, 1)
        self.assertIn('does not show space as gone yet', output)
        self.assertNotIn('nothing was deleted', output.lower())
        self.api.states = ['Available', 'Available', 'Shutdown']
        original = self.d.atomic_write
        def fail_deleted(path, data):
            if path.name == 'space.json' and json.loads(data)['phase'] == 'deleted':
                raise OSError('fixture journal failure')
            return original(path, data)
        with patch.object(self.d, 'atomic_write', side_effect=fail_deleted):
            code, output, _ = self.run_delete()
        self.assertEqual(code, 1)
        self.assertIn('cleanup is not finished', output)
        self.assertNotIn('nothing was deleted', output.lower())
        self.assertEqual(read_json(self.state / 'deletions/space.json')['phase'], 'delete-requested')
        self.api.states = ['Deleted']
        code, output, _ = self.run_delete()
        self.assertEqual(code, 0, output)
        self.assertIn('Deleted space.', output)

    def test_each_phase_resumes_safely(self):
        for phase in ('stop-requested', 'delete-requested'):
            self.journal(phase)
            self.api.states = ['Shutdown']
            code, output, _ = self.run_delete()
            self.assertEqual(code, 1)
            self.assertIn('restart it:', output)
            self.assertEqual(self.mutations(), [])
        self.journal('delete-requested')
        self.api.states = ['Available']
        code, output, ask = self.run_delete(answer=EOFError())
        self.assertEqual(code, 1, output)
        ask.assert_called_once()
        self.assertGreaterEqual(self.mocks['target_scan.scan'].call_count, 2)
        self.assertEqual(self.mutations(), [])
        archive = self.retired()
        self.journal('delete-requested', 'sealed', archive=str(archive))
        self.api.states = ['Deleted']
        code, output, ask = self.run_delete(name='space', answer=EOFError())
        self.assertEqual(code, 0, output)
        ask.assert_not_called()
        self.mocks['scratch.terminate_retired'].assert_called_once()
        self.assertNotIn('delete', self.api.calls)
        # Retirement returned, but the caller crashed before saving its archive or step.
        self.bind()
        self.worker_running = False
        completed = self.d.workspace.retire(self.project, self.state, 'space')
        self.journal('deleted', cleanup={'keeper': 'done', 'sync worker': 'done'})
        self.factory.side_effect = AssertionError('known-deleted replay is local')
        self.mocks['ssh_hosts.remove'].reset_mock()
        self.mocks['herdr.remove_profile'].reset_mock()
        code, output, _ = self.run_delete(name='space')
        self.assertEqual(code, 0, output)
        saved = read_json(self.state / 'deletions/space.json')
        self.assertEqual((saved['kind'], saved['archive']), ('paired', str(completed)))
        self.mocks['ssh_hosts.remove'].assert_called_once()
        self.mocks['herdr.remove_profile'].assert_called_once()

    def test_rebind_during_cleanup_leaves_the_new_pairing_alone(self):
        archive = self.retired()
        self.journal(archive=str(archive))
        code, output, _ = self.run_delete(name='space')
        self.assertEqual(code, 0, output)
        self.assertIn('The project moved to new-space meanwhile', output)
        for name in ('keeper.stop', 'scratch.stop', 'ssh_hosts.remove', 'herdr.remove_profile'):
            self.mocks[name].assert_not_called()
        self.mocks['scratch.terminate_retired'].assert_called_once()
        self.mocks['restore.end_target_proxies'].assert_called_once_with('space', self.state)

    def test_externally_deleted_target_cleans_up_only_after_typing_delete(self):
        self.api.states = ['Deleted']
        for answer in ('yes', EOFError()):
            code, output, _ = self.run_delete(answer=answer)
            self.assertEqual(code, 1)
            self.assertIn('This Mac still keeps:', output)
            self.assertEqual(self.mutations(), [])
        self.assertEqual(self.run_delete()[0], 0)

    def test_paired_target_is_never_started(self):
        self.api.states = ['Shutdown']
        code, output, _ = self.run_delete(name='space', start=True)
        self.assertEqual(code, 1)
        self.assertIn('work:cs:restart', output)
        self.assertEqual(self.mutations(), [])

    def test_start_is_its_own_operation(self):
        self.retired()
        self.api.states = ['Shutdown', 'Available']
        code, output, _ = self.run_delete(name='space', start=True)
        self.assertEqual(code, 0, output)
        self.assertIn('Started space. Check it:', output)
        self.assertEqual(self.mutations(), ['post_start'])
        self.mocks['target_scan.scan'].assert_not_called()
        for tty, repository, state in ((False, 'example/project', 'Shutdown'),
                                       (True, 'example/other', 'Shutdown'),
                                       (True, 'example/project', 'Available')):
            self.api.calls = []
            self.api.repository, self.api.states = repository, [state]
            self.assertEqual(self.run_delete(name='space', start=True, tty=tty)[0], 1)
            self.assertEqual(self.mutations(), [])
        self.journal('deleted', 'sealed')
        self.api.calls = []
        self.assertEqual(self.run_delete(name='space', start=True)[0], 1)
        self.assertEqual(self.api.calls, [])

    def test_never_paired_target_is_not_connected(self):
        (self.state / 'scratch.json').unlink()
        code, output, _ = self.run_delete(name='space', check=True, as_json=True)
        self.assertEqual(code, 1)
        self.assertTrue(all('pair it first' in row['text'] for row in json.loads(output)['categories']))
        self.connect.assert_not_called()
        self.bind()
        self.connect.return_value.__enter__.side_effect = ValueError('fixture private transport error')
        code, output, _ = self.run_delete(check=True, as_json=True)
        self.assertEqual(code, 1)
        self.assertIn('could not connect to space', json.loads(output)['message'])
        self.assertNotIn('private transport error', output)
        self.assertEqual(self.mutations(), [])

    def test_another_repository_is_refused_before_any_check(self):
        self.api.repository = 'example/another'
        code, output, _ = self.run_delete(check=True, as_json=True)
        self.assertEqual(code, 1)
        self.assertIn('another repository', json.loads(output)['message'])
        self.connect.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_a_deleted_journal_resumes_without_an_api_call_and_checks_its_own_account(self):
        cleanup = {step: 'done' for step in self.d.STEPS['paired']}
        cleanup['Herdr profile'] = 'Herdr could not be read'
        self.journal(cleanup=cleanup)
        self.factory.side_effect = AssertionError('deleted replay must not touch the API')
        code, output, _ = self.run_delete()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.mutations(), ['herdr.remove_profile'])
        self.journal(account='other')
        code, output, _ = self.run_delete(check=True, as_json=True)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['outcome'], 'refused')

    def test_inside_a_codespace_or_with_an_env_token_refuses(self):
        for env in ({'CODESPACES': 'true'}, {'GH_TOKEN': 'fixture'}, {'GITHUB_TOKEN': 'fixture'}):
            code, output, _ = self.run_delete(check=True, as_json=True, env=env)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output)['outcome'], 'refused')
            self.assertNotIn('fixture', output)
        self.factory.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_cleanup_failure_is_pending_and_independent_steps_still_run(self):
        self.journal()
        self.mocks['restore.end_target_proxies'].side_effect = ValueError('a Herdr connection to space is still open')
        code, output, _ = self.run_delete()
        self.assertEqual(code, 1)
        self.assertIn('cleanup is not finished: Herdr connections', output)
        self.mocks['ssh_hosts.remove'].assert_called_once()
        self.mocks['herdr.remove_profile'].assert_called_once()
        self.assertEqual(read_json(self.state / 'deletions/space.json')['phase'], 'deleted')
        self.mocks['restore.end_target_proxies'].side_effect = None
        with patch.object(self.d, 'atomic_write', side_effect=OSError('fixture journal failure')):
            code, output, _ = self.run_delete(name='space')
        self.assertEqual(code, 1)
        self.assertIn('cleanup is not finished', output)
        self.assertNotIn('nothing was deleted', output.lower())

    def test_paired_cleanup_order_and_disarm_line(self):
        arming.arm(self.state, gh_user='octo', codespace='space', consent='x', billable_owner='octo', ports=[], now=1)
        observed = []
        for name in ('keeper.stop', 'scratch.stop', 'restore.end_target_proxies', 'ssh_hosts.remove',
                     'herdr.remove_profile'):
            def action(*args, _name=name, **kwargs):
                observed.append(_name)
                if _name == 'scratch.stop':
                    self.worker_running = False
                if _name == 'restore.end_target_proxies':
                    self.assertFalse((self.state / 'scratch.json').exists())
                return 'removed'
            self.mocks[name].side_effect = action
        code, output, _ = self.run_delete()
        self.assertEqual(code, 0, output)
        self.assertEqual(observed, ['keeper.stop', 'scratch.stop', 'restore.end_target_proxies',
                                    'ssh_hosts.remove', 'herdr.remove_profile'])
        self.assertIn('Recovery is off: space was deleted.', output)
        self.assertFalse(arming.read(self.state)['armed'])

    def test_no_pairing_names_the_generated_task_in_human_and_json(self):
        (self.state / 'scratch.json').unlink()
        self.mocks['standing.read'].return_value = replace(self.local, generated=True, managed=frozenset())
        for as_json in (False, True):
            code, output, _ = self.run_delete(check=True, as_json=as_json)
            self.assertEqual(code, 1)
            message = json.loads(output)['message'] if as_json else output.strip()
            self.assertEqual(message, 'No Codespace is paired. Name one: '
                             'task devcontainer:work:cs:delete -- --codespace NAME')
        self.factory.assert_not_called()

    def test_delete_lock_and_foreign_pairing_refuse_without_mutation(self):
        with locked(self.state / 'delete.lock'):
            code, output, _ = self.run_delete(check=True, as_json=True)
        self.assertEqual(code, 1)
        self.assertIn('Another delete', json.loads(output)['message'])
        self.write(self.state / 'scratch.json', dict(identity='github.com/example/other', codespace='space'))
        self.assertEqual(json.loads(self.run_delete(check=True, as_json=True)[1])['outcome'], 'refused')
        self.factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
