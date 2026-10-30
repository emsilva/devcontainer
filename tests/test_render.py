import hashlib
import json
import os
import subprocess
import tarfile
import shutil
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REF = 'a' * 40
PYTHON = 'ghcr.io/devcontainers/features/python:1'
NODE = 'ghcr.io/devcontainers/features/node:1'


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.selection = dict(runtimes={}, tools=[], agents=False, dotfiles=False,
                              ssh=False, ports=[], package_managers={}, polyglot=False)

    def render(self, **changes):
        from devcontainer_setup.render import render
        return render(self.project, ROOT, dict(self.selection, **changes), REF)

    def install(self, files):
        for name, data in files.items():
            target = self.project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def stub_post_create_installers(self):
        for name in ('install-agents.sh', 'install-tools.sh'):
            (self.project / '.devcontainer/scripts' / name).write_text('exit 0\n')
        tools = self.project / 'post-create-bin'
        tools.mkdir()
        sudo = tools / 'sudo'
        sudo.write_text('#!/bin/sh\nexit 0\n')
        sudo.chmod(0o755)
        return str(tools) + os.pathsep + os.environ['PATH']

    def config(self, files):
        return json.loads(files['.devcontainer/devcontainer.json'])

    def legacy_fixture(self):
        with tarfile.open(ROOT / 'tests/fixtures/legacy-base.tar.gz') as bundle:
            bundle.extractall(self.project, filter='data')

    def test_node_is_a_baseline_runtime(self):
        from devcontainer_setup.render import validate
        files = self.render()
        catalog = json.loads((ROOT / '.devcontainer/devcontainer.json').read_text())
        features = self.config(files)['features']
        self.assertIn(NODE, features)
        self.assertEqual(features[NODE]['version'], catalog['features'][NODE]['version'])
        self.install(files)
        self.assertEqual(validate(self.project), [])

    def test_project_node_version_wins(self):
        baseline = self.config(self.render())['features']
        self.assertIn(NODE, baseline)
        self.assertEqual(self.config(self.render(runtimes={'node': '22'}))['features'][NODE]['version'], '22')

    def test_validate_requires_node_feature(self):
        from devcontainer_setup.render import validate
        files = self.render()
        config = self.config(files)
        config['features'].pop(NODE, None)
        config['customizations']['devcontainerSetup']['managedConfig']['features'].pop(NODE, None)
        files['.devcontainer/devcontainer.json'] = json.dumps(config).encode()
        self.install(files)
        self.assertIn('Missing selected feature: ' + NODE, validate(self.project))

    def test_plaud_support_tracks_node_major(self):
        from devcontainer_setup import render
        self.assertTrue(hasattr(render, 'plaud_supported'))
        for version, expected in [('16', False), ('17.9', False), ('18', True),
                                  ('22.3', True), ('24', True), ('lts', True)]:
            with self.subTest(version=version):
                self.assertEqual(render.plaud_supported(version), expected)

    def test_native_agents_are_mandatory_and_record_no_node_dependency(self):
        files = self.render(agents=False)
        config = self.config(files)
        metadata = config['customizations']['devcontainerSetup']
        self.assertIs(metadata['selections']['agents'], True)
        self.assertIn(NODE, config['features'])
        self.assertNotIn('node', metadata['dependencies'])
        self.assertNotIn('agents', metadata['pinningLimitations'])
        self.assertFalse(any('codex:' in name or 'claude-code:' in name for name in config['features']))
        self.assertEqual(files['.devcontainer/scripts/install-agents.sh'],
                         (ROOT / '.devcontainer/scripts/install-agents.sh').read_bytes())
        post = files['.devcontainer/scripts/post-create-selected.sh'].decode()
        self.assertLess(post.index('install-agents.sh'), post.index('validate-selected.sh'))
        self.assertIn('install-agents.sh --check', files['.devcontainer/scripts/validate-selected.sh'].decode())
        self.assertIn('upgrade:agents:', files['.devcontainer/Taskfile.yml'].decode())
        self.assertIn('install-agents.sh --update', files['.devcontainer/Taskfile.yml'].decode())
        self.assertNotIn('PATH', config['containerEnv'])
        self.assertTrue(config['remoteEnv']['PATH'].startswith('/home/vscode/.local/bin:/home/vscode/.opencode/bin:'))
        self.assertIn('sudo -n bash .devcontainer/scripts/system-path.sh "$PATH"', post)

    def test_system_path_is_rendered_registered_and_ordered(self):
        from devcontainer_setup.render import validate
        files = self.render()
        name = '.devcontainer/scripts/system-path.sh'
        self.assertIn(name, files)
        self.assertEqual(files[name], (ROOT / 'templates/scripts/system-path.sh').read_bytes())
        post = files['.devcontainer/scripts/post-create-selected.sh'].decode()
        self.assertLess(post.index('system-path.sh'), post.index('install-agents.sh'))
        self.assertLess(post.index('install-agents.sh'), post.index('validate-selected.sh'))
        check = files['.devcontainer/scripts/validate-selected.sh'].decode()
        self.assertNotIn('export PATH=', check)
        self.assertIn('fresh_shell_lookup', check)
        self.assertIn('sudo -n bash templates/scripts/system-path.sh "$PATH"',
                      (ROOT / '.devcontainer/scripts/post-create.sh').read_text())
        self.install(files)
        self.assertEqual(validate(self.project), [])
        config = self.config(files)
        del config['customizations']['devcontainerSetup']['managedFiles'][name]
        (self.project / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
        self.assertIn('Missing managed script registration: ' + name, validate(self.project))

    def test_generated_tools_are_locked_registered_and_ordered(self):
        from devcontainer_setup.render import validate
        files = self.render()
        assets = ['scripts/install-tools.sh', 'tools/ccswap-constraints.txt',
                  'tools/plaud-cli/package.json', 'tools/plaud-cli/package-lock.json', 'tools/README.md']
        for asset in assets:
            self.assertIn('.devcontainer/' + asset, files)
        post = files['.devcontainer/scripts/post-create-selected.sh'].decode()
        self.assertLess(post.index('install-agents.sh'), post.index('install-tools.sh'))
        self.assertLess(post.index('install-tools.sh'), post.index('validate-selected.sh'))
        check = files['.devcontainer/scripts/validate-selected.sh'].decode()
        self.assertIn('install-tools.sh --check', check)
        commands = check.split('for tool in ', 1)[1].split(';', 1)[0].split()
        fresh = check.split('for tool in ', 2)[2].split(';', 1)[0].split()
        for tool in ('ccswap', 'plaud', 'node'):
            self.assertIn(tool, commands)
            self.assertIn(tool, fresh)
        task = files['.devcontainer/Taskfile.yml'].decode()
        self.assertLess(task.index('install-agents.sh --update'), task.index('install-tools.sh --update'))
        self.assertNotIn('install-tools.sh', (ROOT / '.devcontainer/scripts/post-create.sh').read_text())
        shared = (ROOT / '.devcontainer/scripts/install-agents.sh').read_text()
        self.assertNotIn('ccswap', shared)
        self.assertNotIn('plaud', shared)
        self.install(files)
        self.assertEqual(validate(self.project), [])
        for asset in assets:
            config = self.config(files)
            name = '.devcontainer/' + asset
            del config['customizations']['devcontainerSetup']['managedFiles'][name]
            (self.project / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
            self.assertIn('Missing managed script registration: ' + name, validate(self.project))

    def test_plaud_is_omitted_for_old_node(self):
        for version, expected in [('16', False), ('18', True), ('lts', True)]:
            with self.subTest(version=version):
                check = self.render(runtimes={'node': version})['.devcontainer/scripts/validate-selected.sh'].decode()
                commands = check.split('for tool in ', 1)[1].split(';', 1)[0].split()
                fresh = check.split('for tool in ', 2)[2].split(';', 1)[0].split()
                self.assertIn('ccswap', commands)
                self.assertEqual('plaud' in commands, expected)
                self.assertEqual('plaud' in fresh, expected)

    def test_adopted_node_version_controls_plaud_requirement(self):
        target = self.project / '.devcontainer/devcontainer.json'
        target.parent.mkdir()
        target.write_text(json.dumps({'features': {NODE: {'version': '16'}}}))
        files = self.render(polyglot=True)
        self.assertEqual(self.config(files)['features'][NODE]['version'], '16')
        check = files['.devcontainer/scripts/validate-selected.sh'].decode()
        self.assertIn('ccswap', check.split('for tool in ', 1)[1].split(';', 1)[0].split())
        for loop in check.split('for tool in ')[1:]:
            self.assertNotIn('plaud', loop.split(';', 1)[0].split())

    def test_old_managed_agents_migrate_while_custom_features_survive(self):
        from devcontainer_setup.render import validate
        files = self.render()
        config = self.config(files)
        metadata = config['customizations']['devcontainerSetup']
        lock = json.loads(files['.devcontainer/devcontainer-lock.json'])
        legacy = ['ghcr.io/anthropics/devcontainer-features/claude-code:1.0',
                  'ghcr.io/jsburckhardt/devcontainer-features/codex:1']
        extra = 'ghcr.io/example/features/custom:1'
        for name in legacy + [extra]:
            config['features'][name] = {}
            lock['features'][name] = {'version': '1.0.0', 'resolved': name.split(':')[0] + '@sha256:' + 'e' * 64,
                                      'integrity': 'sha256:' + 'e' * 64}
        for name in legacy:
            metadata['managedConfig']['features'][name] = {}
        metadata['selections']['agents'] = False
        helper = '.devcontainer/scripts/install-agents.sh'
        del files[helper]
        del metadata['managedFiles'][helper]
        files['.devcontainer/devcontainer-lock.json'] = json.dumps(lock).encode()
        metadata['managedFiles']['.devcontainer/devcontainer-lock.json'] = hashlib.sha256(files['.devcontainer/devcontainer-lock.json']).hexdigest()
        files['.devcontainer/devcontainer.json'] = json.dumps(config).encode()
        self.install(files)
        updated = self.render(agents=False)
        config = self.config(updated)
        lock = json.loads(updated['.devcontainer/devcontainer-lock.json'])
        for name in legacy:
            self.assertNotIn(name, config['features'])
            self.assertNotIn(name, lock['features'])
        self.assertIn(extra, config['features'])
        self.assertIn(extra, lock['features'])
        self.install(updated)
        self.assertEqual(validate(self.project), [])
        self.assertEqual(updated, self.render(agents=False))

    def test_adoption_rejects_incompatible_agent_user_home_or_path_without_writes(self):
        dc = self.project / '.devcontainer'
        dc.mkdir()
        for fields in ({'remoteUser': 'alice'}, {'remoteEnv': {'HOME': '/home/alice'}},
                       {'containerEnv': {'HOME': '/root'}},
                       {'remoteEnv': {'PATH': '/custom/bin:${containerEnv:PATH}'}}):
            with self.subTest(fields=fields):
                config = {'image': 'custom:image', **fields}
                path = dc / 'devcontainer.json'
                before = json.dumps(config).encode()
                path.write_bytes(before)
                with self.assertRaisesRegex(ValueError, 'Native agents.*reviewed migration'):
                    self.render(polyglot=True)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(dc.iterdir()), [path])

    def test_adopted_agent_environment_cannot_drift_past_validation(self):
        from devcontainer_setup.render import validate
        dc = self.project / '.devcontainer'
        dc.mkdir()
        baseline = {'image': 'custom:image', 'remoteUser': 'vscode',
                    'remoteEnv': {'HOME': '/home/vscode', 'PATH': '/home/vscode/.local/bin:/home/vscode/.opencode/bin:/custom/bin:${containerEnv:PATH}'},
                    'containerEnv': {'HOME': '/home/vscode'}}
        (dc / 'devcontainer.json').write_text(json.dumps(baseline))
        files = self.render(polyglot=True)
        self.install(files)
        self.assertEqual(validate(self.project), [])
        self.assertEqual(files, self.render(polyglot=True))
        for fields in ({'remoteUser': 'alice'}, {'remoteEnv': {'HOME': '/home/alice'}},
                       {'containerEnv': {'HOME': '/root'}},
                       {'remoteEnv': {'PATH': '/custom/bin:${containerEnv:PATH}'}}):
            with self.subTest(fields=fields):
                config = self.config(files)
                for name, value in fields.items():
                    if isinstance(value, dict):
                        config[name].update(value)
                    else:
                        config[name] = value
                (dc / 'devcontainer.json').write_text(json.dumps(config))
                self.assertTrue(any('Native agents' in error for error in validate(self.project)))
                with self.assertRaisesRegex(ValueError, 'Native agents.*reviewed migration'):
                    self.render(polyglot=True)

    def test_known_legacy_base_adoption_validates_and_repeats(self):
        from devcontainer_setup.__main__ import check_candidate
        from devcontainer_setup.render import validate
        self.legacy_fixture()
        old_helper = self.project / '.devcontainer/scripts/post-create.sh'
        before = old_helper.read_bytes()
        custom = self.project / '.devcontainer/custom-project.txt'
        custom.write_text('project-owned')
        files = self.render(polyglot=True, agents=True, dotfiles=True,
                            runtimes={'python': '3.13'})
        check_candidate(files, self.project)
        config = self.config(files)
        self.assertNotIn('onCreateCommand', config)
        self.assertNotIn('postStartCommand', config)
        self.assertNotIn('post-create.sh', json.dumps(config['postCreateCommand']))
        self.install(files)
        self.assertEqual(old_helper.read_bytes(), before)
        self.assertEqual(custom.read_text(), 'project-owned')
        self.assertNotIn('.devcontainer/custom-project.txt', files)
        self.assertEqual(validate(self.project), [])
        self.assertEqual(files, self.render(polyglot=True, agents=True, dotfiles=True,
                                           runtimes={'python': '3.13'}))

    def test_modified_legacy_files_are_protected(self):
        self.legacy_fixture()
        for relative in ('.devcontainer/Taskfile.yml', '.devcontainer/scripts/post-create.sh',
                         '.devcontainer/devcontainer.json'):
            with self.subTest(relative=relative):
                path = self.project / relative
                before = path.read_bytes()
                path.write_bytes(before + b'\n# custom change\n' if not relative.endswith('.json')
                                 else before.replace(b'Polyglot', b'Custom'))
                changed = path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'legacy|unmanaged'):
                    self.render(polyglot=True)
                self.assertEqual(path.read_bytes(), changed)
                path.write_bytes(before)

    def test_pnpm_explicit_version_is_installed_and_runtime_checked(self):
        from devcontainer_setup.render import validate
        files = self.render(package_managers={'node': 'pnpm'},
                            package_manager_versions={'node': '9.15.4'})
        self.assertEqual(self.config(files)['features'][NODE]['pnpmVersion'], '9.15.4')
        self.install(files)
        self.assertEqual(validate(self.project), [])
        self.assertEqual(files, self.render(package_managers={'node': 'pnpm'},
                                           package_manager_versions={'node': '9.15.4'}))
        helper = self.project / '.devcontainer/scripts/validate-selected.sh'
        # These probes isolate validation from both installer check phases.
        for installer in ('install-agents.sh', 'install-tools.sh'):
            (self.project / '.devcontainer/scripts' / installer).write_text('exit 0\n')
        content = helper.read_text()
        self.assertIn('fresh_shell_lookup', content)
        commands = content.split('for tool in ', 1)[1].split(';', 1)[0].split()
        bins = self.project / 'bin'
        bins.mkdir()
        for command in commands:
            if command == 'python3':
                continue
            path = bins / command
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        system = self.project / 'system/etc'
        system.mkdir(parents=True)
        (system / 'environment').write_text('PATH="' + str(bins) + os.pathsep + os.environ['PATH'] + '"\n')
        manager = bins / 'pnpm'
        for version, expected in [('9.15.4', 0), ('12.11.2', 1)]:
            manager.write_text('#!/bin/sh\necho ' + version + '\n')
            result = subprocess.run(['/bin/bash', str(helper)], cwd=self.project, capture_output=True, text=True,
                                    env=dict(os.environ, HOME=str(self.project), PATH=str(bins) + os.pathsep + os.environ['PATH'],
                                             DEVCONTAINER_SYSTEM_ROOT=str(system.parent)))
            self.assertEqual(result.returncode, expected, result.stderr)
            if expected:
                self.assertIn('9.15.4', result.stderr)
                self.assertIn('12.11.2', result.stderr)

    def test_unsupported_manager_version_overrides_stop_with_actionable_conflict(self):
        for manager in ('yarn', 'npm', 'bun'):
            with self.subTest(manager=manager), self.assertRaisesRegex(ValueError, 'version.*(support|locked)|locked.*version'):
                self.render(package_managers={'node': manager},
                            package_manager_versions={'node': '9.15.4'})

    def test_minimal_is_selective_and_does_not_write(self):
        files = self.render()
        config = self.config(files)
        self.assertIn(PYTHON, config['features'])
        self.assertIn('ghcr.io/va-h/devcontainers-features/uv:1', config['features'])
        self.assertIn('ghcr.io/devcontainers-extra/features/go-task:1', config['features'])
        self.assertIn(NODE, config['features'])
        for runtime in ('ruby', 'rust', 'go'):
            self.assertNotIn(f'ghcr.io/devcontainers/features/{runtime}:1', config['features'])
        self.assertFalse(list(self.project.iterdir()))
        joined = b'\n'.join(files.values())
        self.assertNotIn(b'emsilva/dotfiles', joined)
        self.assertNotIn(b'GOMODCACHE', joined)
        self.assertFalse(any('.npm' in str(mount) for mount in config.get('mounts', [])))
        self.assertIn('.devcontainer/tools/plaud-cli/package-lock.json', files)
        self.assertNotIn('.devcontainer/Dockerfile', files)
        self.assertNotIn('Taskfile.yml', files)

    def test_feature_lock_and_independent_node_selection(self):
        files = self.render(runtimes={'node': '24'}, agents=True, tools=['azure', 'docker'])
        config = self.config(files)
        lock = json.loads(files['.devcontainer/devcontainer-lock.json'])
        self.assertEqual(config['features'][NODE]['version'], '24')
        self.assertNotIn('ghcr.io/anthropics/devcontainer-features/claude-code:1.0', config['features'])
        self.assertEqual(set(config['features']), set(lock['features']))
        self.assertTrue(all('@sha256:' not in key for key in config['features']))
        self.assertTrue(all('@sha256:' in value['resolved'] for value in lock['features'].values()))
        self.assertIn(NODE, self.config(self.render(agents=True))['features'])

    def test_repeat_is_identical_and_valid(self):
        from devcontainer_setup.render import validate
        first = self.render(runtimes={'node': '24'}, tools=['azure'], ports=[3000])
        self.install(first)
        self.assertEqual(first, self.render(runtimes={'node': '24'}, tools=['azure'], ports=[3000]))
        self.assertEqual(validate(self.project), [])

    def test_user_ports_commands_settings_and_files_survive(self):
        files = self.render(ports=[3000])
        self.install(files)
        config = self.config(files)
        config['forwardPorts'].append(4567)
        config['postCreateCommand']['project'] = 'echo project'
        config['customizations']['vscode']['settings']['my.setting'] = True
        (self.project / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
        (self.project / '.devcontainer/custom.txt').write_text('mine')
        result = self.render(ports=[8080])
        updated = self.config(result)
        self.assertIn(4567, updated['forwardPorts'])
        self.assertIn(8080, updated['forwardPorts'])
        self.assertEqual(updated['postCreateCommand']['project'], 'echo project')
        self.assertTrue(updated['customizations']['vscode']['settings']['my.setting'])
        self.assertNotIn('.devcontainer/custom.txt', result)

    def test_changed_managed_helper_and_feature_stop_update(self):
        files = self.render()
        self.install(files)
        helper = self.project / '.devcontainer/scripts/validate-selected.sh'
        helper.write_text('edited')
        with self.assertRaisesRegex(ValueError, 'validate-selected'):
            self.render()
        self.install(files)
        config = self.config(files)
        config['features'][PYTHON]['version'] = '3.9'
        (self.project / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'features'):
            self.render()

    def test_selected_build_extras_and_small_runtime_helpers(self):
        files = self.render(dotfiles=True, tools=['devtunnel', 'precommit'], ssh=True)
        self.assertIn('.devcontainer/Dockerfile', files)
        dockerfile = files['.devcontainer/Dockerfile'].decode()
        self.assertIn('sha256sum -c', dockerfile)
        self.assertIn('chezmoi', dockerfile)
        self.assertIn('devtunnel', dockerfile)
        self.assertIn('pre-commit==', dockerfile)
        self.assertIn('.devcontainer/scripts/apply-dotfiles.sh', files)
        config = self.config(files)
        self.assertIn(2222, config['forwardPorts'])
        self.assertNotIn('image', config)
        self.assertEqual(config['build']['dockerfile'], 'Dockerfile')
        for name, data in files.items():
            if name.endswith('.sh') and name != '.devcontainer/scripts/install-agents.sh':
                self.assertLess(len(data.splitlines()), 100)

    def test_unmanaged_environment_requires_explicit_polyglot(self):
        dc = self.project / '.devcontainer'
        dc.mkdir()
        (dc / 'devcontainer.json').write_text(json.dumps({'image': 'custom:image', 'features': {NODE: {'version': '22'}}, 'postCreateCommand': 'echo mine'}))
        with self.assertRaisesRegex(ValueError, 'unmanaged|migration|polyglot'):
            self.render()
        result = self.render(polyglot=True)
        config = self.config(result)
        self.assertEqual(config['image'], 'custom:image')
        self.assertEqual(config['features'][NODE]['version'], '22')
        self.assertIn('echo mine', json.dumps(config['postCreateCommand']))

    def test_missing_or_modified_lock_and_helper_fail_validation(self):
        from devcontainer_setup.render import validate
        self.install(self.render())
        (self.project / '.devcontainer/devcontainer-lock.json').write_text('{"features":{}}')
        self.assertTrue(validate(self.project))
        (self.project / '.devcontainer/scripts/validate-selected.sh').unlink()
        self.assertTrue(any('validate-selected' in error for error in validate(self.project)))

    def test_lock_transitive_dependency_closure(self):
        from devcontainer_setup.render import render
        source = self.project / 'source'
        (source / '.devcontainer').mkdir(parents=True)
        for name in ('devcontainer.json', 'devcontainer-lock.json'):
            shutil.copyfile(ROOT / '.devcontainer' / name, source / '.devcontainer' / name)
        shutil.copytree(ROOT / 'templates', source / 'templates')
        shutil.copytree(ROOT / '.devcontainer/scripts', source / '.devcontainer/scripts')
        lock_path = source / '.devcontainer/devcontainer-lock.json'
        lock = json.loads(lock_path.read_text())
        dependency = 'ghcr.io/schlich/devcontainer-features/starship:0'
        lock['features'][PYTHON]['dependsOn'] = [dependency]
        lock_path.write_text(json.dumps(lock))
        result = render(self.project, source, self.selection, REF)
        config = self.config(result)
        locked = json.loads(result['.devcontainer/devcontainer-lock.json'])['features']
        self.assertIn(dependency, config['features'])
        self.assertIn(dependency, locked)
        self.assertIn('ghcr.io/devcontainers/features/common-utils:2', locked)

    def test_polyglot_validates_all_selected_tool_commands(self):
        result = self.render(polyglot=True)
        commands = result['.devcontainer/scripts/validate-selected.sh'].decode()
        for name in ('aws', 'az', 'gcloud', 'docker', 'kubectl', 'helm'):
            self.assertIn(name, commands.split('for tool in ', 1)[1].split(';', 1)[0].split())

    def test_native_agents_do_not_restrict_project_node_version(self):
        self.assertEqual(self.config(self.render(agents=True, runtimes={'node': '16'}))['features'][NODE]['version'], '16')

    def test_package_manager_requires_its_runtime(self):
        result = self.render(package_managers={'node': 'pnpm'})
        self.assertIn(NODE, self.config(result)['features'])
        self.assertNotEqual(self.config(result)['features'][NODE]['pnpmVersion'], 'none')

    def test_managed_runtime_version_tampering_fails_validation(self):
        from devcontainer_setup.render import validate
        files = self.render()
        self.install(files)
        config = self.config(files)
        config['features'][PYTHON]['version'] = '3.9'
        (self.project / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
        self.assertTrue(any('features' in error for error in validate(self.project)))

    def test_removing_managed_command_stops_update(self):
        files = self.render()
        self.install(files)
        config = self.config(files)
        config['postCreateCommand'] = {'project': 'echo mine'}
        (self.project / '.devcontainer/devcontainer.json').write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'command'):
            self.render()

    def test_directory_at_generated_file_path_is_actionable(self):
        (self.project / '.devcontainer/scripts/validate-selected.sh').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'file|directory'):
            self.render()

    def test_cached_launcher_runs_pinned_runner_and_cleans_staging(self):
        from devcontainer_setup.render import render
        upstream = self.project / 'upstream'
        (upstream / 'scripts').mkdir(parents=True)
        (upstream / 'scripts/setup.sh').write_text('printf "%s\\n" "$@" "${DEVCONTAINER_SETUP_REF:-MISSING}"\n')
        (upstream / 'devcontainer_setup').mkdir()
        for name in ('__init__.py', 'releases.py', 'ui.py', '__main__.py'):
            shutil.copyfile(ROOT / 'devcontainer_setup' / name, upstream / 'devcontainer_setup' / name)
        terminal = upstream / 'devcontainer_setup/project_sync'
        terminal.mkdir()
        for name in ('__init__.py', 'terminal.py'):
            shutil.copyfile(ROOT / 'devcontainer_setup/project_sync' / name, terminal / name)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(upstream), *args], text=True).strip()
        git('init', '-b', 'main')
        git('config', 'user.name', 'Render Test')
        git('config', 'user.email', 'render@example.com')
        git('add', '.')
        git('commit', '-m', 'Fixture')
        sha = git('rev-parse', 'HEAD')
        self.install(render(self.project, ROOT, self.selection, sha))
        cache = self.project / 'cache'
        result = subprocess.run(['bash', '.devcontainer/scripts/setup-wizard.sh', '--check'],
                                cwd=self.project, text=True, capture_output=True,
                                env=dict(os.environ, XDG_CACHE_HOME=str(cache),
                                         SETUP_DEVCONTAINER_REPO=str(upstream)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['--project', str(self.project.resolve()), '--check', sha])
        self.assertTrue((cache / 'devcontainer-setup/releases' / sha / 'scripts/setup.sh').is_file())
        self.assertFalse(list((cache / 'devcontainer-setup/releases').glob('.download.*')))

    def test_local_dotfiles_file_is_data_and_codespaces_environment_wins(self):
        files = self.render(dotfiles=True)
        self.assertFalse((self.project / '.devcontainer').exists())
        self.install(files)
        validator = self.project / '.devcontainer/scripts/validate-selected.sh'
        validator.write_text('exit 0\n')
        apply = self.project / '.devcontainer/scripts/apply-dotfiles.sh'
        apply.write_text('printf "%s\\n" "$1" >> "$TEST_LOG"\n')
        literal = 'https://example.invalid/$(touch owned).git'
        (self.project / '.devcontainer/devcontainer.env').write_text('# Local runtime settings\nPERSONAL_PAT=private-fixture\nDOTFILES_REPO=' + literal + '\nSSH_AUTH_SOCK=/tmp/example\n')
        base_env = dict(os.environ, TEST_LOG=str(self.project / 'apply.log'),
                        PATH=self.stub_post_create_installers())
        base_env.pop('DOTFILES_REPO', None)
        base_env.pop('CODESPACES', None)
        cases = [({}, literal), ({'DOTFILES_REPO': 'existing/local'}, 'existing/local'),
                 ({'CODESPACES': 'true', 'DOTFILES_REPO': 'remote/repo'}, 'remote/repo'),
                 ({'CODESPACES': 'true'}, None), ({'CODESPACES': ''}, None)]
        for additions, expected in cases:
            with self.subTest(environment=additions):
                log = self.project / 'apply.log'
                log.unlink(missing_ok=True)
                result = subprocess.run(['bash', '.devcontainer/scripts/post-create-selected.sh'],
                                        cwd=self.project, env=dict(base_env, **additions),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if expected is None:
                    self.assertFalse(log.exists())
                else:
                    self.assertTrue(log.exists())
                    self.assertEqual(log.read_text().splitlines(), [expected])
                self.assertFalse((self.project / 'owned').exists())

    def test_dotfiles_with_a_zshrc_make_zsh_the_login_shell(self):
        import shutil
        script = Path(__file__).resolve().parents[1] / 'templates/scripts/apply-dotfiles.sh'
        for brings_zshrc, current, switches in ((True, '/bin/bash', True), (False, '/bin/bash', False),
                                                (True, 'ZSH', False)):
            with self.subTest(brings_zshrc=brings_zshrc, current=current), tempfile.TemporaryDirectory() as home:
                tools, log = Path(home) / 'tools', Path(home) / 'calls.log'
                tools.mkdir()
                zsh = tools / 'zsh'
                def tool(name, body):
                    (tools / name).write_text('#!/bin/sh\n' + body + '\n')
                    (tools / name).chmod(0o755)
                tool('zsh', 'exit 0')
                tool('chezmoi', 'touch "$HOME/.zshrc"' if brings_zshrc else 'exit 0')
                tool('getent', f'echo "tester:x:1000:1000::$HOME:{zsh if current == "ZSH" else current}"')
                tool('sudo', 'echo "sudo $*" >> "$TEST_LOG"')
                if not shutil.which('sha256sum'):
                    tool('sha256sum', 'shasum -a 256')
                env = dict(os.environ, HOME=home, PATH=f'{tools}:{os.environ["PATH"]}', TEST_LOG=str(log))
                env.pop('XDG_STATE_HOME', None)
                for _ in range(2):  # The second run takes the already-applied path.
                    log.unlink(missing_ok=True)
                    result = subprocess.run(['bash', str(script), 'owner/dotfiles'], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = log.read_text().splitlines() if log.exists() else []
                    user = subprocess.check_output(['id', '-un'], text=True).strip()
                    self.assertEqual(calls, [f'sudo -n chsh -s {zsh} {user}'] if switches else [])

    def test_duplicate_local_dotfiles_assignments_are_rejected(self):
        self.install(self.render(dotfiles=True))
        (self.project / '.devcontainer/scripts/validate-selected.sh').write_text('exit 0\n')
        (self.project / '.devcontainer/scripts/apply-dotfiles.sh').write_text('touch should-not-apply\n')
        (self.project / '.devcontainer/devcontainer.env').write_text('DOTFILES_REPO=one/repo\nOTHER=kept\nDOTFILES_REPO=two/repo\n')
        env = dict(os.environ, PATH=self.stub_post_create_installers())
        env.pop('CODESPACES', None)
        env.pop('DOTFILES_REPO', None)
        result = subprocess.run(['bash', '.devcontainer/scripts/post-create-selected.sh'],
                                cwd=self.project, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DOTFILES_REPO', result.stderr)
        self.assertFalse((self.project / 'should-not-apply').exists())

    def test_detected_optional_managers_build_before_features(self):
        for family, manager in [('python', 'poetry'), ('python', 'pipenv'), ('node', 'bun')]:
            with self.subTest(manager=manager):
                files = self.render(package_managers={family: manager})
                dockerfile = files['.devcontainer/Dockerfile'].decode()
                if family == 'python':
                    self.assertIn('apt-get install -y --no-install-recommends python3 python3-venv', dockerfile)
                    self.assertIn(manager + '==', dockerfile)
                else:
                    self.assertIn('sha256sum -c', dockerfile)
                    self.assertIn('bun-linux-', dockerfile)
                self.assertIn(manager, files['.devcontainer/scripts/validate-selected.sh'].decode())

    def test_adoption_rerun_preserves_all_user_owned_configuration(self):
        dc = self.project / '.devcontainer'
        dc.mkdir()
        original = {'image': 'custom:image', 'features': {NODE: {'version': '22'}},
                    'forwardPorts': [3000], 'onCreateCommand': 'echo preparation',
                    'postCreateCommand': 'echo mine',
                    'mounts': ['source=mine,target=/custom,type=volume'],
                    'customizations': {'vscode': {'settings': {'editor.tabSize': 7}}}}
        (dc / 'devcontainer.json').write_text(json.dumps(original))
        first = self.render(polyglot=True)
        self.install(first)
        second = self.render(polyglot=True)
        self.assertEqual(first, second)
        config = self.config(second)
        for key in ('image', 'forwardPorts', 'onCreateCommand', 'mounts'):
            self.assertEqual(config[key], original[key])
        self.assertEqual(config['features'][NODE]['version'], '22')
        self.assertEqual(config['postCreateCommand']['project'], 'echo mine')
        config['image'] = 'custom:new'
        config['features'][NODE]['version'] = '22.1'
        (dc / 'devcontainer.json').write_text(json.dumps(config))
        updated = self.config(self.render(polyglot=True))
        self.assertEqual(updated['image'], 'custom:new')
        self.assertEqual(updated['features'][NODE]['version'], '22.1')

    def test_adoption_build_extras_use_only_build_shape(self):
        from devcontainer_setup.render import validate
        dc = self.project / '.devcontainer'
        dc.mkdir()
        canonical = json.loads((ROOT / '.devcontainer/devcontainer.json').read_text())
        (dc / 'devcontainer.json').write_text(json.dumps({'image': canonical['image']}))
        files = self.render(polyglot=True, dotfiles=True)
        config = self.config(files)
        self.assertIn('build', config)
        self.assertNotIn('image', config)
        self.install(files)
        self.assertEqual(validate(self.project), [])
        self.assertEqual(files, self.render(polyglot=True, dotfiles=True))
        config['image'] = canonical['image']
        (dc / 'devcontainer.json').write_text(json.dumps(config))
        self.assertTrue(any('image' in error and 'build' in error for error in validate(self.project)))

    def test_adoption_existing_build_does_not_add_image(self):
        dc = self.project / '.devcontainer'
        dc.mkdir()
        (dc / 'devcontainer.json').write_text(json.dumps({'build': {'dockerfile': 'Customfile'}}))
        files = self.render(polyglot=True)
        self.assertNotIn('image', self.config(files))
        self.assertEqual(self.config(files)['build'], {'dockerfile': 'Customfile'})
        self.install(files)
        self.assertEqual(files, self.render(polyglot=True))

    def test_node_uses_supported_options_and_declares_yarn_dependency(self):
        files = self.render(runtimes={'node': '24'})
        config = self.config(files)
        self.assertNotIn('installYarn', config['features'][NODE])
        self.assertEqual(config['features'][NODE]['nvmVersion'], '0.40.8')
        metadata = config['customizations']['devcontainerSetup']
        self.assertIn('yarn', metadata['dependencies'])
        self.assertIn('Feature', metadata['dependencies']['yarn'])
        self.assertIn('yarn', files['.devcontainer/scripts/validate-selected.sh'].decode())

    def test_docker_pins_exist_in_both_microsoft_noble_architecture_indices(self):
        availability = json.loads((ROOT / 'tests/fixtures/moby-noble-packages.json').read_text())
        docker = self.config(self.render(tools=['docker']))['features'][
            'ghcr.io/devcontainers/features/docker-in-docker:2']
        self.assertIs(docker['moby'], True)
        for architecture, index in availability['architectures'].items():
            for package, option in [('moby-engine', 'version'), ('moby-cli', 'version'),
                                    ('moby-buildx', 'mobyBuildxVersion')]:
                with self.subTest(architecture=architecture, package=package):
                    available = index['packages'][package]['available_versions']
                    # The locked Feature soft-matches the upstream version before the distro suffix.
                    upstream = {version.split('-')[0] for version in available}
                    self.assertIn(docker[option], upstream)
                    if package != 'moby-buildx':
                        self.assertNotIn('29.9.1', upstream)  # The failing Docker CE-derived pin.

    @staticmethod
    def cli_lock_bytes(lock):
        # Dev Containers CLI 0.83.3 generateLockfile/writeLockfile field order.
        records = {name: {key: entry[key] for key in
                         ('version', 'resolved', 'integrity', 'dependsOn') if key in entry}
                   for name, entry in sorted(lock['features'].items())}
        return json.dumps({'features': records}, indent=2, ensure_ascii=False).encode()

    def install_legacy_lock(self):
        files = self.render(polyglot=True)
        lock_name = '.devcontainer/devcontainer-lock.json'
        config_name = '.devcontainer/devcontainer.json'
        lock = json.loads(files[lock_name])
        files[lock_name] = (json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode()
        config = self.config(files)
        config['customizations']['devcontainerSetup']['managedFiles'][lock_name] = hashlib.sha256(files[lock_name]).hexdigest()
        files[config_name] = json.dumps(config).encode()
        self.install(files)
        return lock

    def test_generated_lock_matches_native_cli_bytes_including_dependencies(self):
        files = self.render(polyglot=True)
        content = files['.devcontainer/devcontainer-lock.json']
        lock = json.loads(content)
        self.assertTrue(any(entry.get('dependsOn') for entry in lock['features'].values()))
        self.assertEqual(content, self.cli_lock_bytes(lock))

    def test_legacy_lock_native_cli_normalization_is_safe_to_validate_and_render(self):
        from devcontainer_setup.render import validate
        lock = self.install_legacy_lock()
        lock_path = self.project / '.devcontainer/devcontainer-lock.json'
        native = self.cli_lock_bytes(lock)
        lock_path.write_bytes(native)
        self.assertEqual(validate(self.project), [])
        updated = self.render(polyglot=True)
        self.assertEqual(json.loads(updated['.devcontainer/devcontainer-lock.json']), lock)
        self.assertEqual(updated['.devcontainer/devcontainer-lock.json'], native)
        self.assertEqual(lock_path.read_bytes(), native)  # Rendering remains read-only.
        self.install(updated)
        self.assertEqual(validate(self.project), [])
        self.assertEqual(updated, self.render(polyglot=True))

    def test_legacy_lock_normalization_rejects_changed_digest_or_dependencies(self):
        from devcontainer_setup.render import validate
        for change in ('resolved', 'integrity', 'dependsOn'):
            with self.subTest(change=change):
                lock = self.install_legacy_lock()
                entry = lock['features'][PYTHON]
                entry[change] = (['ghcr.io/devcontainers/features/common-utils:2'] if change == 'dependsOn'
                                 else ('ghcr.io/devcontainers/features/python@' if change == 'resolved' else '')
                                 + 'sha256:' + 'e' * 64)
                lock_path = self.project / '.devcontainer/devcontainer-lock.json'
                changed = self.cli_lock_bytes(lock)
                lock_path.write_bytes(changed)
                self.assertTrue(any('Modified managed helper/file' in error for error in validate(self.project)))
                with self.assertRaisesRegex(ValueError, 'Modified or missing managed file'):
                    self.render(polyglot=True)
                self.assertEqual(lock_path.read_bytes(), changed)
                # Reset only this temporary test checkout for the next independent mutation.
                shutil.rmtree(self.project / '.devcontainer')

    def test_legacy_lock_arbitrary_reformatting_remains_protected(self):
        from devcontainer_setup.render import validate
        lock = self.install_legacy_lock()
        (self.project / '.devcontainer/devcontainer-lock.json').write_text(json.dumps(lock))
        self.assertTrue(any('Modified managed helper/file' in error for error in validate(self.project)))
        with self.assertRaisesRegex(ValueError, 'Modified or missing managed file'):
            self.render(polyglot=True)

    def test_supported_docker_secondary_pin_and_compose_limitation(self):
        config = self.config(self.render(tools=['docker']))
        docker = config['features']['ghcr.io/devcontainers/features/docker-in-docker:2']
        self.assertEqual(docker['mobyBuildxVersion'], '0.38.0')
        self.assertIn(docker['dockerDashComposeVersion'], ['none', 'v1', 'v2'])
        limitations = config['customizations']['devcontainerSetup']['pinningLimitations']
        self.assertIn('compose', limitations)
        self.assertIn('none/v1/v2', limitations['compose'])

    def refreshed_catalog(self):
        source = self.project / 'refreshed-source'
        (source / '.devcontainer').mkdir(parents=True)
        for name in ('devcontainer.json', 'devcontainer-lock.json'):
            shutil.copyfile(ROOT / '.devcontainer' / name, source / '.devcontainer' / name)
        shutil.copytree(ROOT / 'templates', source / 'templates')
        shutil.copytree(ROOT / '.devcontainer/scripts', source / '.devcontainer/scripts')
        lock_path = source / '.devcontainer/devcontainer-lock.json'
        lock = json.loads(lock_path.read_text())
        updated = dict(lock['features'][PYTHON], version='1.8.99',
                       resolved='ghcr.io/devcontainers/features/python@sha256:' + '9' * 64,
                       integrity='sha256:' + '9' * 64)
        lock['features'][PYTHON] = updated
        lock_path.write_text(json.dumps(lock))
        return source, updated

    def test_same_source_rerun_retains_exact_existing_feature_lock(self):
        from devcontainer_setup.render import render
        original = self.render()
        self.install(original)
        source, refreshed = self.refreshed_catalog()
        result = render(self.project, source, self.selection, REF)
        previous = json.loads(original['.devcontainer/devcontainer-lock.json'])['features'][PYTHON]
        actual = json.loads(result['.devcontainer/devcontainer-lock.json'])['features'][PYTHON]
        self.assertNotEqual(previous, refreshed)
        self.assertEqual(actual, previous)
        self.assertEqual(original, result)

    def test_new_source_refreshes_catalog_locks_and_preserves_custom_features(self):
        from devcontainer_setup.render import render, validate
        custom = 'ghcr.io/example/features/custom:1'
        custom_lock = {'version': '1.2.3', 'integrity': 'sha256:' + 'c' * 64,
                       'resolved': 'ghcr.io/example/features/custom@sha256:' + 'c' * 64}
        dc = self.project / '.devcontainer'
        dc.mkdir()
        canonical = json.loads((ROOT / '.devcontainer/devcontainer.json').read_text())
        (dc / 'devcontainer.json').write_text(json.dumps({'image': canonical['image'],
                                                        'features': {custom: {'version': '1.2.3'}}}))
        (dc / 'devcontainer-lock.json').write_text(json.dumps({'features': {custom: custom_lock}}))
        initial = self.render(polyglot=True)
        self.install(initial)
        source, refreshed = self.refreshed_catalog()
        result = render(self.project, source, dict(self.selection, polyglot=True), 'b' * 40)
        actual = json.loads(result['.devcontainer/devcontainer-lock.json'])['features']
        self.assertEqual(actual[PYTHON], refreshed)
        self.assertEqual(actual[custom], custom_lock)
        self.assertEqual(self.config(result)['features'][custom], {'version': '1.2.3'})
        self.assertEqual(self.config(result)['customizations']['devcontainerSetup']['sourceRef'], 'b' * 40)
        self.install(result)
        self.assertEqual(validate(self.project), [])
        self.assertEqual(result, render(self.project, source, dict(self.selection, polyglot=True), 'b' * 40))

    def test_source_ref_must_be_immutable(self):
        from devcontainer_setup.render import render
        with self.assertRaisesRegex(ValueError, 'revision|source|40'):
            render(self.project, ROOT, self.selection, 'main')

    def test_symlink_target_is_rejected(self):
        (self.project / '.devcontainer').symlink_to(ROOT / '.devcontainer', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.render()


if __name__ == '__main__':
    unittest.main()
