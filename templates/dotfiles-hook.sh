# shellcheck shell=bash
# Local runtime preferences are data; never execute this gitignored file.
if [ -z "${CODESPACES+x}" ] && [ -z "${DOTFILES_REPO:-}" ] && [ -f .devcontainer/devcontainer.env ]; then
  DOTFILES_REPO=$(python3 - <<'PY'
from pathlib import Path
lines = Path('.devcontainer/devcontainer.env').read_text().splitlines()
prefix = 'DOTFILES_REPO='
if len(lines) != 1 or not lines[0].startswith(prefix) or not lines[0][len(prefix):]:
    raise SystemExit('Expected one nonempty DOTFILES_REPO= value in .devcontainer/devcontainer.env')
print(lines[0][len(prefix):], end='')
PY
  )
  export DOTFILES_REPO
fi
if [ -n "${DOTFILES_REPO:-}" ]; then
  bash .devcontainer/scripts/apply-dotfiles.sh "$DOTFILES_REPO"
fi
