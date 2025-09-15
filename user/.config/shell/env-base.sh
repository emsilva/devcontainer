#!/bin/sh
# Shared base environment (POSIX). Idempotent.
# Provides consistent PATH + core language vars for bash/zsh (login & non-interactive via BASH_ENV).

# Guard
[ -n "${__ENV_BASE_LOADED:-}" ] && return 0 || true
__ENV_BASE_LOADED=1

DEFAULT_SYSTEM_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
[ -n "$PATH" ] || PATH="$DEFAULT_SYSTEM_PATH"

path_add() {
  d="$1"; [ -d "$d" ] || return 0
  case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH" ;; esac
}

# Prepend user-local/tool directories not guaranteed by system profile.d script
path_add "$HOME/.local/bin"
path_add "$HOME/bin"
path_add "$HOME/.npm-global/bin"
path_add "$HOME/.cargo/bin"
path_add "$HOME/.local/share/pnpm"

export PATH

# Core language/tool environment
export GOPATH="${GOPATH:-$HOME/go}"
export GOMODCACHE="${GOMODCACHE:-$HOME/.cache/go-mod}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export VIRTUAL_ENV_DISABLE_PROMPT=1
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"

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

# Provide BASH_ENV so non-interactive bash gains (at least) GOPATH/PATH if spawned fresh
export BASH_ENV="$HOME/.config/shell/noninteractive-env.sh"

# Done
