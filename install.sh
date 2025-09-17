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

git add .devcontainer

if git diff --cached --quiet; then
  info "No changes detected after copy; nothing to commit"
  exit 0
fi

COMMIT_MESSAGE="setting up devcontainer $SOURCE_SHA"
info "Committing .devcontainer with message: $COMMIT_MESSAGE"
git commit -m "$COMMIT_MESSAGE"

info "Done"
