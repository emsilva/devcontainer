# Repository Guidelines

## Project Structure & Module Organization
- `install.sh` bootstraps this devcontainer into another repo; treat the root as the canonical template.
- `.devcontainer/devcontainer.json` defines features (Go, Node, Python, Ruby, Rust, CLIs) and lifecycle hooks.
- `.devcontainer/scripts/` holds bash helpers (`post-create.sh`, `post-start.sh`, `upgrade-codex.sh`, `validate-*.sh`); keep additions idempotent and container-safe.
- `.devcontainer/user/` stores dotfile templates (`*.template`) that render on post-create; symlinks are built automatically.
- `Taskfile.yml` (root and `.devcontainer/Taskfile.yml`) track devcontainer rebuild/upgrade flows; add new tasks rather than ad-hoc scripts when feasible.

## Build, Test & Development Commands
- `task rebuild` — recreate the container locally with the Dev Containers CLI (requires `@devcontainers/cli`).
- `task upgrade:self` — pull the latest canonical `.devcontainer` assets.
- `task upgrade:codex` — refresh the Codex CLI in-place.
- `bash .devcontainer/scripts/validate-devcontainer.sh` — verify required tools, PATH wiring, and Docker connectivity (run inside the container; outside requires Docker access).

## Coding Style & Naming Conventions
- Bash scripts: use `#!/usr/bin/env bash`, `set -euo pipefail`, two-space indents, and descriptive function names (`verb_object`).
- JSON/Taskfile updates: keep keys sorted logically, trailing commas disallowed, and align with VS Code `editor.formatOnSave`.
- Template placeholders follow `__UPPER_SNAKE__`; document new variables in `README.md`.

## Testing Guidelines
- After script changes, run `bash .devcontainer/scripts/validate-host.sh` on the host and `validate-devcontainer.sh` inside the workspace.
- For lifecycle hooks, rebuild with `task rebuild` to ensure `post-create.sh` and `post-start.sh` complete without manual intervention.
- Capture regressions by adding quick `set -x` probes locally; remove instrumentation before committing.

## Commit & Pull Request Guidelines
- Commits follow concise, imperative sentence case (`Add Ruby feature`, `Document feature set`); scope one concern per commit.
- Include changeset summaries plus rationale in PR descriptions, reference related issues, and attach screenshots or logs when touching UX-facing scripts.
- Confirm CI or manual validation commands in the PR checklist before requesting review.

## Security & Configuration Tips
- Keep API keys (`CONTEXT7_API_KEY`, `EXA_API_KEY`, `PERSONAL_PAT`) out of git; rely on `remoteEnv` and Codespaces secrets.
- Avoid hardcoding host-specific paths; prefer env vars or feature inputs so the container remains portable.
- Review `.gitignore` after adding generated files to ensure templates and rendered dotfiles stay untracked.
