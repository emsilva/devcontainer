# `task work`: one entry point, replace and a checked delete: design (#87, #62)

Date: 2026-11-02. The fourth step of the owner's program (ruling 14 order: link helper, wizard, sign-ins, `task
work`). Owner rulings 1, 5-8 (2026-10-25), 22 (2026-11-01) and 24 (Grok, 2026-11-02), in
`tmp/codespace-lifecycle/ux-run-2026-10-25/RULINGS.md`; ruling 20 ("Only for real decisions") lets the lead write
this, have it checked, plan and build it. Issue texts (offline): `tmp/codespace-lifecycle/task-work/issues.md`.
Read-only analysis with file:line evidence and five red tests against 936db93:
`tmp/codespace-lifecycle/task-work/analysis/REPORT.md` (REPORT). Builder check:
`tmp/codespace-lifecycle/task-work/builder-check/CHECK.md` (CHECK), settled below.

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
- Ruling 7: "Agents may stop a Codespace headless; restart stays human-only." Its source (TYPED-UX.md:123): "a lead
  can't stop its own Codespace at the end of a plan."
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
  armed project, consent carries over a move with one printed line, and an agent, on the Mac or inside the
  Codespace, can stop it headless.
- Replace is one rerunnable operation: create like the old one, flush the old one, move sync, recovery and Herdr,
  sign in fresh, check, then offer the checked delete. It is reached from the wizard's Codespace menu and `task work`.
- `task work:cs:delete` checks, shows a verdict per category, takes a typed `delete`, deletes, and removes only what
  setup created for that Codespace.

## Non-goals

- The sign-in sequence itself (step 3, `docs/superpowers/specs/2026-11-02-fresh-signins-design.md`); replace calls it.
  Connecting without entering Herdr's TUI (`herdr.py:249-251`) is built there (its How it works needs it too) and
  reused here.
- #87 items 9-10: the copy pass naming `task work` everywhere, the Herdr popup key, sidebar token and toasts.
- A full-screen TUI (ruling 1). Restart without a terminal (ruling 7). `task work` in a repository without the
  generator (the README's install line stays the way in).

## Anti-goals

- Printing state that starts, downloads, signs in or spends anything. `_choose_pairing` starts stopped Codespaces
  and `fetch_release()` downloads (REPORT §2): neither is a reader.
- Calling a Codespace deleted on a 404 alone (#60's rule: only a full inventory proves it).
- A second replacement because a later phase failed, or a second Codespace after a lost create reply (today
  `codespaces.py:370-372` stops with "Check gh codespace list before retrying" and records nothing).
- Deleting on a stale verdict, without the typed word, or through a `--yes`.
- Any write to `arming.json` when the target is not the active pairing: its `owner_stop_at` is project-wide
  (`arming.py:156-157`), and the keeper would read NEW's next lifetime stop as "stopped by you" (`lifecycle.py:213-215`,
  `keeper.py:591-593`) and never restart it (CHECK C1).
- Loosening `_consent_on_file`'s exact-Codespace check (`project_sync/__main__.py:62-84`) to let a move pass, or
  calling `arm()` to transfer consent (it resets the hold, owner stop and timestamps).
- A second conversation transport: #100's relay stays the only one.
- Removing an SSH block or Herdr profile setup did not create; wiping `known_hosts`, keys or the state directory.

## How it works

### Four stages, two releases

Step 4 starts once the sign-ins step has shipped (ruling 20 order). Each stage is planned, built and gated alone: **A**
smarter commands; **B** `task work` printer and menu; **C** checked delete; **D** replace (needs C and the sign-ins
step). A and B are pushed and rolled out when green, with a live check of the stop and the move; C and D together after
the full live test (call 1). A stage that is not released stays on its branch, so a generator fix pushed meanwhile never
carries it.

### A. Smarter commands (rulings 5, 6, 7)

- **Stop on the Mac** runs before the terminal gate (`project_sync/__main__.py:352`) and keeps recording the owner stop
  before the request. Restart and a first `arm` stay terminal-only. Its Taskfile annotation stops saying interactive.
- **Stop from inside the Codespace** (call 9): `_owner_power` refuses there today (`__main__.py:223-228`), and the
  pairing and `arming.json` live on the Mac. Inside a Codespace, `task work:cs:stop` leaves a stop request that the
  Mac's worker picks up, as it picks up link requests today; the Mac records the owner stop, then stops. With no
  worker running it says so and nothing stops (the idle timeout still applies).
- **Plain `work:cs -- --rebind NEW` on an armed project moves recovery too** (today it refuses,
  `__main__.py:363-368`). Order (CHECK C6, Q8): read NEW's billing owner and decide consent between `_choose_pairing`
  and `scratch.retarget`, asking any typed `arm` outside `workspace-control.lock` (`_arm` takes that lock and
  `locked()` is not reentrant, `state.py:283-296`); write a move intent; stop and join OLD's keeper; retarget; write
  the consent move; start NEW's keeper; connect Herdr. A decline moves nothing. A run that finds a move intent finishes
  it instead of asking again. `:armed` stays as an alias.
- **Consent move:** a new `arming.py` helper, called only by a verified rebind or replace. When the saved digest matches
  OLD's consent text, the gh account is the same and NEW's live billing owner is the same, it rewrites the target and
  digest for NEW, keeps `consented_at`, `armed_at`, ports, hold and the restart ledger, records `moved_from` and
  `moved_at` (`arming.read` accepts new keys, `arming.py:20-27`), and prints `Recovery moved OLD → NEW; consent from
  DATE kept (ACCOUNT, billing OWNER).` Otherwise it asks for a typed `arm` for NEW. OLD's owner stop and pending
  restore are retired with its history (REPORT §5).

### B. `task work` (#87 items 6-7, rulings 1, 5)

- A root `task work` with a `desc` (#70), dispatching to the vendored runtime, wherever continuity is on (all three
  projects). Reading state, choosing actions and rendering are separate functions.
- "Not set up" means partial setup: metadata without a registry or pairing, or an open setup PR; it offers setup.
- The first frame comes from local state alone (registry, receipts, keeper and worker records, arming). Remote reads
  (Codespace state, setup PR, upstream tip) run in one bounded probe with a 2-second deadline, shown with their age or
  as `unknown` when unfinished, and joined before exit (call 7). It never calls today's whole `workspace.status()`,
  whose probes allow up to 30 s each (REPORT §2).
- The setup PR, the launch receipt, `sourceRef` and the checkout comparison come from setup modules that generated
  projects do not vendor (`render.py:404-406`): the runtime gets small readers of its own, and a test pins them to the
  setup functions (CHECK Q2).
- Line one is the most urgent fact. For an armed project whose keeper is not running: `Recovery is off until you
  connect` (ruling 5; resuming on connect is already built, REPORT §2).
- The menu offers 3-6 actions, the likely one first: Available → connect; running, not armed → connect, arm; stopped by
  you → start and connect; unexpected stop → the reason, then connect; failed creation → logs or resume; confirmed
  deleted → rebind or create; setup PR open → resume setup; checkout behind → `git -C <checkout> merge --ff-only
  origin/<default>` (Git refuses an unsafe case itself); newer generator → upgrade. Each prints its exact command before
  running it. Details prints `work:status`. Without a TTY: the state, then `Next:` lines, exit 0.

### C. Checked delete (ruling 8, #62)

- `task work:cs:delete [-- --codespace NAME]`; without a name, the current pairing. `-- --check --json` runs the
  checks headless and makes no destructive call (call 2). The repository and account are verified on every run.
- Checks run over the package's read-only connection, never raw `gh codespace ssh`. A stopped or unreachable target
  reads `unknown` with an explicit "start it to check" action; it is never started silently. A target never paired
  from this Mac is checked only when a replace journal names it (its host key is accepted then); otherwise `unknown`,
  "pair it first" (CHECK Q9). Per category (REPORT §4):
  - **Git:** every repository and linked worktree under `/workspaces` (not only `Codespaces.roots()`, which looks at
    100 top-level folders): changes including untracked files, stashes, and each branch and detached head as ref → id.
    The Mac runs `git ls-remote` and the containment test in its own checkout, after a fetch there (the Codespace's
    non-login `python3` has no `GITHUB_TOKEN`, `transport.py:415-418`, `codespaces.py:40-46`). An id the Mac cannot
    find in a remote tip is unpushed (CHECK Q3).
  - **Scratch:** the current pairing synchronized with no conflicts; for a retired OLD, its files compared with the
    final flush that replace recorded (stage D step 3).
  - **History:** for the current pairing, a completed cycle with nothing pending, incomplete or unsupported; for a
    retired OLD, no conversation turn newer than the final capture replace recorded (owner input 2 sets what "kept on
    the Mac" allows).
  - **Agents:** a process inventory on the target over the same connection (herdr, claude, codex, grok, opencode),
    not the project's Herdr machine, which points at NEW after a move (CHECK Q10). A failed read is `unknown`.
- Each category reads SAFE, UNSAFE (with the outstanding paths, counts or commit ids) or UNKNOWN; deletion is offered
  only when all are SAFE. Contents, remote URLs and credentials are never shown. The verdict names its scope:

      Checked bookish-zebra-… at 11:44:
        Git      2 repositories, 3 worktrees: no changes, stashes or unpushed commits
        Scratch  synchronized at 11:44 (151 files)
        History  nothing waiting to reach this Mac
        Agents   none running
      Not checked: files Git ignores, home-folder data outside sync,
      Grok and OpenCode conversations.
      Type delete to delete it.

- After the typed `delete` (terminal only, bound to the name and account), in this order (CHECK C2): re-check the
  fingerprints while it is still Available (any change voids the verdict); record the stop, in `arming.json` only for
  the active pairing and in the delete journal otherwise (CHECK C1); stop it; confirm Shutdown and its identity by
  API; delete. A 2xx is confirmed by a full inventory; a timeout is unknown until the inventory says otherwise. Phases
  are journaled: a rerun after a stop whose delete failed reads `unknown` and needs an explicit start to check again.
- Cleanup, journaled and retried until done. Deleting the active pairing stops its keeper, worker and forwards,
  retires the binding into history, disarms with a printed line (call 4), and removes the SSH block and Herdr profile
  setup recorded as its own (`herdr machine remove`). Deleting a retired OLD leaves NEW's pairing, keeper,
  `arming.json`, alias and profile byte-identical. Either way OLD's paused Mutagen session is terminated (it is only
  paused at a move, `scratch.py:586-589`). Scratch payloads, checkpoints, relay records and the restart ledger are kept.

### D. Replace (rulings 8, 6, 22, 21)

One operation with a journal in the project state (operation id, OLD, NEW, account, OLD's resources, the commit NEW is
built from, the inventory and request time before create, a receipt per phase) and one lock. A rerun prints the phase it
is in and continues it (REPORT §3). The journal ends when the operation completes, when the owner abandons it (NEW is
left to the checked delete), or when either Codespace is deleted (CHECK Q11).

1. **Inspect OLD:** its live machine and cores, idle timeout, billing owner. Unreadable resources stop the operation
   (no four-core fallback). Starting a stopped OLD to read it is an explicit choice. Consent for NEW is decided here
   (stage A's rules); a decline ends the operation before anything is created (CHECK Q7).
2. **Create NEW** from `origin/<default>`'s tip, when it contains the checkout's installed setup files (otherwise it
   refuses and offers setup; CHECK Q4), with OLD's machine and idle timeout. It uses the ordinary launch receipt, which
   then names NEW; OLD's receipt is kept in the journal, and the resume hint names the replace rerun (CHECK Q5). The
   creation callback records NEW's start evidence for its keeper anchor (`codespaces.py:393-395`; CHECK M2). After a
   lost create reply the rerun adopts exactly one new Codespace on that repository and branch created after the
   request; with none it rechecks for a bounded time, then asks before creating again; with several it asks (Q6).
3. **Validate and personalize NEW, then flush OLD** (CHECK C5, M1). A dotfiles failure retries dotfiles; it never
   rebuilds. Then, while OLD is still paired: list its running agents and ask the owner to end them or wait (never
   killed); run one final normal cycle (scratch flush and conversation capture) and record its receipts in the
   journal. After the move nothing can carry OLD's late writes to the Mac.
4. **Move** sync, recovery (stage A's consent move) and Herdr to NEW, without entering Herdr's TUI. A nonzero Herdr
   result is not "connected".
5. **Sign in fresh** with the step-3 sequence (every selected tool, Grok and each Claude account). A failed or skipped
   tool joins a retry list and does not block steps 6-7 (call 3).
6. **Check NEW:** validation, a completed sync cycle, NEW's keeper if armed, Herdr reachable, and every OLD
   conversation revision present on NEW through #100's relay (by revision and content, not by count).
7. **Offer stage C's delete of OLD**, with a new check of OLD against step 3's receipts. Declining keeps both; `task
   work` keeps listing OLD.

Before step 4, cancelling keeps OLD paired and NEW listed (never deleted automatically). After step 4 the operation
finishes forward; going back is an explicit rebind (call 8).

**Where it runs** (call 6, CHECK Q1). The coordinator lives in `devcontainer_setup/replacement.py`, because it needs
`Codespaces.launch`, which generated projects do not vendor (`render.py:400-407`). The wizard's Codespace menu gains
`replace <paired>` (the wizard design's call 1). `task work` reaches it through the pinned setup runner in a replace
mode. The coordinator does inspect, create, validate and personalize itself; the flush, move, check and delete run as
subprocesses of the project's own `project-sync.sh`, so the worker and keeper they start are the vendored runtime's
(`runtime_id` hashes the package's path, `state.py:310-322`, and a worker started from the release cache would read as
not current, `workspace.py:143-146`). In this repo the coordinator runs from `scripts/setup.sh`.

## Owner inputs (pending; each changes the build)

1. **What the delete check must cover.** Git, scratch, Claude and Codex conversations, and live agents are checked.
   Files Git ignores, home-folder data outside sync (the 10-25 hand check looked at both), and Grok and OpenCode
   conversations (not captured today, `conversations.py:462`, `596`) are named under "Not checked" and do not block.
   Is that enough, or must delete inventory and export them before it offers `delete`? Exporting them is a new feature
   with its own size and secret-handling rules.
2. **Conversations a replacement cannot open.** When a conversation is kept on the Mac but NEW refused to publish it,
   may OLD still be deleted (the verdict names each one), or must it block? And should `task work` offer "forget this
   conversation" (stop relaying it and clear the degraded sync it causes today; #100's call 5 left that to this
   step), keeping the Mac copy?

## Constraints

- Generated projects run older pins until rolled out. A shared file whose loader rejects unknown keys (the user
  `config.json`, wizard design Constraints) gets no new key; `arming.json` may (CHECK C7). New files are written
  atomically and private.
- Every existing safety check stays: reviewed-head and default-branch containment, receipt before validation, the
  exact-Codespace consent check for ordinary connects, the keeper's name-versus-pairing refusal.
- Docs (#62): README's "can be deleted at any time" (`README.md:106`) and `docs/project-continuity.md` name the safe
  order (replace, check, then the checked delete) and the commands.
- Commit messages plain imperative, no AI attribution. Gates: `task check`, `task test`.

## Tests the plan must include

REPORT's five red probes are analysis scaffolding; the builder writes its own red tests in the suite for the same
behaviors (replace offered for a paired premium 120-minute Codespace before allocation; a replace mode; a delete
check; headless stop; a same-account move keeping consent). Tests that pin today's behavior change on purpose:
`tests/test_project_sync_cli.py:637` (plain armed rebind refused) and `tests/test_root_tasks.py:49-50` (`work:cs:stop`
interactive). Plus REPORT §7's acceptance list, at least:
- **A:** a same-account move prints one line and keeps timestamps, hold and ports; a changed billing owner asks; a
  crash between retarget and the consent move is finished by the next `work:cs`; an ordinary connect still refuses a
  mismatched target; a stop requested inside a Codespace is recorded on the Mac before the request.
- **B:** each #87 state; zero starts, downloads and logins while printing (fake API counting calls); no TTY prints
  `Next:` and exits 0; 6 lines, 78 columns, 3-6 actions; the 2-second deadline with probes joined.
- **C:** a clean main plus a dirty second worktree, or a stash → UNSAFE and zero DELETE calls; an unpushed commit the
  Mac cannot find → UNSAFE; late OLD scratch, a turn newer than the final capture, a failed process read → UNSAFE or
  UNKNOWN; only the exact word proceeds; EOF, headless or a changed target → zero destructive calls; deleting a retired
  OLD leaves NEW's `arming.json` byte-identical and NEW's next lifetime stop is still restarted (CHECK C1).
- **D:** premium/120 m beats a stale four-core receipt; unreadable resources fail closed; a failure at each phase
  resumes on rerun; a lost create reply never creates twice; a dotfiles retry never rebuilds; the worker and keeper
  after a replace are current for the vendored runtime; OLD → Mac → NEW conversations arrive with exact revisions.
- **Live** (ruling 20; call 10): from a clone of innovate with its own `XDG_STATE_HOME`, with `~/.ssh/config` and the
  Herdr machine list backed up and real innovate's alias block set aside and restored (owner ruling 2026-10-27
  "innovate, with backup"; `tmp/codespace-lifecycle/keeper-safety/T4-RUNBOOK.md`): pair a throwaway Codespace from a
  test branch, replace it (NEW from the test branch through a test-only override), check, typed delete, then clean up
  everything the test created.

## Left to the builder

Module names (REPORT suggests `project_sync/work.py` and `project_sync/deletion.py`), the journal encoding, the Gum
layout, the bounded probe's structure, the Codespace-to-Mac stop request's format, the test-only branch override's name.

## Calls made by the lead (each can be overruled)

1. **Four stages, two releases.** A and B ship when green; C and D after one live test of replace end to end. One
   release per stage would mean twelve project PRs with nothing for the owner to do differently in between; one
   release for all four would hold a finished headless stop behind the whole step (CHECK "Stage order").
2. **Agents can check, not delete.** Ruling 8's "scriptable `work:cs:delete`" is read as: a task command an agent can
   run with `--check --json` headless; deleting needs the typed word in a terminal and there is no `--yes` (rulings 7
   and 8: stop is headless, delete "takes a typed `delete`"). REPORT §8.3 named this an owner question; it is the
   cautious reading and can be widened later.
3. **A failed sign-in does not block replace's check or the delete offer.** It is listed for retry: sign-ins can be
   redone on NEW, OLD's data cannot be recovered after deletion.
4. **Deleting the active pairing disarms it**, with a printed line: #60 was a project armed to a Codespace that no
   longer existed.
5. **NEW is built from the default branch's tip.** Feature-branch work stays on OLD until its checked delete shows it
   pushed.
6. **The replace coordinator is setup-level; flush, move, check and delete run in the project's own runtime.**
7. **The printer's remote reads are bounded at 2 seconds** and shown dated or `unknown`.
8. **Before the move, cancel keeps OLD paired and NEW listed; after it, finish forward**; going back is an explicit
   rebind through the existing safe path.

## Builder check (2026-11-02) and the calls it settled

A fresh reader checked the first draft (52ac368) against the code and found 11 questions, 7 contradictions and 8
missing pieces; the lead verified each code claim it relied on in source (CHECK). The sections above now carry the
answers. The most serious: deleting a retired OLD would have recorded the owner stop in NEW's `arming.json` and
switched off NEW's automatic restart (C1); the draft re-checked fingerprints after the stop, when the read-only
connection can no longer reach it (C2); it dropped the final flush of OLD before the move (C5); and a replace run from
the release cache would leave a worker the project's runtime does not count as current (Q1). The calls:

9. **Ruling 7 covers an agent inside the Codespace**, as its source says ("a lead can't stop its own Codespace"): the
   request goes through the Mac, which records the owner stop (C3). If a Mac-side agent is enough, this shrinks.
10. **The live test follows the 2026-10-27 ruling "innovate, with backup"**, as today's live session did, with a
    test-only branch override that the menu never shows (C4). No new owner input.
11. **The consent decision comes before the retarget, its prompt outside the lock, with a move intent for a crash
    between them** (C6, Q8).
12. **The Git check's containment test runs on the Mac** (Q3); **agents on OLD are read as processes** (Q10).
13. **Step 7 checks OLD against the final flush's receipts**, so no capture of an unpaired OLD is needed (C5).
