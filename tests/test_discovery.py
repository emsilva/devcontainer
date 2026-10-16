import json
from pathlib import Path
import subprocess
import tempfile
import unittest

try:
    from devcontainer_setup.discovery import scan
except ImportError:
    scan = lambda project: {}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_empty_project_has_python_baseline_and_all_runtime_entries(self):
        found = scan(self.root)
        self.assertEqual(set(found.get('runtimes', {})), {'python', 'node', 'ruby', 'go', 'rust'})
        self.assertEqual(found['runtimes']['python']['version'], '3.13')
        self.assertTrue(found['runtimes']['python']['selected'])
        self.assertEqual(found['ports'], [])
        self.assertTrue(all(isinstance(x, str) for x in found['runtimes']['python']['evidence']))

    def test_nested_node_ci_docker_and_workflow_tools(self):
        self.put('web/package.json', json.dumps({'engines': {'node': '>=24 <25'}, 'packageManager': 'pnpm@10.2.1'}))
        self.put('web/pnpm-lock.yaml', 'lockfileVersion: 9')
        self.put('web/Dockerfile', 'FROM node:24-alpine AS build\nRUN npm ci\nFROM node:24-slim AS runtime\nEXPOSE 3570\n')
        self.put('.github/workflows/ci.yml', 'steps:\n - uses: actions/setup-node@v4\n   with:\n     node-version: 24\n')
        self.put('Taskfile.yml', "cmds:\n - az containerapp show\n - docker run opa test\n - devtunnel host demo\n - lsof -ti tcp:8787\n - echo 'example http://localhost:18787'\n")
        self.put('.pre-commit-config.yaml', 'repos: []')
        found = scan(self.root)
        self.assertEqual(found['runtimes']['node']['version'], '24')
        self.assertEqual(found['runtimes']['node']['conflicts'], [])
        self.assertTrue(any('web/package.json' in x for x in found['runtimes']['node']['evidence']))
        self.assertTrue(any('build' in x for x in found['runtimes']['node']['evidence']))
        for name in ('azure', 'docker', 'devtunnel', 'precommit'):
            self.assertTrue(found['tools'][name]['selected'], name)
        self.assertFalse(found['tools']['aws']['selected'])
        self.assertEqual(found['ports'], [3570, 8787])
        self.assertEqual(found['package_managers']['node']['selected'], 'pnpm')

    def test_generic_ignores_ignored_scratch_and_discounted_trees(self):
        self.put('.gitignore', '.venv/\nnode_modules/\n.bundle/\nscratch/\n')
        self.put('scratch/package.json', '{"engines":{"node":"99"}}')
        self.put('vendor/Gemfile', "source 'https://rubygems.org'")
        self.put('examples/go.mod', 'module demo\ngo 1.24\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'vendor', 'examples'], check=True)
        found = scan(self.root)
        for name in ('node', 'ruby', 'go', 'rust'):
            self.assertFalse(found['runtimes'][name]['selected'], name)
        self.assertEqual(found['runtimes']['node']['version'], None)
        self.assertTrue(found['warnings'])

    def test_pins_and_manifest_constraints(self):
        self.put('backend/pyproject.toml', '[project]\nrequires-python = ">=3.12,<3.13"\n')
        self.put('backend/.python-version', '3.12.7\n')
        self.put('backend/uv.lock', 'version=1')
        self.put('.ruby-version', '3.4.2')
        self.put('Gemfile', "gem 'rails'\n")
        self.put('worker/go.mod', 'module example.com/worker\ngo 1.24.1\n')
        self.put('rust/Cargo.toml', '[package]\nname="sample"\nrust-version="1.85"\n')
        found = scan(self.root)
        for name, version in [('python', '3.12.7'), ('ruby', '3.4.2'), ('go', '1.24.1'), ('rust', '1.85')]:
            self.assertTrue(found['runtimes'][name]['selected'], name)
            self.assertEqual(found['runtimes'][name]['version'], version)
        self.assertEqual(found['package_managers']['python']['selected'], 'uv')

    def test_incompatible_versions_and_locks_are_explicit(self):
        self.put('web/.node-version', '20')
        self.put('other/package.json', '{"engines":{"node":">=24"}}')
        self.put('web/package-lock.json', '{}')
        self.put('web/yarn.lock', '# yarn')
        found = scan(self.root)
        self.assertTrue(found['runtimes']['node']['selected'])
        self.assertIsNone(found['runtimes']['node']['version'])
        self.assertTrue(found['runtimes']['node']['conflicts'])
        self.assertIsNone(found['package_managers']['node']['selected'])
        self.assertTrue(found['package_managers']['node']['conflicts'])

    def test_source_support_needs_corroboration_and_symlinks_are_not_read(self):
        self.put('one.rb', 'puts "hello"')
        outside = Path(self.temp.name).parent / (self.root.name + '-external.json')
        outside.write_text('{"engines":{"node":"99"}}')
        self.addCleanup(outside.unlink)
        (self.root / 'package.json').symlink_to(outside)
        found = scan(self.root)
        self.assertFalse(found['runtimes']['node']['selected'])
        self.assertFalse(found['runtimes']['ruby']['selected'])
        self.put('lib/two.rb', 'puts "hello"')
        self.assertTrue(scan(self.root)['runtimes']['ruby']['selected'])

    def test_malformed_manifest_is_visible_and_no_prose_cloud_selection(self):
        self.put('package.json', '{broken')
        self.put('README.md', 'We use Azure, AWS and Google Cloud; try devtunnel.')
        found = scan(self.root)
        self.assertTrue(found['warnings'])
        self.assertFalse(any(value['selected'] for value in found['tools'].values()))


if __name__ == '__main__':
    unittest.main()
