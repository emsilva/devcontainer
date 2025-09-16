#!/usr/bin/env bash
set -euo pipefail

echo "🔍 Running devcontainer validation from host..."

# Find the running devcontainer
container_id=$(docker ps --filter "label=devcontainer.local_folder=$PWD" -q | head -n1)
if [ -z "$container_id" ]; then
  echo "❌ No running devcontainer found for $PWD" >&2
  echo "Available containers:" >&2
  docker ps --format "table {{.ID}}\t{{.Names}}\t{{.Image}}" | head -5 >&2
  exit 1
fi

echo "✅ Found container: $container_id"
echo "🚀 Running validation script inside container..."
echo

# Run the validation script inside the container
remote_user=${DEVCONTAINER_REMOTE_USER:-vscode}
docker exec --user "$remote_user" "$container_id" bash -c "cd /workspaces/$(basename "$PWD") && REMOTE_USER=$remote_user VALIDATE_ENV_INTERNAL=1 TARGET=local bash scripts/validate-devcontainer.sh"

echo
echo "✅ Validation complete!"
