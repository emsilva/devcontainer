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

# cswap-switch service: keep Claude Code on the account with the most remaining
# quota by looping `cswap switch --strategy best` every 5 minutes in the
# background. A pidfile keeps restarts from stacking loops. Logs errors (and
# no-ops) to ~/.local/state/cswap-switch.log until accounts exist (`cswap add`).
if command -v cswap >/dev/null 2>&1 || [ -x "${HOME}/.local/bin/cswap" ]; then
  cswap_state_dir="${HOME}/.local/state"
  cswap_log="${cswap_state_dir}/cswap-switch.log"
  cswap_pidfile="${cswap_state_dir}/cswap-switch.pid"
  mkdir -p "$cswap_state_dir"
  if [ -f "$cswap_pidfile" ] && kill -0 "$(cat "$cswap_pidfile")" 2>/dev/null; then
    echo "🔁 cswap-switch service already running (pid $(cat "$cswap_pidfile"))"
  else
    echo "🔁 Starting cswap-switch service (strategy: best, every 300s → ${cswap_log})"
    # Single quotes are deliberate: HOME/date/cswap must expand in the child shell.
    # shellcheck disable=SC2016
    nohup sh -c '
      PATH="${HOME}/.local/bin:${PATH}"
      while true; do
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $(cswap switch --strategy best 2>&1)"
        sleep 300
      done
    ' >>"$cswap_log" 2>&1 &
    echo $! >"$cswap_pidfile"
  fi
fi

echo "✅ post-start complete"
