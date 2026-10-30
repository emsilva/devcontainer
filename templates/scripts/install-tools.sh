#!/usr/bin/env bash
set -euo pipefail
mode=${1:-install}
if [ "$#" -gt 1 ]; then echo "Usage: $0 [--update|--check]" >&2; exit 2; fi
case "$mode" in install|--update|--check) ;; *) echo "Usage: $0 [--update|--check]" >&2; exit 2 ;; esac
if [ "$(id -u)" = 0 ]; then echo 'Run install-tools.sh as the remote user, not root.' >&2; exit 1; fi
CCSWAP_VERSION=0.42.0
PLAUD_VERSION=0.3.15
# Plaud package-lock.json engines.node: mute-stream@2.0.0 and undici@6.29.1.
PLAUD_NODE_RANGE='^18.17.0 || >=20.5.0'
tools_dir=$(cd "$(dirname "$0")/../tools" && pwd)
local_bin="$HOME/.local/bin"
plaud_root="$HOME/.local/share/plaud-cli"
temporary='' pending=''
cleanup() {
  [ -z "$temporary" ] || rm -rf "$temporary"
  [ -z "$pending" ] || rm -f "$local_bin/$pending"
  return 0
}
trap cleanup EXIT

check_ccswap() {
  local version
  version=$("$local_bin/ccswap" --version) || return 1
  version=$(printf '%s\n' "$version" | awk 'END {print $NF}')
  [ "${version#v}" = "$CCSWAP_VERSION" ]
}

check_plaud() {
  python3 - "$plaud_root" "$local_bin/plaud" "$PLAUD_VERSION" <<'PY' || return 1
import json
import os
from pathlib import Path
import sys

root, launcher, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
try:
    package = root / 'node_modules/@plaud-ai/cli/package.json'
    metadata = json.loads(package.read_text())
    assert metadata['version'] == version
    entry = (package.parent / metadata['bin']['plaud']).resolve(strict=True)
    entry.relative_to(root.resolve())
    assert launcher.is_symlink() and launcher.resolve(strict=True) == entry
    assert os.access(entry, os.X_OK)
except (OSError, ValueError, KeyError, TypeError, AssertionError):
    sys.exit(1)
PY
  PLAUD_TELEMETRY_DISABLED=1 PLAUD_NO_UPDATE_NOTIFIER=1 "$local_bin/plaud" --help >/dev/null
}

umask 077
if [ "$mode" != --check ] && { [ "$mode" = --update ] || ! check_ccswap >/dev/null 2>&1; }; then
  pending=ccswap
  mkdir -p "$local_bin"
  UV_TOOL_BIN_DIR="$local_bin" uv tool install --force --constraints "$tools_dir/ccswap-constraints.txt" "ccswap==$CCSWAP_VERSION"
fi
check_ccswap
pending=''
node_version=$(node --version)
if ! python3 - "$node_version" "$PLAUD_NODE_RANGE" <<'PY'
import re
import sys

match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', sys.argv[1])
if not match:
    sys.exit(1)
version = tuple(map(int, match.groups()))
for clause in sys.argv[2].split(' || '):
    minimum = tuple(map(int, clause.lstrip('^>=').split('.')))
    if version >= minimum and (not clause.startswith('^') or version[0] == minimum[0]):
        sys.exit(0)
sys.exit(1)
PY
then
  printf 'Plaud needs Node %s; this container has Node %s\n' "${PLAUD_NODE_RANGE/ || / or }" "$node_version"
  exit 0
fi
if [ "$mode" = --check ]; then check_plaud; exit 0; fi
if [ "$mode" != --update ] && check_plaud >/dev/null 2>&1; then
  echo 'Managed Plaud is complete; keeping installed release.'
  exit 0
fi
pending=plaud
mkdir -p "$HOME/.local/share" "$local_bin"
temporary=$(mktemp -d "$HOME/.local/share/.plaud-cli.XXXXXXXXXX")
cp "$tools_dir/plaud-cli/package.json" "$tools_dir/plaud-cli/package-lock.json" "$temporary/"
npm ci --ignore-scripts --prefix "$temporary"
rm -f "$local_bin/plaud"
rm -rf "$plaud_root"
mv "$temporary" "$plaud_root"
temporary=''
ln -sfn "$plaud_root/node_modules/@plaud-ai/cli/dist/index.js" "$local_bin/plaud"
check_plaud
pending=''
