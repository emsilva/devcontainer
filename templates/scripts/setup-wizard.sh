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
staging=$(mktemp -d "$cache_root/.download.XXXXXX")
trap 'rm -rf "$staging"' EXIT
git init --quiet "$staging/source"
git -C "$staging/source" -c core.hooksPath=/dev/null -c core.autocrlf=false fetch --quiet --depth=1 --no-tags -- \
  "${SETUP_DEVCONTAINER_REPO:-https://github.com/emsilva/devcontainer.git}" "$revision"
git -C "$staging/source" -c core.hooksPath=/dev/null -c core.autocrlf=false checkout --quiet --detach FETCH_HEAD
# Authenticate every tracked cached byte/mode/link using the fresh source authority.
(
  cd "$staging/source"
  python3 -I -B -X pycache_prefix=/dev/null -c 'from pathlib import Path; import sys; sys.path.insert(0, str(Path.cwd())); from devcontainer_setup.releases import publish_release; publish_release(Path.cwd(), sys.argv[1], Path(sys.argv[2]))' "$revision" "$cache_root"
)
rm -rf "$staging"
trap - EXIT
export DEVCONTAINER_SETUP_REF="$revision"
exec bash "$runner" --project "$PWD" "$@"
