#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"
bash .devcontainer/scripts/install-agents.sh --check
missing=0
# The renderer replaces this token with the selected command list.
# shellcheck disable=SC2043
for tool in __TOOLS__; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Missing selected tool: %s\n' "$tool" >&2
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
