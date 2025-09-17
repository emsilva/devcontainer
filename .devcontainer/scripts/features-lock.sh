#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PYTHON_BIN=${PYTHON:-python3}
LOCK_FILE=${LOCK_FILE:-"${SCRIPT_DIR}/../features.lock"}

if ! command -v crane >/dev/null 2>&1; then
  echo "crane is required; please ensure it is installed" >&2
  exit 2
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/features_lock.py" "${1:-check}" "$LOCK_FILE"
