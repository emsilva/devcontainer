#!/usr/bin/env bash
set -euo pipefail

echo "🏗️  on-create: build-time provisioning (prebuild-cacheable)"

# Variables
USER_HOME="${HOME:-/home/vscode}"
SUDO=""
command -v sudo >/dev/null 2>&1 && SUDO="sudo"

# The language/CLI features publish their bins via containerEnv, but prepend the common
# toolchain dirs explicitly so on-create is self-sufficient for the installs below.
export PATH="/usr/local/go/bin:/usr/local/cargo/bin:${USER_HOME}/.local/bin:${USER_HOME}/go/bin:${PATH}"

ensure_user_dir() {
  dir="$1"
  mkdir -p "$dir" 2>/dev/null || true
}

# Take ownership of a dir that may be a root-owned named-volume mount (or a
# Docker-created mount parent), so the user can write to it during on-create.
ensure_owned() {
  dir="$1"
  if [ -n "$SUDO" ]; then
    $SUDO mkdir -p "$dir" 2>/dev/null || true
    $SUDO chown "$(id -u):$(id -g)" "$dir" 2>/dev/null || true
  else
    mkdir -p "$dir" 2>/dev/null || true
  fi
}

restore_man_pages_if_needed() {
  local sentinel="${USER_HOME}/.config/.manpages-restored"
  # Opt-in only: this runs apt-get update + install + unminimize, which adds minutes
  # to a fresh build/prebuild. Enable by setting DEVCONTAINER_RESTORE_MANPAGES=1.
  [ "${DEVCONTAINER_RESTORE_MANPAGES:-0}" = "1" ] || return 0
  if ! command -v unminimize >/dev/null 2>&1 && [ ! -x /usr/local/sbin/unminimize ]; then
    return
  fi
  if [ -f /usr/share/man/man1/ls.1.gz ] || [ -f "$sentinel" ]; then
    return
  fi
  echo "📚 Restoring man pages (unminimize)"
  if [ -n "$SUDO" ]; then
    if ! $SUDO apt-get update; then
      echo "  ⚠ Failed to refresh apt cache; skipping man page restore" >&2
      return
    fi
    if ! DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y man-db manpages manpages-dev; then
      echo "  ⚠ Failed to install man packages; skipping unminimize" >&2
      return
    fi
    if ! yes | $SUDO unminimize; then
      echo "  ⚠ unminimize failed; run manually if you need man pages" >&2
      return
    fi
  else
    if ! apt-get update; then
      echo "  ⚠ Failed to refresh apt cache; skipping man page restore" >&2
      return
    fi
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y man-db manpages manpages-dev; then
      echo "  ⚠ Failed to install man packages; skipping unminimize" >&2
      return
    fi
    if ! yes | unminimize; then
      echo "  ⚠ unminimize failed; run manually if you need man pages" >&2
      return
    fi
  fi
  ensure_user_dir "${USER_HOME}/.config"
  if ! printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$sentinel"; then
    echo "  ⚠ Failed to write sentinel file $sentinel" >&2
  fi
}

# Named-volume cache mounts (e.g. ~/.cache/go-mod) and the Docker-created mount parent
# (~/.cache) are root-owned at on-create time; post-create fixes them later, but the
# installs below (go build/mod cache, gem cache) need them writable NOW.
ensure_owned "${USER_HOME}/.cache"
ensure_owned "${USER_HOME}/.cache/go-mod"
ensure_owned "${USER_HOME}/.local"
ensure_user_dir "${USER_HOME}/.config"
ensure_user_dir "${USER_HOME}/.local/bin"

# The base image ships ~/.oh-my-zsh plus a stock ~/.zshrc that sources it; this
# template uses antidote (via the user's chezmoi dotfiles) instead. Drop the
# unused install, and swap the stock .zshrc — broken once oh-my-zsh is gone —
# for a minimal default (chezmoi dotfiles overwrite it when applied).
rm -rf "${USER_HOME}/.oh-my-zsh"
if [ ! -f "${USER_HOME}/.zshrc" ] || grep -q "oh-my-zsh" "${USER_HOME}/.zshrc"; then
  cat >"${USER_HOME}/.zshrc" <<'ZSHRC'
# Minimal template default — replaced by your chezmoi dotfiles when applied
# (set DOTFILES_REPO or run `load-dotfiles <repo>`).
HISTSIZE=10000
SAVEHIST=10000
: "${HISTFILE:=$HOME/.zsh_history}"
setopt inc_append_history hist_ignore_dups

command -v starship >/dev/null 2>&1 && eval "$(starship init zsh)"

[ -r /usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh ] &&
  source /usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh
[ -r /usr/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh ] &&
  source /usr/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh
ZSHRC
fi

restore_man_pages_if_needed

# Enable corepack for pnpm/yarn if available
if command -v corepack >/dev/null 2>&1; then
  corepack enable >/dev/null 2>&1 || true
fi

# Install crane (OCI utility) via Go if missing
if ! command -v crane >/dev/null 2>&1; then
  if command -v go >/dev/null 2>&1; then
    echo "🪝 Installing crane (go-containerregistry)"
    if GO111MODULE=on GOBIN="${USER_HOME}/.local/bin" go install github.com/google/go-containerregistry/cmd/crane@v0.21.6; then
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
    if cargo install vivid --version "^0.10" --locked; then
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

# Ruby is provided by the devcontainers ruby feature (pinned to 3.4 in devcontainer.json),
# which manages Ruby via RVM under /usr/local/rvm. Source RVM so gem/rails resolve against
# that Ruby instead of a second, separately-compiled rbenv install (which previously froze
# Ruby at 3.4.5 and installed Rails into the wrong interpreter).
if [ -s /usr/local/rvm/scripts/rvm ]; then
  # RVM's script is not written for `set -euo pipefail`; relax while sourcing.
  set +eu
  # shellcheck disable=SC1091
  . /usr/local/rvm/scripts/rvm >/dev/null 2>&1 || true
  set -eu
fi

# Install a pinned Rails into the feature's Ruby (patch floats within 8.1)
if command -v gem >/dev/null 2>&1; then
  rails_requirement="~> 8.1.0"
  if ! command -v rails >/dev/null 2>&1; then
    echo "🚂 Installing Rails (${rails_requirement})"
    if gem install rails -v "${rails_requirement}" --no-document; then
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

# Install chezmoi (dotfiles manager) — pinned release binary
# (chezmoi's vanity import path is not `go install`-friendly, so use the release tarball.)
if ! command -v chezmoi >/dev/null 2>&1; then
  chezmoi_version="2.70.5"
  case "$(uname -m)" in
    x86_64 | amd64) chezmoi_arch="amd64" ;;
    aarch64 | arm64) chezmoi_arch="arm64" ;;
    *) chezmoi_arch="" ;;
  esac
  if [ -n "$chezmoi_arch" ]; then
    echo "🏠 Installing chezmoi ${chezmoi_version}"
    chezmoi_url="https://github.com/twpayne/chezmoi/releases/download/v${chezmoi_version}/chezmoi_${chezmoi_version}_linux_${chezmoi_arch}.tar.gz"
    tmp_cz="$(mktemp -d)"
    if curl -fsSL "$chezmoi_url" -o "$tmp_cz/chezmoi.tar.gz" && tar -C "$tmp_cz" -xzf "$tmp_cz/chezmoi.tar.gz" chezmoi; then
      if [ -n "${SUDO}" ]; then
        $SUDO install -m 0755 "$tmp_cz/chezmoi" /usr/local/bin/chezmoi
      else
        install -m 0755 "$tmp_cz/chezmoi" /usr/local/bin/chezmoi
      fi
    else
      echo "  ⚠ Failed to install chezmoi" >&2
    fi
    rm -rf "$tmp_cz"
  else
    echo "  ⚠ Unsupported arch for chezmoi install: $(uname -m)" >&2
  fi
else
  echo "🏠 chezmoi already available"
fi

# Install neovim (pinned static build) for the LazyVim dotfiles
if ! command -v nvim >/dev/null 2>&1; then
  nvim_version="v0.12.2"
  case "$(uname -m)" in
    x86_64 | amd64) nvim_arch="x86_64" ;;
    aarch64 | arm64) nvim_arch="arm64" ;;
    *) nvim_arch="" ;;
  esac
  if [ -n "$nvim_arch" ]; then
    echo "📝 Installing neovim ${nvim_version}"
    nvim_url="https://github.com/neovim/neovim/releases/download/${nvim_version}/nvim-linux-${nvim_arch}.tar.gz"
    if curl -fsSL "$nvim_url" -o /tmp/nvim.tar.gz; then
      if [ -n "${SUDO}" ]; then
        $SUDO tar -C /opt -xzf /tmp/nvim.tar.gz && $SUDO ln -sfn "/opt/nvim-linux-${nvim_arch}/bin/nvim" /usr/local/bin/nvim
      else
        tar -C /opt -xzf /tmp/nvim.tar.gz && ln -sfn "/opt/nvim-linux-${nvim_arch}/bin/nvim" /usr/local/bin/nvim
      fi
      rm -f /tmp/nvim.tar.gz
    else
      echo "  ⚠ Failed to download neovim ${nvim_version}" >&2
    fi
  else
    echo "  ⚠ Unsupported arch for neovim install: $(uname -m)" >&2
  fi
else
  echo "📝 neovim already available"
fi

# Install ast-grep (structural code search; not packaged in noble) via npm, pinned
if ! command -v ast-grep >/dev/null 2>&1; then
  if command -v npm >/dev/null 2>&1; then
    echo "🔍 Installing ast-grep (@ast-grep/cli)"
    if npm install -g --no-fund --no-audit @ast-grep/cli@0.44.1; then
      echo "  ✅ ast-grep installed"
    else
      echo "  ⚠ Failed to install ast-grep" >&2
    fi
  else
    echo "  ⚠ npm not found; skipping ast-grep install" >&2
  fi
else
  echo "🔍 ast-grep already available"
fi

# Install herdr (agent-herd orchestrator) via its installer (lands in ~/.local/bin)
if ! command -v herdr >/dev/null 2>&1; then
  echo "🐑 Installing herdr"
  if curl -fsSL https://herdr.dev/install.sh | sh; then
    echo "  ✅ herdr installed"
  else
    echo "  ⚠ Failed to install herdr" >&2
  fi
else
  echo "🐑 herdr already available"
fi

# Install claude-swap (Claude Code multi-account rotation) as a uv tool, pinned.
# The cswap-switch background service in post-start.sh depends on this.
if ! command -v cswap >/dev/null 2>&1 && [ ! -x "${USER_HOME}/.local/bin/cswap" ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "🔁 Installing claude-swap 0.19.0"
    if uv tool install claude-swap==0.19.0; then
      echo "  ✅ claude-swap installed"
    else
      echo "  ⚠ Failed to install claude-swap" >&2
    fi
  else
    echo "  ⚠ uv not found; skipping claude-swap install" >&2
  fi
else
  echo "🔁 claude-swap already available"
fi

# Install the `load-dotfiles` convenience command (same logic as `task setup:dotfiles`)
if [ -f .devcontainer/scripts/setup-dotfiles.sh ]; then
  if [ -n "${SUDO}" ]; then
    $SUDO install -m 0755 .devcontainer/scripts/setup-dotfiles.sh /usr/local/bin/load-dotfiles 2>/dev/null || true
  else
    install -m 0755 .devcontainer/scripts/setup-dotfiles.sh /usr/local/bin/load-dotfiles 2>/dev/null || true
  fi
fi

echo "✅ on-create complete"
