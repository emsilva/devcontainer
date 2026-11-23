#!/usr/bin/env bash
# Shared authority for canonical and generated containers. Vendor releases move.
set -euo pipefail

OPENCODE_VERSION=2.1.4

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
launcher = home / '.local/bin' / name
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
    if name == 'grok':
        native = home / '.grok/bin/grok'
        assert launcher.is_symlink() and native.is_symlink()
        assert native.lstat().st_uid == os.getuid()
        assert Path(os.path.abspath(launcher.parent / os.readlink(launcher))) == native
        target.relative_to(home / '.grok/downloads')
        assert target.samefile(native)
        for parent in native.parents:
            assert parent.stat().st_uid == os.getuid()
            if parent == home:
                break
    if name == 'opencode':
        assert launcher.is_symlink()
        assert target.samefile(home / '.opencode/bin/opencode')
    if name == 'agy':
        assert launcher.is_file() and not launcher.is_symlink()
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
  local name="$1" launcher="$HOME/.local/bin/$1" version
  check_layout "$name" || return 1
  if [ "$name" = agy ]; then
    version=$(AGY_CLI_DISABLE_AUTO_UPDATE=true "$launcher" --version) || return 1
  else
    version=$("$launcher" --version) || return 1
  fi
  printf '%s\n' "$version"
  if [ "$name" = agy ]; then
    case "$version" in
      [0-9]*.[0-9]*) ;;
      *) echo 'Managed Antigravity CLI must report a numeric version.' >&2; return 1 ;;
    esac
  fi
  if [ "$name" = opencode ]; then
    version=$(printf '%s\n' "$version" | awk 'END {print $NF}')
    case "${version#v}" in
      2.*) ;;
      *) echo 'Managed OpenCode must report major version 2.' >&2; return 1 ;;
    esac
  fi
  if [ "$name" = grok ]; then
    case "$version" in
      'grok '*) ;;
      *) echo 'Managed Grok must report a version starting with grok .' >&2; return 1 ;;
    esac
  fi
  if [ "$name" = codex ]; then
    # Read-only package probe: no daemon, authentication, or model request.
    "$launcher" app-server daemon pid-update-loop --check-package-ownership || return 1
  fi
}

if [ "$mode" = --check ]; then
  for agent in codex claude opencode grok agy; do check_agent "$agent"; done
  exit 0
fi

umask 077
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
for agent in codex claude opencode grok agy; do
  if [ "$mode" != --update ] && check_agent "$agent" >/dev/null 2>&1; then
    printf 'Managed %s is complete; keeping installed release.\n' "$agent"
    continue
  fi
  case "$agent" in
    # The documented Codex URL has returned 404 for hours (openai/codex#56254);
    # the same script is published beside the releases and on GitHub Releases.
    codex) urls='https://chatgpt.com/codex/install.sh https://releases.openai.com/codex/install.sh https://github.com/openai/codex/releases/latest/download/install.sh'; shell='sh' ;;
    claude) urls=https://claude.ai/install.sh; shell='bash' ;;
    grok) urls=https://x.ai/cli/install.sh; shell='bash' ;;
    opencode) urls=https://opencode.ai/v2/install; shell='bash' ;;
    agy) urls=https://antigravity.google/cli/install.sh; shell='bash' ;;
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
    grok)
      # The vendor selects profiles by SHELL basename; an unknown shell avoids edits.
      # Deployment settings must never turn this installation into a deployment.
      (
        unset GROK_DEPLOYMENT_KEY GROK_PROXY_URL
        GROK_BIN_DIR="$HOME/.grok/bin" GROK_CHANNEL=stable SHELL=/bin/false bash "$installer"
      )
      ln -sfn "$HOME/.grok/bin/grok" "$HOME/.local/bin/grok"
      ;;
    opencode)
      bash "$installer" --no-modify-path --version "$OPENCODE_VERSION"
      ln -sfn "$HOME/.opencode/bin/opencode" "$HOME/.local/bin/opencode"
      ;;
    agy)
      # Run install.sh against a fresh -d staging directory and throwaway HOME so:
      # 1) --update still refreshes an existing ~/.local/bin/agy (install.sh exits early if target exists);
      # 2) agy install (which install.sh calls without --skip-path) cannot edit personal shell profiles.
      agy_home="$temporary/agy-home"
      agy_bin="$temporary/agy-bin"
      rm -rf "$agy_home" "$agy_bin"
      mkdir -p "$agy_home" "$agy_bin" "$HOME/.local/bin"
      HOME="$agy_home" AGY_CLI_DISABLE_AUTO_UPDATE=true bash "$installer" -d "$agy_bin"
      [ -x "$agy_bin/agy" ] || { echo "Managed Antigravity CLI installer did not produce staged $agy_bin/agy." >&2; exit 1; }
      cp "$agy_bin/agy" "$HOME/.local/bin/agy.new"
      chmod +x "$HOME/.local/bin/agy.new"
      mv -f "$HOME/.local/bin/agy.new" "$HOME/.local/bin/agy"
      AGY_CLI_DISABLE_AUTO_UPDATE=true "$HOME/.local/bin/agy" install --skip-path --skip-aliases --dir "$HOME/.local/bin" >/dev/null || true
      ;;
  esac
  check_agent "$agent"
done
