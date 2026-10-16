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

    def config(self, files):
        return json.loads(files['.devcontainer/devcontainer.json'])

    def test_minimal_is_selective_and_does_not_write(self):
        files = self.render()
        config = self.config(files)
        self.assertIn(PYTHON, config['features'])
        self.assertIn('ghcr.io/va-h/devcontainers-features/uv:1', config['features'])
        self.assertIn('ghcr.io/devcontainers-extra/features/go-task:1', config['features'])
        for runtime in ('node', 'ruby', 'rust', 'go'):
            self.assertNotIn(f'ghcr.io/devcontainers/features/{runtime}:1', config['features'])
        self.assertFalse(list(self.project.iterdir()))
        joined = b'\n'.join(files.values())
        self.assertNotIn(b'emsilva/dotfiles', joined)
        self.assertNotIn(b'GOMODCACHE', joined)
        self.assertNotIn(b'.npm', joined)
        self.assertNotIn('.devcontainer/Dockerfile', files)
        self.assertNotIn('Taskfile.yml', files)

    def test_feature_lock_and_agent_node_dependency(self):
        files = self.render(runtimes={'node': '24'}, agents=True, tools=['azure', 'docker'])
        config = self.config(files)
        lock = json.loads(files['.devcontainer/devcontainer-lock.json'])
        self.assertEqual(config['features'][NODE]['version'], '24')
        self.assertIn('ghcr.io/anthropics/devcontainer-features/claude-code:1.0', config['features'])
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
            if name.endswith('.sh'):
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

    def test_rejects_conflicting_project_and_agent_node_versions(self):
        with self.assertRaisesRegex(ValueError, 'Node|node'):
            self.render(agents=True, runtimes={'node': '16'})

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
        self.install(self.render())
        upstream = self.project / 'upstream/scripts'
        upstream.mkdir(parents=True)
        (upstream / 'setup.sh').write_text('printf "%s\\n" "$@"\n')
        archive = self.project / 'upstream.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            bundle.add(upstream.parent, arcname='release')
        fake_bin = self.project / 'bin'
        fake_bin.mkdir()
        curl = fake_bin / 'curl'
        curl.write_text('#!/usr/bin/env bash\nwhile [ "$1" != "-o" ]; do shift; done\ncp "$TEST_ARCHIVE" "$2"\n')
        curl.chmod(0o755)
        cache = self.project / 'cache'
        result = subprocess.run(['bash', '.devcontainer/scripts/setup-wizard.sh', '--check'],
                                cwd=self.project, text=True, capture_output=True,
                                env=dict(os.environ, XDG_CACHE_HOME=str(cache), TEST_ARCHIVE=str(archive),
                                         PATH=str(fake_bin)+os.pathsep+os.environ['PATH']))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['--project', str(self.project.resolve()), '--check'])
        self.assertTrue((cache / 'devcontainer-setup/releases' / REF / 'scripts/setup.sh').is_file())
        self.assertFalse(list((cache / 'devcontainer-setup/releases').glob('.download.*')))

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
