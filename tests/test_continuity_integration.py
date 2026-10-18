"""Consumer generation and real Taskfile surface for project continuity."""
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
