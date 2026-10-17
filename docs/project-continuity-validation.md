# Continuity validation

The acceptance case is continuing project work on a Mac after its Codespace is
unreachable, including edits that were never committed.

## Live checks

A disposable checkout in an existing Codespace exercised the real controller,
SSH transport, native remote Herdr setup, and pinned Mutagen worker. The fixture
supplied its Codespace selection because its fake origin deliberately differed
from the Codespace's real repository. Repository matching and machine startup
remain covered separately by the adapter tests; this was not a public wizard
installation into the real project checkout.

Observed results:

- Remote Herdr attached and `pwd` identified the separate seeded checkout.
- A periodic Mac checkpoint received staged binary bytes, different unstaged
  bytes, an untracked file, and project notes created remotely without a commit.
- Scratch synchronized in both directions.
- With the Codespace actually stopped, local recovery retained those exact
  staged/unstaged bytes, untracked data and notes. The original Mac checkout and
  raw index remained unchanged. Local Herdr opened the recovered checkout.
- Native Codex fork-of-fork recovery retained the completed `ORCHID` turn using
  both a fresh native store and the normal profile; source transcripts stayed
  byte-identical. A real Claude source/fork turn retained the same context and
  preserved its source and import alias.

Owned fixture sessions, SSH aliases, workers and checkouts were removed. The
Codespace was returned to its initial stopped state. Global native configuration
and authentication were preserved. Private receipts and checkpoint evidence are
retained outside Git in the maintainer's test lab.

## Regression gates

The release check ran 376 tests successfully, with one expected skip on the
case-insensitive Mac filesystem. The pinned real Mutagen stop/restart/deletion
gate ran. `task check` passed Python compilation, ShellCheck and whitespace checks.
An independent whole-branch review approved the final implementation after
replaying the reported interruption failures.

## Failure and upgrade checks

Automated checks exercise immutable-store corruption, partial uploads/downloads,
identity mismatch, staged/unstaged recovery, same-checkpoint deduplication,
100-file transfer batching, unsafe paths and native dependency/scope refusals.
Receiver acknowledgement follows complete verified object arrival.

Interruption tests cover scratch rebind after old-session termination, SSH config
and receipt publication, and native transcript publication/index reconciliation.
Owner-modified state and foreign native identities remain preserved and refused.

Generator migration tests run the complete render/review/apply/commit path.
Unchanged retired helpers disappear from disk, Git index and ownership metadata;
owner edits, unknown helpers and custom tasks survive. Hook resurrection of a
retired helper refuses the commit and cannot be accepted as an automatic rewrite.

## Current limits

Only completed supported native turns are recoverable. Global-memory context,
Codex child agents, and unproven Claude Agent/Task dependencies refuse transfer.
Conversations started in nested subdirectories are not currently discovered.
The managed launch commands start at the checkout root.

Code protection can fail on unstable reads, unsupported Git/filesystem layouts,
or objects above the transfer limit. Native credentials, databases and known
runtime stores in a checkout or its reachable Git history refuse capture. These
failures and conversation issues must be checked in `task work:status`.

The Mac must be awake and connected. Protection extends only through its last
acknowledged checkpoint, not through work created after that receipt.
