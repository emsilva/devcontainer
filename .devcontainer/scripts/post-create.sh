#!/usr/bin/env bash
set -euo pipefail

echo "🔧 post-create: provisioning (lean mode)"

# Variables
USER_HOME="${HOME}"
SUDO=""
command -v sudo >/dev/null 2>&1 && SUDO="sudo"

ensure_volume_dir() {
  dir="$1"
  if ! mkdir -p "$dir" 2>/dev/null; then
    if [ -n "$SUDO" ]; then
      $SUDO mkdir -p "$dir"
    fi
  fi
  if [ -n "$SUDO" ]; then
    $SUDO chown -R vscode:vscode "$dir" 2>/dev/null || true
  fi
}

ensure_user_dir() {
  dir="$1"
  mkdir -p "$dir" 2>/dev/null || true
}

repair_claude_install_permissions() {
  if ! command -v claude >/dev/null 2>&1; then
    return
  fi

  user_name="$(id -un)"
  group_name="$(id -gn)"
  claude_bin="$(command -v claude)"

  echo "🤖 Repairing Claude CLI ownership for self-upgrades"

  fix_owner() {
    path="$1"
    [ -e "$path" ] || return
    if [ -n "$SUDO" ]; then
      $SUDO chown -R "${user_name}:${group_name}" "$path" 2>/dev/null || true
    else
      chown -R "${user_name}:${group_name}" "$path" 2>/dev/null || true
    fi
  }

  # Binary path can be a symlink into NVM/global npm directories.
  fix_owner "$claude_bin"
  if command -v readlink >/dev/null 2>&1; then
    resolved_bin="$(readlink -f "$claude_bin" 2>/dev/null || true)"
    [ -n "${resolved_bin:-}" ] && fix_owner "$resolved_bin"
  fi

  if command -v npm >/dev/null 2>&1; then
    npm_prefix="$(npm config get prefix 2>/dev/null || true)"
    npm_root_global="$(npm root -g 2>/dev/null || true)"
    [ -n "${npm_prefix:-}" ] && fix_owner "$npm_prefix"
    if [ -n "${npm_root_global:-}" ]; then
      fix_owner "$npm_root_global"
      fix_owner "${npm_root_global}/@anthropic-ai"
    fi
  fi
}

ensure_login_shell() {
  desired_shell="/usr/bin/zsh"
  command -v chsh >/dev/null 2>&1 || return
  [ -x "$desired_shell" ] || {
    echo "  ⚠ Desired shell $desired_shell not found; skipping chsh" >&2
    return
  }

  current_shell="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f7)"
  if [ "$current_shell" = "$desired_shell" ]; then
    echo "🌀 Default shell already set to zsh"
    return
  fi

  user_name="$(id -un)"
  if [ -n "$SUDO" ]; then
    if $SUDO chsh -s "$desired_shell" "$user_name" >/dev/null 2>&1; then
      echo "🌀 Updated default shell to zsh"
    else
      echo "  ⚠ Failed to set default shell to zsh via sudo" >&2
    fi
  else
    if chsh -s "$desired_shell" "$user_name" >/dev/null 2>&1; then
      echo "🌀 Updated default shell to zsh"
    else
      echo "  ⚠ Failed to set default shell to zsh" >&2
    fi
  fi
}

configure_timezone() {
  fallback_tz="America/Sao_Paulo"
  desired_tz="${DEVCONTAINER_TZ:-${TZ:-}}"
  [ -n "$desired_tz" ] || desired_tz="$fallback_tz"

  if [ -z "$desired_tz" ]; then
    echo "  ⚠ No timezone configured; keeping container default"
    return
  fi

  zoneinfo_path="/usr/share/zoneinfo/${desired_tz}"
  if [ ! -e "$zoneinfo_path" ]; then
    echo "  ⚠ Timezone ${desired_tz} not found under /usr/share/zoneinfo; skipping" >&2
    return
  fi

  current_link="$(readlink /etc/localtime 2>/dev/null || true)"
  if [ "$current_link" = "$zoneinfo_path" ]; then
    echo "🕒 Timezone already set to ${desired_tz}"
  else
    if [ -n "$SUDO" ]; then
      if $SUDO ln -sf "$zoneinfo_path" /etc/localtime && printf '%s\n' "$desired_tz" | $SUDO tee /etc/timezone >/dev/null; then
        echo "🕒 Set timezone to ${desired_tz}"
      else
        echo "  ⚠ Failed to update timezone to ${desired_tz}" >&2
      fi
    else
      if ln -sf "$zoneinfo_path" /etc/localtime && printf '%s\n' "$desired_tz" >/etc/timezone; then
        echo "🕒 Set timezone to ${desired_tz}"
      else
        echo "  ⚠ Failed to update timezone to ${desired_tz}" >&2
      fi
    fi
  fi

  export TZ="$desired_tz"
}

apply_dotfiles_if_configured() {
  # Personal dotfiles are applied per-user via chezmoi (see scripts/setup-dotfiles.sh).
  # Runs after gh auth so private repos resolve via the credential helper.
  if [ -n "${DOTFILES_REPO:-}" ]; then
    if [ -f .devcontainer/scripts/setup-dotfiles.sh ]; then
      bash .devcontainer/scripts/setup-dotfiles.sh "${DOTFILES_REPO}" \
        || echo "  ⚠ dotfiles apply failed (retry: load-dotfiles ${DOTFILES_REPO})" >&2
    fi
  else
    echo "💡 No DOTFILES_REPO set — run 'task setup:dotfiles -- <repo>' or 'load-dotfiles <repo>' to load your dotfiles"
  fi
}

authorize_ssh_keys() {
  # Authorize key(s) for the in-container sshd (port 2222, via the sshd feature).
  # SSH_AUTHORIZED_KEYS holds one or more public keys (newline-separated) — e.g. a
  # Codespaces secret, or an entry in .devcontainer/devcontainer.env locally.
  [ -n "${SSH_AUTHORIZED_KEYS:-}" ] || return 0
  echo "🔑 Installing SSH authorized key(s) for the in-container sshd (port 2222)"
  mkdir -p "${USER_HOME}/.ssh"
  chmod 700 "${USER_HOME}/.ssh"
  printf '%s\n' "${SSH_AUTHORIZED_KEYS}" >"${USER_HOME}/.ssh/authorized_keys"
  chmod 600 "${USER_HOME}/.ssh/authorized_keys"
}

# Load local-only secrets/config for non-Codespaces runs (gitignored). Absent in
# Codespaces, where PERSONAL_PAT / DOTFILES_REPO arrive as Codespaces secrets.
if [ -f .devcontainer/devcontainer.env ]; then
  set -a
  # shellcheck disable=SC1091
  . .devcontainer/devcontainer.env
  set +a
fi

# Basic permissions and caches (volumes are mounted by devcontainer)
echo "📁 Ensuring caches and history volume perms"
ensure_volume_dir /commandhistory
ensure_volume_dir "${USER_HOME}/.npm"
ensure_volume_dir "${USER_HOME}/.cache"
ensure_volume_dir "${USER_HOME}/.local"
ensure_user_dir "${USER_HOME}/.cache/starship"
ensure_user_dir "${USER_HOME}/.cache/uv"
ensure_user_dir "${USER_HOME}/.cache/go-mod"
ensure_user_dir "${USER_HOME}/.local/bin"
ensure_user_dir "${USER_HOME}/.config"

# Ensure default shell is zsh for the vscode user (aligns with VS Code terminal profile)
ensure_login_shell

# Configure system timezone if requested
configure_timezone

# Claude is installed through a feature at build time and may be owned by root.
# Normalize ownership so `claude update` works after deployment.
repair_claude_install_permissions

# Basic Git defaults
git config --global init.defaultBranch main
git config --global pull.rebase false
git config --global fetch.prune true
git config --global diff.colorMoved zebra
git config --global core.editor "${EDITOR:-code --wait}"

# Optional GitHub CLI auth setup (when gh is installed)
if command -v gh >/dev/null 2>&1; then
  personal_pat="${PERSONAL_PAT:-}"
  if [ -n "$personal_pat" ]; then
    echo "🔐 Configuring GitHub CLI authentication via PERSONAL_PAT"
    cleared=""
    for env_var in GH_TOKEN GITHUB_TOKEN GH_AUTH_TOKEN; do
      if [ -n "${!env_var:-}" ]; then
        if [ -n "$cleared" ]; then
          cleared="$cleared, $env_var"
        else
          cleared="$env_var"
        fi
      fi
      unset "$env_var" || true
    done
    if [ -n "$cleared" ]; then
      echo "  ↺ Cleared preset GitHub token env vars: $cleared"
    fi
    if printf '%s\n' "$personal_pat" | gh auth login --with-token --hostname github.com --git-protocol https >/dev/null 2>&1; then
      if gh auth setup-git >/dev/null 2>&1; then
        echo "  🔁 Configured git credential helper via gh"
      else
        echo "  ⚠ Failed to configure git credential helper via gh" >&2
      fi
      if gh auth status >/dev/null 2>&1; then
        echo "  ✅ GitHub CLI authenticated with PERSONAL_PAT"
      else
        echo "  ⚠ GitHub CLI authenticated but status check failed" >&2
      fi
    else
      echo "  ⚠ Failed to authenticate GitHub CLI with PERSONAL_PAT" >&2
    fi
    unset personal_pat
  else
    echo "🔐 GitHub CLI detected; PERSONAL_PAT not provided — reusing existing auth"
    if gh auth setup-git >/dev/null 2>&1; then
      echo "  🔁 Configured git credential helper via existing gh auth"
    else
      echo "  ⚠ Unable to configure git credential helper via gh" >&2
    fi
    if gh auth status >/dev/null 2>&1; then
      echo "  ✅ Using existing GitHub CLI authentication"
    else
      echo "  ⚠ GitHub CLI not authenticated; run 'gh auth login' if needed" >&2
    fi
  fi
fi

# Authorize SSH key(s) for the in-container sshd — only when SSH_AUTHORIZED_KEYS is set
authorize_ssh_keys

# Apply personal dotfiles (chezmoi) — only when DOTFILES_REPO is set
apply_dotfiles_if_configured

echo "✅ post-create complete"
