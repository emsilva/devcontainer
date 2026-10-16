# Native Coding Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Execute this approved user-directed amendment continuously.

**Goal:** Install and update complete native Codex, Claude Code, and OpenCode packages as the container user in every environment.

**Architecture:** A canonical user installation helper is copied verbatim into generated projects; post-create installs before validation. Runtime selection stays independent. Vendor installers select current releases, and a Taskfile target explicitly refreshes all agents.

**Tech Stack:** Bash, Python standard library renderer and unittest suite, Dev Containers lifecycle and environment configuration, go-task.

## Global Constraints

- Codex, Claude Code, and OpenCode are mandatory; old opt-outs cannot disable them.
- Official native installers execute as `vscode` by default, with user-owned packages; no sudo/root or copying only a binary.
- No model request, authentication-token transfer, or global host configuration change.
- Preserve unrelated project work and adopted devcontainer fields.
- All generated files must carry existing ownership hashes and pass the current safe merge rules.
- Local commits are required when gates pass. Do not push or allocate/rebuild a Codespace from the worker.

### Task 1: Native baseline and lifecycle integration

**Files:** `.devcontainer/scripts/install-agents.sh`, `templates/scripts/post-create-selected.sh`, `templates/scripts/validate-selected.sh`, `templates/Taskfile.yml`, `.devcontainer/devcontainer.json`, `.devcontainer/devcontainer-lock.json`, `.devcontainer/Taskfile.yml`, `.devcontainer/scripts/post-create.sh`, `.devcontainer/scripts/validate-devcontainer.sh`, `devcontainer_setup/render.py`, `devcontainer_setup/__main__.py`, `devcontainer_setup/preferences.py`, `README.md`, and directly relevant tests.

**Interfaces:** The shared helper accepts no arguments for idempotent installation, `--update` for intentional updates, and `--check` for installation validation without networking or model calls. The renderer copies its bytes to `.devcontainer/scripts/install-agents.sh`. Post-create calls it before validation. `task devcontainer:upgrade:agents` calls it with `--update`.

- [ ] Add failing behavioral tests using a temporary user home and stub vendor installers. Cover all three official installer URLs, complete Codex installation vs a bare system binary, user ownership, PATH resolution, repeat setup, update, and failure propagation. Demonstrate RED before implementation.
- [ ] Add rendering/wizard regressions proving all three agents cannot be deselected, agent Features disappear, old generated projects migrate safely, Node is not forced, and the update task is emitted.
- [ ] Implement the shared helper. Download each installer to a private temporary directory with curl's failure handling before running its documented shell, preserve complete vendor-managed layouts, and check exact user launcher paths. Refuse root, unknown arguments, and fallbacks to system launchers. Use the documented OpenAI noninteractive option.
- [ ] Wire both canonical and selected containers. Keep one shared helper authority, remove old agent Features and obsolete ownership repair, add the required PATH environment and update tasks, and preserve dotfiles/lifecycle hooks.
- [ ] Remove the TUI opt-out and obsolete agent-induced Node dependency. Treat legacy false flags as accepted but ineffective; review output names all three baseline tools.
- [ ] Update documentation and tests to reflect vendor-controlled moving CLI versions. Run focused tests, `task test`, and `task check`; commit only owned files with no AI attribution.

### Task 2: Independent review and real existing-Codespace acceptance

**Owner:** Root and an independent reviewer; the implementation worker does not access the Codespace.

- [ ] Review the complete branch against its merge-base and repair any material findings.
- [ ] Root reruns test/check gates and regenerates Mainstage configuration through the safe Git transaction. Preserve existing docs commits; commit generated files only.
- [ ] Hash-check and back up the existing cloud repair files before updating them. Run the shared helper as `vscode`, then the emitted selected validator and `task devcontainer:check`.
- [ ] Test Codex/Claude/OpenCode terminal launch up to authentication or a ready prompt without submitting a model request; verify user ownership and PATH in actual shell sessions. Preserve diagnostic evidence.
- [ ] Install the immutable generator release in user caches while the source branch is unpublished. Prepare the exact push/sync proposal for both repositories; request confirmation only for the outward step.
