# Minimal login shell environment for zsh
# Loaded before interactive ~/.zshrc; keep lean for non-interactive contexts.
# Purpose: ensure PATH + core language/toolchain variables available to
# scripts, cron, GUI-launched terminals, remote commands, etc.

# Guard against multiple sourcing
if [[ -n "${__ZPROFILE_LOADED:-}" ]]; then
  return 0
fi
__ZPROFILE_LOADED=1

# Locale / editor
export LANG=en_US.UTF-8
export EDITOR="${EDITOR:-code --wait}"
export PAGER="${PAGER:-less}"
export SYSTEMD_EDITOR="$EDITOR"

# Base PATH segments (prepend user-local first)
path_pre=(
  "$HOME/.local/bin"
  "$HOME/bin"
  "$HOME/.npm-global/bin"
)

# Language/tool specific additions (only if directories exist to avoid bloat)
[[ -d "$HOME/.cargo/bin" ]] && path_pre+=("$HOME/.cargo/bin")
[[ -d "$HOME/go/bin" ]] && path_pre+=("$HOME/go/bin")
[[ -d "$HOME/.local/share/pnpm" ]] && path_pre+=("$HOME/.local/share/pnpm")
[[ -d "/usr/local/go/bin" ]] && path_pre+=("/usr/local/go/bin")

# Compose PATH uniquely (avoid duplicates)
new_path=()
for p in "${path_pre[@]}" /usr/local/bin /usr/bin /bin /usr/sbin /sbin; do
  [[ -d "$p" ]] || continue
  if [[ ":$PATH:" != *":$p:"* ]]; then
    new_path+=("$p")
  fi
done
if [[ -n "$ZSH_VERSION" ]]; then
  # In zsh, arrays join with spaces; explicitly join with colons for PATH
  local IFS=:
  export PATH="${new_path[*]}"
else
  export PATH="${new_path[*]}"
fi

# Ensure /usr/local/go/bin present if go exists there but missing (defensive)
if [[ -x /usr/local/go/bin/go ]] && ! command -v go >/dev/null 2>&1; then
  case ":$PATH:" in
    *":/usr/local/go/bin:"*) ;;
    *) export PATH="/usr/local/go/bin:$PATH" ;;
  esac
fi

# Go environment
export GOPATH="${GOPATH:-$HOME/go}"
export GOMODCACHE="${GOMODCACHE:-$HOME/.cache/go-mod}"

# Python settings (match devcontainer expectations)
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export VIRTUAL_ENV_DISABLE_PROMPT=1
# UV cache dir if using uv
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"

# NPM noise reduction
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false

# Ripgrep config path (only set if file exists to avoid warnings)
if command -v rg >/dev/null 2>&1 && [[ -f "$HOME/.config/ripgrep/ripgreprc" ]]; then
  export RIPGREP_CONFIG_PATH="$HOME/.config/ripgrep/ripgreprc"
fi

# Starship prompt may be initialized only in interactive shells; just define cache dir here
export STARSHIP_CACHE="$HOME/.cache/starship"
mkdir -p "$STARSHIP_CACHE" 2>/dev/null || true

# Node Version Manager lightweight path injection: if a current symlink exists, expose it
for nvm_dir in "/usr/local/share/nvm" "$HOME/.nvm"; do
  if [[ -d "$nvm_dir" ]]; then
    export NVM_DIR="$nvm_dir"
    if [[ -d "$NVM_DIR/current/bin" ]]; then
      case ":$PATH:" in
        *":$NVM_DIR/current/bin:"*) ;;
        *) export PATH="$NVM_DIR/current/bin:$PATH" ;;
      esac
    elif [[ -L "$NVM_DIR/current" ]]; then
      resolved=$(readlink "$NVM_DIR/current")
      [[ -d "$resolved/bin" ]] && export PATH="$resolved/bin:$PATH"
    fi
    break
  fi
done

# PNPM home
[[ -d "$HOME/.local/share/pnpm" ]] && export PNPM_HOME="$HOME/.local/share/pnpm"

# Cargo env (only export if file exists; avoid sourcing cost here)
if [[ -f "$HOME/.cargo/env" ]]; then
  # For non-interactive shells we prefer not to "source" heavy scripts.
  # Instead just ensure ~/.cargo/bin is already in PATH (done above) and skip.
  :
fi

# GPG tty only meaningful for interactive terminals; skip if not a TTY
if [[ -t 1 ]] && command -v tty >/dev/null 2>&1; then
  GPG_TTY=$(tty 2>/dev/null) && export GPG_TTY
fi

# History file definition is handled in interactive ~/.zshrc; do not override here.

# Optionally load user overrides early (lightweight)
[[ -f "$HOME/.zprofile.local" ]] && source "$HOME/.zprofile.local"

# Minimal environment ready; interactive customizations happen in ~/.zshrc
