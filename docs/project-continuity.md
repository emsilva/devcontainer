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
| `task work:status` | Show scratch and conversation synchronization status |
| `task work:stop` | Stop this project's owned sync worker |

Run the connection tasks on your Mac. Inside Herdr, connection returns to your
shell; select the Codespace group to work remotely. From another terminal, it
opens local Herdr with that group available. Repeated entry reuses the existing
project workspace. The controller verifies checkout identity and does not alter
its branch or index.

Disconnect leaves remote sessions, local Herdr and synchronization running. It
needs no SSH or GitHub connection and is idempotent. Use `work:stop` separately to
stop synchronization. Setup preserves custom project Taskfile operations and
retires only verified producer-owned old work/agent launcher tasks.

## Scratch and project memory

Scratch sync excludes its tracked `.gitignore` and README markers, VCS metadata,
and transient tool caches. Deletions propagate. Simultaneous conflicting edits
remain for explicit resolution; neither side silently wins.

Shared project memory is Markdown under `.scratch/memory/`. Setup adds guidance
to AGENTS.md and CLAUDE.md, so ordinary native agent launches can read and maintain
those notes. Project Codex policy disables its shared global memory feature for
this workflow. Claude's project-local settings route auto-memory to
`.scratch/memory/claude/`, preserving unrelated local settings and any explicit
auto-memory enable/disable choice. A conflicting custom memory directory is
reported and preserved. No global profile or memory settings are changed.

## Conversations

The Mac worker captures verified stable prefixes ending at supported completed
turns, including required native ancestors. It transfers content-addressed blobs
and validates their hashes and project identity before publication. Interruption
leaves the previous successful receipt and source histories intact.

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

If an agent import is unsupported, scratch sync can continue independently.
Inspect `task work:status`; scratch connection health alone does not prove that
all conversations have been published.

## Availability and migration

A sleeping, disconnected or powered-off Mac receives nothing. The last successful
conversation receipt and actual scratch status determine what is locally
available. The polling delay is not a recovery interval guarantee: capture,
validation and transfer take additional time.

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
spend credits. Run `task work:cs` again after a Mac reboot.

Connections wait up to 45 seconds for a successful SSH handshake and separately
up to 45 seconds for the saved Herdr machine's remote API after startup. Only
read-only readiness probes are retried; workspace creation and focus run once.
An owned SSH alias uses GitHub's native SSH proxy with a dedicated strict host
key. Herdr activity reports Codespace activity; silent background sync retains
the configured idle timeout. Existing SSH and Herdr global settings are preserved.

Advanced commands remain `task scratch:configure -- --codespace NAME --remote-root
/workspaces/PROJECT`, `scratch:start`, `scratch:status`, `scratch:logs`,
`scratch:backup`, and `scratch:stop`. Scratch backups contain scratch payloads only.
Historical full-checkout recovery evidence is superseded by this design.
