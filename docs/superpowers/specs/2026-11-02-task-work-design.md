# `task work`: one entry point, replace and a checked delete: design (#87, #62)

Date: 2026-11-02. The fourth step of the owner's program (ruling 14 order: link helper, wizard, sign-ins, `task
work`). Owner rulings 1, 5-8 (2026-10-25), 22 (2026-11-01) and 24 (Grok, 2026-11-02), in
`tmp/codespace-lifecycle/ux-run-2026-10-25/RULINGS.md`; ruling 20 ("Only for real decisions") lets the lead write
this, have it checked, plan and build it. Issue texts (offline): `tmp/codespace-lifecycle/task-work/issues.md`.
Read-only analysis with file:line evidence and five red tests against 936db93:
`tmp/codespace-lifecycle/task-work/analysis/REPORT.md` (REPORT).

**Status: draft, two owner inputs pending** (section "Owner inputs"). Everything else is decided below.

## The owner's words

- Ruling 1: "Smarter commands, then `task work`. The commands take their own next steps first [...] Then add one
  state-aware `task work` entry point, a Gum menu that shows the state and offers only the actions that fit.
  Existing commands stay for scripts and agents. No full-screen TUI."
- Ruling 5: "After a Mac restart, recovery comes back on connect only. [...] Connecting an armed project with `task
  work` or `work:cs` starts the keeper, so the `:armed` name isn't needed. Until then recovery is off, and `task
  work` says so first."
- Ruling 6: "Arm consent carries over a move when the GitHub account and billing owner are unchanged, with a printed
  line saying so. A new account or billing owner asks again."
- Ruling 7: "Agents may stop a Codespace headless; restart stays human-only."
- Ruling 8: "Replace creates the new one like last time, moves sync, recovery and Herdr, checks it, then offers to
  delete the old one. Delete checks Git, scratch and pending conversations, shows a verdict, takes a typed `delete`,
  then cleans up. Both are reached from `task work`, plus a scriptable `work:cs:delete`."
- Ruling 22: "A rebuild or replacement keeps the cores of the Codespace it replaces."
- #87 (ruling text): `task work` prints where the project stands in at most 6 lines of 78 columns, then a Gum menu of
  the 3-6 actions that fit, the likely one first; each item prints the command it runs; without a TTY it prints the
  state and the exact "Next:" commands and exits 0.

## Goals

- One `task work` tells apart #87's states (not set up, no Codespace, stopped by you or not, running armed or not,
  deleted, recovery off after a Mac restart, failed creation, setup PR open, checkout behind, generator update) and
  offers the actions that fit. Today's daily surface is 13 names (the 11 `work:*` targets in
  `templates/continuity-tasks.yml`, plus setup and upgrade; REPORT §1) and they all stay.
- The commands take their own next steps: plain `work:cs` (with or without `-- --rebind`) carries recovery for an
  armed project, consent carries over a move with one printed line, and an agent can stop a Codespace headless.
- Replace is one rerunnable operation: create like the old one, move sync, recovery and Herdr, sign in fresh, check,
  then offer the checked delete. It is reached from the wizard's Codespace menu and from `task work`.
- `task work:cs:delete` checks, shows a verdict per category, takes a typed `delete`, deletes, and removes only what
  setup created for that Codespace.

## Non-goals

- The sign-in sequence itself (step 3, `docs/superpowers/specs/2026-11-02-fresh-signins-design.md`); replace calls it.
- #87 items 9-10: the copy pass naming `task work` everywhere, the Herdr popup key, sidebar token and toasts.
- A full-screen TUI (ruling 1). Restart without a terminal (ruling 7).

## Anti-goals

- Printing state that starts, downloads, signs in or spends anything. `_choose_pairing` starts stopped Codespaces
  and `fetch_release()` downloads (REPORT §2): neither is a reader.
- Calling a Codespace deleted on a 404 alone (#60's rule: only a full inventory proves it).
- A second replacement because a later phase failed, or a second Codespace after a lost create reply (today
  `codespaces.py:370-372` stops with "Check gh codespace list before retrying" and records nothing).
- Deleting on a stale verdict, without the typed word, headless, or through a `--yes`.
- Loosening `_consent_on_file`'s exact-Codespace check (`project_sync/__main__.py:62-84`) to let a move pass, or
  calling `arm()` to transfer consent (it resets the hold, owner stop and timestamps).
- A second conversation transport: #100's relay stays the only one.
- Removing an SSH block or Herdr profile setup did not create; wiping `known_hosts`, keys or the state directory.

## How it works

### Four stages

Each stage is planned, built and gated alone, in this order: **A** smarter commands; **B** `task work` printer and
menu; **C** checked delete; **D** replace (needs C, and the sign-ins step shipped). Stages merge to local main as their
gates pass; the step is pushed and rolled out once, after one live test of the whole step (call 1).

### A. Smarter commands (rulings 5, 6, 7)

- `work:cs:stop` runs before the terminal gate (`project_sync/__main__.py:352`) and keeps recording the owner stop
  before the request. Restart and a first `arm` stay terminal-only. Its Taskfile annotation stops saying interactive.
- Plain `work:cs -- --rebind NEW` on an armed project moves recovery too (today it refuses, `__main__.py:363-368`):
  stop and join OLD's keeper, retarget sync, move consent, start NEW's keeper, then Herdr. `:armed` stays as an alias.
- Consent move: a new helper in `arming.py`, called only by a verified rebind or replace. When the saved digest matches
  OLD's consent text, the gh account is the same and NEW's live billing owner is the same, it rewrites the target and
  digest for NEW, keeps `consented_at`, `armed_at`, ports, hold and the restart ledger, records `moved_from` and
  `moved_at`, and prints `Recovery moved OLD → NEW; consent from DATE kept (ACCOUNT, billing OWNER).` Otherwise it
  asks for a typed `arm` for NEW before anything moves; a decline moves nothing and says so. OLD's owner stop and
  pending restore are retired with its history (REPORT §5).

### B. `task work` (#87 items 6-7, rulings 1, 5)

- A root `task work` with a `desc` (#70), dispatching to the vendored runtime. Reading state, choosing actions and
  rendering are separate functions.
- The first frame comes from local state alone (registry, receipts, keeper and worker records, arming). Remote reads
  (Codespace state, setup PR, upstream tip) run in one bounded probe with a 2-second deadline, shown with their age or
  as `unknown` when unfinished, and joined before exit (call 7). It never calls today's whole `workspace.status()`,
  whose probes allow up to 30 s each (REPORT §2).
- Line one is the most urgent fact. For an armed project whose keeper is not running: `Recovery is off until you
  connect` (ruling 5; resuming on connect is already built, REPORT §2).
- The menu offers 3-6 actions, the likely one first (REPORT §2 table: Available → connect; stopped by you → start
  and connect; unexpected stop → the reason, then connect; failed creation → logs or resume; confirmed deleted →
  rebind or create; setup PR open → resume setup; checkout behind → update it; newer generator → upgrade). Each prints
  its exact command before running it. Details prints `work:status`. Without a TTY: the state, then `Next:` lines,
  exit 0.

### C. Checked delete (ruling 8, #62)

- `task work:cs:delete [-- --codespace NAME]`; without a name, the current pairing. `-- --check --json` runs the
  checks headless and makes no destructive call (call 2). The repository and account are verified on every run.
- Checks run over the package's read-only connection, never raw `gh codespace ssh`. A stopped or unreachable target
  reads `unknown` with an explicit "start it to check" action; it is never started silently. Per category (REPORT §4):
  - **Git:** every repository and linked worktree under `/workspaces` (not only `Codespaces.roots()`, which looks at
    100 top-level folders): changes including untracked files, stashes, and every branch or detached head not
    contained in a tip `git ls-remote` reports now.
  - **Scratch:** the current pairing synchronized with no conflicts; for a retired OLD, a content inventory against
    the Mac after its writers stop.
  - **History:** a final capture through the existing checkpoint path, matched with the Mac's receipt; anything
    pending, incomplete or unsupported blocks (owner input 2 sets what "kept on the Mac" allows).
  - **Agents:** live agents on its Herdr machine. An unreadable reply is `unknown` (today `restore.Herdr.agents()`
    returns `[]` on a malformed reply, `restore.py:166-172`, so it gets a strict variant).
- Each category reads SAFE, UNSAFE (with the outstanding paths, counts or commit ids) or UNKNOWN; deletion is offered
  only when all are SAFE. Contents, remote URLs and credentials are never shown. The verdict names its scope:

      Checked bookish-zebra-… at 11:44:
        Git      2 repositories, 3 worktrees: no changes, stashes or unpushed commits
        Scratch  synchronized at 11:44 (151 files)
        History  nothing waiting to reach this Mac
        Agents   none running
      Not checked: files Git ignores, home-folder data outside sync.
      Type delete to delete it.

- After the typed `delete` (terminal only, bound to the name and account): record the owner stop, stop it, confirm
  it is Shutdown, re-check the fingerprints; any change voids the verdict. A 2xx is confirmed by a full inventory; a
  timeout is unknown until the inventory says otherwise. Phases are journaled so a rerun never repeats an unchecked
  delete.
- Cleanup, journaled and retried until done. Deleting the active pairing stops its keeper, worker and forwards,
  retires the binding into history, disarms with a printed line (call 4), and removes the SSH block and Herdr profile
  setup recorded as its own (`herdr machine remove`). Deleting a retired OLD leaves NEW's pairing, keeper, consent,
  alias and profile untouched. Scratch payloads, checkpoints, relay records and the restart ledger are always kept.

### D. Replace (rulings 8, 6, 22, 21)

One operation with a journal in the project state (operation id, OLD, NEW, account, OLD's resources, reviewed commit,
a receipt per phase) and one lock. A rerun prints the phase it is in and continues it (REPORT §3):

1. **Inspect OLD:** its live machine and cores, idle timeout, billing owner. Unreadable resources stop the operation
   (no four-core fallback). Starting a stopped OLD to read it is an explicit choice.
2. **Create NEW** on the reviewed default branch with OLD's machine and idle timeout. A replacement receipt names NEW
   before validation; after a lost create reply the rerun adopts NEW from the inventory instead of creating again.
3. **Validate and personalize NEW.** A dotfiles failure retries dotfiles; it never rebuilds.
4. **Move** sync, recovery (stage A's consent move) and Herdr to NEW. A nonzero Herdr result is not "connected".
5. **Sign in fresh** with the step-3 sequence (every selected tool, Grok and each Claude account). A failed or skipped
   tool joins a retry list and does not block steps 6-7 (call 3).
6. **Check NEW:** validation, a completed sync cycle, NEW's keeper if armed, Herdr reachable, and every OLD
   conversation revision present on NEW through #100's relay (by revision and content, not by count).
7. **Offer stage C's delete of OLD**, with a new check of OLD. Declining keeps both; `task work` keeps listing OLD.

Before step 4, cancelling keeps OLD paired and NEW listed (never deleted automatically). After step 4 the operation
finishes forward; going back is an explicit rebind (call 8).

The coordinator lives in `devcontainer_setup/replacement.py`, because it needs `Codespaces.launch`, which generated
projects do not vendor (`render.py:400-407` copies only `project_sync/*.py`). The wizard's Codespace menu gains
`replace <paired>` (the wizard design's call 1). `task work` runs it through the pinned setup runner in a replace mode.
Check and delete live in `project_sync`, so they work from the vendored runtime.

## Owner inputs (pending; each changes the build)

1. **What the delete check must cover.** Git, scratch, conversations and live agents are checked. Files Git ignores
   and home-folder data outside sync (the 10-25 hand check looked at both) are named under "Not checked" and do not
   block. Is that enough, or must delete inventory and export them before it offers `delete`? Exporting them is a
   new feature with its own size and secret-handling rules.
2. **Conversations a replacement cannot open.** When a conversation is kept on the Mac but NEW refused to publish it,
   may OLD still be deleted (the verdict names each one), or must it block? And should `task work` offer "forget this
   conversation" (stop relaying it and clear the degraded sync it causes today; #100's call 5 left that to this
   step), keeping the Mac copy?

## Constraints

- Generated projects run older pins until rolled out, and old pins reject unknown keys in shared files (wizard design,
  Constraints): new state goes in new files, written atomically and private.
- Every existing safety check stays: reviewed-head and default-branch containment, receipt before validation, the
  exact-Codespace consent check for ordinary connects, the keeper's name-versus-pairing refusal.
- The sign-ins step ships before stage D (ruling 20 order).
- Commit messages plain imperative, no AI attribution. Gates: `task check`, `task test`.

## Tests the plan must include

REPORT's five red probes are analysis scaffolding; the builder writes its own red tests in the suite for the same
behaviors (replace offered for a paired premium 120-minute Codespace before allocation; a replace mode; a delete
check; headless stop; a same-account move keeping consent). Plus REPORT §7's acceptance list, at least:
- **B:** each #87 state; zero starts, downloads and logins while printing (fake API counting calls); no TTY prints
  `Next:` and exits 0; 6 lines, 78 columns, 3-6 actions; the 2-second deadline with probes joined.
- **C:** a clean main plus a dirty second worktree, or a stash → UNSAFE and zero DELETE calls; late OLD scratch,
  incomplete history, a malformed Herdr reply → UNKNOWN; only the exact word proceeds; EOF, headless or a changed
  target → zero destructive calls; deleting OLD leaves NEW's keeper, consent, alias and profile byte-identical.
- **D:** premium/120 m beats a stale four-core receipt; unreadable resources fail closed; a failure at each phase
  resumes on rerun; a lost create reply never creates twice; a dotfiles retry never rebuilds; OLD → Mac → NEW
  conversations arrive with exact revisions.
- **A:** a same-account move prints one line and keeps timestamps, hold and ports; a changed billing owner asks; an
  ordinary connect still refuses a mismatched target.
- Live (ruling 20): on a throwaway Codespace from the innovate test branch, replace → check → typed delete, then
  cleanup of everything the test created.

## Left to the builder

Module names (REPORT suggests `project_sync/work.py` and `project_sync/deletion.py`), the journal encoding, the Gum
layout, the bounded probe's structure, the strict Herdr agents reader.

## Calls made by the lead (each can be overruled)

1. **Four stages, one push and one roll-out per step.** One live test exercises replace end to end; rolling out each
   stage would mean twelve project PRs with nothing for the owner to do differently in between.
2. **Agents can check, not delete.** `--check --json` runs headless; deleting needs the typed word in a terminal and
   there is no `--yes` (rulings 7 and 8: stop is headless, delete "takes a typed `delete`").
3. **A failed sign-in does not block replace's check or the delete offer.** It is listed for retry: sign-ins can be
   redone on NEW, OLD's data cannot be recovered after deletion.
4. **Deleting the active pairing disarms it**, with a printed line: #60 was a project armed to a Codespace that no
   longer existed.
5. **NEW is built from the reviewed default branch.** Feature-branch work stays on OLD until its checked delete
   shows it pushed.
6. **The replace coordinator is setup-level; check and delete are in the vendored runtime** (packaging boundary).
7. **The printer's remote reads are bounded at 2 seconds** and shown dated or `unknown`.
8. **Before the move, cancel keeps OLD paired and NEW listed; after it, finish forward**; going back is an explicit
   rebind through the existing safe path.
