import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]


class WizardCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(Path(self.tmp.name) / 'config'),
                        XDG_CACHE_HOME=str(Path(self.tmp.name) / 'cache'))
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test User')
        self.git('config', 'user.email', 'test@example.com')
        (self.root / 'package.json').write_text('{"engines":{"node":"24"}}\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Initial')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'devcontainer_setup', '--project', str(self.root), *args],
                              cwd=SOURCE, env=self.env, capture_output=True, text=True)

    def test_legacy_agent_opt_out_is_ignored_without_an_agent_prompt(self):
        from devcontainer_setup.__main__ import defaults, _select
        from devcontainer_setup.discovery import scan
        preferences = {'agentic_tools': False, 'apply_dotfiles': False, 'dotfiles_repo': ''}
        chosen = defaults(scan(self.root), preferences, {})
        self.assertIs(chosen['agents'], True)
        self.assertIs(defaults(scan(self.root), preferences, {'selections': dict(chosen, agents=False)})['agents'], True)
        class UI:
            def multi(self, title, options, selected): return selected
            def confirm(self, title, default=True):
                if 'Claude' in title or 'Codex' in title or 'OpenCode' in title:
                    raise AssertionError('Agents must not have an opt-out prompt')
                return False
        chosen = _select(UI(), scan(self.root), chosen, preferences)
        self.assertIs(chosen['agents'], True)

    def test_launch_choices_work_before_local_branch_is_published(self):
        from devcontainer_setup.__main__ import _launch_options
        from devcontainer_setup.codespaces import Codespaces
        self.git('switch', '-c', 'docs/local-only')
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        before = self.git('rev-parse', 'HEAD')
        calls = []

        def github(args, **kwargs):
            calls.append(args)
            if args[1] != 'api':
                self.fail(f'Machine selection must not push or allocate: {args}')
            if args[2].endswith('/machines'):
                if any(arg.startswith('ref=') for arg in args):
                    return subprocess.CompletedProcess(args, 1, '', "The 'ref' provided was invalid (HTTP 400)")
                payload = {'machines': [{'name': 'standardLinux32gb', 'cpus': 4,
                            'memory_in_bytes': 16 * 1024**3, 'storage_in_bytes': 32 * 1024**3}]}
            else:
                self.assertEqual(args[2], 'user/codespaces?per_page=100')
                payload = [{'codespaces': []}]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')

        case = self
        class UI:
            def choose(self, title, options, selected=None):
                return 'Push and open a Codespace in the browser' if title == 'After setup' else selected or options[0]

            def confirm(self, title, default=True):
                case.assertNotEqual(title, 'Continue with commit only?',
                                    'Unpublished branches must still offer Codespaces launch')
                return default

        options = _launch_options(UI(), Codespaces(run=github), self.root, 'docs/local-only', None)
        self.assertEqual(options['machine'], 'standardLinux32gb')
        self.assertEqual(options['client'], 'browser')
        self.assertEqual(options['idle_timeout'], '30m')
        self.assertFalse(any(arg.startswith('ref=') for call in calls for arg in call))
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_existing_taskfile_variants_keep_real_task_operations(self):
        import shutil
        from devcontainer_setup.__main__ import integrate_taskfile
        self.assertIsNotNone(shutil.which('task'), 'real go-task required')
        for name in ('Taskfile.yml', 'Taskfile.yaml', 'taskfile.yml', 'taskfile.yaml',
                     'Taskfile.dist.yml', 'Taskfile.dist.yaml', 'taskfile.dist.yml', 'taskfile.dist.yaml'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / name).write_text("version: '3'\ntasks:\n  up:\n    cmds: ['echo existing']\n  test:\n    cmds: ['echo tests']\n")
                files = {}
                integrate_taskfile(root, files)
                self.assertIn(name, files)
                for relative, data in files.items():
                    (root / relative).write_bytes(data)
                result = subprocess.run(['task', '--list-all', '--json'], cwd=root, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                names = {task['name'] for task in json.loads(result.stdout)['tasks']}
                self.assertTrue({'up', 'test'} <= names, names)
                self.assertEqual([path.name for path in root.glob('*') if 'taskfile' in path.name.lower()], [name])

    def test_task_include_mapping_detection_and_atomic_refusal(self):
        from devcontainer_setup.__main__ import integrate_taskfile
        unsupported = (
            "includes: {owner: owner.yml}\ntasks:\n  owner: {}\n",
            "includes:\n  'devcontainer': other.yml\ntasks:\n  owner: {}\n",
            "includes:\n  devcontainer: other.yml\n  'devcontainer': .devcontainer/Taskfile.yml\ntasks:\n",
            "includes:\n  owner: owner.yml\nincludes:\n  other: other.yml\ntasks:\n",
            'includes:\n  owner: "{{.OWNER}}"\ntasks:\n  owner: {}\n',
            "includes:\n  owner:\n    taskfile: '{{.OWNER}}'\n    optional: true\ntasks:\n  owner: {}\n",
        )
        for original in unsupported:
            with self.subTest(original=original):
                (self.root / 'Taskfile.yml').write_text(original)
                files = {'sentinel': b'unchanged'}
                with self.assertRaises(ValueError):
                    integrate_taskfile(self.root, files)
                self.assertEqual(files, {'sentinel': b'unchanged'})
                self.assertEqual((self.root / 'Taskfile.yml').read_text(), original)
        original = ('version: "3"\nincludes:\n    "devcontainer":\n'
                    '        taskfile: ".devcontainer/Taskfile.yml"\n'
                    '        optional: true\ntasks:\n    "owner": {cmds: [echo OWNER]}\n')
        (self.root / 'Taskfile.yml').write_text(original)
        files = {}
        integrate_taskfile(self.root, files)
        self.assertNotIn('Taskfile.yml', files)

    def test_package_manager_pin_survives_defaults_and_renders(self):
        from devcontainer_setup.__main__ import defaults
        from devcontainer_setup.discovery import scan
        from devcontainer_setup.render import render
        (self.root / 'package.json').write_text('{"packageManager":"pnpm@9.15.4","engines":{"node":"24","pnpm":"9.15.4"}}')
        chosen = defaults(scan(self.root), {'agentic_tools': False, 'apply_dotfiles': False}, {})
        self.assertEqual(chosen['package_manager_versions'], {'node': '9.15.4'})
        config = json.loads(render(self.root, SOURCE, chosen, 'a' * 40)['.devcontainer/devcontainer.json'])
        self.assertEqual(config['features']['ghcr.io/devcontainers/features/node:1']['pnpmVersion'], '9.15.4')

    def test_manager_version_conflict_prompts_with_both_declarations(self):
        import contextlib
        import io
        from devcontainer_setup.__main__ import defaults, _select
        from devcontainer_setup.discovery import scan
        (self.root / 'package.json').write_text('{"packageManager":"pnpm@9.15.4"}')
        (self.root / 'nested').mkdir()
        (self.root / 'nested/package.json').write_text('{"packageManager":"pnpm@10.2.1"}')
        found = scan(self.root)
        preferences = {'agentic_tools': False, 'apply_dotfiles': False, 'dotfiles_repo': ''}
        class UI:
            prompts = []
            def multi(self, title, options, selected): return selected
            def confirm(self, title, default=True): return False
            def input(self, title, value=''):
                self.prompts.append(title)
                return '9.15.4'
        ui, output = UI(), io.StringIO()
        with contextlib.redirect_stdout(output):
            chosen = _select(ui, found, defaults(found, preferences, {}), preferences)
        self.assertTrue(any('pnpm' in prompt and 'version' in prompt for prompt in ui.prompts))
        self.assertIn('9.15.4', output.getvalue())
        self.assertIn('10.2.1', output.getvalue())
        self.assertEqual(chosen['package_manager_versions'], {'node': '9.15.4'})

    def test_fresh_taskfile_has_working_lifecycle_aliases(self):
        from devcontainer_setup.__main__ import integrate_taskfile
        files = {}
        integrate_taskfile(self.root, files)
        (self.root / 'Taskfile.yml').write_bytes(files['Taskfile.yml'])
        (self.root / '.devcontainer').mkdir()
        (self.root / '.devcontainer/Taskfile.yml').write_bytes((SOURCE / 'templates/Taskfile.yml').read_bytes())
        for operation in ('up', 'down', 'status', 'build'):
            result = subprocess.run(['task', '--dry', operation], cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_unresolved_manager_versions_cannot_silently_dry_run_default(self):
        (self.root / 'package.json').write_text('{"packageManager":"pnpm@9.15.4"}')
        (self.root / 'nested').mkdir()
        (self.root / 'nested/package.json').write_text('{"packageManager":"pnpm@10.2.1"}')
        result = self.cli('--dry-run')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('version', result.stderr)
        self.assertNotIn('12.11.2', result.stdout)

    def test_ambiguous_taskfiles_fail_without_changing_either(self):
        from devcontainer_setup.__main__ import integrate_taskfile
        for name in ('Taskfile.yml', 'Taskfile.yaml'):
            (self.root / name).write_text("version: '3'\n")
        files = {}
        with self.assertRaisesRegex(ValueError, 'Multiple supported Taskfiles'):
            integrate_taskfile(self.root, files)
        self.assertEqual(files, {})

    def test_scan_reports_node_and_python_without_writing(self):
        result = self.cli('--scan')
        self.assertEqual(result.returncode, 0, result.stderr)
        scan = json.loads(result.stdout)
        self.assertEqual(scan['runtimes']['node']['version'], '24')
        self.assertTrue(scan['runtimes']['python']['selected'])
        self.assertFalse((self.root / '.devcontainer').exists())
        self.assertFalse(Path(self.env['XDG_CONFIG_HOME']).exists())

    def test_dry_run_shows_generated_diff_without_mutating_checkout(self):
        before = self.git('rev-parse', 'HEAD')
        result = self.cli('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('devcontainer.json', result.stdout)
        self.assertIn('python:1', result.stdout)
        self.assertNotIn('ruby:1', result.stdout)
        self.assertFalse((self.root / '.devcontainer').exists())
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)

    def test_noninteractive_mutation_is_refused(self):
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('terminal', result.stderr.lower())
        self.assertFalse((self.root / '.devcontainer').exists())

    def test_invalid_configuration_check_fails(self):
        result = self.cli('--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('devcontainer', result.stderr.lower())

    def test_upgrade_handoff_uses_fetched_immutable_runner_without_repeating_fetch(self):
        from devcontainer_setup.__main__ import upgrade_handoff
        release = Path(self.tmp.name) / ('b' * 40)
        with patch('devcontainer_setup.__main__.fetch_release', return_value=(release, 'b' * 40)) as fetch:
            with patch('devcontainer_setup.__main__.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(upgrade_handoff(self.root, 'a' * 40, 'existing-space'), 0)
        fetch.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['bash', str(release / 'scripts/setup.sh')])
        self.assertIn('--upgrade-loaded', command)
        self.assertIn('--resume', command)
        self.assertEqual(run.call_args.kwargs['env']['DEVCONTAINER_SETUP_REF'], 'b' * 40)

    def test_advanced_selection_can_override_runtime_versions_and_ports(self):
        from devcontainer_setup.__main__ import _select, defaults
        from devcontainer_setup.discovery import scan
        found = scan(self.root)
        preferences = {'apply_dotfiles': False, 'dotfiles_repo': '', 'agentic_tools': True}
        class UI:
            def multi(self, title, options, selected):
                return selected
            def confirm(self, title, default=True):
                return title == 'Advanced environment options?' or title == 'Include Claude Code and Codex?'
            def input(self, title, value=''):
                if title == 'node version':
                    return '26'
                if title.startswith('Forwarded ports'):
                    return '3570,8787'
                return value
        result = _select(UI(), found, defaults(found, preferences, {}), preferences)
        self.assertEqual(result['runtimes']['node'], '26')
        self.assertEqual(result['ports'], [3570, 8787])

    def test_new_dotfiles_repository_is_checked_without_shell_execution(self):
        from devcontainer_setup.__main__ import repository_access
        with patch('devcontainer_setup.__main__.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(repository_access('emsilva/dotfiles'))
        self.assertEqual(run.call_args.args[0], ['git', 'ls-remote', '--exit-code', '--',
                                               'https://github.com/emsilva/dotfiles.git', 'HEAD'])
        self.assertEqual(run.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')

    def test_dotfiles_https_url_cannot_persist_credentials(self):
        from devcontainer_setup.__main__ import dotfiles_repository
        with self.assertRaises(ValueError):
            dotfiles_repository('https://personal-token@github.com/owner/dotfiles.git')


    def test_reuse_reviews_actual_settings_without_creation_prompts(self):
        from devcontainer_setup.__main__ import _launch_options
        from devcontainer_setup.codespaces import Codespaces
        import contextlib
        import io
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        resource = {'name': 'existing-space', 'repository': {'full_name': 'owner/project'},
                    'git_status': {'ref': 'main'}, 'machine': {'name': 'expensive-8-core'},
                    'idle_timeout_minutes': 60, 'retention_period_minutes': 4320, 'location': 'WestEurope'}
        for resume in ('existing-space', None):
            with self.subTest(resume=resume):
                calls = []
                def github(args, **kwargs):
                    calls.append(args)
                    self.assertNotIn('/machines', args[2])
                    payload = ([{'codespaces': [resource]}] if '?' in args[2] else resource)
                    return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
                class UI:
                    def choose(self, title, options, selected=None):
                        if title == 'After setup': return 'Push and open a Codespace in the browser'
                        if title == 'Existing codespaces for this branch': return 'Reuse existing-space'
                        raise AssertionError(f'Unexpected creation prompt: {title}')
                    def confirm(self, title, default=True): return True
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    options = _launch_options(UI(), Codespaces(run=github), self.root, 'main', resume)
                self.assertEqual(options['machine'], 'expensive-8-core')
                self.assertEqual(options['idle_timeout'], '60m')
                self.assertEqual(options['location'], 'WestEurope')
                self.assertEqual(options['retention'], '4320m')
                self.assertIn('expensive-8-core', output.getvalue())
                self.assertIn('WestEurope', output.getvalue())


    def test_host_launcher_isolates_project_and_ambient_python_imports(self):
        (self.root / 'devcontainer_setup.py').write_text('print("PROJECT_MODULE_EXECUTED")\n')
        poison = Path(self.tmp.name) / 'poison'
        poison.mkdir()
        (poison / 'json.py').write_text('raise RuntimeError("AMBIENT_MODULE_EXECUTED")\n')
        for ambient in ('', str(poison)):
            if ambient:
                (self.root / 'devcontainer_setup.py').unlink()
            with self.subTest(ambient=ambient):
                result = subprocess.run(['bash', str(SOURCE / 'scripts/setup.sh'), '--scan'],
                    cwd=self.root, env=dict(self.env, PYTHONPATH=ambient), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('PROJECT_MODULE_EXECUTED', result.stdout)
                self.assertNotIn('AMBIENT_MODULE_EXECUTED', result.stderr)
                self.assertIn('runtimes', json.loads(result.stdout))


    def test_launch_discovery_failures_offer_commit_only_fallback(self):
        import contextlib
        import io
        from devcontainer_setup.__main__ import _launch_options
        from devcontainer_setup.codespaces import Codespaces
        from devcontainer_setup.ui import Cancelled
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        for failure in ('missing-gh', 'forbidden-existing', 'forbidden-resume',
                        'missing-machine-metadata', 'forbidden-machines'):
            for accept_fallback in (True, False):
                with self.subTest(failure=failure, accept_fallback=accept_fallback):
                    prompts = []
                    def github(args, **kwargs):
                        if failure == 'missing-gh':
                            raise FileNotFoundError('gh')
                        if failure == 'missing-machine-metadata':
                            return subprocess.CompletedProcess(args, 0, '{}', '')
                        if failure == 'forbidden-machines' and '?' in args[2]:
                            return subprocess.CompletedProcess(args, 0, '[{"codespaces": []}]', '')
                        return subprocess.CompletedProcess(args, 1, '', 'No Codespaces permission (HTTP 403)')
                    class UI:
                        def choose(self, title, options, selected=None):
                            if title == 'After setup': return 'Push and open a Codespace in the browser'
                            raise AssertionError(f'Unexpected launch prompt: {title}')
                        def confirm(self, title, default=True):
                            prompts.append(title)
                            return accept_fallback
                    resume = ('existing-space' if failure in ('forbidden-resume',
                              'missing-machine-metadata') else None)
                    error_output = io.StringIO()
                    with contextlib.redirect_stderr(error_output):
                        if accept_fallback:
                            self.assertIsNone(_launch_options(UI(), Codespaces(run=github), self.root,
                                                              'main', resume))
                        else:
                            with self.assertRaises(Cancelled):
                                _launch_options(UI(), Codespaces(run=github), self.root, 'main', resume)
                    self.assertEqual(prompts, ['Continue with commit only?'])
                    self.assertIn('Launch unavailable:', error_output.getvalue())
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_missing_github_origin_offers_commit_only_fallback(self):
        import contextlib
        import io
        from devcontainer_setup.__main__ import _launch_options
        from devcontainer_setup.codespaces import Codespaces
        prompts = []
        class UI:
            def choose(self, title, options, selected=None):
                return 'Push and open a Codespace in the browser'
            def confirm(self, title, default=True):
                prompts.append(title)
                return True
        def github(args, **kwargs):
            raise AssertionError(f'No GitHub call without an origin: {args}')
        error_output = io.StringIO()
        with contextlib.redirect_stderr(error_output):
            self.assertIsNone(_launch_options(UI(), Codespaces(run=github), self.root, 'main', None))
        self.assertEqual(prompts, ['Continue with commit only?'])
        self.assertIn('git remote add origin', error_output.getvalue())

    def test_after_setup_choice_leads_with_the_codespace_and_defaults_to_it(self):
        from devcontainer_setup.__main__ import _launch_options
        from devcontainer_setup.codespaces import Codespaces
        menus = []
        class UI:
            def choose(self, title, options, selected=None):
                menus.append((options, selected))
                return next(option for option in options if option.startswith('Commit'))
        def github(args, **kwargs):
            raise AssertionError(f'Commit only makes no GitHub call: {args}')
        self.assertIsNone(_launch_options(UI(), Codespaces(run=github), self.root, 'main', None))
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        self.assertIsNone(_launch_options(UI(), Codespaces(run=github), self.root, 'main', None))
        (options, without_origin), (_, with_origin) = menus
        self.assertEqual([option for option in options if option.startswith('Commit')], ['Commit only (no Codespace)'])
        self.assertTrue(all('Codespace' in option for option in options))
        self.assertEqual(without_origin, 'Commit only (no Codespace)')
        self.assertEqual(with_origin, 'Push and open a Codespace in the browser')

    def test_gum_refuses_a_default_it_would_split_at_commas(self):
        from devcontainer_setup.ui import Gum
        gum = Gum.__new__(Gum)
        gum.binary = '/usr/bin/false'
        with self.assertRaisesRegex(ValueError, 'comma'):
            gum.choose('After setup', ['Commit only, no Codespace', 'Other'], 'Commit only, no Codespace')

    def test_commit_only_ending_says_how_to_get_a_codespace(self):
        import contextlib
        import io
        from devcontainer_setup.__main__ import setup_continuity
        for inside, expected in (('', 'task devcontainer:setup'), ('true', 'task work:cs on your Mac')):
            with self.subTest(inside=inside):
                output = io.StringIO()
                with patch.dict(os.environ, CODESPACES=inside), contextlib.redirect_stdout(output):
                    setup_continuity(self.root, None, None)
                self.assertIn(expected, output.getvalue())
                if not inside:
                    self.assertIn('No Codespace was opened', output.getvalue())

    def test_machine_profile_is_the_macs_minus_mac_only_parts(self):
        from devcontainer_setup.__main__ import _machine_profile
        home = Path(self.tmp.name) / 'home'
        profile = home / '.config/chezmoi/machine.json'
        with patch.dict(os.environ, HOME=str(home)):
            self.assertEqual(_machine_profile(self.root), {'git': {'name': 'Test User', 'email': 'test@example.com'}})
            profile.parent.mkdir(parents=True)
            profile.write_text(json.dumps({
                'codespace': 'mac-side-name',
                'git': {'name': 'Mac Name', 'email': 'mac@example.com', 'workEmail': 'work@example.com'},
                'claudeSettings': {'permissions': {'defaultMode': 'bypassPermissions'}, 'hooks': {
                    'SessionStart': [{'hooks': [{'type': 'command', 'command': f'{home}/bin/mac-only.sh'}]}],
                    'Stop': [{'hooks': [{'type': 'command', 'command': '~/bin/portable.sh'},
                                        {'type': 'command', 'command': f'bash {home}/bin/other.sh'}]}]}}}))
            self.assertEqual(_machine_profile(self.root), {
                'git': {'name': 'Mac Name', 'email': 'mac@example.com', 'workEmail': 'work@example.com'},
                'claudeSettings': {'permissions': {'defaultMode': 'bypassPermissions'}, 'hooks': {
                    'Stop': [{'hooks': [{'type': 'command', 'command': '~/bin/portable.sh'}]}]}}})

    def test_git_identity_is_the_projects_effective_identity(self):
        from devcontainer_setup.__main__ import _git_identity
        self.assertEqual(_git_identity(self.root), ('Test User', 'test@example.com'))
        self.git('config', '--unset', 'user.email')
        with patch.dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1'):
            self.assertIsNone(_git_identity(self.root))

    def test_github_repository_reads_only_a_github_origin(self):
        from devcontainer_setup.__main__ import _github_repository
        self.assertIsNone(_github_repository(self.root))
        self.git('remote', 'add', 'origin', 'https://gitlab.com/owner/project.git')
        self.assertIsNone(_github_repository(self.root))
        self.git('remote', 'set-url', 'origin', 'git@github.com:owner/project.git')
        self.assertEqual(_github_repository(self.root), 'owner/project')

    def test_wizard_names_a_missing_origin_before_the_first_question(self):
        import contextlib
        import io
        from devcontainer_setup.__main__ import interactive
        from devcontainer_setup.discovery import scan
        from devcontainer_setup.ui import Cancelled
        class FirstQuestionCancels:
            def __getattr__(self, name):
                raise Cancelled()
        error_output = io.StringIO()
        with patch.dict(os.environ, XDG_CONFIG_HOME=str(Path(self.tmp.name) / 'config')), \
                patch('devcontainer_setup.__main__.Gum', FirstQuestionCancels), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error_output), \
                self.assertRaises(Cancelled):
            interactive(self.root, scan(self.root), {}, 'ref', None)
        self.assertIn('git remote add origin', error_output.getvalue())

    def test_launch_reuse_cancellation_does_not_offer_error_fallback(self):
        from devcontainer_setup.__main__ import _launch_options
        from devcontainer_setup.codespaces import Codespaces
        from devcontainer_setup.ui import Cancelled
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        prompts = []
        class UI:
            def choose(self, title, options, selected=None):
                return 'Push and open a Codespace in the browser'
            def confirm(self, title, default=True):
                prompts.append(title)
                return False
        def github(args, **kwargs):
            return subprocess.CompletedProcess(args, 0,
                '{"machine":{"name":"actual-machine"},"idle_timeout_minutes":30}', '')
        with self.assertRaises(Cancelled):
            _launch_options(UI(), Codespaces(run=github), self.root, 'main', 'existing-space')
        self.assertEqual(prompts, ['Reuse this codespace with these existing resource settings?'])


if __name__ == '__main__':
    unittest.main()
