"""Consumer generation and real Taskfile surface for project continuity."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from devcontainer_setup.render import render, validate


SOURCE = Path(__file__).resolve().parents[1]
REF = 'a' * 40


class ContinuityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # The wizard canonicalizes its Git root before pure integration. macOS
        # exposes TemporaryDirectory through the system /var -> /private/var link.
        self.root = (Path(self.temp.name) / 'project').resolve()
        self.root.mkdir()
        self.selection = dict(runtimes={'python': '3.13'}, tools=[], dotfiles=False,
                              agents=True, ssh=False, ports=[], package_managers={},
                              polyglot=False, continuity=True)

    def install(self, files):
        for name, data in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def test_selected_continuity_packages_canonical_runtime_and_ssh(self):
        files = render(self.root, SOURCE, self.selection, REF)
        config = json.loads(files['.devcontainer/devcontainer.json'])
        self.assertIs(config['customizations']['devcontainerSetup']['selections'].get('continuity'), True)
        self.assertIn('ghcr.io/devcontainers/features/sshd:1', config['features'])
        self.assertIn(2222, config['forwardPorts'])
        self.assertIn('.devcontainer/scripts/project-sync.sh', files)
        self.assertIn('python3 -I', files['.devcontainer/scripts/project-sync.sh'].decode())
        modules = list((SOURCE / 'devcontainer_setup/project_sync').glob('*.py'))
        self.assertTrue(modules)
        for module in modules:
            self.assertEqual(files['.devcontainer/scripts/project_sync/' + module.name], module.read_bytes())
        self.install(files)
        self.assertEqual(validate(self.root), [])
        self.assertEqual(files, render(self.root, SOURCE, self.selection, REF))

    def test_opt_out_preserves_legacy_render_surface(self):
        files = render(self.root, SOURCE, dict(self.selection, continuity=False), REF)
        self.assertNotIn('.devcontainer/scripts/project-sync.sh', files)
        self.assertNotIn('ghcr.io/devcontainers/features/sshd:1',
                         json.loads(files['.devcontainer/devcontainer.json'])['features'])

    def test_notes_and_root_aliases_preserve_existing_project_operations(self):
        from devcontainer_setup.__main__ import integrate_continuity, integrate_taskfile
        original = "version: '3'\ntasks:\n  up:\n    cmds: ['echo user-up']\n  test:\n    cmds: ['echo user-test']\n"
        (self.root / 'Taskfile.yaml').write_text(original)
        (self.root / 'AGENTS.md').write_text('# Owner guidance\n\nKeep this sentence.\n')
        files = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, files)
        integrate_continuity(self.root, files, True)
        self.assertTrue(files['AGENTS.md'].startswith(b'# Owner guidance\n\nKeep this sentence.\n'))
        self.assertIn('.scratch/.gitignore', files)
        self.assertIn('.scratch/README.md', files)
        self.assertIn('.codex/config.toml', files)
        self.install(files)
        listed = subprocess.run(['task', '--list-all', '--json'], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        names = {entry['name'] for entry in json.loads(listed.stdout)['tasks']}
        self.assertTrue({'up', 'test', 'scratch:configure', 'scratch:start', 'scratch:stop',
                         'scratch:status', 'scratch:logs', 'scratch:backup',
                         'work:cs', 'work:disconnect', 'work:status', 'work:stop'} <= names,
                        sorted(names))
        self.assertFalse({'work', 'work:codex', 'work:claude'} & names)
        result = subprocess.run(['task', 'test'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('user-test', result.stdout)
        repeated = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, repeated)
        integrate_continuity(self.root, repeated, True)
        self.assertTrue(set(repeated) <= set(files))
        for name, data in repeated.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def inventory(self):
        result = subprocess.run(['task', '--list-all', '--json'], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {entry['name'] for entry in json.loads(result.stdout)['tasks']}

    def test_real_owner_semantics_and_include_context_survive_full_candidate(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_continuity, integrate_taskfile
        for width in (2, 4):
            with self.subTest(width=width):
                indent = ' ' * width
                (self.root / 'owner.yml').write_text(
                    "version: '3'\ntasks:\n  extra:\n    cmds: [echo INCLUDED]\n")
                owner = ("version: '3'\n# See .devcontainer/Taskfile.yml for generated helpers\n"
                         "vars:\n  documentation: .devcontainer/Taskfile.yml\nincludes:\n"
                         + indent + "'owner':\n" + indent * 2 + "taskfile: owner.yml\n"
                         + "tasks:\n" + indent + "'owner':\n" + indent * 2
                         + "preconditions: [test -f owner.yml]\n" + indent * 2
                         + "cmds: [echo OWNER]\n" + indent + '\"work:cs\":\n'
                         + indent * 2 + "cmds: [echo CUSTOM]\n")
                (self.root / 'Taskfile.yml').write_text(owner)
                before = self.inventory()
                for task in ('owner', 'owner:extra', 'work:cs'):
                    result = subprocess.run(['task', task], cwd=self.root, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                files = render(self.root, SOURCE, self.selection, REF)
                integrate_taskfile(self.root, files)
                integrate_continuity(self.root, files, True)
                check_candidate(files, self.root)
                self.install(files)
                self.assertTrue(before <= self.inventory())
                for task, output in [('owner', 'OWNER'), ('owner:extra', 'INCLUDED'), ('work:cs', 'CUSTOM')]:
                    result = subprocess.run(['task', task], cwd=self.root, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(output, result.stdout)
                self.assertIn('devcontainer:work:status', self.inventory())

    def test_candidate_refuses_silently_lost_owner_without_running_commands(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_taskfile
        original = "version: '3'\ntasks:\n  owner:\n    cmds: [touch SHOULD_NOT_RUN]\n"
        (self.root / 'Taskfile.yml').write_text(original)
        files = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, files)
        files['Taskfile.yml'] = files['Taskfile.yml'].replace(b'  owner:', b'  lost:')
        with self.assertRaisesRegex(ValueError, 'owner'):
            check_candidate(files, self.root)
        self.assertFalse((self.root / 'SHOULD_NOT_RUN').exists())

    def test_candidate_rejects_dangling_managed_aliases(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_continuity, integrate_taskfile
        files = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, files)
        integrate_continuity(self.root, files, True)
        files['Taskfile.yml'] = files['Taskfile.yml'].split(b'\nincludes:')[0] + b'\n'
        with self.assertRaisesRegex(ValueError, 'delegate'):
            check_candidate(files, self.root)

    def test_candidate_inventory_does_not_run_status_vars_or_preconditions(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_continuity, integrate_taskfile
        original = ('version: "3"\nvars:\n  OWNER:\n    sh: touch VAR_EXECUTED; echo OWNER\n'
                    'tasks:\n  owner:\n    desc: "{{.OWNER}}"\n'
                    '    preconditions: [test -f ABSENT]\n'
                    '    status: [touch STATUS_EXECUTED]\n'
                    '    cmds: [touch COMMAND_EXECUTED]\n')
        # Absolute markers catch execution in either temporary inventory tree,
        # as well as an accidental invocation in the owner checkout.
        for name in ('VAR_EXECUTED', 'STATUS_EXECUTED', 'COMMAND_EXECUTED'):
            original = original.replace('touch ' + name, 'touch ' + str(self.root / name))
        (self.root / 'Taskfile.yml').write_text(original)
        files = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, files)
        integrate_continuity(self.root, files, True)
        check_candidate(files, self.root)
        self.assertEqual((self.root / 'Taskfile.yml').read_text(), original)
        for name in ('VAR_EXECUTED', 'STATUS_EXECUTED', 'COMMAND_EXECUTED'):
            self.assertFalse((self.root / name).exists())

    def test_candidate_cannot_hide_root_taskfile_deletion_by_restaging_old_file(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_taskfile
        (self.root / 'Taskfile.yml').write_text('version: "3"\ntasks:\n  owner: {}\n')
        files = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, files)
        files['Taskfile.yml'] = None
        with self.assertRaisesRegex(ValueError, 'Taskfile'):
            check_candidate(files, self.root)

    def test_candidate_refuses_include_context_through_external_directory_link(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_taskfile
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (outside / 'Taskfile.yml').write_text('version: "3"\ntasks:\n  external: {}\n')
        (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        (self.root / 'Taskfile.yml').write_text(
            'version: "3"\nincludes:\n  owner: linked/Taskfile.yml\ntasks:\n  owner: {}\n')
        files = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, files)
        with self.assertRaisesRegex(ValueError, 'include'):
            check_candidate(files, self.root)

    def test_generated_graph_retirement_preserves_owner_prefix(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_taskfile
        generated = b'version: "3"\ntasks:\n  managed: {}\n  obsolete: {}\n'
        for owner_name in ('devcontainer:owner', 'devcontainer:nested:owner'):
            with self.subTest(owner_name=owner_name):
                config = json.loads(render(self.root, SOURCE, self.selection, REF)['.devcontainer/devcontainer.json'])
                (self.root / '.devcontainer').mkdir(exist_ok=True)
                (self.root / '.devcontainer/Taskfile.yml').write_bytes(generated)
                config['customizations']['devcontainerSetup']['managedFiles'] = {
                    '.devcontainer/Taskfile.yml': hashlib.sha256(generated).hexdigest()}
                (self.root / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
                stanza = f'  "{owner_name}":\n    cmds: [echo OWNER_PREFIX]\n'
                original = ('version: "3"\nincludes:\n  devcontainer: .devcontainer/Taskfile.yml\n'
                            'tasks:\n  owner: {}\n' + stanza)
                (self.root / 'Taskfile.yml').write_text(original)
                self.assertIn('devcontainer:obsolete', self.inventory())
                result = subprocess.run(['task', owner_name], cwd=self.root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('OWNER_PREFIX', result.stdout)
                files = render(self.root, SOURCE, self.selection, REF)
                integrate_taskfile(self.root, files)
                files['Taskfile.yml'] = original.replace(stanza, '').encode()
                snapshot = dict(files)
                with self.assertRaisesRegex(ValueError, 'lost owner Task commands: ' + owner_name):
                    check_candidate(files, self.root)
                self.assertEqual(files, snapshot)
                self.assertEqual((self.root / 'Taskfile.yml').read_text(), original)
                files['Taskfile.yml'] = original.encode()
                check_candidate(files, self.root)
                self.install(files)
                self.assertNotIn('devcontainer:obsolete', self.inventory())
                result = subprocess.run(['task', owner_name], cwd=self.root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('OWNER_PREFIX', result.stdout)

    def test_optional_internal_symlink_context_is_never_omitted_from_baseline(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_taskfile
        actual = self.root / 'actual'
        actual.mkdir()
        (actual / 'Taskfile.yml').write_text('version: "3"\ntasks:\n  extra: {}\n')
        (self.root / 'linked.yml').symlink_to('actual/Taskfile.yml')
        original = ('version: "3"\nincludes:\n  owner:\n    taskfile: linked.yml\n'
                    '    optional: true\ntasks:\n  keep: {}\n')
        (self.root / 'Taskfile.yml').write_text(original)
        self.assertIn('owner:extra', self.inventory())
        files = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, files)
        files['Taskfile.yml'] = b'version: "3"\ntasks:\n  keep: {}\n'
        snapshot = dict(files)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            check_candidate(files, self.root)
        self.assertEqual(files, snapshot)
        self.assertEqual((self.root / 'Taskfile.yml').read_text(), original)

    def test_empty_include_mapping_at_eof_survives_full_candidate(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_continuity, integrate_taskfile
        original = 'version: "3"\ntasks:\n  owner:\n    cmds: [echo EOF_OWNER]\nincludes:'
        (self.root / 'Taskfile.yml').write_text(original)
        self.assertEqual(self.inventory(), {'owner'})
        files = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, files)
        integrate_continuity(self.root, files, True)
        check_candidate(files, self.root)
        self.assertIn(b'includes:\n  devcontainer:', files['Taskfile.yml'])
        self.install(files)
        self.assertIn('owner', self.inventory())
        result = subprocess.run(['task', 'owner'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('EOF_OWNER', result.stdout)

    def candidate(self, project, enabled=True):
        from devcontainer_setup.__main__ import integrate_continuity, integrate_taskfile
        files = render(project, SOURCE, dict(self.selection, continuity=enabled), REF)
        integrate_taskfile(project, files)
        integrate_continuity(project, files, enabled)
        return files

    def delegate_fixture(self, project, owner, target='obsolete', included=None):
        config = json.loads(render(project, SOURCE, self.selection, REF)['.devcontainer/devcontainer.json'])
        generated = ('version: "3"\ntasks:\n  ' + target + ':\n    cmds: [echo OWNER_DELEGATE]\n'
                     '  unreferenced: {}\n').encode()
        config['customizations']['devcontainerSetup']['managedFiles'] = {
            '.devcontainer/Taskfile.yml': hashlib.sha256(generated).hexdigest()}
        (project / '.devcontainer').mkdir()
        (project / '.devcontainer/Taskfile.yml').write_bytes(generated)
        (project / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
        (project / 'Taskfile.yml').write_text(
            'version: "3"\nincludes:\n  devcontainer: .devcontainer/Taskfile.yml\n'
            + ('  owner: owner.yml\n' if included else '') + 'tasks:\n' + owner)
        if included:
            (project / 'owner.yml').write_text(included)

    def assert_owner_runs(self, project, task='owner'):
        result = subprocess.run(['task', task], cwd=project, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('OWNER_DELEGATE', result.stdout)

    def test_owner_task_references_block_retirement_but_allow_retained_delegate(self):
        from devcontainer_setup.__main__ import check_candidate
        forms = (
            '  owner: {cmds: [{task: "devcontainer:obsolete"}]}\n',
            "  'owner':\n    'cmds':\n      - 'task': 'devcontainer:obsolete'\n",
            '  owner:\n    cmds: [{task: devcontainer:obsolete}]\n',
            '  owner: {deps: [devcontainer:obsolete]}\n',
            '  owner:\n    deps:\n      - devcontainer:obsolete\n',
            '  owner:\n    deps:\n      - task: "devcontainer:obsolete"\n',
            "  owner: {deps: [{'task': 'devcontainer:obsolete'}]}\n",
            '  owner:\n    cmds:\n      - task: devcontainer:obsolete\n        vars: {OWNER: retained}\n',
            '  owner:\n    deps:\n      - vars: {OWNER: retained}\n        task: devcontainer:obsolete\n',
            '  owner: {"cmds":[{"task":"devcontainer:obsolete"}]}\n',
        )
        for owner in forms:
            with self.subTest(owner=owner), tempfile.TemporaryDirectory(dir=self.root) as directory:
                project = Path(directory)
                self.delegate_fixture(project, owner)
                self.assert_owner_runs(project)
                original = (project / 'Taskfile.yml').read_bytes()
                files = self.candidate(project)
                snapshot = dict(files)
                with self.assertRaisesRegex(ValueError, 'owner.*reference.*devcontainer:obsolete'):
                    check_candidate(files, project)
                self.assertEqual(files, snapshot)
                self.assertEqual((project / 'Taskfile.yml').read_bytes(), original)
                # Keeping a still-supported delegate is valid; the gate must
                # neither rewrite the wrapper nor refuse all generated changes.
                files['.devcontainer/Taskfile.yml'] += b'  obsolete:\n    cmds: [echo OWNER_DELEGATE]\n'
                config = json.loads(files['.devcontainer/devcontainer.json'])
                config['customizations']['devcontainerSetup']['managedFiles']['.devcontainer/Taskfile.yml'] = (
                    hashlib.sha256(files['.devcontainer/Taskfile.yml']).hexdigest())
                files['.devcontainer/devcontainer.json'] = json.dumps(config).encode()
                check_candidate(files, project)
                for name, data in files.items():
                    path = project / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if data is not None:
                        path.write_bytes(data)
                self.assertIn(owner.encode(), (project / 'Taskfile.yml').read_bytes())
                self.assert_owner_runs(project)

    def test_disable_refuses_owner_call_to_retiring_continuity_delegate_or_alias(self):
        from devcontainer_setup.__main__ import check_candidate
        for target in ('devcontainer:work:status', 'work:status'):
            with self.subTest(target=target), tempfile.TemporaryDirectory(dir=self.root) as directory:
                project = Path(directory)
                self.delegate_fixture(project, '  owner: {cmds: [{task: "' + target + '"}]}\n',
                                      target='work:status')
                from devcontainer_setup.root_tasks import integrate
                root = project / 'Taskfile.yml'
                root.write_text(integrate(root.read_text()))
                self.assert_owner_runs(project)
                original = root.read_bytes()
                files = self.candidate(project, enabled=False)
                snapshot = dict(files)
                with self.assertRaisesRegex(ValueError, 'owner.*reference.*' + target):
                    check_candidate(files, project)
                self.assertEqual(files, snapshot)
                self.assertEqual(root.read_bytes(), original)

    def test_included_owner_references_resolve_root_and_local_namespaces(self):
        from devcontainer_setup.__main__ import check_candidate
        for field in ('cmds', 'deps'):
            for target in (':devcontainer:obsolete', 'helpers:obsolete'):
                with self.subTest(field=field, target=target), tempfile.TemporaryDirectory(dir=self.root) as directory:
                    project = Path(directory)
                    included = ('version: "3"\nincludes:\n  helpers: .devcontainer/Taskfile.yml\n'
                                'tasks:\n  wrapper: {' + field + ': [{task: "' + target + '"}]}\n')
                    self.delegate_fixture(project, '  keep: {}\n', included=included)
                    self.assert_owner_runs(project, 'owner:wrapper')
                    files = self.candidate(project)
                    snapshot = dict(files)
                    with self.assertRaisesRegex(ValueError, 'owner:wrapper.*reference.*obsolete'):
                        check_candidate(files, project)
                    self.assertEqual(files, snapshot)
                    self.assertEqual((project / 'owner.yml').read_text(), included)

    def test_dynamic_owner_reference_refuses_unproven_retirement_without_evaluation(self):
        from devcontainer_setup.__main__ import check_candidate
        self.delegate_fixture(self.root, '  owner: {cmds: [{task: "{{.TARGET}}"}]}\n')
        root = self.root / 'Taskfile.yml'
        root.write_text(root.read_text() + 'vars:\n  TARGET: devcontainer:obsolete\n')
        self.assert_owner_runs(self.root)
        files = self.candidate(self.root)
        snapshot = dict(files)
        with self.assertRaisesRegex(ValueError, 'owner.*reference.*dynamic'):
            check_candidate(files, self.root)
        self.assertEqual(files, snapshot)

    def test_internal_delegate_and_alias_references_cannot_hide_from_inventory(self):
        from devcontainer_setup.__main__ import check_candidate
        for reference in ('devcontainer:obsolete', 'devcontainer:old-alias'):
            with self.subTest(reference=reference), tempfile.TemporaryDirectory(dir=self.root) as directory:
                project = Path(directory)
                self.delegate_fixture(project, '  owner: {deps: ["' + reference + '"]}\n')
                path = project / '.devcontainer/Taskfile.yml'
                path.write_text(path.read_text().replace('  obsolete:\n',
                                '  obsolete:\n    internal: true\n    aliases: [old-alias]\n'))
                config = json.loads((project / '.devcontainer/devcontainer.json').read_bytes())
                config['customizations']['devcontainerSetup']['managedFiles']['.devcontainer/Taskfile.yml'] = (
                    hashlib.sha256(path.read_bytes()).hexdigest())
                (project / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
                self.assert_owner_runs(project)
                files = self.candidate(project)
                with self.assertRaisesRegex(ValueError, 'owner.*reference.*' + reference):
                    check_candidate(files, project)

    def test_reference_retirement_validation_never_runs_owner_evaluation(self):
        from devcontainer_setup.__main__ import check_candidate
        self.delegate_fixture(self.root, '  owner: {}\n')
        markers = [self.root / name for name in ('VAR', 'STATUS', 'PRECONDITION', 'COMMAND')]
        root = self.root / 'Taskfile.yml'
        text = root.read_text().replace('  owner: {}\n',
            '  owner:\n    desc: "{{.OWNER}}"\n'
            f'    preconditions: [touch {markers[2]}]\n'
            f'    status: [touch {markers[1]}]\n'
            '    cmds:\n      - |\n'
            f'        touch {markers[3]}\n'
            '        echo "task: devcontainer:obsolete is shell text"\n')
        text += f'vars:\n  OWNER:\n    sh: touch {markers[0]}; echo OWNER\n'
        root.write_text(text)
        check_candidate(self.candidate(self.root), self.root)
        for path in markers:
            self.assertFalse(path.exists(), path)

    def test_deferred_owner_task_and_wildcard_references_cannot_escape_retirement_check(self):
        from devcontainer_setup.__main__ import check_candidate
        cases = (
            ('  owner: {cmds: [{defer: {task: "devcontainer:obsolete"}}]}\n', 'obsolete'),
            ('  owner:\n    cmds:\n      - defer:\n          task: devcontainer:obsolete\n', 'obsolete'),
            ('  owner: {cmds: [{task: "devcontainer:obsolete:one"}]}\n', '"obsolete:*"'),
        )
        for owner, target in cases:
            with self.subTest(owner=owner), tempfile.TemporaryDirectory(dir=self.root) as directory:
                project = Path(directory)
                self.delegate_fixture(project, owner, target=target)
                self.assert_owner_runs(project)
                files = self.candidate(project)
                with self.assertRaisesRegex(ValueError, 'owner.*reference.*obsolete'):
                    check_candidate(files, project)

    def test_unsupported_multiline_alias_cannot_hide_a_retired_reference(self):
        from devcontainer_setup.__main__ import check_candidate
        self.delegate_fixture(self.root, '  owner: {deps: [devcontainer:old-alias]}\n')
        path = self.root / '.devcontainer/Taskfile.yml'
        path.write_text(path.read_text().replace('  obsolete:\n',
            '  obsolete:\n    aliases:\n      - >-\n        old-alias\n'))
        config = json.loads((self.root / '.devcontainer/devcontainer.json').read_bytes())
        config['customizations']['devcontainerSetup']['managedFiles']['.devcontainer/Taskfile.yml'] = (
            hashlib.sha256(path.read_bytes()).hexdigest())
        (self.root / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
        self.assert_owner_runs(self.root)
        with self.assertRaisesRegex(ValueError, 'reference.*aliases'):
            check_candidate(self.candidate(self.root), self.root)

    def test_implicit_include_default_alias_remains_a_protected_reference(self):
        from devcontainer_setup.__main__ import check_candidate
        self.delegate_fixture(self.root, '  owner: {cmds: [{task: devcontainer}]}\n', target='default')
        self.assert_owner_runs(self.root)
        with self.assertRaisesRegex(ValueError, 'owner.*reference.*devcontainer'):
            check_candidate(self.candidate(self.root), self.root)

    def test_trailing_task_list_trivia_preserves_reference_and_alias_semantics(self):
        from devcontainer_setup.__main__ import check_candidate
        for trivia in ('\n', '      # owner comment\n'):
            forms = (
                ('  owner:\n    cmds:\n      - task: devcontainer:obsolete\n' + trivia + '  next: {}\n', 'owner'),
                ('  owner:\n    deps:\n      - task: devcontainer:obsolete\n' + trivia + '  next: {}\n', 'owner'),
                ('  owner:\n    deps:\n      - devcontainer:obsolete\n' + trivia + '  next: {}\n', 'owner'),
                ('  owner:\n    aliases:\n      - shortcut\n' + trivia
                 + '    cmds: [{task: devcontainer:obsolete}]\n  next: {}\n', 'shortcut'),
            )
            for owner, invocation in forms:
                with self.subTest(trivia=trivia, invocation=invocation, owner=owner), tempfile.TemporaryDirectory(dir=self.root) as directory:
                    project = Path(directory)
                    self.delegate_fixture(project, owner)
                    self.assert_owner_runs(project, invocation)
                    original = (project / 'Taskfile.yml').read_bytes()
                    missing = self.candidate(project)
                    retained = dict(missing)
                    retained['.devcontainer/Taskfile.yml'] += b'  obsolete:\n    cmds: [echo OWNER_DELEGATE]\n'
                    config = json.loads(retained['.devcontainer/devcontainer.json'])
                    config['customizations']['devcontainerSetup']['managedFiles']['.devcontainer/Taskfile.yml'] = (
                        hashlib.sha256(retained['.devcontainer/Taskfile.yml']).hexdigest())
                    retained['.devcontainer/devcontainer.json'] = json.dumps(config).encode()
                    snapshot = dict(retained)
                    check_candidate(retained, project)
                    self.assertEqual(retained, snapshot)
                    self.assertEqual((project / 'Taskfile.yml').read_bytes(), original)
                    snapshot = dict(missing)
                    with self.assertRaisesRegex(ValueError, 'owner.*reference.*devcontainer:obsolete'):
                        check_candidate(missing, project)
                    self.assertEqual(missing, snapshot)
                    self.assertEqual((project / 'Taskfile.yml').read_bytes(), original)
                    for name, data in retained.items():
                        path = project / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if data is not None:
                            path.write_bytes(data)
                    self.assertIn(owner.encode(), (project / 'Taskfile.yml').read_bytes())
                    self.assert_owner_runs(project, invocation)

    def test_trivia_filter_does_not_accept_a_multiline_quoted_alias(self):
        from devcontainer_setup.__main__ import check_candidate
        owner = ("  owner:\n    aliases:\n      - 'short\n        #cut'\n"
                 '    cmds: [{task: devcontainer:obsolete}]\n')
        self.delegate_fixture(self.root, owner)
        self.assert_owner_runs(self.root)
        retained = self.candidate(self.root)
        retained['.devcontainer/Taskfile.yml'] += b'  obsolete:\n    cmds: [echo OWNER_DELEGATE]\n'
        config = json.loads(retained['.devcontainer/devcontainer.json'])
        config['customizations']['devcontainerSetup']['managedFiles']['.devcontainer/Taskfile.yml'] = (
            hashlib.sha256(retained['.devcontainer/Taskfile.yml']).hexdigest())
        retained['.devcontainer/devcontainer.json'] = json.dumps(config).encode()
        with self.assertRaisesRegex(ValueError, 'quoted Taskfile scalar'):
            check_candidate(retained, self.root)

    def test_disable_and_reenable_retires_only_verified_aliases(self):
        from devcontainer_setup.__main__ import check_candidate, integrate_continuity, integrate_taskfile
        (self.root / 'Taskfile.yml').write_text(
            "version: '3'\ntasks:\n    'work:cs':\n        cmds: [echo CUSTOM]\n")
        for enabled in (True, False, True, False):
            files = render(self.root, SOURCE, dict(self.selection, continuity=enabled), REF)
            integrate_taskfile(self.root, files)
            integrate_continuity(self.root, files, enabled)
            check_candidate(files, self.root)
            self.install(files)
            names = self.inventory()
            self.assertIn('work:cs', names)
            self.assertEqual('work:status' in names, enabled)
            result = subprocess.run(['task', 'work:cs'], cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('CUSTOM', result.stdout)

    def test_git_add_all_tracks_markers_and_instructions_but_excludes_working_notes(self):
        from devcontainer_setup.__main__ import integrate_continuity, integrate_taskfile
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        files = render(self.root, SOURCE, self.selection, REF)
        integrate_taskfile(self.root, files)
        integrate_continuity(self.root, files, True)
        self.install(files)
        memory = self.root / '.scratch/memory'
        memory.mkdir()
        (memory / 'MEMORY.md').write_text('Project-only working notes\n')
        (self.root / '.scratch/experiment.py').write_text('print("scratch")\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '--all'], check=True)
        indexed = subprocess.check_output(['git', '-C', str(self.root), 'ls-files'], text=True).splitlines()
        self.assertIn('.scratch/.gitignore', indexed)
        self.assertIn('.scratch/README.md', indexed)
        self.assertIn('AGENTS.md', indexed)
        self.assertNotIn('.scratch/memory/MEMORY.md', indexed)
        self.assertNotIn('.scratch/experiment.py', indexed)


if __name__ == '__main__':
    unittest.main()
