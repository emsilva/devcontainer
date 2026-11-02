"""Exercise the shared helper with local vendor fixtures; never download agents."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / '.devcontainer/scripts/install-agents.sh'
CODEX_INSTALLERS = ('https://chatgpt.com/codex/install.sh', 'https://releases.openai.com/codex/install.sh',
                    'https://github.com/openai/codex/releases/latest/download/install.sh')


class AgentInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'calls'
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + ':' + os.environ['PATH'],
                        TEST_CALLS=str(self.log), TEST_FIXTURES=str(self.root))
        self.write(self.bin / 'curl', '''#!/bin/bash
set -eu
printf '%s\\n' "$*" >> "$TEST_CALLS"
url='' out=''
while [ "$#" -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2;; https:*) url="$1"; shift;; *) shift;; esac
done
case " ${FAIL_DOWNLOAD:-} " in *" $url "*) echo 'exit 0' > "$out"; exit 22;; esac
case "$url" in
 https://chatgpt.com/codex/install.sh|https://releases.openai.com/codex/install.sh) name=codex;;
 https://github.com/openai/codex/releases/latest/download/install.sh) name=codex;;
 https://x.ai/cli/install.sh) name=grok;;
 https://claude.ai/install.sh) name=claude;;
 https://opencode.ai/install|https://opencode.ai/v2/install) name=opencode;;
 *) exit 90;;
esac
cp "$TEST_FIXTURES/$name.sh" "$out"
''')
        self.write(self.root / 'codex.sh', '''#!/bin/sh
set -eu
[ "$CODEX_NON_INTERACTIVE" = 1 ]
[ "$PATH" = "$HOME/.local/bin:$HOME/.opencode/bin:/usr/bin:/bin" ]
[ "$CODEX_HOME" = "$HOME/.codex" ]
[ "$CODEX_INSTALL_DIR" = "$HOME/.local/bin" ]
printf 'install codex %s\\n' "$*" >> "$TEST_CALLS"
[ "${FAIL_INSTALL:-}" != codex ] || exit 33
r="$HOME/.codex/packages/standalone/releases/test"
mkdir -p "$r/bin" "$r/codex-path" "$r/codex-resources" "$HOME/.local/bin"
echo '{}' > "$r/codex-package.json"
for x in bin/codex bin/codex-code-mode-host codex-path/rg codex-resources/bwrap; do
  printf '#!/bin/sh\\n[ "${FAIL_CHECK:-}" != codex ] || exit 44\\necho codex-test\\n' > "$r/$x"
  chmod +x "$r/$x"
done
ln -sf bin/codex "$r/codex"
ln -sfn "$r" "$HOME/.codex/packages/standalone/current"
ln -sf "$r/bin/codex" "$HOME/.local/bin/codex"
''')
        for name, directory in [('claude', '.local/bin'), ('opencode', '.opencode/bin')]:
            native = ('mkdir -p "$HOME/.local/share/claude/versions"\nmv "$HOME/.local/bin/claude" "$HOME/.local/share/claude/versions/test"\nln -sf "$HOME/.local/share/claude/versions/test" "$HOME/.local/bin/claude"' if name == 'claude' else '')
            version = '${TEST_OPENCODE_VERSION:-2.1.4}' if name == 'opencode' else 'claude-test'
            self.write(self.root / (name + '.sh'), f'''#!/bin/bash
set -eu
printf 'install {name} %s\\n' "$*" >> "$TEST_CALLS"
[ "${{FAIL_INSTALL:-}}" != {name} ] || exit 33
mkdir -p "$HOME/{directory}"
rm -f "$HOME/{directory}/{name}"
printf '#!/bin/sh\\necho {version}\\n' > "$HOME/{directory}/{name}"
chmod +x "$HOME/{directory}/{name}"
{native}
''')

        self.write(self.root / 'grok.sh', '''#!/bin/bash
set -eu
executing_shell=${BASH:-}
[ "${executing_shell##*/}" = bash ]
printf 'install grok %s\\n' "$*" >> "$TEST_CALLS"
[ "${FAIL_INSTALL:-}" != grok ] || exit 33
[ "${GROK_BIN_DIR:-$HOME/.grok/bin}" = "$HOME/.grok/bin" ]
[ "${GROK_CHANNEL:-stable}" = stable ]
[ "${GROK_DEPLOYMENT_KEY+x}" != x ]
[ "${GROK_PROXY_URL+x}" != x ]
case "${SHELL##*/}" in bash|zsh|fish) echo rewritten >> "$HOME/.bashrc";; esac
mkdir -p "$HOME/.grok/downloads" "$HOME/.grok/bin"
printf '#!/bin/sh\\n[ "${FAIL_CHECK:-}" != grok ] || exit 44\\necho "${TEST_GROK_VERSION:-grok 1.0.70 (test) [stable]}"\\n' > "$HOME/.grok/downloads/grok-linux-x86_64"
chmod +x "$HOME/.grok/downloads/grok-linux-x86_64"
ln -sfn ../downloads/grok-linux-x86_64 "$HOME/.grok/bin/grok"
''')

    def write(self, path, content):
        path.write_text(content)
        path.chmod(0o755)

    def run_helper(self, *args, **env):
        return subprocess.run(['bash', str(HELPER), *args], env=dict(self.env, **env),
                              capture_output=True, text=True)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_grok_install_idempotence_update_and_check(self):
        self.assert_ok(self.run_helper())
        calls = self.log.read_text()
        self.assertIn('https://x.ai/cli/install.sh', calls)
        self.assertIn('install grok ', calls)
        launcher = self.home / '.local/bin/grok'
        self.assertEqual(Path(os.readlink(launcher)), self.home / '.grok/bin/grok')
        self.assertEqual(launcher.resolve(), self.home / '.grok/downloads/grok-linux-x86_64')
        self.assert_ok(self.run_helper())
        check = self.run_helper('--check')
        self.assert_ok(check)
        self.assertIn('grok 1.0.70', check.stdout)
        self.assertEqual(calls, self.log.read_text())
        self.assert_ok(self.run_helper('--update'))
        self.assertEqual(self.log.read_text().count('install grok '), 2)

    def test_grok_rejects_wrong_chain_and_external_downloads(self):
        self.assert_ok(self.run_helper())
        launcher = self.home / '.local/bin/grok'
        native = self.home / '.grok/bin/grok'
        binary = self.home / '.grok/downloads/grok-linux-x86_64'
        for target in (binary, self.bin / 'external-grok'):
            with self.subTest(target=target):
                self.write(self.bin / 'external-grok', '#!/bin/sh\necho grok external\n')
                launcher.unlink(missing_ok=True)
                launcher.symlink_to(target)
                self.assertNotEqual(self.run_helper('--check').returncode, 0)
        launcher.unlink()
        launcher.symlink_to(native)
        native.parent.mkdir(parents=True, exist_ok=True)
        native.unlink(missing_ok=True)
        native.symlink_to(self.bin / 'external-grok')
        self.assertNotEqual(self.run_helper('--check').returncode, 0)

    def test_grok_requires_version_prefix(self):
        self.assertNotEqual(self.run_helper(TEST_GROK_VERSION='1.0.70').returncode, 0)

    def test_grok_neutralizes_vendor_environment_and_preserves_profiles(self):
        profile = self.home / '.bashrc'
        profile.write_text('# personal profile\n')
        self.assert_ok(self.run_helper(SHELL='/bin/bash', GROK_BIN_DIR=str(self.root / 'redirected'),
                                       GROK_CHANNEL='enterprise', GROK_DEPLOYMENT_KEY='fixture-only',
                                       GROK_PROXY_URL='https://fixture.invalid'))
        self.assertIn('install grok ', self.log.read_text())
        self.assertEqual(profile.read_text(), '# personal profile\n')
        self.assertFalse((self.root / 'redirected').exists())

    def test_grok_download_failure_propagates(self):
        result = self.run_helper(FAIL_DOWNLOAD='https://x.ai/cli/install.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not download the grok installer', result.stderr)
        self.assertNotIn('install grok ', self.log.read_text())

    def test_grok_syntax_failure_prevents_execution(self):
        self.write(self.root / 'grok.sh', 'echo executed >> "$TEST_CALLS"\nif then\n')
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('executed', self.log.read_text())

    def test_grok_install_failure_propagates(self):
        self.assertEqual(self.run_helper(FAIL_INSTALL='grok').returncode, 33)

    def test_grok_version_failure_propagates(self):
        self.assertNotEqual(self.run_helper(FAIL_CHECK='grok').returncode, 0)

    def test_opencode_v2_installer_is_pinned(self):
        self.assert_ok(self.run_helper())
        calls = self.log.read_text()
        self.assertIn('https://opencode.ai/v2/install', calls)
        self.assertIn('install opencode --no-modify-path --version 2.1.4', calls)
        self.assertNotIn('https://opencode.ai/install', calls)

    def test_opencode_is_linked_into_local_bin(self):
        self.assert_ok(self.run_helper())
        launcher = self.home / '.local/bin/opencode'
        self.assertTrue(launcher.is_symlink())
        self.assertEqual(launcher.resolve(), self.home / '.opencode/bin/opencode')

    def test_opencode_1x_is_replaced(self):
        self.assert_ok(self.run_helper())
        self.write(self.home / '.opencode/bin/opencode', '#!/bin/sh\necho 1.19.4\n')
        before = self.log.read_text()
        self.assert_ok(self.run_helper())
        self.assertEqual(self.log.read_text()[len(before):].count('install opencode'), 1)
        self.assert_ok(self.run_helper('--check'))
        self.assertFalse(any(self.home.glob('.*rc')))

    def test_check_fails_for_opencode_1x(self):
        self.assert_ok(self.run_helper())
        self.write(self.home / '.opencode/bin/opencode', '#!/bin/sh\necho 1.19.4\n')
        self.assertNotEqual(self.run_helper('--check').returncode, 0)

    def test_install_idempotent_update_and_check(self):
        self.assert_ok(self.run_helper())
        calls = self.log.read_text()
        for url in ('https://chatgpt.com/codex/install.sh', 'https://claude.ai/install.sh', 'https://opencode.ai/v2/install'):
            self.assertIn(url, calls)
        self.assertIn('--fail', calls)
        self.assertIn('install opencode --no-modify-path --version 2.1.4', calls)
        self.assertIn('install claude latest', calls)
        self.assert_ok(self.run_helper())
        self.assert_ok(self.run_helper('--check'))
        self.assertEqual(calls, self.log.read_text())
        self.assert_ok(self.run_helper('--update'))
        self.assertEqual(self.log.read_text().count('install codex'), 2)
        self.assertFalse(any(self.home.glob('.*rc')))
        for path in self.home.rglob('*'):
            self.assertEqual(path.stat().st_uid, os.getuid())

    def test_rejects_system_only_and_incomplete_user_codex(self):
        self.write(self.bin / 'codex', '#!/bin/sh\necho codex-bare\n')
        self.assertNotEqual(self.run_helper('--check').returncode, 0)
        self.assertFalse(self.log.exists())
        self.assert_ok(self.run_helper())
        package = self.home / '.codex/packages/standalone/current'
        for member in ('codex-package.json', 'bin/codex-code-mode-host', 'codex-path/rg', 'codex-resources/bwrap'):
            with self.subTest(member=member):
                if member == 'codex-resources/bwrap' and sys.platform != 'linux':
                    continue
                path = package / member
                before = path.read_bytes()
                path.unlink()
                self.assertNotEqual(self.run_helper('--check').returncode, 0)
                self.write(path, before.decode())
        self.assertNotEqual(self.run_helper('--check', FAIL_CHECK='codex').returncode, 0)
        (self.home / '.local/bin/codex').unlink()
        (self.home / '.local/bin/codex').symlink_to(self.bin / 'codex')
        self.assertNotEqual(self.run_helper('--check').returncode, 0)

    def test_claude_requires_native_versions_launcher(self):
        self.assert_ok(self.run_helper())
        launcher = self.home / '.local/bin/claude'
        launcher.unlink()
        self.write(launcher, '#!/bin/sh\necho claude-raw\n')
        self.assertNotEqual(self.run_helper('--check').returncode, 0)

    def test_conflicting_global_codex_does_not_rewrite_profiles(self):
        self.write(self.bin / 'codex', '#!/bin/sh\necho bare-system\n')
        # A vendor installer detecting a global npm Codex adds a profile block.
        installer = self.root / 'codex.sh'
        installer.write_text(installer.read_text().replace('set -eu', 'set -eu\nexisting=$(command -v codex || true)\n[ -z "$existing" ] || [ "$existing" = "$HOME/.local/bin/codex" ] || echo rewritten >> "$HOME/.bashrc"', 1))
        profile = self.home / '.bashrc'
        profile.write_text('# user profile\n')
        self.assert_ok(self.run_helper())
        self.assertEqual(profile.read_text(), '# user profile\n')

    def test_download_install_and_validation_failures_propagate(self):
        for env in ({'FAIL_DOWNLOAD': ' '.join(CODEX_INSTALLERS)}, {'FAIL_DOWNLOAD': 'https://claude.ai/install.sh'},
                    {'FAIL_INSTALL': 'codex'}, {'FAIL_INSTALL': 'claude'},
                    {'FAIL_INSTALL': 'opencode'}, {'FAIL_CHECK': 'codex'}):
            with self.subTest(env=env):
                self.assertNotEqual(self.run_helper('--update', **env).returncode, 0)

    def test_codex_falls_back_to_the_release_hosted_installers(self):
        # The documented URL has returned 404 for hours (openai/codex#56254); the same script is published twice more.
        self.assert_ok(self.run_helper())
        self.assertNotIn(CODEX_INSTALLERS[1], self.log.read_text())
        for failing in range(1, len(CODEX_INSTALLERS)):
            with self.subTest(failing=failing):
                self.log.unlink()
                self.assert_ok(self.run_helper('--update', FAIL_DOWNLOAD=' '.join(CODEX_INSTALLERS[:failing])))
                calls = self.log.read_text()
                for url in CODEX_INSTALLERS[:failing + 1]:
                    self.assertIn(url, calls)
                self.assertEqual(calls.count('install codex'), 1)
        result = self.run_helper('--update', FAIL_DOWNLOAD=' '.join(CODEX_INSTALLERS))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not download the codex installer', result.stderr)
        self.assertNotIn('install codex', self.log.read_text().split(CODEX_INSTALLERS[-1])[-1])

    def test_refuses_root_and_unknown_arguments_before_network(self):
        self.write(self.bin / 'id', '#!/bin/sh\necho 0\n')
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('root', result.stderr)
        self.assertFalse(self.log.exists())
        self.assertNotEqual(self.run_helper('--unknown').returncode, 0)
