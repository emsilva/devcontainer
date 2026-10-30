"""Generated tools use local installers and homes only; no package downloads."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ToolInstallTests(unittest.TestCase):
    def setUp(self):
        from devcontainer_setup.render import render
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        files = render(self.project, ROOT, {}, 'a' * 40)
        for name, data in files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.helper = self.project / '.devcontainer/scripts/install-tools.sh'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'calls'
        self.env = dict(os.environ, HOME=str(self.home),
                        PATH=f'{self.home}/.local/bin:{self.bin}:' + os.environ['PATH'],
                        TEST_CALLS=str(self.log))
        self.write(self.bin / 'id', '#!/bin/sh\necho "${TEST_UID:-1000}"\n')
        self.write(self.bin / 'node', '#!/bin/sh\necho "${TEST_NODE_VERSION:-v24.0.0}"\n')
        self.write(self.bin / 'uv', '''#!/bin/bash
set -eu
printf 'uv %s\\n' "$*" >> "$TEST_CALLS"
[ "$UV_TOOL_BIN_DIR" = "$HOME/.local/bin" ]
mkdir -p "$UV_TOOL_BIN_DIR" "$HOME/.local/share/ccswap"
printf '#!/bin/sh\\necho ccswap v0.42.0\\n' > "$HOME/.local/share/ccswap/ccswap"
chmod +x "$HOME/.local/share/ccswap/ccswap"
ln -sfn "$HOME/.local/share/ccswap/ccswap" "$UV_TOOL_BIN_DIR/ccswap"
[ "${FAIL_UV:-}" != 1 ] || exit 23
''')
        self.write(self.bin / 'npm', '''#!/bin/bash
set -eu
printf 'npm %s\\n' "$*" >> "$TEST_CALLS"
[ "$1" = ci ] && [ "$2" = --ignore-scripts ] && [ "$3" = --prefix ]
stage="$4"
[ -f "$stage/package.json" ] && [ -f "$stage/package-lock.json" ]
package="$stage/node_modules/@plaud-ai/cli"
mkdir -p "$package/dist"
echo '{"version":"0.3.15","bin":{"plaud":"dist/index.js"}}' > "$package/package.json"
cat > "$package/dist/index.js" <<'SH'
#!/bin/sh
[ "$PLAUD_TELEMETRY_DISABLED" = 1 ] && [ "$PLAUD_NO_UPDATE_NOTIFIER" = 1 ] || exit 91
[ "$1" = --help ] || exit 92
[ "${FAIL_PLAUD_HELP:-}" != 1 ] || exit 44
echo 'Plaud usage'
SH
chmod +x "$package/dist/index.js"
[ "${FAIL_NPM:-}" != 1 ] || exit 22
''')

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)

    def run_helper(self, *args, **env):
        self.assertTrue(self.helper.is_file(), 'Missing generated install-tools.sh')
        return subprocess.run(['bash', str(self.helper), *args], cwd=self.project,
                              env=dict(self.env, **env), capture_output=True, text=True)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ''

    def test_installs_pinned_ccswap_and_plaud(self):
        self.assert_ok(self.run_helper())
        calls = self.calls()
        self.assertIn('uv tool install --force --constraints ', calls)
        self.assertIn('/.devcontainer/tools/ccswap-constraints.txt ccswap==0.42.0', calls)
        self.assertIn('npm ci --ignore-scripts --prefix ', calls)
        for name in ('ccswap', 'plaud'):
            self.assertTrue((self.home / '.local/bin' / name).is_symlink())
        self.assertEqual((self.home / '.local/bin/plaud').resolve(),
                         self.home / '.local/share/plaud-cli/node_modules/@plaud-ai/cli/dist/index.js')
        self.assert_ok(self.run_helper('--check'))
        self.assertFalse(any(self.home.glob('.*rc')))

    def test_rerun_keeps_both(self):
        self.assert_ok(self.run_helper())
        calls = self.calls()
        self.assert_ok(self.run_helper())
        self.assert_ok(self.run_helper('--check'))
        self.assertEqual(self.calls(), calls)

    def test_update_reinstalls_both(self):
        self.assert_ok(self.run_helper())
        self.assert_ok(self.run_helper('--update'))
        self.assertEqual(self.calls().count('uv tool install'), 2)
        self.assertEqual(self.calls().count('npm ci'), 2)

    def test_other_versions_are_replaced(self):
        self.assert_ok(self.run_helper())
        self.write(self.home / '.local/share/ccswap/ccswap', '#!/bin/sh\necho 0.37.0\n')
        package = self.home / '.local/share/plaud-cli/node_modules/@plaud-ai/cli/package.json'
        metadata = json.loads(package.read_text())
        metadata['version'] = '0.3.14'
        package.write_text(json.dumps(metadata))
        self.assertNotEqual(self.run_helper('--check').returncode, 0)
        self.assert_ok(self.run_helper())
        self.assertEqual(self.calls().count('uv tool install'), 2)
        self.assertEqual(self.calls().count('npm ci'), 2)
        self.assert_ok(self.run_helper('--check'))

    def test_global_plaud_does_not_satisfy_managed_install(self):
        self.write(self.bin / 'plaud', '#!/bin/sh\nexit 0\n')
        self.assertNotEqual(self.run_helper('--check').returncode, 0)
        self.assert_ok(self.run_helper())
        self.assertTrue((self.home / '.local/bin/plaud').is_symlink())
        self.assertIn('npm ci', self.calls())

    def test_failed_npm_leaves_no_link(self):
        self.assert_ok(self.run_helper())
        result = self.run_helper('--update', FAIL_NPM='1')
        self.assertEqual(result.returncode, 22, result.stderr)
        self.assertFalse(os.path.lexists(self.home / '.local/bin/plaud'))
        self.assertFalse(list((self.home / '.local/share').glob('.plaud-cli.*')))
        self.assert_ok(self.run_helper())
        self.assert_ok(self.run_helper('--check'))
        self.assertEqual(self.calls().count('npm ci'), 3)

    def test_failed_uv_leaves_no_link(self):
        result = self.run_helper(FAIL_UV='1')
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertFalse(os.path.lexists(self.home / '.local/bin/ccswap'))
        self.assertNotIn('npm ci', self.calls())
        self.assert_ok(self.run_helper())
        self.assert_ok(self.run_helper('--check'))

    def test_old_node_skips_plaud(self):
        result = self.run_helper(TEST_NODE_VERSION='v16.20.2')
        self.assert_ok(result)
        self.assertEqual(result.stdout.count('Plaud needs Node ^18.17.0 or >=20.5.0; this container has Node v16.20.2'), 1)
        self.assertNotIn('npm ci', self.calls())
        self.assertFalse(os.path.lexists(self.home / '.local/bin/plaud'))
        self.assert_ok(self.run_helper('--check', TEST_NODE_VERSION='v16.20.2'))

    def test_installer_matches_locked_node_range(self):
        for version, supported in [('v18.16.0', False), ('v18.17.0', True),
                                   ('v20.4.0', False), ('v24.0.0', True)]:
            with self.subTest(version=version):
                before = self.calls()
                result = self.run_helper('--update', TEST_NODE_VERSION=version)
                self.assert_ok(result)
                self.assertEqual('npm ci' in self.calls()[len(before):], supported)
                message = 'Plaud needs Node ^18.17.0 or >=20.5.0; this container has Node ' + version
                self.assertEqual(result.stdout.count(message), 0 if supported else 1)
                calls = self.calls()
                self.assert_ok(self.run_helper('--check', TEST_NODE_VERSION=version))
                self.assertEqual(self.calls(), calls)

    def test_check_requires_tree_link_version_and_help(self):
        self.assert_ok(self.run_helper())
        launcher = self.home / '.local/bin/plaud'
        launcher.unlink()
        self.write(self.bin / 'global-plaud', '#!/bin/sh\nexit 0\n')
        launcher.symlink_to(self.bin / 'global-plaud')
        self.assertNotEqual(self.run_helper('--check').returncode, 0)
        self.assert_ok(self.run_helper())
        self.assertNotEqual(self.run_helper('--check', FAIL_PLAUD_HELP='1').returncode, 0)
        self.assert_ok(self.run_helper('--check'))

    def test_refuses_root_and_unknown_arguments(self):
        result = self.run_helper(TEST_UID='0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('root', result.stderr)
        for args in [('--unknown',), ('--check', '--update')]:
            with self.subTest(args=args):
                self.assertNotEqual(self.run_helper(*args).returncode, 0)
        self.assertFalse(self.log.exists())
