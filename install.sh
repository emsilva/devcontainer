#!/bin/sh
# Bootstrap the canonical devcontainer config into the current repo.
set -eu

REPO_URL=${SETUP_DEVCONTAINER_REPO:-https://github.com/emsilva/devcontainer.git}
REF=${SETUP_DEVCONTAINER_REF:-main}
TMP_DIR=${SETUP_DEVCONTAINER_TMPDIR:-/tmp/devcontainer-setup}

info() {
  printf '%s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

command -v git >/dev/null 2>&1 || die "git is required"

ensure_taskfile_shim() {
  taskfile="$REPO_ROOT/Taskfile.yml"

  if grep -qs '\.devcontainer/Taskfile\.yml' "$taskfile" 2>/dev/null; then
    info "Taskfile.yml already references devcontainer shim; skipping"
    return
  fi

  if [ ! -f "$taskfile" ]; then
    info "Creating Taskfile.yml with devcontainer shim"
    cat >"$taskfile" <<'EOF'
version: '3'

includes:
  devcontainer:
    taskfile: .devcontainer/Taskfile.yml
    optional: true
EOF
    return
  fi

  command -v python3 >/dev/null 2>&1 || die "python3 is required to update Taskfile.yml"

  info "Adding devcontainer shim to existing Taskfile.yml"
  TASKFILE_PATH="$taskfile" python3 <<'PY'
import os
import sys

path = os.environ["TASKFILE_PATH"]

with open(path, "r", encoding="utf-8") as fh:
    lines = fh.read().splitlines()

target = ".devcontainer/Taskfile.yml"
if any(target in line for line in lines):
    sys.exit(0)

includes_idx = None
includes_indent = 0
for idx, line in enumerate(lines):
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        continue
    if stripped.startswith("includes:"):
        includes_idx = idx
        includes_indent = len(line) - len(stripped)
        break

entry = [
    " " * (includes_indent + 2) + "devcontainer:",
    " " * (includes_indent + 4) + "taskfile: .devcontainer/Taskfile.yml",
    " " * (includes_indent + 4) + "optional: true",
]

if includes_idx is None:
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("includes:")
    lines.extend(entry)
else:
    insert_at = includes_idx + 1
    total = len(lines)
    while insert_at < total:
        current = lines[insert_at]
        if not current.strip():
            insert_at += 1
            continue
        current_indent = len(current) - len(current.lstrip())
        if current_indent <= includes_indent:
            break
        insert_at += 1
    if insert_at > includes_idx + 1 and lines[insert_at - 1].strip():
        entry = [""] + entry
    lines = lines[:insert_at] + entry + lines[insert_at:]

with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
PY
}

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  die "run this from within a git repository"
fi

if [ -n "$(git status --porcelain)" ]; then
  die "working tree is not clean; commit or stash changes before running"
fi

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

trap 'rm -rf "$TMP_DIR"' EXIT INT TERM
rm -rf "$TMP_DIR"

info "Cloning $REPO_URL (ref: $REF)"
if ! git clone --depth 1 --branch "$REF" "$REPO_URL" "$TMP_DIR" 2>/dev/null; then
  die "failed to clone $REPO_URL@$REF"
fi

if [ ! -d "$TMP_DIR/.devcontainer" ]; then
  die "source repository does not contain a .devcontainer directory"
fi

SOURCE_SHA=$(git -C "$TMP_DIR" rev-parse HEAD)
info "Fetched commit $SOURCE_SHA"

if git ls-files --error-unmatch .devcontainer >/dev/null 2>&1; then
  info "Removing tracked .devcontainer from current repository"
  git rm -r .devcontainer >/dev/null 2>&1 || die "failed to remove tracked .devcontainer"
elif [ -e .devcontainer ]; then
  info "Removing existing untracked .devcontainer"
  rm -rf .devcontainer || die "failed to remove existing .devcontainer"
fi

info "Copying new .devcontainer into place"
cp -a "$TMP_DIR/.devcontainer" .

ensure_taskfile_shim

git add .devcontainer
if [ -f Taskfile.yml ]; then
  git add Taskfile.yml
fi

if git diff --cached --quiet; then
  info "No changes detected after copy; nothing to commit"
  exit 0
fi

COMMIT_MESSAGE="setting up devcontainer $SOURCE_SHA"
info "Committing .devcontainer with message: $COMMIT_MESSAGE"
git commit -m "$COMMIT_MESSAGE"

info "Done"
