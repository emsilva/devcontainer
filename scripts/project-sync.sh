#!/usr/bin/env bash
set -euo pipefail

runtime_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../devcontainer_setup" && pwd)
exec python3 -I -B -X pycache_prefix=/dev/null -c \
  'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("project_sync",run_name="__main__")' \
  "$runtime_root" "$@"
