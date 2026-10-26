# Keeper safety fixes: start evidence, deleted Codespaces, recovery on connect

Addendum to `2026-10-21-codespace-armed-recovery-design.md`. It covers issues #61, #60 and #57, the first step of the owner's 2026-10-25 order ("safety first"; rulings in `tmp/codespace-lifecycle/ux-run-2026-10-25/RULINGS.md`, items 5 and 14). #79 ships separately.

## Why

On 2026-10-25, replacing innovate's Codespace and bringing up strata-sap's showed three ways recovery can be off or wrong while everything looks healthy:

- **#61, late anchor.** A fresh keeper anchors GitHub's 12-hour clock on the API's `last_used_at`. Opening the Codespace in a browser or editor seems to move `last_used_at`. It was 10 min 18 s late on strata-sap and 9 min late on innovate, against a ±15 min window. One more editor open after a Mac restart would turn the real 12-hour stop into OTHER, and nothing would restart it overnight.
- **#60, deleted reads as auth.** A deleted Codespace answers HTTP 404. Every surface reports that as authentication or network trouble. The keeper keeps polling, keeps retrying the keep-alive and keeps the Mac awake, for a Codespace that no longer exists.
- **#57, no keeper after a restart.** After a Mac restart the arming survives, but nothing starts the keeper again. Only `task work:cs:armed` does; plain `task work:cs` doesn't, though the docs say either works.

**Goal:** recovery is on whenever the owner believes it is, and every surface names the real cause and the real fix.

**Anti-goals:**
- a restart the owner didn't ask for;
- a second billed Codespace;
- a consent prompt the owner didn't trigger.

## Part A: start evidence (#61)

**What gets recorded.** This package records its own evidence of each start, per Codespace, in the project's state directory. The file is `start-evidence.json`, written atomically and private like the other state files: `{"<codespace>": {"started_at": <epoch>, "source": "created"|"started"|"keeper", "stopped_at": <epoch|null>}}`. Writers:

1. **Setup** (`devcontainer_setup/codespaces.py`, the `gh codespace create` path): the Mac's time just before the create command, with source `created`.
   - The record is keyed by the new Codespace's name and written even when setup does not pair it.
   - The state directory is the one `setup_continuity` already computes (`discover_project` + `state_dir`).
2. **Owner-present starts** (`work:cs` through `Codespaces.start_codespace`, and `work:cs:restart` through `_owner_power`): the start routine's `posted_at`, with source `started`.
3. **The keeper's own restart**: `posted_at`, with source `keeper`.
4. **The keeper, on classifying any stop** (OWNER, LIFETIME or OTHER): sets `stopped_at` = the stop's first-seen time. A start recorded before a seen stop is never used for a later run.
5. **The sync worker, on learning a down state** (`scratch.explain`, a state in `keeper.DOWN`): it sets `stopped_at` the same way. Unarmed projects have no keeper, so without this their evidence would outlive any number of unseen stops. (My call.)

Only an accepted start writes `started_at`; a refused or failed one writes nothing. Writes never block or fail a start: a write error is logged and ignored.

**When the keeper picks its anchor.** This applies wherever the keeper anchors from `last_used_at` today (`keeper.py` `_up`):
- it has no anchor yet (a fresh keeper);
- its anchor is older than 12 h 15 min;
- a gap of more than 5 minutes between good readings, with `last_used_at` moved, could hide a stop and a start (R16).

In each of those cases it gathers every plausible start time as a candidate:
- **evidence:** usable when it is for this Codespace, `stopped_at` is unset or earlier than `started_at`, and `started_at` falls within `[reading.at − LIFETIME − WINDOW, reading.at]`;
- **api:** `last_used_at`, as today;
- **previous:** in the gap case, the anchor it had before the gap.

Candidates within `WINDOW` of each other count as one. The status shows the earliest one as the main anchor.

**Why several candidates (owner ruling, 2026-10-25: "accept either").** After a gap the keeper can't tell an editor open (the 12-hour clock didn't move) from an unseen stop and start (the clock restarted).
- Missing the real 12-hour stop leaves the Codespace down overnight, which is what arming exists to prevent.
- A false restart needs a stop for another reason to land within ±15 minutes of another candidate's 12-hour mark, and it stays inside the 2-per-24-hour budget.

So the keeper leans to restarting. This deliberately reverses two earlier review rulings, which become LIFETIME tests: `test_a_reading_gap_that_could_hide_a_restart_anchors_from_last_used_at` and `test_a_start_the_keeper_never_saw_is_anchored_from_last_used_at` (`tests/test_keeper.py`). Rewrite their names and comments to say why.

Without a gap, nothing changes: a newer `last_used_at` during continuous readings is still ignored as a no-op request.

**Classification.** `lifecycle.classify` takes the list of candidate anchors.
- LIFETIME when the stop fits the window of any candidate.
- OWNER uses the earliest candidate. That makes OWNER the more likely answer, and OWNER is the safe side: no restart.
- The keeper's own accepted POST stays the single anchor of the run it started, as today.

**Status copy.** Drop the "anchor api/observed/keeper" jargon: `12-hour stop expected ~00:06, counted from its last start at 12:06`. When two candidates are kept: `… expected ~00:06 (or ~00:16)`.

**First test.** A fresh keeper whose `last_used_at` is the real start + 6 h, with evidence at the real start and a stop at start + 12 h, must classify LIFETIME. It classifies OTHER today. The mirror test: evidence older than an unseen stop, and `last_used_at` at the real start, also classifies LIFETIME.

## Part B: a deleted Codespace is named as deleted (#60)

**Readings carry their HTTP status.**
- `lifecycle.Reading` gains `http` (int; 0 for a network failure).
- `transport.codespace_state` keeps the status the same way: a small result, or `'Deleted'` as the state.
- A 404 alone is not proof, because GitHub also answers 404 for a Codespace the token can't see. So a 404 is confirmed with one `GET /user/codespaces` by the same account:
  - **Deleted** only if that list returns 200 and lacks the name;
  - a list that fails keeps the auth reading;
  - 401 or 403 is auth, and 0 is network, as today.

**The keeper, on a confirmed Deleted:**
- notifies once (key `deleted-<codespace>`);
- stops the keep-alive child;
- releases caffeinate;
- polls every 15 minutes in phase `deleted`.

It doesn't exit. A rebind replaces it (`keeper.start` already replaces a keeper whose Codespace differs), and so does a disarm. Releasing caffeinate needs a hold/release handle passed into `Keeper`, which today only `supervise` holds. Ruling 17 (#64, release caffeinate after an owner stop) will reuse the same handle.

**Copy:**
- Notification: `<project>'s Codespace <name> was deleted. Recovery is paused and this Mac can sleep. Pair a new one with task work:cs:armed -- --rebind <name>.`
- Status: `Codespace    <name> no longer exists on GitHub (deleted). Pair another with task work:cs:armed -- --rebind <name>, or run task work:disarm.` For an unarmed project, `task work:cs -- --rebind <name>` instead.
- **Only the keeper says "deleted" outright,** because it reads with the armed account (`gh_user`). The worker and `work:cs` use whatever account is active, and a different account also gets 404 plus a list without the name. So they name the account and hedge: `<name> is not visible to <login>: it was deleted, or it belongs to another account.` (My call; pairing does not record an account, and adding one is out of scope.)
- The worker learns this through its existing state read after a failure (`scratch.explain`). It logs once, `Scratch paused: <name> was deleted. Your .scratch/ is intact (last synchronized HH:MM).`, and retries at most every 15 minutes, never every minute.
  - Notifications keep today's split: an unarmed project hears from the worker, an armed one from the keeper.
  - The worker's keeper watch must not report "Recovery is not running" for a keeper in phase `deleted`.
- `work:cs` on a missing pairing lists this repository's Codespaces (the same list setup uses) and names the rebind: `<name> is not visible to <login>: it was deleted, or it belongs to another account. This repository's Codespaces: a, b. Move to one with task work:cs -- --rebind <name>, or create one with task devcontainer:setup.`
- `work:cs:stop` and `work:cs:restart` on a 404: `<name> no longer exists on GitHub; there is nothing to stop.` The owner-stop record is still written first, as today (R33).

## Part C: recovery comes back on connect (#57)

Owner ruling: no LaunchAgent. Plain `task work:cs` on an armed project starts the keeper, using the consent on file.

- In `project_sync/__main__.py`, `--codespace` on an armed project runs the same steps as `--armed`, in the same place (after the worker is ready, before Herdr): the foreign-process check, then the keeper. It never prompts.
- **The consent counts only when `_arm`'s existing short-circuit holds:** the same consent hash, Codespace and gh account.
  - Otherwise `work:cs` connects without the keeper and prints `Recovery is armed but needs your confirmation again (<reason>); run task work:cs:armed.`
  - It never asks inline.
- A foreign gh process blocks the keeper, not the connection. It prints the existing message and continues.
- Plain `work:cs -- --rebind` on an armed project still refuses, as today. Carrying consent over a move is ruling 6 (#85), not this step.
- **Copy:**
  - status `Keeper       not running; run task work:cs`;
  - the worker's notice `Recovery for <project> is not running; run task work:cs`;
  - `docs/project-continuity.md` "after a Mac reboot" line: `Run task work:cs after a Mac restart; for an armed project it starts recovery again. Until then recovery is off.`

## Constraints

- Python standard library only. Keep the existing patterns:
  - fixed-text errors with no secrets;
  - atomic private state writes;
  - `locked(...)` around arming writes.
- Each part ships alone, with its own tests, gates (`task check`, `task test`) and commit to main, in the order A, B, C.
  - All three touch `keeper.py` and status. So they are built one after another, never in parallel.
- No change to the restart budget, the window, the hold or the owner-stop rules.

## Owner rulings in this design

- 2026-10-25: when the keeper can't tell an editor open from an unseen restart, it accepts either start time ("accept either"), instead of trusting `last_used_at` alone or this package's own record alone.

## My calls (not ruled by the owner)

- The worker also records `stopped_at`, so an unarmed project's start evidence expires at its next seen stop.
- 404 is confirmed by a list call before it counts as Deleted, because GitHub also hides inaccessible resources behind 404.
- The keeper stays running in phase `deleted` instead of exiting, so status and the worker's keeper watch stay truthful.
- The consent wording change ("while this Mac is awake; a closed lid or a restart pauses this") waits for the ruling-17 step (#64). Changing it now would make every armed project confirm again.

## Left to the builder

Names of helpers and fields beyond those above, test structure, and how the evidence helpers are shared between `devcontainer_setup` and `project_sync`.

## Verification after the code lands

On an innovate test-branch Codespace created directly with `gh codespace create --branch <test branch>`, billed to kyndryl-agentic-ai and never paired with the real innovate checkout (ruling 15). Setup creates only on the default branch and would merge into innovate's main, so the `created` writer is proven by offline tests only; live, the test records `started` evidence through `work:cs`.
- a fresh keeper anchors on its own evidence, not on `last_used_at`;
- `last_used_at` before and after a browser open (the controlled check #61 asks for);
- deleting it gives the Deleted status line, one notification, released caffeinate and a paused worker;
- after `task work:stop`, plain `task work:cs` brings the keeper back.

The test must use state separate from the real innovate pairing; the plan settles how. The Codespace is deleted when the test passes.
