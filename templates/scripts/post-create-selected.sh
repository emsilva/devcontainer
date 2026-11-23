#!/usr/bin/env bash
set -euo pipefail
SUDO=""
command -v sudo >/dev/null 2>&1 && SUDO="sudo"

if ! sudo -n bash .devcontainer/scripts/system-path.sh "$PATH" "$(command -v python3)" "$HOME/.local/bin/open-on-mac"; then
  # The recovery command expands PATH when copied into the user's shell.
  # shellcheck disable=SC2016
  echo 'Could not configure system PATH; run: sudo bash .devcontainer/scripts/system-path.sh "$PATH" "$(command -v python3)" "$HOME/.local/bin/open-on-mac"' >&2
  exit 1
fi
bash .devcontainer/scripts/install-agents.sh
bash .devcontainer/scripts/install-tools.sh
bash .devcontainer/scripts/validate-selected.sh

# Align default login shell to zsh when installed (non-fatal).
if command -v chsh >/dev/null 2>&1 && [ -x /usr/bin/zsh ]; then
  current_shell=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f7 || true)
  if [ "$current_shell" != "/usr/bin/zsh" ]; then
    if [ -n "$SUDO" ]; then
      $SUDO chsh -s /usr/bin/zsh "$(id -un)" >/dev/null 2>&1 || true
    else
      chsh -s /usr/bin/zsh "$(id -un)" >/dev/null 2>&1 || true
    fi
  fi
fi

# Apply requested container timezone when DEVCONTAINER_TZ is provided in the environment.
if [ -n "${DEVCONTAINER_TZ:-}" ] && [ -e "/usr/share/zoneinfo/${DEVCONTAINER_TZ}" ]; then
  if [ -n "$SUDO" ]; then
    $SUDO ln -sf "/usr/share/zoneinfo/${DEVCONTAINER_TZ}" /etc/localtime 2>/dev/null || true
    printf '%s\n' "${DEVCONTAINER_TZ}" | $SUDO tee /etc/timezone >/dev/null 2>&1 || true
  fi
  export TZ="${DEVCONTAINER_TZ}"
fi

# Authenticate gh with PERSONAL_PAT when provided via environment/Codespaces secrets.
if command -v gh >/dev/null 2>&1 && [ -n "${PERSONAL_PAT:-}" ]; then
  unset GH_TOKEN GITHUB_TOKEN GH_AUTH_TOKEN || true
  if printf '%s\n' "${PERSONAL_PAT}" | gh auth login --with-token --hostname github.com --git-protocol https >/dev/null 2>&1; then
    gh auth setup-git >/dev/null 2>&1 || true
  fi
fi

# Preinstall herdr when absent so first task work:cs needs no interactive install prompt.
if ! command -v herdr >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/herdr" ]; then
  curl -fsSL https://herdr.dev/install.sh | sh >/dev/null 2>&1 || true
fi
__DOTFILES__
