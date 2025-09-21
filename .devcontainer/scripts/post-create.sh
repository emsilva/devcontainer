#!/usr/bin/env bash
set -eu

echo "🔧 post-create: provisioning (lean mode)"

# Variables
USER_HOME="${HOME}"
USER_DOTFILES_DIR=".devcontainer/user"
SUDO=""; command -v sudo >/dev/null 2>&1 && SUDO="sudo"

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

# Generate Claude and Codex configs from templates (if present)
CLAUDE_TEMPLATE="${USER_DOTFILES_DIR}/.claude.json.template"
CLAUDE_CONFIG="${USER_DOTFILES_DIR}/.claude.json"
if [ -f "$CLAUDE_TEMPLATE" ]; then
  echo "🧩 Generating Claude config from template..."
  cp "$CLAUDE_TEMPLATE" "$CLAUDE_CONFIG"
  [ -n "${CONTEXT7_API_KEY:-}" ] && sed -i "s/__CONTEXT7_API_KEY__/${CONTEXT7_API_KEY}/g" "$CLAUDE_CONFIG" || echo "  ⚠ CONTEXT7_API_KEY not set"
  [ -n "${EXA_API_KEY:-}" ] && sed -i "s/__EXA_API_KEY__/${EXA_API_KEY}/g" "$CLAUDE_CONFIG" || echo "  ⚠ EXA_API_KEY not set"
fi

CODEX_TEMPLATE="${USER_DOTFILES_DIR}/.codex/config.toml.template"
CODEX_GENERATED="${USER_DOTFILES_DIR}/.codex/config.toml"
if [ -f "$CODEX_TEMPLATE" ]; then
  echo "🧩 Generating Codex config from template..."
  mkdir -p "$(dirname "$CODEX_GENERATED")"
  cp "$CODEX_TEMPLATE" "$CODEX_GENERATED"
  [ -n "${CONTEXT7_API_KEY:-}" ] && sed -i "s/__CONTEXT7_API_KEY__/${CONTEXT7_API_KEY}/g" "$CODEX_GENERATED" || echo "  ⚠ CONTEXT7_API_KEY not set"
  [ -n "${EXA_API_KEY:-}" ] && sed -i "s/__EXA_API_KEY__/${EXA_API_KEY}/g" "$CODEX_GENERATED" || echo "  ⚠ EXA_API_KEY not set"
fi

# Symlink user dotfiles from .devcontainer/user into $HOME (templates excluded)
if [ -d "${USER_DOTFILES_DIR}" ]; then
  echo "🧷 Linking user dotfiles from ${USER_DOTFILES_DIR}"
  while IFS= read -r -d '' dir; do
    [ "${dir}" = "${USER_DOTFILES_DIR}" ] && continue
    rel_dir="${dir#"${USER_DOTFILES_DIR}"/}"
    mkdir -p "${USER_HOME}/${rel_dir}"
  done < <(find "${USER_DOTFILES_DIR}" -type d -print0)

  while IFS= read -r -d '' src; do
    case "$src" in *.template) continue;; esac
    rel_path="${src#"${USER_DOTFILES_DIR}"/}"
    dest="${USER_HOME}/${rel_path}"
    mkdir -p "$(dirname "$dest")"
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
      mv "$dest" "${dest}.pre-devcontainer.$(date +%s)"
    fi
    ln -sfn "${PWD}/${src}" "$dest"
  done < <(find "${USER_DOTFILES_DIR}" -type f -print0)
fi

# Harden oh-my-zsh permissions for completion safety
if [ -d "${USER_HOME}/.oh-my-zsh" ]; then
  if [ -n "${SUDO:-}" ]; then
    $SUDO chmod go-w "${USER_HOME}/.oh-my-zsh" "${USER_HOME}/.oh-my-zsh/custom" 2>/dev/null || true
  else
    chmod go-w "${USER_HOME}/.oh-my-zsh" "${USER_HOME}/.oh-my-zsh/custom" 2>/dev/null || true
  fi
fi

# Minimal system-wide PATH baseline for login shells -> source shared env script when possible
CORE_PROFILED="/etc/profile.d/00-core-path.sh"
{
  cat > "/tmp/00-core-path.sh" <<'EOF'
#!/bin/sh
if [ -z "${HOME:-}" ]; then
  h=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)
  [ -n "$h" ] && HOME="$h" && export HOME
fi

if [ -n "${HOME:-}" ] && [ -f "$HOME/.config/shell/env-base.sh" ]; then
  # shellcheck disable=SC1090
  . "$HOME/.config/shell/env-base.sh"
else
  PATH="${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
  export PATH
fi
EOF
  if [ -n "${SUDO:-}" ]; then $SUDO cp /tmp/00-core-path.sh "$CORE_PROFILED" && $SUDO chmod 644 "$CORE_PROFILED" || true; else cp /tmp/00-core-path.sh "$CORE_PROFILED" && chmod 644 "$CORE_PROFILED" || true; fi
  rm -f /tmp/00-core-path.sh 2>/dev/null || true
} || true

# Enable corepack for pnpm/yarn if available
if command -v corepack >/dev/null 2>&1; then
  corepack enable >/dev/null 2>&1 || true
fi

# Install crane (OCI utility) via Go if missing
if ! command -v crane >/dev/null 2>&1; then
  if command -v go >/dev/null 2>&1; then
    echo "🪝 Installing crane (go-containerregistry)"
    if GO111MODULE=on GOBIN="${USER_HOME}/.local/bin" go install github.com/google/go-containerregistry/cmd/crane@latest; then
      if [ -x "${USER_HOME}/.local/bin/crane" ]; then
        if [ -n "${SUDO}" ]; then
          $SUDO ln -sfn "${USER_HOME}/.local/bin/crane" /usr/local/bin/crane
        else
          ln -sfn "${USER_HOME}/.local/bin/crane" /usr/local/bin/crane
        fi
      fi
    else
      echo "  ⚠ Failed to install crane" >&2
    fi
  else
    echo "  ⚠ go not found; skipping crane install" >&2
  fi
else
  echo "🪝 crane already available"
fi

# Install vivid via cargo when available
if command -v cargo >/dev/null 2>&1; then
  if ! command -v vivid >/dev/null 2>&1; then
    echo "🌈 Installing vivid (cargo)"
    if cargo install vivid; then
      echo "  ✅ vivid installed"
    else
      echo "  ⚠ Failed to install vivid" >&2
    fi
  else
    echo "🌈 vivid already available"
  fi
else
  echo "  ⚠ cargo not found; skipping vivid install" >&2
fi

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
      export GITHUB_TOKEN="$personal_pat"
      echo "  🔄 Exported GITHUB_TOKEN for downstream tooling"
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

# Install Rails when Ruby is available
if command -v gem >/dev/null 2>&1; then
  if ! command -v rails >/dev/null 2>&1; then
    echo "🚂 Installing Rails (Ruby on Rails)"
    if gem install rails --no-document; then
      echo "  ✅ Rails installed"
    else
      echo "  ⚠ Failed to install Rails" >&2
    fi
  else
    echo "🚂 Rails already available"
  fi
else
  echo "  ⚠ RubyGems not available; skipping Rails install" >&2
fi

echo "✅ post-create complete"
