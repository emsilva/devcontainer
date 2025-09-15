#!/bin/sh
# Minimal environment for non-interactive bash via BASH_ENV.
[ -n "${__NONINT_ENV_LOADED:-}" ] && return 0 || true
__NONINT_ENV_LOADED=1

# Source env-base if not already loaded (PATH, GOPATH, etc.)
if [ -z "${__ENV_BASE_LOADED:-}" ] && [ -f "$HOME/.config/shell/env-base.sh" ]; then
  # shellcheck disable=SC1090
  . "$HOME/.config/shell/env-base.sh"
fi

# Light extras specific to non-interactive contexts can go here.
# (Intentionally minimal to keep script cheap.)

export GOPATH="${GOPATH:-$HOME/go}"
