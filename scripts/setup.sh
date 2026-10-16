#!/usr/bin/env bash
set -euo pipefail

source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
command -v python3 >/dev/null 2>&1 || { echo 'Install Python 3.11+ to run setup.' >&2; exit 1; }
python3 -I -B -X pycache_prefix=/dev/null -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo 'Python 3.11 or newer is required.' >&2; exit 1; }
# Ignore target/ambient Python imports and unverified cached bytecode.
exec python3 -I -B -X pycache_prefix=/dev/null -c 'import runpy, sys; source = sys.argv.pop(1); sys.path.insert(0, source); runpy.run_module("devcontainer_setup", run_name="__main__")' "$source_root" "$@"
