# task work and setup wizard: UX round 1

Date: 2026-11-14. Base: `main` at af769fc. Inputs: the Fable UX review
(`tmp/codespace-lifecycle/task-work/ux-review-2026-11-13/REVIEW.md`, items 1-12) and the owner's items (a)-(e)
from 2026-11-13/14 (`tmp/codespace-lifecycle/task-work/stage-c/LEAD-STATE.md`, entries of 2026-11-13T23:5xZ and
2026-11-14).

## Intent

The owner wants to set up a project, connect, arm and replace by driving `task work` and the setup wizard alone,
without a lead explaining the screen. On 2026-11-13 that failed four ways, and each one cost a conversation:

- strata-mf and ai-hub-demo ran the install one-liner from Mac checkouts holding the project while GitHub's `main`
  held only the org template. The wizard scans and publishes from `origin/main`, so the setup merged onto the
  template, both Codespaces held no project code, and both import PRs then conflicted on `Taskfile.yml`.
- The wizard's new `Taskfile.yml` claimed `up`, `down`, `status` and `build` as aliases of the devcontainer tasks.
  Those names belong to the project's own stack (owner's global rule), and they caused the conflicts.
- A prompt titled only `Codespace` offered `create like last time` on a first setup. The owner could not tell
  what was being asked.
- `task work` on strata-mf showed `Setup did not finish; run setup again` for a validated Codespace that only
  lacked a pairing, and offered a wizard rerun instead of Connect. `task work:signin` on ai-hub-demo failed with
  `The connected worker is already handling links.` and exit 1, pointing nowhere.

The review adds what costs time around the daily screen: Replace sits second and starts without a question, the
Codespace name crowds out times and causes, and several screens use internal words.

The work is split in two (owner, 2026-11-14: "follow your recommendation"). **Round 1, this document:** everything
that cannot create, bill or delete a Codespace. **Round 2, later:** review items 2 (delete progress and finishing
an interrupted delete), 3 (recovery after a Mac restart, other projects, the Herdr sidebar marker from #87 step 10)
and 5 (calling the owner back during a replace), plus the replace preview's "what is running there now" row,
which needs the early scan of the old Codespace from #113.

## Goals

Success: on a fresh project and on the three live projects, the owner can read each screen and pick the right
action without help, and today's four failures cannot recur.

### A. Setup wizard (`devcontainer_setup/__main__.py`)

**A1. Stop when GitHub's default branch does not hold the project (owner item a).** Before anything else that
can publish (so also before `interactive()` looks for an open setup PR to resume, `__main__.py:1118-1126`), fetch
`origin/<default>` and compare the files tracked at this checkout's `HEAD` with those at `origin/<default>`,
leaving out on both sides the paths the setup itself writes (`.devcontainer/`). Those are the same on both sides
once a setup has landed, and about 60 of them would let a small project pass while `main` lacks it. When the
default branch holds fewer than half of the remaining files, print one refusal and exit non-zero, having asked
nothing, written nothing and pushed nothing:

```
GitHub's main holds 3 of the 212 files tracked on docs/design-20261023.
A Codespace starts from main, so it would hold almost none of this project.
Merge the project into main first, then run the setup from a checkout on main.
```

The test runs on every branch, `main` included: a Mac whose own `main` holds the project while GitHub's `main`
holds only the template must be refused too, with the fix worded for it (`Push this Mac's main to GitHub, or merge
it there, first`). A checkout whose `HEAD` is an ancestor of `origin/<default>` (on `main` and only behind) always
passes: the Codespace gets everything it has. A checkout with no `origin` keeps today's behavior (commit-only
setup). *(Lead's calls, from the builder check: the PR-resume path, the `.devcontainer/` exclusion, the ancestor
pass.)*

This is the same test as the leads' `codespace-ready` step 0 (`ready.py`, check 1). The two must agree, so round 1
also gives `ready.py` (dotfiles repo, `~/.local/share/chezmoi/private_dot_claude/skills/codespace-ready/scripts/`)
the `.devcontainer/` exclusion and the ancestor pass, with tests.

**A2. Never write `up`, `down`, `status` or `build` (owner item b).** When the project has no Taskfile, the wizard
still creates `Taskfile.yml` (the devcontainer include and the `work` task block need one), with no tasks of its
own. Existing Taskfiles are handled as today. Projects that already have the four aliases keep them; nothing
removes lines from a project's Taskfile.

**A3. Every prompt is a question, and the answers stay on screen (owner item c).** Gum clears a prompt once it
is answered, which is why Review was hard to read against what had been asked. After each wizard prompt, one line
stays: the question and the answer. Every prompt title becomes a plain question, for example:

| Today | Round 1 |
|---|---|
| `Publish` | `How should the setup reach GitHub?` |
| `Codespace` | `Create a Codespace now?` (unpaired) / `Which Codespace should this use?` (several listed) |
| `create like last time` | `create a new Codespace` when this repository has no launch history; with history, name what is reused: `create one like <name> (<machine>, idle <timeout>)` |
| `Codespaces machine` | `Which machine size?` |
| `Stop when idle for` | `Stop it after how long idle?` |
| `Editor after create` | `Open an editor once it is created?` |
| `Review setup` | `Review: go ahead with these answers?` |

The wording in the right column is the lead's call; the builder may refine it with the `ux-writing` skill as long as
each title is a question and no label assumes a history the project does not have.

### B. The `task work` screen (`project_sync/overview.py`, `standing.py`)

**B1. A set-up project that is not paired offers Connect (owner item d).** Today `standing._setup` returns
`partial` when the managed `devcontainer.json` is present but this Mac has no `workspace.json` or `scratch.json`.
Those files appear only when the project is paired at the first connect. So every new Codespace, and every second
Mac, reads "Setup did not finish". Round 1: managed metadata present means set up. A project with no pairing takes
the existing `unpaired` situation (`No Codespace is paired with this checkout`, Connect when GitHub lists one,
Create). An unfinished launch keeps the existing `launch` situation (`Setup of Codespace NAME did not finish
validation`, Resume). The `partial` value and its "Setup did not finish" line go away.

**B2. The screen says when the checkout is not on the default branch (owner item a).** Today only `behind` and
`diverged` on the default branch get a line. Add one for a checkout on another branch: `This checkout is on
docs/x; Codespaces start from main`.

**B3. Name the Codespace once, then labelled rows (review item 4).** Line one is `<project folder> · <Codespace>`
(or `<project folder> · no Codespace paired`). The rest are rows with a label column:

```
consult-sm-innovate · literate-pancake-jjjrq4j9rqwrc5qp6
Codespace  stopped by GitHub's 12-hour limit at 07:55
Recovery   armed; next 12-hour stop about 17:45
Sync       last completed 07:54
Sign-ins   Plaud is not signed in
Checkout   on docs/x; Codespaces start from main
```

The screen keeps the owner's #87 limits: at most 6 lines of 78 cells, a Gum menu of 3-6 actions. Menu labels drop
the Codespace name and the command (`Replace this Codespace`, `Finish the replace`, `Check and delete the replaced
Codespace`). The command still prints as `Running: …` once chosen and in the `Next:` lines without a terminal.
This changes #87's "each item prints the command it runs": the owner chose labels only on 2026-11-14 ("follow your
recommendation"), over keeping commands where they fit or everywhere.
Tests render with a 36-character Codespace name (run 3's `bookish-space-meme-v66pqg6wpvvqcpw7j`; `literate-pancake-jjjrq4j9rqwrc5qp6` is 34), not
`paired-cs`.

**B4. Replace moves down (review item 1, order).** Replace sits below Connect, Connect and arm recovery, Start
and connect, Stop and every other action, directly above the retired-Codespace delete (which keeps its reserved
slot, `overview.py:352-358`) and `Show details`. Like that delete, Replace gets a reserved slot: the cut to six
actions drops other items first, so Replace never disappears in a busy state. Its argv is unchanged. *(Lead's
call, from the builder check.)*

**B5. The Recovery row says what the owner plans around (review item 7).** Armed and running: `armed; next 12-hour
stop about 17:45` (the figure `task work:status` already prints as `12-hour stop expected`). During a stop:
`restarting it now`, `on hold until 09:30` or `out of restarts until 20:12`, from the keeper's own state.

**B6. After a replace, say the old Codespace is still there (review item 8).** When a replaced Codespace is still
listed on GitHub: `The replaced Codespace is still on GitHub; check and delete it when ready`.

**B7. Sign-in state reaches the screen (review item 11).** From the sign-in record: `Sign-ins   Plaud is not
signed in`, with the action `Sign in` (`task work:signin:all`).

**B8. `Show recovery and sync logs` appears only when a log exists (review item 12).** It no longer pads a short
menu. `Exit` counts toward #87's three-item minimum, so "not set up" shows `Set up this project`, `Show details`,
`Exit`: one real action where there is one thing to do. *(Lead's call, from the builder check; the test at
`test_overview.py:171` changes with it.)*

**B9. `Show details` leads with the one fix (review item 9).** After a Mac restart it says `Recovery is off:
connect with task work:cs` once, not `run task work:cs` then twice `run task work:cs:armed`. No row wider than 78
cells.

### C. Starting a replace (`devcontainer_setup/replacement.py`)

**C1. A fresh replace asks first, and the default is no (review item 1).** When there is no replace journal,
after the read-only checks `inspect()` already makes and before anything is written or created:

```
Replace literate-pancake-jjjrq4j9rqwrc5qp6?
  New one  basicLinux32gb (2 cores), stops after 240 min idle, main at 9f2fae2
  Billing  kyndryl-agentic-ai, from the moment it is created
  You      end agents when asked, approve the sign-ins, decide the delete
> Not now
  Replace it
```

`Not now`, Esc and Ctrl-C leave no journal and print `Nothing was replaced.`, exit 0. A replace that already has a
journal (a rerun after an interruption) does not ask this again. The `Now` row the review drew (agents running on
the old Codespace) waits for round 2.

**C2. Continue or abandon says what abandon does (review item 10).** `Continue the replace` and `Abandon it:
<old> stays paired; <new> stays until you delete it` (or `nothing was created`, when nothing was).

**C3. Small copy (review item 12).** Refusals say `task devcontainer:setup`, not `task setup`
(`replacement.py:127`, `:135`). The agents gate says where to end them (`in their Herdr panes`) and answers an
unknown word with `Not understood: press Enter, or type stop.`

### D. Connect and sign-in (`project_sync/__main__.py`, `links.py`, `workspace.py`)

**D1. `task work:signin` while connected points to the right command (owner item e).** Both places in
`links.signin_main` that print `The connected worker is already handling links.` say instead that the connected
sync already finishes sign-ins and that `task work:signin:all` starts them, and exit 0.

**D2. A wrong word at `arm` no longer aborts the connect (review item 6).** In `task work:cs:armed`, anything but
`arm` prints `Not armed. Connecting without recovery; arm later from task work.` and the connect goes on to Herdr.

**D3. No pending-history line on a healthy connect (review item 12).** The connect no longer prints
`Conversation history is pending; inspect task work:status.` (`workspace.py:532-534`): before Herdr opens, the
first cycle is usually still pending (up to 120 s), and after Herdr the line would come hours late. Instead
`task work` shows a `History` row when conversation history has been pending for more than 5 minutes, read from
the same state the History row in `task work:status` reads. The connect itself does not wait. *(Lead's call, from
the builder check.)*

### E. Words (review, question 2)

On every screen round 1 touches, use one name per thing: Mac (not local, your Mac, Desktop), final sync (not final
flush, final capture, flush and move), Recovery plus a state (not keeper, keep-alive, heartbeat), sync (not sync
worker, Mac worker), paired (not binding), Connect. Internal terms (`the lead`, operation ids, `pending
(desktop_to_codespace:transfer)`, phase names) stay out of screens and stay in `--json` and logs. A full sweep of
screens round 1 does not touch is not required.

## Done means

*(Lead's call, from the builder check.)* No Codespace is created, rebuilt or deleted to prove round 1.

1. `task check` and `task test` green on the branch in the default environment, re-run by the lead; each new test
   shown failing with its change removed. The dotfiles gates (`task test`, `task scan`) green for `ready.py`.
2. A render file for the owner: every `task work` situation drawn by `overview.view` with run 3's real names
   (36-character Codespace name), each line measured at 78 cells or less; the wizard's prompts and the A1 refusal
   captured from a run against a local fake GitHub remote (as `ready.py`'s tests do).
3. Roll-out with the usual recipe (Codespace `none`, never rebuild) to mainstage, innovate and strata-sap under the
   owner's standing yes. strata-mf and ai-hub-demo get it too only on the owner's word at roll-out time.
4. The owner, on a live project: `task work` (reads it unaided), Replace then `Not now` (nothing written), and
   `task work:signin` while connected (points to `work:signin:all`, exit 0).

## Non-goals

- Round 2's items (above).
- Any change to what a command does beyond what is listed. Every menu action keeps its argv.
- Migrating projects that already have the four aliases.
- New owner-facing commands or flags.

## Anti-goals (a failure even if every test passes)

- Anything that lets a Codespace be created, rebuilt or deleted with fewer confirmations than today.
- A `task work` screen over 6 lines or 78 cells, or a menu without the command it will run printed once chosen.
- A test that passes on a short fixture name but cuts a real one.
- A refusal (A1) that fires on a checkout that is an ancestor of `origin/<default>`, or on a project with no
  `origin`.
- Losing a line from a project's own Taskfile.

## Constraints

- The owner's #87 rulings: at most 6 lines of 78 columns, a Gum menu of 3-6 actions, the likely one first;
  without a terminal, print the state and the `Next:` commands and exit 0. Menu items show labels only (B3, owner
  2026-11-14); the command prints once chosen. Ruling 5: only a connect turns recovery on.
- Never rebuild a paired Codespace; replacing one needs its own yes (C1 is that yes).
- Gates: `task check` and `task test` green on the branch, in the default environment.
- Copy follows the `ux-writing` skill. No AI attribution in commits or PRs.

## Approaches

**Scope.** One round of all 17 items, or two rounds. Two rounds won (owner, 2026-11-14): round 1 holds nothing
that can delete or bill, so it ships sooner, and round 2's interrupted-delete finish gets the safety review it
needs on its own.

**A1's test.** Three ways were considered:

- *No shared history* (`git merge-base HEAD origin/main` prints nothing). It catches strata-mf (its old branch,
  `0f5524c`, shares no history with the template's `b3c90ad`), but it misses a branch that merged the template
  main in: that branch shares history while `main` still lacks the code. It also cannot run in a shallow clone.
- *Most of the checkout's files are on the default branch* (picked). It measures what the Codespace would actually
  hold, catches both cases, and is the test the leads' `ready.py` already uses. Its false positive, a feature
  branch that adds more files than `main` has, has a clean way out: run the setup from `main`, which the wizard
  builds from anyway.
- *Look for an open import PR.* Rejected: it needs GitHub, covers only the org-template path, and A1's test
  already covers it.

**Where the replace question lives (C1).** In the replace command, not the `task work` menu, so the typed command
asks too and a menu change cannot bypass it.

## Left to the builder

- Exact wording beyond the examples, within the rules above.
- How the answer trail (A3) is printed (in the Gum wrapper or at each call), as long as every wizard prompt leaves
  one line and `task work`'s own menu does not print its choice twice.
- How B3's rows are ranked when more than six facts apply, keeping today's ranking intent (problems and anything
  needing action first).
- Where B5's figures come from (the keeper's state, as `task work:status` reads it).
- Test layout, provided each new test fails with its change removed.

## Fix round 1 (2026-11-14)

Added after the six groups merged (`fec86be`). Sources: the Codex review (`F1`), the owner's live use of `task work`
on innovate, and the lead's list from the builders' reports.

**FR1. Show details wraps by screen width (Codex F1, P2).** `workspace._fit_row` wraps with character counts, so a
row of short wide words (`"接続 エラー " * 12` in a sync error) prints a 112-cell line. Wrap by `overview.cells`,
keeping the rule that a single word wider than the room (a path) is not broken. Every `task work:status` and connect
Recovery line stays at 78 cells or less, wide text included.

**FR2. `Open in Herdr` while connected (owner, 2026-11-14: "its already connected, shouldnt it give me an option to
arm?").** The screen offered `Connect` to a project that was already connected and armed. While the sync runs for the
paired Codespace and recovery is not off, the Connect item reads `Open in Herdr`. Its argv stays `task work:cs`.
When recovery is off, it stays `Connect` (a connect is what turns recovery back on).

**FR3. A sign-in item whenever connected (owner, 2026-11-14: "we should also have an item on the ux for signin
all").** While the sync runs for the paired Codespace, the menu offers `Sign in to the tools`
(`task work:signin:all`), directly above Replace. When the sign-in record names a tool that is not signed in, the
same item moves up to right after the first action, with the `Sign-ins` row as today. It is never reserved: in a
full menu it gives way before Replace and the retired delete.

**FR4. "final sync" everywhere on screen (spec E).** `cutover.py`'s `Final flush of … recorded`, `… while its final
flush was recorded …` and `… final flush is not complete` say "final sync".

**FR5. No "keeper" on the connect screen (spec E).** The connect's `The keeper has not reported within 60 seconds;
inspect task work:logs.` names Recovery instead, and says `see task work:logs`.

**FR6. The docs match the screens.** `README.md` and `docs/project-continuity.md` describe the C1 question
(`Not now` / `Replace it`), the C2 options, the new phase words, the labels-only menu, A1's refusal and A2's Taskfile.

**FR7. Two load flakes, fixed at their tests (tests only, own commits).** Under load averages near 140 on the
shared Mac, `tests/test_probe.py`'s `assert_dead` timed out on a 1 s `ps`, and `tests/test_signins.py`'s
`test_ctrl_c_on_the_codespace_reads_as_a_cancel` timed out on a 5 s wait. Both pass alone. The budgets are not the
behaviour under test; raise them so only a hang fails.
