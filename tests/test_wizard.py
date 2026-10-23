import json
import os
from pathlib import Path
import shlex
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
                        if title == 'Existing codespaces for this repository': return 'Reuse existing-space'
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
                '{"machine":{"name":"actual-machine"},"idle_timeout_minutes":30,"git_status":{"ref":"main"}}', '')
        with self.assertRaises(Cancelled):
            _launch_options(UI(), Codespaces(run=github), self.root, 'main', 'existing-space')
        self.assertEqual(prompts, ['Reuse this codespace with these existing resource settings?'])

    def test_codespace_on_another_branch_is_listed_and_refused(self):
        import contextlib
        import io
        from devcontainer_setup.__main__ import _launch_options
        from devcontainer_setup.codespaces import Codespaces
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        old = {'name': 'old-space', 'repository': {'full_name': 'owner/project'}, 'git_status': {'ref': 'setup/devcontainer'},
               'machine': {'name': 'standardLinux32gb'}, 'idle_timeout_minutes': 30}
        for resume in (None, 'old-space'):
            with self.subTest(resume=resume):
                menus, prompts = [], []
                def github(args, **kwargs):
                    if args[1:3] != ['api', 'user/codespaces?per_page=100'] and args[1:3] != ['api', 'user/codespaces/old-space']:
                        self.fail(f'Nothing is created or allocated: {args}')
                    payload = [{'codespaces': [old]}] if '?' in args[2] else old
                    return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
                class UI:
                    def choose(self, title, options, selected=None):
                        menus.append((title, options))
                        return 'Push and open a Codespace in the browser' if title == 'After setup' else options[-1]
                    def confirm(self, title, default=True):
                        prompts.append(title)
                        return True
                error_output = io.StringIO()
                with contextlib.redirect_stderr(error_output), contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(_launch_options(UI(), Codespaces(run=github), self.root, 'main', resume))
                if not resume:
                    self.assertEqual(menus[1], ('Existing codespaces for this repository',
                                                ['Create a new codespace', 'Reuse old-space (on setup/devcontainer)']))
                self.assertIn('Launch unavailable: Move old-space to main first; it is on setup/devcontainer.',
                              error_output.getvalue())
                self.assertEqual(prompts, ['Continue with commit only?'])


# Serves ssh://git@github.com/<owner>/<repo>.git from $FAKE_GITHUB, so a GitHub
# origin needs no network: Git runs `<command> host "git-upload-pack '/path'"`.
FAKE_SSH = '''import os, shlex, sys
service, path = shlex.split(sys.argv[-1])
os.execvp('git', ['git', service.removeprefix('git-'), os.path.join(os.environ['FAKE_GITHUB'], path.lstrip('/'))])
'''


# GitHub's REST and Codespaces calls, offline: PRs live in a state file and merge into
# the bare origin with Git itself; every call's argv and stdin are recorded there.
FAKE_GH = r'''#!{python} -I
import json, os, pathlib, re, subprocess, sys
from urllib.parse import parse_qs, urlsplit
state_path, origin = pathlib.Path(os.environ['FAKE_GH_STATE']), os.environ['FAKE_GH_ORIGIN']
args = sys.argv[1:]
stdin = sys.stdin.read() if '--input' in args else None
state = json.loads(state_path.read_text())
state['calls'].append({{'args': args, 'stdin': stdin}})
MACHINE = {{'name': 'standardLinux32gb', 'cpus': 4, 'memory_in_bytes': 16 * 1024**3, 'storage_in_bytes': 32 * 1024**3}}


def git(*command, env=None):
    return subprocess.run(['git', '--git-dir', origin, *command], capture_output=True, text=True, env=env)


def reply(value, code=0, error=''):
    state_path.write_text(json.dumps(state))
    print(value if isinstance(value, str) else json.dumps(value))
    sys.stderr.write(error)
    sys.exit(code)


def merge(pr, body):
    base = git('rev-parse', 'refs/heads/' + pr['base']['ref']).stdout.strip()
    tree = git('merge-tree', '--write-tree', base, pr['head']['sha']).stdout.split()[0]
    env = dict(os.environ, GIT_AUTHOR_NAME='GitHub', GIT_AUTHOR_EMAIL='noreply@github.com',
               GIT_COMMITTER_NAME='GitHub', GIT_COMMITTER_EMAIL='noreply@github.com')
    sha = git('commit-tree', tree, '-p', base, '-p', pr['head']['sha'], '-m', body['commit_title'],
              '-m', body['commit_message'], env=env).stdout.strip()
    git('update-ref', 'refs/heads/' + pr['base']['ref'], sha, base)
    pr.update(state='closed', merged=True, merge_commit_sha=sha)
    return sha


if args[0] == 'api':
    method, endpoint, skip = 'GET', None, False
    for index, arg in enumerate(args[1:], 1):
        if skip:
            skip = False
        elif arg in ('--method', '--input', '-f', '-F'):
            method = args[index + 1] if arg == '--method' else method
            skip = True
        elif not arg.startswith('-') and endpoint is None:
            endpoint = arg
    path, query = urlsplit(endpoint).path, parse_qs(urlsplit(endpoint).query)
    prs = state['prs']
    if path == 'user':
        reply({{'login': 'octo'}})
    if path == 'user/codespaces':
        reply([{{'codespaces': state['codespaces']}}])
    if path.startswith('user/codespaces/'):
        reply(next(space for space in state['codespaces'] if space['name'] == path.rsplit('/', 1)[1]))
    if path == 'repos/owner/project/codespaces/machines':
        reply({{'machines': [MACHINE], 'total_count': 1}})
    if path == 'repos/owner/project/pulls' and method == 'POST':
        body = json.loads(stdin)
        sha = git('rev-parse', 'refs/heads/' + body['head']).stdout.strip()
        pr = {{'number': len(prs) + 1, 'title': body['title'], 'body': body['body'], 'state': 'open', 'merged': False,
              'merge_commit_sha': None, 'base': {{'ref': body['base']}},
              'head': {{'ref': body['head'], 'sha': sha, 'repo': {{'full_name': 'owner/project'}}}}}}
        prs.append(pr)
        reply(pr)
    if path == 'repos/owner/project/pulls' and state.get('list_failures'):
        state['list_failures'] -= 1
        reply('', 1, 'gh: Could not resolve to a Repository (HTTP 502)\n')
    if path == 'repos/owner/project/pulls':
        found = [pr for pr in prs if pr['state'] == query['state'][0] and pr['base']['ref'] == query['base'][0]
                 and ('head' not in query or 'owner:' + pr['head']['ref'] == query['head'][0])]
        reply([found] if '--slurp' in args else found)
    match = re.fullmatch(r'repos/owner/project/pulls/(\d+)(/merge)?', path)
    if match:
        pr = next(pr for pr in prs if pr['number'] == int(match[1]))
        if not match[2]:
            reply(dict(pr, head=dict(pr['head'], sha=state['moved_head'])) if state.get('moved_head') else pr)
        if state.get('refusal'):
            reply({{'message': state['refusal']}}, 1, 'gh: ' + state['refusal'] + ' (HTTP 405)\n')
        if state.get('phantom_merge'):
            pr.update(state='closed', merged=True, merge_commit_sha='f' * 40)
            reply({{'sha': 'f' * 40, 'merged': True, 'message': 'Pull Request successfully merged'}})
        reply({{'sha': merge(pr, json.loads(stdin)), 'merged': True, 'message': 'Pull Request successfully merged'}})
    if path.endswith('/status'):
        reply({{'state': 'pending', 'total_count': 0, 'statuses': []}})
    if path.endswith('/check-runs'):
        reply({{'total_count': 0, 'check_runs': []}})
if args[:2] == ['codespace', 'create']:
    state['codespaces'].append({{'name': 'new-space', 'state': 'Available', 'machine': {{'name': MACHINE['name']}},
                                'idle_timeout_minutes': 30, 'retention_period_minutes': None, 'location': None,
                                'repository': {{'full_name': 'owner/project'}},
                                'git_status': {{'ref': args[args.index('--branch') + 1]}}}})
    reply('new-space')
if args[:2] == ['codespace', 'ssh']:
    contains = re.search(r'--is-ancestor (\S+) HEAD; then', args[-1])
    if contains:
        reply('yes' if git('merge-base', '--is-ancestor', contains[1], 'refs/heads/main').returncode == 0 else 'no')
    reply('main' if 'branch --show-current' in args[-1] else '')
if args[:2] == ['codespace', 'code']:
    reply('')
reply('', 1, 'fake gh: unsupported ' + ' '.join(args) + '\n')
'''


class SetupFromDefaultBranchTests(unittest.TestCase):
    """A GitHub origin served offline; the owner's checkout sits on a feature branch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        ssh = base / 'bin/fake-ssh.py'
        ssh.parent.mkdir()
        ssh.write_text(FAKE_SSH)
        self.state = base / 'state'
        self.env = dict(os.environ, HOME=str(base / 'home'), XDG_CONFIG_HOME=str(base / 'config'),
                        XDG_CACHE_HOME=str(base / 'cache'), XDG_STATE_HOME=str(self.state),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_SSH_VARIANT='simple',
                        GIT_SSH_COMMAND=f'{shlex.quote(sys.executable)} -I {shlex.quote(str(ssh))}',
                        FAKE_GITHUB=str(base / 'github'))
        (base / 'home').mkdir()
        # Every GitHub call reaches only this fake, never the real gh.
        gh = base / 'bin/gh'
        gh.write_text(FAKE_GH.format(python=sys.executable))
        gh.chmod(0o700)
        self.gh_state = base / 'gh-state.json'
        self.gh_state.write_text(json.dumps({'calls': [], 'prs': [], 'codespaces': []}))
        self.env.update(PATH=f'{gh.parent}{os.pathsep}{os.environ["PATH"]}', FAKE_GH_STATE=str(self.gh_state),
                        FAKE_GH_ORIGIN=str(base / 'github/owner/project.git'))
        environment = patch.dict(os.environ, self.env)
        environment.start()
        self.addCleanup(environment.stop)
        self.origin = base / 'github/owner/project.git'
        self.origin.parent.mkdir(parents=True)
        subprocess.check_call(['git', 'init', '--quiet', '--bare', '-b', 'main', str(self.origin)])
        self.root = base / 'project'
        self.root.mkdir()
        self.git('init', '--quiet', '-b', 'main')
        self.git('config', 'user.name', 'Test User')
        self.git('config', 'user.email', 'test@example.com')
        (self.root / 'package.json').write_text('{"engines":{"node":"24"}}\n')
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'Initial')
        self.git('remote', 'add', 'origin', 'ssh://git@github.com/owner/project.git')
        self.git('push', '--quiet', '-u', 'origin', 'main')
        self.clone = base / 'other-machine'

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'devcontainer_setup', '--project', str(self.root), *args],
                              cwd=SOURCE, env=self.env, capture_output=True, text=True)

    def push_to_main(self, files):
        """Advance origin's main from another clone, as another machine would."""
        if not self.clone.exists():
            subprocess.check_call(['git', 'clone', '--quiet', str(self.origin), str(self.clone)])
        for name, text in files.items():
            (self.clone / name).parent.mkdir(parents=True, exist_ok=True)
            (self.clone / name).write_text(text)
        run = ['git', '-C', str(self.clone), '-c', 'user.name=Other', '-c', 'user.email=other@example.com']
        subprocess.check_call([*run, 'add', '.'])
        subprocess.check_call([*run, 'commit', '--quiet', '-m', 'Remote work'])
        subprocess.check_call([*run, 'push', '--quiet', 'origin', 'HEAD:refs/heads/main'])
        return subprocess.check_output([*run, 'rev-parse', 'HEAD'], text=True).strip()

    def checkout_feature(self):
        # Feature-only files would show in anything rendered from this checkout.
        self.git('switch', '--quiet', '-c', 'feature/local')
        (self.root / 'Taskfile.yml').write_text("version: '3'\ntasks:\n  feature-only:\n    cmds: [echo feature]\n")
        (self.root / 'go.mod').write_text('module example.com/feature\n\ngo 1.26\n')
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'Feature work')
        (self.root / 'package.json').write_text('{"engines":{"node":"24"},"stashed":true}\n')
        self.git('stash', 'push', '--quiet', '-m', 'owner stash')
        (self.root / 'package.json').write_text('{"engines":{"node":"24"},"private":true}\n')
        (self.root / 'scratch.txt').write_text('untracked owner work\n')

    def snapshot(self):
        files = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob('*')
                 if path.is_file() and '.git' not in path.relative_to(self.root).parts}
        return (self.git('branch', '--show-current'), self.git('rev-parse', 'HEAD'),
                self.git('status', '--porcelain', '--untracked-files=all'), self.git('ls-files', '--stage'),
                files, self.git('stash', 'list'),
                subprocess.run(['git', '-C', str(self.root), 'rev-parse', '--verify', '--quiet', 'refs/stash'],
                               capture_output=True, text=True).stdout.strip())

    def worktree_root(self):
        from devcontainer_setup.project_sync.state import discover_project
        return self.state / 'devcontainer-setup/setup-worktrees' / discover_project(self.root).project_id

    def assert_nothing_left(self):
        self.assertEqual(self.git('branch', '--list', 'setup/*'), '')
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('\nworktree '), 0)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.origin), 'for-each-ref', 'refs/heads/setup'],
                                                 text=True), '')
        self.assertFalse((self.worktree_root() / 'setup-devcontainer').exists())

    def ui(self, answers, seen):
        case = self

        class UI:
            def multi(self, title, options, selected):
                return selected

            def input(self, title, value=''):
                return answers.get(title, value)

            def confirm(self, title, default=True):
                seen.setdefault('confirms', []).append(title)
                return answers.get(title, False)

            def choose(self, title, options, selected=None):
                seen.setdefault('choices', []).append(title)
                if title == 'Review setup':
                    seen['worktrees'] = case.git('worktree', 'list', '--porcelain')
                if title not in answers:
                    raise AssertionError(f'Unexpected choice: {title}')
                return answers[title]
        return UI

    def interact(self, answers, ref='a' * 40, ui=None, **kwargs):
        import contextlib
        import io
        from devcontainer_setup.__main__ import interactive
        from devcontainer_setup.discovery import scan
        seen = self.seen = {}
        output = io.StringIO()
        with patch('devcontainer_setup.__main__.Gum', ui or self.ui(answers, seen)), \
                patch('devcontainer_setup.__main__._ensure_runner'), \
                patch('devcontainer_setup.__main__.repository_access', return_value=True), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                seen['result'] = interactive(self.root, scan(self.root), {}, ref, None, **kwargs)
            finally:
                seen['output'] = output.getvalue()
        return seen

    COMMIT_ONLY = {'After setup': 'Commit only (no Codespace)', 'Review setup': 'Continue',
                   'Commit these setup files?': True}
    LAUNCH = {'After setup': 'Push and open a Codespace in the browser',
              'Codespaces machine': '4 cores / 16 GB RAM / 32 GB disk (standardLinux32gb)',
              'Stop when idle for': '30m', 'Review setup': 'Continue',
              'Commit setup files, push setup/devcontainer and open a PR into main?': True}
    MERGE = 'Merge PR #1 into main as octo? Required reviews are bypassed if your account is allowed to.'
    RERUN = 'Merge it, then run task devcontainer:setup again to create the Codespace.'

    def gh(self, **changes):
        state = json.loads(self.gh_state.read_text())
        if changes:
            state.update(changes)
            self.gh_state.write_text(json.dumps(state))
        return state

    def origin_git(self, *args):
        return subprocess.check_output(['git', '--git-dir', str(self.origin), *args], text=True).strip()

    def first(self, calls, *prefix):
        return next(index for index, call in enumerate(calls) if call['args'][:len(prefix)] == list(prefix))

    def assert_created_on_main_at(self, calls, sha):
        create = calls[self.first(calls, 'codespace', 'create')]['args']
        self.assertEqual(create[create.index('--branch') + 1], 'main')
        machines = [call['args'] for call in calls if 'repos/owner/project/codespaces/machines' in call['args']]
        self.assertIn(f'ref={sha}', machines[-1])
        from devcontainer_setup.codespaces import receipt_path
        receipt = json.loads(receipt_path(self.state, 'owner/project', 'main').read_text())
        self.assertEqual((receipt['codespace'], receipt['branch'], receipt['commit']), ('new-space', 'main', sha))
        self.assertEqual(calls[-1]['args'], ['codespace', 'code', '--codespace', 'new-space', '--web'])

    def decline_merge(self):
        """A first launch run whose merge the owner declines: PR #1 stays open."""
        with self.assertRaises(ValueError) as caught:
            self.interact(dict(self.LAUNCH, **{self.MERGE: False}))
        self.assertEqual(str(caught.exception), f'PR #1 is open. {self.RERUN}')
        return len(self.gh()['calls'])

    def test_rendered_files_come_from_the_worktree(self):
        from devcontainer_setup.__main__ import integrate_taskfile
        from devcontainer_setup.discovery import scan
        self.checkout_feature()
        # Control: this checkout's own render would carry both feature-only signals.
        files = {}
        integrate_taskfile(self.root, files)
        self.assertIn(b'feature-only', files['Taskfile.yml'])
        self.assertTrue(scan(self.root)['runtimes']['go']['selected'])
        before = self.snapshot()
        result = self.cli('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('+++ b/Taskfile.yml', result.stdout)
        self.assertNotIn('feature-only', result.stdout)
        self.assertNotIn('devcontainers/features/go:1', result.stdout)
        self.assertIn('devcontainers/features/node:1', result.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assert_nothing_left()

    def test_owner_branch_config_neither_blocks_nor_shapes_a_github_run(self):
        import contextlib
        import io
        from devcontainer_setup.__main__ import main
        from devcontainer_setup.ui import Cancelled

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        class FirstQuestionCancels:
            def __getattr__(self, name):
                raise Cancelled()
        self.checkout_feature()
        config = self.root / '.devcontainer/devcontainer.json'
        config.parent.mkdir()
        stale = json.dumps({'customizations': {'devcontainerSetup': {'sourceRef': 'c' * 40}}}) + '\n'
        # Exists only on this feature branch; main has no devcontainer.json at all.
        for label, text in (('malformed', '{not json\n'), ('stale', stale)):
            with self.subTest(label):
                config.write_text(text)
                self.git('add', str(config))
                self.git('commit', '--quiet', '-m', f'Feature-only {label} configuration')
                before = self.snapshot()
                result = self.cli('--dry-run')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('not json', result.stdout)
                self.assertNotIn('c' * 40, result.stdout)
                output = Terminal()
                with patch('devcontainer_setup.__main__.Gum', FirstQuestionCancels), \
                        patch.object(sys, 'stdin', Terminal()), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(main(['--project', str(self.root)]), 130, output.getvalue())
                self.assertNotIn('generator revision', output.getvalue())
                self.assertNotIn('Cannot read existing devcontainer configuration', output.getvalue())
                self.assertEqual(self.snapshot(), before)
                self.assert_nothing_left()

    def test_dry_run_failing_inside_render_leaves_nothing(self):
        import contextlib
        import io
        from devcontainer_setup.__main__ import main
        self.checkout_feature()
        before = self.snapshot()
        for error in (ValueError('render rejected input'), KeyboardInterrupt()):
            with self.subTest(type(error).__name__):
                output = io.StringIO()
                with patch('devcontainer_setup.__main__.render', side_effect=error), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    if isinstance(error, ValueError):
                        self.assertEqual(main(['--project', str(self.root), '--dry-run']), 1)
                    else:
                        with self.assertRaises(KeyboardInterrupt):
                            main(['--project', str(self.root), '--dry-run'])
                self.assertEqual(self.snapshot(), before)
                self.assert_nothing_left()

    def test_dry_run_removes_its_worktree_on_error(self):
        # Only origin's main has the unresolved pins; this checkout has none.
        self.push_to_main({'package.json': '{"packageManager":"pnpm@9.15.4"}\n',
                           'nested/package.json': '{"packageManager":"pnpm@10.2.1"}\n'})
        self.checkout_feature()
        before = self.snapshot()
        result = self.cli('--dry-run')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('package-manager version', result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assert_nothing_left()

    def test_review_names_the_setup_branch_and_cancel_leaves_nothing(self):
        from devcontainer_setup.ui import Cancelled
        self.checkout_feature()
        before = self.snapshot()
        with self.assertRaises(Cancelled):
            self.interact({'After setup': 'Commit only (no Codespace)', 'Review setup': 'Cancel'})
        self.assertIn('Branch: setup/devcontainer (from origin/main)', self.seen['output'])
        self.assertIn(f'worktree {self.worktree_root() / "setup-devcontainer"}\n', self.seen['worktrees'])
        self.assertEqual(self.snapshot(), before)
        self.assert_nothing_left()

    def test_cancel_after_apply_keeps_the_cancellation_and_names_the_cleanup(self):
        from devcontainer_setup.ui import Cancelled
        self.checkout_feature()
        before = self.snapshot()
        # Worktrees share this hook; its rewrite leaves the worktree with staged changes.
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nprintf "# hook\\n" >> .gitignore\ngit add .gitignore\n')
        hook.chmod(0o755)
        with self.assertRaises(Cancelled):
            self.interact(self.COMMIT_ONLY)
        self.assertIn('Commit the hook-updated setup files shown above?', self.seen['confirms'])
        self.assertIn('run by hand:', self.seen['output'])
        self.assertIn('worktree remove', self.seen['output'])
        worktree = self.worktree_root() / 'setup-devcontainer'
        self.assertTrue(worktree.exists())  # Reported, never silently deleted.
        self.assertEqual(self.git('rev-parse', 'refs/heads/setup/devcontainer'),
                         subprocess.check_output(['git', '-C', str(self.origin), 'rev-parse', 'main'], text=True).strip())
        self.assertEqual(self.snapshot(), before)

    def test_cleanup_failure_after_commit_reports_the_commit_and_finishes(self):
        self.checkout_feature()
        before = self.snapshot()
        # Leaves an untracked file in the worktree after a good commit, so its removal refuses.
        hook = self.root / '.git/hooks/post-commit'
        hook.write_text('#!/bin/sh\necho cache > hook-cache.txt\n')
        hook.chmod(0o755)
        with patch.dict(os.environ, CODESPACES=''):
            seen = self.interact(dict(self.COMMIT_ONLY, **{'Sync scratch files and project notes with your Mac?': True}))
        self.assertEqual(seen['result'], 0, seen['output'])
        output = seen['output']
        self.assertTrue(self.git('rev-parse', '--verify', 'refs/heads/setup/devcontainer'))
        self.assertLess(output.index('Setup commit is on branch setup/devcontainer'),
                        output.index('worktree remove --force'))
        self.assertNotIn('nothing else was changed', output)
        self.assertIn('No Codespace was opened', output)  # setup_continuity ran, as on success.
        self.assertTrue((self.worktree_root() / 'setup-devcontainer/hook-cache.txt').exists())
        self.assertEqual(self.snapshot(), before)

    def test_commit_only_commits_in_the_worktree_and_keeps_the_branch(self):
        self.checkout_feature()
        before, main = self.snapshot(), self.push_to_main({'remote.txt': 'newer main\n'})
        seen = self.interact(self.COMMIT_ONLY)
        self.assertEqual(seen['result'], 0, seen['output'])
        self.assertIn('Branch: setup/devcontainer (from origin/main)', seen['output'])
        self.assertIn(f'worktree {self.worktree_root() / "setup-devcontainer"}\n', seen['worktrees'])
        sha = self.git('rev-parse', 'refs/heads/setup/devcontainer')
        self.assertEqual(self.git('rev-parse', f'{sha}^'), main)
        self.assertIn('.devcontainer/devcontainer.json', self.git('show', '--name-only', '--format=', sha))
        self.assertNotIn('feature-only', self.git('show', f'{sha}:Taskfile.yml'))
        self.assertNotIn('devcontainers/features/go:1', self.git('show', f'{sha}:.devcontainer/devcontainer.json'))
        self.assertEqual(self.git('show', '-s', '--format=%B', sha), 'Configure project devcontainer')
        self.assertIn('setup/devcontainer', seen['output'].split(f'Setup commit: {sha}')[1])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('\nworktree '), 0)
        self.assertFalse((self.worktree_root() / 'setup-devcontainer').exists())
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.origin), 'for-each-ref', 'refs/heads/setup'],
                                                 text=True), '')

    def test_first_setup_writes_no_settings_file_the_checkout_does_not_ignore(self):
        self.checkout_feature()
        before = self.snapshot()
        seen = self.interact(dict(self.COMMIT_ONLY, **{'Apply dotfiles inside the container?': True,
                                                       'Dotfiles repository': 'owner/dotfiles'}))
        self.assertEqual(seen['result'], 0, seen['output'])
        self.assertEqual(self.snapshot(), before)
        self.assertIn('DOTFILES_REPO=owner/dotfiles', seen['output'])
        self.assertNotIn('could not be saved', seen['output'])

    def test_revision_gate_reads_origin_default(self):
        from devcontainer_setup.ui import Cancelled
        marker = {'customizations': {'devcontainerSetup': {'sourceRef': 'c' * 40}}}
        self.push_to_main({'.devcontainer/devcontainer.json': json.dumps(marker) + '\n'})
        self.checkout_feature()
        with self.assertRaisesRegex(ValueError, 'origin/main.*--upgrade'):
            self.interact({})
        self.assert_nothing_left()
        class FirstQuestionCancels:
            def __getattr__(self, name):
                raise Cancelled()
        with self.assertRaises(Cancelled):
            self.interact({}, ui=FirstQuestionCancels, upgrade=True)
        self.assert_nothing_left()

    def test_launch_merges_the_setup_pr_then_creates_the_codespace_on_main(self):
        self.checkout_feature()
        before, main = self.snapshot(), self.origin_git('rev-parse', 'main')
        seen = self.interact(dict(self.LAUNCH, **{self.MERGE: True}))
        self.assertEqual(seen['result'], 0, seen['output'])
        calls, (pr,) = self.gh()['calls'], self.gh()['prs']
        merged = self.origin_git('rev-parse', 'main')
        self.assertEqual(self.origin_git('log', '-1', '--format=%P', merged).split(), [main, pr['head']['sha']])
        self.assertEqual(self.git('rev-parse', f'{pr["head"]["sha"]}^'), main)
        body = json.loads(calls[self.first(calls, 'api', '--method', 'POST', 'repos/owner/project/pulls')]['stdin'])
        self.assertEqual((body['title'], body['head'], body['base']),
                         ('Configure project devcontainer', 'setup/devcontainer', 'main'))
        self.assertIn('Branch: setup/devcontainer (from origin/main)', body['body'])
        self.assertIn('Files: ', body['body'])
        self.assertNotIn(str(self.root), body['body'])
        self.assertNotIn('Launch:', body['body'])
        merge = self.first(calls, 'api', '--method', 'PUT', 'repos/owner/project/pulls/1/merge')
        self.assertEqual(json.loads(calls[merge]['stdin']),
                         {'merge_method': 'merge', 'commit_title': 'Configure project devcontainer (#1)',
                          'commit_message': 'Reviewed and merged with task devcontainer:setup.',
                          'sha': pr['head']['sha']})
        for stamp in ('co-authored-by', 'generated with', 'noreply@anthropic.com', '\U0001f916'):
            self.assertNotIn(stamp, json.dumps(calls, ensure_ascii=False).lower())
        self.assertFalse(any(call['args'][:2] == ['pr', 'merge'] for call in calls))
        self.assertLess(self.first(calls, 'api', '--method', 'POST'), merge)
        self.assertLess(merge, self.first(calls, 'codespace', 'create'))
        self.assert_created_on_main_at(calls, merged)
        self.assert_nothing_left()
        # A feature-branch checkout is never switched, merged into or rebased.
        self.assertEqual(self.snapshot(), before)
        self.assertIn('Your checkout lacks the new setup files until you merge or pull main.', seen['output'])

    def test_cancelled_merge_leaves_pr_open_and_no_codespace(self):
        reason = 'At least 1 approving review is required by reviewers with write access.'
        for label in ('declined', 'refused'):
            with self.subTest(label):
                self.setUp()
                self.checkout_feature()
                before, main = self.snapshot(), self.origin_git('rev-parse', 'main')
                if label == 'refused':
                    self.gh(refusal=reason)
                with self.assertRaises(ValueError) as caught:
                    self.interact(dict(self.LAUNCH, **{self.MERGE: label == 'refused'}))
                expected = f'PR #1 is open: {reason.rstrip(".")}. {self.RERUN}' if label == 'refused' else f'PR #1 is open. {self.RERUN}'
                self.assertEqual(str(caught.exception), expected)
                (pr,) = self.gh()['prs']
                self.assertEqual(pr['state'], 'open')
                self.assertEqual(self.origin_git('rev-parse', 'refs/heads/setup/devcontainer'), pr['head']['sha'])
                self.assertEqual(self.git('rev-parse', 'refs/heads/setup/devcontainer'), pr['head']['sha'])
                self.assertEqual(self.origin_git('rev-parse', 'main'), main)
                self.assertFalse(any(call['args'][:1] == ['codespace'] for call in self.gh()['calls']))
                self.assertEqual(list((self.state / 'devcontainer-setup').glob('*.json')), [])
                self.assertEqual(self.git('worktree', 'list', '--porcelain').count('\nworktree '), 0)
                self.assertEqual(self.snapshot(), before)

    def test_merge_refuses_a_head_moved_after_review(self):
        self.checkout_feature()
        self.gh(moved_head='e' * 40)
        with self.assertRaises(ValueError) as caught:
            self.interact(dict(self.LAUNCH, **{self.MERGE: True}))
        reviewed = self.git('rev-parse', 'refs/heads/setup/devcontainer')
        self.assertEqual(str(caught.exception), f'PR #1 is at {"e" * 12}, not the reviewed {reviewed[:12]}; '
                         f'nothing was merged. {self.RERUN}')
        # The R45 rerun binds to the head it listed and showed.
        with self.assertRaises(ValueError) as caught:
            self.interact(dict(self.LAUNCH, **{self.MERGE: True}))
        self.assertIn(f'(setup/devcontainer into main, at {reviewed[:12]})', self.seen['output'])
        self.assertEqual(str(caught.exception), f'PR #1 is at {"e" * 12}, not the reviewed {reviewed[:12]}; '
                         f'nothing was merged. {self.RERUN}')
        calls = self.gh()['calls']
        self.assertFalse(any(call['args'][:3] == ['api', '--method', 'PUT'] for call in calls))
        self.assertFalse(any(call['args'][:1] == ['codespace'] for call in calls))
        (pr,) = self.gh()['prs']
        self.assertEqual((pr['state'], pr['head']['sha']), ('open', reviewed))
        self.assertEqual(self.origin_git('rev-parse', 'refs/heads/setup/devcontainer'), reviewed)

    def test_rerun_resumes_the_open_setup_pr(self):
        from devcontainer_setup.git_ops import GitPlan
        # A clean checkout on main: the merge fast-forwards it.
        main, start = self.origin_git('rev-parse', 'main'), self.decline_merge()
        head = self.gh()['prs'][0]['head']['sha']
        with patch.object(GitPlan, 'push', side_effect=AssertionError('a second setup branch was pushed')), \
                patch('devcontainer_setup.__main__.setup_worktree', side_effect=AssertionError('a worktree was cut')):
            seen = self.interact(dict(self.LAUNCH, **{self.MERGE: True}))
        self.assertEqual(seen['result'], 0, seen['output'])
        self.assertIn(f'Setup PR #1 is open: Configure project devcontainer (setup/devcontainer into main, at {head[:12]}).',
                      seen['output'])
        self.assertNotIn('Review setup', seen['choices'])
        calls = self.gh()['calls'][start:]
        self.assertFalse(any(call['args'][:3] == ['api', '--method', 'POST'] for call in calls))
        self.assertEqual(len(self.gh()['prs']), 1)
        merged = self.origin_git('rev-parse', 'main')
        self.assertEqual(self.origin_git('log', '-1', '--format=%P', merged).split(), [main, head])
        self.assert_created_on_main_at(calls, merged)
        self.assert_nothing_left()
        self.assertEqual((self.git('branch', '--show-current'), self.git('rev-parse', 'HEAD')), ('main', merged))
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), '')
        self.assertTrue((self.root / '.devcontainer/devcontainer.json').is_file())
        self.assertNotIn('lacks the new setup files', seen['output'])

    def test_two_open_setup_prs_are_refused(self):
        prs = [{'number': number, 'title': 'Configure project devcontainer', 'state': 'open', 'merged': False,
                'base': {'ref': 'main'}, 'head': {'ref': head, 'sha': 'c' * 40, 'repo': {'full_name': 'owner/project'}}}
               for number, head in ((1, 'setup/devcontainer'), (2, 'setup/devcontainer-2'))]
        self.gh(prs=prs)
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            self.interact(self.LAUNCH)
        self.assertIn('#1 (setup/devcontainer), #2 (setup/devcontainer-2)', str(caught.exception))
        self.assertTrue(str(caught.exception).endswith('then run task devcontainer:setup again to create the Codespace.'))
        self.assertEqual(self.seen.get('choices', []), [])
        self.assertEqual([call['args'][:3] for call in self.gh()['calls']],
                         [['api', 'repos/owner/project/pulls?state=open&base=main&per_page=100', '--paginate']])
        self.assertFalse(self.worktree_root().exists())
        self.assertEqual(self.snapshot(), before)

    def test_already_merged_goes_straight_to_create(self):
        from devcontainer_setup.git_ops import GitPlan
        self.decline_merge()
        # The owner merges PR #1 on GitHub, then runs setup again.
        subprocess.run(['gh', 'api', '--method', 'PUT', 'repos/owner/project/pulls/1/merge', '--input', '-'], check=True,
                       input=json.dumps({'commit_title': 'Merge pull request #1', 'commit_message': 'By hand'}),
                       capture_output=True, text=True)
        merged, start = self.origin_git('rev-parse', 'main'), len(self.gh()['calls'])
        # Pulled already, with untracked work: no fast-forward, and nothing is missing either.
        self.git('pull', '--quiet', '--ff-only', 'origin', 'main')
        (self.root / 'scratch.txt').write_text('untracked owner work\n')
        before = self.snapshot()
        with patch.object(GitPlan, 'push', side_effect=AssertionError('an empty setup branch was pushed')):
            seen = self.interact(dict(self.LAUNCH, **{'Create or open the selected codespace on main?': True}))
        self.assertEqual(seen['result'], 0, seen['output'])
        self.assertIn('Files: already configured', seen['output'])
        self.assertNotIn('lacks the new setup files', seen['output'])
        self.assertEqual(self.snapshot(), before)
        calls = self.gh()['calls'][start:]
        self.assertFalse(any(call['args'][:2] == ['api', '--method'] for call in calls))
        self.assert_created_on_main_at(calls, merged)
        # The merged setup branch is pruned locally and on origin.
        self.assert_nothing_left()

    def test_no_codespace_until_origin_contains_the_merge(self):
        self.checkout_feature()
        self.gh(phantom_merge=True)
        with self.assertRaisesRegex(ValueError, 'Cannot confirm that origin/main contains the setup merge ffffffffffff'):
            self.interact(dict(self.LAUNCH, **{self.MERGE: True}))
        self.assertFalse(any(call['args'][:1] == ['codespace'] for call in self.gh()['calls']))
        # Nothing is pruned on an unconfirmed merge.
        self.assertTrue(self.origin_git('rev-parse', '--verify', 'refs/heads/setup/devcontainer'))
        self.assertTrue(self.git('rev-parse', '--verify', 'refs/heads/setup/devcontainer'))

    def test_cleanup_failure_after_merge_never_blocks_the_codespace(self):
        self.checkout_feature()
        # Leaves an untracked file in the worktree after a good commit, so its removal refuses.
        hook = self.root / '.git/hooks/post-commit'
        hook.write_text('#!/bin/sh\necho cache > hook-cache.txt\n')
        hook.chmod(0o755)
        seen = self.interact(dict(self.LAUNCH, **{self.MERGE: True}))
        self.assertEqual(seen['result'], 0, seen['output'])
        # The post-merge cleanup names every step left to run, the remote delete included.
        self.assertIn('worktree remove --force', seen['output'])
        self.assertIn('push origin --delete setup/devcontainer', seen['output'])
        self.assert_created_on_main_at(self.gh()['calls'], self.origin_git('rev-parse', 'main'))

    def test_no_second_setup_pr_when_the_first_check_failed(self):
        from devcontainer_setup.git_ops import GitPlan
        self.checkout_feature()
        prs = [{'number': 1, 'title': 'Configure project devcontainer', 'state': 'open', 'merged': False,
                'base': {'ref': 'main'}, 'head': {'ref': 'setup/devcontainer-7', 'sha': 'c' * 40,
                                                   'repo': {'full_name': 'owner/project'}}}]
        self.gh(prs=prs, list_failures=1)
        with patch.object(GitPlan, 'push', side_effect=AssertionError('a second setup branch was pushed')), \
                self.assertRaises(ValueError) as caught:
            self.interact(self.LAUNCH)
        self.assertIn('Note: cannot check GitHub for an open setup PR', self.seen['output'])
        self.assertEqual(str(caught.exception), f'Setup PR #1 (setup/devcontainer-7) is open. {self.RERUN}')
        self.assertEqual(len(self.gh()['prs']), 1)

    def test_reuse_carries_the_retired_setup_branch_receipt(self):
        from devcontainer_setup.__main__ import _launch_on_default
        from devcontainer_setup.codespaces import Codespaces, receipt_path
        old = receipt_path(self.state, 'owner/project', 'setup/devcontainer')
        values = {'codespace': 'old-space', 'repository': 'owner/project', 'branch': 'setup/devcontainer',
                  'commit': 'c' * 40, 'validated_commit': 'c' * 40, 'rebuild_pending': False}
        Codespaces._save_receipt(old, values)
        self.push_to_main({'.devcontainer/devcontainer.json': json.dumps({'workspaceFolder': '/workspaces/kept'}) + '\n'})
        launched = []

        class API:
            def launch(self, *args, **kwargs):
                launched.append((args, kwargs))
                return 'old-space'
        sha = self.origin_git('rev-parse', 'main')
        launch = {'machine': 'standardLinux32gb', 'idle_timeout': '30m', 'client': 'browser', 'resume': 'old-space'}
        preferences = {'apply_dotfiles': False, 'dotfiles_repo': ''}
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_launch_on_default(API(), self.root, 'owner/project', 'main', sha, launch, preferences)[0], 'old-space')
        ((args, kwargs),) = launched
        self.assertEqual(args, ('owner/project', 'main', sha))
        self.assertEqual(kwargs['receipt'], receipt_path(self.state, 'owner/project', 'main'))
        self.assertEqual(kwargs['workspace'], '/workspaces/kept')
        self.assertEqual(json.loads(kwargs['receipt'].read_text()), dict(values, branch='main'))
        self.assertEqual(json.loads(old.read_text()), values)

    def test_no_origin_keeps_current_flow(self):
        self.git('remote', 'remove', 'origin')
        for branch in ('main', 'feature/local'):
            with self.subTest(branch=branch):
                if branch != 'main':
                    self.git('switch', '--quiet', '-c', branch, 'main')
                seen = self.interact(dict(self.COMMIT_ONLY, **{'Apply dotfiles inside the container?': True,
                                                               'Dotfiles repository': 'owner/dotfiles'}))
                self.assertEqual(seen['result'], 0, seen['output'])
                expected = 'setup/devcontainer' if branch == 'main' else branch
                self.assertIn(f'Branch: {expected}\n', seen['output'])
                self.assertEqual(self.git('branch', '--show-current'), expected)
                self.assertIn('.devcontainer/devcontainer.json', self.git('show', '--name-only', '--format=', 'HEAD'))
                self.assertEqual((self.root / '.devcontainer/devcontainer.env').read_text(), 'DOTFILES_REPO=owner/dotfiles\n')
                self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), '')
                self.assertFalse((self.state / 'devcontainer-setup/setup-worktrees').exists())
                self.assertEqual(self.git('worktree', 'list', '--porcelain').count('\nworktree '), 0)
                if branch == 'main':
                    self.git('switch', '--quiet', 'main')
                    self.git('clean', '-fdxq')


if __name__ == '__main__':
    unittest.main()
