> Historical full-checkout recovery design/evidence. Superseded by the scratch-only continuity design in [project continuity](../../project-continuity.md).

# Herdr workspaces and continuous recovery

Approved intent: `task work:cs` connects to a project Herdr workspace in Codespaces;
`task work` opens local work. Project files, scratch, notes and completed agent
conversation turns must already be recoverable on the Mac when Codespaces becomes
unreachable. The owner explicitly requested removal of superseded/dead code.

## Daily behavior

Use two primary commands: `work` and `work:cs`. Optional `work:codex` and
`work:claude` start fresh on the current host; `--resume` opens a project-only
picker with native and checkpoint conversations. Remove their cross-host variants
and the daily menu. `work:status` reports the last locally acknowledged recovery
checkpoint, conversation limitations and scratch conflicts. `work:stop` stops only
this project's Mac replication worker.

Remote entry discovers/selects the Codespace, explicitly starts it if needed,
retains its prior work in a checkpoint, and makes the Mac's current project
snapshot/context available in a separate persistent `/workspaces` checkout. It
registers/reuses an owned Herdr machine and named project session and focuses the
matching workspace. Repeated entry reuses that working copy rather than replacing
ongoing work. Local entry requires no Codespace contact: when switching back it
materializes the latest acknowledged remote snapshot in a new local checkout,
then reuses/focuses a local Herdr workspace there. Existing checkouts and histories
are never reset, merged or overwritten.

Herdr's installed CLI cannot select a saved remote machine in an existing client.
Inside Herdr, remote attachment therefore uses an explicit child-only configuration
with `experimental.allow_nested=true`; all global settings and `HERDR_ENV` remain
unchanged. A dedicated owned SSH alias/include lets the saved machine remain usable
from existing clients too. The alias points only to the live owned forwarding
worker and uses its private verified host-key store. Background reconnects never
start a Codespace. Missing/incompatible remote Herdr uses its official setup flow.

## Recovery data

Private state remains in the established project namespace outside Git. A
content-addressed store holds immutable SHA256 blobs and manifests. A receiver
acknowledges a manifest only after validating every referenced blob and durably
writing it. Unchanged snapshots reuse blobs/manifests. Source timestamps are
separate from the Mac's acknowledgement time. No automatic backup pruning.

Working-tree capture includes Git HEAD history needed offline, index entries and
blob contents, tracked deletions, executable modes, safe symlink records,
nonignored untracked files, and ignored `.scratch` payloads. Exclude `.git`, ignored
caches and private runtime/profile files. Restore only to an absent destination;
reproduce staged/unstaged state without network or modification of the source
repository. Unsupported submodules/special files and unstable reads report a
failure rather than claiming full protection.

Conversation capture reads bounded, stable prefixes while native writers remain
active. Drop incomplete trailing JSONL records and retain the last validated
completed-turn boundary for native continuation. Preserve original bytes and
required project-scoped ancestry/child/task records. No native resume/injection
calls during capture. Refused formats/scopes remain explicit issues; do not claim
those conversations protected or transfer unrelated/global personal memories.
Credentials, databases, global settings and personal memory libraries never sync.

Recovery creates a new native conversation identity from a checkpoint, with the
destination runtime cwd/roots and current project notes. It never takes over the
unreachable original. Codex uses native fork-by-path with automatic goal continuation
deferred; detached ancestor history must be proven. Claude uses native fork-session
with a new ID and preserves its imported parent. Native auth/settings remain on
each host. Plain native project sessions are observed as well as task launches.

Scratch and project notes keep their existing safe two-way sync. The owned worker
also captures/pulls remote checkpoints periodically. Switching the working root
stops and safely retargets only the owned scratch session, retaining prior snapshots.
When the Mac sleeps/disconnects, no new local acknowledgement is possible. The
guarantee is the latest completed local checkpoint, not an unsynced token/turn or a
portable copy of a running process.

## Retirement and acceptance

Retire the daily menu, same-ID custody-transfer protocol, guardians/adoption/foreign
owner workflow and unused CLI/task surfaces after native fork recovery is proven.
Keep shared setup terminal UI, identity/private IO, secure transport, scratch sync,
mandatory native installers and only the native validators/client functions used
by checkpoints. Old private recovery data remains untouched.

Installed-project upgrades explicitly review deletion of retired managed helpers
only when their bytes still match recorded hashes. Refuse edited/symlink helpers.
Remove only exact historically generated root task delegates; preserve custom
tasks. New generated aliases live in an owned checksummed block. Git hook and
commit guards verify deletion as rigorously as file creation.

Acceptance: run real local/remote Herdr and both native agents in disposable
project fixtures, capture live turns and uncommitted/staged/untracked edits, sever
the connection without handoff, and prove offline local recovery plus native
continuation retains completed context and notes. Include fork-of-fork ancestry,
partial JSONL, active writers, interruption, restart, conflicts, unchanged-profile
receipts, alias idempotence and upgrade deletion checks. Root reruns gates/smoke
before the authorized PR merge. Do not merge Mainstage's branch.
