# Project scratch, memory, and conversation continuity

The owner authorizes autonomous spikes, implementation, live tests with minimal
real agent turns, and a branch/PR/merge in `emsilva/devcontainer`. Include automatic
two-way scratch sync. Preserve the normal Codex and Claude profiles. Continue the
same conversation with one writer at a time. Transfer only the selected project's
memory. Do not merge the Mainstage documentation branch.

## Product behavior

Projects receive tracked `.scratch/.gitignore` and `.scratch/README.md` markers.
Their working contents are Git-ignored. Project notes live in
`.scratch/memory/`; they are maintained by agents according to managed blocks in
the project's `AGENTS.md` and `CLAUDE.md`, and can be shared across tools.
Instruction blocks use project-relative paths and preserve existing guidance.
Native global Codex memory is disabled for newly created project chats using a
small managed project configuration block, without changing the user's global
settings. Already injected historical context is not silently rewritten.

The setup wizard offers project continuity. This adds reusable Taskfile targets
for scratch configuration/start/stop/status/logs/backup and agent launch/list/
handoff. Tool installation is lazy and checksum-pinned. Python remains the only
required host runtime besides the existing GitHub CLI/SSH prerequisites.

Scratch configuration selects one existing Codespace and its checkout. It
validates repository identity before binding the pair. The Mac runs a detached
supervisor; ordinary shell exit does not stop it. The supervisor waits when the
Codespace is stopped and resumes after it becomes available. It does not start a
Codespace, register a system login service, or modify global SSH configuration.

## Identity and local state

Derive repository identity from the normalized origin URL, including host and
owner/repository. GitHub SSH and HTTPS spellings map to the same identity. The
directory basename is not identity. Worktrees share project memory but retain
their exact runtime checkout selection and Git metadata.

Host-local registry, process leases, receipts, logs, backups, and pinned tools
live under `${XDG_STATE_HOME:-~/.local/state}/devcontainer-setup/projects/<id>/`.
Invalid/relative XDG overrides fall back to the home directory. State files are
private, atomic, and reject unsafe symlink/path traversal. No token or profile
database is written into the project or synchronization payload.

## Scratch transport

Use Mutagen `two-way-safe` with SHA256 and its normal conflict reporting.
Only `.scratch/` contents are synchronized; tracked marker files, caches, VCS,
and owned handoff/control state are excluded. Never use automatic winner-takes-
all conflict resolution. Deletions propagate and conflicts preserve both sides.
Provide explicit recovery snapshots, including a snapshot before the initial
sync and before a handoff. Snapshot names are immutable and remain outside Git.

Mutagen uses ordinary SSH through a private loopback forward to Codespace SSH
port 2222. Keep daemon state isolated from any existing Mutagen installation.
The background GitHub CLI receives a process-local `GITHUB_API_URL` pointing at
an authenticated loopback API adapter. That adapter forwards only the bound
Codespace's read endpoint to GitHub and rejects every mutation. It keeps the real
GitHub token in memory and gives the child an adapter-only token. Therefore a
race between an availability check and CLI startup cannot issue `/start` to
GitHub. The port-forward path sends no activity RPC keepalive.

Start/stop are idempotent. Terminate only owned processes and sessions, verify
process identity before signaling, and keep logs/status useful after failures.
Network loss and Mac sleep must not discard state. Unsupported case collisions
or symlinks must remain visible rather than silently overwrite data.

## Conversation handoff

Evaluate pinned `codex-claude-transfer` against synthetic native histories and
record its failure modes. Source audit at commit
`13a84383a5e00db61d527e782f36f4bbf0c707d2` found inherited-fork merge refusal,
unsafe active-writer assumptions, nonfatal conflict exit codes, and memory
selection/redaction gaps. It is not a production write dependency. Use the
narrow native snapshot/import path proven by the earlier Codex round trip.

Select exact session UUIDs, never the latest file. For Codex, include required
history ancestors after checking identity/scope. Snapshot complete JSONL records
with digests and Git/project metadata; recognize compressed files explicitly or
refuse them with a concrete explanation. Preserve historical message bytes.
For Claude, retain session IDs and structural events and place the transcript
under the destination project's native directory. Amended 2026-10-24 (#40): a
Claude transcript is checked only for project scope, memory markers and session
identity; record, attachment and request-cache formats are copied as they are.
Subagent/task state is not carried yet (#52); the transcript syncs without it.

Import accepts absent files, identical files, or a proven append-only descendant.
It preserves a compatible destination that is already ahead and refuses
divergent histories. Back up existing files before replacement. Validate the
entire bundle before writing. Stage changes and persist a recovery receipt so an
interruption can be inspected/retried. Native SQLite and credential files never
cross hosts. Codex uses its own APIs to rebuild indexes and compare all history
pages. Runtime cwd/workspace roots come from the destination, not search/replace
over old conversation text. Fresh project-note context is supplied on launch.

Managed agent launches hold a per-session/project-tool local lease. Handoff
requires a quiescent source and destination and transfers durable ownership with
a nonce/receipt. Offline or uncertain ownership cannot be taken over implicitly.
Do not kill an existing user conversation. Detect foreign active writers where
possible and refuse uncertain operations. Plain native CLI bypass remains an
explicit boundary: the documented managed entrypoints enforce the protocol.

Legacy transcripts can contain global memory or unrelated paths already injected
into history. The importer must flag this and refuse a strict project-only
export when scope cannot be established; it must not silently redact/rewrite the
historical conversation. New project launches use the project memory policy.

## Integration and compatibility

Use the generator's existing GitPlan review/commit transaction. Keep whole-file
ownership for generated `.devcontainer` helpers; use separately owned blocks for
mixed-ownership root instruction/config files. Existing user edits, Taskfiles,
hooks, custom features, and staged changes survive. Modified managed blocks
require review rather than silent replacement. Existing installations remain
valid until continuity is selected/configured; repeat generation is identical.

Distribute Python helpers with the authenticated generator archive and as managed
container helpers. Keep the canonical implementation in one source location.
Pin third-party binaries by version, platform, and SHA256; do not use `latest`,
pipe remote installers to a shell, or require Go/Node to run the feature.

## Acceptance evidence

1. Existing wizard suite and ShellCheck pass; add tests before each new behavior.
2. Gitignore survives `git add --all`; mixed ownership and reruns preserve edits.
3. Two unrelated repositories with identical basenames stay isolated.
4. Native Codex/Claude same-ID round trips retain all supported history and correct
   cwd; verify project notes reach actual model requests and minimal real turns.
5. Root and inherited-fork cases, parent/subagent dependencies, legacy/paginated
   history, malformed/truncated data, wrong IDs/projects, and version gaps get
   explicit success or refusal evidence.
6. Divergence, local-ahead, interrupted transfer, concurrent launch/handoff,
   recovery, and unknown ownership preserve existing data.
7. Live scratch create/edit/rename/delete in both directions, Unicode/spaces,
   binary/large files, conflict preservation, reconnect, and no outside-root
   changes pass. Exercise the stopped-state race against a fake API and prove no
   upstream mutation; verify actual stopped Codespace stays stopped.
8. Real detached Taskfile workflow, logs/status/stop, fresh-project bootstrap,
   container lifecycle, normal profile/auth preservation, and adjacent wizard
   behavior pass before PR/merge. Root independently reruns gates and smoke.
9. Remove only owned fixtures/processes, stop the test Codespace, keep checksum
   receipts, and prune merged implementation worktrees/branches after landing.
