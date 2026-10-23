#!/usr/bin/env bash
# Shared authority for canonical and generated containers. Vendor releases move.
set -euo pipefail

mode="${1:-install}"
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$mode" != --update ] && [ "$mode" != --check ]; }; then
  echo 'Usage: install-agents.sh [--update|--check]' >&2
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
  echo 'Refusing to install or check agents as root; run as the remote user (vscode).' >&2
  exit 1
fi
export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"
# Keep vendor overrides from redirecting a managed installation outside this home.
export CODEX_HOME="$HOME/.codex" CODEX_INSTALL_DIR="$HOME/.local/bin"
unset CODEX_RELEASE CODEX_INSTALL_DAEMON_ONLY CODEX_INSTALL_DEFER_SELECTION VERSION

check_layout() {
  python3 - "$1" <<'PY'
import os
from pathlib import Path
import sys

name = sys.argv[1]
home = Path.home().resolve()
launcher = home / ('.opencode/bin/opencode' if name == 'opencode' else '.local/bin/' + name)
try:
    target = launcher.resolve(strict=True)
    target.relative_to(home)
    assert launcher.lstat().st_uid == os.getuid() and target.stat().st_uid == os.getuid()
    assert os.access(target, os.X_OK)
    for path in (launcher, target):
        for parent in path.parents:
            assert parent.stat().st_uid == os.getuid()
            if parent == home:
                break
    if name == 'claude':
        assert launcher.is_symlink()
        target.relative_to(home / '.local/share/claude/versions')
    if name == 'codex':
        package = (home / '.codex/packages/standalone/current').resolve(strict=True)
        package.relative_to(home / '.codex/packages/standalone')
        assert target.samefile(package / 'bin/codex')
        for member in ('codex-package.json', 'bin/codex', 'bin/codex-code-mode-host',
                       'codex', 'codex-path/rg') + (('codex-resources/bwrap',) if sys.platform == 'linux' else ()):
            path = package / member
            assert path.is_file()
            if member != 'codex-package.json':
                assert os.access(path, os.X_OK)
        # Every retained release and its launchers must remain user-updatable.
        root = home / '.codex/packages/standalone'
        for path in [root, *root.rglob('*')]:
            assert path.lstat().st_uid == os.getuid()
            if path.is_symlink():
                path.resolve(strict=True).relative_to(root.resolve())
except (OSError, ValueError, AssertionError):
    print(f'Incomplete or non-user-owned managed {name} installation: {launcher}', file=sys.stderr)
    sys.exit(1)
PY
}

check_agent() {
  local name="$1" launcher="$HOME/.local/bin/$1"
  [ "$name" != opencode ] || launcher="$HOME/.opencode/bin/opencode"
  check_layout "$name" || return 1
  "$launcher" --version || return 1
  if [ "$name" = codex ]; then
    # Read-only package probe: no daemon, authentication, or model request.
    "$launcher" app-server daemon pid-update-loop --check-package-ownership || return 1
  fi
}

if [ "$mode" = --check ]; then
  for agent in codex claude opencode; do check_agent "$agent"; done
  exit 0
fi

umask 077
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
for agent in codex claude opencode; do
  if [ "$mode" != --update ] && check_agent "$agent" >/dev/null 2>&1; then
    printf 'Managed %s is complete; keeping installed release.\n' "$agent"
    continue
  fi
  case "$agent" in
    # The documented Codex URL has returned 404 for hours (openai/codex#56254);
    # the same script is published beside the releases and on GitHub Releases.
    codex) urls='https://chatgpt.com/codex/install.sh https://releases.openai.com/codex/install.sh https://github.com/openai/codex/releases/latest/download/install.sh'; shell='sh' ;;
    claude) urls=https://claude.ai/install.sh; shell='bash' ;;
    opencode) urls=https://opencode.ai/install; shell='bash' ;;
  esac
  installer="$temporary/$agent.sh"
  for url in $urls; do
    if curl --fail --location --silent --show-error "$url" -o "$installer" && [ -s "$installer" ]; then
      break
    fi
    # Never run what a failed download left behind.
    rm -f "$installer"
  done
  [ -s "$installer" ] || { echo "Could not download the $agent installer from $urls" >&2; exit 1; }
  "$shell" -n "$installer"
  case "$agent" in
    # Hide old global/npm Codex commands from vendor conflict detection so it
    # cannot add a PATH block to personal profiles during migration.
    codex) PATH="$HOME/.local/bin:$HOME/.opencode/bin:/usr/bin:/bin" CODEX_NON_INTERACTIVE=1 sh "$installer" ;;
    claude) bash "$installer" latest ;;
    opencode) bash "$installer" --no-modify-path ;;
  esac
  check_agent "$agent"
done
