import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

from devcontainer_setup.project_sync.state import Project, read_json
from devcontainer_setup.project_sync import herdr


def completed(value):
    return subprocess.CompletedProcess([], 0, json.dumps(value), '')


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

    def test_local_reuses_actual_checkout_and_focuses_opaque_workspace(self):
        rows = {'result': {'workspaces': [
            {'workspace_id': 'opaque-other', 'label': 'repo', 'worktree': {'checkout_path': '/other'}},
            {'workspace_id': 'opaque-real', 'label': 'anything', 'worktree': {'checkout_path': str(self.project.root)}}]}}
        with mock.patch.object(herdr.subprocess, 'run', side_effect=[completed(rows), completed({'result': {}})]) as run:
            self.assertEqual(herdr.open_local(self.project, self.state), 0)
        self.assertEqual(run.call_args_list[-1].args[0], ['herdr', 'workspace', 'focus', 'opaque-real'])
        self.assertFalse(any('create' in call.args[0] for call in run.call_args_list))

    def test_local_creates_in_current_herdr_or_attaches_named_session_outside(self):
        with mock.patch.object(herdr.subprocess, 'run', side_effect=[
            completed({'result': {'workspaces': []}}),
            completed({'result': {'workspace': {'workspace_id': 'new-opaque'}}}),
            completed({'result': {}}),
        ]) as run:
            self.assertEqual(herdr.open_local(self.project, self.state), 0)
        create = run.call_args_list[1].args[0]
        self.assertIn(str(self.project.root), create)
        self.assertIn('--no-focus', create)
        with mock.patch.dict(os.environ, {'HERDR_ENV': ''}), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[
                    completed({'result': {'workspaces': [{'workspace_id': 'outside',
                        'worktree': {'checkout_path': str(self.project.root)}}]}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_local(self.project, self.state), 0)
        self.assertEqual(call.call_args.kwargs['cwd'], self.project.root)
        self.assertIn('--session', call.call_args.args[0])

    def test_outside_existing_session_focuses_recovery_checkout_before_attach(self):
        with mock.patch.dict(os.environ, {'HERDR_ENV': ''}), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[
                    completed({'result': {'workspaces': [{'workspace_id': 'old-root',
                        'worktree': {'checkout_path': '/old/local/checkout'}}]}}),
                    completed({'result': {'workspace': {'workspace_id': 'recovery-root'}}}),
                    completed({'result': {}})]) as run, \
                mock.patch.object(herdr.subprocess, 'Popen') as popen, \
                mock.patch.object(herdr.subprocess, 'call', return_value=0):
            self.assertEqual(herdr.open_local(self.project, self.state), 0)
        prefix = ['herdr', '--session', 'project-' + 'a' * 24]
        self.assertEqual(run.call_args_list[0].args[0], prefix + ['workspace', 'list'])
        self.assertEqual(run.call_args_list[-1].args[0], prefix + ['workspace', 'focus', 'recovery-root'])
        popen.assert_not_called()

    def test_outside_stopped_session_starts_detached_then_focuses_actual_root(self):
        stopped = subprocess.CompletedProcess([], 1, '', json.dumps({'error': {'code': 'server_not_running'}}))
        with mock.patch.dict(os.environ, {'HERDR_ENV': ''}), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[stopped,
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'recovered'}}}), completed({'result': {}})]) as run, \
                mock.patch.object(herdr.subprocess, 'Popen') as popen, \
                mock.patch.object(herdr.subprocess, 'call', return_value=0):
            self.assertEqual(herdr.open_local(self.project, self.state), 0)
        self.assertEqual(popen.call_args.args[0], ['herdr', '--session', 'project-' + 'a' * 24, 'server'])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(popen.call_args.kwargs['cwd'], self.project.root)
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(run.call_args_list[-1].args[0][-2:], ['focus', 'recovered'])

    def test_outside_protocol_failure_does_not_start_another_server(self):
        broken = subprocess.CompletedProcess([], 1, '', json.dumps({'error': {'code': 'protocol_mismatch'}}))
        with mock.patch.dict(os.environ, {'HERDR_ENV': ''}), \
                mock.patch.object(herdr.subprocess, 'run', return_value=broken), \
                mock.patch.object(herdr.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'Herdr command failed'):
                herdr.open_local(self.project, self.state)
        popen.assert_not_called()

    def test_remote_reuses_exact_tuple_and_checkout_then_nested_copy_preserves_settings(self):
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
        self.assertEqual(call.call_args.args[0], ['herdr', '--remote', 'alias', '--session', 'project-' + 'a' * 24])
        child = call.call_args.kwargs['env']
        self.assertEqual(child['HERDR_ENV'], '1')
        self.assertEqual(child['HOME'], str(self.home))
        nested = tomllib.loads(Path(child['HERDR_CONFIG_PATH']).read_text())
        self.assertTrue(nested['experimental']['allow_nested'])
        self.assertEqual(nested['experimental']['other'], 'preserve')
        self.assertTrue(nested['session']['resume_agents_on_restore'])
        self.assertEqual(nested['ui']['sound']['path'], str(config.parent / 'sounds/bell.wav'))
        self.assertEqual(nested['ui']['sound']['done_path'], '/absolute/done.wav')
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
        self.assertIn('--remote', calls[-1])

    def test_disabled_machine_enable_uses_native_text_output(self):
        machine = {'id': 'opaque-disabled', 'target': 'alias', 'session': 'project-' + 'a' * 24, 'enabled': False}
        with mock.patch.object(herdr, 'configure_ssh', return_value='alias'), \
                mock.patch.object(herdr.subprocess, 'run', side_effect=[completed([machine]),
                    completed({'result': {'workspaces': []}}),
                    completed({'result': {'workspace': {'workspace_id': 'new'}}}), completed({'result': {}})]), \
                mock.patch.object(herdr.subprocess, 'call', return_value=0) as call:
            self.assertEqual(herdr.open_remote(self.project, self.state, '/remote'), 0)
        self.assertEqual(call.call_args_list[0].args[0], ['herdr', 'machine', 'enable', 'opaque-disabled'])

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
            self.assertEqual(herdr.open_local(self.project, self.state), 0)
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
            self.assertEqual(herdr.open_local(self.project, self.state), 0)
        self.assertEqual(run.call_args_list[1].args[0], ['herdr', 'pane', 'list', '--workspace', 'existing'])
        self.assertEqual(run.call_args_list[-1].args[0], ['herdr', 'workspace', 'focus', 'existing'])
        self.assertFalse(any('create' in call.args[0] for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
