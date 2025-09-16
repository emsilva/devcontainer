#!/bin/sh
set -eu

echo "🚀 post-start: initializing session"

# Ensure proper permissions on mounted volumes (they can reset)
sudo chown -R vscode:vscode /commandhistory 2>/dev/null || true

# Ensure history file exists with proper permissions
if [ -d "/commandhistory" ]; then
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

# Check for updates to global tools (but don't block)
(
  # Update tldr cache in background
  command -v tldr >/dev/null 2>&1 && tldr --update 2>/dev/null || true
) &


echo "✅ post-start complete"