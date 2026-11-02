# A replacement Codespace gets its predecessor's conversations: design (#100)

Date: 2026-11-02. Issue #100, filed after innovate's 2026-10-30 replacement; offline copy of its text:
`tmp/codespace-lifecycle/innovate-replace-2026-10-30/issue-replacement-sessions.md`. Owner ruling 23 ("Carry
old sessions on replace (#100)") puts it in the current program; ruling 20 ("Only for real decisions") means
the lead writes the design, a reviewer checks it, then plan, herd build and live test, with no stop unless an
answer changes what gets built. Read-only analysis behind it, with file and line evidence:
`tmp/codespace-lifecycle/sync-fixes/analysis/REPORT.md` (section "#100", REPORT below). It recommended option
(b), relaying received checkpoints; this design is option (b).

A builder check (a fresh reader, 2026-11-02) found five open questions and four places where the first draft
contradicted the code. Each was checked against the source; the draft's two-mechanism design (relay import
journals, plus a record only for refused entries) was replaced by the single received record below. The calls
it settled are listed at the end.

## The owner's words

From the issue's "Expected": "a replacement Codespace gets the conversations its predecessor delivered to the
Mac, either by making received Codespace sessions available on the Mac (so Desktop → Codespace carries them
on) or by publishing them at `--rebind`." Out of scope there: "Codex worker sessions under
`.claude/worktrees/*` are skipped by design."

## What actually broke (checked on the Mac, 2026-11-02)

The issue looked for the old Codespace's session ids in the Mac's native store and found none. That check
cannot succeed for Claude: an imported Claude conversation is published under a new id. The Mac's innovate
state (`~/.local/state/devcontainer-setup/projects/b93a9cad02201713044b771d/conversation-imports/`, 123
journals) holds 23 complete import journals for the old lead session `ce562d17…`: 22 written on 2026-10-26,
all superseded, and one on 2026-10-30 at 14:38Z (`6b563120…`, cutoff 5,836,130 bytes). So the Mac did make
the latest revision available. What failed is the next hop (REPORT, "first/second missing edge"):

1. Desktop → Codespace captures only the Mac's native sessions (`conversations.capture_conversations`).
2. A native session that is an unchanged import is dropped from that capture on purpose (`_unchanged_import`,
   called from `keep`) so an import never echoes back; that also keeps it from a replacement.

And a received revision the Mac never published (refused, or never attempted because the stage timed out or the
worker died) has no lasting record: its only pointer is the `codespace` receipt slot, which the next received
snapshot replaces (`checkpoint_store.py` acknowledge; REPORT, "receipt lifetime").

## Goals

- After a replacement and its first completed sync cycle, the new Codespace lists every conversation the old
  one delivered to the Mac, through the existing new-id publication path.
- This holds whether or not the Mac itself made that conversation available (both RED cases in
  `tmp/codespace-lifecycle/sync-fixes/analysis/evidence/red-tests.log`, `ReplacementRed`, turn green), and
  whether or not the Mac ever got to try.
- It holds across a chain of replacements (A → B → C) and never needs an old Codespace to be reachable.
- A Codespace never gets a copy of a conversation that was started on it, even after its own copy is deleted.
- Once a conversation is published, later cycles change nothing (no new forks, no new journals).

## Non-goals

- Codex worker sessions under `.claude/worktrees/*` (skipped by design, as today).
- Publishing at `--rebind` (the issue's second route). The regular worker cycle after a rebind carries the
  relay and retries every cycle; a one-shot publish at rebind would not.
- Merging continued copies. A conversation continued on the Mac and its original stay two conversations, as
  they are today on the Codespace the original came from.
- Making an unsupported conversation available. Version, record, scope and completed-turn checks refuse
  exactly what they refuse today, with the same visible issues.
- Recovering what the Mac received before this ships but never published: no record of it survived.
- The checked delete (#62) and its "pending conversations" verdict, and a way to forget a conversation: both
  belong to `task work` (#87).

## Anti-goals (a failure even if it technically works)

- Duplicates on the Codespace a conversation started on, or a new fork on every cycle.
- Losing a predecessor's conversation to housekeeping (`Store.collect`) while it is the only copy.
- A sync cycle that fails, or never finishes a replacement's first relay, because of one conversation.
- Breaking today's ordinary publication: a Codex fork's ancestors are written under their original ids
  (`_publish_codex`), and that must keep working exactly as now.

## How it works

The Mac is the hub: every Codespace syncs only with it, so only the Mac relays. Nothing changes in what a
Codespace captures.

### 1. The Mac keeps a record of every conversation it receives

When the Mac acknowledges a snapshot received from a Codespace, it records each conversation entry in it,
before the receipt slot is replaced, in the checkpoint store under its existing lock. The record is the entry
exactly as received (session id, cutoff, file blob references, source root), keyed by its revision. A crash
between the record and the receipt leaves an extra record, never a missing one.

Records retire only on proof. When a later received revision of the same `(agent, session id)` starts with
an earlier one's retained bytes (the append test `_supersede_imports` already uses), the earlier record
retires. Two revisions of one id where neither starts with the other (possible for Codex, where an ancestor
can be continued under its original id on another host) are both kept. A record never retires for age, for
being published on the Mac, or for being refused. `Store.collect` treats every live record as a root, like
the import journals it already keeps.

Seeding, once, the first time the new code runs: the entries of the manifest the current `codespace` receipt
names, and the `source_checkpoint` of every complete import journal without `superseded.json`. For innovate
that brings back old Codespace A's published conversations (for example `ce562d17…` at 14:38Z on 10-30).

### 2. Desktop → Codespace sends the records along

The Mac's outbound capture is today's native capture plus every live record's entry, each marked as relayed
(an extra entry field; schema 2 stays as it is). A record whose revision equals a native entry in the same
manifest is sent once. Each relayed entry is validated against the store first; one whose blobs are missing or
corrupt is an issue, and the rest of the manifest still goes.

### 3. A Codespace never takes back what started on it

Each capture records the session ids it saw that were started on that side (native, and not created by an
import journal there). This list is kept in the project's state and only grows. When a Codespace publishes an
entry marked as relayed, it first checks that list, then its native store for a session with that id that no
import journal there created. If either says the conversation started here, nothing is written and the entry
counts as available (reported as `unchanged`, with the native id). Entries that are not marked as relayed go
through publication exactly as today: this check never applies to them, so Codex ancestors keep working.

### What the owner sees

The counts keep their meaning: a relayed conversation is one more entry, received when the Codespace
acknowledges the snapshot, available once published or found to have started there. A relayed conversation the
Codespace refuses is an issue in that direction and keeps it degraded, as a refused native conversation does
today. Publication journals each conversation as it goes, so a replacement's first relay that runs out of time
is finished by the next cycles, with nothing published twice.

## Constraints

- Every relayed entry goes through the destination's normal `_preflight` and publication path: new id,
  journal before native changes, no model turn, no writer takeover. Nothing here writes a native session any
  other way.
- Each remote call runs the Mac's own `project_sync` code (`RemoteEndpoint.call` sends it), so there is no
  mixed-version case to support.
- Records and the started-here list are per project (`project_id` and `identity`).
- Locks: records are written and collected under the checkpoint store's lock. Do not take
  `conversation-recovery.lock` inside it.
- Docs: `docs/project-continuity.md` gets a short paragraph: a replacement receives what its predecessors
  delivered to the Mac, and a conversation is never copied back to the Codespace it started on.

## Tests the plan must include

- Both `ReplacementRed` cases green: the predecessor's conversation reaches the replacement when the Mac
  published it and when the Mac refused it.
- Never attempted: the Mac acknowledges a snapshot, then publication stops (the stage timeout, a
  `BaseException`) before the entry; the replacement still gets it.
- Echo: with the original binding unchanged, repeated cycles create no copy on the Codespace it started on;
  the same after that Codespace's own transcript is deleted.
- Today's path unchanged: a Mac Codex session and its fork, published to a Codespace without relay, still give
  the ancestor its own new-id copy (the case the first draft's check would have broken).
- Idempotence: three cycles after a replacement leave one published copy and no new journal.
- Chain: A → B → C, with A gone before C is bound, still delivers A's conversation to C.
- Records: an appended revision retires the earlier one; a diverging revision of the same id keeps both;
  `collect(apply=True)` with records more than seven days old keeps their blobs; seeding from the current
  receipt and from complete, non-superseded journals.
- A relayed entry with a missing or corrupt blob is one issue, and the rest of the manifest syncs.
- A first relay interrupted after some entries completes on the next cycle with no duplicate.

## Left to the builder

Function and file names; the record and started-here file layouts; where in acknowledge the record is written;
how the started-here check finds a native session for each agent (the Claude namespace path, the Codex index);
how existing fixtures in `tests/test_replication.py`, `tests/test_checkpoint_store.py` and
`tests/test_native_publication.py` are extended (several capture and publish in one profile, so relayed
fixtures need a second one).

## Calls made by the lead (each can be overruled)

1. **One record of everything received, written at acknowledge** (builder check Q1). The first draft recorded
   only refused entries, which missed entries publication never reached. Recording at acknowledge also covers
   a crash or stage timeout.
2. **Only the Mac relays.** It is the hub; relaying from Codespaces too would only bounce conversations back.
3. **Retire only on an append proof, never by length or age** (Q2). Diverging revisions are kept.
4. **The started-here check applies only to relayed entries, and uses a started-here list as well as the
   native file** (Q4, C1). A file check alone would hand a deleted conversation back to where it started, and
   applied to every entry it would break Codex ancestors.
5. **A conversation no Codespace can publish stays visible and keeps sync degraded**, as a refused native
   conversation does today (Q3). Forgetting a conversation is a `task work` (#87) action, designed there.
6. **No batching.** Publication journals each entry as it goes, so a long first relay finishes over several
   cycles (Q5); the plan tests that, and adds batching only if the test shows a cycle cannot make progress.
