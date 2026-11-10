"""Offline coordinator tests; real launch logic, private state and scripted runtime receipts."""
import contextlib
import copy
from datetime import datetime, timezone
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from devcontainer_setup.codespaces import Codespaces, receipt_path
from devcontainer_setup.project_sync import replacing, standing
from devcontainer_setup.project_sync.state import atomic_write, discover_project, locked, read_json, state_dir
from tests.test_codespaces import FakeGitHub
from tests.test_replacing import journal


class Clock:
    def __init__(self):
        self.value = 2000.0
        self.waits = []

    def now(self):
        self.value += .01
        return self.value

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.value += seconds


class UI:
    def __init__(self):
        self.answers = []
        self.calls = []

    def choose(self, title, options):
        self.calls.append((title, options))
        answer = self.answers.pop(0) if self.answers else 'continue'
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            return options[answer]
        return answer


class API:
    """Inspection fake whose launch delegates to the production Codespaces.launch."""
    def __init__(self, case):
        self.case = case
        self.gh = FakeGitHub()
        self.gh.resource.update(machine={'name': 'premium', 'cpus': 8}, idle_timeout_minutes=120)
        self.client = Codespaces(run=self.command)
        self.old = dict(name='old-space', repository={'full_name': 'owner/project'},
                        machine={'name': 'premium', 'cpus': 8}, idle_timeout_minutes=120,
                        billable_owner={'login': 'owner'}, git_status={'ref': 'feature/topic'})
        self.rows = [copy.deepcopy(self.old)]
        self.account = 'owner'
        self.catalogue = [{'name': 'premium', 'cpus': 8}]
        self.calls = []
        self.launches = []
        self.mode = None
        self.visible = True
        self.inventory = None
        self.details = {}
        self.failure = None
        self.preflight_failure = False
        self.create_hook = None
        self.create_journals = []

    def fail(self, step):
        if self.failure == step:
            raise ValueError('https://private.invalid/path ghp_TEST_ONLY_NOT_A_CREDENTIAL')

    def get(self, name):
        self.calls.append(('get', name))
        self.fail('get')
        value = self.old if name == 'old-space' else self.details.get(name, self.row(name))
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    def user(self):
        self.calls.append(('user',))
        self.fail('user')
        return self.account

    def machines(self, repo, commit):
        self.calls.append(('machines', repo, commit))
        self.fail('machines')
        return self.catalogue

    def existing(self, repo, branch=None, *, complete=False):
        self.calls.append(('existing', repo, branch, complete))
        self.case.assertTrue(complete)
        self.fail('existing')
        if self.inventory:
            return self.inventory()
        return copy.deepcopy(self.rows)

    def row(self, name='new-space', **changes):
        value = dict(name=name, repository=copy.deepcopy(self.old['repository']), git_status={'ref': 'main'},
                     machine={'name': 'premium', 'cpus': 8},
                     created_at=datetime.fromtimestamp(2000, timezone.utc).isoformat())
        value.update(changes)
        return value

    def command(self, args, **kwargs):
        if args[1] == 'api' and args[2].endswith('/machines'):
            self.gh.calls.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps({'machines': self.catalogue}), '')
        if args[1:3] == ['codespace', 'create']:
            self.create_journals.append(read_json(self.case.state / 'replace.json'))
            if self.create_hook:
                self.create_hook()
            if self.visible:
                self.rows.append(self.row())
            result = self.gh(args, **kwargs)
            if self.mode == 'empty':
                return subprocess.CompletedProcess(args, 0, '', '')
            if self.mode == 'lost':
                raise ValueError('reply lost')
            if self.mode == 'interrupt':
                raise KeyboardInterrupt
            return result
        if args[1] == 'api' and args[2].startswith('user/codespaces/'):
            self.gh.calls.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps(dict(self.row(args[2].split('/')[-1]),
                **self.gh.resource, state='Available', git_status={'ref': self.gh.api_ref})), '')
        return self.gh(args, **kwargs)

    def launch(self, *args, **kwargs):
        self.launches.append((args, kwargs))
        self.fail('launch')
        if self.preflight_failure:
            raise ValueError('preflight failed')
        return self.client.launch(*args, **kwargs)


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('devcontainer_setup.replacement'),
                             'replacement coordinator is missing')
        self.module = importlib.import_module('devcontainer_setup.replacement')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        for args in (('init', '--quiet', '-b', 'main'),
                     ('remote', 'add', 'origin', 'https://github.com/owner/project.git')):
            subprocess.run(['git', '-C', str(self.project), *args], check=True, capture_output=True)
        self.env = {'HOME': str(self.root), 'XDG_STATE_HOME': str(self.root / 'state'),
                    'XDG_CONFIG_HOME': str(self.root / 'config')}
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.identity = discover_project(self.project)
        self.state = state_dir(self.identity, self.env)
        self.save('scratch.json', dict(version=1, identity=self.identity.identity, root=str(self.project),
                                      codespace='old-space', remote_root='/workspaces/project'))
        runtime = self.project / '.devcontainer/scripts/project-sync.sh'
        runtime.parent.mkdir(parents=True)
        runtime.write_text('# fixture: runner never executes this file\n')
        self.api, self.ui, self.clock = API(self), UI(), Clock()
        self.steps, self.answers, self.prompts = [], [], []
        self.check_ok, self.cutover_ok, self.delete_code, self.signin_code = True, True, 1, 0
        self.runner_hook = None
        self.signin_record = None
        self.output = io.StringIO()
        self.sequence = 0
        self.pin = patch.object(self.module.replace_inputs, 'pin', return_value='a' * 40).start()
        self.differs = patch.object(self.module.replace_inputs, 'setup_differs', return_value=None).start()
        self.default = patch.object(self.module.git_ops, 'default_branch', return_value='main').start()
        self.addCleanup(patch.stopall)
        self.options = dict(workspace='/workspaces/project', dotfiles_repo='owner/dotfiles',
                            machine_profile=None, state_root=self.root / 'state', open_after_create='browser')
        patch.object(self.module, 'launch_options', return_value=self.options).start()
        patch.object(self.module, 'load_preferences', return_value={}).start()

    def save(self, name, value):
        atomic_write(self.state / name, json.dumps(value).encode())

    def active(self, **changes):
        self.sequence += 1
        value = journal(repository='owner/project', account='owner', phase='create-requested', new=None,
                        operation=f'{self.sequence:012x}',
                        resources=dict(machine='premium', cpus=8, idle_timeout_minutes=120, billable_owner='owner'),
                        at={'inspected': 1999.0, 'create-requested': 2000.0},
                        request=dict(at=2000.0, before=['old-space'], retries=[]))
        value.update(changes)
        value['at'].setdefault(value['phase'], 2000.0)
        replacing.write(self.state, value)
        return value

    def ask(self, prompt):
        self.prompts.append(prompt)
        answer = self.answers.pop(0) if self.answers else 'stop'
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def runner(self, argv, **kwargs):
        self.steps.append((argv, kwargs, read_json(self.state / 'replace.json')))
        if self.runner_hook:
            result = self.runner_hook(argv)
            if result is not None:
                return result
        operation = read_json(self.state / 'replace.json')['operation']
        if 'cutover' in argv:
            if self.cutover_ok:
                config = read_json(self.state / 'scratch.json')
                self.save('scratch.json', dict(config, codespace='new-space'))
                self.save('retired/old-space.json', {'operation': operation})
            result = dict(schema=1, operation=operation, step='cutover', old='old-space', new='new-space',
                          moved=self.cutover_ok, sealed_at=2001.0, cycle_id='cycle', snapshot_id='snapshot',
                          herdr=0, at=self.clock.now())
        elif 'check' in argv:
            result = dict(schema=1, operation=operation, step='check', codespace='new-space',
                          checked_at=self.clock.now(), sync='ok', cycle_completed_at=self.clock.now(),
                          recovery='not armed', herdr='ok', conversations={key: [] for key in
                          ('on_new', 'kept', 'forgotten', 'missing', 'pending', 'diverged', 'kept_mac_only')},
                          ok=self.check_ok)
        elif '--signin-all' in argv:
            self.save('signins.json', self.signin_record or dict(schema=1, codespace='new-space',
                updated_at=self.clock.now(), tools={'codex': {'state': 'signed-in'}, 'grok': {'state': 'absent'}}))
            return subprocess.CompletedProcess(argv, self.signin_code)
        else:
            self.assertIn('delete', argv)
            self.assertEqual(argv[-2:], ['--codespace', 'old-space'])
            if not self.delete_code:
                self.save('deletions/old-space.json', {'phase': 'cleaned'})
            return subprocess.CompletedProcess(argv, self.delete_code)
        path = Path(argv[argv.index('--receipt') + 1])
        atomic_write(path, json.dumps(result).encode())
        return subprocess.CompletedProcess(argv, 0 if result.get('moved', result.get('ok')) else 1)

    def run_replace(self, **kwargs):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return self.module.run(self.project, api=self.api, ui=self.ui, ask=self.ask, env=self.env,
                                   tty=kwargs.pop('tty', True), runner=self.runner, now=self.clock.now,
                                   sleep=self.clock.sleep, **kwargs)

    def creates(self):
        return [row for row in self.api.gh.calls if row[1:3] == ['codespace', 'create']]

    def archives(self):
        return sorted([read_json(path) for path in (self.state / 'replacements').glob('*.json')],
                      key=lambda value: value['ended_at'])

    def valid_launch(self):
        path = receipt_path(self.root / 'state', 'owner/project', 'main')
        atomic_write(path, json.dumps(dict(codespace='new-space', repository='owner/project', branch='main',
            validated_commit='a' * 40, rebuild_pending=False)).encode())

    def mixed_case_origin(self):
        subprocess.run(['git', '-C', str(self.project), 'remote', 'set-url', 'origin',
                        'https://github.com/Example/Project.git'], check=True, capture_output=True)
        self.identity = discover_project(self.project)
        self.state = state_dir(self.identity, self.env)
        self.save('scratch.json', dict(version=1, identity=self.identity.identity, root=str(self.project),
                                      codespace='old-space', remote_root='/workspaces/Project'))
        self.api.old['repository']['full_name'] = 'Example/Project'
        self.api.rows = [copy.deepcopy(self.api.old)]

    def test_mixed_case_origin_preserves_olds_setup_receipt(self):
        self.mixed_case_origin()
        old = dict(codespace='old-space', repository='Example/Project', branch='feature/topic', commit='c' * 40)
        path = receipt_path(self.root / 'state', 'Example/Project', old['branch'])
        self.api.client._save_receipt(path, old)
        self.assertEqual(self.run_replace(), 0, self.output.getvalue())
        self.assertEqual(self.archives()[0]['old_receipt'], old)

    def test_mixed_case_origin_new_launch_is_visible_to_standing(self):
        self.mixed_case_origin()
        self.assertEqual(self.run_replace(), 0, self.output.getvalue())
        launch = standing.read(self.project, env=self.env, timeout=30).launch
        self.assertIsNotNone(launch, 'standing cannot find the replacement launch receipt')
        self.assertEqual(launch.codespace, 'new-space')
        self.assertTrue(launch.complete)

    def test_mixed_case_origin_preserves_default_workspace_folder(self):
        self.mixed_case_origin()
        self.options['workspace'] = None
        self.assertEqual(self.run_replace(), 0, self.output.getvalue())
        self.assertEqual(self.archives()[0]['remote_root'], '/workspaces/Project')
        self.assertEqual(self.api.launches[0][1]['workspace'], '/workspaces/Project')

    def test_mixed_case_origin_preserves_journal_repository(self):
        self.mixed_case_origin()
        self.assertEqual(self.run_replace(), 0, self.output.getvalue())
        self.assertEqual(self.archives()[0]['repository'], 'Example/Project')
        self.assertEqual(self.api.create_journals[0]['repository'], 'Example/Project')
        self.assertTrue(all(call[1] == 'Example/Project' for call in self.api.calls
                            if call[0] in ('machines', 'existing')))

    def test_unreadable_or_foreign_origin_repository_refuses_before_api(self):
        self.mixed_case_origin()
        for repo in (None, 'other/project'):
            with self.subTest(repo=repo), patch('devcontainer_setup.__main__._github_repository', return_value=repo):
                self.assertEqual(self.run_replace(), 1)
                self.assertIn('The pairing belongs to another project; nothing was created.', self.output.getvalue())
                self.assertFalse(self.api.calls)
        self.assertEqual(self.creates(), [])

    def test_premium_120_minutes_beats_a_stale_four_core_receipt(self):
        path = receipt_path(self.root / 'state', 'owner/project', 'main')
        atomic_write(path, json.dumps(dict(codespace='other-space', machine='standard',
                                          idle_timeout_minutes=30)).encode())
        self.assertEqual(self.run_replace(), 0)
        argv = self.creates()[0]
        self.assertEqual(argv[argv.index('--machine') + 1], 'premium')
        self.assertEqual(argv[argv.index('--idle-timeout') + 1], '120m')
        self.assertEqual(self.archives()[0]['resources']['cpus'], 8)

    def test_unreadable_resources_create_nothing(self):
        old = copy.deepcopy(self.api.old)
        for field, target, key in (('machine', None, 'machine'), ('core count', 'machine', 'cpus'),
                                    ('idle timeout', None, 'idle_timeout_minutes'),
                                    ('billing owner', None, 'billable_owner')):
            with self.subTest(field=field):
                self.api.old = copy.deepcopy(old)
                (self.api.old[target] if target else self.api.old).pop(key)
                self.assertEqual(self.run_replace(), 1)
                self.assertIn(f"old-space's {field} cannot be read from GitHub; nothing was created.",
                              self.output.getvalue())
        self.assertEqual(self.creates(), [])

    def test_machine_not_offered_or_setup_missing_creates_nothing(self):
        self.api.catalogue = []
        self.assertEqual(self.run_replace(), 1)
        self.assertIn('not offered for main at aaaaaaaaaaaa', self.output.getvalue())
        self.api.catalogue = [{'name': 'premium', 'cpus': 8}]
        self.differs.return_value = 'different selections'
        self.assertEqual(self.run_replace(), 1)
        self.assertIn('Run task setup first.', self.output.getvalue())
        self.assertEqual(self.creates(), [])

    def test_a_tip_that_moves_before_create_is_checked_again(self):
        self.pin.side_effect = ['a' * 40, 'b' * 40]
        self.api.gh.remote_sha = 'b' * 40
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(self.differs.call_args_list[-1].args, (self.project, 'b' * 40))
        self.assertEqual(self.api.create_journals[0]['commit'], 'b' * 40)
        self.assertIn('main moved to bbbbbbbbbbbb', self.output.getvalue())

    def test_journal_holds_the_request_before_create(self):
        self.assertEqual(self.run_replace(), 0)
        value = self.api.create_journals[0]
        self.assertEqual(value['phase'], 'create-requested')
        self.assertEqual(value['request']['before'], ['old-space'])
        self.assertEqual(value['request']['retries'], [])
        self.assertIsNone(value['new'])

    def test_lost_create_reply_adopts_exactly_one(self):
        for mode in ('empty', 'lost', 'interrupt'):
            with self.subTest(mode=mode):
                self.api.mode = mode
                self.assertEqual(self.run_replace(), 1 if mode == 'interrupt' else 0)
                if mode == 'interrupt':
                    self.api.mode = None
                    self.assertEqual(self.run_replace(), 0)
                self.assertEqual(len(self.creates()), 1)
                archived = self.archives()[-1]
                evidence = read_json(self.state / 'start-evidence.json')
                self.assertIn('created', json.dumps(evidence))
                self.assertIn(str(archived['request']['at']), json.dumps(evidence))
                self.assertEqual(sum(call[1:3] == ['codespace', 'rebuild'] for call in self.api.gh.calls), 1)
                self.api.gh.calls.clear()
                self.api.rows = [copy.deepcopy(self.api.old)]
                receipt_path(self.root / 'state', 'owner/project', 'main').unlink(missing_ok=True)
                self.save('scratch.json', dict(read_json(self.state / 'scratch.json'), codespace='old-space'))

    def test_adoption_ignores_older_or_other_branch_codespaces(self):
        self.active()
        self.api.rows += [self.api.row('older', created_at='1970-01-01T00:00:00Z'),
                          self.api.row('branch', git_status={'ref': 'other'}),
                          self.api.row('machine', machine={'name': 'other'}), self.api.row()]
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(self.api.launches[0][1]['resume'], 'new-space')
        self.assertEqual(self.creates(), [])

    def assert_current_pairing_is_not_adopted(self, validated_commit):
        self.active()
        self.api.rows.append(self.api.row('work-space'))
        self.save('scratch.json', dict(read_json(self.state / 'scratch.json'), codespace='work-space'))
        if validated_commit is not None:
            path = receipt_path(self.root / 'state', 'owner/project', 'main')
            atomic_write(path, json.dumps(dict(codespace='work-space', repository='owner/project', branch='main',
                commit=validated_commit, validated_commit=validated_commit, rebuild_pending=False)).encode())
        self.cutover_ok = False
        self.assertEqual(self.run_replace(), 1)
        self.assertIsNone(replacing.read(self.state)['new'])
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'work-space')
        self.assertFalse(self.api.launches)
        self.assertEqual(self.creates(), [])
        self.assertFalse(any(row[1:3] == ['codespace', 'rebuild'] for row in self.api.gh.calls))

    def test_current_pairing_validated_at_the_pin_is_never_adopted(self):
        self.assert_current_pairing_is_not_adopted('a' * 40)

    def test_current_pairing_validated_at_a_later_tip_is_never_adopted(self):
        self.assert_current_pairing_is_not_adopted('b' * 40)

    def test_current_pairing_without_a_receipt_is_never_adopted(self):
        self.assert_current_pairing_is_not_adopted(None)

    def test_previously_connected_names_are_excluded_without_hiding_new(self):
        self.active()
        self.save('accounts.json', {'schema': 1, 'codespaces': {'known-space': 'another-account'}})
        self.api.rows.extend([{'name': 'known-space'}, self.api.row()])
        self.api.details['known-space'] = ValueError('must not read a known Codespace')
        self.ui.answers = ['abandon']
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(self.archives()[-1]['new'], 'new-space')
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'old-space')
        self.assertNotIn(('get', 'known-space'), self.api.calls)
        self.assertFalse(self.api.launches)
        self.assertEqual(self.creates(), [])
        self.assertFalse(any(row[1:3] == ['codespace', 'rebuild'] for row in self.api.gh.calls))

    def test_lost_reply_adopts_before_offering_abandon(self):
        self.active()
        self.api.rows.append(self.api.row())
        self.ui.answers = ['abandon']
        self.assertEqual(self.run_replace(), 0)
        archived = self.archives()[-1]
        self.assertEqual((archived['phase'], archived['new'], archived['ended']),
                         ('created', 'new-space', 'abandoned'))
        self.assertIn('new-space stays listed. Check and delete it with task work:cs:delete -- --codespace new-space.',
                      self.output.getvalue())
        self.assertNotIn('nothing was created', self.output.getvalue())
        self.assertFalse(self.api.launches)
        self.assertEqual(self.creates(), [])

    def test_lost_reply_row_choice_precedes_continue_or_abandon(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                self.active()
                self.api.rows = [self.api.old, self.api.row()]
                row = {'name': 'other-space'} if uncertain else self.api.row('other-space')
                self.api.rows.append(row)
                self.api.details['other-space'] = row
                self.ui.calls.clear()
                self.ui.answers = ['cancel']
                self.assertEqual(self.run_replace(), 1)
                self.assertEqual(len(self.ui.calls), 1)
                self.assertEqual(self.ui.calls[0][1][-1], 'cancel')
                self.assertNotIn('abandon', self.ui.calls[0][1])
                self.assertEqual(replacing.read(self.state)['phase'], 'create-requested')
                self.assertFalse(self.api.launches)
                self.assertEqual(self.creates(), [])

    def test_lost_reply_proves_none_before_abandon_or_typed_create(self):
        self.active()
        choose = self.ui.choose
        def after_recheck(title, options):
            self.assertGreaterEqual(sum(self.clock.waits), 120)
            self.assertTrue(any(call[0] == 'existing' for call in self.api.calls))
            self.assertFalse(self.prompts)
            return choose(title, options)
        with patch.object(self.ui, 'choose', side_effect=after_recheck):
            self.ui.answers = ['abandon']
            self.assertEqual(self.run_replace(), 0)
        self.assertIsNone(self.archives()[-1]['new'])
        self.assertIn('nothing was created', self.output.getvalue())
        self.assertEqual(self.creates(), [])
        self.active()
        self.clock.waits.clear()
        self.ui.answers, self.answers = ['continue'], ['create']
        with patch.object(self.ui, 'choose', side_effect=after_recheck):
            self.assertEqual(self.run_replace(), 0)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.creates()), 1)

    def test_lost_reply_unreadable_inventory_or_other_account_never_offers_abandon(self):
        for failure in ('incomplete', 'unreadable', 'account'):
            with self.subTest(failure=failure):
                self.active()
                self.api.account = 'another' if failure == 'account' else 'owner'
                error = OSError('unreadable inventory') if failure == 'unreadable' else ValueError('incomplete inventory')
                self.api.inventory = lambda: (_ for _ in ()).throw(error)
                self.ui.calls.clear()
                self.ui.answers = ['abandon']
                self.assertEqual(self.run_replace(), 1)
                self.assertFalse(self.ui.calls)
                self.assertEqual(replacing.read(self.state)['phase'], 'create-requested')
                self.assertFalse(self.archives())
                self.assertFalse(self.prompts)
                self.assertEqual(self.creates(), [])

    def test_a_competing_creation_is_a_choice_not_an_adoption(self):
        self.active()
        self.api.rows += [self.api.row(), self.api.row('competing-space')]
        self.valid_launch()
        self.ui.answers = [1, 'continue']
        self.cutover_ok = False
        self.assertEqual(self.run_replace(), 1)
        self.assertEqual(self.api.launches[0][1]['resume'], 'competing-space')
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.ui.calls[0][1][-1], 'cancel')

    def test_an_uncertain_row_is_never_none(self):
        self.active()
        self.api.rows += [{'name': 'uncertain-space', 'repository': {'full_name': 'owner/project'}}]
        self.api.details['uncertain-space'] = self.api.rows[-1]
        for malformed in (False, True):
            if malformed:
                self.api.rows[-1] = self.api.row('uncertain-space', git_status={'ref': 123}, machine={'name': 456})
                self.api.details['uncertain-space'] = self.api.rows[-1]
            self.ui.answers = ['cancel']
            self.assertEqual(self.run_replace(), 1)
            self.assertEqual(replacing.read(self.state)['phase'], 'create-requested')
            self.assertIn('uncertain-space cannot be fully read from GitHub', self.output.getvalue())
            self.assertEqual(len(self.ui.calls[-1][1]), 2)
            self.assertFalse(self.prompts)
        self.api.details['uncertain-space'] = self.api.row('uncertain-space', git_status={'ref': 'other'})
        self.assertEqual(self.run_replace(), 1)
        self.assertTrue(self.prompts)
        self.assertEqual(self.creates(), [])

    def test_a_known_nonmatch_with_a_missing_field_is_excluded(self):
        self.active()
        for key, value in (('git_status', {'ref': 'other'}), ('machine', {'name': 'other'}),
                           ('created_at', '1970-01-01T00:00:00Z')):
            self.api.rows.append(dict(name='nonmatch-' + key.replace('_', '-'),
                                      repository={'full_name': 'owner/project'}, **{key: value}))
        self.answers = ['create']
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(len(self.creates()), 1)
        self.assertGreaterEqual(sum(self.clock.waits), 120)
        self.assertEqual(len(self.ui.calls), 1)

    def test_a_late_first_codespace_is_named_after_a_second_create(self):
        original = self.active()['request']
        self.answers = ['create']
        self.api.create_hook = lambda: self.api.rows.append(self.api.row('late-space'))
        self.assertEqual(self.run_replace(), 0)
        value = self.archives()[0]
        self.assertEqual(value['request']['at'], original['at'])
        self.assertEqual(value['request']['before'], original['before'])
        self.assertEqual(len(value['request']['retries']), 1)
        self.assertIn('Also new since the create request: late-space', self.output.getvalue())

    def test_a_preflight_failure_on_the_second_create_keeps_the_first_request(self):
        original = self.active()['request']
        self.answers = ['create']
        self.api.preflight_failure = True
        self.assertEqual(self.run_replace(), 1)
        self.assertEqual(replacing.read(self.state)['request'], original)
        self.assertEqual(self.creates(), [])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn('preflight failed', self.output.getvalue())

    def test_an_incomplete_inventory_never_offers_create(self):
        self.active()
        self.api.inventory = lambda: (_ for _ in ()).throw(ValueError('incomplete inventory'))
        self.assertEqual(self.run_replace(), 1)
        self.assertFalse(self.prompts)
        self.assertEqual(self.creates(), [])

    def test_a_failure_before_the_post_is_not_a_lost_reply(self):
        self.api.preflight_failure = True
        self.assertEqual(self.run_replace(), 1)
        self.assertEqual(replacing.read(self.state)['phase'], 'inspected')
        self.assertFalse(self.clock.waits)
        self.assertEqual(self.creates(), [])

    def test_none_found_asks_before_creating_again(self):
        self.active()
        self.assertEqual(self.run_replace(), 1)
        self.assertEqual(self.creates(), [])
        self.assertGreaterEqual(sum(self.clock.waits), 120)
        self.answers = ['create']
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(len(self.creates()), 1)

    def test_several_found_asks_which_to_keep(self):
        self.active()
        self.api.rows += [self.api.row(), self.api.row('other-space')]
        self.ui.answers = ['cancel']
        self.assertEqual(self.run_replace(), 1)
        self.assertEqual(len(self.ui.calls[-1][1]), 3)
        self.assertEqual(self.creates(), [])
        self.assertFalse(self.api.launches)

    def test_another_account_stops_reconciliation(self):
        self.active()
        self.api.account = 'another'
        self.assertEqual(self.run_replace(), 1)
        self.assertIn('started as owner, but gh is signed in as another', self.output.getvalue())
        self.assertEqual(self.creates(), [])
        self.assertFalse(any(call[0] == 'existing' for call in self.api.calls))

    def test_old_receipt_comes_from_olds_own_branch(self):
        old = dict(codespace='old-space', repository='owner/project', branch='feature/topic', commit='c' * 40)
        other = dict(codespace='other-space', repository='owner/project', branch='main', commit='d' * 40)
        for value in (old, other):
            path = receipt_path(self.root / 'state', 'owner/project', value['branch'])
            atomic_write(path, json.dumps(value).encode())
        self.assertEqual(self.run_replace(), 0)
        value = self.archives()[0]
        self.assertEqual(value['old_receipt'], old)
        self.assertEqual(value['branch_receipt'], other)
        self.api.old['git_status']['ref'] = 'renamed'
        self.save('scratch.json', dict(read_json(self.state / 'scratch.json'), codespace='old-space'))
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(self.archives()[-1]['old_receipt'], old)

    def test_each_phase_resumes_on_rerun(self):
        for phase in replacing.PHASES:
            with self.subTest(phase=phase):
                paired = 'old-space' if replacing.PHASES.index(phase) < replacing.PHASES.index('moved') else 'new-space'
                self.save('scratch.json', dict(read_json(self.state / 'scratch.json'), codespace=paired))
                self.active(phase=phase, new=None if phase in ('inspected', 'create-requested') else 'new-space')
                self.api.rows = [self.api.old, self.api.row()]
                self.valid_launch()
                self.steps.clear()
                self.api.launches.clear()
                self.api.gh.calls.clear()
                self.assertEqual(self.run_replace(), 0)
                self.assertIn('A replace of old-space is at', self.output.getvalue())
                self.assertLessEqual(len(self.creates()), 1)
                commands = [argv[4] for argv, _, _ in self.steps]
                if phase == 'offered':
                    self.assertEqual(commands, [])
                elif phase == 'checked':
                    self.assertEqual(commands, ['delete'])
                elif phase == 'signed-in':
                    self.assertEqual(commands, ['replace', 'delete'])
                elif phase == 'moved':
                    self.assertEqual(commands, ['work', 'replace', 'delete'])
        self.assert_each_persisted_phase_survives_an_interruption()

    def test_dotfiles_retry_never_rebuilds(self):
        self.api.gh.fail_personalization = True
        self.assertEqual(self.run_replace(), 1)
        self.assertEqual(replacing.read(self.state)['phase'], 'created')
        self.api.gh.fail_personalization = False
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(len(self.creates()), 1)
        self.assertFalse(any(row[1:3] == ['codespace', 'rebuild'] for row in self.api.gh.calls))

    def test_runtime_steps_run_in_the_projects_own_runtime(self):
        with contextlib.ExitStack() as stack:
            for name in ('scratch.start', 'keeper.start', 'workspace.remote', 'herdr.open_remote'):
                stack.enter_context(patch('devcontainer_setup.project_sync.' + name,
                                          side_effect=AssertionError('in-process runtime effect')))
            self.assertEqual(self.run_replace(), 0)
        self.assertEqual(len(self.steps), 4)
        for argv, kwargs, _ in self.steps:
            self.assertEqual(argv[:4], ['bash', str(self.project / '.devcontainer/scripts/project-sync.sh'),
                                        '--project', str(self.project)])
            self.assertEqual(kwargs['cwd'], self.project)
            self.assertEqual(kwargs['env'], self.env)
            self.assertFalse({'stdin', 'stdout', 'stderr', 'capture_output'} & kwargs.keys())
        # Canonical repository layout uses the fallback wrapper.
        (self.project / '.devcontainer/scripts/project-sync.sh').unlink()
        self.active(phase='moved', new='new-space')
        self.valid_launch()
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(self.steps[-1][0][1], str(self.project / 'scripts/project-sync.sh'))
        self.assert_stale_cutover_receipts_never_advance()

    def test_decline_or_cancel_before_the_move_keeps_old_paired(self):
        self.cutover_ok = False
        self.assertEqual(self.run_replace(), 1)
        value = replacing.read(self.state)
        self.save('retired/old-space.json', {'operation': value['operation']})
        self.ui.answers = ['abandon']
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(read_json(self.state / 'scratch.json')['codespace'], 'old-space')
        self.assertFalse((self.state / 'retired/old-space.json').exists())
        self.assertEqual(self.archives()[0]['ended'], 'abandoned')
        self.assertIn('work:cs:delete -- --codespace new-space', self.output.getvalue())
        self.assertFalse(any('delete' in argv for argv, _, _ in self.steps))
        self.active(phase='inspected')
        self.ui.answers = ['abandon']
        self.assertEqual(self.run_replace(), 0)
        self.assertIn('nothing was created.', self.output.getvalue())

    def test_a_started_move_is_never_abandoned(self):
        value = self.active(phase='ready', new='new-space')
        self.save('retired/old-space.json', {'operation': value['operation']})
        self.save('workspace-pending.json', {'codespace': 'new-space'})
        self.valid_launch()
        self.ui.answers = ['abandon']
        self.assertEqual(self.run_replace(), 0)
        self.assertFalse(self.ui.calls)
        self.assertIsNotNone(self.steps[0][2]['moving'])
        self.assertIn('finishing it.', self.output.getvalue())
        self.assert_failed_latch_write_never_starts_the_cutover()

    def test_a_failed_replay_still_finishes_forward_next_time(self):
        value = self.active(phase='ready', new='new-space')
        self.save('retired/old-space.json', {'operation': value['operation']})
        self.save('workspace-pending.json', {'codespace': 'new-space'})
        self.valid_launch()
        def fail(argv):
            (self.state / 'workspace-pending.json').unlink()
            raise KeyboardInterrupt
        self.runner_hook = fail
        self.assertEqual(self.run_replace(), 1)
        self.assertIsNotNone(replacing.read(self.state)['moving'])
        self.runner_hook = None
        self.assertEqual(self.run_replace(), 0)
        self.assertFalse(self.ui.calls)
        self.assert_cutover_error_records_new_move_facts()

    def test_phases_never_go_back(self):
        for phase, commands in (('signed-in', ['replace', 'delete']), ('checked', ['delete']), ('offered', [])):
            with self.subTest(phase=phase):
                value = self.active(phase=phase, new='new-space', moving=2000.0)
                self.save('retired/old-space.json', {'operation': value['operation']})
                self.save('scratch.json', dict(read_json(self.state / 'scratch.json'), codespace='new-space'))
                self.valid_launch()
                self.steps.clear()
                self.assertEqual(self.run_replace(), 0)
                self.assertEqual([argv[4] for argv, _, _ in self.steps], commands)
        self.assertFalse(self.ui.calls)

    def test_failed_signin_does_not_block_check_or_offer(self):
        self.signin_code = 1
        self.signin_record = dict(codespace='new-space', updated_at=3000,
            tools={key: {'state': state} for key, state in
                   (('codex', 'failed'), ('claude', 'other-method'), ('grok', 'absent'))})
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(self.archives()[0]['signins']['retry'], ['codex'])
        self.assertIn('Not signed in yet on new-space: codex.', self.output.getvalue())
        self.assertTrue(any('delete' in argv for argv, _, _ in self.steps))

    def test_new_check_failure_stops_before_the_offer(self):
        self.check_ok = False
        self.assertEqual(self.run_replace(), 1)
        self.assertEqual(replacing.read(self.state)['phase'], 'signed-in')
        self.assertFalse(any('delete' in argv for argv, _, _ in self.steps))
        self.steps.clear()
        self.check_ok = True
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual([argv[4:6] for argv, _, _ in self.steps],
                         [['replace', 'cutover'], ['replace', 'check'], ['delete', '--codespace']])

    def test_a_delete_of_either_name_ends_the_journal(self):
        for name in ('old-space', 'new-space'):
            self.active(new='new-space')
            self.save('deletions/' + name + '.json', {'phase': 'deleted'})
            self.assertEqual(self.run_replace(), 0)
            self.assertFalse(self.api.calls)
            self.assertEqual(self.archives()[-1]['ended'], 'deleted')
            (self.state / 'deletions' / (name + '.json')).unlink()

    def test_declined_delete_still_completes(self):
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(self.archives()[0]['ended'], 'completed')
        self.assertIn('old-space is kept; task work offers its delete.', self.output.getvalue())
        self.assertIn('Connect with task work:cs.', self.output.getvalue())

    def test_deleted_old_with_cleanup_pending_is_not_reported_as_kept(self):
        def deleted(argv):
            if 'delete' in argv:
                self.save('deletions/old-space.json', {'phase': 'deleted'})
                return subprocess.CompletedProcess(argv, 1)
            return None
        self.runner_hook = deleted
        self.assertEqual(self.run_replace(), 0)
        self.assertEqual(self.archives()[-1]['ended'], 'completed')
        self.assertIn('Replaced old-space with new-space.', self.output.getvalue())
        self.assertNotIn('old-space is kept', self.output.getvalue())

    def test_nothing_gh_printed_reaches_a_journal_or_receipt(self):
        for step in ('get', 'machines', 'user', 'existing', 'launch'):
            self.api.failure = step
            self.assertEqual(self.run_replace(), 1)
            for folder in ('replace.json', 'replacements', 'replace-receipts'):
                path = self.state / folder
                paths = path.glob('*.json') if path.is_dir() else [path] if path.exists() else []
                for item in paths:
                    self.assertNotIn('private.invalid', item.read_text())
                    self.assertNotIn('ghp_TEST_ONLY', item.read_text())
            (self.state / 'replace.json').unlink(missing_ok=True)
        self.assertEqual(self.creates(), [])
        self.api.failure = None
        self.assertEqual(self.run_replace(), 0)
        for folder in ('replacements', 'replace-receipts'):
            path = self.state / folder
            self.assertTrue(path.is_dir(), folder + ' must exist after the runtime steps')
            files = list(path.glob('*.json'))
            self.assertTrue(files, folder + ' must contain records to inspect')
            for item in files:
                self.assertNotIn('private.invalid', item.read_text())
                self.assertNotIn('ghp_TEST_ONLY', item.read_text())

    def test_check_receipt_extra_fields_are_not_copied_into_the_journal(self):
        self.active(phase='signed-in', new='new-space')
        self.valid_launch()
        receipts = []
        def check(argv):
            if 'check' not in argv:
                return None
            path = Path(argv[argv.index('--receipt') + 1])
            value = dict(schema=1, operation=replacing.read(self.state)['operation'], step='check',
                         codespace='new-space', ok=True, external_output='https://private.invalid/ghp_TEST_ONLY')
            atomic_write(path, json.dumps(value).encode())
            receipts.append(path)
            return subprocess.CompletedProcess(argv, 0)
        self.runner_hook = check
        self.assertEqual(self.run_replace(), 0)
        self.assertIn('external_output', read_json(receipts[0]))
        saved = self.archives()[-1]['check']
        self.assertTrue(saved['ok'])
        self.assertNotIn('external_output', saved)
        self.assertNotIn('private.invalid', json.dumps(self.archives()[-1]))

    def test_launch_receipt_extra_fields_are_not_copied_into_the_journal(self):
        old = dict(codespace='old-space', repository='owner/project', branch='feature/topic', commit='c' * 40)
        branch = dict(codespace='other-space', repository='owner/project', branch='main', commit='b' * 40)
        for value in (old, branch):
            path = receipt_path(self.root / 'state', 'owner/project', value['branch'])
            atomic_write(path, json.dumps(dict(value, external_output='https://private.invalid/ghp_TEST_ONLY')).encode())
        self.assertEqual(self.run_replace(), 0)
        saved = self.archives()[-1]
        self.assertNotIn('external_output', saved['old_receipt'])
        self.assertNotIn('external_output', saved['branch_receipt'])
        self.assertEqual(saved['old_receipt'], old)
        self.assertEqual(saved['branch_receipt'], branch)
        self.assertNotIn('private.invalid', json.dumps(saved))

    def test_another_replace_running_refuses(self):
        with locked(self.state / 'replace.lock'):
            self.assertEqual(self.run_replace(), 1)
        self.assertIn('Another replace for this project is running', self.output.getvalue())
        self.assertFalse(self.api.calls)

    def test_inside_a_codespace_without_a_terminal_or_with_a_token_refuses(self):
        for key in ('CODESPACES', 'GH_TOKEN', 'GITHUB_TOKEN'):
            self.env[key] = 'true' if key == 'CODESPACES' else 'fixture'
            self.assertEqual(self.run_replace(), 1)
            self.env.pop(key)
        self.assertEqual(self.run_replace(tty=False), 1)
        self.assertFalse(self.api.calls)
        self.assertFalse((self.state / 'replace.json').exists())
        self.assert_unpaired_or_foreign_pairing_never_calls_api()

    def test_test_branch_override_comes_only_from_the_environment(self):
        self.env['SETUP_REPLACE_TEST_BRANCH'] = 'trial'
        self.api.gh.remote_branch = self.api.gh.api_ref = 'trial'
        self.assertEqual(self.run_replace(), 0)
        self.assertFalse(self.default.called)
        argv = self.creates()[0]
        self.assertEqual(argv[argv.index('--branch') + 1], 'trial')
        self.assertEqual(self.archives()[0]['branch'], 'trial')

    def assert_unpaired_or_foreign_pairing_never_calls_api(self):
        config = read_json(self.state / 'scratch.json')
        for change in (dict(codespace=None), dict(identity='github.com/other/project'), dict(root='/other')):
            self.save('scratch.json', dict(config, **change))
            self.assertEqual(self.run_replace(), 1)
            self.assertFalse(self.api.calls)

    def assert_failed_latch_write_never_starts_the_cutover(self):
        self.steps.clear()
        value = self.active(phase='ready', new='new-space')
        self.save('retired/old-space.json', {'operation': value['operation']})
        self.save('workspace-pending.json', {'codespace': 'new-space'})
        with patch.object(replacing, 'write', side_effect=OSError('write failed')):
            self.assertEqual(self.run_replace(), 1)
        self.assertFalse(self.steps)
        self.assertIsNone(replacing.read(self.state)['moving'])
        self.assertEqual(replacing.read(self.state)['phase'], 'ready')

    def assert_cutover_error_records_new_move_facts(self):
        value = self.active(phase='ready', new='new-space')
        def fail(argv):
            self.save('retired/old-space.json', {'operation': value['operation']})
            self.save('scratch-retarget.json', {'candidate': {'codespace': 'new-space'}})
            raise OSError('child failed')
        self.runner_hook = fail
        self.assertEqual(self.run_replace(), 1)
        self.assertIsNotNone(replacing.read(self.state)['moving'])
        self.assertEqual(replacing.read(self.state)['phase'], 'ready')

    def assert_stale_cutover_receipts_never_advance(self):
        self.steps.clear()
        value = self.active(phase='ready', new='new-space')
        path = replacing.receipt_path(self.state, value['operation'], 'cutover')
        atomic_write(path, json.dumps(dict(schema=1, operation=value['operation'], step='cutover',
                                          old='old-space', new='new-space', moved=True)).encode())
        self.runner_hook = lambda argv: subprocess.CompletedProcess(argv, 1)
        self.assertEqual(self.run_replace(), 1)
        self.assertEqual(replacing.read(self.state)['phase'], 'ready')
        self.assertEqual(len(self.steps), 1)

    def test_check_requires_a_fresh_matching_success_receipt_and_launch_validation(self):
        for change in ('missing', 'wrong-operation', 'wrong-name', 'nonzero', 'setup', 'malformed'):
            with self.subTest(change=change):
                self.steps.clear()
                self.active(phase='signed-in', new='new-space')
                self.valid_launch()
                if change == 'setup':
                    receipt_path(self.root / 'state', 'owner/project', 'main').unlink()
                def check(argv):
                    path = Path(argv[argv.index('--receipt') + 1])
                    result = dict(schema=1, step='check', operation=replacing.read(self.state)['operation'],
                                  codespace='new-space', ok=True)
                    if change == 'wrong-operation':
                        result['operation'] = 'ffffffffffff'
                    if change == 'wrong-name':
                        result['codespace'] = 'other-space'
                    if change != 'missing':
                        atomic_write(path, b'{' if change == 'malformed' else json.dumps(result).encode())
                    return subprocess.CompletedProcess(argv, 1 if change == 'nonzero' else 0)
                self.runner_hook = check
                self.assertEqual(self.run_replace(), 1)
                self.assertEqual(replacing.read(self.state)['phase'], 'signed-in')
                self.assertFalse(any('delete' in argv for argv, _, _ in self.steps))

    def test_unreadable_or_stale_signins_are_unknown_and_do_not_block(self):
        for kind in ('missing', 'malformed', 'stale', 'other-space'):
            with self.subTest(kind=kind):
                self.active(phase='moved', new='new-space')
                self.valid_launch()
                (self.state / 'signins.json').unlink(missing_ok=True)
                def signin(argv):
                    if '--signin-all' not in argv:
                        return None
                    if kind == 'malformed':
                        atomic_write(self.state / 'signins.json', b'{')
                    if kind in ('stale', 'other-space'):
                        self.save('signins.json', dict(codespace='other-space' if kind == 'other-space' else 'new-space',
                                  updated_at=0 if kind == 'stale' else self.clock.now(), tools={}))
                    return subprocess.CompletedProcess(argv, 1)
                self.runner_hook = signin
                self.assertEqual(self.run_replace(), 0)
                self.assertEqual(self.archives()[-1]['signins']['retry'], ['unknown'])

    def assert_each_persisted_phase_survives_an_interruption(self):
        real_write = replacing.write
        for phase in replacing.PHASES:
            with self.subTest(phase=phase):
                (self.state / 'replace.json').unlink(missing_ok=True)
                self.save('scratch.json', dict(read_json(self.state / 'scratch.json'), codespace='old-space'))
                self.api.rows = [self.api.old]
                self.api.gh.calls.clear()
                self.api.launches.clear()
                self.ui.answers.clear()
                self.answers = ['create']
                def interrupt(state, value):
                    real_write(state, value)
                    if value['phase'] == phase:
                        raise KeyboardInterrupt
                with patch.object(replacing, 'write', side_effect=interrupt):
                    self.assertEqual(self.run_replace(), 1)
                self.assertEqual(replacing.read(self.state)['phase'], phase)
                self.assertEqual(self.run_replace(), 0)
                self.assertLessEqual(len(self.creates()), 1)
