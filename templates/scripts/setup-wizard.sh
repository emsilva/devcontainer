#!/usr/bin/env bash
set -euo pipefail
revision='__SOURCE_REF__'
case "${XDG_CACHE_HOME:-}" in
  /*) cache_base=$XDG_CACHE_HOME ;;
  *) cache_base=$HOME/.cache ;;
esac
cache_root="$cache_base/devcontainer-setup/releases"
runner="$cache_root/$revision/scripts/setup.sh"
mkdir -p "$cache_root"

allow_offline=0
force_offline=0
for arg in "$@"; do
  case "$arg" in
    --check|--scan|--dry-run|--doctor) allow_offline=1 ;;
    --offline) allow_offline=1; force_offline=1 ;;
  esac
done

refresh_cache() {
  local staging
  staging=$(mktemp -d "$cache_root/.download.XXXXXX")
  trap 'rm -rf "$staging"' EXIT
  git init --quiet "$staging/source"
  if ! git -C "$staging/source" -c core.hooksPath=/dev/null -c core.autocrlf=false fetch --quiet --depth=1 --no-tags -- \
    "${SETUP_DEVCONTAINER_REPO:-https://github.com/emsilva/devcontainer.git}" "$revision"; then
    rm -rf "$staging"
    trap - EXIT
    return 2
  fi
  git -C "$staging/source" -c core.hooksPath=/dev/null -c core.autocrlf=false checkout --quiet --detach FETCH_HEAD
  # Authenticate every tracked cached byte/mode/link using the fresh source authority.
  if ! (
    cd "$staging/source"
    python3 -I -B -X pycache_prefix=/dev/null -c 'from pathlib import Path; import sys; sys.path.insert(0, str(Path.cwd())); from devcontainer_setup.releases import fetch_release; fetch_release(str(Path.cwd()), sys.argv[1], Path(sys.argv[2]))' "$revision" "$cache_root"
  ); then
    rm -rf "$staging"
    trap - EXIT
    return 1
  fi
  rm -rf "$staging"
  trap - EXIT
  return 0
}

if [ "$force_offline" = 1 ] && [ -f "$runner" ]; then
  :
else
  rc=0
  refresh_cache || rc=$?
  if [ "$rc" = 2 ]; then
    if [ "$allow_offline" = 1 ] && [ -f "$runner" ]; then
      echo "Note: GitHub is unreachable; running cached release $revision offline." >&2
    else
      echo "Setup: could not fetch generator release $revision from GitHub." >&2
      exit 1
    fi
  elif [ "$rc" != 0 ]; then
    exit "$rc"
  fi
fi

export DEVCONTAINER_SETUP_REF="$revision"
exec bash "$runner" --project "$PWD" "$@"
