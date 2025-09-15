# ──────────────────────────────────────────────────────────────────────────────
# PERFORMANCE: Optional profiling (run with ZSH_PROFILE=1 zsh -i -c exit)
# ──────────────────────────────────────────────────────────────────────────────
[[ -n "$ZSH_PROFILE" ]] && zmodload zsh/zprof

# ──────────────────────────────────────────────────────────────────────────────
# PERFORMANCE: Skip system compinit to avoid double initialization
# ──────────────────────────────────────────────────────────────────────────────
skip_global_compinit=1

# ──────────────────────────────────────────────────────────────────────────────
# PERFORMANCE: Command lookup cache
# ──────────────────────────────────────────────────────────────────────────────
typeset -gA _CMD_CACHE
_has_cmd() {
  if [[ -z "${_CMD_CACHE[$1]}" ]]; then
    command -v "$1" &>/dev/null && _CMD_CACHE[$1]=1 || _CMD_CACHE[$1]=0
  fi
  return $(( 1 - ${_CMD_CACHE[$1]} ))
}

# ──────────────────────────────────────────────────────────────────────────────
# PERFORMANCE: Cache expensive operations
# ──────────────────────────────────────────────────────────────────────────────
ZSH_CACHE_DIR="${HOME}/.cache/zsh"
mkdir -p "$ZSH_CACHE_DIR"

# Helper to cache command output
_cache_cmd() {
  local cache_file="$ZSH_CACHE_DIR/$1"
  local max_age="${2:-86400}"  # Default 24 hours
  shift 2

  if [[ ! -f "$cache_file" ]] || [[ -n "$(find "$cache_file" -mmin +$((max_age/60)) 2>/dev/null)" ]]; then
    "$@" > "$cache_file" 2>/dev/null
  fi
  cat "$cache_file" 2>/dev/null
}

# ──────────────────────────────────────────────────────────────────────────────
# 1) PATH & ENVIRONMENT
# ──────────────────────────────────────────────────────────────────────────────
# Core PATH and base vars are centralized in user/.config/shell/env-base.sh (sourced
# via ~/.zprofile and ~/.profile). This file only layers interactive/tool specifics.

# Ruby gems bin (cached)
if _has_cmd ruby && _has_cmd gem; then
  GEM_PATH=$(_cache_cmd "ruby_gem_path" 604800 ruby -e 'print Gem.user_dir')  # Cache 7 days
  [[ -n "$GEM_PATH" ]] && export PATH="$GEM_PATH/bin:$PATH"
fi

# Go path (always prepare GOPATH; /usr/local/go/bin already in PATH above)
export GOPATH="${GOPATH:-$HOME/go}"
export GOMODCACHE="${GOMODCACHE:-$HOME/.cache/go-mod}"
mkdir -p "$GOPATH/bin" "$GOMODCACHE" 2>/dev/null || true
export PATH="$GOPATH/bin:$PATH"

# Rust/Cargo
[[ -f "$HOME/.cargo/env" ]] && source "$HOME/.cargo/env"
[[ -d "$HOME/.cargo/bin" ]] && export PATH="$HOME/.cargo/bin:$PATH"

# Node Version Manager (nvm) - Lazy loading to improve startup time
for nvm_dir in "/usr/local/share/nvm" "$HOME/.nvm"; do
  if [[ -d "$nvm_dir" ]]; then
    export NVM_DIR="$nvm_dir"
    # Lazy load NVM - only source when nvm command is used
    nvm() {
      unset -f nvm
      [[ -s "$NVM_DIR/nvm.sh" ]] && source "$NVM_DIR/nvm.sh"
      [[ -s "$NVM_DIR/bash_completion" ]] && source "$NVM_DIR/bash_completion"
      nvm "$@"
    }
    break
  fi
done

# Add current NVM node version to PATH (if exists)
# This makes node/npm/npx available immediately without loading full NVM
if [[ -d "$NVM_DIR/current/bin" ]]; then
  export PATH="$NVM_DIR/current/bin:$PATH"
elif [[ -L "$NVM_DIR/current" ]]; then
  # Follow symlink to actual version
  NVM_CURRENT=$(readlink "$NVM_DIR/current")
  [[ -d "$NVM_CURRENT/bin" ]] && export PATH="$NVM_CURRENT/bin:$PATH"
fi

# PNPM
export PNPM_HOME="$HOME/.local/share/pnpm"
[[ -d "$PNPM_HOME" ]] && export PATH="$PNPM_HOME:$PATH"

# UV (Python package manager) - installed by post-create
[[ -f "$HOME/.local/bin/uv" ]] && export PATH="$HOME/.local/bin:$PATH"
[[ -f "$HOME/.cargo/bin/uv" ]] && export PATH="$HOME/.cargo/bin:$PATH"

# Claude Code CLI
[[ -f "$HOME/.local/bin/claude" ]] && export PATH="$HOME/.local/bin:$PATH"

# Core environment variables
export ZSH="$HOME/.oh-my-zsh"
export LANG=en_US.UTF-8
export EDITOR="${EDITOR:-code --wait}"
export PAGER="${PAGER:-less}"
export SYSTEMD_EDITOR="${EDITOR}"

# GPG TTY for git signing
export GPG_TTY=$(tty)

# Ripgrep configuration
_has_cmd rg && export RIPGREP_CONFIG_PATH="$HOME/.config/ripgrep/ripgreprc"

# Fix Starship permissions
export STARSHIP_CACHE="$HOME/.cache/starship"
[[ ! -d "$STARSHIP_CACHE" ]] && mkdir -p "$STARSHIP_CACHE"

# Python settings (from devcontainer.json)
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export VIRTUAL_ENV_DISABLE_PROMPT=1

# NPM settings (from devcontainer.json)
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false

# UV Cache (from devcontainer mounts)
export UV_CACHE_DIR="$HOME/.cache/uv"

# ──────────────────────────────────────────────────────────────────────────────
# 2) HISTORY - CRITICAL FOR DEVCONTAINER PERSISTENCE
# ──────────────────────────────────────────────────────────────────────────────
# Use the mounted volume for history persistence
if [[ -d "/commandhistory" ]]; then
  export HISTFILE="/commandhistory/.zsh_history"
  # Ensure the file exists and has proper permissions
  [[ ! -f "$HISTFILE" ]] && touch "$HISTFILE"
else
  # Fallback if not in devcontainer
  export HISTFILE="$HOME/.zsh_history"
fi

export HISTSIZE=10000
export SAVEHIST=10000

setopt EXTENDED_HISTORY          # Write the history file in the ':start:elapsed;command' format
setopt HIST_IGNORE_ALL_DUPS      # Delete an old recorded event if a new event is a duplicate
setopt HIST_FIND_NO_DUPS         # Do not display a previously found event
setopt INC_APPEND_HISTORY        # Write to the history file immediately, not when the shell exits
setopt SHARE_HISTORY             # Share history between all sessions
setopt HIST_VERIFY               # Do not execute immediately upon history expansion
setopt HIST_IGNORE_SPACE         # Do not record an event starting with a space
setopt HIST_REDUCE_BLANKS        # Remove superfluous blanks from each command line

# ──────────────────────────────────────────────────────────────────────────────
# 3) SHELL OPTIONS
# ──────────────────────────────────────────────────────────────────────────────
setopt AUTO_CD                   # If command is a directory, cd into it
setopt CORRECT                   # Try to correct the spelling of commands
setopt PROMPT_SUBST              # Enable parameter expansion, command substitution, etc. in prompts
setopt NO_BEEP                   # Don't beep on errors
setopt AUTO_PUSHD                # Make cd push the old directory onto the directory stack
setopt PUSHD_IGNORE_DUPS         # Don't push multiple copies of the same directory
setopt INTERACTIVE_COMMENTS      # Allow comments in interactive shell
setopt COMPLETE_IN_WORD          # Complete from both ends of a word

# ──────────────────────────────────────────────────────────────────────────────
# 4) OH-MY-ZSH & PLUGINS
# ──────────────────────────────────────────────────────────────────────────────
# Set ZOXIDE_CMD_OVERRIDE before oh-my-zsh loads the plugin
export ZOXIDE_CMD_OVERRIDE="z"

# Base plugins
plugins=(
  git
  history-substring-search
  sudo
  extract
  command-not-found
)

# Add conditional plugins based on available commands (reduced for performance)
_has_cmd npm && plugins+=(npm)
_has_cmd yarn && plugins+=(yarn)
_has_cmd docker && plugins+=(docker)
# Removed heavy plugins: kubectl, terraform, aws, docker-compose, golang, python, pip
_has_cmd gh && plugins+=(gh)
# _has_cmd fzf && plugins+=(fzf)  # Disabled - using git-installed fzf
_has_cmd zoxide && plugins+=(zoxide)

# Add custom plugins if available (installed by post-create)
[[ -d "$ZSH/custom/plugins/zsh-autosuggestions" ]] && plugins+=(zsh-autosuggestions)
[[ -d "$ZSH/custom/plugins/zsh-syntax-highlighting" ]] && plugins+=(zsh-syntax-highlighting)

ZSH_THEME=""  # Using starship instead
source $ZSH/oh-my-zsh.sh

# ──────────────────────────────────────────────────────────────────────────────
# 5) EXTERNAL PLUGIN LOADING (for system-installed plugins)
# ──────────────────────────────────────────────────────────────────────────────
# Load system-installed zsh plugins if oh-my-zsh versions aren't available
if [[ ! -d "$ZSH/custom/plugins/zsh-syntax-highlighting" ]]; then
  for plugin_path in \
    "/usr/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh" \
    "/usr/share/zsh/plugins/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
  do
    [[ -f "$plugin_path" ]] && source "$plugin_path" && break
  done
fi

if [[ ! -d "$ZSH/custom/plugins/zsh-autosuggestions" ]]; then
  for plugin_path in \
    "/usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh" \
    "/usr/share/zsh/plugins/zsh-autosuggestions/zsh-autosuggestions.zsh"
  do
    [[ -f "$plugin_path" ]] && source "$plugin_path" && break
  done
fi

# ──────────────────────────────────────────────────────────────────────────────
# 6) COLORS & PROMPT
# ──────────────────────────────────────────────────────────────────────────────
# Starship prompt (cached init)
if _has_cmd starship && ! echo "$PROMPT" | grep -q starship; then
  STARSHIP_INIT=$(_cache_cmd "starship_init" 86400 starship init zsh --print-full-init)  # Cache 1 day
  [[ -n "$STARSHIP_INIT" ]] && eval "$STARSHIP_INIT"
fi

# Better LS_COLORS handling with cache
if _has_cmd vivid; then
  LS_COLORS_CACHED=$(_cache_cmd "ls_colors_vivid" 604800 vivid generate catppuccin-macchiato)  # Cache 7 days
  if [[ -n "$LS_COLORS_CACHED" ]]; then
    export LS_COLORS="$LS_COLORS_CACHED"
  else
    export LS_COLORS="rs=0:di=01;34:ln=01;36:mh=00:pi=40;33:so=01;35:do=01;35:bd=40;33;01:cd=40;33;01:or=40;31;01:mi=00:su=37;41:sg=30;43:ca=30;41:tw=30;42:ow=34;42:st=37;44:ex=01;32"
  fi
else
  export LS_COLORS="rs=0:di=01;34:ln=01;36:mh=00:pi=40;33:so=01;35:do=01;35:bd=40;33;01:cd=40;33;01:or=40;31;01:mi=00:su=37;41:sg=30;43:ca=30;41:tw=30;42:ow=34;42:st=37;44:ex=01;32"
fi

# ──────────────────────────────────────────────────────────────────────────────
# 7) FZF CONFIGURATION (Git Installation)
# ──────────────────────────────────────────────────────────────────────────────
if [ -d "$HOME/.fzf" ]; then
  # Add fzf to PATH
  export PATH="$HOME/.fzf/bin:$PATH"

  # Source shell integration files
  [[ -f "$HOME/.fzf/shell/completion.zsh" ]] && source "$HOME/.fzf/shell/completion.zsh"
  [[ -f "$HOME/.fzf/shell/key-bindings.zsh" ]] && source "$HOME/.fzf/shell/key-bindings.zsh"

  # FZF settings
  export FZF_DEFAULT_OPTS="--height 40% --layout=reverse --border --preview-window=right:60%"

  # Use fd if available (installed as fdfind in Ubuntu)
  if _has_cmd fd || _has_cmd fdfind; then
    FD_CMD=$(command -v fd || command -v fdfind)
    export FZF_DEFAULT_COMMAND="$FD_CMD --type f --hidden --follow --exclude .git"
    export FZF_CTRL_T_COMMAND="$FZF_DEFAULT_COMMAND"
    export FZF_ALT_C_COMMAND="$FD_CMD --type d --hidden --follow --exclude .git"
  elif _has_cmd rg; then
    export FZF_DEFAULT_COMMAND='rg --files --hidden --follow --glob "!.git/*"'
    export FZF_CTRL_T_COMMAND="$FZF_DEFAULT_COMMAND"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# 8) ALIASES
# ──────────────────────────────────────────────────────────────────────────────
# Better defaults
alias ls='ls --color=auto'
alias ll='ls -alF'
alias la='ls -A'
alias l='ls -CF'
alias grep='grep --color=auto'
alias fgrep='fgrep --color=auto'
alias egrep='egrep --color=auto'

# Safety nets
alias rm='rm -i'
alias cp='cp -i'
alias mv='mv -i'

# Navigation shortcuts
alias ..='cd ..'
alias ...='cd ../..'
alias ....='cd ../../..'
alias h='history'
alias j='jobs -l'

# Use z for zoxide navigation
_has_cmd zoxide && alias cd='z'

# System info
alias df='df -h'
alias du='du -h'
alias free='free -h'

# Git shortcuts
alias g='git status'
# Remove any gg alias from oh-my-zsh git plugin to avoid function definition errors
unalias gg 2>/dev/null
gg() {
  if [[ $# -eq 0 ]]; then
    echo "Usage: gg <commit message>"
    return 1
  fi
  # Guard: detect if there is anything to commit (tracked or untracked)
  if git diff --quiet && git diff --cached --quiet; then
    # Check untracked files
    if [[ -z "$(git ls-files --others --exclude-standard | head -n1)" ]]; then
      echo "No changes to commit"
      return 0
    fi
  fi
  git add -A
  git commit -m "$*"
}
alias ga='git add'
alias gc='git commit'
alias gp='git push'
alias gl='git log --oneline --graph --decorate'
alias gd='git diff'
alias gco='git checkout'
alias gb='git branch'
alias gpr='git pull --rebase'
alias gf='git fetch --all --prune'

# Codex shortcut (bypass approvals/sandbox explicitly)
alias codex='codex --dangerously-bypass-approvals-and-sandbox'
alias claude='claude --dangerously-skip-permissions'

# Modern tool replacements (handle Ubuntu naming)
if _has_cmd bat; then
  alias cat='bat --style=plain --paging=never'
elif _has_cmd batcat; then
  alias cat='batcat --style=plain --paging=never'
  alias bat='batcat --paging=never'
fi

if _has_cmd fdfind && ! _has_cmd fd; then
  alias fd='fdfind'
fi

# Docker shortcuts
if _has_cmd docker; then
  alias dps='docker ps'
  alias dpsa='docker ps -a'
  alias dimg='docker images'
  alias dexec='docker exec -it'
  alias dlogs='docker logs -f'
  alias dprune='docker system prune -a'
  alias dcup='docker compose up -d'
  alias dcdown='docker compose down'
  alias dclogs='docker compose logs -f'
fi

# Python shortcuts
alias py='python3'
alias pip='pip3'
alias vact='source .venv/bin/activate || source venv/bin/activate'
alias vdeact='deactivate'

# Node/NPM shortcuts
alias nr='npm run'
alias ni='npm install'
alias nid='npm install --save-dev'
alias nig='npm install -g'
alias nt='npm test'
alias nb='npm run build'
alias nd='npm run dev'

# Devcontainer specific
alias rebuild='devcontainer rebuild --no-cache'
alias reopen='devcontainer reopen'

# ──────────────────────────────────────────────────────────────────────────────
# 9) FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────
# Create directory and cd into it
mkcd() {
  mkdir -p "$1" && cd "$1"
}

# Extract various archive types
extract() {
  if [ -f "$1" ]; then
    case "$1" in
      *.tar.bz2)   tar xjf "$1"    ;;
      *.tar.gz)    tar xzf "$1"    ;;
      *.tar.xz)    tar xJf "$1"    ;;
      *.bz2)       bunzip2 "$1"    ;;
      *.rar)       unrar x "$1"    ;;
      *.gz)        gunzip "$1"     ;;
      *.tar)       tar xf "$1"     ;;
      *.tbz2)      tar xjf "$1"    ;;
      *.tgz)       tar xzf "$1"    ;;
      *.zip)       unzip "$1"      ;;
      *.Z)         uncompress "$1" ;;
      *.7z)        7z x "$1"       ;;
      *.deb)       ar x "$1"       ;;
      *.tar.zst)   tar --use-compress-program=unzstd -xf "$1" ;;
      *)           echo "'$1' cannot be extracted via extract()" ;;
    esac
  else
    echo "'$1' is not a valid file"
  fi
}

# Find files/directories by name
ff() {
  find . -type f -iname "*$1*" 2>/dev/null
}

fdir() {
  find . -type d -iname "*$1*" 2>/dev/null
}

# Quick backup
backup() {
  cp "$1" "$1.backup-$(date +%Y%m%d-%H%M%S)"
}

# Tree view
tree() {
  if _has_cmd tree; then
    command tree "$@"
  else
    find . -print | sed -e 's;[^/]*/;|____;g;s;____|; |;g'
  fi
}

# Quick notes
note() {
  local notes_dir="$HOME/notes"
  mkdir -p "$notes_dir"
  if [[ $# -eq 0 ]]; then
    ls -la "$notes_dir"
  else
    local note_file="$notes_dir/$(date +%Y-%m-%d)_$1.md"
    ${EDITOR:-vim} "$note_file"
  fi
}

# Node/NPM helpers
node_modules_size() {
  find . -name "node_modules" -type d -prune | xargs du -sh | sort -h
}

clean_node_modules() {
  find . -name "node_modules" -type d -prune -exec rm -rf '{}' +
  echo "All node_modules directories have been removed"
}

# Python virtual environment helpers
venv() {
  if [[ $# -eq 0 ]]; then
    # Activate existing venv
    if [[ -f .venv/bin/activate ]]; then
      source .venv/bin/activate
      echo "Activated .venv"
    elif [[ -f venv/bin/activate ]]; then
      source venv/bin/activate
      echo "Activated venv"
    else
      echo "No virtual environment found. Create one with: venv create"
    fi
  elif [[ "$1" == "create" ]]; then
    # Create new venv
    if _has_cmd uv; then
      echo "Creating venv with uv..."
      uv venv
      source .venv/bin/activate
      uv pip install --upgrade pip
    else
      echo "Creating venv with python..."
      python3 -m venv .venv
      source .venv/bin/activate
      pip install --upgrade pip
    fi
    echo "Virtual environment created and activated"
  elif [[ "$1" == "delete" ]]; then
    # Delete venv
    deactivate 2>/dev/null
    rm -rf .venv venv
    echo "Virtual environment deleted"
  else
    echo "Usage: venv [create|delete]"
  fi
}

# UV Python package manager helpers
if _has_cmd uv; then
  alias uvs='uv sync'
  alias uva='uv add'
  alias uvr='uv run'
  alias uvp='uv pip'
fi

# Devcontainer helpers
dc-rebuild() {
  echo "Rebuilding devcontainer..."
  devcontainer rebuild --no-cache --workspace-folder .
}

dc-logs() {
  docker logs -f $(docker ps -q --filter "label=devcontainer.local_folder=$PWD")
}

# Port forwarding helper
forward() {
  if [[ $# -eq 1 ]]; then
    echo "Forwarding port $1..."
    gh codespace ports forward "$1:$1"
  elif [[ $# -eq 2 ]]; then
    echo "Forwarding port $1 to $2..."
    gh codespace ports forward "$1:$2"
  else
    echo "Usage: forward <port> [local_port]"
  fi
}

# Update all git submodules to their remote tracking branches.
# Usage: gsubsync [-n|--dry-run] [--merge|--rebase] [--init]
# -n / --dry-run : Show what would be updated without performing fetch/reset
# --merge        : Merge upstream changes instead of fast-forward (default fast-forward)
# --rebase       : Rebase local commits (mutually exclusive with --merge)
# --init         : Ensure submodules are initialized
gsubsync() {
  local dry_run=0 mode="ff" do_init=0 branch="" remote="origin"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n|--dry-run) dry_run=1; shift ;;
      --merge) mode="merge"; shift ;;
      --rebase) mode="rebase"; shift ;;
  -b|--branch) branch="$2"; shift 2 ;;
  -R|--remote) remote="$2"; shift 2 ;;
      --init) do_init=1; shift ;;
      -h|--help)
        cat <<'EOF'
Usage: gsubsync [options]
Update each git submodule to the latest remote commit.

Options:
  -n, --dry-run    Show planned actions only
  --merge          Merge upstream changes (default: fast-forward/reset)
  --rebase         Rebase local commits
  -b, --branch B   Force branch B for all submodules
  --init           Ensure submodules are initialized
  -h, --help       Show this help
EOF
        return 0 ;;
      *) echo "Unknown option: $1" >&2; return 2 ;;
    esac
  done

  [[ -f .gitmodules ]] || { echo "No .gitmodules file found" >&2; return 1; }
  command -v git >/dev/null || { echo "git not available" >&2; return 1; }

  (( do_init )) && git submodule update --init --recursive || true

  local -a subpaths=()
  while IFS= read -r p; do [[ -n "$p" ]] && subpaths+=("$p"); done \
    < <(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $3}')
  (( ${#subpaths[@]} )) || { echo "No submodules defined"; return 0; }

  local root; root="$(pwd)"

  for path in "${subpaths[@]}"; do
    if [[ ! -e "$path/.git" && ! -f "$path/.git" ]]; then
      echo "Skipping $path (not initialized)"
      continue
    fi

  local target_branch=""
    if [[ -n "$branch" ]]; then
      target_branch="$branch"
    else
      local name b upstream symref
      name="$(git config -f .gitmodules --get-regexp "^submodule\\..*\\.path$" | awk -v p="$path" '$3==p {print $1}' | sed -E 's/^submodule\\.|\\.path$//g')"
      b="$(git config -f .gitmodules --get "submodule.$name.branch" 2>/dev/null || true)"
      if [[ "$b" == "." ]]; then
        target_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
      elif [[ -n "$b" ]]; then
        target_branch="$b"
      else
        upstream="$(git -C "$path" for-each-ref --format='%(upstream:short)' "$(git -C "$path" symbolic-ref -q HEAD)" 2>/dev/null || true)"
        if [[ -n "$upstream" ]]; then
          target_branch="${upstream#*/}"
        else
          git -C "$path" remote set-head -a "$remote" >/dev/null 2>&1 || true
          symref="$(git -C "$path" symbolic-ref --short -q "refs/remotes/$remote/HEAD" 2>/dev/null | awk -F/ '{print $2}')"
          target_branch="${symref:-main}"
        fi
      fi
    fi

    if (( dry_run )); then
      echo "==> $path"
      echo "  fetch $remote"
      echo "  update to $remote/$target_branch ($mode)"
      continue
    fi

    git -C "$path" fetch "$remote" --tags || { echo "Fetch failed in $path" >&2; continue; }
    local old new
    old="$(git -C "$path" rev-parse --short=12 HEAD)"

    case "$mode" in
      merge)
        git -C "$path" checkout -B "$target_branch" "$remote/$target_branch" 2>/dev/null || git -C "$path" checkout "$target_branch"
        git -C "$path" merge --ff-only "$remote/$target_branch" || git -C "$path" merge "$remote/$target_branch" || { echo "Merge failed in $path" >&2; continue; } ;;
      rebase)
        git -C "$path" checkout -B "$target_branch" "$remote/$target_branch" 2>/dev/null || git -C "$path" checkout "$target_branch"
        git -C "$path" rebase "$remote/$target_branch" || { git -C "$path" rebase --abort || true; echo "Rebase failed in $path" >&2; continue; } ;;
      ff|ff-only|*)
        git -C "$path" reset --hard "$remote/$target_branch" || { echo "Reset failed in $path" >&2; continue; } ;;
    esac

    new="$(git -C "$path" rev-parse --short=12 HEAD)"
    if [[ "$old" == "$new" ]]; then
      echo "==> $path is already up to date ($new)"
      continue
    fi

    git -C "$root" add -- "$path" || { echo "Stage failed for $path" >&2; continue; }
    git -C "$root" commit -m "bump to submodule $path: $old -> $new" || true
    echo "==> $path: $old -> $new (committed)"
  done
}

# ──────────────────────────────────────────────────────────────────────────────
# 10) COMPLETION & KEY BINDINGS
# ──────────────────────────────────────────────────────────────────────────────
# Initialize completions with aggressive cache optimization
autoload -Uz compinit
# Skip security check completely in devcontainer for speed
compinit -C

# Enable better completion
zstyle ':completion:*' menu select
zstyle ':completion:*' matcher-list 'm:{a-zA-Z}={A-Za-z}'
zstyle ':completion:*' list-colors "${(s.:.)LS_COLORS}"
zstyle ':completion:*' verbose yes
zstyle ':completion:*:descriptions' format '%B%d%b'
zstyle ':completion:*:messages' format '%d'
zstyle ':completion:*:warnings' format 'No matches for: %d'
zstyle ':completion:*' group-name ''

# Speed up completions
zstyle ':completion:*' accept-exact '*(N)'
zstyle ':completion:*' use-cache on
zstyle ':completion:*' cache-path "$HOME/.zsh/cache"
zstyle ':completion:*' rehash true

# Better kill completion
zstyle ':completion:*:*:kill:*:processes' list-colors '=(#b) #([0-9]#)*=0=01;31'
zstyle ':completion:*:*:kill:*' menu yes select
zstyle ':completion:*:kill:*' force-list always

# Key bindings for history search
if [[ -n "${plugins[(r)history-substring-search]}" ]]; then
  bindkey '^[[A' history-substring-search-up
  bindkey '^[[B' history-substring-search-down
  bindkey '^P' history-substring-search-up
  bindkey '^N' history-substring-search-down
fi

# Better word navigation
bindkey '^[[1;5C' forward-word   # Ctrl+Right
bindkey '^[[1;5D' backward-word  # Ctrl+Left
bindkey '^[[H' beginning-of-line # Home
bindkey '^[[F' end-of-line       # End

# Quick sudo - Alt+S
bindkey -s '^[s' '^Asudo ^E'

# Edit command in editor
autoload -z edit-command-line
zle -N edit-command-line
bindkey '^X^E' edit-command-line

# ──────────────────────────────────────────────────────────────────────────────
# 11) QUICK DIRECTORY BOOKMARKS
# ──────────────────────────────────────────────────────────────────────────────
hash -d docs="$HOME/Documents"
hash -d dl="$HOME/Downloads"
hash -d proj="$HOME/Projects"
hash -d conf="$HOME/.config"
hash -d tmp=/tmp
hash -d ws=/workspaces
hash -d dc=/workspaces/.devcontainer

# ──────────────────────────────────────────────────────────────────────────────
# 12) DEVCONTAINER/CODESPACES SPECIFIC
# ──────────────────────────────────────────────────────────────────────────────
# GitHub Codespaces environment
if [[ -n "$CODESPACES" ]]; then
  export GITHUB_TOKEN="${GITHUB_TOKEN:-$GH_TOKEN}"
  export BROWSER="code --goto"
fi

# VSCode terminal integration
if [[ "$TERM_PROGRAM" == "vscode" ]]; then
  export EDITOR="code --wait"
fi

# Suppress welcome banner in nested shells
if [[ ${SHLVL:-1} -gt 1 && -z "${DEVCONTAINER_SKIP_WELCOME:-}" ]]; then
  export DEVCONTAINER_SKIP_WELCOME=1
fi

# Auto-activate Python virtual environment
if [[ -z "$VIRTUAL_ENV" ]]; then
  if [[ -f .venv/bin/activate ]]; then
    source .venv/bin/activate
  elif [[ -f venv/bin/activate ]]; then
    source venv/bin/activate
  fi
fi

# Auto-enable corepack for pnpm/yarn
if _has_cmd corepack; then
  corepack enable 2>/dev/null
fi

# ──────────────────────────────────────────────────────────────────────────────
# 13) EXTERNAL TOOL COMPLETIONS (LAZY LOADED)
# ──────────────────────────────────────────────────────────────────────────────

# GitHub CLI - lazy load on first use
if _has_cmd gh; then
  __init_gh_completion() {
    unset -f __init_gh_completion
    eval "$(_cache_cmd "gh_completion" 86400 gh completion -s zsh)"  # Cache 1 day
  }
  # Trigger on first tab completion
  compdef __init_gh_completion gh
fi

# UV - lazy load on first use
if _has_cmd uv; then
  __init_uv_completion() {
    unset -f __init_uv_completion
    eval "$(_cache_cmd "uv_completion" 86400 uv generate-shell-completion zsh)"  # Cache 1 day
  }
  compdef __init_uv_completion uv
fi

# AWS CLI (already file-based, keep as is)
[[ -f /usr/local/bin/aws_zsh_completer.sh ]] && source /usr/local/bin/aws_zsh_completer.sh

# Google Cloud SDK (already file-based, keep as is)
[[ -f "$HOME/google-cloud-sdk/path.zsh.inc" ]] && source "$HOME/google-cloud-sdk/path.zsh.inc"
[[ -f "$HOME/google-cloud-sdk/completion.zsh.inc" ]] && source "$HOME/google-cloud-sdk/completion.zsh.inc"

# Claude CLI completions (disabled - was taking 5+ seconds)
# if _has_cmd claude; then
#   CLAUDE_COMPLETIONS="$(claude completions zsh 2>/dev/null || true)"
#   # Only eval if it looks like real completion code (often starts with '_' or '#compdef')
#   if [[ "$CLAUDE_COMPLETIONS" == _* || "$CLAUDE_COMPLETIONS" == \#compdef* ]]; then
#     eval "$CLAUDE_COMPLETIONS"
#   fi
# fi

# ──────────────────────────────────────────────────────────────────────────────
# 14) LOCAL OVERRIDES
# ──────────────────────────────────────────────────────────────────────────────
# Source local configuration if it exists
[[ -f "$HOME/.zshrc.local" ]] && source "$HOME/.zshrc.local"

# Display system info (only on first shell of the day)
FASTFETCH_MARKER="$ZSH_CACHE_DIR/fastfetch_shown_$(date +%Y%m%d)"
if [[ -z "$DEVCONTAINER_SKIP_WELCOME" ]] && [[ ! -f "$FASTFETCH_MARKER" ]]; then
  if _has_cmd fastfetch; then
    fastfetch
    touch "$FASTFETCH_MARKER"
  elif _has_cmd neofetch; then
    neofetch
    touch "$FASTFETCH_MARKER"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# PERFORMANCE: Show profiling results if enabled
# ──────────────────────────────────────────────────────────────────────────────
[[ -n "$ZSH_PROFILE" ]] && zprof
