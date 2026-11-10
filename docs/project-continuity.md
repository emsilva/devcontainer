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
| `task work:signin:all` | Start or retry sign-ins to every tool on the paired Codespace |
| `task work:signin` | Finish waiting Codespace browser sign-ins on this Mac |
| `task work:disconnect` | Disconnect only this project's saved Codespace machine from local Herdr |
| `task work:status` | Show a short offline scratch and conversation summary |
| `task work:status -- --json` | Show cycle, stage, receipt and publication detail as JSON |
| `task work:stop` | Stop this project's owned sync worker and its keeper |
| `task work:cs:armed` | Connect as `work:cs`, then arm restart after GitHub's 12-hour stop |
| `task work:hold -- 09:30` / `-- off` | Hold restarts and resume prompts until a local time / end the hold |
| `task work:disarm` | Stop this project's keeper and its keep-alive; sync continues |
| `task work:cs:stop` | Record your stop, then stop the Codespace, from your Mac or through its connected Mac worker |
| `task work:cs:restart` | Stop and start the Codespace now, outside the restart budget |
| `task work:cs:delete [-- --codespace NAME]` | Check the named Codespace (the pairing by default), then delete only after a typed `delete` |
| `task work:logs` | Show the last keeper and worker log lines, merged by time |

Run the connection tasks on your Mac. Inside Herdr, connection returns to your
shell; select the Codespace group to work remotely. From another terminal, it
opens local Herdr with that group available. Repeated entry reuses the existing
project workspace. The controller verifies checkout identity and does not alter
its branch or index. Local Python, tested GitHub CLI, selected credentials, SSH
key and Herdr prerequisites are checked before starting a stopped Codespace.
Herdr opens once the current verified scratch pairing is synchronized and SSH
is prepared. Before Herdr opens, `work:cs` checks Herdr's resume hooks for
Claude, Codex and OpenCode on the Codespace, and installs any that are missing
or outdated, so that a restart can resume the agents. The same step also
installs the Herdr skill that matches the Codespace's Herdr. The same step also
links `~/.claude/handoff-feedback` on the Codespace to the checkout's
`.scratch/handoff-feedback`, so that feedback syncs to the Mac. `work:status`
shows the last check. The same step copies the checkout's gitignored `.env`
files the Codespace lacks and never overwrites an existing file. `work:status`
shows that check. Conversation exchange runs in the worker; pending or refused
history is reported without blocking the connection.

Disconnect leaves remote sessions, local Herdr and synchronization running. It
needs no SSH or GitHub connection and is idempotent. Use `work:stop` separately to
stop synchronization. Setup preserves custom project Taskfile operations and
retires only verified producer-owned old work/agent launcher tasks.

## Sign-ins

Connect with `task work:cs`, then start or retry with `task work:signin:all`.
To do both before Herdr opens, run `task work:cs -- --then-signin-all`. The
sequence waits for the link helper on both machines before starting each login.
If it cannot reach the Mac, reconnect with `task work:cs` and retry.

Plaud, Codex, Claude, OpenCode and Grok run in that order in your terminal on the
Codespace, opening their approval pages on this Mac. Choose one Claude account
in the browser; its email appears in the result. For Grok, enter the code shown
in the terminal. Credentials stay on the Codespace. The Mac saves only states,
check times and Claude's email in its private project state.

Existing sign-ins, including other authentication methods, are kept. A tool
whose status cannot be checked is skipped; only a confirmed signed-out tool
starts a login. Failed approvals continue to the next tool; the summary names
what to retry with `task work:signin:all`. Ctrl-C stops the sequence and keeps
the completed results. Missing tools are listed and skipped.

`task work:signin` finishes waiting sign-ins when the worker is disconnected.
The connected worker already handles those callbacks. It remains the callback
recovery command; use `task work:signin:all` to start new sign-ins.

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

The same guidance treats a Codespace as disposable: anything worth keeping goes
in `.scratch/` as it is made, with working files in `.scratch/tmp/`. Agents in a
Git worktree write to the main checkout's `.scratch/`, since only that one syncs.
`tmp/` holds only regenerable output. Setup replaces an unedited guidance block
when the wording changes; an edited block stops for review.

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

Passing the version check admits a release for validation only. The record,
scope, ancestry and completed-turn checks decide which histories can sync;
an unknown Codex record type is refused.

A replacement Codespace receives the conversations its predecessors delivered to
the Mac, including ones the Mac did not publish locally. A conversation is never
copied back to the Codespace it started on. Later sync cycles retry delivery
without creating duplicate imports.

## Replace a Codespace

On your Mac, run `task setup -- --replace` in this repository, or
`task devcontainer:setup -- --replace` in a generated project. Both commands need
a terminal and a real gh account, with `GH_TOKEN` and `GITHUB_TOKEN` unset.
`task work` offers `Replace NAME` after connect or start. The setup wizard offers
`replace NAME` for a paired Codespace it can read, including one on a feature
branch; it publishes any reviewed setup changes before starting the replacement.

Replacement runs in this order:

1. Inspect the paired Codespace's live machine, cores, idle timeout and billing
   owner. Unreadable resources stop the operation before creating anything. The
   default branch must carry this checkout's installed setup.
2. Create the new Codespace from the default branch's tip, using the old one's
   machine and idle timeout. The old one's branch may differ. Validate and apply
   dotfiles to the new one. A dotfiles retry does not rebuild it.
3. While the old one is still paired, decide recovery consent, ask you to end any
   running agents, then record its final normal sync cycle and conversation flush.
   The command never kills agents. Unfinished scratch or history stops the move.
4. Move sync, recovery and Herdr to the new Codespace without opening Herdr's TUI.
   Matching GitHub account and billing owner carry your recovery consent; otherwise
   you type `arm` before the final flush. Nothing asks between that flush and move.
5. Sign in through the existing all-tool sequence. Failed or skipped sign-ins are
   listed for `task work:signin:all`; they do not block the next check.
6. Check setup validation, a completed sync cycle, recovery when armed, Herdr
   reachability and the old one's conversation revisions on the new one. Refused
   conversations kept on this Mac and explicitly forgotten conversations are named.
   A failed check keeps the old Codespace and stops before offering deletion.
7. Offer the old one's [checked deletion](#checked-deletion), with fresh evidence
   against its final flush and the typed word `delete`. Declining keeps it listed
   in `task work`. Connect to the new one with `task work:cs` when finished.

The project keeps a private replace journal and one operation lock. Rerun the same
replace command after interruption: it prints its phase and continues it.
`task work` shows `Finish replacing NAME` ahead of other work, below a recovery
connection when recovery is off. After a lost create reply it checks the inventory
before offering `continue` or `abandon`. One matching new Codespace is adopted;
several candidates or unreadable candidates require a choice. If none appears
during the two-minute recheck, `continue` leads to the typed `create` that alone
allows another create request.

A rerun with the old Codespace still paired records a fresh final flush, unless it
is finishing publication of a partial move. If OLD changes while the flush is
recorded, it prints `OLD changed while its final flush was recorded; nothing moved.`
When sync is paused and OLD remains paired, it prints
`Sync with OLD is paused. Run the replace again, or task work:cs to resume it.`

Before the move starts, a rerun offers `continue` or `abandon`. Abandon keeps the old
pairing and leaves any new Codespace listed for an explicit checked delete; nothing
deletes it automatically. Ctrl-C or EOF keeps the current phase for a rerun. Once
the move starts, reruns finish forward, including after a partial move. Returning
to the old Codespace then requires an explicit `task work:cs -- --rebind NAME`.
Deletion of either Codespace ends that replacement operation. A completed replace
keeps its archived journal, even when you declined the old one's deletion.

## Checked deletion

Replace first, then check the old Codespace with
`task work:cs:delete -- --codespace NAME`. Run it on your Mac: it verifies the
repository and the GitHub account recorded when the Codespace was connected or armed,
checks the Codespace and prints a verdict for
Git, Scratch, History and Agents. Every category must be safe. Type `delete`
in the terminal to authorize deletion; the command checks everything again
before recording the stop, waiting for Shutdown and deleting it. It confirms
deletion with GitHub's inventory before cleaning up.

Git covers every repository and linked worktree under `/workspaces`: changes,
untracked files, stashes and commits that have not reached GitHub. Scratch and
Codex/Claude conversations must have reached this Mac; an unfinished turn blocks
deletion. A retired Codespace is compared with the final flush recorded when it
was replaced. Running Claude, Codex, Grok or OpenCode agents also block deletion.
Conversations kept on this Mac that the replacement refused are named in the
verdict and do not block it.

The verdict lists what is not checked: files Git ignores, home-folder data
outside sync, and Grok and OpenCode conversations. Nothing exports those files.
For agents or a headless terminal, run
`task work:cs:delete -- --codespace NAME --check --json`; it only checks and
makes no changes. Without `--codespace NAME`, it checks the current pairing.

A stopped or unreachable Codespace reads Unknown. Start a retired one explicitly
with `task work:cs:delete -- --codespace NAME --start`, then run the check again;
use `task work:cs:restart` for the current pairing. The check never starts one
silently. A Codespace moved with `--rebind` has no final-flush record and cannot
be deleted this way.

Deleting the current pairing turns recovery off, stops its keeper, sync worker
and forwards, archives the binding, and removes its owned SSH alias and Herdr
profile. An adopted Herdr profile is kept. Deleting a retired Codespace leaves
the new pairing and its recovery alone. Either path ends the deleted
Codespace's paused sync session, while retaining scratch payloads, conversation
checkpoints, relay records and the restart ledger. If cleanup is interrupted,
rerun the same delete command in a terminal to finish it. `task work` offers
one pending cleanup and the oldest listed retired Codespace with a final-flush
record; it never offers deletion of the current pairing.

## Conversation availability

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
GitHub refuses the request. It runs without a terminal on your Mac. Inside the
Codespace it asks the connected Mac worker to record and carry out the stop;
the Codespace's `GITHUB_TOKEN` is not used for this. The worker must support
stop requests; an older worker or a sign-in-only connection is insufficient.
With no capable worker it prints "No Mac worker is connected to this Codespace,
so nothing stops" and leaves no request; the idle timeout still applies.
On success it prints "Your Mac recorded this as your stop" before the result.
If the Mac has not taken the request within 60 seconds, it removes it and
prints "Your Mac did not take the stop request within 60 seconds, so nothing
stops." If the Mac took it but has not answered, it says so and directs you to
`task work:status` on your Mac. An uncertain removal has the same outcome.
Ctrl-C prints "Cancelled; nothing stops." only when it removes the request;
otherwise it prints "Cancelled, but your Mac already took the stop request."
An expired request cannot run later.

`task work:cs:restart` stays Mac-only and requires a terminal. It restarts
outside the budget; for an armed project the keeper then restores Herdr and
tells the lead. The Mac's stop and restart use the armed account, or the active
gh account for an unarmed project, and refuse while `GH_TOKEN` or `GITHUB_TOKEN`
is set on the Mac. Run task work:cs after a Mac restart; for an armed project it starts recovery again. Until then recovery is off.
Arming refuses while another gh process, such as a project's own `cs:ports` forward or a mirror script, holds this Codespace's link; stop it first.
The managed notes tell agents in the Codespace which ports reach the owner, and
the Mac URL for each.

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
