# Daily Work Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Ship one intention-based terminal entrypoint for project scratch sync and native conversation continuation.

**Architecture:** Reuse project_sync identity, private state, transport, ownership and handoff. A workflow service provides discovery and orchestration; a small Gum menu consumes it. The generator and setup integrate the same canonical runtime.

**Tech Stack:** Python 3.11 standard library, Gum, go-task, GitHub CLI, owned OpenSSH transport, existing Mutagen worker.

## Global Constraints

- Normal native profiles and project-only memory; no global settings or aliases.
- One writer per conversation; preserve existing ownership/handoff and retry receipts.
- A stopped Codespace starts only from an explicit user action; background sync cannot wake it.
- Preserve existing project files, root tasks, and explicit continuity opt-out.
- Git branch, HEAD, and dirty mismatches block cross-host continuation; never change Git automatically.
- Scope all discovery and metadata to this project; sanitize terminal controls and bound strings.
- Private preferences and pairing stay outside Git using existing safe state primitives.
- Do not modify Mainstage's existing branch or user conversations during tests.
- All new behavior gets meaningful fail-first coverage and real terminal/remote smoke.

---

### Task 1: Workflow service

**Files:** Create `devcontainer_setup/project_sync/workflows.py`, `tests/test_workflows.py`; modify `project_sync/handoff.py`, `project_sync/scratch.py` only for read-only inventory and verified scratch flush/status.

**Interfaces:** `Workflows(project, state)`; `is_remote: bool`; `binding() -> dict|None`; `spaces() -> list[dict]`; `space() -> dict|None`; `roots(name) -> list[str]`; `pair(name, remote_root=None) -> dict`; `start_codespace() -> None`; `inventory(remote=False) -> dict` with `host`, `git` (`branch`, `head`, `status`), `sessions` (catalog rows plus `ownership`); `ensure_sync() -> dict`; `sync_ready() -> None`; `continue_session(agent, session_id, destination) -> int` (`destination` is `mac` or `codespace`); `new_session(agent) -> tuple[int,str]` creates and launches locally; `adopt(agent, session_id) -> None` local explicit adoption; `preferences() -> dict`; `remember(agent, session_id, location) -> None`.

- [ ] Write fail-first tests for safe private preferences, repo/branch filtered discovery, checkout discovery/identity validation and ambiguous roots, start requiring direct call, inventory project scope, Git mismatch and dirty guards, scratch reconciliation conflicts, transfer direction and reuse without redundant handoff, active/uncertain rejection, and interactive SSH launch command quoting.
- [ ] Run `python3 -m unittest discover -s tests -p test_workflows.py -v` and record expected failures before implementing.
- [ ] Implement service using existing endpoints and a read-only inventory RPC. `inventory` includes ownership for Codex/Claude. Preserve RPC allowlist, bounds, and existing failure codes. Use owned transport for discovery and interactive remote launch. Git mismatch errors name how to reconcile manually. Newly discovered remote conversations remain selectable from Mac.
- [ ] Add structured scratch inspection/flush using owned Mutagen session identity; verify synchronization and expose conflict paths without marking a running worker as synchronized. Keep background no-start boundary.
- [ ] Re-run covering workflow, transport, ownership, handoff, and scratch tests; commit only owned files when green. Write `.superpowers/sdd/work-launcher/backend-report.md` with commands, fail-first evidence, limits and commit.

### Task 2: Conversation catalog and terminal menu

**Files:** Modify `project_sync/sessions.py`, create `project_sync/work.py`, `project_sync/terminal.py`, `tests/test_work.py`, `tests/test_session_catalog.py`; modify `devcontainer_setup/ui.py` as a compatibility re-export only.

**Interfaces:** Add `title` and `updated_at` to existing `sessions.list_sessions` rows while preserving every existing key and strict project scope. Sort by recency. `work.run(project, state, *, ui=None, service=None) -> int` consumes Task 1's `Workflows`; lazily import it to allow independent UI tests. `terminal.Gum`, `terminal.Cancelled`, `terminal.gum_binary`, `terminal.cache_home` retain the existing wizard adapter API.

- [ ] Write fail-first catalog tests for Codex/Claude title and first-user-prompt fallback, missing title, recency, malformed history, control characters and unrelated project exclusion. Write menu tests with a fake UI/service for empty and remembered states, same-host resume, other-host resume, explicit start label, ambiguous Codespace/checkout selection, local new agent selection, adoption, running session guidance, errors/retry, cancellation, and remote-local operation.
- [ ] Run the new test files and record expected failures.
- [ ] Extend catalog metadata using bounded reads and terminal sanitization. Keep native import/export validation unchanged. Move Gum implementation to self-contained terminal module; preserve old wizard imports.
- [ ] Implement concise dynamic menu with primary continue here/there, other conversation, new conversation and settings/troubleshooting. Never display UUIDs as primary labels. Re-enter menu after a successful managed launch; remember before launching so interruption can retry. Remote host offers local continuation and tells user to use Mac for returning custody.
- [ ] Cover settings with readable status, sync start/stop and conflict paths; expose recovery via explicit adoption and actionable guidance, retain low-level CLI commands. Errors return to the menu with Retry/Back and do not auto-adopt or force-start.
- [ ] Re-run UI/catalog, project CLI and wizard tests, commit only owned files when green; write `.superpowers/sdd/work-launcher/menu-report.md` with evidence and commit.

### Task 3: Generator/setup integration and acceptance (root)

**Files:** Modify `project_sync/__main__.py`, `devcontainer_setup/__main__.py`, `render.py`, root/templates Taskfiles, README and project-continuity docs; add integration/wizard coverage.

- [ ] Write fail-first tests for `work` CLI dispatch and non-TTY refusal, generated namespaced/root work targets, existing root work preservation, setup default/upgrade preservation, post-launch automatic pair/start, commit-only deferral, and resumable pairing error after successful commit/push.
- [ ] Add `work` parser dispatch; use shared menu without importing consumer-unavailable wizard modules.
- [ ] Generate interactive `devcontainer:work` plus safe root alias. Add canonical `task work`. Keep existing lifecycle and low-level tasks.
- [ ] Default fresh sync selection to yes; preserve explicit selection. After successful Codespaces launch returns its name, pair verified discovered checkout and start worker. Explain deferred/retry path in plain language.
- [ ] Rewrite README daily flow around `task work`, retain advanced commands in detailed docs; document the Git check, supported conversation histories, one-writer rule and remote terminal behavior accurately.
- [ ] Run full tests and check, real generated-consumer terminal smoke, live isolated Mac/Codespace disposable conversation and scratch round trip with active-writer/Git/conflict/refusal checks; stop all test-owned processes and restore Codespace state.
- [ ] Commit green integration, independent whole-branch review against merge-base with origin/main, fix findings and rerun affected gates. Push branch, open PR, check CI, merge using prior delivery authorization, prune merged worktree/branch and verify canonical main clean.
