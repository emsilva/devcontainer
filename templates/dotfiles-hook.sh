# shellcheck shell=bash
# Local runtime preferences are data; never execute this gitignored file.
if [ -z "${CODESPACES+x}" ] && [ -z "${DOTFILES_REPO:-}" ] && [ -f .devcontainer/devcontainer.env ]; then
  DOTFILES_REPO=$(python3 - <<'PY'
from pathlib import Path
lines = Path('.devcontainer/devcontainer.env').read_text().splitlines()
prefix = 'DOTFILES_REPO='
repositories = [line[len(prefix):] for line in lines if line.startswith(prefix)]
if len(repositories) != 1 or not repositories[0]:
    raise SystemExit('Expected one nonempty DOTFILES_REPO= value in .devcontainer/devcontainer.env')
print(repositories[0], end='')
PY
  )
  export DOTFILES_REPO
fi
if [ -n "${DOTFILES_REPO:-}" ]; then
  bash .devcontainer/scripts/apply-dotfiles.sh "$DOTFILES_REPO"
fi
