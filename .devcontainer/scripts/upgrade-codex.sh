#!/usr/bin/env bash
# Compatibility entrypoint; the baseline agents update together.
set -euo pipefail
exec bash "$(dirname "${BASH_SOURCE[0]}")/install-agents.sh" --update
