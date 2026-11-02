#!/usr/bin/env bash
set -euo pipefail
bash .devcontainer/scripts/install-agents.sh --check
bash .devcontainer/scripts/install-tools.sh --check
missing=0
# Read the system's PATH, without borrowing the caller's additions.
system_path=$(python3 - "${DEVCONTAINER_SYSTEM_ROOT:-}/etc/environment" <<'PY'
from pathlib import Path
import shlex
import sys

default = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
try:
    lines = Path(sys.argv[1]).read_text().splitlines()
except FileNotFoundError:
    lines = []
for line in lines:
    if line.lstrip().startswith('PATH='):
        values = shlex.split(line.strip()[5:], comments=True)
        print(values[0] if values else '')
        break
else:
    print(default)
PY
)
fresh_shells=()
for shell in bash zsh; do
  if shell_path=$(command -v "$shell"); then
    fresh_shells+=("$shell_path")
  elif [ "$shell" = zsh ]; then
    echo 'Skipping fresh zsh lookup: zsh is not installed.' >&2
  else
    echo 'Cannot run a fresh bash lookup: bash is not installed.' >&2
    missing=1
  fi
done

fresh_shell_lookup() {
  local tool="$1" shell failed=0
  for shell in "${fresh_shells[@]}"; do
    # Expand the positional argument only in the fresh child shell.
    # shellcheck disable=SC2016
    if ! env -i HOME="$HOME" USER="${USER:-vscode}" PATH="$system_path" \
      "$shell" -c 'command -v "$1"' fresh-shell "$tool" >/dev/null 2>&1; then
      failed=1
    fi
  done
  if [ "$failed" -ne 0 ]; then
    printf 'Not on the PATH of a fresh shell: %s\n' "$tool" >&2
    return 1
  fi
}

browser_helper="$HOME/.local/bin/open-on-mac"
if [ ! -x "$browser_helper" ]; then
  echo 'Missing browser helper: open-on-mac' >&2
  missing=1
fi
if ! python3 - "$HOME/.local/bin/xdg-open" "$browser_helper" <<'PY'
from pathlib import Path
import sys

link, helper = map(Path, sys.argv[1:])
sys.exit(0 if link.is_symlink() and link.resolve() == helper.resolve() else 1)
PY
then
  echo 'Wrong xdg-open link: expected open-on-mac' >&2
  missing=1
fi
if ! system_browser=$(python3 - "${DEVCONTAINER_SYSTEM_ROOT:-}/etc/environment" "$browser_helper" <<'PY'
from pathlib import Path
import shlex
import sys

try:
    values = [shlex.split(line.split('=', 1)[1], comments=True)
              for line in Path(sys.argv[1]).read_text().splitlines()
              if line.lstrip().startswith('BROWSER=')]
    if values != [[sys.argv[2]]]:
        sys.exit(1)
    print(values[0][0])
except (OSError, ValueError):
    sys.exit(1)
PY
); then
  echo 'Missing or wrong system BROWSER: expected open-on-mac' >&2
  missing=1
else
  for shell in "${fresh_shells[@]}"; do
    # shellcheck disable=SC2016
    if ! env -i HOME="$HOME" USER="${USER:-vscode}" PATH="$system_path" BROWSER="$system_browser" \
      "$shell" -c 'test "$BROWSER" = "$1"' fresh-browser "$browser_helper"; then
      echo 'Wrong BROWSER in a fresh shell: expected open-on-mac' >&2
      missing=1
    fi
  done
fi

# The renderer replaces this token with the selected command list.
# shellcheck disable=SC2043
for tool in __TOOLS__; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Missing selected tool: %s\n' "$tool" >&2
    missing=1
  fi
done
# The renderer keeps this separate from installation completeness checks.
# shellcheck disable=SC2043
for tool in __FRESH_TOOLS__; do
  if ! fresh_shell_lookup "$tool"; then
    missing=1
  fi
done
# The renderer adds calls with the selected exact versions below.
# shellcheck disable=SC2317,SC2329
check_manager_version() {
  local tool="$1" expected="$2" actual
  if ! actual=$("$tool" --version); then
    printf 'Cannot verify selected %s version (expected %s)\n' "$tool" "$expected" >&2
    missing=1
  elif [ "$actual" != "$expected" ]; then
    printf 'Selected %s version mismatch: expected %s, got %s\n' "$tool" "$expected" "$actual" >&2
    missing=1
  fi
}
__MANAGER_VERSIONS__
exit "$missing"
