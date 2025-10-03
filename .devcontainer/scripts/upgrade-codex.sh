#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="openai"
REPO_NAME="codex"
BINARY_NAME="codex"
INSTALL_PREFIX="/usr/local/bin"

if command -v tput >/dev/null 2>&1 && [ -n "${TERM:-}" ]; then
  bold="$(tput bold)"
  reset="$(tput sgr0)"
else
  bold=""
  reset=""
fi

log() {
  printf '%s%s%s\n' "$bold" "$*" "$reset" >&2
}

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

require_tools() {
  local missing=()
  for tool in curl jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      missing+=("$tool")
    fi
  done

  if [ ${#missing[@]} -gt 0 ]; then
    printf 'Missing required tools: %s\n' "${missing[*]}" >&2
    printf 'Install them and re-run this script.\n' >&2
    exit 1
  fi
}

normalize_tag() {
  local tag="$1"
  if [[ "$tag" == rust-v* ]]; then
    printf '%s' "$tag"
  else
    printf 'rust-v%s' "$tag"
  fi
}

is_truthy() {
  local value="${1:-}"
  value="${value,,}"
  case "$value" in
    1|true|yes|y)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

fetch_release_by_tag() {
  local raw_tag="$1"
  local tag
  tag=$(normalize_tag "$raw_tag")
  local release_url="https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/tags/$tag"
  log "Fetching Codex release metadata for $tag"
  local response
  if ! response=$(curl -fsSL "$release_url" 2>/dev/null); then
    printf 'Codex release %s not found.\n' "$tag" >&2
    return 1
  fi
  local actual_tag
  actual_tag=$(printf '%s' "$response" | jq -r '.tag_name // ""')
  if [ -z "$actual_tag" ]; then
    printf 'Codex release %s is missing a tag_name field.\n' "$tag" >&2
    return 1
  fi
  printf '%s' "$actual_tag"
}

fetch_latest_version() {
  if [ -n "${CODEX_FORCE_VERSION:-}" ]; then
    local forced_version
    if ! forced_version=$(fetch_release_by_tag "$CODEX_FORCE_VERSION"); then
      return 1
    fi
    printf '%s' "$forced_version"
    return
  fi
  if [ -n "${CODEX_VERSION:-}" ]; then
    printf '%s' "$(normalize_tag "$CODEX_VERSION")"
    return
  fi
  local releases_url="https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases"
  log "Fetching latest Codex release from $releases_url"
  local releases_json
  releases_json=$(curl -fsSL "$releases_url")
  local version allow_prereleases
  allow_prereleases=${CODEX_ALLOW_PRERELEASES:-}
  if is_truthy "$allow_prereleases"; then
    log "Including prerelease Codex builds (CODEX_ALLOW_PRERELEASES=true)"
    version=$(printf '%s' "$releases_json" | jq -r '([.[] | select(.draft|not)] | first | .tag_name) // ""')
  else
    log "Selecting latest stable Codex release (set CODEX_ALLOW_PRERELEASES=true to include prereleases)"
    version=$(printf '%s' "$releases_json" | jq -r '([.[] | select((.draft|not) and (.prerelease|not) and (.tag_name | test("alpha"; "i") | not))] | first | .tag_name) // ""')
  fi
  if [ -z "$version" ]; then
    printf 'Unable to determine latest Codex release.\n' >&2
    return 1
  fi
  printf '%s' "$version"
}

select_asset() {
  local version="$1"
  local os arch target
  os=$(uname | tr '[:upper:]' '[:lower:]')
  arch=$(uname -m)

  case "$arch" in
    x86_64 | amd64)
      target="x86_64-unknown-linux-musl"
      arch="x86_64"
      ;;
    i686 | i386)
      target="i686-unknown-linux-musl"
      arch="i386"
      ;;
    armv7l)
      target="armv7-unknown-linux-gnueabihf"
      arch="armv7"
      ;;
    aarch64 | arm64)
      target="aarch64-unknown-linux-gnu"
      arch="arm64"
      ;;
    *)
      printf 'Unsupported architecture: %s\n' "$arch" >&2
      exit 1
      ;;
  esac

  local release_url="https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/tags/$version"
  log "Inspecting release assets for $version"
  local assets
  assets=$(curl -fsSL "$release_url" | jq -r '.assets[].name' 2>/dev/null || true)

  if [ -n "$assets" ]; then
    local zst_asset tar_asset zip_asset

    zst_asset=$(printf '%s\n' "$assets" | grep -E "^${BINARY_NAME}-${target}\.zst$" | head -n1 || true)
    if [ -z "$zst_asset" ]; then
      zst_asset=$(printf '%s\n' "$assets" | grep -E "^${BINARY_NAME}-(cli-)?[^[:space:]]*${target}\.zst$" | grep -v "responses-api-proxy" | head -n1 || true)
    fi
    if [ -z "$zst_asset" ]; then
      zst_asset=$(printf '%s\n' "$assets" | grep -i "${target}\.zst" | grep -v "responses-api-proxy" | head -n1 || true)
    fi

    tar_asset=$(printf '%s\n' "$assets" | grep -E "^${BINARY_NAME}-${target}\.tar.gz$" | head -n1 || true)
    if [ -z "$tar_asset" ]; then
      tar_asset=$(printf '%s\n' "$assets" | grep -i "${target}\.tar.gz" | grep -v "responses-api-proxy" | head -n1 || true)
    fi

    zip_asset=$(printf '%s\n' "$assets" | grep -E "^${BINARY_NAME}-${target}\.zip$" | head -n1 || true)
    if [ -z "$zip_asset" ]; then
      zip_asset=$(printf '%s\n' "$assets" | grep -i "${target}\.zip" | grep -v "responses-api-proxy" | head -n1 || true)
    fi

    if [ -n "$zst_asset" ]; then
      printf '%s\n' "https://github.com/$REPO_OWNER/$REPO_NAME/releases/download/$version/$zst_asset zst"
      return
    fi
    if [ -n "$tar_asset" ]; then
      printf '%s\n' "https://github.com/$REPO_OWNER/$REPO_NAME/releases/download/$version/$tar_asset tar"
      return
    fi
    if [ -n "$zip_asset" ]; then
      printf '%s\n' "https://github.com/$REPO_OWNER/$REPO_NAME/releases/download/$version/$zip_asset zip"
      return
    fi
  fi

  local candidate="https://github.com/$REPO_OWNER/$REPO_NAME/releases/download/$version/${BINARY_NAME}-${target}.zst"
  printf '%s\n' "$candidate zst"
}

extract_binary() {
  local file="$1"
  local format="$2"
  local dest="$3"

  case "$format" in
    zst)
      if ! command -v zstd >/dev/null 2>&1; then
        printf 'The zstd utility is required to extract %s. Install zstd and retry.\n' "$file" >&2
        exit 1
      fi
      zstd -d "$file" -o "$dest"
      ;;
    tar)
      if ! command -v tar >/dev/null 2>&1; then
        printf 'The tar utility is required to extract %s. Install tar and retry.\n' "$file" >&2
        exit 1
      fi
      tar -xf "$file"
      local found
      found=$(find . -type f -name "$BINARY_NAME" -perm -u+x | head -n1 || true)
      if [ -z "$found" ]; then
        printf 'Codex binary not found in archive %s\n' "$file" >&2
        exit 1
      fi
      mv "$found" "$dest"
      ;;
    zip)
      if ! command -v unzip >/dev/null 2>&1; then
        printf 'The unzip utility is required to extract %s. Install unzip and retry.\n' "$file" >&2
        exit 1
      fi
      unzip -q "$file"
      local found_zip
      found_zip=$(find . -type f -name "$BINARY_NAME" -perm -u+x | head -n1 || true)
      if [ -z "$found_zip" ]; then
        printf 'Codex binary not found in archive %s\n' "$file" >&2
        exit 1
      fi
      mv "$found_zip" "$dest"
      ;;
    *)
      printf 'Unknown archive format: %s\n' "$format" >&2
      exit 1
      ;;
  esac
}

install_binary() {
  local source="$1"
  local tmp_wrapper

  run_root install -m 0755 "$source" "$INSTALL_PREFIX/$BINARY_NAME"

  tmp_wrapper=$(mktemp)
  cat >"$tmp_wrapper" <<'WRAP'
#!/usr/bin/env bash
export OPENAI_API_KEY="${OPENAI_API_KEY:-}"
echo "Starting codex - remember to set your OPENAI_API_KEY environment variable"
exec codex "$@"
WRAP
  run_root install -m 0755 "$tmp_wrapper" "$INSTALL_PREFIX/${BINARY_NAME}-wrapper"
  rm -f "$tmp_wrapper"
  run_root ln -sf "$INSTALL_PREFIX/${BINARY_NAME}-wrapper" "$INSTALL_PREFIX/${BINARY_NAME}-cli"
}

print_version_summary() {
  local binary_path="$1"
  local release_tag="$2"

  if "$binary_path" --help 2>&1 | grep -q -- '--version'; then
    "$binary_path" --version
  else
    log "Installed Codex release $release_tag (CLI does not expose --version)"
  fi
}

main() {
  require_tools
  local version
  version=$(fetch_latest_version)
  log "Latest Codex version: $version"
  local asset_info download_url format
  asset_info=$(select_asset "$version")
  download_url="${asset_info%% *}"
  format="${asset_info##* }"

  log "Downloading Codex from $download_url"
  local tmp_dir tmp_file
  tmp_dir=$(mktemp -d)
  trap 'if [ -n "${tmp_dir:-}" ]; then rm -rf "$tmp_dir"; fi' EXIT
  cd "$tmp_dir"
  tmp_file="codex-download"
  curl -fL "$download_url" -o "$tmp_file"

  local extracted="$tmp_dir/$BINARY_NAME"
  extract_binary "$tmp_file" "$format" "$extracted"
  chmod +x "$extracted"
  install_binary "$extracted"

  log "Codex upgrade complete"
  print_version_summary "$INSTALL_PREFIX/$BINARY_NAME" "$version"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
