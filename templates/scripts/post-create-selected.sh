#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"
bash .devcontainer/scripts/install-agents.sh
bash .devcontainer/scripts/validate-selected.sh
__DOTFILES__
