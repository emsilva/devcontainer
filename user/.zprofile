# Minimal login shell environment for zsh
# Delegates core PATH and base environment setup to shared POSIX script
# at ~/.config/shell/env-base.sh so bash and other shells can reuse it.

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

# Source shared base environment (idempotent)
if [ -f "$HOME/.config/shell/env-base.sh" ]; then
  . "$HOME/.config/shell/env-base.sh"
fi

# (Most core environment now set in env-base.sh)

# Create starship cache dir lazily (avoid failing if readonly)
[ -d "$STARSHIP_CACHE" ] || mkdir -p "$STARSHIP_CACHE" 2>/dev/null || true

# Lightweight NVM current symlink exposure (retained for login shells)
for nvm_dir in "/usr/local/share/nvm" "$HOME/.nvm"; do
  if [ -d "$nvm_dir" ]; then
    export NVM_DIR="$nvm_dir"
    if [ -d "$NVM_DIR/current/bin" ]; then
      case ":$PATH:" in *":$NVM_DIR/current/bin:"*) ;; *) PATH="$NVM_DIR/current/bin:$PATH"; export PATH;; esac
    elif [ -L "$NVM_DIR/current" ]; then
      resolved=$(readlink "$NVM_DIR/current")
      [ -d "$resolved/bin" ] && PATH="$resolved/bin:$PATH" && export PATH
    fi
    break
  fi
done

# PNPM home (already in PATH via env-base if directory exists; only set var)
[ -d "$HOME/.local/share/pnpm" ] && export PNPM_HOME="$HOME/.local/share/pnpm"

# Skip sourcing heavy cargo env here (handled interactively in .zshrc)

# GPG tty only meaningful for interactive terminals; skip if not a TTY
if [[ -t 1 ]] && command -v tty >/dev/null 2>&1; then
  GPG_TTY=$(tty 2>/dev/null) && export GPG_TTY
fi

# History file definition is handled in interactive ~/.zshrc; do not override here.

# Optionally load user overrides early (lightweight)
[[ -f "$HOME/.zprofile.local" ]] && source "$HOME/.zprofile.local"

# Minimal environment ready; interactive customizations happen in ~/.zshrc
