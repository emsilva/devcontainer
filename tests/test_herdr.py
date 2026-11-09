import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from devcontainer_setup.project_sync.state import Project, read_json
from devcontainer_setup.project_sync import herdr


def completed(value):
    return subprocess.CompletedProcess([], 0, json.dumps(value), '')


def cold_socket_error(reason='Connection refused (os error 111)'):
    # Captured native failure after a real stopped Codespace was started:
    # machine enable returns before the remote saved session recreates its API.
    return subprocess.CompletedProcess([], 1, '',
        'Error: Custom { kind: ConnectionAborted, error: "remote SSH connection failed: '
        'failed to connect to remote Herdr API socket /fixture/herdr.sock: ' + reason + '" }')


class HerdrTests(unittest.TestCase):
    def fake_catalog(self, rows):
        catalog, calls = self.home / 'catalog.json', self.home / 'calls.jsonl'
        catalog.write_text(json.dumps(rows))
        binary = self.home / 'herdr'
        binary.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
catalog = pathlib.Path({str(catalog)!r})
args = sys.argv[1:]
with open({str(calls)!r}, 'a') as out: out.write(json.dumps(args) + '\\n')
rows = json.loads(catalog.read_text())
if args[:2] == ['machine', 'list']:
 if os.environ.get('FAKE_UNREADABLE'): sys.exit(1)
 print(json.dumps(rows))
elif args[:2] == ['machine', 'add']:
 rows.append(dict(id='new-id', target=args[2], session=args[-1], enabled=True))
 catalog.write_text(json.dumps(rows))
elif args[:2] == ['machine', 'remove']:
 if not os.environ.get('FAKE_KEEP_PROFILE'):
  catalog.write_text(json.dumps([row for row in rows if row['id'] != args[2]]))
elif args == ['--version']: print('herdr fixture')
''')
        binary.chmod(0o700)
        env = mock.patch.dict(os.environ, {'PATH': str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        return catalog, calls

    def test_receipt_records_whether_setup_created_the_profile(self):
        alias, session = 'devcontainer-' + self.project.project_id, herdr._session(self.project)
        catalog, _ = self.fake_catalog([])
        self.pair('space')
        receipt_path = self.state / 'herdr-machine.json'
        with mock.patch.object(herdr, 'configure_ssh', return_value=alias), mock.patch.object(herdr, '_workspace'):
            herdr.open_remote(self.project, self.state, '/remote', expected_codespace='space')
            self.assertIs(read_json(receipt_path)['created'], True)
            herdr.open_remote(self.project, self.state, '/remote', expected_codespace='space')
            self.assertIs(read_json(receipt_path)['created'], True)
            receipt_path.unlink()
            herdr.open_remote(self.project, self.state, '/remote', expected_codespace='space')
            self.assertIs(read_json(receipt_path)['created'], False)
            saved = read_json(receipt_path)
            saved.pop('created')
            receipt_path.write_text(json.dumps(saved))
            herdr.open_remote(self.project, self.state, '/remote', expected_codespace='space')
            self.assertNotEqual(read_json(receipt_path).get('created'), True)
            # Replacement produces a different opaque ID.
            rows = json.loads(catalog.read_text())
            rows[0]['id'] = 'old-id'
            catalog.write_text(json.dumps(rows))
            saved.update(machine_id='old-id', codespace='old-space')
            receipt_path.write_text(json.dumps(saved))
            herdr.open_remote(self.project, self.state, '/remote', expected_codespace='space')
            self.assertIs(read_json(receipt_path)['created'], True)
            self.assertEqual(read_json(receipt_path)['machine_id'], 'new-id')

    def test_only_a_created_profile_is_removed(self):
        from devcontainer_setup.project_sync.state import atomic_write
        alias, session = 'devcontainer-' + self.project.project_id, herdr._session(self.project)
        owned = dict(id='owned', target=alias, session=session, enabled=True)
        foreign = dict(id='foreign', target=alias, session='other', enabled=True)
        catalog, calls = self.fake_catalog([owned, foreign])
        receipt = self.state / 'herdr-machine.json'
        for provenance in ({}, {'created': False}, {'created': True}):
            atomic_write(receipt, json.dumps(dict(machine_id='owned', alias=alias, session=session, **provenance)).encode())
            self.assertEqual(herdr.remove_profile(self.project, self.state), 'removed' if provenance.get('created') else 'kept')
            self.assertFalse(receipt.exists())
        self.assertEqual(json.loads(catalog.read_text()), [foreign])
        removals = [json.loads(line) for line in calls.read_text().splitlines() if 'remove' in line]
        self.assertEqual(removals, [['machine', 'remove', 'owned']])
        self.assertEqual(herdr.remove_profile(self.project, self.state), 'absent')
        atomic_write(receipt, json.dumps(dict(machine_id='foreign', created=True)).encode())
        self.assertEqual(herdr.remove_profile(self.project, self.state), 'absent')
        self.assertEqual(json.loads(catalog.read_text()), [foreign])

    def test_unreadable_herdr_keeps_the_receipt(self):
        from devcontainer_setup.project_sync.state import atomic_write
        alias, session = 'devcontainer-' + self.project.project_id, herdr._session(self.project)
        self.fake_catalog([dict(id='owned', target=alias, session=session)])
        receipt = self.state / 'herdr-machine.json'
        atomic_write(receipt, json.dumps(dict(machine_id='owned', alias=alias, session=session, created=True)).encode())
        before = receipt.read_bytes()
        for extra, reason in (({'FAKE_UNREADABLE': '1'}, 'Herdr could not be read'),
                              ({'PATH': str(self.home / 'missing')}, 'Herdr could not be read'),
                              ({'FAKE_KEEP_PROFILE': '1'}, 'Herdr did not remove the profile')):
            with self.subTest(extra=extra), mock.patch.dict(os.environ, extra):
                with self.assertRaisesRegex(ValueError, reason):
                    herdr.remove_profile(self.project, self.state)
            self.assertEqual(receipt.read_bytes(), before)

    def test_a_connect_resumed_after_retirement_writes_nothing(self):
        self.pair('old-space')
        started, finished, errors = threading.Event(), threading.Event(), []
        def connect():
            started.set()
            try:
                herdr.open_remote(self.project, self.state, '/remote', expected_codespace='old-space')
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
        with mock.patch.object(herdr, 'configure_ssh') as configure, mock.patch.object(herdr, '_json') as catalog:
            with herdr.locked(self.state / 'workspace-control.lock', blocking=True):
                thread = threading.Thread(target=connect)
                thread.start()
                self.assertTrue(started.wait(2))
                waited = not finished.wait(.1)
                (self.state / 'scratch.json').unlink()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertTrue(waited)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertIn('No Codespace is paired', str(errors[0]))
        configure.assert_not_called()
        catalog.assert_not_called()
        self.assertFalse((self.state / 'herdr-machine.json').exists())

    def test_a_connect_resumed_after_a_rebind_writes_nothing(self):
        self.pair('old-space')
        path = self.state / 'scratch.json'
        binding = read_json(path)
        for change in ({'codespace': 'new-space'}, {'identity': 'github.com/another/repo'}, {'remote_root': '/elsewhere'}):
            with self.subTest(change=change):
                path.write_text(json.dumps(dict(binding, **change)))
                before = path.read_bytes()
                with mock.patch.object(herdr, 'configure_ssh') as configure, mock.patch.object(herdr, '_json') as catalog:
                    with self.assertRaisesRegex(ValueError, 'The project moved to another Codespace meanwhile'):
                        herdr.open_remote(self.project, self.state, '/remote', expected_codespace='old-space')
                configure.assert_not_called()
                catalog.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.state / 'herdr-machine.json').exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.project = Project(self.home / 'repo', 'git@github.com:a/b', 'github.com/a/b', 'a' * 24)
        self.project.root.mkdir()
        self.state = self.home / 'state/devcontainer-setup/projects' / self.project.project_id
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'HERDR_ENV': '1'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_workspace_reuses_actual_checkout_and_focuses_opaque_workspace(self):
        rows = {'result': {'workspaces': [
            {'workspace_id': 'opaque-other', 'label': 'repo', 'worktree': {'checkout_path': '/other'}},
            {'workspace_id': 'opaque-real', 'label': 'anything', 'worktree': {'checkout_path': str(self.project.root)}}]}}
        with mock.patch.object(herdr.subprocess, 'run', side_effect=[completed(rows), completed({'result': {}})]) as run:
            herdr._workspace(self.project, str(self.project.root), [])
        self.assertEqual(run.call_args_list[-1].args[0], ['herdr', 'workspace', 'focus', 'opaque-real'])
        self.assertFalse(any('create' in call.args[0] for call in run.call_args_list))


    def test_remote_inside_herdr_reuses_machine_without_nested_client_or_settings_changes(self):
        self.pair('space', '/workspaces/recovered')
        config = self.home / '.config/herdr/config.toml'
        config.parent.mkdir(parents=True)
        original = '''[experimental]
allow_nested = false
other = "preserve"
[session]
resume_agents_on_restore = true
[ui.sound]
path = "sounds/bell.wav"
done_path = "/absolute/done.wav"
request_path = "request.wav"
'''
        config.write_text(original)
        rows = [
            {'id': 'wrong-session', 'target': 'alias', 'session': 'default', 'enabled': True},
            {'id': 'opaque-machine', 'target': 'alias', 'session': 'project-' + 'a' * 24, 'enabled': True},
        ]
        workspaces = {'result': {'workspaces': [{'workspace_id': 'remote-workspace',
                       'worktree': {'checkout_path': '/workspaces/recovered'}}]}}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed(rows), completed(workspaces), completed({'result': {}})]) as run, \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/workspaces/recovered', expected_codespace=self.codespace), 0)
        self.assertEqual(run.call_args_list[1].args[0][:3], ['herdr', '--machine', 'opaque-machine'])
        call.assert_not_called()
        self.assertFalse((self.state / 'herdr-nested.toml').exists())
        self.assertEqual(config.read_text(), original)
        self.assertNotIn('HERDR_CONFIG_PATH', os.environ)
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['machine_id'], 'opaque-machine')

    def test_missing_machine_uses_official_setup_then_reads_generated_id(self):
        self.pair('space', '/remote')
        machine = {'id': 'created-id', 'target': 'alias', 'session': 'project-' + 'a' * 24, 'enabled': True}
        calls = []
        def call(args, **kwargs):
            calls.append(args)
            return 0
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([]), completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'created-w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=call):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', expected_codespace=self.codespace), 0)
        self.assertEqual(calls[0], ['herdr', 'machine', 'add', 'alias', '--label', 'repo (Codespace)',
                                  '--remote-session', 'project-' + 'a' * 24])
        self.assertEqual(len(calls), 1)

    def test_machine_add_runs_without_the_workspace_lock(self):
        self.pair('space')
        machine = dict(id='new', target='alias', session=herdr._session(self.project), enabled=True)
        def call(args, **kwargs):
            self.assertEqual(args[:3], ['herdr', 'machine', 'add'])
            with herdr.locked(self.state / 'workspace-control.lock'):
                pass
            return 0
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr, '_matching_machines', side_effect=[[], [machine]]), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=call), \
                mock.patch.object(herdr, '_workspace'):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', expected_codespace='space'), 0)
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['machine_id'], 'new')

    def test_a_rebind_during_machine_add_preserves_the_new_receipt(self):
        self.pair('space')
        receipt = self.state / 'herdr-machine.json'
        newer = dict(machine_id='newer', codespace='new-space')
        def call(args, **kwargs):
            self.assertEqual(args[:3], ['herdr', 'machine', 'add'])
            self.pair('new-space')
            receipt.write_text(json.dumps(newer))
            return 0
        machine = dict(id='stale', target='alias', session=herdr._session(self.project), enabled=True)
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr, '_matching_machines', side_effect=[[], [machine]]), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=call), \
                mock.patch.object(herdr, '_workspace') as opened:
            with self.assertRaisesRegex(ValueError, 'The project moved to another Codespace meanwhile'):
                herdr.open_remote(self.project, self.state, '/remote', expected_codespace='space')
        self.assertEqual(read_json(receipt), newer)
        opened.assert_not_called()

    def test_concurrent_connects_create_only_one_owned_profile(self):
        self.pair('space')
        entered, resume, second_started = threading.Event(), threading.Event(), threading.Event()
        second_catalog = threading.Event()
        profiles, errors = [], []
        def catalog(*args):
            if threading.current_thread().name == 'second-connect':
                second_catalog.set()
            return list(profiles)
        def call(args, **kwargs):
            self.assertEqual(args[:3], ['herdr', 'machine', 'add'])
            if not entered.is_set():
                entered.set()
                if not resume.wait(10):
                    raise RuntimeError('fixture setup timed out')
            profiles.append(dict(id='profile-' + str(len(profiles)), target='alias',
                                 session=herdr._session(self.project), enabled=True))
            return 0
        def connect(second=False):
            if second:
                second_started.set()
            try:
                herdr.open_remote(self.project, self.state, '/remote', expected_codespace='space')
            except BaseException as error:
                errors.append(error)
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr, '_matching_machines', side_effect=catalog), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=call), \
                mock.patch.object(herdr, '_workspace'):
            first = threading.Thread(target=connect)
            second = threading.Thread(target=connect, args=(True,), name='second-connect')
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                second.start()
                self.assertTrue(second_started.wait(5))
                second_catalog.wait(.2)
            finally:
                resume.set()
                first.join(10)
                if second.ident is not None:
                    second.join(10)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(profiles), 1)
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['machine_id'], profiles[0]['id'])

    def pair(self, codespace, remote_root='/remote'):
        from devcontainer_setup.project_sync.state import atomic_write
        self.codespace = codespace
        atomic_write(self.state / 'scratch.json', json.dumps(dict(codespace=codespace,
                     identity=self.project.identity, root=str(self.project.root), remote_root=remote_root)).encode())

    def bind(self, codespace, receipt):
        from devcontainer_setup.project_sync.state import atomic_write
        self.pair(codespace)
        atomic_write(self.state / 'herdr-machine.json', json.dumps(receipt).encode())

    def test_a_replaced_codespace_gets_a_fresh_machine_and_retires_the_old_profile(self):
        session = 'project-' + 'a' * 24
        self.bind('new-space', {'machine_id': 'old-id', 'alias': 'alias', 'session': session, 'codespace': 'old-space'})
        old = {'id': 'old-id', 'target': 'alias', 'session': session, 'enabled': True}
        new = {'id': 'new-id', 'target': 'alias', 'session': session, 'enabled': True}
        calls = []
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([old]), completed([old, new]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]) as run, \
                mock.patch.object(herdr.subprocess, 'call', side_effect=lambda args, **_: calls.append(args) or 0):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', expected_codespace=self.codespace), 0)
        # The old profile's remote Herdr lives on the old Codespace; only setup installs it on the new one.
        self.assertEqual(calls, [['herdr', 'machine', 'add', 'alias', '--label', 'repo (Codespace)', '--remote-session', session],
                                 ['herdr', 'machine', 'remove', 'old-id']])
        self.assertEqual(run.call_args_list[2].args[0][:3], ['herdr', '--machine', 'new-id'])
        receipt = read_json(self.state / 'herdr-machine.json')
        self.assertEqual((receipt['machine_id'], receipt['codespace']), ('new-id', 'new-space'))

    def test_setup_that_saves_no_new_machine_keeps_the_old_profile(self):
        session = 'project-' + 'a' * 24
        self.bind('new-space', {'machine_id': 'old-id', 'alias': 'alias', 'session': session, 'codespace': 'old-space'})
        old = {'id': 'old-id', 'target': 'alias', 'session': session, 'enabled': True}
        calls = []
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([old]), completed([old])]), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=lambda args, **_: calls.append(args) or 0):
            with self.assertRaisesRegex(ValueError, 'existing profile kept'):
                herdr.open_remote(self.project, self.state, '/remote', expected_codespace=self.codespace)
        self.assertEqual([args[:3] for args in calls], [['herdr', 'machine', 'add']])
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['codespace'], 'old-space')

    def test_a_missing_binding_never_counts_as_a_replaced_codespace(self):
        from devcontainer_setup.project_sync.state import atomic_write
        session = 'project-' + 'a' * 24
        atomic_write(self.state / 'herdr-machine.json', json.dumps(
            {'machine_id': 'kept-id', 'alias': 'alias', 'session': session, 'codespace': 'old-space'}).encode())
        before = (self.state / 'herdr-machine.json').read_bytes()
        with mock.patch.object(herdr, 'configure_ssh') as configure, \
                mock.patch.object(herdr.subprocess, 'run') as run, \
                mock.patch.object(herdr.subprocess, 'call') as call:
            with self.assertRaisesRegex(ValueError, 'No Codespace is paired; run task work:cs'):
                herdr.open_remote(self.project, self.state, '/remote', expected_codespace='old-space')
        configure.assert_not_called()
        run.assert_not_called()
        call.assert_not_called()
        self.assertEqual((self.state / 'herdr-machine.json').read_bytes(), before)

    def test_a_receipt_from_before_codespace_tracking_adopts_the_current_machine(self):
        session = 'project-' + 'a' * 24
        self.bind('current-space', {'machine_id': 'kept-id', 'alias': 'alias', 'session': session})
        machine = {'id': 'kept-id', 'target': 'alias', 'session': session, 'enabled': True}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', expected_codespace=self.codespace), 0)
        call.assert_not_called()
        receipt = read_json(self.state / 'herdr-machine.json')
        self.assertEqual((receipt['machine_id'], receipt['codespace']), ('kept-id', 'current-space'))

    def test_a_codespace_without_herdr_gets_a_fresh_machine_whatever_the_receipt_says(self):
        # Measured 2026-10-24: a receipt naming the current Codespace, on a Codespace with no Herdr (#51).
        session = 'project-' + 'a' * 24
        self.bind('new-space', {'machine_id': 'old-id', 'alias': 'alias', 'session': session, 'codespace': 'new-space'})
        old = {'id': 'old-id', 'target': 'alias', 'session': session, 'enabled': True}
        new = {'id': 'new-id', 'target': 'alias', 'session': session, 'enabled': True}
        calls = []
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([old]), completed([old, new]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=lambda args, **_: calls.append(args) or 0):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', remote_herdr='', expected_codespace=self.codespace), 0)
        self.assertEqual(calls, [['herdr', 'machine', 'add', 'alias', '--label', 'repo (Codespace)', '--remote-session', session],
                                 ['herdr', 'machine', 'remove', 'old-id']])
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['machine_id'], 'new-id')

    def test_a_codespace_with_another_herdr_version_gets_a_fresh_machine(self):
        # Herdr setup installs the version this Mac runs; a mismatched remote cannot serve it.
        session = 'project-' + 'a' * 24
        self.bind('space', {'machine_id': 'old-id', 'alias': 'alias', 'session': session, 'codespace': 'space'})
        old = {'id': 'old-id', 'target': 'alias', 'session': session, 'enabled': True}
        new = {'id': 'new-id', 'target': 'alias', 'session': session, 'enabled': True}
        calls = []
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr, '_local_version', return_value='herdr 1.3'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([old]), completed([old, new]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=lambda args, **_: calls.append(args) or 0):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', remote_herdr='herdr 1.2', expected_codespace=self.codespace), 0)
        self.assertEqual([args[:3] for args in calls], [['herdr', 'machine', 'add'], ['herdr', 'machine', 'remove']])

    def test_setup_runs_once_for_a_version_pair_it_left_in_place(self):
        # Herdr may keep a compatible remote of another version; one prompt per pair, not per connection.
        session = 'project-' + 'a' * 24
        self.bind('space', {'machine_id': 'new-id', 'alias': 'alias', 'session': session, 'codespace': 'space',
                            'herdr_versions': ['herdr 1.3', 'herdr 1.2']})
        machine = {'id': 'new-id', 'target': 'alias', 'session': session, 'enabled': True}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr, '_local_version', return_value='herdr 1.3'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call, \
                mock.patch('builtins.print') as shown:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', remote_herdr='herdr 1.2', expected_codespace=self.codespace), 0)
        call.assert_not_called()
        self.assertIn('herdr 1.2', ' '.join(str(c.args) for c in shown.call_args_list))
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['herdr_versions'], ['herdr 1.3', 'herdr 1.2'])

    def test_setup_records_the_version_pair_it_ran_for(self):
        session = 'project-' + 'a' * 24
        self.bind('space', {'machine_id': 'old-id', 'alias': 'alias', 'session': session, 'codespace': 'space'})
        old = {'id': 'old-id', 'target': 'alias', 'session': session, 'enabled': True}
        new = {'id': 'new-id', 'target': 'alias', 'session': session, 'enabled': True}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr, '_local_version', return_value='herdr 1.3'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([old]), completed([old, new]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', remote_herdr='herdr 1.2', expected_codespace=self.codespace), 0)
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['herdr_versions'], ['herdr 1.3', 'herdr 1.2'])

    def test_setup_after_an_unread_probe_keeps_the_settled_version_pair(self):
        session = 'project-' + 'a' * 24
        self.bind('new-space', {'machine_id': 'old-id', 'alias': 'alias', 'session': session, 'codespace': 'old-space',
                                'herdr_versions': ['herdr 1.3', 'herdr 1.2']})
        old = {'id': 'old-id', 'target': 'alias', 'session': session, 'enabled': True}
        new = {'id': 'new-id', 'target': 'alias', 'session': session, 'enabled': True}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([old]), completed([old, new]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', remote_herdr=None, expected_codespace=self.codespace), 0)
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['herdr_versions'], ['herdr 1.3', 'herdr 1.2'])

    def test_the_local_version_is_read_like_the_remote_one(self):
        for stdout, code, expected in (('herdr 1.3\n', 0, 'herdr 1.3'), ('herdr 1.3\nA new version is available\n', 0, None),
                                       ('', 0, None), ('1.3\n', 0, None), ('herdr 1.3\n', 1, None)):
            with self.subTest(stdout=stdout, code=code), \
                    mock.patch.object(herdr.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, stdout, '')):
                self.assertEqual(herdr._local_version(), expected)

    def test_an_unreadable_local_version_keeps_a_profile_whose_codespace_has_herdr(self):
        session = 'project-' + 'a' * 24
        self.bind('space', {'machine_id': 'kept-id', 'alias': 'alias', 'session': session, 'codespace': 'space'})
        machine = {'id': 'kept-id', 'target': 'alias', 'session': session, 'enabled': True}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr, '_local_version', return_value=None), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', remote_herdr='herdr 1.2', expected_codespace=self.codespace), 0)
        call.assert_not_called()

    def test_a_codespace_that_has_herdr_keeps_its_profile_even_after_a_move(self):
        session = 'project-' + 'a' * 24
        self.bind('new-space', {'machine_id': 'kept-id', 'alias': 'alias', 'session': session, 'codespace': 'old-space'})
        machine = {'id': 'kept-id', 'target': 'alias', 'session': session, 'enabled': True}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr, '_local_version', return_value='herdr 1.2'), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', remote_herdr='herdr 1.2', expected_codespace=self.codespace), 0)
        call.assert_not_called()
        receipt = read_json(self.state / 'herdr-machine.json')
        self.assertEqual((receipt['machine_id'], receipt['codespace']), ('kept-id', 'new-space'))

    def test_remote_outside_herdr_opens_local_ui_for_the_saved_codespace_group(self):
        self.pair('space', '/remote')
        machine = {'id': 'existing-id', 'target': 'alias', 'session': 'project-' + 'a' * 24, 'enabled': True}
        with mock.patch.dict(os.environ, {'HERDR_ENV': ''}), \
                mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'existing-w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', expected_codespace=self.codespace), 0)
        self.assertEqual(call.call_args.args[0], ['herdr'])
        self.assertEqual(call.call_args.kwargs['cwd'], self.project.root)

    def test_disabled_machine_enable_uses_native_text_output(self):
        self.pair('space', '/remote')
        machine = {'id': 'opaque-disabled', 'target': 'alias', 'session': 'project-' + 'a' * 24, 'enabled': False}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'new'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', expected_codespace=self.codespace), 0)
        self.assertEqual(call.call_args_list[0].args[0], ['herdr', 'machine', 'enable', 'opaque-disabled'])

    def test_cold_remote_api_waits_before_reusing_the_existing_launch_directory(self):
        self.pair('space', '/remote')
        machine = {'id': 'opaque-machine', 'target': 'alias', 'session': 'project-' + 'a' * 24, 'enabled': False}
        rows = {'result': {'workspaces': [{'workspace_id': 'existing', 'focused': True}]}}
        panes = {'result': {'panes': [{'workspace_id': 'existing', 'cwd': '/remote'}]}}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    cold_socket_error(), cold_socket_error('No such file or directory (os error 2)'),
                    completed(rows), completed(panes), completed({'result': {}})]) as run, \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call, \
                mock.patch.object(herdr.clock, 'sleep') as sleep:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', expected_codespace=self.codespace), 0)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(call.call_args_list, [mock.call(['herdr', 'machine', 'enable', 'opaque-machine'])])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands.count(['herdr', '--machine', 'opaque-machine', 'workspace', 'list']), 3)
        self.assertEqual(commands[-1], ['herdr', '--machine', 'opaque-machine', 'workspace', 'focus', 'existing'])
        self.assertFalse(any('create' in command for command in commands))

    def test_cold_api_deadline_fails_without_creating_or_focusing_a_workspace(self):
        with mock.patch.object(herdr.subprocess, 'run', return_value=cold_socket_error()) as run, \
                mock.patch.object(herdr.clock, 'monotonic', side_effect=[100, 100, 100, 146]), \
                mock.patch.object(herdr.clock, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'Herdr.*45 seconds'):
                herdr._workspace(self.project, '/remote', ['--machine', 'opaque-machine'])
        self.assertEqual(len(run.call_args_list), 1)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 15)
        self.assertEqual(run.call_args.args[0][-2:], ['workspace', 'list'])

    def test_remote_read_probe_timeout_can_recover_without_replaying_a_mutation(self):
        rows = {'result': {'workspaces': [{'workspace_id': 'existing', 'worktree': {'checkout_path': '/remote'}}]}}
        with mock.patch.object(herdr.subprocess, 'run', side_effect=[
                subprocess.TimeoutExpired(['herdr'], 15), completed(rows), completed({'result': {}})]) as run, \
                mock.patch.object(herdr.clock, 'sleep'):
            herdr._workspace(self.project, '/remote', ['--machine', 'opaque-machine'])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands.count(['herdr', '--machine', 'opaque-machine', 'workspace', 'focus', 'existing']), 1)

    def test_permanent_remote_errors_fail_immediately_and_keep_native_details_private(self):
        for stderr in ('Host key verification failed. private-detail',
                       'Permission denied (publickey). private-detail',
                       json.dumps({'error': {'code': 'permission_denied', 'message': 'private-detail'}})):
            with self.subTest(stderr=stderr), \
                    mock.patch.object(herdr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', stderr)) as run, \
                    mock.patch.object(herdr.clock, 'sleep') as sleep:
                with self.assertRaisesRegex(ValueError, 'workspace list') as failure:
                    herdr._workspace(self.project, '/remote', ['--machine', 'opaque-machine'])
                self.assertNotIn('private-detail', str(failure.exception))
                self.assertEqual(run.call_count, 1)
                sleep.assert_not_called()

    def test_create_and_focus_are_never_retried_when_the_remote_api_drops(self):
        for operation in ('create', 'focus'):
            rows = [] if operation == 'create' else [
                {'workspace_id': 'existing', 'worktree': {'checkout_path': '/remote'}}]
            with self.subTest(operation=operation), \
                    mock.patch.object(herdr.subprocess, 'run', side_effect=[
                        completed({'result': {'workspaces': rows}}), cold_socket_error()]) as run, \
                    mock.patch.object(herdr.clock, 'sleep') as sleep:
                with self.assertRaisesRegex(ValueError, 'workspace ' + operation):
                    herdr._workspace(self.project, '/remote', ['--machine', 'opaque-machine'])
                self.assertEqual(run.call_count, 2)
                sleep.assert_not_called()

    def test_null_checkout_metadata_and_duplicate_labels_do_not_select_wrong_workspace(self):
        def native(args, **kwargs):
            if args[1:3] == ['workspace', 'list']:
                return completed({'result': {'workspaces': [{'workspace_id': 'wrong', 'label': 'repo', 'worktree': None}]}})
            if args[1:3] == ['pane', 'list']:
                return completed({'result': {'panes': [{'workspace_id': 'wrong', 'cwd': '/other',
                                                      'foreground_cwd': str(self.project.root)}]}})
            if args[1:3] == ['workspace', 'create']:
                return completed({'result': {'workspace': {'workspace_id': 'new'}}})
            return completed({'result': {}})
        with mock.patch.object(herdr.subprocess, 'run', side_effect=native) as run:
            herdr._workspace(self.project, str(self.project.root), [])
        self.assertEqual(run.call_args_list[-1].args[0], ['herdr', 'workspace', 'focus', 'new'])

    def test_native_workspace_without_worktree_metadata_reuses_its_launch_directory(self):
        rows = {'result': {'workspaces': [
            {'workspace_id': 'existing', 'label': 'repo', 'focused': True}]}}
        panes = {'result': {'panes': [{'workspace_id': 'existing',
                  'cwd': str(self.project.root), 'foreground_cwd': str(self.project.root / 'src')}]}}
        def native(args, **kwargs):
            if args[1:3] == ['workspace', 'list']: return completed(rows)
            if args[1:3] == ['pane', 'list']: return completed(panes)
            if args[1:3] == ['workspace', 'create']:
                return completed({'result': {'workspace': {'workspace_id': 'duplicate'}}})
            return completed({'result': {}})
        with mock.patch.object(herdr.subprocess, 'run', side_effect=native) as run:
            herdr._workspace(self.project, str(self.project.root), [])
        self.assertEqual(run.call_args_list[1].args[0], ['herdr', 'pane', 'list', '--workspace', 'existing'])
        self.assertEqual(run.call_args_list[-1].args[0], ['herdr', 'workspace', 'focus', 'existing'])
        self.assertFalse(any('create' in call.args[0] for call in run.call_args_list))


    def remote_with_home_workspace(self, home_pane):
        rows = {'result': {'workspaces': [{'workspace_id': 'w1', 'label': '~', 'worktree': None,
                                           'tab_count': 1, 'pane_count': 1}]}}
        def native(args, **kwargs):
            if args[3:5] == ['workspace', 'list']: return completed(rows)
            if args[3:5] == ['pane', 'list']: return completed({'result': {'panes': [home_pane]}})
            if args[3:5] == ['workspace', 'create']:
                return completed({'result': {'workspace': {'workspace_id': 'w2'}}})
            return completed({'result': {}})
        with mock.patch.object(herdr.subprocess, 'run', side_effect=native) as run:
            herdr._workspace(self.project, '/workspaces/repo', ['--machine', 'opaque-machine'])
        return [call.args[0] for call in run.call_args_list]

    def test_codespace_default_home_workspace_is_closed_when_idle(self):
        calls = self.remote_with_home_workspace({'workspace_id': 'w1', 'cwd': '/home/vscode',
                                                 'agent': None, 'foreground_process': None})
        self.assertIn(['herdr', '--machine', 'opaque-machine', 'workspace', 'close', 'w1'], calls)
        self.assertEqual(calls[-1], ['herdr', '--machine', 'opaque-machine', 'workspace', 'focus', 'w2'])

    def test_codespace_home_workspace_in_use_is_kept(self):
        for busy in ({'agent': 'claude'}, {'foreground_process': 'vim'}):
            with self.subTest(busy=busy):
                calls = self.remote_with_home_workspace(dict({'workspace_id': 'w1', 'cwd': '/home/vscode',
                                                              'agent': None, 'foreground_process': None}, **busy))
                self.assertFalse(any('close' in call for call in calls))

    def test_prepared_runs_once_after_the_receipt_and_before_the_workspace(self):
        self.pair('space', '/remote')
        session = 'project-' + 'a' * 24
        machine = {'id': 'created-id', 'target': 'alias', 'session': session, 'enabled': False}
        order = []

        def call(args, **kwargs):
            order.append(tuple(args[:3]))
            return 0

        def prepared():
            order.append('prepared')
            with herdr.locked(self.state / 'workspace-control.lock'):
                pass  # The callback runs after setup releases its non-reentrant lock.
            self.assertEqual(read_json(self.state / 'herdr-machine.json')['machine_id'], 'created-id')

        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([]), completed([machine])]), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=call), \
                mock.patch.object(herdr, '_workspace', side_effect=lambda *a, **k: order.append('workspace')):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote', prepared=prepared, expected_codespace=self.codespace), 0)
        self.assertEqual(order, [('herdr', 'machine', 'add'), ('herdr', 'machine', 'enable'), 'prepared', 'workspace'])
        self.assertEqual(order.count('prepared'), 1)

    def test_prepared_is_not_called_when_machine_add_fails(self):
        self.pair('space', '/remote')
        called = []
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', return_value=completed([])), \
                mock.patch.object(herdr.subprocess, 'call', return_value=1), \
                mock.patch.object(herdr, '_workspace', side_effect=AssertionError('workspace opened')):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote',
                                               prepared=lambda: called.append('prepared'), expected_codespace=self.codespace), 1)
        self.assertEqual(called, [])
        self.assertFalse((self.state / 'herdr-machine.json').exists())

    def test_prepared_runs_when_the_machine_was_already_saved(self):
        self.pair('space', '/remote')
        session = 'project-' + 'a' * 24
        machine = {'id': 'kept-id', 'target': 'alias', 'session': session, 'enabled': True}
        order = []
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', return_value=completed([machine])), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=AssertionError('setup')) as call, \
                mock.patch.object(herdr, '_workspace', side_effect=lambda *a, **k: order.append('workspace')):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote',
                                               prepared=lambda: order.append('prepared'), expected_codespace=self.codespace), 0)
        call.assert_not_called()
        self.assertEqual(order, ['prepared', 'workspace'])

    def test_prepared_is_not_called_when_machine_enable_fails(self):
        self.pair('space', '/remote')
        session = 'project-' + 'a' * 24
        machine = {'id': 'kept-id', 'target': 'alias', 'session': session, 'enabled': False}
        called = []
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', return_value=completed([machine])), \
                mock.patch.object(herdr.subprocess, 'call', return_value=1), \
                mock.patch.object(herdr, '_workspace', side_effect=AssertionError('workspace opened')):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote',
                                               prepared=lambda: called.append('prepared'), expected_codespace=self.codespace), 1)
        self.assertEqual(called, [])

    def test_prepared_is_not_called_when_machine_remove_fails(self):
        session = 'project-' + 'a' * 24
        self.bind('new-space', {'machine_id': 'old-id', 'alias': 'alias', 'session': session, 'codespace': 'old-space'})
        old = {'id': 'old-id', 'target': 'alias', 'session': session, 'enabled': True}
        new = {'id': 'new-id', 'target': 'alias', 'session': session, 'enabled': True}
        calls = []

        def call(args, **kwargs):
            calls.append(args[:3])
            return 1 if args[1:3] == ['machine', 'remove'] else 0

        called = []
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([old]), completed([old, new])]), \
                mock.patch.object(herdr.subprocess, 'call', side_effect=call), \
                mock.patch.object(herdr, '_workspace', side_effect=AssertionError('workspace opened')):
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote',
                                               prepared=lambda: called.append('prepared'), expected_codespace=self.codespace), 1)
        self.assertEqual(called, [])
        self.assertEqual(calls, [['herdr', 'machine', 'add'], ['herdr', 'machine', 'remove']])
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['codespace'], 'old-space')

    def test_local_home_workspace_is_never_closed(self):
        rows = {'result': {'workspaces': [{'workspace_id': 'w1', 'label': '~', 'worktree': None,
                                           'tab_count': 1, 'pane_count': 1}]}}
        def native(args, **kwargs):
            if args[1:3] == ['workspace', 'list']: return completed(rows)
            if args[1:3] == ['pane', 'list']:
                return completed({'result': {'panes': [{'workspace_id': 'w1', 'cwd': str(self.home)}]}})
            if args[1:3] == ['workspace', 'create']:
                return completed({'result': {'workspace': {'workspace_id': 'w2'}}})
            return completed({'result': {}})
        with mock.patch.object(herdr.subprocess, 'run', side_effect=native) as run:
            herdr._workspace(self.project, str(self.project.root), [])
        self.assertFalse(any('close' in call.args[0] for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
