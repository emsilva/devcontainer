# Repository Guidelines

## Project Structure & Module Organization
- `install.sh` bootstraps this devcontainer into another repo; treat the root as the canonical template.
- `.devcontainer/devcontainer.json` defines features (Go, Node, Python, Ruby, Rust, CLIs) and lifecycle hooks.
- `.devcontainer/scripts/` holds bash helpers (`on-create.sh`, `post-create.sh`, `post-start.sh`, `setup-dotfiles.sh`, `upgrade-codex.sh`, `validate-*.sh`); keep additions idempotent and container-safe.
- Personal dotfiles are not stored here — they are applied at create time via chezmoi from `$DOTFILES_REPO` (see `setup-dotfiles.sh`).
- `Taskfile.yml` (root and `.devcontainer/Taskfile.yml`) track devcontainer rebuild/upgrade flows; add new tasks rather than ad-hoc scripts when feasible.

## Build, Test & Development Commands
- `task devcontainer:rebuild` — recreate the container locally with the Dev Containers CLI (requires `@devcontainers/cli`).
- `task devcontainer:upgrade:self` — pull the latest canonical `.devcontainer` assets.
- `task devcontainer:upgrade:lock` — bump feature versions in `devcontainer-lock.json` (review the diff, then commit).
- `task devcontainer:upgrade:codex` — refresh the Codex CLI in-place.
- `task devcontainer:setup:dotfiles -- <repo>` (or `load-dotfiles <repo>`) — apply chezmoi dotfiles.
- `bash .devcontainer/scripts/validate-devcontainer.sh` — verify required tools, PATH wiring, and Docker connectivity (run inside the container; outside requires Docker access).

## Coding Style & Naming Conventions
- Bash scripts: use `#!/usr/bin/env bash`, `set -euo pipefail`, two-space indents, and descriptive function names (`verb_object`).
- JSON/Taskfile updates: keep keys sorted logically, trailing commas disallowed, and align with VS Code `editor.formatOnSave`.
- Pass feature option booleans as JSON booleans (`true`/`false`), not quoted strings.

## Testing Guidelines
- After script changes, run `bash .devcontainer/scripts/validate-host.sh` on the host and `validate-devcontainer.sh` inside the workspace.
- For lifecycle hooks, rebuild with `task devcontainer:rebuild` to ensure `on-create.sh`, `post-create.sh`, and `post-start.sh` complete without manual intervention.
- Capture regressions by adding quick `set -x` probes locally; remove instrumentation before committing.

## Commit & Pull Request Guidelines
- Commits follow concise, imperative sentence case (`Add Ruby feature`, `Document feature set`); scope one concern per commit.
- Include changeset summaries plus rationale in PR descriptions, reference related issues, and attach screenshots or logs when touching UX-facing scripts.
- Confirm CI or manual validation commands in the PR checklist before requesting review.

## Security & Configuration Tips
- Keep secrets (`PERSONAL_PAT`, etc.) out of git; use Codespaces secrets, or a gitignored `.devcontainer/devcontainer.env` locally (see `devcontainer.env.example`).
- Avoid hardcoding host-specific paths; prefer env vars or feature inputs so the container remains portable.
- Do not wire secrets through `${localEnv:}` (it blanks them in Codespaces); see `SECURITY.md`.
