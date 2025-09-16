#!/bin/sh
# Shared base environment (POSIX). Idempotent.
# Provides consistent PATH + core language vars for bash/zsh (login & non-interactive via BASH_ENV).

# Guard
if [ -n "${__ENV_BASE_LOADED:-}" ]; then
  return 0
fi
__ENV_BASE_LOADED=1

DEFAULT_SYSTEM_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
[ -n "$PATH" ] || PATH="$DEFAULT_SYSTEM_PATH"

path_add() {
  d="$1"; [ -d "$d" ] || return 0
  case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH" ;; esac
}

# Go toolchains (system + user) – ensure available to all shells early
path_add "/usr/local/go/bin"
path_add "$HOME/go/bin"

# Prepend user-local/tool directories not guaranteed by system profile.d script
path_add "$HOME/.local/bin"
path_add "$HOME/bin"
path_add "$HOME/.npm-global/bin"
path_add "$HOME/.cargo/bin"
path_add "$HOME/.local/share/pnpm"
path_add "$HOME/.pulumi/bin"

# NVM (Node feature exposes /usr/local/share/nvm)
if [ -d "/usr/local/share/nvm" ]; then
  export NVM_DIR="/usr/local/share/nvm"
elif [ -d "$HOME/.nvm" ]; then
  export NVM_DIR="$HOME/.nvm"
fi

if [ -n "${NVM_DIR:-}" ]; then
  if [ -d "$NVM_DIR/current/bin" ]; then
    path_add "$NVM_DIR/current/bin"
  elif [ -L "$NVM_DIR/current" ]; then
    resolved="$(readlink "$NVM_DIR/current")"
    [ -d "$resolved/bin" ] && path_add "$resolved/bin"
  fi
fi

export PATH

# Core language/tool environment
export GOPATH="${GOPATH:-$HOME/go}"
export GOMODCACHE="${GOMODCACHE:-$HOME/.cache/go-mod}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export VIRTUAL_ENV_DISABLE_PROMPT=1
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export PNPM_HOME="${PNPM_HOME:-$HOME/.local/share/pnpm}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"
export PULUMI_SKIP_UPDATE_CHECK=1
export PULUMI_HOME="$HOME/.pulumi"
export ZSH="${ZSH:-$HOME/.oh-my-zsh}"

# Locale/editor (lightweight)
export LANG="${LANG:-en_US.UTF-8}"
export EDITOR="${EDITOR:-code --wait}"
export PAGER="${PAGER:-less}"

# Ripgrep config if present (silently skip otherwise)
if command -v rg >/dev/null 2>&1 && [ -f "$HOME/.config/ripgrep/ripgreprc" ]; then
  export RIPGREP_CONFIG_PATH="$HOME/.config/ripgrep/ripgreprc"
fi

# Ensure STARSHIP_CACHE dir variable (creation deferred to interactive shell to save cost)
export STARSHIP_CACHE="${STARSHIP_CACHE:-$HOME/.cache/starship}"

# Done
