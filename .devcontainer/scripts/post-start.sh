#!/bin/sh
set -eu

echo "🚀 post-start: initializing session"

# Ensure the command-history volume is owned by the user. Only chown when it has
# actually reset, to avoid a sudo recursive chown on every start.
if [ -d "/commandhistory" ]; then
  if [ "$(stat -c '%U' /commandhistory 2>/dev/null || echo '')" != "$(id -un)" ]; then
    sudo chown -R "$(id -un):$(id -gn)" /commandhistory 2>/dev/null || true
  fi
  touch /commandhistory/.zsh_history
  chmod 600 /commandhistory/.zsh_history
fi

# Update git index to handle any file permission changes
if [ -d .git ]; then
  git update-index --refresh 2>/dev/null || true
fi

# Auto-activate Python virtual environment if it exists
if [ -f .venv/bin/activate ]; then
  echo "🐍 Python virtual environment detected at .venv"
elif [ -f venv/bin/activate ]; then
  echo "🐍 Python virtual environment detected at venv"
fi

# Refresh the tldr cache at most once a week, in the background (never blocks startup).
if command -v tldr >/dev/null 2>&1; then
  tldr_marker="${HOME}/.cache/.tldr-updated"
  if [ ! -f "$tldr_marker" ] || [ -n "$(find "$tldr_marker" -mtime +7 2>/dev/null)" ]; then
    (
      if tldr --update >/dev/null 2>&1; then
        mkdir -p "$(dirname "$tldr_marker")" 2>/dev/null || true
        touch "$tldr_marker" 2>/dev/null || true
      fi
    ) &
  fi
fi

echo "✅ post-start complete"
