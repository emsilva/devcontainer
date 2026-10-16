#!/usr/bin/env bash
set -euo pipefail
revision='__SOURCE_REF__'
cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/devcontainer-setup/releases"
runner="$cache_root/$revision/scripts/setup.sh"
if [ ! -f "$runner" ]; then
  mkdir -p "$cache_root"
  staging=$(mktemp -d "$cache_root/.download.XXXXXX")
  trap 'rm -rf "$staging"' EXIT
  curl --fail --location --silent --show-error \
    "https://codeload.github.com/emsilva/devcontainer/tar.gz/$revision" -o "$staging/source.tar.gz"
  mkdir "$staging/release"
  tar -xzf "$staging/source.tar.gz" --strip-components=1 -C "$staging/release"
  test -f "$staging/release/scripts/setup.sh"
  if [ ! -d "$cache_root/$revision" ]; then
    mv "$staging/release" "$cache_root/$revision"
  fi
  rm -rf "$staging"
  trap - EXIT
fi
export DEVCONTAINER_SETUP_REF="$revision"
exec bash "$runner" --project "$PWD" "$@"
