import json
import os
from pathlib import Path
import subprocess
import tempfile
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
            self.assertEqual(herdr.open_remote(self.project, self.state, '/workspaces/recovered'), 0)
        self.assertEqual(run.call_args_list[1].args[0][:3], ['herdr', '--machine', 'opaque-machine'])
        call.assert_not_called()
        self.assertFalse((self.state / 'herdr-nested.toml').exists())
        self.assertEqual(config.read_text(), original)
        self.assertNotIn('HERDR_CONFIG_PATH', os.environ)
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['machine_id'], 'opaque-machine')

    def test_missing_machine_uses_official_setup_then_reads_generated_id(self):
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
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote'), 0)
        self.assertEqual(calls[0], ['herdr', 'machine', 'add', 'alias', '--label', 'repo (Codespace)',
                                  '--remote-session', 'project-' + 'a' * 24])
        self.assertEqual(len(calls), 1)

    def bind(self, codespace, receipt):
        from devcontainer_setup.project_sync.state import atomic_write
        atomic_write(self.state / 'scratch.json', json.dumps({'codespace': codespace}).encode())
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
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote'), 0)
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
                herdr.open_remote(self.project, self.state, '/remote')
        self.assertEqual([args[:3] for args in calls], [['herdr', 'machine', 'add']])
        self.assertEqual(read_json(self.state / 'herdr-machine.json')['codespace'], 'old-space')

    def test_a_missing_binding_never_counts_as_a_replaced_codespace(self):
        from devcontainer_setup.project_sync.state import atomic_write
        session = 'project-' + 'a' * 24
        atomic_write(self.state / 'herdr-machine.json', json.dumps(
            {'machine_id': 'kept-id', 'alias': 'alias', 'session': session, 'codespace': 'old-space'}).encode())
        machine = {'id': 'kept-id', 'target': 'alias', 'session': session, 'enabled': True}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote'), 0)
        call.assert_not_called()

    def test_a_receipt_from_before_codespace_tracking_adopts_the_current_machine(self):
        session = 'project-' + 'a' * 24
        self.bind('current-space', {'machine_id': 'kept-id', 'alias': 'alias', 'session': session})
        machine = {'id': 'kept-id', 'target': 'alias', 'session': session, 'enabled': True}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote'), 0)
        call.assert_not_called()
        receipt = read_json(self.state / 'herdr-machine.json')
        self.assertEqual((receipt['machine_id'], receipt['codespace']), ('kept-id', 'current-space'))

    def test_remote_outside_herdr_opens_local_ui_for_the_saved_codespace_group(self):
        machine = {'id': 'existing-id', 'target': 'alias', 'session': 'project-' + 'a' * 24, 'enabled': True}
        with mock.patch.dict(os.environ, {'HERDR_ENV': ''}), \
                mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'existing-w'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote'), 0)
        self.assertEqual(call.call_args.args[0], ['herdr'])
        self.assertEqual(call.call_args.kwargs['cwd'], self.project.root)

    def test_disabled_machine_enable_uses_native_text_output(self):
        machine = {'id': 'opaque-disabled', 'target': 'alias', 'session': 'project-' + 'a' * 24, 'enabled': False}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'new'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote'), 0)
        self.assertEqual(call.call_args_list[0].args[0], ['herdr', 'machine', 'enable', 'opaque-disabled'])

    def test_cold_remote_api_waits_before_reusing_the_existing_launch_directory(self):
        machine = {'id': 'opaque-machine', 'target': 'alias', 'session': 'project-' + 'a' * 24, 'enabled': False}
        rows = {'result': {'workspaces': [{'workspace_id': 'existing', 'focused': True}]}}
        panes = {'result': {'panes': [{'workspace_id': 'existing', 'cwd': '/remote'}]}}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    cold_socket_error(), cold_socket_error('No such file or directory (os error 2)'),
                    completed(rows), completed(panes), completed({'result': {}})]) as run, \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call, \
                mock.patch.object(herdr.clock, 'sleep') as sleep:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote'), 0)
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
