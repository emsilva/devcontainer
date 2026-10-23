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
        self.root = Path(self.tmp.name)
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
 https://claude.ai/install.sh) name=claude;;
 https://opencode.ai/install) name=opencode;;
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
            self.write(self.root / (name + '.sh'), f'''#!/bin/bash
set -eu
printf 'install {name} %s\\n' "$*" >> "$TEST_CALLS"
[ "${{FAIL_INSTALL:-}}" != {name} ] || exit 33
mkdir -p "$HOME/{directory}"
rm -f "$HOME/{directory}/{name}"
printf '#!/bin/sh\\necho {name}-test\\n' > "$HOME/{directory}/{name}"
chmod +x "$HOME/{directory}/{name}"
{native}
''')

    def write(self, path, content):
        path.write_text(content)
        path.chmod(0o755)

    def run_helper(self, *args, **env):
        return subprocess.run(['bash', str(HELPER), *args], env=dict(self.env, **env),
                              capture_output=True, text=True)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_install_idempotent_update_and_check(self):
        self.assert_ok(self.run_helper())
        calls = self.log.read_text()
        for url in ('https://chatgpt.com/codex/install.sh', 'https://claude.ai/install.sh', 'https://opencode.ai/install'):
            self.assertIn(url, calls)
        self.assertIn('--fail', calls)
        self.assertIn('install opencode --no-modify-path', calls)
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
