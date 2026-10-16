#!/usr/bin/env bash
set -euo pipefail

source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
command -v python3 >/dev/null 2>&1 || { echo 'Install Python 3.11+ to run setup.' >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo 'Python 3.11 or newer is required.' >&2; exit 1; }
export PYTHONPATH="$source_root${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m devcontainer_setup "$@"
