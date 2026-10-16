import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from devcontainer_setup.codespaces import Codespaces


MACHINE = {'name': 'standardLinux32gb', 'display_name': '4 cores, 16 GB RAM',
           'cpus': 4, 'memory_in_bytes': 17179869184, 'storage_in_bytes': 34359738368}


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.fail_personalization = False
        self.remote_sha = 'a' * 40

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        output = ''
        if args[1:3] == ['codespace', 'create']:
            output = 'new-space\n'
        elif args[1] == 'api':
            endpoint = args[2]
            if endpoint.endswith('/machines'):
                output = json.dumps({'machines': [MACHINE], 'total_count': 1})
            elif endpoint.startswith('user/codespaces/new-space'):
                output = json.dumps({'name': 'new-space', 'state': 'Available',
                    'repository': {'full_name': 'owner/project'}, 'git_status': {'ref': 'setup/devcontainer'}})
        if args[1:3] == ['codespace', 'ssh']:
            if args[-1].endswith('git rev-parse HEAD'):
                output = self.remote_sha + '\n'
            if 'git merge --ff-only' in args[-1]:
                self.remote_sha = 'a' * 40
            if self.fail_personalization:
                return subprocess.CompletedProcess(args, 1, '', 'private dotfiles access denied')
        return subprocess.CompletedProcess(args, 0, output, '')


class CodespacesTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeGitHub()
        self.api = Codespaces(run=self.runner)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.receipt = Path(self.tmp.name) / 'launch.json'

    def test_machine_choices_come_from_repository_api_with_ref(self):
        self.assertEqual(self.api.machines('owner/project', 'feature/test'), [MACHINE])
        args = self.runner.calls[0]
        self.assertIn('repos/owner/project/codespaces/machines', args)
        self.assertIn('ref=feature/test', args)
        self.assertIn('GET', args)

    def test_launch_pushes_before_allocation_and_opens_selected_client(self):
        calls = self.runner.calls
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                calls.append(['push-verified'])
                return 'a' * 40
        name = self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                               'emsilva/dotfiles', self.receipt)
        self.assertEqual(name, 'new-space')
        self.assertEqual(calls[0], ['push-verified'])
        create = next(c for c in calls if c[1:3] == ['codespace', 'create'])
        self.assertIn('setup/devcontainer', create)
        self.assertIn(MACHINE['name'], create)
        self.assertNotIn('--default-permissions', create)
        self.assertNotIn('--status', create)
        self.assertEqual(calls[-1], ['gh', 'codespace', 'code', '--codespace', 'new-space', '--web'])
        self.assertEqual(json.loads(self.receipt.read_text())['codespace'], 'new-space')

    def test_personalization_quotes_repo_as_one_remote_argument(self):
        repo = "owner/dotfiles'; touch /tmp/should-not-exist; '"
        self.api.personalize('new-space', repo)
        remote = self.runner.calls[-1][-1]
        self.assertEqual(shlex.split(remote), ['load-dotfiles', repo])

    def test_failed_personalization_keeps_receipt_and_includes_resume(self):
        self.runner.fail_personalization = True
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        with self.assertRaisesRegex(ValueError, '--resume new-space'):
            self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                            'emsilva/dotfiles', self.receipt)
        self.assertTrue(self.receipt.exists())
        self.assertFalse(any('delete' in c for c in self.runner.calls))

    def test_unavailable_selected_machine_never_creates(self):
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        with self.assertRaisesRegex(ValueError, 'machine'):
            self.api.launch(Plan(), 'owner/project', 'no-longer-available', '30m', 'browser',
                            None, self.receipt)
        self.assertFalse(any(c[1:3] == ['codespace', 'create'] for c in self.runner.calls))

    def test_push_failure_never_creates(self):
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                raise ValueError('diverged')
        with self.assertRaisesRegex(ValueError, 'diverged'):
            self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                            None, self.receipt)
        self.assertEqual(self.runner.calls, [])

    def test_reuse_uses_supported_rebuild_command_and_never_creates(self):
        self.runner.remote_sha = 'b' * 40
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        self.assertIn(['gh', 'codespace', 'rebuild', '--codespace', 'new-space'], self.runner.calls)
        self.assertFalse(any(c[1:3] == ['codespace', 'create'] for c in self.runner.calls))

    def test_retry_of_current_codespace_skips_rebuild(self):
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        self.assertFalse(any(c[1:3] == ['codespace', 'rebuild'] for c in self.runner.calls))

    def test_remote_revision_must_equal_reviewed_commit_before_opening(self):
        self.runner.remote_sha = 'b' * 40
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        with self.assertRaisesRegex(ValueError, 'reviewed commit'):
            self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                            None, self.receipt)
        self.assertFalse(any(c[1:3] == ['codespace', 'code'] for c in self.runner.calls))
        self.assertTrue(self.receipt.exists())


if __name__ == '__main__':
    unittest.main()
