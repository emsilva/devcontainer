# Scratch-only continuity implementation plan

> **For agentic workers:** Use subagent-driven-development for the independent native publication and continuity controller tasks, followed by Root integration and live validation.

**Goal:** Use one existing checkout per host, keep Git manual, and automatically synchronize only `.scratch/` and project-scoped agent conversations/memory while ordinary `codex`/`claude` commands remain usable.

**Architecture:** Mutagen retains the two-way scratch transport. Immutable, verified conversation checkpoints provide bounded native history imports in each direction; no Git bundle, index, tracked files, or untracked files outside scratch enter them. The Mac controller connects Herdr to the existing Codespace repository and runs the detached continuity worker.

**Tech stack:** Python standard library, Bash, Task, GitHub CLI, Herdr, Mutagen, native Codex app-server for metadata-only imports and Claude transcript format.

## Global constraints

- Git operations remain manual. Continuity never commits, pushes, pulls, switches branches, stashes, resets, restores source files, or creates a checkout.
- Existing generated checkouts and previous checkpoint data are preserved during migration; do not silently discard their work.
- Only `.scratch/` payloads and verified project conversations are transferred. Shared project memory is `.scratch/memory/`, exposed through project AGENTS.md/CLAUDE.md instructions. Do not replicate credentials, settings, global SQLite files, global memory, unrelated projects, or whole native homes.
- A conversation has one active writing host. Never overwrite an existing native conversation; published imports use new native IDs. Read running sources only through validated completed prefixes.
- Unchanged imports do not create new native forks or echo back indefinitely. New completed turns remain transferable.
- Keep strict host keys, no-start background transport, bounded waits, saved-machine reuse, and independent disconnect behavior.
- Remove redundant `work`, `work:codex`, `work:claude` tasks and launchers; normal agent commands and their native resume pickers are the user interface.
- Work in this isolated source branch. Preserve user-owned Taskfile entries, instructions and settings. No global runtime setting changes or attribution trailers.

## Task 1: Publish conversations for native resume commands

**Owner/files:** Native worker owns `project_sync/conversations.py`, `sessions.py`, `agents.py`, and native/session tests. Root owns the remaining files.

**Interface:** Add `publish_conversations(project, store, snapshot_id, *, state=None, env=None) -> dict` with `imported`, `unchanged`, and `issues` lists. Consume schema-2 manifests containing `project` and `conversations`, no `files`. Preserve existing capture API and validation of supported native closure. The RPC/controller invokes this API on either destination; it must not launch an interactive agent or a model turn.

- [ ] Write failing tests for idempotent picker-ready imports, metadata-only changes, conflicting native identities, completed-turn advance and echo suppression.
- [ ] Codex: publish a separate new-ID selected-leaf transcript with destination metadata; preserve retained history rows and dependency ancestor bytes/offsets. Use native no-turn read/resume metadata operations once to index that exclusively owned import. No developer context injection or automatic resume of completed publications. Native metadata-only forks were tested and hidden by the native session list, so do not use that path.
- [ ] Claude: materialize a new standalone session with destination cwd/namespace and new session ID while preserving messages, ancestry and cache/request-dependent fields. Verify native discovery with the official SDK; it discovers standalone transcripts without synthetic index files.
- [ ] Journal owned imports safely. Derive stable revision keys from semantic content/cutoff/closure, excluding mtime/display fields. Suppress unchanged imported sources/dependencies/forks from capture; allow newly completed turns to be exported.
- [ ] Remove dead managed launch/argv/memory injection functions after callers are retired by Root; retain no-turn client/history utilities.
- [ ] Run native/session covering tests, commit only owned paths, write report under ignored `.superpowers/sdd/`.

## Task 2: Conversation-only sync and existing-checkout controller

**Owner/files:** Continuity worker owns `replication.py`, `rpc.py`, `workspace.py`, `scratch.py`, `codespaces.py`, deletion of `project_files.py` and focused replication/workspace/scratch/project-files tests.

**Interfaces:** `capture(project, store, *, env=None)` creates schema-2 conversation-only manifest. `validate_manifest` rejects legacy full-repository manifests for new transfers while retaining old stored data untouched. `sync_project_data(connection, project, state, remote_root)` transfers local conversations to remote and publishes them, then captures remote conversations and publishes them locally; returns the successful local receipt plus publication/issue status. RPC `publish` calls Task 1's publisher. RPC `restore` is removed.

- [ ] Write failing tests proving no source/Git/index objects enter a checkpoint, no repository mutation/restore API exists, and both import directions run with retry-safe behavior.
- [ ] Reuse verified object transport and store. Remove file capture and restore dependency. Do not weaken digest, identity, size or interrupted-transfer checks.
- [ ] Replace background code checkpoints with the two-way conversation cycle while keeping scratch synchronization independent and conflict-preserving. Call notes.configure_memory(project) on local connection and remote publish to route native Claude memory through the project-local setting. Upgrade the runtime producer version so old workers are replaced normally.
- [ ] Connect Herdr to a verified existing, non-generated Codespace checkout; filter `.devcontainer-work` candidates. Existing checkout and dirty index remain untouched; branch differences do not trigger automatic Git operations.
- [ ] Migrate legacy registry/bindings without using generated or recovered working copies. Pause and retain the old verified owned Mutagen session/baseline during endpoint retarget, rather than terminating it; retain config/receipt archives and safely reconcile pending legacy retarget intents. Preserve legacy state/directories. If original checkout cannot be identified uniquely, show available choices rather than creating or overwriting a directory.
- [ ] Local status stays available offline; report actual scratch and conversation synchronization health instead of code-recovery promises. Remove unused local recovery and agent-launch branches.
- [ ] Run focused replication/workspace/scratch tests, commit owned paths, write report.

## Task 3: Task surface, generation, documentation and live migration

**Owner/files:** Root owns `__main__.py`, `root_tasks.py`, render/retired-file manifests, root and template Taskfiles, project-sync CLI, generated instructions, README and continuity docs, their integration tests, and consumer delivery.

- [ ] Add fail-first task/generation tests for exactly the new daily surface: work:cs, work:disconnect, work:status, work:stop; retain advanced scratch operations and app lifecycle tasks. Preserve owner-defined tasks and remove only checksum/exact producer-owned retired tasks/files.
- [ ] Remove ordinary work/agent launch options and custom resume interface. Plain native commands read shared project notes through existing scoped project guidance.
- [ ] Refresh README and current continuity documentation: manual Git, existing checkout, native resume, supported history limits, one writer, awake Mac, conflicts and actual sync receipts. Mark older full-code recovery documents as superseded where retained as historical evidence.
- [ ] Run Root full suite, host validation, Task/check gates; obtain independent whole-branch review against origin/main merge-base.
- [ ] Live disposable-profile tests on Mac and existing Codespace: native picker/list discovery, completed turns in both directions, unchanged no-echo cycles, scratch two-way edits/conflicts, source/index unchanged, expected tools preserved, reconnect and disconnect reuse.
- [ ] Deliver source branch/PR/merge after gates and CI pass, prune merged worktree/branch. Refresh/push only Mainstage's current docs branch. Preserve legacy extra checkout; point actual sync/Herdr at original checkout without copying source code.

## Acceptance

The native `codex`/`claude` commands see published supported conversations; project notes and scratch edits sync; no code/index/Git state transfers or generated checkouts occur; unchanged background cycles do not create more conversations; saved-machine reconnect/disconnect and ordinary project operations continue working. Root personally reruns gates and real smoke tests before merge.
