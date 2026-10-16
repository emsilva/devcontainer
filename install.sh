#!/bin/sh
# Download an immutable wizard release; target changes require its final approval.
set -eu

command -v git >/dev/null 2>&1 || { echo 'git is required' >&2; exit 1; }
project_root=$(git rev-parse --show-toplevel) || exit 1

# A checked-out installer runs its own source, useful before publishing a release.
case "$0" in
  */install.sh)
    installer_root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
    if [ -f "$installer_root/scripts/setup.sh" ] && [ -d "$installer_root/devcontainer_setup" ]; then
      exec bash "$installer_root/scripts/setup.sh" --project "$project_root" "$@"
    fi
    ;;
esac

repo_url=${SETUP_DEVCONTAINER_REPO:-https://github.com/emsilva/devcontainer.git}
source_ref=${SETUP_DEVCONTAINER_REF:-main}
case "${XDG_CACHE_HOME:-}" in
  /*) cache_base=$XDG_CACHE_HOME ;;
  *) cache_base=$HOME/.cache ;;
esac
cache_root=$cache_base/devcontainer-setup/releases
mkdir -p "$cache_root"
staging=$(mktemp -d "$cache_root/.install.XXXXXX")
trap 'rm -rf "$staging"' EXIT HUP INT TERM
git init --quiet "$staging/source"
git -C "$staging/source" remote add origin "$repo_url"
git -C "$staging/source" fetch --quiet --depth 1 origin "$source_ref"
git -C "$staging/source" checkout --quiet --detach FETCH_HEAD
revision=$(git -C "$staging/source" rev-parse HEAD)
if [ ! -f "$staging/source/scripts/setup.sh" ]; then
  echo 'The requested devcontainer revision does not include the setup wizard.' >&2
  exit 1
fi
if [ ! -d "$cache_root/$revision" ]; then
  mv "$staging/source" "$cache_root/$revision"
fi
rm -rf "$staging"
trap - EXIT HUP INT TERM
export DEVCONTAINER_SETUP_REF="$revision"
# Piped installs use the terminal for questions, never the downloaded script stream.
case " $* " in
  *' --scan '*|*' --dry-run '*|*' --check '*)
    exec bash "$cache_root/$revision/scripts/setup.sh" --project "$project_root" "$@" ;;
esac
exec bash "$cache_root/$revision/scripts/setup.sh" --project "$project_root" "$@" </dev/tty
