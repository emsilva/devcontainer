# Project continuity

Work in the existing repository checkout on your Mac and in Codespaces. Run
`codex` or `claude` normally, and use `codex resume` or `claude --resume` to choose
a native conversation. Continue a given conversation on one host at a time.

## What moves between machines

| Data | Transfer |
| --- | --- |
| Source code, commits, branches, staged and unstaged changes | Git operations you perform yourself |
| `.scratch/` payloads, including `.scratch/memory/` | Automatic two-way safe sync |
| Supported completed project conversation turns | Verified native session publication in each direction |
| Credentials, global settings/databases and personal memory libraries | Remain on each host |

Sync does not commit, push, pull, switch branches, stash, restore source files, or
create checkouts. Uncommitted code stays on the host where it was edited.

## Daily commands

| Command | Result |
| --- | --- |
| `task work:cs` | Start/connect the paired Codespace, start sync, connect its saved Herdr machine |
| `task work:disconnect` | Disconnect only this project's saved Codespace machine from local Herdr |
| `task work:status` | Show a short offline scratch and conversation summary |
| `task work:status -- --json` | Show cycle, stage, receipt and publication detail as JSON |
| `task work:stop` | Stop this project's owned sync worker and its keeper |
| `task work:cs:armed` | Connect as `work:cs`, then arm restart after GitHub's 12-hour stop |
| `task work:hold -- 09:30` / `-- off` | Hold restarts and resume prompts until a local time / end the hold |
| `task work:disarm` | Stop this project's keeper and its keep-alive; sync continues |
| `task work:cs:stop` | Record your stop, then stop the Codespace; it is never restarted |
| `task work:cs:restart` | Stop and start the Codespace now, outside the restart budget |
| `task work:logs` | Show the last keeper and worker log lines, merged by time |

Run the connection tasks on your Mac. Inside Herdr, connection returns to your
shell; select the Codespace group to work remotely. From another terminal, it
opens local Herdr with that group available. Repeated entry reuses the existing
project workspace. The controller verifies checkout identity and does not alter
its branch or index. Local Python, tested GitHub CLI, selected credentials, SSH
key and Herdr prerequisites are checked before starting a stopped Codespace.
Herdr opens once the current verified scratch pairing is synchronized and SSH
is prepared. Conversation exchange runs in the worker; pending or refused
history is reported without blocking the connection.

Disconnect leaves remote sessions, local Herdr and synchronization running. It
needs no SSH or GitHub connection and is idempotent. Use `work:stop` separately to
stop synchronization. Setup preserves custom project Taskfile operations and
retires only verified producer-owned old work/agent launcher tasks.

## Scratch and project memory

Scratch sync excludes the root `.gitignore` and `README.md` markers, VCS metadata,
and transient entries named `.DS_Store`, `__pycache__`, `.venv`, `node_modules`,
`.cache`, `.project-sync`, `.handoff`, or `.mutagen` at any depth. Excluded trees
are pruned before validation, so links inside tool caches do not block startup.
Included links, special files, case collisions and a linked `.scratch` root are
refused on both endpoints; unreadable included directories fail validation.
Deletions propagate. Simultaneous conflicting edits remain for explicit resolution; neither side silently wins.

Shared project memory is Markdown under `.scratch/memory/`. Setup adds guidance
to AGENTS.md and CLAUDE.md, so ordinary native agent launches can read and maintain
those notes. Project Codex policy disables its shared global memory feature for
this workflow. Claude's project-local settings route auto-memory to
`.scratch/memory/claude/`, preserving unrelated local settings and any explicit
auto-memory enable/disable choice. A conflicting custom memory directory is
reported and preserved. No global profile or memory settings are changed.

Before changing Claude's memory path, setup copies and verifies this repository's
existing native memory index and topic files. The originals remain intact.
Already redirected empty scratch directories are backfilled too. Conflicting
notes or custom memory locations stop migration without replacing either copy;
inspect `task work:status` before retrying. A completed migration is recorded
locally, so later edits and deletions in scratch are preserved. An interrupted
note copy can leave an incomplete destination file: resolve that reported
conflict using the retained original before retrying. Restart Claude after the
first migration so the next session loads the copied index.

## Conversations

The Mac worker captures verified stable prefixes ending at supported completed
turns, including required native ancestors. It transfers content-addressed blobs
and validates their hashes and project identity before publication. Interruption
leaves the previous successful receipt and source histories intact. Each direction
has its own capture, transfer and publication result. A failure in one direction
still allows the other direction to acquire a receipt and publish history.
Invalid checkout identity or authentication fails the connection verification.

Native imports use new IDs in the destination host's ordinary agent home and
checkout namespace. Native Codex metadata operations prepare imported history without a
model turn; Claude imports retain validated message content and ancestry. Use
the normal native resume picker to select an imported conversation. Unchanged
revisions are deduplicated, and unchanged imported histories are not echoed back
as new conversations. Locally continued or active sessions are preserved.

Only project-scoped conversations are eligible. Histories with unrelated-project
or global-memory context can be refused. Unsupported child-agent/sidecar history
or other unvalidated native formats are reported. Start agent conversations
from the checkout root so its scope can be established. An unfinished turn may
not be available until its next supported completion.

Native Codex can hide automated `exec` sessions and histories containing only
inherited turns from its default resume picker. Such imports are reported as
issues; they are not counted as successfully available conversations.

If an agent import is unsupported, scratch sync can continue independently.
Inspect `task work:status`; scratch connection health alone does not prove that
all conversations have been published.

## Availability and migration

A sleeping, disconnected or powered-off Mac receives nothing. Status separates
received bytes from verified native availability. Received history has a
snapshot-bound receiver receipt; available history has a matching successful
native publication result. Received history awaiting publication is pending.
Capture-refused histories count once as unsupported, with their capture issues
listed separately from publication failures. Inventory errors without a session
ID are reported as issues, rather than counted as histories.

Counts describe the current cycle's unique source conversations, rather than a
lifetime total or everything in an old manifest. Full JSON includes each
snapshot, receiver receipt, publication result, cycle ID and timestamps. Previous
verified receipts remain visible separately. A new, partial, failed or stale
cycle never inherits a previous healthy result. Old native imports remain on
disk, but stale status cannot assert their current availability.

The worker updates its heartbeat at progress and connection checks. Idle progress
expires after 30 seconds; an active conversation stage is bounded at 240 seconds,
and completed results expire after 300 seconds. Each remote RPC has its own
180-second limit. These are failure/freshness bounds, not recovery guarantees:
capture, validation and transfer add to the polling delay. Waiting or conflicting
scratch state never reports a ready connection or healthy synchronization.

When Codespaces is unavailable, use your existing Mac checkout and the ordinary
native agents. Imported conversations and received scratch files remain available.
Commit/push/pull code through Git; conversations cannot restore uncommitted code.

Older versions created remote `.devcontainer-work/` and private local `recovered/`
checkouts. Migration selects a verified ordinary checkout and preserves those
old directories and private checkpoint data. It never merges their code or
indexes into the active checkout. Old scratch bindings are retained for review;
new endpoints use their own safe reconciliation baseline. Inspect retired
checkouts before manually deleting them.

## Transport and advanced controls

The foreground connection command can start a stopped Codespace. The detached
worker has a GET-only GitHub API capability and cannot restart a Codespace or
spend credits. The keeper of an armed project is the one background process that
can start a Codespace. It does so only after a stop it classified as GitHub's
12-hour stop, at most 2 restarts in any 24 hours and at least 30 minutes apart,
never during a hold and never for a stop that began in one. It then ends any
stale Herdr proxy, waits up to 10 minutes for Herdr, and sends the project's one
`lead-*` agent the resume message, unless a hold began meanwhile. Every other
outcome, a Codespace that something else started, and every stop it does not
restart except your own, gets one Mac notification that names what to run.
`task work:cs:stop` records your stop first, so it is never restarted, even when
GitHub refuses the request. `task work:cs:restart` restarts with you present,
outside the budget; for an armed project the keeper then restores Herdr and
tells the lead. Both run on your Mac, use the armed account, or the active gh
account for an unarmed project, and refuse while `GH_TOKEN` or `GITHUB_TOKEN` is
set. Run `task work:cs` (or `task work:cs:armed`) again after a Mac reboot.
Arming refuses while another gh process, such as a project's own `cs:ports` forward or a mirror script, holds this Codespace's link; stop it first.

Connections wait up to 45 seconds for a successful SSH handshake and separately
up to 45 seconds for the saved Herdr machine's remote API after startup. Only
read-only readiness probes are retried; workspace creation and focus run once.
An owned SSH alias uses GitHub's native SSH proxy with a dedicated strict host
key. Herdr activity reports Codespace activity; silent background sync retains
the configured idle timeout. Existing SSH and Herdr global settings are preserved.

Advanced commands remain `task scratch:configure -- --codespace NAME --remote-root
/workspaces/PROJECT`, `scratch:start`, `scratch:status`, `scratch:logs`,
`scratch:backup`, and `scratch:stop`. Scratch backups retain included scratch
payloads and the root marker files even when Git ignores them. They exclude the same transient entries as sync, including
cache links. Backups remain private and immutable; they contain no checkout or
conversation data.
Historical full-checkout recovery evidence is superseded by this design.
