import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from devcontainer_setup.codespaces import Codespaces, migrate_receipt, receipt_path


MACHINE = {'name': 'standardLinux32gb', 'display_name': '4 cores, 16 GB RAM',
           'cpus': 4, 'memory_in_bytes': 17179869184, 'storage_in_bytes': 34359738368}


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.inputs = []
        self.resource = {"machine": dict(MACHINE), "idle_timeout_minutes": 30,
                         "retention_period_minutes": 10080, "location": "EastUs"}
        self.fail_personalization = False
        self.remote_sha = 'a' * 40
        self.ssh_failures = []
        self.fail_validation = False
        self.remote_dirty = ''
        self.remote_branch = 'main'
        self.api_ref = 'main'
        self.descendants = set()
        self.api_dirty = False
        self.ssh_timeouts = []
        self.fail_rebuild_once = False
        self.container_generation = 100
        self.identity_sequence = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        self.inputs.append(kwargs.get('input'))
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
                    **self.resource, 'repository': {'full_name': 'owner/project'}, 'git_status': {'ref': self.api_ref,
                    'has_uncommitted_changes': self.api_dirty, 'has_unpushed_changes': self.api_dirty}})
        if args[1:3] == ['codespace', 'ssh']:
            self.ssh_timeouts.append(kwargs.get('timeout'))
            if self.ssh_failures:
                failure = self.ssh_failures.pop(0)
                if isinstance(failure, Exception):
                    raise failure
                return subprocess.CompletedProcess(args, 255, '', failure)
            contains = re.search(r'git merge-base --is-ancestor (\S+) HEAD; then', args[-1])
            if contains:
                target = contains[1]
                output = 'yes\n' if self.remote_sha == target or self.remote_sha in self.descendants else 'no\n'
            elif args[-1].endswith('git rev-parse HEAD'):
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
            if self.fail_personalization and 'load-dotfiles ' in args[-1]:
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
        return self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
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

    def test_launch_reports_a_new_codespace_and_the_time_before_create(self):
        # #61: setup records the Mac's time just before gh codespace create.
        import time
        seen, created_at = [], []
        real = self.runner

        def runner(args, **kwargs):
            if args[1:3] == ['codespace', 'create']:
                created_at.append(time.time())
            return real(args, **kwargs)

        self.api.run = runner
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser', None, self.receipt,
                        on_created=lambda name, at: seen.append((name, at)))
        self.assertEqual([name for name, _at in seen], ['new-space'])
        self.assertLessEqual(seen[0][1], created_at[0])

    def test_launch_does_not_report_a_resumed_codespace(self):
        # #61: a reused Codespace is not a create, so it reports nothing.
        seen = []
        self.resume(on_created=lambda name, at: seen.append((name, at)))
        self.assertEqual(seen, [])
        self.assertFalse(any(call[1:3] == ['codespace', 'create'] for call in self.runner.calls))

    def test_a_failing_created_callback_still_leaves_the_launch_receipt(self):
        # #61: a failing created callback cannot leave a billed Codespace without its receipt.
        def fail(name, at):
            raise ValueError('project state cannot be found')

        with self.assertRaises(ValueError):
            self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser', None, self.receipt,
                            on_created=fail)
        self.assertTrue(self.receipt.exists())
        self.assertEqual(json.loads(self.receipt.read_text())['codespace'], 'new-space')

    def test_codespace_created_on_default_branch_at_merge_sha(self):
        # Another PR merged after the setup merge: main's head contains the merge commit, not equals it.
        self.runner.remote_sha = 'f' * 40
        self.runner.descendants = {'f' * 40}
        calls = self.runner.calls
        name = self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                               'emsilva/dotfiles', self.receipt)
        self.assertEqual(name, 'new-space')
        machine_request = next(c for c in calls if c[:2] == ['gh', 'api'] and c[2].endswith('/machines'))
        self.assertIn('ref=' + 'a' * 40, machine_request)
        self.assertFalse(any(arg.startswith('devcontainer_path=') for arg in machine_request))
        create = next(c for c in calls if c[1:3] == ['codespace', 'create'])
        self.assertEqual(create[create.index('--branch') + 1], 'main')
        self.assertIn(MACHINE['name'], create)
        self.assertNotIn('--default-permissions', create)
        self.assertNotIn('--status', create)
        self.assertFalse(any('push' in arg for call in calls for arg in call))
        self.assertEqual(calls[-1], ['gh', 'codespace', 'code', '--codespace', 'new-space', '--web'])
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual((receipt['codespace'], receipt['branch'], receipt['commit'], receipt['validated_commit']),
                         ('new-space', 'main', 'a' * 40, 'a' * 40))

    def test_reuse_refuses_codespace_on_other_branch(self):
        self.runner.api_ref = 'setup/devcontainer'
        with self.assertRaises(ValueError) as caught:
            self.resume()
        self.assertEqual(str(caught.exception), 'Move new-space to main first; it is on setup/devcontainer.')
        self.assertFalse(any(c[1] == 'codespace' for c in self.runner.calls))
        self.assertFalse(self.receipt.exists())

    def test_reuse_migrates_a_receipt_only_once_confirmed_on_default(self):
        state = Path(self.tmp.name) / 'state'
        values = {'codespace': 'new-space', 'repository': 'owner/project', 'branch': 'setup/devcontainer',
                  'commit': 'a' * 40, 'validated_commit': 'a' * 40, 'rebuild_pending': False}
        self.api._save_receipt(receipt_path(state, 'owner/project', 'setup/devcontainer'), values)
        target = receipt_path(state, 'owner/project', 'main')
        self.runner.api_ref = 'setup/devcontainer'
        with self.assertRaisesRegex(ValueError, 'Move new-space to main first'):
            self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                            None, target, resume='new-space', state_root=state)
        self.assertFalse(target.exists())
        self.runner.api_ref = 'main'
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                        None, target, resume='new-space', state_root=state)
        self.assertEqual(json.loads(target.read_text())['branch'], 'main')
        # The migrated receipt certified the image: no rebuild.
        self.assertFalse(any(c[1:3] == ['codespace', 'rebuild'] for c in self.runner.calls))

    def test_receipt_migrates_from_retired_setup_branch(self):
        state = Path(self.tmp.name) / 'state'
        old = receipt_path(state, 'owner/project', 'setup/devcontainer')
        digest = hashlib.sha256(b'owner/project:setup/devcontainer').hexdigest()[:16]
        self.assertEqual(old, state / 'devcontainer-setup' / f'{digest}.json')
        values = {'codespace': 'new-space', 'repository': 'owner/project', 'branch': 'setup/devcontainer',
                  'commit': 'c' * 40, 'validated_commit': 'c' * 40, 'rebuild_pending': False}
        self.api._save_receipt(old, values)
        # Neither another codespace's receipt nor another repository's is copied.
        self.api._save_receipt(receipt_path(state, 'owner/project', 'setup/devcontainer-2'),
                               dict(values, codespace='other-space', branch='setup/devcontainer-2'))
        self.api._save_receipt(receipt_path(state, 'owner/other', 'setup/devcontainer'),
                               dict(values, repository='owner/other', commit='d' * 40))
        self.assertIsNone(migrate_receipt(state, 'owner/project', 'main', 'missing-space'))
        new = migrate_receipt(state, 'owner/project', 'main', 'new-space')
        self.assertEqual(new, receipt_path(state, 'owner/project', 'main'))
        self.assertEqual(json.loads(new.read_text()), dict(values, branch='main'))
        self.assertEqual(json.loads(old.read_text()), values)  # Kept beside it.
        self.assertIsNone(migrate_receipt(state, 'owner/project', 'main', 'new-space'))  # Never overwritten.
        # The rewritten branch is what lets reuse trust the validated image: no rebuild.
        self.runner.remote_sha = 'c' * 40
        self.api.launch('owner/project', 'main', 'c' * 40, MACHINE['name'], '30m', 'browser',
                        None, new, resume='new-space')
        self.assertFalse(any(c[1:3] == ['codespace', 'rebuild'] for c in self.runner.calls))

    def test_personalization_quotes_repo_as_one_remote_argument(self):
        repo = "owner/dotfiles'; touch /tmp/should-not-exist; '"
        self.api.personalize('new-space', repo)
        remote = self.runner.calls[-1][-1]
        self.assertEqual(shlex.split(remote), ['bash', '-lc', shlex.join(['load-dotfiles', repo])])

    def test_personalization_seeds_a_missing_machine_profile_before_dotfiles(self):
        profile = {'git': {'name': 'Mannu Silva', 'email': 'mannu@example.com'},
                   'claudeSettings': {'permissions': {'defaultMode': 'bypassPermissions'}}}
        self.api.personalize('new-space', 'emsilva/dotfiles', profile)
        (seed, dotfiles), (sent, _) = self.runner.calls[-2:], self.runner.inputs[-2:]
        self.assertEqual(shlex.split(dotfiles[-1]), ['bash', '-lc', 'load-dotfiles emsilva/dotfiles'])
        self.assertNotIn('Mannu', seed[-1], 'The profile travels on stdin, not the command line')
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home)
            subprocess.run(['bash', '-c', seed[-1]], input=sent, env=env, check=True, text=True)
            written = Path(home) / '.config/chezmoi/machine.json'
            self.assertEqual(json.loads(written.read_text()), profile)
            self.assertEqual(written.stat().st_mode & 0o777, 0o600)
            written.write_text('{"git": {"name": "Kept"}}')
            subprocess.run(['bash', '-c', seed[-1]], input=sent, env=env, check=True, text=True)
            self.assertEqual(json.loads(written.read_text()), {'git': {'name': 'Kept'}})

    def test_failed_personalization_keeps_receipt_and_includes_resume(self):
        self.runner.fail_personalization = True
        with self.assertRaisesRegex(ValueError, '--resume new-space'):
            self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                            'emsilva/dotfiles', self.receipt)
        self.assertTrue(self.receipt.exists())
        self.assertFalse(any('delete' in c for c in self.runner.calls))

    def test_unavailable_selected_machine_never_creates(self):
        with self.assertRaisesRegex(ValueError, 'machine'):
            self.api.launch('owner/project', 'main', 'a' * 40, 'no-longer-available', '30m', 'browser',
                            None, self.receipt)
        self.assertFalse(any(c[1:3] == ['codespace', 'create'] for c in self.runner.calls))

    def test_reuse_uses_supported_rebuild_command_and_never_creates(self):
        self.runner.remote_sha = 'b' * 40
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        self.assertIn(['gh', 'codespace', 'rebuild', '--codespace', 'new-space'], self.runner.calls)
        self.assertFalse(any(c[1:3] == ['codespace', 'create'] for c in self.runner.calls))

    def test_remote_commands_that_reach_github_run_in_a_login_shell(self):
        # A non-login codespace SSH shell has no GITHUB_TOKEN, so git fetch and
        # a dotfiles clone cannot authenticate there.
        self.runner.remote_sha = 'b' * 40
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                        'emsilva/dotfiles', self.receipt, resume='new-space')
        remote = [c[-1] for c in self.runner.calls if c[1:3] == ['codespace', 'ssh']]
        fetch = next(command for command in remote if ' fetch origin ' in command)
        dotfiles = next(command for command in remote if 'load-dotfiles' in command)
        for command in (fetch, dotfiles):
            self.assertEqual(shlex.split(command)[:2], ['bash', '-lc'], command)
        self.assertEqual(shlex.split(shlex.split(dotfiles)[2]), ['load-dotfiles', 'emsilva/dotfiles'])

    def test_fetch_authenticates_from_the_login_token_without_a_configured_helper(self):
        # GitHub's recovery container sets GITHUB_TOKEN but configures no git
        # credential helper, and the resume fetch runs there before the rebuild.
        self.runner.remote_sha = 'b' * 40
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        fetch = next(c[-1] for c in self.runner.calls if c[1:3] == ['codespace', 'ssh'] and ' fetch origin ' in c[-1])
        words = shlex.split(shlex.split(fetch)[2])
        end = words.index('fetch')
        start = max(i for i, word in enumerate(words[:end]) if word == 'git')
        with tempfile.TemporaryDirectory() as home:
            env = {'PATH': os.environ['PATH'], 'HOME': home, 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0', 'GITHUB_TOKEN': 'login-token'}
            result = subprocess.run(['git', *words[start + 1:end], 'credential', 'fill'], env=env, cwd=home,
                                    input='protocol=https\nhost=github.com\n\n', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('password=login-token\n', result.stdout)

    def test_retry_of_current_codespace_skips_rebuild(self):
        self.api._save_receipt(self.receipt, {'codespace': 'new-space', 'repository': 'owner/project',
            'branch': 'main', 'commit': 'a' * 40,
            'validated_commit': 'a' * 40, 'rebuild_pending': False})
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        self.assertFalse(any(c[1:3] == ['codespace', 'rebuild'] for c in self.runner.calls))

    def test_matching_checkout_without_validated_image_requires_rebuild(self):
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        self.assertEqual(sum(c[1:3] == ['codespace', 'rebuild'] for c in self.runner.calls), 1)
        self.assertEqual(json.loads(self.receipt.read_text())['validated_commit'], 'a' * 40)

    def test_previous_image_receipt_cannot_certify_a_new_checked_out_commit(self):
        self.api._save_receipt(self.receipt, {'codespace': 'new-space', 'repository': 'owner/project',
            'branch': 'main', 'commit': 'b' * 40,
            'validated_commit': 'b' * 40, 'rebuild_pending': False})
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        self.assertEqual(sum(c[1:3] == ['codespace', 'rebuild'] for c in self.runner.calls), 1)

    def test_failed_initial_validation_never_attests_to_a_built_image(self):
        self.runner.fail_validation = True
        with self.assertRaisesRegex(ValueError, 'Missing selected tool'):
            self.launch()
        self.assertIsNone(json.loads(self.receipt.read_text()).get('validated_commit'))
        self.runner.fail_validation = False
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        self.assertEqual(sum(c[1:3] == ['codespace', 'rebuild'] for c in self.runner.calls), 1)

    def test_resume_checks_live_git_instead_of_stale_api_dirty_flags(self):
        self.runner.api_dirty = True
        self.assertEqual(self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                         None, self.receipt, resume='new-space'), 'new-space')

    def test_failed_rebuild_is_retried_even_after_git_already_advanced(self):
        self.runner.remote_sha = 'b' * 40
        self.runner.fail_rebuild_once = True
        with self.assertRaisesRegex(ValueError, 'temporary rebuild API failure'):
            self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                            None, self.receipt, resume='new-space')
        self.assertTrue(json.loads(self.receipt.read_text()).get('rebuild_pending'))
        self.assertEqual(self.runner.remote_sha, 'a' * 40)
        self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                        None, self.receipt, resume='new-space')
        self.assertEqual(sum(c[1:3] == ['codespace', 'rebuild'] for c in self.runner.calls), 2)
        self.assertFalse(json.loads(self.receipt.read_text())['rebuild_pending'])

    def test_rebuild_waits_for_new_container_when_api_still_says_available(self):
        self.runner.remote_sha = 'b' * 40
        self.runner.identity_sequence = [100, 100, 101]
        with patch('devcontainer_setup.codespaces.time.sleep'):
            self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                            None, self.receipt, resume='new-space')
        self.assertEqual(self.runner.identity_sequence, [])

    def test_resume_preserves_actual_remote_changes_even_when_api_says_clean(self):
        self.runner.remote_dirty = ' M source.py\n'
        with self.assertRaisesRegex(ValueError, 'source.py'):
            self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                            None, self.receipt, resume='new-space')
        self.assertFalse(any(' fetch origin ' in call[-1] for call in self.runner.calls))
        self.assertFalse(any(call[1:3] == ['codespace', 'rebuild'] for call in self.runner.calls))

    def test_resume_preserves_remote_branch_when_api_ref_is_stale(self):
        self.runner.remote_branch = 'user-work'
        with self.assertRaisesRegex(ValueError, 'Move new-space to main first; it is on user-work'):
            self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                            None, self.receipt, resume='new-space')
        self.assertFalse(any(' fetch origin ' in call[-1] for call in self.runner.calls))

    def test_remote_revision_must_contain_reviewed_commit_before_opening(self):
        self.runner.remote_sha = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'reviewed commit'):
            self.api.launch('owner/project', 'main', 'a' * 40, MACHINE['name'], '30m', 'browser',
                            None, self.receipt)
        self.assertFalse(any(c[1:3] == ['codespace', 'code'] for c in self.runner.calls))
        self.assertTrue(self.receipt.exists())


    def resume(self, **options):
        values = dict(machine=MACHINE['name'], idle_timeout='30m', client='browser',
                      dotfiles_repo=None, receipt=self.receipt, resume='new-space')
        values.update(options)
        return self.api.launch('owner/project', 'main', 'a' * 40, **values)

    def test_invalid_receipt_path_refused_before_allocation(self):
        for path in (Path(self.tmp.name), Path(self.tmp.name) / 'file/launch.json'):
            with self.subTest(path=path):
                (Path(self.tmp.name) / 'file').write_text('owner data')
                self.receipt = path
                with self.assertRaisesRegex(ValueError, 'receipt|state'):
                    self.launch()
                self.assertFalse(any(c[1:3] == ['codespace', 'create'] for c in self.runner.calls))
                self.assertEqual((Path(self.tmp.name) / 'file').read_text(), 'owner data')

    def test_receipt_failure_after_allocation_always_reports_resume(self):
        for resume in (False, True):
            for save_number in (1, 2):
                with self.subTest(resume=resume, save_number=save_number):
                    self.runner.calls.clear()
                    self.receipt.unlink(missing_ok=True)
                    save = self.api._save_receipt
                    count = 0
                    def failing_save(path, values):
                        nonlocal count
                        count += 1
                        if count == save_number:
                            raise OSError('simulated receipt disk failure')
                        save(path, values)
                    with patch.object(self.api, '_save_receipt', side_effect=failing_save):
                        with self.assertRaisesRegex(ValueError,
                                'task devcontainer:setup -- --resume new-space'):
                            self.resume() if resume else self.launch()
                    self.assertEqual(sum(c[1:3] == ['codespace', 'create'] for c in self.runner.calls),
                                     0 if resume else 1)
                    if self.receipt.exists():
                        self.assertIsNone(json.loads(self.receipt.read_text())['validated_commit'])
                    self.assertFalse(any(c[1:3] == ['codespace', 'code'] for c in self.runner.calls))

    def test_resume_rejects_conflicting_resource_settings_without_mutation(self):
        for options in ({'machine': 'cheaper-machine'}, {'idle_timeout': '15m'},
                        {'retention': '24h'}, {'location': 'WestEurope'}):
            with self.subTest(options=options):
                self.runner.calls.clear()
                with self.assertRaisesRegex(ValueError, 'existing|Existing|resource'):
                    self.resume(**options)
                self.assertFalse(self.receipt.exists())
                self.assertTrue(all(c[1] == 'api' and len(c) == 3 for c in self.runner.calls))

    def test_resume_requires_actual_machine_metadata(self):
        self.runner.resource.pop('machine')
        with self.assertRaisesRegex(ValueError, 'machine.*metadata|metadata.*machine'):
            self.resume()
        self.assertFalse(self.receipt.exists())

    def test_resume_receipt_records_actual_resource_settings(self):
        self.resume(location='EastUs', retention='168h')
        record = json.loads(self.receipt.read_text())
        self.assertEqual(record['machine'], MACHINE['name'])
        self.assertEqual(record['idle_timeout_minutes'], 30)
        self.assertEqual(record['retention_period_minutes'], 10080)
        self.assertEqual(record['location'], 'EastUs')


RERUN = 'Merge it, then run task devcontainer:setup again to create the Codespace.'


class FakePulls:
    """gh api for one pull request: records argv and stdin; refuses and holds checks as told."""

    def __init__(self):
        self.calls, self.inputs = [], []
        self.listed = []
        self.pr = {'number': 7, 'title': 'Configure project devcontainer', 'state': 'open', 'merged': False,
                   'merge_commit_sha': None, 'base': {'ref': 'main'},
                   'head': {'ref': 'setup/devcontainer', 'sha': 'c' * 40, 'repo': {'full_name': 'owner/project'}}}
        self.refusals = []         # GitHub's message for each refused merge, in order
        self.json_errors = True    # gh prints the error body on stdout as well as its stderr line
        self.status = []           # (state, total_count) per status read; then ('success', 1)
        self.runs = []             # check runs (a status, or a run) per check-runs read; then none
        self.merged_by_hand = False
        self.moved_head = None     # the head GitHub reports after the first refusal
        self.merge_output = None   # what a successful merge prints instead of its usual body
        self.timeouts, self.clock, self.put_seconds, self.hang = [], None, 0, None
        self.marks_merged = True

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        self.inputs.append(kwargs.get('input'))
        self.timeouts.append(kwargs.get('timeout'))
        endpoint = next(arg for arg in args[2:] if arg.startswith('repos/') or arg == 'user')
        path = endpoint.split('?')[0]
        if self.hang and path.endswith(self.hang):
            raise subprocess.TimeoutExpired(args, kwargs.get('timeout'))
        if path.endswith('/merge'):
            if self.clock:
                self.clock.now += self.put_seconds
            if self.refusals:
                message = self.refusals.pop(0)
                if self.merged_by_hand:
                    self.pr.update(merged=True, state='closed', merge_commit_sha='e' * 40)
                if self.moved_head:
                    self.pr['head'] = dict(self.pr['head'], sha=self.moved_head)
                body = json.dumps({'message': message, 'status': '405'}) if self.json_errors else ''
                return subprocess.CompletedProcess(args, 1, body, f'gh: {message} (HTTP 405)\n')
            if self.marks_merged:
                self.pr.update(merged=True, state='closed', merge_commit_sha='d' * 40)
            if self.merge_output is not None:
                return subprocess.CompletedProcess(args, 0, self.merge_output, '')
            return self.reply(args, {'sha': 'd' * 40, 'merged': True, 'message': 'Pull Request successfully merged'})
        if path.endswith('/status'):
            state, total = self.status.pop(0) if self.status else ('success', 1)
            return self.reply(args, {'state': state, 'total_count': total, 'statuses': []})
        if path.endswith('/check-runs'):
            runs = [run if isinstance(run, dict) else {'status': run} for run in (self.runs.pop(0) if self.runs else [])]
            return self.reply(args, {'total_count': len(runs), 'check_runs': runs})
        if path == 'repos/owner/project/pulls/7':
            return self.reply(args, self.pr)
        if path == 'repos/owner/project/pulls' and '--method' in args:
            return self.reply(args, dict(self.pr, number=8))
        if path == 'repos/owner/project/pulls':
            return self.reply(args, self.listed)
        return subprocess.CompletedProcess(args, 1, '', f'unexpected {args}')

    @staticmethod
    def reply(args, value):
        return subprocess.CompletedProcess(args, 0, json.dumps(value), '')

    def merges(self):
        return [index for index, call in enumerate(self.calls) if call[1:4] == ['api', '--method', 'PUT']]


class FakeClock:
    def __init__(self):
        self.now, self.slept = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class AccountTests(unittest.TestCase):
    @staticmethod
    def user(login):
        return Codespaces(run=lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps({'login': login}), '')).user()

    def test_user_accepts_enterprise_managed_logins(self):
        # An enterprise managed user's login ends in _<shortcode>, such as mannu_kyndryl.
        for login in ('octo', 'octo-cat', 'mannu_kyndryl'):
            with self.subTest(login=login):
                self.assertEqual(self.user(login), login)

    def test_user_rejects_a_missing_or_malformed_login(self):
        for login in (None, '', 7, 'octo cat', '../octo', 'octo\n'):
            with self.subTest(login=login), self.assertRaisesRegex(ValueError, 'GitHub returned no account name'):
                self.user(login)


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        self.pulls = FakePulls()
        self.api = Codespaces(run=self.pulls)
        self.clock = self.pulls.clock = FakeClock()

    def merge(self, expected='c' * 40, **options):
        options.setdefault('sleep', self.clock.sleep)
        with patch('devcontainer_setup.codespaces.time.monotonic', self.clock.monotonic):
            return self.api.merge_pr('owner/project', 7, 'Configure project devcontainer (#7)',
                                     'Reviewed and merged with task devcontainer:setup.', expected, **options)

    def test_merge_refuses_a_head_other_than_the_reviewed_one(self):
        with self.assertRaises(ValueError) as caught:
            self.merge('b' * 40, sleep=self.fail)
        self.assertEqual(str(caught.exception), f'PR #7 is at {"c" * 12}, not the reviewed {"b" * 12}; nothing was merged. {RERUN}')
        self.assertEqual(self.pulls.merges(), [])
        # The head moves while setup waits for checks: the next merge is never sent.
        self.setUp()
        self.pulls.refusals, self.pulls.status, self.pulls.moved_head = ['Required status check "ci" is expected.'], [('pending', 1)], 'f' * 40
        with self.assertRaises(ValueError) as caught:
            self.merge(sleep=self.fail)
        self.assertEqual(str(caught.exception), f'PR #7 is at {"f" * 12}, not the reviewed {"c" * 12}; nothing was merged. {RERUN}')
        self.assertEqual(len(self.pulls.merges()), 1)
        # Merged by someone else at another head: no codespace on a commit nobody reviewed here.
        self.setUp()
        self.pulls.pr.update(merged=True, state='closed', merge_commit_sha='e' * 40)
        with self.assertRaises(ValueError) as caught:
            self.merge('b' * 40, sleep=self.fail)
        self.assertEqual(str(caught.exception), f'PR #7 was merged at {"c" * 12}, not the reviewed {"b" * 12}; no codespace '
                         'was created. Run task devcontainer:setup again to create the Codespace.')

    def test_merge_refuses_a_closed_pr(self):
        self.pulls.pr.update(state='closed')
        with self.assertRaises(ValueError) as caught:
            self.merge(sleep=self.fail)
        self.assertEqual(str(caught.exception), 'PR #7 was closed without being merged; nothing was merged. '
                         'Run task devcontainer:setup again to open a new one.')
        self.assertEqual(self.pulls.merges(), [])

    def test_merge_wait_is_bounded_by_the_clock(self):
        # Each refused merge takes 400 s: the 15-minute bound is wall time, not a count of sleeps.
        self.pulls.refusals = ['Required status check "ci" is expected.'] * 30
        self.pulls.status, self.pulls.put_seconds = [('pending', 1)] * 30, 400
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError) as caught:
            self.merge()
        self.assertEqual(self.clock.slept, [30, 30])
        self.assertTrue(str(caught.exception).endswith(RERUN))

    def test_hung_github_call_ends_as_a_refusal(self):
        for hang in ('/merge', '/status', 'pulls/7'):
            with self.subTest(hang=hang):
                self.setUp()
                self.pulls.hang = hang
                self.pulls.refusals, self.pulls.status = ['Required status check "ci" is expected.'], [('pending', 1)]
                with self.assertRaises(ValueError) as caught:
                    self.merge(sleep=self.fail)
                self.assertEqual(str(caught.exception),
                                 f'PR #7 is not confirmed merged: GitHub did not answer within 60s. {RERUN}')
                self.assertTrue(all(self.pulls.timeouts), self.pulls.timeouts)

    def test_failed_checks_refuse_without_waiting(self):
        reason = 'Required status check "ci" has failed'
        self.pulls.refusals, self.pulls.status = [reason + '.'], [('failure', 1)]
        self.pulls.runs = [[{'status': 'completed', 'conclusion': 'failure'}, {'status': 'completed', 'conclusion': 'success'}]]
        with self.assertRaises(ValueError) as caught:
            self.merge(sleep=self.fail)
        self.assertEqual(str(caught.exception), f'PR #7 is open: {reason}. {RERUN}')

    def test_merge_without_its_sha_reads_the_merge_commit(self):
        for output in ('{}', '', 'not json'):
            with self.subTest(output=output):
                self.setUp()
                self.pulls.merge_output = output
                self.assertEqual(self.merge(sleep=self.fail), 'd' * 40)
        # GitHub took the merge but does not show it yet: a fixed text, never a traceback.
        self.setUp()
        self.pulls.merge_output, self.pulls.marks_merged = '{}', False
        self.pulls.pr['merge_commit_sha'] = 'f' * 40  # An open PR's test merge, never the merge itself.
        with self.assertRaises(ValueError) as caught:
            self.merge(sleep=self.fail)
        self.assertEqual(str(caught.exception), 'GitHub accepted the merge of PR #7 but does not show it merged yet; '
                         'run task devcontainer:setup again to create the Codespace.')

    def test_merge_uses_rest_with_verbatim_message(self):
        self.assertEqual(self.merge(sleep=self.fail), 'd' * 40)
        (index,) = self.pulls.merges()
        self.assertEqual(self.pulls.calls[index],
                         ['gh', 'api', '--method', 'PUT', 'repos/owner/project/pulls/7/merge', '--input', '-'])
        self.assertEqual(json.loads(self.pulls.inputs[index]),
                         {'merge_method': 'merge', 'commit_title': 'Configure project devcontainer (#7)',
                          'commit_message': 'Reviewed and merged with task devcontainer:setup.', 'sha': 'c' * 40})
        sent = json.dumps(self.pulls.calls) + ''.join(text for text in self.pulls.inputs if text)
        self.assertNotIn('Co-authored-by', sent)
        self.assertFalse(any(call[1:3] == ['pr', 'merge'] for call in self.pulls.calls))

    def test_merge_waits_for_pending_checks_then_merges(self):
        reason = 'Required status check "ci" is expected.'
        self.pulls.refusals = [reason, reason]
        # A pending commit status, then no statuses (GitHub still says "pending") but a queued check run.
        self.pulls.status, self.pulls.runs = [('pending', 1), ('pending', 0)], [['completed', 'queued']]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.merge(), 'd' * 40)
        self.assertEqual(self.clock.slept, [30, 30])
        self.assertEqual(len(self.pulls.merges()), 3)
        self.assertEqual(len([line for line in output.getvalue().splitlines() if 'PR #7' in line]), 2)

    def test_merge_stops_waiting_after_its_limit(self):
        reason = 'Required status check "ci" is expected.'
        self.pulls.refusals = [reason] * 10
        self.pulls.status = [('pending', 1)] * 10
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError) as caught:
            self.merge(wait=90)
        self.assertEqual(self.clock.slept, [30, 30, 30])
        self.assertEqual(str(caught.exception), f'PR #7 is open: Required status check "ci" is expected. {RERUN}')

    def test_merge_refusal_reports_github_reason_and_rerun(self):
        reason = 'At least 1 approving review is required by reviewers with write access.'
        for json_errors in (True, False):
            with self.subTest(json_errors=json_errors):
                self.setUp()
                self.pulls.json_errors = json_errors
                self.pulls.refusals = [reason]
                # No statuses at all: GitHub reports "pending" with a zero count; nothing is running.
                self.pulls.status = [('pending', 0)]
                with self.assertRaises(ValueError) as caught:
                    self.merge(sleep=self.fail)
                self.assertEqual(str(caught.exception), f'PR #7 is open: {reason.rstrip(".")}. {RERUN}')
                self.assertEqual(len(self.pulls.merges()), 1)

    def test_merge_returns_merge_commit_when_already_merged(self):
        self.pulls.pr.update(merged=True, state='closed', merge_commit_sha='e' * 40)
        self.assertEqual(self.merge(sleep=self.fail), 'e' * 40)
        self.assertEqual(self.pulls.merges(), [])
        # Merged by hand while setup was trying: GitHub's refusal is followed by the merged state.
        self.setUp()
        self.pulls.refusals, self.pulls.merged_by_hand = ['Pull Request is not mergeable'], True
        self.assertEqual(self.merge(sleep=self.fail), 'e' * 40)

    def test_open_pr_reuses_existing(self):
        self.pulls.listed = [dict(self.pulls.pr, number=5)]
        self.assertEqual(self.api.open_pr('owner/project', 'setup/devcontainer', 'main',
                                          'Configure project devcontainer', 'Files: Taskfile.yml'), 5)
        query = parse_qs(urlsplit(self.pulls.calls[0][2]).query)
        self.assertEqual((query['head'], query['base'], query['state']), (['owner:setup/devcontainer'], ['main'], ['open']))
        self.assertFalse(any('POST' in call for call in self.pulls.calls))
        self.pulls.listed = []
        self.assertEqual(self.api.open_pr('owner/project', 'setup/devcontainer', 'main',
                                          'Configure project devcontainer', 'Files: Taskfile.yml'), 8)
        self.assertEqual(self.pulls.calls[-1], ['gh', 'api', '--method', 'POST', 'repos/owner/project/pulls', '--input', '-'])
        self.assertEqual(json.loads(self.pulls.inputs[-1]), {'title': 'Configure project devcontainer', 'head': 'setup/devcontainer',
                                                             'base': 'main', 'body': 'Files: Taskfile.yml'})

    def test_open_setup_prs_are_only_setup_heads_from_this_repository(self):
        head = self.pulls.pr['head']
        self.pulls.listed = [[dict(self.pulls.pr, number=3), dict(self.pulls.pr, number=4, head=dict(head, ref='setup/devcontainer-2')),
                              dict(self.pulls.pr, number=5, head=dict(head, ref='feature/setup/devcontainer')),
                              dict(self.pulls.pr, number=6, head=dict(head, repo={'full_name': 'fork/project'})),
                              dict(self.pulls.pr, number=9, head=dict(head, repo=None))]]
        self.assertEqual([pr['number'] for pr in self.api.setup_prs('owner/project', 'main')], [3, 4])
        query = parse_qs(urlsplit(self.pulls.calls[0][2]).query)
        self.assertEqual((query['base'], query['state']), (['main'], ['open']))


if __name__ == '__main__':
    unittest.main()
