"""Hermetic CLI moves and replay at every durable recovery/registry boundary."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from devcontainer_setup.project_sync import __main__ as cli, arming, keeper, lifecycle, scratch, workspace
from devcontainer_setup.project_sync.codespaces import Codespaces
from devcontainer_setup.project_sync.state import Project, atomic_write, locked, read_json


class RecoveryMoveTests(unittest.TestCase):
    def setUp(self):
        self.patch('subprocess.run', side_effect=AssertionError('unmocked subprocess'))
        self.patch('subprocess.Popen', side_effect=AssertionError('unmocked subprocess'))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'desktop'
        self.root.mkdir()
        self.state = self.base / 'state'
        self.project = Project(self.root, 'https://github.com/example/ordinary',
                               'github.com/example/ordinary', 'fixture-project')
        self.old_root = '/workspaces/old-project'
        self.new_root = '/workspaces/new-project'
        self.binding('old-cs', self.old_root)
        workspace._write(self.state, dict(workspace.registry(self.project, self.state), codespace='old-cs',
                         active_host='codespace', remote_root=self.old_root, remote_active_root=self.old_root,
                         verified_remote_historical_roots=['/workspaces/old-only']))
        self.armed = arming.arm(self.state, gh_user='me', codespace='old-cs',
                               consent=arming.consent_text(self.root.name, 'old-cs', 'me'),
                               billable_owner='me', ports=[3000], now=1000.0)
        self.armed.update(hold_until=9000.0, owner_stop_at=1500.0, pending_restore={'rid': 'old-restart'})
        arming.write(self.state, self.armed)
        lifecycle.Ledger(self.state / keeper.LEDGER).append('intent', 'retained-restart', at=1000.0)
        self.order = []
        self.out, self.err = io.StringIO(), io.StringIO()
        replacement = patch.dict(os.environ, {'CODESPACES': 'false'})
        replacement.start()
        self.addCleanup(replacement.stop)
        self.patch('devcontainer_setup.project_sync.__main__.discover_project', return_value=self.project)
        self.patch('devcontainer_setup.project_sync.__main__.state_dir', return_value=self.state)
        self.patch('devcontainer_setup.project_sync.workspace._project', return_value=self.project)
        self.patch('devcontainer_setup.project_sync.arming.account', return_value='me')
        self.patch('devcontainer_setup.project_sync.codespaces.Codespaces._api_env', return_value={})
        self.row = dict(name='new-cs', state='Available', repository={'full_name': 'example/ordinary'},
                        billable_owner={'login': 'me'})
        self.api = self.patch('devcontainer_setup.project_sync.codespaces.Codespaces._api',
                              side_effect=lambda *_args, **_kwargs: dict(self.row))
        self.patch('devcontainer_setup.project_sync.codespaces.Codespaces.space', side_effect=self.space)
        self.roots = self.patch('devcontainer_setup.project_sync.codespaces.Codespaces.roots',
                                return_value=[self.new_root])
        self.start_codespace = self.patch('devcontainer_setup.project_sync.codespaces.Codespaces.start_codespace')
        self.evidence = self.patch('devcontainer_setup.project_sync.evidence.record_start')
        self.patch('devcontainer_setup.project_sync.keeper.foreign_processes', return_value=[])
        self.patch('devcontainer_setup.project_sync.keeper.status', return_value={'running': False})
        self.stop = self.patch('devcontainer_setup.project_sync.keeper.stop',
                               side_effect=lambda *_: self.order.append('keeper.stop'))
        self.start = self.patch('devcontainer_setup.project_sync.keeper.start', side_effect=self.start_keeper)
        self.patch('devcontainer_setup.project_sync.keeper.wait_ready', return_value={'phase': 'watching'})
        self.patch('devcontainer_setup.project_sync.workspace._prerequisites')
        self.patch('devcontainer_setup.project_sync.transport.CodespaceConnection', return_value=MagicMock())
        self.patch('devcontainer_setup.project_sync.workspace._verify_roots', side_effect=self.verify_roots)
        self.patch('devcontainer_setup.project_sync.workspace._remote_herdr', return_value=None)
        self.retarget = self.patch('devcontainer_setup.project_sync.scratch.retarget', side_effect=self.retarget_to)
        self.patch('devcontainer_setup.project_sync.scratch.start')
        self.patch('devcontainer_setup.project_sync.workspace._ready', return_value={'conversations': {'outcome': 'complete'}})
        self.patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh')
        self.patch('devcontainer_setup.project_sync.restore.end_proxies_elsewhere', return_value=[])
        self.patch('devcontainer_setup.project_sync.herdr.open_remote',
                   side_effect=lambda *_a, **_k: self.order.append('herdr') or 0)
        self.input = self.patch('builtins.input', return_value='arm')
        self.patch('devcontainer_setup.project_sync.__main__._arm', side_effect=AssertionError('move called _arm'))
        self.patch('devcontainer_setup.project_sync.workspace._write', side_effect=self.write_registry_value)
        self.real_write_registry = lambda state, value: atomic_write(
            state / 'workspace.json', json.dumps(value, sort_keys=True).encode() + b'\n')
        self.move = importlib.import_module('devcontainer_setup.project_sync.move')
        real_begin = self.move.begin
        self.begin = self.patch('devcontainer_setup.project_sync.move.begin')
        self.begin.side_effect = lambda *a, **k: (self.order.append('intent'), real_begin(*a, **k))[1]
        real_consent, real_arm = arming.move_consent, arming.arm
        self.consent = self.patch('devcontainer_setup.project_sync.arming.move_consent')
        self.consent.side_effect = lambda *a, **k: (self.order.append('consent'), real_consent(*a, **k))[1]
        self.fresh = self.patch('devcontainer_setup.project_sync.arming.arm')
        self.fresh.side_effect = lambda *a, **k: (self.order.append('consent'), real_arm(*a, **k))[1]

    def patch(self, target, **kwargs):
        replacement = patch(target, **kwargs)
        result = replacement.start()
        self.addCleanup(replacement.stop)
        return result

    def binding(self, name, root):
        scratch._write(self.state / scratch.CONFIG, dict(version=1, identity=self.project.identity,
                       root=str(self.root), codespace=name, remote_root=root))

    def space(self):
        row = dict(self.row)
        row['name'] = read_json(self.state / scratch.CONFIG)['codespace']
        return row

    def verify_roots(self, _connection, _project, roots):
        self.assertNotIn('/workspaces/old-only', roots, 'OLD history must not be verified on NEW')
        return roots

    def retarget_to(self, _root, _state, name, remote_root):
        self.order.append('retarget')
        self.binding(name, remote_root)

    def write_registry_value(self, state, value):
        self.order.append('registry')
        self.real_write_registry(state, value)

    def start_keeper(self, _root, _state):
        with locked(self.state / 'workspace-control.lock'):
            self.order.append('keeper.start')
        self.keeper_ports = arming.read(self.state)['ports']
        self.keeper_target = arming.read(self.state)['codespace']
        return {'nonce': 'fixture-nonce'}

    def connect(self, *flags, action='--codespace'):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err), \
                patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True):
            return cli.main(['--project', str(self.root), 'work', action, *flags])

    def snapshot(self):
        return {name: (self.state / name).read_bytes()
                for name in (arming.ARMING, scratch.CONFIG, 'workspace.json', keeper.LEDGER)}

    def decide(self):
        return self.move.decide(self.project, self.state, 'new-cs', ask=self.input)

    def crash_after_retarget(self):
        def crash(*args):
            self.retarget_to(*args)
            raise ValueError('crash after retarget')
        self.retarget.side_effect = crash
        self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
        self.retarget.side_effect = self.retarget_to

    def test_same_account_move_carries_consent_with_one_line(self):
        before = self.snapshot()
        self.assertEqual(self.connect('--rebind', 'new-cs'), 0, self.err.getvalue())
        self.assertEqual(self.order, ['intent', 'keeper.stop', 'retarget', 'registry', 'consent', 'keeper.start', 'herdr'])
        self.input.assert_not_called()
        value = arming.read(self.state)
        for field in ('consented_at', 'armed_at', 'hold_until', 'ports'):
            self.assertEqual(value[field], self.armed[field])
        self.assertNotIn('owner_stop_at', value)
        self.assertNotIn('pending_restore', value)
        self.assertEqual(self.snapshot()[keeper.LEDGER], before[keeper.LEDGER])
        self.assertEqual(self.keeper_target, 'new-cs')
        self.assertEqual(self.out.getvalue().count('Recovery moved old-cs → new-cs; consent from '), 1)
        self.assertFalse((self.state / self.move.INTENT).exists())
        self.assertFalse((self.state / workspace.PENDING).exists())
        self.fresh.assert_not_called()

    def test_port_flags_reach_the_new_keeper(self):
        self.assertEqual(self.connect('--rebind', 'new-cs', '--port', '8080'), 0)
        self.assertEqual(self.keeper_ports, [3000, 8080])
        value = arming.read(self.state)
        self.assertEqual((value['armed_at'], value['consented_at'], value['hold_until']), (1000.0, 1000.0, 9000.0))
        archived = [read_json(path) for path in (self.state / 'arming-history').glob('*.json')]
        self.assertEqual(archived, [self.armed])
        self.row['name'] = 'third-cs'
        self.assertEqual(self.connect('--rebind', 'third-cs', '--no-port', '3000'), 0)
        self.assertEqual(self.keeper_ports, [8080])
        self.assertEqual(arming.read(self.state)['ports'], [8080])

    def test_changed_owner_asks_and_arms_new(self):
        self.row['billable_owner'] = {'login': 'other'}
        self.assertEqual(self.connect('--rebind', 'new-cs'), 0, self.err.getvalue())
        self.input.assert_called_once()
        self.consent.assert_not_called()
        self.fresh.assert_called_once()
        value = arming.read(self.state)
        self.assertEqual((value['codespace'], value['billable_owner'], value['ports']), ('new-cs', 'other', [3000]))
        self.assertNotEqual(value['consented_at'], 1000.0)
        self.assertLess(self.order.index('consent'), self.order.index('keeper.start'))
        self.assertIn('Armed as me for new-cs; ports ', self.out.getvalue())

    def test_decline_moves_nothing(self):
        self.row['billable_owner'] = {'login': 'other'}
        before = self.snapshot()
        for answer in ('no', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=type(answer).__name__):
                self.input.side_effect = [answer]
                self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
                self.assertEqual(self.snapshot(), before)
                self.retarget.assert_not_called()
                self.stop.assert_not_called()
                self.start_codespace.assert_not_called()
                self.assertFalse((self.state / self.move.INTENT).exists())

    def test_owner_changed_after_deciding_refuses_before_any_start(self):
        initial = dict(self.row)
        before = self.snapshot()
        for owner in ('other', ''):
            with self.subTest(owner=owner):
                changed = dict(initial, state='Shutdown', billable_owner={'login': owner})
                self.api.side_effect = [initial, changed]
                self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
                self.assertEqual(self.snapshot(), before)
                self.assertIn('changed billing owner since you confirmed', self.err.getvalue())
                self.start_codespace.assert_not_called()
                self.evidence.assert_not_called()
                self.roots.assert_not_called()
                self.stop.assert_not_called()
                self.retarget.assert_not_called()

    def test_arming_changed_after_deciding_refuses(self):
        self.row['state'] = 'Shutdown'
        def edit(*_args, **_kwargs):
            arming.write(self.state, dict(self.armed, hold_until=10000.0))
            return [self.new_root]
        self.roots.side_effect = edit
        self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
        self.assertIn('Recovery changed while you were deciding', self.err.getvalue())
        self.stop.assert_not_called()
        self.retarget.assert_not_called()
        self.assertFalse((self.state / self.move.INTENT).exists())

    def test_arming_changed_after_deciding_refuses_before_any_start(self):
        self.row['state'] = 'Shutdown'
        real_decide = self.move.decide
        for change in ({'armed': False}, {'hold_until': 10000.0}):
            with self.subTest(change=change):
                arming.write(self.state, self.armed)
                before = self.snapshot()

                def edit_after_deciding(*args, **kwargs):
                    decision = real_decide(*args, **kwargs)
                    arming.write(self.state, dict(self.armed, **change))
                    before[arming.ARMING] = (self.state / arming.ARMING).read_bytes()
                    return decision

                with patch.object(self.move, 'decide', side_effect=edit_after_deciding):
                    self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
                self.assertIn(self.move.CHANGED, self.err.getvalue())
                self.start_codespace.assert_not_called()
                self.evidence.assert_not_called()
                self.roots.assert_not_called()
                self.begin.assert_not_called()
                self.stop.assert_not_called()
                self.retarget.assert_not_called()
                self.start.assert_not_called()
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.state / self.move.INTENT).exists())
                self.assertFalse((self.state / workspace.PENDING).exists())

    def test_account_switch_during_validation_moves_nothing(self):
        before = self.snapshot()
        account = arming.account

        def switch_during_roots(*_args, **_kwargs):
            account.return_value = 'other'
            return [self.new_root]

        self.roots.side_effect = switch_during_roots
        self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
        self.assertIn('The gh account changed since you confirmed; nothing moved. Run the command again.',
                      self.err.getvalue())
        self.begin.assert_not_called()
        self.stop.assert_not_called()
        self.retarget.assert_not_called()
        self.start.assert_not_called()
        self.consent.assert_not_called()
        self.fresh.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.state / self.move.INTENT).exists())
        self.assertFalse((self.state / workspace.PENDING).exists())

    def test_crash_after_retarget_is_finished_by_plain_connect(self):
        original_registry = read_json(self.state / 'workspace.json')
        self.crash_after_retarget()
        self.assertTrue((self.state / workspace.PENDING).exists())
        self.assertEqual(self.connect(), 0, self.err.getvalue())
        self.assertEqual(arming.read(self.state)['codespace'], 'new-cs')
        value = read_json(self.state / 'workspace.json')
        self.assertEqual(value['verified_remote_historical_roots'], [])
        self.assertEqual(value['remote_root'], self.new_root)
        self.assertIn(original_registry, [read_json(p) for p in (self.state / 'workspace-history').glob('*.json')])
        self.assertEqual(self.keeper_target, 'new-cs')
        self.input.assert_not_called()
        self.assertEqual(self.consent.call_count, 1)
        self.assertFalse((self.state / workspace.PENDING).exists())
        self.assertFalse((self.state / self.move.INTENT).exists())

    def test_crash_after_registry_before_consent_is_finished(self):
        with patch.object(self.move, 'commit', side_effect=ValueError('crash before consent')):
            self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
        self.assertFalse((self.state / workspace.PENDING).exists())
        self.assertEqual(arming.read(self.state)['codespace'], 'old-cs')
        self.assertEqual(self.connect(), 0)
        self.assertEqual(self.consent.call_count, 1)
        self.assertEqual(self.keeper_target, 'new-cs')
        self.input.assert_not_called()

    def test_crash_after_consent_before_intent_unlink(self):
        initial_registry = (self.state / 'workspace.json').read_bytes()
        unlink = Path.unlink
        for disarmed in (False, True):
            with self.subTest(disarmed=disarmed):
                self.binding('old-cs', self.old_root)
                arming.write(self.state, self.armed)
                atomic_write(self.state / 'workspace.json', initial_registry)
                def crash(path, *args, **kwargs):
                    if path == self.state / self.move.INTENT:
                        raise OSError('crash before intent unlink')
                    return unlink(path, *args, **kwargs)
                with patch.object(Path, 'unlink', new=crash):
                    self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
                self.assertEqual(arming.read(self.state)['codespace'], 'new-cs')
                if disarmed:
                    self.assertEqual(self.connect(action='--disarm'), 0)
                before = (self.state / arming.ARMING).read_bytes()
                self.start.reset_mock()
                self.assertEqual(self.connect(), 0, self.err.getvalue())
                self.assertEqual((self.state / arming.ARMING).read_bytes(), before)
                self.assertFalse((self.state / self.move.INTENT).exists())
                self.assertNotIn('interrupted and recovery settings changed', self.out.getvalue())
                if disarmed:
                    self.start.assert_not_called()
        self.assertEqual(self.consent.call_count, 2)
        self.input.assert_not_called()

    def test_unarmed_rebind_without_a_registry_file(self):
        arming.write(self.state, dict(self.armed, armed=False))
        (self.state / 'workspace.json').unlink()
        self.crash_after_retarget()
        self.assertIsNone(read_json(self.state / workspace.PENDING)['predecessor_sha256'])
        self.assertEqual(self.connect(), 0, self.err.getvalue())
        self.assertEqual(read_json(self.state / 'workspace.json')['codespace'], 'new-cs')
        self.assertFalse((self.state / workspace.PENDING).exists())
        self.start.assert_not_called()
        self.binding('old-cs', self.old_root)
        (self.state / 'workspace.json').unlink()
        self.assertEqual(self.connect('--rebind', 'new-cs'), 0)
        self.assertEqual(read_json(self.state / 'workspace.json')['codespace'], 'new-cs')

    def test_crash_before_retarget_keeps_old(self):
        before = self.snapshot()
        self.retarget.side_effect = ValueError('crash before retarget')
        self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
        self.retarget.side_effect = self.retarget_to
        self.row['name'] = 'old-cs'
        self.roots.return_value = [self.old_root, '/workspaces/old-only']
        self.patch('devcontainer_setup.project_sync.workspace._verify_roots', side_effect=lambda _c, _p, roots: roots)
        self.assertEqual(self.connect(), 0, self.err.getvalue())
        self.assertEqual((self.state / arming.ARMING).read_bytes(), before[arming.ARMING])
        self.assertIn('The move to new-cs did not finish; recovery stays on old-cs.', self.out.getvalue())
        self.assertFalse((self.state / self.move.INTENT).exists())
        self.assertFalse((self.state / workspace.PENDING).exists())

    def test_replay_after_disarm_does_not_rearm(self):
        for carry in (True, False):
            with self.subTest(carry=carry):
                self.binding('old-cs', self.old_root)
                arming.write(self.state, self.armed)
                self.row['billable_owner'] = {'login': 'me' if carry else 'other'}
                self.crash_after_retarget()
                self.assertEqual(self.connect(action='--disarm'), 0)
                before = (self.state / arming.ARMING).read_bytes()
                self.start.reset_mock()
                self.assertEqual(self.connect(), 0, self.err.getvalue())
                self.assertEqual((self.state / arming.ARMING).read_bytes(), before)
                self.assertFalse(arming.read(self.state)['armed'])
                self.start.assert_not_called()
                self.assertFalse((self.state / self.move.INTENT).exists())

    def test_armed_alias_rebind_takes_the_same_path(self):
        self.assertEqual(self.connect('--rebind', 'new-cs', action='--armed'), 0)
        self.fresh.assert_not_called()
        self.input.assert_not_called()
        self.assertEqual(self.keeper_target, 'new-cs')
        self.assertEqual(self.consent.call_count, 1)

    def test_unarmed_rebind_is_unchanged(self):
        arming.write(self.state, dict(self.armed, armed=False))
        with patch.object(arming, 'account') as account:
            self.assertEqual(self.connect('--rebind', 'new-cs'), 0)
        account.assert_not_called()
        self.begin.assert_not_called()
        self.stop.assert_not_called()
        self.start.assert_not_called()
        self.input.assert_not_called()
        self.assertEqual(read_json(self.state / scratch.CONFIG)['codespace'], 'new-cs')

    def test_rebind_keeps_then_signin_all(self):
        with patch('devcontainer_setup.project_sync.signins.run',
                   side_effect=lambda *_: self.order.append('signins') or 0) as signins:
            self.assertEqual(self.connect('--rebind', 'new-cs', '--then-signin-all'), 0)
        signins.assert_called_once_with(self.root, self.state, 'new-cs')
        self.assertEqual(self.order[-3:], ['keeper.start', 'signins', 'herdr'])

    def test_missing_owner_and_wrong_repository_never_move(self):
        before = self.snapshot()
        for change in ({'billable_owner': {}}, {'repository': {'full_name': 'example/elsewhere'}}):
            with self.subTest(change=change), patch.object(Codespaces, '_api', return_value=dict(self.row, **change)):
                self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
                self.assertEqual(self.snapshot(), before)
        self.start_codespace.assert_not_called()
        self.retarget.assert_not_called()
        self.input.assert_not_called()

    def test_changed_account_and_policy_require_confirmation(self):
        for value in (dict(self.armed, gh_user='other'), dict(self.armed, consent_sha256='stale')):
            with self.subTest(value=value):
                arming.write(self.state, value)
                self.input.side_effect = ['no']
                self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
        self.assertEqual(self.input.call_count, 2)
        self.retarget.assert_not_called()

    def test_later_hold_or_port_edit_blocks_replay(self):
        for change in ({'hold_until': 12345.0}, {'ports': [8080]}):
            with self.subTest(change=change):
                self.binding('old-cs', self.old_root)
                arming.write(self.state, self.armed)
                decision = self.decide()
                self.move.begin(self.state, decision, now=2000.0)
                self.binding('new-cs', self.new_root)
                arming.write(self.state, dict(self.armed, **change))
                before = (self.state / arming.ARMING).read_bytes()
                with contextlib.redirect_stdout(self.out):
                    self.move.finish_pending(self.project, self.state)
                self.assertEqual((self.state / arming.ARMING).read_bytes(), before)
                self.assertFalse((self.state / self.move.INTENT).exists())
        self.consent.assert_not_called()
        self.fresh.assert_not_called()

    def test_registry_replay_requires_the_predecessor_and_target(self):
        original = (self.state / 'workspace.json').read_bytes()
        self.crash_after_retarget()
        changed = dict(read_json(self.state / 'workspace.json'), owner_note='retained')
        self.real_write_registry(self.state, changed)
        before = (self.state / 'workspace.json').read_bytes()
        pending = read_json(self.state / workspace.PENDING)
        for target, predecessor in (('new-cs', before), ('unrelated-cs', original)):
            with self.subTest(target=target):
                atomic_write(self.state / 'workspace.json', predecessor)
                self.binding(target, self.new_root)
                atomic_write(self.state / workspace.PENDING, json.dumps(pending).encode())
                with patch.object(workspace, '_choose_pairing', side_effect=ValueError('end after replay')):
                    with self.assertRaisesRegex(ValueError, 'end after replay'):
                        workspace.remote(self.project, self.state)
                self.assertEqual((self.state / 'workspace.json').read_bytes(), predecessor)
                self.assertFalse((self.state / workspace.PENDING).exists())

    def test_decision_prompt_runs_without_the_workspace_lock(self):
        self.row['billable_owner'] = {'login': 'other'}
        def answer(*_):
            with locked(self.state / 'workspace-control.lock'):
                return 'arm'
        self.input.side_effect = answer
        self.assertEqual(self.connect('--rebind', 'new-cs'), 0)
        self.input.assert_called_once()


if __name__ == '__main__':
    unittest.main()
