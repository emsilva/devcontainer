# A replacement Codespace gets its predecessor's conversations: design (#100)

Date: 2026-11-02. Issue #100, filed after innovate's 2026-10-30 replacement; offline copy of its text:
`tmp/codespace-lifecycle/innovate-replace-2026-10-30/issue-replacement-sessions.md`. Owner ruling 23 ("Carry
old sessions on replace (#100)") puts it in the current program; ruling 20 ("Only for real decisions") means
the lead writes the design, a reviewer checks it, then plan, herd build and live test, with no stop unless an
answer changes what gets built. Read-only analysis behind it, with file and line evidence:
`tmp/codespace-lifecycle/sync-fixes/analysis/REPORT.md` (section "#100", REPORT below). It recommended option
(b), relaying received checkpoints; this design is option (b), built on records the code already keeps.

## The owner's words

From the issue's "Expected": "a replacement Codespace gets the conversations its predecessor delivered to the
Mac, either by making received Codespace sessions available on the Mac (so Desktop → Codespace carries them
on) or by publishing them at `--rebind`." Out of scope there: "Codex worker sessions under
`.claude/worktrees/*` are skipped by design."

## What actually broke (checked on the Mac, 2026-11-02)

The issue looked for the old Codespace's session ids in the Mac's native store and found none. That check
cannot succeed: an import is always published under a new id (REPORT, "U — why those particular histories
were unavailable"). The Mac's innovate state (`~/.local/state/devcontainer-setup/projects/b93a9cad02201713044b771d/conversation-imports/`)
shows the old lead session `ce562d17…` was imported on the Mac five times between 2026-10-26 and 2026-10-30,
the last at 2026-10-30 14:38Z (journal `6b563120…`, `status: complete`, cutoff 5,836,130 bytes). So the Mac
did make it available. What failed is the next hop, and REPORT names both missing edges:

1. Desktop → Codespace captures only the Mac's native sessions (`conversations.capture_conversations`).
2. A native session that is an unchanged import is dropped from that capture on purpose
   (`_unchanged_import`, called from `keep`), so an import never echoes back to where it came from. The
   same rule also stops it reaching a replacement, which never had it.

Separately, a received revision the Mac refuses to publish has no durable record at all: the only pointer to
it is the `codespace` receipt slot, which the next received snapshot replaces (REPORT, "receipt lifetime").

## Goals

- After a replacement and its first completed sync cycle, the new Codespace lists every conversation the old
  one delivered to the Mac, under new ids, through the existing new-id publication path.
- This holds whether or not the Mac itself could make that conversation available (both RED cases in
  `tmp/codespace-lifecycle/sync-fixes/analysis/evidence/red-tests.log`, `ReplacementRed`, turn green).
- It holds across a chain of replacements (A → B → C) and never needs an old Codespace to be reachable.
- The Codespace a conversation came from never gets a copy of its own conversation back.
- Repeated cycles change nothing once a conversation is published (no new forks, no growing issue list).

## Non-goals

- Codex worker sessions under `.claude/worktrees/*` (skipped by design, as today).
- Publishing at `--rebind` (the issue's second route). The regular worker cycle after a rebind carries the
  relay, and it retries on every cycle, which a one-shot publish at rebind would not.
- Merging two continued copies of one conversation. Independently continued copies stay separate
  conversations, as today.
- Making an unsupported conversation available. Version, record, scope and completed-turn checks keep
  refusing exactly what they refuse today, at every destination, with the same visible issues.
- The checked delete (#62) and its "pending conversations" verdict. That step is designed with `task work`.

## Anti-goals (a failure even if it technically works)

- Duplicate conversations appearing on the Codespace they came from, or a new fork on every cycle.
- Losing a predecessor's conversation to housekeeping (`Store.collect`) while it is still the only copy.
- A sync cycle that fails or stalls because one relayed conversation is refused.

## How it works

### 1. Relay what the Mac imported (the outbound edge)

Where capture drops an unchanged import today, it adds the import's original instead: the `source_checkpoint`
entry stored in that import's journal (`conversation-imports/<revision>/receipt.json`). That entry is the exact
manifest entry the Mac received, with its original session id, cutoff and blob references. Its blobs are
already in the store, and `Store.collect` already keeps every blob any import journal names
(`checkpoint_store.py`, "Published conversations keep proving their source against these blobs"). The
materialized copy is still not exported, as today.

Only the journal that matched the native file is used, so a superseded older revision is never relayed. An
import the owner has continued on the Mac is no longer unchanged: it is exported as the Mac's own conversation,
as today, and its original is not relayed separately.

This runs on both sides, because both run the same capture: the Mac relays an old Codespace's conversations to
the new one, and a Codespace relays the Mac-born originals it imported back toward the Mac (where the
existing per-revision journal makes them `unchanged`).

### 2. Keep what the Mac received but could not publish (the durable record)

When publication refuses a received entry, the receiving side writes a small relay record for it: the entry
itself, the reason, and when. One record per `(agent, source session id)`: a newer received revision of the
same session replaces the older record, a revision that is not newer (shorter cutoff) never does. A record is
removed when that same revision is later published on this side (it then relays through step 1). Capture adds
every live record's entry to the outbound manifest, after validating it against the store, exactly like a
step-1 entry. `Store.collect` treats relay records as roots, like import journals. A record is never dropped
for age.

### 3. Never copy a conversation back to where it came from (the destination guard)

Before publishing an entry, the destination checks its own native store for a session with the entry's id in
this project. If one exists, that destination is where the conversation came from: nothing is written, and the
entry counts as available (reported as `unchanged`, with the native id as its session id). This is what lets
step 1 run on every cycle without echoing an import back to its origin.

### What the owner sees

The status counts keep their meaning: a relayed conversation is one more entry, received when the destination
acknowledges the snapshot and available once published (or found native, step 3). A relayed conversation the
destination refuses is an issue in that direction's details, as any refused conversation is today.

## Constraints

- Every relayed entry goes through the destination's normal `_preflight` and publication path: new id,
  journal before native changes, no model turn, no writer takeover. Nothing in this design writes a native
  session any other way.
- No new remote operation. An older Codespace without this change still receives a normal schema-2 manifest.
- Relay records and the destination guard are per project (`project_id` and `identity`), like import journals.
- Locks: relay records are written and read under the same lock as import journals
  (`conversation-recovery.lock`).
- Docs: `docs/project-continuity.md` gets a short paragraph saying a replacement receives what its predecessor
  delivered, and that a conversation is never copied back to the Codespace it came from.

## Tests the plan must include

- Both `ReplacementRed` cases green: the predecessor's conversation reaches the replacement when the Mac
  published it, and when the Mac refused it.
- Echo: with the original binding unchanged, repeated cycles create no copy on the origin Codespace and
  report the conversation available.
- Idempotence: three cycles after a replacement leave one published copy and no new journal.
- Chain: A → B → C, where A is unreachable by the time C is bound, still delivers A's conversation to C.
- Continued on the Mac: an import with a new Mac turn is exported as the Mac's conversation and its original
  is not relayed as well.
- Relay record lifecycle: a newer revision replaces the record, an older one does not, publication removes
  it, `collect(apply=True)` with the record more than seven days old keeps its blobs.
- A relayed entry whose blob is missing or corrupt is one issue, and the rest of the manifest still syncs.
- Codex: a relayed fork keeps its ancestor closure; unsupported records stay refused (#53's tests).

## Left to the builder

Function and file names; where in capture the substitution sits; the relay record's file layout under the
project state dir; how the destination guard finds a native session for each agent (the Claude namespace
path, the Codex index); how existing fixtures in `tests/test_replication.py` and
`tests/test_native_publication.py` are extended.
