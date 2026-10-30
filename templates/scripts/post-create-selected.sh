#!/usr/bin/env bash
set -euo pipefail
if ! sudo -n bash .devcontainer/scripts/system-path.sh "$PATH" 2>/dev/null; then
  # The recovery command expands PATH when copied into the user's shell.
  # shellcheck disable=SC2016
  echo 'Could not configure system PATH; run: sudo bash .devcontainer/scripts/system-path.sh "$PATH"' >&2
  exit 1
fi
bash .devcontainer/scripts/install-agents.sh
bash .devcontainer/scripts/install-tools.sh
bash .devcontainer/scripts/validate-selected.sh
__DOTFILES__
