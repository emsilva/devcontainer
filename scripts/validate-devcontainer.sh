#!/usr/bin/env bash
set -euo pipefail

inside_container() {
  [ -f /.dockerenv ] || [ -n "${CODESPACES:-}" ]
}

escape_single_quotes() {
  sed "s/'/'\\''/g"
}

remote_user=${REMOTE_USER:-vscode}

exec_container_sh() {
  docker exec --user "$remote_user" "$CONTAINER_ID" sh -c "$1"
}

exec_container_bash_lc() {
  docker exec --user "$remote_user" "$CONTAINER_ID" bash -lc "$1"
}

run_in_target() {
  local shell_cmd=$1
  local snippet=$2
  if [ "$shell_cmd" = "__current" ]; then
    ( eval "$snippet" )
    return
  fi
  local escaped
  escaped=$(printf '%s' "$snippet" | escape_single_quotes)
  local full_cmd="${shell_cmd} '${escaped}'"
  if [ "$TARGET" = "container" ]; then
    exec_container_sh "$full_cmd"
  else
    sh -c "$full_cmd"
  fi
}

run_tool_checks() {
  read -r -d '' snippet <<'EOS' || true
set -e
for tool in go node python3 uv aws az gcloud gh docker fzf task starship pulumi; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf '%-10s %s\n' "$tool" "$(command -v "$tool")"
    case "$tool" in
      go) go version ;;
      pulumi) pulumi version ;;
      node) node --version ;;
      python3) python3 --version ;;
      uv) uv --version ;;
      aws) aws --version ;;
      az) az version --output table 2>/dev/null | head -1 || az --version ;;
      gcloud) gcloud version 2>/dev/null | head -1 || echo "gcloud $(gcloud --version 2>&1 | head -1)" ;;
      gh) gh --version ;;
      docker) 
        if docker version >/dev/null 2>&1; then
          docker --version
        else
          echo "Docker CLI present (daemon not accessible)"
        fi ;;
      fzf) fzf --version ;;
      task) task --version ;;
      starship) starship --version ;;
    esac
  else
    printf '%-10s %s\n' "$tool" "NOT FOUND"
  fi
  echo "---"
done
EOS
  if [ "$TARGET" = "container" ]; then
    exec_container_bash_lc "$snippet"
  else
    bash -lc "$snippet"
  fi
}

# Collect environment data from a specific shell context
collect_env_data() {
  local shell_cmd="$1"
  read -r -d '' env_snippet <<'EOS' || true
set -e
user_name=$(id -un 2>/dev/null || echo unknown)
shell_name=$(ps -p $$ -o comm= 2>/dev/null || echo unknown)
echo "USER=$user_name"
echo "SHELL=$shell_name"
echo "PATH=$PATH"
echo "GOPATH=${GOPATH:-<unset>}"
echo "GOMODCACHE=${GOMODCACHE:-<unset>}"
echo "NVM_DIR=${NVM_DIR:-<unset>}"
echo "PNPM_HOME=${PNPM_HOME:-<unset>}"
echo "UV_CACHE_DIR=${UV_CACHE_DIR:-<unset>}"
echo "PULUMI_HOME=${PULUMI_HOME:-<unset>}"
for bin in go node python3 uv aws az gcloud gh docker fzf task starship pulumi; do
  if command -v "$bin" >/dev/null 2>&1; then
    path=$(command -v "$bin")
    resolved=$(realpath "$path" 2>/dev/null || echo "$path")
    if [ "$bin" = "docker" ] && ! docker version >/dev/null 2>&1; then
      echo "which $bin -> $path (daemon not accessible)"
    else
      echo "which $bin -> $path"
    fi
    echo "realpath $bin -> $resolved"
  else
    echo "which $bin -> NOT FOUND"
    echo "realpath $bin -> NOT FOUND"
  fi
done
EOS

  if [ "$shell_cmd" = "__current" ]; then
    ( eval "$env_snippet" )
  else
    local escaped
    escaped=$(printf '%s' "$env_snippet" | escape_single_quotes)
    local full_cmd="${shell_cmd} '${escaped}'"
    if [ "${TARGET:-}" = "container" ] && [ -n "${CONTAINER_ID:-}" ]; then
      exec_container_sh "$full_cmd"
    else
      sh -c "$full_cmd"
    fi
  fi
}

# Compare two environment data sets
compare_env_data() {
  local baseline="$1"
  local current="$2"
  local shell_name="$3"
  local issues=0
  
  # Check critical environment variables
  local vars="USER SHELL GOPATH GOMODCACHE NVM_DIR PNPM_HOME UV_CACHE_DIR PULUMI_HOME"
  for var in $vars; do
    local baseline_val current_val
    baseline_val=$(echo "$baseline" | grep "^${var}=" | cut -d= -f2- || echo "<missing>")
    current_val=$(echo "$current" | grep "^${var}=" | cut -d= -f2- || echo "<missing>")
    
    if [ "$baseline_val" != "$current_val" ]; then
      echo "  ❌ $var differs: '$baseline_val' vs '$current_val'"
      issues=$((issues + 1))
    fi
  done
  
  # Check tool availability consistency
  local tools="go node python3 uv aws az gcloud gh docker fzf task starship pulumi"
  for tool in $tools; do
    local baseline_path current_path baseline_real current_real
    baseline_path=$(echo "$baseline" | grep "^which $tool ->" | sed "s/which $tool -> //" || echo "NOT FOUND")
    current_path=$(echo "$current" | grep "^which $tool ->" | sed "s/which $tool -> //" || echo "NOT FOUND")
    baseline_real=$(echo "$baseline" | grep "^realpath $tool ->" | sed "s/realpath $tool -> //" || echo "NOT FOUND")
    current_real=$(echo "$current" | grep "^realpath $tool ->" | sed "s/realpath $tool -> //" || echo "NOT FOUND")
    
    baseline_path="${baseline_path/ (daemon not accessible)/}"
    current_path="${current_path/ (daemon not accessible)/}"
    baseline_real="${baseline_real/ (daemon not accessible)/}"
    current_real="${current_real/ (daemon not accessible)/}"
    
    if [ "$baseline_path" != "$current_path" ] && [ "$baseline_real" != "$current_real" ]; then
      echo "  ❌ $tool path differs: '$baseline_path' vs '$current_path'"
      issues=$((issues + 1))
    fi
  done
  
  # Check PATH consistency (count of directories)
  local baseline_path_count current_path_count
  baseline_path_count=$(echo "$baseline" | grep "^PATH=" | cut -d= -f2- | tr ':' '\n' | wc -l)
  current_path_count=$(echo "$current" | grep "^PATH=" | cut -d= -f2- | tr ':' '\n' | wc -l)
  
  if [ "$baseline_path_count" != "$current_path_count" ]; then
    echo "  ⚠️  PATH has different number of directories: $baseline_path_count vs $current_path_count"
    issues=$((issues + 1))
  fi
  
  if [ "$issues" -eq 0 ]; then
    echo "  ✅ $shell_name environment consistent with baseline"
  else
    echo "  📊 $shell_name has $issues differences from baseline"
  fi
  
  return "$issues"
}

run_env_checks() {
  local checks=(
    "Current shell|__current"
    "Bash login|bash -lc"
    "Bash non-login|bash -c"
    "POSIX sh|sh -c"
    "Zsh login|zsh -lc"
    "Zsh interactive|zsh -ic"
  )
  
  echo "== Environment by shell (with consistency validation) =="
  
  local entry label cmd baseline_data
  local total_issues=0
  local first_run=true
  
  for entry in "${checks[@]}"; do
    IFS='|' read -r label cmd <<<"$entry"
    echo
    echo "=== $label ==="
    
    # Collect environment data
    local env_data
    env_data=$(collect_env_data "$cmd" 2>&1 || echo "ERROR: Failed to collect data")
    
    # Display the data
    echo "$env_data"
    
    # Store baseline from first successful run
    if [ "$first_run" = true ] && [ "$env_data" != "ERROR: Failed to collect data" ]; then
      baseline_data="$env_data"
      first_run=false
      echo "  📝 Using as baseline for comparison"
    elif [ "$env_data" != "ERROR: Failed to collect data" ] && [ -n "$baseline_data" ]; then
      # Compare with baseline
      echo
      echo "  🔍 Consistency check:"
      local shell_issues
      if compare_env_data "$baseline_data" "$env_data" "$label"; then
        shell_issues=0
      else
        shell_issues=$?
      fi
      total_issues=$((total_issues + shell_issues))
    elif [ "$env_data" = "ERROR: Failed to collect data" ]; then
      echo "  ❌ Failed to collect environment data"
      total_issues=$((total_issues + 1))
    fi
  done
  
  echo
  echo "== Environment Consistency Summary =="
  if [ "$total_issues" -eq 0 ]; then
    echo "✅ All shell environments are consistent!"
  else
    echo "⚠️  Found $total_issues environment inconsistencies across shells"
    echo "💡 Consider checking your shell configuration files"
  fi
}

run_checks() {
  echo '== Tool versions =='
  run_tool_checks
  echo
  run_env_checks
}

if [ "${VALIDATE_ENV_INTERNAL:-}" = "1" ]; then
  TARGET=${TARGET:-local}
  CONTAINER_ID=${CONTAINER_ID:-}
  run_checks
  exit 0
fi

if inside_container; then
  TARGET=local
  run_checks
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required when running outside the container." >&2
  exit 1
fi

# Test Docker connectivity first
if ! docker ps >/dev/null 2>&1; then
  echo "Cannot connect to Docker daemon. Please check:" >&2
  echo "1. Docker daemon is running" >&2
  echo "2. You have permission to access Docker socket" >&2
  echo "3. Try: sudo usermod -aG docker \$USER && newgrp docker" >&2
  exit 1
fi

container_id=$(docker ps --filter "label=devcontainer.local_folder=$PWD" -q | head -n1)
if [ -z "$container_id" ]; then
  echo "No running devcontainer found for $PWD" >&2
  echo "Available containers:" >&2
  docker ps --format "table {{.ID}}\t{{.Names}}\t{{.Image}}" | head -5 >&2
  exit 1
fi
workspace_name=$(basename "$PWD")
container_script="/workspaces/${workspace_name}/scripts/validate-devcontainer.sh"
remote_user_main=${DEVCONTAINER_REMOTE_USER:-vscode}
docker exec --user "$remote_user_main" "$container_id" bash -lc "cd /workspaces/${workspace_name} && REMOTE_USER=$remote_user_main VALIDATE_ENV_INTERNAL=1 TARGET=local CONTAINER_ID=$container_id '$container_script'"
