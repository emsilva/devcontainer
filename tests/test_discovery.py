import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from devcontainer_setup.discovery import scan


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

    def test_explicit_manager_pin_and_engines_version_are_retained(self):
        self.put('package.json', json.dumps({'packageManager': 'pnpm@9.15.4',
                  'engines': {'node': '24', 'pnpm': '9.15.4'}}))
        manager = scan(self.root)['package_managers']['node']
        self.assertEqual(manager['selected'], 'pnpm')
        self.assertEqual(manager['version'], '9.15.4')
        self.assertEqual(manager['versions']['pnpm']['conflicts'], [])
        self.assertTrue(any('9.15.4' in item for item in manager['evidence']))

    def test_nested_incompatible_manager_versions_require_visible_resolution(self):
        self.put('web/package.json', '{"packageManager":"pnpm@9.15.4"}')
        self.put('other/package.json', '{"packageManager":"pnpm@10.2.1"}')
        manager = scan(self.root)['package_managers']['node']
        self.assertIsNone(manager['version'])
        self.assertEqual(len(manager['versions']['pnpm']['conflicts']), 2)

    def test_engine_only_manager_declarations_are_not_discarded(self):
        self.put('package.json', '{"engines":{"pnpm":"9.15.4"}}')
        manager = scan(self.root)['package_managers']['node']
        self.assertEqual(manager['selected'], 'pnpm')
        self.assertEqual(manager['version'], '9.15.4')

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
        self.assertFalse(found['runtimes']['ruby']['selected'])
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

    def test_compose_source_files_and_docs_do_not_recommend_tools(self):
        self.put('server/compose.ts', 'export const compose = () => 1')
        self.put('docs/example.sh', 'az login\n')
        found = scan(self.root)
        self.assertFalse(found['tools']['docker']['selected'])
        self.assertFalse(found['tools']['azure']['selected'])

    def test_workflow_commands_and_ci_setup_without_version(self):
        self.put('Taskfile.yml', 'cmds:\n - go test ./...\n - ruby app.rb\n - cargo build\n - python3 -m pytest\n - npm run build\n')
        found = scan(self.root)
        for name in ('go', 'ruby', 'rust', 'node'):
            self.assertTrue(found['runtimes'][name]['selected'], name)
        self.assertEqual(found['package_managers']['node']['selected'], 'npm')
        self.assertEqual(found['runtimes']['python']['version'], '3.13')

    def test_unsupported_version_expression_requires_review(self):
        self.put('package.json', '{"engines":{"node":">=20 bananas"}}')
        found = scan(self.root)
        self.assertIsNone(found['runtimes']['node']['version'])
        self.assertTrue(found['runtimes']['node']['conflicts'])

    def test_missing_pyproject_constraint_keeps_python_baseline(self):
        self.put('pyproject.toml', '[project]\nname="hello"\nversion="99.0"\n')
        self.assertEqual(scan(self.root)['runtimes']['python']['version'], '3.13')

    def test_two_component_tilde_does_not_accept_next_minor(self):
        self.put('package.json', '{"engines":{"node":"~20.1"}}')
        self.put('.node-version', '20.9')
        found = scan(self.root)['runtimes']['node']
        self.assertIsNone(found['version'])
        self.assertTrue(found['conflicts'])

    def test_caret_zero_major_and_tilde_boundaries(self):
        for declaration, pin, compatible in [('^0.2', '0.3.0', False), ('^0.2', '0.2.9', True), ('^0.0.2', '0.0.3', False), ('~20.1', '20.1.5', True), ('~20', '20.9', True)]:
            with self.subTest(declaration=declaration, pin=pin):
                self.put('package.json', json.dumps({'engines': {'node': declaration}}))
                self.put('.node-version', pin)
                found = scan(self.root)['runtimes']['node']
                self.assertEqual(bool(found['conflicts']), not compatible)

    def test_large_npm_lock_retains_conflict_with_other_lock(self):
        self.put('package-lock.json', 'x' * (512 * 1024 + 1))
        self.put('pnpm-lock.yaml', 'lockfileVersion: 9')
        found = scan(self.root)['package_managers']['node']
        self.assertIsNone(found['selected'])
        self.assertTrue(found['conflicts'])

    def test_pipfile_full_version_conflicts_with_incompatible_pin(self):
        self.put('Pipfile', '[requires]\npython_full_version = "3.12.1"\n')
        self.put('.python-version', '3.12.9')
        self.assertTrue(scan(self.root)['runtimes']['python']['conflicts'])

    def test_go_mod_rejects_older_ci_toolchain(self):
        self.put('go.mod', 'module example.com/demo\ngo 1.24\n')
        self.put('.github/workflows/test.yml', 'steps:\n - uses: actions/setup-go@v5\n   with:\n     go-version: 1.22\n')
        self.assertTrue(scan(self.root)['runtimes']['go']['conflicts'])

    def test_binary_and_large_lockfiles_still_supply_filename_evidence(self):
        self.put('web/bun.lockb', '').write_bytes(b'\xff\x00bun')
        self.put('api/uv.lock', 'x' * (512 * 1024 + 1))
        found = scan(self.root)
        self.assertTrue(found['runtimes']['node']['selected'])
        self.assertEqual(found['package_managers']['node']['selected'], 'bun')
        self.assertEqual(found['package_managers']['python']['selected'], 'uv')

    def test_pipfile_requires_controls_python_version(self):
        self.put('Pipfile', '[requires]\npython_version = "3.12"\n')
        found = scan(self.root)
        self.assertEqual(found['runtimes']['python']['version'], '3.12')
        self.assertEqual(found['package_managers']['python']['selected'], 'pipenv')

    def test_echoed_tool_and_runtime_examples_are_not_workflows(self):
        self.put('Taskfile.yml', "cmds:\n - echo 'az login; aws configure; gcloud auth login; docker run demo; devtunnel host x; kubectl get pods; pre-commit run; npm run dev --port 3000; cargo build'\n")
        found = scan(self.root)
        self.assertFalse(any(row['selected'] for row in found['tools'].values()))
        self.assertFalse(found['runtimes']['node']['selected'])
        self.assertFalse(found['runtimes']['rust']['selected'])
        self.assertEqual(found['ports'], [])

    def test_valid_open_range_finds_interior_version(self):
        self.put('package.json', '{"engines":{"node":">20 <24"}}')
        found = scan(self.root)['runtimes']['node']
        self.assertIsNotNone(found['version'])
        self.assertEqual(found['conflicts'], [])
        self.assertGreater(int(found['version'].split('.')[0]), 20)
        self.assertLess(int(found['version'].split('.')[0]), 24)

    def test_go_mod_minimum_accepts_newer_ci_toolchain(self):
        self.put('go.mod', 'module example.com/demo\ngo 1.22\n')
        self.put('.github/workflows/test.yml', 'steps:\n - uses: actions/setup-go@v5\n   with:\n     go-version: 1.24\n')
        found = scan(self.root)['runtimes']['go']
        self.assertEqual(found['version'], '1.24')
        self.assertEqual(found['conflicts'], [])

    def test_yaml_quoted_echo_scalar_does_not_select_cloud_tool(self):
        for scalar in ["'echo \"Use az login for deployment\"'", '"echo \'Use az login for deployment\'"', "'echo \"Use az login\" && docker run demo'"]:
            with self.subTest(scalar=scalar):
                self.put('Taskfile.yml', 'tasks:\n  help:\n    cmds:\n      - ' + scalar + '\n')
                tools = scan(self.root)['tools']
                self.assertFalse(tools['azure']['selected'])
                self.assertEqual(tools['docker']['selected'], '&& docker' in scalar)

    def test_python_strict_minor_bounds_choose_compatible_patch(self):
        for constraint in ('>3.12,<3.13', '>3.12.0,<3.12.2'):
            with self.subTest(constraint=constraint):
                self.put('pyproject.toml', '[project]\nrequires-python = "' + constraint + '"\n')
                found = scan(self.root)['runtimes']['python']
                self.assertEqual(found['conflicts'], [])
                self.assertEqual(found['version'], '3.12.1')


if __name__ == '__main__':
    unittest.main()
