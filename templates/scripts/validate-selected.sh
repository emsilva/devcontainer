#!/usr/bin/env bash
set -euo pipefail
missing=0
# The renderer replaces this token with the selected command list.
# shellcheck disable=SC2043
for tool in __TOOLS__; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Missing selected tool: %s\n' "$tool" >&2
    missing=1
  fi
done
exit "$missing"
