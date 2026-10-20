#!/usr/bin/env bash
set -euo pipefail
repo="${1:-${DOTFILES_REPO:-}}"
if [ -z "$repo" ]; then
  echo 'Supply a dotfiles repository argument or set DOTFILES_REPO.' >&2
  exit 1
fi

# Herdr panes and SSH sessions start the account shell, so dotfiles that
# configure zsh only take effect once zsh is the login shell.
use_zsh_when_configured() {
  local zsh_path user
  zsh_path=$(command -v zsh) || return 0
  [ -f "$HOME/.zshrc" ] || return 0
  user=$(id -un)
  [ "$(getent passwd "$user" | cut -d: -f7)" != "$zsh_path" ] || return 0
  sudo -n chsh -s "$zsh_path" "$user" || echo "Could not make zsh the login shell; run: sudo chsh -s $zsh_path $user" >&2
}

state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/devcontainer-setup"
mkdir -p "$state_dir"
chmod 700 "$state_dir"
repo_digest=$(printf '%s' "$repo" | sha256sum | cut -d ' ' -f 1)
stamp="$state_dir/dotfiles-$repo_digest"
if [ -f "$stamp" ]; then
  printf 'Dotfiles already applied for this repository.\n'
  use_zsh_when_configured
  exit 0
fi
chezmoi init --apply -- "$repo"
(umask 077; touch "$stamp")
use_zsh_when_configured
