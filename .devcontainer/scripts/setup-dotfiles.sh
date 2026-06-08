#!/usr/bin/env bash
# Apply a chezmoi-managed dotfiles repo into $HOME.
# Repo is resolved from: $1 arg  ->  $DOTFILES_REPO  ->  interactive prompt.
# Backs `task setup:dotfiles`, the `load-dotfiles` command, and postCreate auto-apply.
set -euo pipefail

repo="${1:-${DOTFILES_REPO:-}}"

if [ -z "$repo" ]; then
  if [ -t 0 ]; then
    printf 'Dotfiles repo (e.g. user/dotfiles or an https/ssh git URL): '
    read -r repo
  fi
fi

if [ -z "$repo" ]; then
  echo "No dotfiles repo provided (pass an arg or set \$DOTFILES_REPO)." >&2
  exit 1
fi

if ! command -v chezmoi >/dev/null 2>&1; then
  echo "chezmoi is not installed; cannot apply dotfiles." >&2
  exit 1
fi

echo "🏠 Applying dotfiles from '${repo}' via chezmoi..."
# Private repos resolve through the gh credential helper configured in post-create.
chezmoi init --apply -- "$repo"
echo "✅ Dotfiles applied. Open a new shell to pick them up."
