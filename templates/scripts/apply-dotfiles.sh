#!/usr/bin/env bash
set -euo pipefail
repo="${1:-${DOTFILES_REPO:-}}"
if [ -z "$repo" ]; then
  echo 'Supply a dotfiles repository argument or set DOTFILES_REPO.' >&2
  exit 1
fi
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/devcontainer-setup"
mkdir -p "$state_dir"
chmod 700 "$state_dir"
repo_digest=$(printf '%s' "$repo" | sha256sum | cut -d ' ' -f 1)
stamp="$state_dir/dotfiles-$repo_digest"
if [ -f "$stamp" ]; then
  printf 'Dotfiles already applied for this repository.\n'
  exit 0
fi
chezmoi init --apply -- "$repo"
(umask 077; touch "$stamp")
