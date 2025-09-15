#!/usr/bin/env bash
set -eu

echo "🔧 post-create: provisioning (Mannu's Devcontainer - Ubuntu Style)"

# Variables
ZSHRC="${HOME}/.zshrc"
ZSH_CUSTOM="${ZSH_CUSTOM:-${HOME}/.oh-my-zsh/custom}"
USER_HOME="${HOME}"
USER_DOTFILES_DIR=".devcontainer/user"
SUDO=""

# Detect if we need sudo
if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# ──────────────────────────────────────────────────────────────────────────────
# Fix permissions for mounted volumes
# ──────────────────────────────────────────────────────────────────────────────
echo "📁 Fixing permissions for mounted volumes..."
$SUDO chown -R vscode:vscode /commandhistory "${USER_HOME}/.npm" "${USER_HOME}/.cache" 2>/dev/null || true
# Create cache directories
mkdir -p "${USER_HOME}/.cache/starship" "${USER_HOME}/.cache/uv" "${USER_HOME}/.cache/go-mod"

# ──────────────────────────────────────────────────────────────────────────────
# Install base tools via apt
# ──────────────────────────────────────────────────────────────────────────────
echo "📦 Installing base tools..."
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -y
$SUDO apt-get install -y --no-install-recommends \
  ca-certificates \
  build-essential \
  pkg-config \
  libssl-dev \
  lsb-release \
  gnupg \
  tzdata \
  bat \
  vim \
  nano \
  zoxide \
  tmux \
  tree \
  tldr \
  curl \
  wget \
  dnsutils \
  net-tools \
  iputils-ping \
  httpie \
  git \
  git-lfs \
  fd-find \
  ripgrep \
  jq \
  yq \
  postgresql-client \
  default-mysql-client \
  sqlite3 \
  redis-tools \
  htop \
  ncdu \
  silversearcher-ag

# Cleanup apt caches to reduce image size
$SUDO apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* 2>/dev/null || true

# ──────────────────────────────────────────────────────────────────────────────
# Timezone configuration (America/Sao_Paulo)
# ──────────────────────────────────────────────────────────────────────────────
if [ -f /usr/share/zoneinfo/America/Sao_Paulo ]; then
  echo "🌐 Setting timezone to America/Sao_Paulo"
  $SUDO ln -sf /usr/share/zoneinfo/America/Sao_Paulo /etc/localtime
  echo 'America/Sao_Paulo' | $SUDO tee /etc/timezone >/dev/null
  if dpkg -s tzdata >/dev/null 2>&1; then
    $SUDO dpkg-reconfigure -f noninteractive tzdata >/dev/null 2>&1 || true
  fi
else
  echo "⚠️  Timezone data missing for America/Sao_Paulo" >&2
fi

# Create symlinks for Ubuntu naming conventions
$SUDO ln -sf /usr/bin/fdfind /usr/local/bin/fd 2>/dev/null || true
$SUDO ln -sf /usr/bin/batcat /usr/local/bin/bat 2>/dev/null || true

# ──────────────────────────────────────────────────────────────────────────────
# Install fastfetch
# ──────────────────────────────────────────────────────────────────────────────
if ! command -v fastfetch >/dev/null 2>&1; then
  echo "🎨 Installing fastfetch..."
  # The universal installer uses bash-specific syntax ([[ ]], process substitutions, etc.)
  # so we invoke it with bash explicitly instead of the default /bin/sh (dash) to prevent errors.
  curl -fsSL https://raw.githubusercontent.com/ValkyrieNexus/fastfetch-universal-installer/main/install-fastfetch-universal.sh | bash || true
fi

# ──────────────────────────────────────────────────────────────────────────────
# Install vivid (LS_COLORS theme generator)
# ──────────────────────────────────────────────────────────────────────────────
if ! command -v vivid >/dev/null 2>&1; then
  echo "🌈 Installing vivid (color theme generator)..."
  VIVID_VERSION="0.10.1"
  VIVID_DEB="vivid_${VIVID_VERSION}_amd64.deb"
  VIVID_URL="https://github.com/sharkdp/vivid/releases/download/v${VIVID_VERSION}/${VIVID_DEB}"
  wget -q "${VIVID_URL}" -O "/tmp/${VIVID_DEB}"
  # Install (auto-fix missing dependencies if any)
  if ! $SUDO dpkg -i "/tmp/${VIVID_DEB}" >/dev/null 2>&1; then
    echo "  Resolving vivid dependencies..."
    $SUDO apt-get update -y && $SUDO apt-get install -f -y --no-install-recommends >/dev/null 2>&1 || true
    $SUDO dpkg -i "/tmp/${VIVID_DEB}" >/dev/null 2>&1 || true
  fi
  rm -f "/tmp/${VIVID_DEB}" 2>/dev/null || true
fi

# ──────────────────────────────────────────────────────────────────────────────
# Install fzf from Git (for proper shell integration)
# ──────────────────────────────────────────────────────────────────────────────
if [ ! -d "${USER_HOME}/.fzf" ]; then
  echo "🔍 Installing fzf from Git..."
  git clone --depth 1 https://github.com/junegunn/fzf.git "${USER_HOME}/.fzf"
  # Install without updating rc files (we handle that in .zshrc)
  "${USER_HOME}/.fzf/install" --no-update-rc --no-bash --no-fish --completion --key-bindings >/dev/null 2>&1 || true
else
  echo "🔍 fzf already installed at ${USER_HOME}/.fzf"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Generate Claude configuration from template with secrets
# ──────────────────────────────────────────────────────────────────────────────
CLAUDE_TEMPLATE="${USER_DOTFILES_DIR}/.claude.json.template"
CLAUDE_CONFIG="${USER_DOTFILES_DIR}/.claude.json"

if [ -f "$CLAUDE_TEMPLATE" ]; then
  echo "🤖 Generating Claude configuration from template..."
  cp "$CLAUDE_TEMPLATE" "$CLAUDE_CONFIG"

  # Replace placeholders with environment variables if available
  if [ -n "${CONTEXT7_API_KEY:-}" ]; then
    sed -i "s/__CONTEXT7_API_KEY__/${CONTEXT7_API_KEY}/g" "$CLAUDE_CONFIG"
    echo "  ✓ Context7 API key configured"
  else
    echo "  ⚠ CONTEXT7_API_KEY not found - MCP server will need manual configuration"
  fi

  if [ -n "${EXA_API_KEY:-}" ]; then
    sed -i "s/__EXA_API_KEY__/${EXA_API_KEY}/g" "$CLAUDE_CONFIG"
    echo "  ✓ Exa API key configured"
  else
    echo "  ⚠ EXA_API_KEY not found - MCP server will need manual configuration"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# Generate Codex configuration (mirrors Claude pattern, stays in user dir)
# ──────────────────────────────────────────────────────────────────────────────
CODEX_TEMPLATE="${USER_DOTFILES_DIR}/.codex/config.toml.template"
CODEX_GENERATED="${USER_DOTFILES_DIR}/.codex/config.toml"
if [ -f "$CODEX_TEMPLATE" ]; then
  echo "🧠 Generating Codex configuration from template..."
  cp "$CODEX_TEMPLATE" "$CODEX_GENERATED"
  if [ -n "${CONTEXT7_API_KEY:-}" ]; then
    sed -i "s/__CONTEXT7_API_KEY__/${CONTEXT7_API_KEY}/g" "$CODEX_GENERATED"
    echo "  ✓ Context7 API key configured (Codex)"
  else
    echo "  ⚠ CONTEXT7_API_KEY not found for Codex (placeholder left)"
  fi
  if [ -n "${EXA_API_KEY:-}" ]; then
    sed -i "s/__EXA_API_KEY__/${EXA_API_KEY}/g" "$CODEX_GENERATED"
    echo "  ✓ Exa API key configured (Codex)"
  else
    echo "  ⚠ EXA_API_KEY not found for Codex (placeholder left)"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# Symlink user dotfiles from .devcontainer/user into $HOME
#   - Mirrors the directory structure under $HOME
#   - Only files are symlinked; parent directories are created as needed
#   - Existing non-symlink targets are backed up once with a timestamp
# ──────────────────────────────────────────────────────────────────────────────
if [ -d "${USER_DOTFILES_DIR}" ]; then
  echo "📁 Creating user directories under $HOME"
  # First pass: create directory structure in $HOME to mirror .devcontainer/user
  while IFS= read -r -d '' dir; do
    # Skip the root folder itself
    if [ "${dir}" = "${USER_DOTFILES_DIR}" ]; then continue; fi
  rel_dir="${dir#"${USER_DOTFILES_DIR}"/}"
    dest_dir="${USER_HOME}/${rel_dir}"
    mkdir -p "${dest_dir}"
  done < <(find "${USER_DOTFILES_DIR}" -type d -print0)

  echo "🧷 Linking user dotfiles from ${USER_DOTFILES_DIR}"
  # Second pass: link files
  while IFS= read -r -d '' src; do
    # Skip template files - they're for generation only
    if [[ "$src" == *.template ]]; then continue; fi
  rel_path="${src#"${USER_DOTFILES_DIR}"/}"
    dest="${USER_HOME}/${rel_path}"
    mkdir -p "$(dirname "${dest}")"
    if [ -e "${dest}" ] && [ ! -L "${dest}" ]; then
      bk="${dest}.pre-devcontainer.$(date +%s)"
      echo "  Backing up ${dest} -> ${bk}"
      mv "${dest}" "${bk}"
    fi
    # Use absolute path from workspace root to keep links stable
    abs_src="${PWD}/${src}"
    ln -sfn "${abs_src}" "${dest}"
    echo "  ${dest} -> ${abs_src}"
  done < <(find "${USER_DOTFILES_DIR}" -type f -print0)
else
  echo "ℹ️  ${USER_DOTFILES_DIR} not found; skipping dotfile linking"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Oh-My-Zsh plugins
# ──────────────────────────────────────────────────────────────────────────────
echo "🔌 Installing Oh-My-Zsh plugins..."
mkdir -p "$ZSH_CUSTOM/plugins"

# zsh-autosuggestions
if [ ! -d "$ZSH_CUSTOM/plugins/zsh-autosuggestions" ]; then
  git clone --depth=1 https://github.com/zsh-users/zsh-autosuggestions \
    "$ZSH_CUSTOM/plugins/zsh-autosuggestions"
fi

# zsh-syntax-highlighting
if [ ! -d "$ZSH_CUSTOM/plugins/zsh-syntax-highlighting" ]; then
  git clone --depth=1 https://github.com/zsh-users/zsh-syntax-highlighting \
    "$ZSH_CUSTOM/plugins/zsh-syntax-highlighting"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Starship prompt
# ──────────────────────────────────────────────────────────────────────────────
if ! command -v starship >/dev/null 2>&1; then
  echo "✨ Installing Starship..."
  mkdir -p "${USER_HOME}/.local/bin"
  curl -fsSL https://starship.rs/install.sh | sh -s -- -y -b "${USER_HOME}/.local/bin"
fi

# Starship config now provided via symlink from .devcontainer/user/.config/starship.toml
mkdir -p "${USER_HOME}/.config"

# ──────────────────────────────────────────────────────────────────────────────
# Python toolchain: uv
# ──────────────────────────────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "📦 Installing uv (Python package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# ──────────────────────────────────────────────────────────────────────────────
# Claude Code CLI
# ──────────────────────────────────────────────────────────────────────────────
if ! command -v claude >/dev/null 2>&1; then
  echo "🤖 Installing Claude Code CLI..."
  curl -fsSL https://claude.ai/install.sh | bash -s latest 2>/dev/null || true
fi

# ──────────────────────────────────────────────────────────────────────────────
# Setup NPM global directory
# ──────────────────────────────────────────────────────────────────────────────
if command -v npm >/dev/null 2>&1; then
  echo "🟢 Configuring NPM..."
  # Detect if Node is managed by nvm without relying on the interactive 'nvm' function.
  # Heuristics:
  #  - NVM_DIR exists OR
  #  - 'node' binary path contains '/nvm/'
  if [ -d "/usr/local/share/nvm" ] || command -v node | grep -q "/nvm/"; then
    # Ensure no user-level prefix is set (prevents nvm warnings)
    npm config delete prefix >/dev/null 2>&1 || true
    if [ -f "${USER_HOME}/.npmrc" ]; then
      # Remove any lingering prefix lines from previous runs
      sed -i '/^prefix=/d' "${USER_HOME}/.npmrc" 2>/dev/null || true
    fi
  else
    # No nvm context detected: set a local user prefix for global packages
    mkdir -p "${USER_HOME}/.npm-global"
    npm config set prefix "${USER_HOME}/.npm-global" >/dev/null 2>&1 || true
  fi

  # Enable corepack for pnpm/yarn.
  # Devcontainer Node feature and/or nvm may already have created yarn/pnpm shims.
  # A second "corepack enable" can throw a noisy EEXIST stack trace; treat as benign.
  if command -v corepack >/dev/null 2>&1; then
    if ! corepack enable 2>/tmp/corepack-enable.log 1>&2; then
      if grep -q 'EEXIST: file already exists' /tmp/corepack-enable.log 2>/dev/null; then
        echo "ℹ️ corepack already enabled (suppressing EEXIST)"
      else
        echo "⚠️ corepack enable encountered an unexpected issue (continuing)" >&2
        sed 's/^/corepack: /' /tmp/corepack-enable.log 2>/dev/null || true
      fi
    fi
    rm -f /tmp/corepack-enable.log 2>/dev/null || true
  fi

  echo "📦 Installing global NPM packages..."
  npm install -g \
    prettier \
    eslint \
    typescript \
    ts-node \
    nodemon \
    npm-check-updates \
    @openai/codex \
    2>/dev/null || true
fi

# ──────────────────────────────────────────────────────────────────────────────
# Project dependency installation
# ──────────────────────────────────────────────────────────────────────────────
echo "📂 Checking for project dependencies..."

# Node.js project
if [ -f package.json ] && command -v node >/dev/null 2>&1; then
  echo "  Installing Node.js dependencies..."
  if [ -f pnpm-lock.yaml ] && command -v pnpm >/dev/null 2>&1; then
    pnpm install
  elif [ -f yarn.lock ] && command -v yarn >/dev/null 2>&1; then
    yarn install
  elif [ -f package-lock.json ]; then
    npm ci || npm install
  else
    npm install
  fi
fi

# Python project
if [ -f requirements.txt ] || [ -f pyproject.toml ] || [ -f Pipfile ]; then
  echo "  Setting up Python environment..."
  if command -v uv >/dev/null 2>&1; then
    if [ -f pyproject.toml ]; then
      uv sync || true
    elif [ -f requirements.txt ]; then
      uv pip install -r requirements.txt || true
    fi
  elif [ -f requirements.txt ]; then
    pip3 install -r requirements.txt || true
  fi
fi

# Go project
if [ -f go.mod ] && command -v go >/dev/null 2>&1; then
  echo "  Downloading Go dependencies..."
  go mod download

  # Install tools if specified
  if [ -f tools.go ]; then
    awk -F'"' '/_ /{print $2}' tools.go | xargs -r -I {} sh -c 'go install {}@latest || true'
  fi
  if [ -f .tools ]; then
    xargs -r -L1 -a .tools -I {} sh -c 'go install {}@latest || true'
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# Task (go-task) - cross-platform task runner
# Do NOT apt-get install 'task' (Taskwarrior). Install from official script.
# Docs: https://taskfile.dev/#/installation
# ──────────────────────────────────────────────────────────────────────────────
if ! command -v task >/dev/null 2>&1; then
  echo "🧰 Installing Task (go-task) ..."
  # Install latest release to ~/.local/bin (already in PATH via .zshrc)
  curl -fsSL https://taskfile.dev/install.sh | sh -s -- -d -b "${USER_HOME}/.local/bin" || true
  # Fallback: install into /usr/local/bin if ~/.local/bin not writable for any reason
  if ! command -v task >/dev/null 2>&1 && [ -n "$SUDO" ]; then
    echo "   Retrying install to /usr/local/bin ..."
    curl -fsSL https://taskfile.dev/install.sh | bash -s -- -d -b /usr/local/bin || true
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# Git configuration
# ──────────────────────────────────────────────────────────────────────────────
echo "🔧 Configuring Git..."
git config --global init.defaultBranch main
git config --global pull.rebase false
git config --global fetch.prune true
git config --global diff.colorMoved zebra
git config --global core.editor "${EDITOR:-code --wait}"

if [ -n "$CODESPACES" ]; then
    echo "🔐 Configuring for GitHub Codespaces..."
    # Codespaces automatically configures Git with your GitHub credentials
    # The GH_TOKEN is already available in the environment

    # Set up Git to use GitHub CLI for authentication
    gh auth setup-git

    # Create SSH directory for any keys you might want to add
    mkdir -p /home/vscode/.ssh
    chmod 700 /home/vscode/.ssh

    echo "💡 Tip: Use 'gh cs ssh' from your local machine to SSH into this codespace"
else
    echo "🔧 Running in local devcontainer..."
    # For local devcontainer, set up SSH directory
    mkdir -p /home/vscode/.ssh
    chmod 700 /home/vscode/.ssh

    # If running locally and you want to copy SSH keys, you can do it here
    # Note: Be careful with SSH key management for security
fi

# ──────────────────────────────────────────────────────────────────────────────
# Create useful directories
# ──────────────────────────────────────────────────────────────────────────────
echo "📁 Creating standard directories..."
mkdir -p \
  "${USER_HOME}/projects" \
  "${USER_HOME}/notes" \
  "${USER_HOME}/.local/bin" \
  "${USER_HOME}/.config" \
  "${USER_HOME}/go/bin" \
  "${USER_HOME}/.cache/go-mod"


# ──────────────────────────────────────────────────────────────────────────────
# Final permission fixes
# ──────────────────────────────────────────────────────────────────────────────
echo "🔒 Final permission fixes..."
$SUDO chown -R vscode:vscode "${USER_HOME}" 2>/dev/null || true

# ──────────────────────────────────────────────────────────────────────────────
# Tier1: System-wide core PATH & HOME synthesis script (/etc/profile.d)
# Provides baseline tooling visibility (Go, user bins) for all login shells.
# Non-login bash coverage handled by Tier2 BASH_ENV variable (added in devcontainer.json).
# Idempotent and safe if sourced multiple times.
# ──────────────────────────────────────────────────────────────────────────────
CORE_PROFILED="/etc/profile.d/00-core-path.sh"
# Always (re)write core profile script using sudo when available (robust for non-root remoteUser)
{
  cat > "/tmp/00-core-path.sh" <<'EOF'
#!/bin/sh
# Core path/bootstrap (generated by post-create.sh). Idempotent.

# Synthesize HOME if missing (some automation may drop it)
if [ -z "${HOME:-}" ]; then
  h=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)
  [ -n "$h" ] && HOME="$h" && export HOME
fi

add_path() {
  d="$1"; [ -d "$d" ] || return 0
  case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH";; esac
}

# System Go toolchain
add_path /usr/local/go/bin

# User-level bins (only when HOME resolved)
if [ -n "${HOME:-}" ]; then
  add_path "$HOME/go/bin"
  add_path "$HOME/.local/bin"
fi

export PATH

# GOPATH (user scope if HOME available)
if [ -n "${HOME:-}" ]; then
  : "${GOPATH:=$HOME/go}"; export GOPATH
fi

EOF
  if [ -n "${SUDO:-}" ]; then
    $SUDO cp /tmp/00-core-path.sh "$CORE_PROFILED" && $SUDO chmod 644 "$CORE_PROFILED" || true
  else
    cp /tmp/00-core-path.sh "$CORE_PROFILED" && chmod 644 "$CORE_PROFILED" || true
  fi
  rm -f /tmp/00-core-path.sh 2>/dev/null || true
} || true

echo "✅ post-create complete!"

# Ensure default shell is zsh (idempotent)
if command -v zsh >/dev/null 2>&1; then
  CURRENT_SHELL=$(getent passwd "vscode" | cut -d: -f7 || echo "")
  if [ "$CURRENT_SHELL" != "$(command -v zsh)" ]; then
    echo "💤 Updating default login shell for vscode user to zsh"
    if command -v chsh >/dev/null 2>&1; then
      $SUDO chsh -s "$(command -v zsh)" vscode 2>/dev/null || echo "⚠️ chsh failed (may be restricted)"
    else
      echo "⚠️ chsh not available; cannot change login shell automatically"
    fi
  else
    echo "✅ vscode user's default shell already zsh"
  fi
fi
