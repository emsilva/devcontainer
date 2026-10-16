import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from devcontainer_setup.codespaces import Codespaces


MACHINE = {'name': 'standardLinux32gb', 'display_name': '4 cores, 16 GB RAM',
           'cpus': 4, 'memory_in_bytes': 17179869184, 'storage_in_bytes': 34359738368}


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.fail_personalization = False
        self.remote_sha = 'a' * 40
        self.ssh_failures = []
        self.fail_validation = False
        self.remote_dirty = ''
        self.remote_branch = 'setup/devcontainer'
        self.api_dirty = False
        self.ssh_timeouts = []
        self.fail_rebuild_once = False
        self.container_generation = 100
        self.identity_sequence = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        output = ''
        if args[1:3] == ['codespace', 'create']:
            output = 'new-space\n'
        elif args[1:3] == ['codespace', 'rebuild']:
            if self.fail_rebuild_once:
                self.fail_rebuild_once = False
                return subprocess.CompletedProcess(args, 1, '', 'temporary rebuild API failure')
            self.container_generation += 1
        elif args[1] == 'api':
            endpoint = args[2]
            if endpoint.endswith('/machines'):
                output = json.dumps({'machines': [MACHINE], 'total_count': 1})
            elif endpoint.startswith('user/codespaces/new-space'):
                output = json.dumps({'name': 'new-space', 'state': 'Available',
                    'repository': {'full_name': 'owner/project'}, 'git_status': {'ref': 'setup/devcontainer',
                    'has_uncommitted_changes': self.api_dirty, 'has_unpushed_changes': self.api_dirty}})
        if args[1:3] == ['codespace', 'ssh']:
            self.ssh_timeouts.append(kwargs.get('timeout'))
            if self.ssh_failures:
                failure = self.ssh_failures.pop(0)
                if isinstance(failure, Exception):
                    raise failure
                return subprocess.CompletedProcess(args, 255, '', failure)
            if args[-1].endswith('git rev-parse HEAD'):
                output = self.remote_sha + '\n'
            elif args[-1].endswith('git branch --show-current'):
                output = self.remote_branch + '\n'
            elif args[-1].endswith('git status --porcelain --untracked-files=all'):
                output = self.remote_dirty
            elif '/proc/1/stat' in args[-1]:
                generation = self.identity_sequence.pop(0) if self.identity_sequence else self.container_generation
                output = f'11111111-1111-1111-1111-111111111111:{generation}'
            if 'git merge --ff-only' in args[-1]:
                self.remote_sha = 'a' * 40
            if self.fail_personalization and args[-1].startswith('load-dotfiles '):
                return subprocess.CompletedProcess(args, 1, '', 'private dotfiles access denied')
            if self.fail_validation and 'scripts/validate-selected.sh' in args[-1]:
                return subprocess.CompletedProcess(args, 1, '', 'Missing selected tool: uv')
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

    def launch(self):
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        return self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                               None, self.receipt)

    def test_available_codespace_retries_transient_ssh_tunnel_failure(self):
        self.runner.ssh_failures = ['kex_exchange_identification: read: Connection reset by peer\n'
                                    'tunnel closed: error forwarding port: unexpected EOF']
        with patch('devcontainer_setup.codespaces.time.sleep'):
            self.assertEqual(self.launch(), 'new-space')
        self.assertEqual(sum(call[1:3] == ['codespace', 'create'] for call in self.runner.calls), 1)
        self.assertEqual(self.runner.calls[-1][1:3], ['codespace', 'code'])

    def test_stalled_ssh_probe_has_timeout_and_retries(self):
        self.runner.ssh_failures = [subprocess.TimeoutExpired(['gh', 'codespace', 'ssh'], 20)]
        with patch('devcontainer_setup.codespaces.time.sleep'):
            self.assertEqual(self.launch(), 'new-space')
        self.assertTrue(all(timeout and 0 < timeout <= 30 for timeout in self.runner.ssh_timeouts))

    def test_persistent_ssh_failure_stops_and_keeps_same_codespace_receipt(self):
        self.runner.ssh_failures = ['tunnel closed: error forwarding port: unexpected EOF'] * 10
        with patch('devcontainer_setup.codespaces.time.sleep'):
            with self.assertRaisesRegex(ValueError, '--resume new-space'):
                self.launch()
        ssh_calls = [call for call in self.runner.calls if call[1:3] == ['codespace', 'ssh']]
        self.assertEqual(len(ssh_calls), 4)
        self.assertEqual(json.loads(self.receipt.read_text())['codespace'], 'new-space')
        self.assertEqual(sum(call[1:3] == ['codespace', 'create'] for call in self.runner.calls), 1)
        self.assertFalse(any(call[1:3] == ['codespace', 'code'] for call in self.runner.calls))

    def test_tool_validation_failure_is_not_retried_as_transport_failure(self):
        self.runner.fail_validation = True
        with patch('devcontainer_setup.codespaces.time.sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'Missing selected tool: uv'):
                self.launch()
        sleep.assert_not_called()

    def test_ssh_permission_failure_is_not_retried(self):
        self.runner.ssh_failures = ['vscode@localhost: Permission denied (publickey).']
        with patch('devcontainer_setup.codespaces.time.sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'Permission denied'):
                self.launch()
        sleep.assert_not_called()

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
        machine_request = next(c for c in calls if c[:2] == ['gh', 'api'] and c[2].endswith('/machines'))
        self.assertIn('ref=' + 'a' * 40, machine_request)
        self.assertFalse(any(arg.startswith('devcontainer_path=') for arg in machine_request))
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

    def test_resume_checks_live_git_instead_of_stale_api_dirty_flags(self):
        self.runner.api_dirty = True
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        self.assertEqual(self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                         None, self.receipt, resume='new-space'), 'new-space')

    def test_failed_rebuild_is_retried_even_after_git_already_advanced(self):
        self.runner.remote_sha = 'b' * 40
        self.runner.fail_rebuild_once = True
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        with self.assertRaisesRegex(ValueError, 'temporary rebuild API failure'):
            self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                            None, self.receipt, resume='new-space')
        self.assertTrue(json.loads(self.receipt.read_text()).get('rebuild_pending'))
        self.assertEqual(self.runner.remote_sha, 'a' * 40)
        self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        self.assertEqual(sum(c[1:3] == ['codespace', 'rebuild'] for c in self.runner.calls), 2)
        self.assertFalse(json.loads(self.receipt.read_text())['rebuild_pending'])

    def test_rebuild_waits_for_new_container_when_api_still_says_available(self):
        self.runner.remote_sha = 'b' * 40
        self.runner.identity_sequence = [100, 100, 101]
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        with patch('devcontainer_setup.codespaces.time.sleep'):
            self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                            None, self.receipt, resume='new-space')
        self.assertEqual(self.runner.identity_sequence, [])

    def test_resume_preserves_actual_remote_changes_even_when_api_says_clean(self):
        self.runner.remote_dirty = ' M source.py\n'
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        with self.assertRaisesRegex(ValueError, 'source.py'):
            self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                            None, self.receipt, resume='new-space')
        self.assertFalse(any('git fetch' in call[-1] for call in self.runner.calls))
        self.assertFalse(any(call[1:3] == ['codespace', 'rebuild'] for call in self.runner.calls))

    def test_resume_preserves_remote_branch_when_api_ref_is_stale(self):
        self.runner.remote_branch = 'user-work'
        class Plan:
            branch = 'setup/devcontainer'
            def push(self):
                return 'a' * 40
        with self.assertRaisesRegex(ValueError, 'branch'):
            self.api.launch(Plan(), 'owner/project', MACHINE['name'], '30m', 'browser',
                            None, self.receipt, resume='new-space')
        self.assertFalse(any('git fetch' in call[-1] for call in self.runner.calls))

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
