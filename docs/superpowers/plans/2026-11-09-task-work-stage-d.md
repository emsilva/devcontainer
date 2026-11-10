# `task work` stage D: replace the paired Codespace in one rerunnable operation. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `task setup -- --replace` (and `replace NAME` in the wizard's Codespace menu, and `task work`) replaces the
paired Codespace OLD with a NEW one. It reads OLD's live machine, cores, idle timeout and billing owner, then creates
NEW from the default branch's tip with OLD's machine and idle timeout, and validates and personalizes it. Next it ends
OLD's agents (asking, never killing) and records OLD's final flush. It moves sync, recovery and Herdr to NEW without
entering Herdr's TUI, signs in fresh, and checks NEW. Then it offers stage C's checked delete of OLD. A rerun after any
failure or crash prints the phase it is in and continues it. It never creates a second Codespace after a lost reply,
and never rebuilds for a dotfiles retry.

**Architecture:** One coordinator, `devcontainer_setup/replacement.py`, setup-level and not vendored (it needs
`Codespaces.launch`). It holds `STATE/replace.lock` for its whole run and is the only writer of the journal
`STATE/replace.json` (STATE = the project state directory, `project_sync.state.state_dir`). It inspects, creates,
validates and personalizes itself. The flush and move (`project-sync.sh replace cutover`), the NEW check
(`project-sync.sh replace check`), the sign-ins (`work --signin-all`) and the delete (`delete --codespace OLD`) run as
subprocesses of the project's own `project-sync.sh`, so the worker and keeper they start are the vendored runtime's.
Each subprocess writes one JSON receipt file that the coordinator journals. Three runtime modules are vendored like
every `project_sync/*.py`: `replacing.py` reads the journal and its receipts, `cutover.py` seals OLD and moves, and
`replace_check.py` checks NEW. Stage C gains a `replacement` target kind (a NEW a journal names that was never
paired), and garbage collection keeps every manifest a final-flush record names.

**Tech Stack:** Python 3 standard library (vendored, run as `python3 -I -B`), go-task, `unittest`, Git, Mutagen and gh
(already pinned).

**Spec:** `docs/superpowers/specs/2026-11-02-task-work-design.md`, section "D. Replace" and "Where it runs", its Goals,
Anti-goals, Constraints, "Tests the plan must include" (D and Live lists), calls 3, 5, 6 and 8, builder-check calls 11
and 13. Stage C plan `docs/superpowers/plans/2026-11-09-task-work-stage-c.md`, "Left for stage D" and its Live check
(CPLAN). Source inventory at f29835f plus Task 5 at 4697af1:
`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-d/inventory/INVENTORY.md` (INV §n). Revision
6 answers the plan checks
`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-d/plancheck/CHECK.md` (F1-F18),
`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-d/plancheck2/CHECK.md` (N1-N11),
`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-d/plancheck3/CHECK.md` (M1-M2 and its
notes), `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-d/plancheck4/CHECK.md` (Q1-Q4) and
`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-d/plancheck5/CHECK.md` (V1-V2). **Line numbers are at c4088a3**; stage C Task 8 (CPLAN) then changes `deletion.py`, `workspace.py`,
`target_scan.py`, `verdict.py` and `seal.py`, so builders re-derive any line in those files and any line a brief
does not repeat.

## Global Constraints

- Gates: `task check` and `task test` (`python3 -m unittest discover -s tests -v`), both exit 0, at your commit, in the
  default environment (no sandbox, no `TMPDIR` override):
  `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-a/RULING-gates.md`.
- Focused runs: `python3 -m unittest …` unpiped (or redirected to a file, then read `$?`).
- Branches: cut from `feature/task-work-cd` at the commit your brief names, merged back into it, never `main`. C and D
  ship together after one live test of replace end to end (spec call 1).
- Commit messages: plain imperative sentence case, one concern per commit, no AI attribution of any kind.
- **Nothing is spent before inspection passes.** Zero `codespace create` calls unless all of these hold:
  - OLD's machine name, core count, idle timeout and billing owner were read from its live API body and are valid;
  - OLD's machine is offered for the pinned commit with the same core count;
  - the pinned commit carries the checkout's installed setup (call 5);
  - the default branch still points at the pinned commit when re-read just before the create (call 6);
  - the journal on disk already holds the request.

  Tests count create calls.
- **One Codespace per operation.** At most one `codespace create` per operation. The only exception is the owner
  typing `create` after the bounded recheck found no new Codespace (call 10). No path deletes NEW. OLD is deleted
  only through `deletion.run` and its typed word.
- **A dotfiles retry never rebuilds.** Zero `codespace rebuild` calls when only personalization failed.
- `arming.json` is written only by the existing move path (`move.commit`, `__main__._arm`), never by
  `replacement.py`, `cutover.py` or `replace_check.py` (CHECK C1 still holds: a retired OLD's delete leaves NEW's bytes
  identical). Consent checks stay exact (`_consent_on_file`, `move.check_arming`).
- **One writer per record.** Only `replacement.py` writes `STATE/replace.json` and `STATE/replacements/`. Runtime
  subprocesses write only their receipt file (the path they are given), plus what the subsystems they call already
  write. No runtime code takes `replace.lock`.
- **The coordinator starts nothing in-process.** `replacement.py` never calls a function that starts, stops, adopts or
  retargets a worker, keeper, Mutagen session, SSH alias or Herdr machine. Those run only in the subprocesses, with
  `cwd=PROJECT` and `--project PROJECT` (spec "Where it runs"; `runtime_id` hashes the package path,
  `state.py:310-322`).
- No prompt between the final flush and the retarget. Once `seal.write` returns, `workspace.remote` runs with no `ask`,
  `input` or `Gum` call in between (call 7).
- **What is stored.** Journals and receipts hold names, logins, 40-hex commit ids, epochs, counts, tool keys, agent
  names, session ids and fixed texts only. They never hold gh, Git or Herdr output, remote URLs, contents or
  credentials. Terminal output keeps each reused command's existing behavior (call 19). New lines this plan adds are
  the Copy section's.
- Replace runs on the Mac, in a terminal, with a real gh account. It refuses inside a Codespace
  (`CODESPACES == 'true'`), without a TTY, and with `GH_TOKEN` or `GITHUB_TOKEN` set.
- Stdlib only. New state files use `project_sync.state.atomic_write` (private, atomic). No new key in the user
  `config.json` or in the setup launch receipt (`codespaces.py:375-377`).
- Do not name a module `work.py` (`render.RETIRED_HELPERS`, `devcontainer_setup/render.py:26-27`).
- No client, organization or Codespace names in code, tests, docs or commit messages (the repository is public).
- Times shown are local `HH:MM`, or `YYYY-MM-DD HH:MM` when not today (as `overview` shows them).

## Calls made by the lead (each can be overruled)

1. **One journal, one writer, kept after it ends.** `STATE/replace.json` holds the one active operation. When it ends
   (`completed`, `abandoned`, or `deleted`), it is archived to `STATE/replacements/<operation>.json` and kept. An
   archived journal still names its NEW, which is what lets the checked delete accept NEW's first host key after an
   abandon (spec: "NEW is left to the checked delete"; INV §9).
2. **A delete ends the journal by fact, not by message.** A journal ends `deleted` when `deletions/<old or
   new>.json` is at `deleted` or `cleaned`. The delete never writes the replace journal and never takes
   `replace.lock`, so a delete run as the coordinator's child (step 7) cannot block on its parent. The coordinator
   archives such a journal on its next run. `standing` and `verdict.resolve` treat it as ended at once. A
   `replacement` target that GitHub already shows deleted gets a `cleaned` deletion journal, so this fact exists for
   it too (F9).
3. **Only the paired Codespace is replaced**, on any branch: NEW is always on the default branch (spec call 5). No
   pairing, or a pairing for another project, refuses before any API call.
4. **Resources come only from OLD's live API body** (`Codespaces.get`): `machine.name`, `machine.cpus` (an int above
   0, not a bool), `idle_timeout_minutes` (an int above 0) and `billable_owner.login`. Anything missing stops the
   operation before anything is spent. The launch receipt, the wizard's four-core default and the rebuild fallback
   are never consulted (INV §2, §12). OLD's machine must appear in `machines(repo, pinned commit)` with the same
   `cpus`. Location and retention are not copied: the spec names machine and idle timeout.
5. **The pinned commit must carry the checkout's installed setup** (F5, N7). That holds when all are true:
   - `render.validate` returns no errors for the checkout, and none for the tree at the pinned commit. That tree is
     checked out in a temporary detached worktree at a unique path under the setup worktree root, and that path alone
     is removed with `git worktree remove --force` in a `finally` (never the setup-branch helpers, which refuse a
     detached tree). Each validation applies the renderer's ownership, user-key and lock-normalization rules to its
     own tree.
   - The two `customizations.devcontainerSetup` objects have equal `sourceRef`, `selections`, `managedConfig` and
     `managedFiles`. Both trees validate against their own marker, so equal markers mean equal managed setup, while
     unrelated user keys may differ.

   Otherwise the replace refuses and names `task setup` (CHECK Q4). The test-only override
   `SETUP_REPLACE_TEST_BRANCH=<branch>` replaces the default branch with that branch for the fetch, the pin and the
   create. It is read only from the environment, and no menu or help text shows it (spec call 10).
6. **NEW is built from the default branch's tip, pinned at inspection and re-read before spending** (F6). `git fetch`
   the branch (`git_ops._fetch_default`) and pin the 40-hex `refs/remotes/origin/<branch>` in the journal. Just
   before `codespace create`, fetch again. If the tip moved, inspect the setup at the new tip, re-pin and journal it,
   and only then create. `--branch` still selects the checkout, so a push in the seconds between that re-read and
   GitHub's clone can give NEW a descendant commit. That case is accepted as named: `launch` requires NEW's HEAD to
   contain the pin, and `validate-selected.sh` validates what NEW actually built. Every resume of this operation uses
   the journal's pin.
7. **The typed `arm` comes before the final flush.** The spec runs stage A's consent at step 4. Here `move.decide`
   (which asks any typed `arm`) runs first inside the cutover, then the agents prompt, then the flush, the seal and
   the retarget. A prompt between the seal and the retarget would let OLD change after its final flush, with no way
   to flush again once paired elsewhere. `move.check_arming` still refuses if `arming.json` changed meanwhile, which
   the check found rare (`keeper.py:724` writes it only to clear a matching `pending_restore`). A rerun then decides
   again and seals again.
8. **Agents are the owner's to end.** Before the flush, the cutover scans OLD. While a `claude`, `codex`, `grok` or
   `opencode` process runs (the stage C rule: a Herdr server alone does not block), it prints them and asks the owner
   to press Enter to check again, or to type `stop`. Nothing is ever killed.
9. **What the final flush requires.** Paired `deletion.gather` evidence for OLD that is `steady`, where
   `verdict.judge` gives SAFE for Scratch, History and Agents. Git is not required: OLD keeps its commits until the
   checked delete proves them pushed. Then `seal.write(..., evidence=evidence, operation=<operation>)` runs, and the
   retarget follows. A rerun before the move seals again (`seal.write` overwrites, `seal.py:131`). `gather`'s wait
   for a later normal cycle (up to two brackets) is the spec's "one final normal cycle". No capture is requested.
   `seal.write` itself also refuses unsteady evidence and a History that is not SAFE (CPLAN Task 8 R6); the cutover
   checks first and prints its own line, so that refusal is a backstop.
10. **A lost create reply is resolved from the inventory, never by creating again** (F1). Before `codespace create`
    the journal records `request = {at, before}`: the request time and the names of every Codespace on the
    repository. That covers an exception, a lost or empty reply, and a killed process. From then on, any run that
    finds `create-requested` with no NEW reconciles before anything else, keeping the original `at` and `before`.
    - First, `api.user()` must equal the journal's account, and the inventory must be complete
      (`existing(..., complete=True)`, Task 4). An incomplete or unreadable inventory proves nothing: the run stops
      with its phase kept and never offers `create` (N4).
    - **Candidates** are rows on the repository whose name is not in `before`, whose `git_status.ref` is the
      branch, whose `machine.name` is the journal's machine, and whose `created_at` (an ISO-8601 UTC string) parses
      as no earlier than `at - 120` seconds (clock-skew allowance). A row not in `before` is excluded as soon as one
      readable field disqualifies it (another ref, another machine, a `created_at` before `at - 120`), whatever else
      is missing (plan check 4 Q1). A row with no disqualifying field and a missing or unreadable one is read once
      more (`api.get(name)`); still incomplete, it is **uncertain**: never adopted without the owner's choice, and
      never counted as none.
    - NEW's branch receipt is not evidence here (plan check 3 M1): its path is per repository and branch
      (`codespaces.py:445-447`) and another setup on that branch rewrites it, so it never narrows the candidates.
    - Exactly one candidate and no uncertain row: it is adopted, as spec step 2 rules, and the `Found NEW…` line
      says what matched. That is the spec's heuristic, not proof of ownership: a clean unrelated Codespace with the
      same branch, machine and resources passes `launch(resume=…)`'s guards (`codespaces.py:339-352,398-404` refuse
      only other resources or user changes). The account, complete-inventory and machine checks narrow it.
    - None and no uncertain row: the coordinator rechecks every 10 s for up to 2 minutes, then asks the owner to
      type `create` (anything else stops, phase kept). Only that typed word allows a second POST. Every later
      reconciliation keeps the first request's `at` and `before` (the second POST's time is appended to
      `request.retries`), so a late Codespace from the first POST is still a candidate. After a second POST
      returns, the coordinator reads the inventory once more and names any other candidate in the `Also new…` line.
    - Several candidates, or any uncertain row: the owner chooses in Gum among those rows, each shown with its
      machine and creation time (`unknown` where unread), or `cancel` (stops, phase kept). No choice here allows a
      POST: another create needs the bounded recheck above to find neither a candidate nor an uncertain row (spec
      D step 2; plan check 5 V1). A row that stays unreadable therefore stops the replace until GitHub shows it
      whole or the owner deletes it, and the stop line says so.

    An adopted NEW gets its start evidence from the coordinator (`evidence.record_start(state, NEW, request.at,
    'created')`). Its first resume rebuilds once, because no receipt validated it. That rebuild is accepted on this
    rare path.
11. **Receipts** (F12). OLD's own launch receipt is `receipt_path(state_root, repo, <OLD's live git_status.ref>)` when
    it names OLD, and its bytes go into the journal as `old_receipt`. NEW uses the ordinary receipt for its own branch.
    That receipt's previous bytes, whatever they name, go into the journal as `branch_receipt`, and `launch` then
    rewrites it for NEW. A retry after a validation or personalization failure is `launch(resume=NEW)` with the
    journal's pin. With `validated_commit` equal to it, `launch` skips the rebuild (`codespaces.py:386-389`).
    When the live-branch receipt does not name OLD, `old_receipt` is the one `codespaces.find_receipt(state_root,
    repo, OLD)` finds (Task 4; the search `migrate_receipt` makes, `codespaces.py:450-469`), else null (N10).
    `launch` gains `resume_hint`, so its failure names the replace rerun.
12. **No TUI during the replace** (F14). `herdr.open_remote` and `workspace.remote` gain `enter=True`. With `False`,
    `open_remote` never runs the `herdr` client. It returns 0 once `_workspace` returned, the existing integer early
    returns from machine setup are unchanged, and `_workspace`'s `ValueError` still propagates. The cutover records a
    nonzero return as its code, and a `ValueError` raised after the binding names NEW as `1`. In both cases the move
    stands and NEW's check reads Herdr. The replace ends with `Connect with task work:cs.`
13. **Sign-ins are recorded, not required** (spec call 3). The coordinator runs `work --signin-all`, then reads
    `STATE/signins.json`. The retry list is every tool whose state is not `signed-in`, `other-method` or `absent`, when
    the record names NEW and was updated after the step began, or `["unknown"]` otherwise. Absent tools are not
    retries. "Fresh" means the existing sequence's fresh sign-in of signed-out tools, never a forced logout. Steps 6
    and 7 go ahead either way.
14. **NEW's check must pass before the delete offer.** Ruling 8 says "checks it, then offers to delete the old one".
    NEW passes when all five hold:
    - the launch receipt shows the pinned commit validated;
    - sync is ready (call 20);
    - recovery points at NEW (when armed);
    - Herdr reachable;
    - every conversation in OLD's final flush is on NEW, kept on this Mac only (NEW refused it, ruling 30), or
      forgotten (#100 call 5).

    A missing, pending or diverged conversation fails the check. OLD stays, and a rerun checks again.
15. **A NEW the journal names and that was never paired is a `replacement` target** (F9). Its first host key is
    accepted (`connection.accept_new = True`, `transport.py:312,363`). Its Git and Agents are checked as for any
    target. Its Scratch is SAFE only when the scan finds no file in `.scratch`, and its History only when it finds no
    conversation file (setup's `.scratch` markers are already excluded, `scratch_tree.py:8`). Anything present there
    exists only on NEW, so it reads UNSAFE. Its cleanup is an explicit no-op branch in `deletion._cleanup`: no
    `Herdr connections` or `sync session` step runs, and the journal goes to `cleaned`.
16. **Step 7 ends the operation whatever the owner answers.** The coordinator runs `delete --codespace OLD` in the
    terminal. Afterwards the journal is archived `completed`. A declined delete leaves OLD sealed and listed, and
    `task work` keeps offering it (stage C's slot for the oldest listed seal). An unfinished delete keeps its own
    journal and repair action.
17. **Cancel before the move, finish forward after it, decided from facts** (spec call 8, F3). The move has started
    when this operation's seal for OLD exists and any of these name NEW:
    - `scratch.json`;
    - `scratch-retarget.json` (its candidate);
    - `recovery-move.json` (its `new`);
    - `workspace-pending.json` (its `codespace`).

    `replacing.move_started(state, journal)` reads exactly these files, plus the journal's own latch (N2). The
    coordinator writes `"moving": <epoch>` into the journal the first time it observes a started move, before any
    cutover run replays it. Replaying the move consumes some of those files, so after that the latch alone keeps the
    operation finishing forward. The coordinator checks `move_started` before every cutover run and after every
    cutover exit, an error or interrupted return included. The latch write is atomic and must succeed before the
    cutover child starts; a failed write stops the run with its phase kept. A started move at a phase before
    `moved` goes to the cutover, which finishes it forward. Phases never go back: a journal at `moved` or later resumes its own phase (N9).
    Ctrl-C or EOF at any prompt stops the run with its phase kept. Abandon (`continue` or `abandon` in Gum) is
    offered only while the phase precedes `moved` and the move has not started. It removes this operation's seal,
    leaves OLD paired and NEW listed, and archives the journal `abandoned`.
18. **`task work` shows an unfinished replace first** (F16). The order is:
    1. `Recovery is off until you connect`, with its connect action;
    2. the unfinished replace (line one, and the action that reruns it);
    3. pending cleanup;
    4. the situation's own actions;
    5. `Replace NAME`, right after the situation's connect or start action, and only when the situation has one
       (no Replace in the setup, setup-PR or failed-creation situations);
    6. the reserved slot for the oldest listed seal;
    7. Details.

    Lower-ranked overlays (forget, checkout, generator update, extra PRs) give way when the budget is full, as today.
    The menu never shows the test branch override.
19. **Terminal output keeps the reused commands' behavior** (F11, not adopted as proposed). Spec D sets no rule that
    hides gh or Git failure text, and the existing setup prints gh's failure reason today (`codespaces.py:67`,
    `:437`). Translating every setup error would hide actionable messages such as a machine type the account may not
    use. Stage C's no-URL rule covers the delete verdict, and the delete runs unchanged here. What this plan adds is
    the storage rule in Global Constraints, tested with credential-shaped and URL-shaped gh failures that must not
    appear in any journal or receipt.
20. **Sync readiness allows NEW's refusals** (F7). `sync_cycle.summary` is not healthy for a cycle where NEW refused
    a conversation (`sync_cycle.py:105-113`), so the check does not use `healthy`. Sync is ready when all of these
    hold:
    - a cycle that is current (`workspace.cycle_scope`), started after the check began, and completed;
    - every cycle-level issue is a mirrored outbound publication refusal (N1). It carries `direction ==
      'desktop_to_codespace'`, an `agent` and a `session_id`, and apart from `direction` it equals an item of the
      outbound row's `publication_issues` (`replication.py:349-354`). Any other cycle issue (memory, capture,
      transfer, inbound) blocks;
    - the inbound row (`codespace_to_desktop`) `complete`, with its receipt bound and equal to the Mac's latest
      codespace receipt;
    - the outbound row's receipt bound to its snapshot (`receipt_matches`), with outcome `complete` or `degraded`.

    A degraded outbound row is then accounted entry by entry. A sealed entry that NEW refused is `kept`. A Mac-only
    entry that NEW refused is named on the Kept line and does not block.
21. **The cutover needs OLD's worker current and OLD still paired** (F4, F8).
    - Before `move.decide`, when `workspace._current_worker` is false for OLD's runtime, the cutover reconnects OLD
      (`move.finish_pending`, then `workspace.remote(project, state, armed=…, enter=False, expected_old=OLD,
      start=False)`), which starts or replaces the worker as an ordinary connect does. `workspace.remote` gains
      `start=True`. With `False`, `_choose_pairing` raises the stopped line instead of calling `start_codespace`
      (`workspace.py:271-274`), so OLD stopping between two reads is never started (N6).
    - `workspace.remote` gains `expected_old`. Right after it takes `workspace-control.lock` (`workspace.py:403`),
      before `_finish_pending_registry`, its prerequisites and the selection, it refuses when `scratch.json` no
      longer names that Codespace. The cutover passes OLD on the move, so a rebind
      made elsewhere during the prompt or the gather is never overwritten.
22. **Replace is offered only for a pairing it can inspect** (plan check 3 M2; narrows F10's "a pairing exists").
    The wizard lists it when PAIRED is among the listed Codespaces and its read succeeded. An unlisted or
    unreadable pairing keeps today's menu: spec D step 1 starts by reading OLD, and a deleted OLD ends the journal,
    so the option could only fail. The coordinator's inspection still refuses an unreadable OLD for `--replace`
    and `task work`.

## Copy (exact strings)

`OLD` and `NEW` are Codespace names, `HH:MM` local time, `SHA12` the first 12 hex digits, and `CMD` a command built by
`overview.task_command` (runtime) or shown as `task …` (setup), formatted with `shlex.join`. `REPLACE` is the
project's setup command plus `-- --replace` (`task_command(local, 'setup', '--', '--replace')`). `PHASE` is the human
word for a phase: `inspected` → `inspect`, `create-requested` / `created` → `create`, `ready` → `flush and move`,
`moved` → `sign in`, `signed-in` → `check`, `checked` / `offered` → `delete offer`.

Coordinator (`replacement.py`):
```
Replacing OLD (MACHINE, N cores, idle timeout M minutes).
Recovery moves with your consent from DATE (LOGIN).
Recovery on NEW will need a typed arm: consent was given as LOGIN, gh is signed in as LOGIN.
Creating the new Codespace from BRANCH at SHA12.
BRANCH moved to SHA12 since the check; checking its setup again.
Created NEW.
Found NEW (MACHINE, created at HH:MM): the only new Codespace on BRANCH with that machine since the create request.
Also new since the create request: NAME (MACHINE, created at HH:MM). This replace did not use it.
Replace stopped at PHASE: REASON. Run REPLACE to continue.
A replace of OLD is at PHASE.
The move to NEW has started; finishing it.
Replace abandoned. OLD stays paired; NEW stays listed. Check and delete it with CMD.
Replace abandoned. OLD stays paired; nothing was created.
NEW is ready. Next, OLD gets the checked delete.
Replaced OLD with NEW.
OLD is kept; task work offers its delete.
Connect with task work:cs.
```
- The second and third lines print only for an armed project. `DATE` is the consent's local date. `CMD` in the first
  abandon line is `work:cs:delete -- --codespace NEW`.
- Refusals (exit 1, nothing spent): `Replacing a Codespace needs a terminal on your Mac.` ·
  `Unset GH_TOKEN before replacing a Codespace; it must use a real gh account.` (and `GITHUB_TOKEN`) ·
  `Another replace for this project is running; wait for it to finish.` ·
  `No Codespace is paired, so there is nothing to replace. Create one with task setup.` ·
  `OLD's FIELD cannot be read from GitHub; nothing was created.` (FIELD: `machine`, `core count`, `idle timeout`,
  `billing owner`) · `OLD belongs to another repository; nothing was created.` ·
  `OLD's machine MACHINE (N cores) is not offered for BRANCH at SHA12; nothing was created.` ·
  `BRANCH at SHA12 does not carry this checkout's setup (REASON); nothing was created. Run task setup first.` (REASON:
  the first `render.validate` error, or `a different generator version`, or `different selections`) ·
  `This replace was started as LOGIN, but gh is signed in as LOGIN. Switch with gh auth switch, then run the replace
  again.`
- Lost reply: `GitHub did not confirm the new Codespace. Looking for it for up to 2 minutes.` ·
  `No new Codespace appeared. Type create to create one, or anything else to stop.` ·
  `New Codespaces since the create request: NAME (MACHINE, created at HH:MM), … Choose the one this replace keeps;
  the others stay as they are.` (an uncertain row shows `unknown` for what could not be read). Gum options: one
  `NAME (MACHINE, created at HH:MM)` per row, then `cancel`. Cancel with an uncertain row listed: `Replace stopped
  at create: NAME cannot be fully read from GitHub, and it may be the Codespace this replace asked for. Check it on
  GitHub, then run REPLACE again.`
- Sign-ins: `Not signed in yet on NEW: TOOLS. Retry with task work:signin:all.` · `Sign-in on NEW did not finish;
  retry with task work:signin:all.`
- NEW not ready: `NEW is not ready: fix the lines marked Not ready, then run REPLACE to check it again. OLD stays.`
- An ended journal found on a rerun: `The replace of OLD ended: NAME was deleted.`

Cutover (`cutover.py`):
```
On OLD: claude (2), codex (1) running. End them, then press Enter to check again, or type stop.
OLD's final flush is not complete; nothing moved:
  Scratch  …
Fix the lines above, then run the replace again.
Final flush of OLD recorded at HH:MM (N files, M conversations).
Moved sync, recovery and Herdr to NEW.
```
- Not armed: `Moved sync and Herdr to NEW.` · Herdr failed: `Moved sync to NEW; Herdr could not connect it. The
  replace checks it again.`
- `OLD is STATE. Start it with task work:cs:restart, then run the replace again.` · `Nothing moved.` (on `stop`, EOF
  or a refused consent) · `This replace record does not match; run the replace again.` · `OLD is no longer the
  paired Codespace; nothing moved.` (an `expected_old` refusal)
- The category lines under "not complete" are `verdict.lines` output for the three categories, unchanged.

Check (`replace_check.py`); a line is two spaces, the label padded to 10 columns, then the text:
```
Checked NEW at HH:MM:
  Setup     validated at SHA12
  Sync      a cycle completed at HH:MM
  Recovery  armed; its keeper is running
  Herdr     reachable
  History   N conversations from OLD are on NEW
Kept on this Mac only: codex SESSION_ID
```
- Recovery not armed: `not armed`. A failing line starts `Not ready: `: `Not ready: no sync cycle completed in 6
  minutes` · `Not ready: the last cycle on NEW did not complete` · `Not ready: recovery still points at OLD` ·
  `Not ready: its keeper is not running` · `Not ready: Herdr cannot reach NEW` · `Not ready: N conversations from
  OLD are not on NEW (agent SESSION_ID, …)` · `Not ready: N conversations from OLD are still being published` ·
  `Not ready: N conversations from OLD have a different version on this Mac (agent SESSION_ID, …)` ·
  `Not ready: the final flush of OLD cannot be read`.
- The Setup line is the coordinator's (it reads the launch receipt). The runtime check prints the other four.
- Lists keep the first 3 items, then `and N more`. JSON and receipts keep up to 100.

`task work` (`overview.py`): line one `Replacing OLD: stopped at PHASE. Finish it: REPLACE`, cut to 78 cells by
`overview.fit`. Its action label is `Finish replacing OLD`. The paired action label is `Replace NAME`.

## Review Focus

1. **A crash or Ctrl-C at every phase**, including between `codespace create` and its reply, and inside the move's
   own publication windows (after the binding moved, before consent or the worker). A rerun continues the phase it
   is in, never offers abandon once the move started, creates at most once, never rebuilds after a personalization
   failure and never deletes NEW (Task 5 `test_each_phase_resumes_on_rerun`,
   `test_lost_create_reply_adopts_exactly_one`, `test_a_started_move_is_never_abandoned`; Task 2
   `test_a_crash_inside_the_move_finishes_forward`).
2. **OLD changes around its final flush**: an agent restarted during the prompt, a scratch edit, `arming.json`
   rewritten during the wait, or a rebind from another terminal. Nothing moves on a stale flush or onto a changed
   pairing. After the move, OLD's delete check reads anything late as changed (Task 2
   `test_flush_requires_scratch_history_and_agents_safe`, `test_no_prompt_between_seal_and_retarget`,
   `test_a_rebind_during_the_prompt_is_never_overwritten`; Task 1 `test_sealed_old_changed_after_the_flush_is_unsafe`).
3. **Recovery across the move.** The same account carries consent with one line. A new account or billing owner asks
   before the flush. A decline moves nothing. After the move NEW's keeper runs, and deleting OLD leaves NEW's
   `arming.json` byte-identical (Task 2 `test_consent_is_decided_before_the_flush`, Task 3
   `test_armed_needs_new_keeper`, Task 1 `test_delete_of_sealed_old_after_a_replace_keeps_new_recovery`).
4. **Conversations that moved on.** A conversation continued after the flush, refused by NEW or forgotten is named,
   not counted missing. Same ids with different bytes anywhere in its files, including a Codex ancestor, are not on
   NEW (Task 3 `test_longer_revision_counts`, `test_a_changed_codex_ancestor_is_not_on_new`,
   `test_a_real_refusal_is_kept_and_does_not_fail`; the real relay chain in
   `test_replace_check_passes_after_a_real_relay`).
5. **Readers during a replace.** `task work` puts the unfinished replace first. A delete of either name ends the
   journal. A never-paired NEW is checkable, its files read UNSAFE, and its delete runs no cleanup step. No runtime
   path takes `replace.lock` (Task 1 `test_delete_never_takes_the_replace_lock`,
   `test_replacement_delete_cleans_nothing_and_rereads_its_journal`; Task 6 `test_unfinished_replace_comes_first`).

---

### Task 1: The journal reader, the `replacement` target, and seal-pinned GC (runtime)

**Files:**
- Create: `devcontainer_setup/project_sync/replacing.py`
- Modify: `devcontainer_setup/project_sync/checkpoint_store.py:287-323` (`collect`),
  `devcontainer_setup/project_sync/verdict.py:101-134` (`resolve`) and `:342-355` (`judge`),
  `devcontainer_setup/project_sync/deletion.py:24-27` (`STEPS`), `:52-117` (`gather`), `:168-238` (`_cleanup`),
  `:330-400` (already deleted, connect, `--start`)
- Test: `tests/test_replacing.py` (new), `tests/test_checkpoint_store.py`, `tests/test_verdict.py`,
  `tests/test_deletion.py`

**Interfaces:**
- Consumes: `state.atomic_write`, `state.read_json`. It reads `deletions/<name>.json`'s `phase` and the four move
  files with `read_json` itself, and imports neither `deletion` nor `verdict` (`deletion` imports `verdict`, which
  imports `replacing`).
- Produces, `replacing.py`:
  - `JOURNAL = 'replace.json'`, `ARCHIVE = 'replacements'`, `LOCK = 'replace.lock'`, `RECEIPTS = 'replace-receipts'`.
  - `PHASES = ('inspected', 'create-requested', 'created', 'ready', 'moved', 'signed-in', 'checked', 'offered')`;
    `ENDED = ('completed', 'abandoned', 'deleted')`.
  - Journal `{"schema": 1, "operation": 12 lowercase hex, "old": str, "new": str | null, "account": str,
    "repository": "OWNER/REPO", "branch": str, "commit": 40 hex, "remote_root": str (NEW's folder), "resources":
    {"machine": str, "cpus": int, "idle_timeout_minutes": int, "billable_owner": str}, "old_receipt": dict | null,
    "branch_receipt": dict | null, "request": {"at": float, "before": [str, …], "retries": [float, …]} | null, "phase": PHASE, "at":
    {PHASE: float}, "signins": {"retry": [str, …], "at": float} | null, "check": dict | null, "moving": float | null,
    "ended": null | ENDED, "ended_at": float | null}`.
  - `read(state) -> dict | None`: the active journal. None when absent, or when an archive with the same `operation`
    exists (a crash between `archive` writing and unlinking). Malformed → `ValueError('The replace record cannot
    be read')`.
  - `write(state, journal) -> None`: validates, then `atomic_write` (sorted keys). Only `replacement.py` calls it.
  - `archive(state, journal, ended, now) -> Path`: writes `STATE/replacements/<operation>.json` with `ended` and
    `ended_at`, then unlinks `replace.json`. Idempotent.
  - `ended_by_fact(state, journal) -> str | None`: the name (OLD or NEW) whose `deletions/<name>.json` phase is
    `deleted` or `cleaned`, else None.
  - `move_started(state, journal) -> bool`: call 17's rule. It is true for the journal's `moving` latch, or for a
    seal file `retired/OLD.json` whose `operation` is the journal's together with one of the four files naming NEW.
  - `naming_new(state, name) -> dict | None`: the newest journal, active or archived, whose `new == name`, unless
    `ended_by_fact` names `name`.
  - `receipt_path(state, operation, step) -> Path` (`STATE/replace-receipts/<operation>-<step>.json`, `step` in
    `cutover`, `check`). `read_receipt(path, operation, step) -> dict | None`: the decoded receipt when its
    `schema == 1`, `operation` and `step` match, else None.
- Produces, `verdict.py`: in `resolve`, after the seal and archive lookups and before `unpaired`,
  `naming_new(state, name)` gives `Target(name, 'replacement', journal['remote_root'], None, None)` (pairing → seal →
  archive → journal → unpaired, CHECK 4h). In `judge`, for `replacement`: Git and Agents as for `moved`. Scratch is
  SAFE `no files in .scratch` when the scan holds no scratch file, else UNSAFE `N files exist only on NAME (a, b, c)`.
  History is SAFE `no conversations on NAME` when it holds no session file, else UNSAFE `N conversation files exist
  only on NAME`. An incomplete scan part reads UNKNOWN, as for every kind.
- Produces, `deletion.py`:
  - `STEPS['replacement'] = ()`.
  - The delete's owner order (CPLAN Task 8 R1) gains the replace journal's `account` for the NEW `naming_new`
    gives, after the delete journal and before `accounts.json`, so a never-paired NEW is never owner-unknown.
  - `_cleanup` returns at once for `replacement`, journaling `cleaned`, with no primitive called.
  - `gather` treats `replacement` like `moved` (one scan, no cutoffs, `steady=True`).
  - In `run`, the connection for a `replacement` target gets `accept_new = True` before it is entered, and `--start`
    accepts `replacement` like `sealed` and `moved`.
  - A `replacement` target that GitHub confirms deleted, with nothing kept, writes `deletions/NAME.json` at
    `cleaned` before printing `NAME is already deleted.`
- Produces, `checkpoint_store.py`: `collect` adds every `STATE/retired/*.json` `snapshot_id` to `named` before
  computing `kept`, reading the files directly (not `seal.names`, which skips unreadable ones). A seal file that
  cannot be read, or whose `snapshot_id` is not 64 hex, raises `ValueError` before anything is deleted. The caller
  already catches that and retries in an hour (`replication.py:237-265`). `_record_received` also retires shorter
  proven-prefix records on an identical retry, not only on a new receive (`checkpoint_store.py:217-239`). A crash
  between writing the new record and unlinking the old one then heals on the next receipt (origin fix for N5). On an
  identical retry it first looks in the relay records it already listed for another record with the same
  `(agent, session_id)` and reads heads only when one exists, so `tests/test_checkpoint_store.py:246`
  (`test_identical_record_retry_does_not_read_heads_again`) holds. Both the retry and the new-receive path decide
  "contains" and "proven prefix" by Task 3's full-closure digest rule over every file, not by the head alone
  (`checkpoint_store.py:224-231` today). The suite's shared `conversation()` fixture
  (`tests/test_checkpoint_store.py:32-37`) then emits complete, truthful per-file metadata (`path`, `size`,
  `sha256`, as capture writes them, `conversations.py:479`), so `test_appended_revision_retires_the_earlier_record`
  (`:70`) and `test_older_revision_arriving_later_is_not_recorded` (`:77`) keep their one-record assertions (plan
  check 4 Q2). A record whose closure cannot be proved is kept, never retired. Retirement on either path also needs both
  closures readable and verified (every blob of the old and the new entry read and hashed): an older record whose
  head blob cannot be read is kept with its unreadable-relay issue, as
  `tests/test_checkpoint_store.py:181` (`test_unreadable_head_cannot_prove_retirement`) requires (plan check 5 V2).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_replacing.py
def test_round_trip_and_malformed(self)             # write/read equal; a bad phase, schema or commit -> ValueError
def test_archive_is_idempotent_and_hides_the_active_copy(self)   # crash after archive write: read() -> None
def test_ended_by_fact_follows_either_deletion_journal(self)     # deletions/NEW.json 'deleted' -> 'NEW'; 'confirmed' -> None
def test_move_started_needs_this_operations_seal_and_a_file_naming_new(self)  # each of the four files alone -> True;
                                                    # a seal from another operation, or no seal -> False;
                                                    # the moving latch alone -> True
def test_naming_new_survives_an_abandon_until_new_is_deleted(self)
def test_receipt_must_match_operation_and_step(self)
# tests/test_checkpoint_store.py
def test_collect_keeps_what_a_seal_names(self)      # manifest older than keep_days and its blobs survive apply=True
def test_collect_deletes_nothing_when_a_seal_cannot_be_read(self)
def test_a_changed_ancestor_is_never_retired(self)  # equal heads, a different Codex ancestor -> both records kept
def test_an_identical_retry_retires_a_crash_left_prefix(self)  # new record written, old not unlinked; the same
                                                    # manifest acknowledged again -> one record left
# tests/test_verdict.py
def test_journal_named_new_is_a_replacement_target(self)
def test_a_paired_sealed_or_archived_name_keeps_its_kind(self)   # precedence over the journal
def test_unnamed_new_stays_unpaired(self)           # no journal, or the journal names another NEW
def test_replacement_with_files_is_unsafe(self)     # one .scratch payload file; one claude session file -> UNSAFE each
def test_empty_replacement_is_safe_for_scratch_and_history(self)  # .scratch/.gitignore and README.md only
def test_sealed_old_changed_after_the_flush_is_unsafe(self)   # pins the existing sealed rule for D's Review Focus 2
# tests/test_deletion.py
def test_replacement_target_accepts_its_first_host_key(self)  # connect() result has accept_new True when entered
def test_replacement_delete_cleans_nothing_and_rereads_its_journal(self)  # end_target_proxies and
                                                    # terminate_retired never called; cleaned; a rerun reads the
                                                    # journal (no ValueError) and prints 'was deleted at'
def test_already_deleted_replacement_records_its_deletion(self)  # confirm_deleted True, no journal -> cleaned
                                                    # journal written; ended_by_fact names NEW
def test_delete_never_takes_the_replace_lock(self)  # replace.lock held (flock) by the test: --check and a typed
                                                    # delete both finish; no call blocks
def test_delete_of_sealed_old_after_a_replace_keeps_new_recovery(self)  # stage C's preservation test, with the
                                                    # seal written by cutover's path and a journal naming both
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_replacing tests.test_checkpoint_store
  tests.test_verdict tests.test_deletion -v`. Expected: exit 1 (`ModuleNotFoundError` for `replacing`; the rest fail
  on the missing kind and retention).
- [ ] **Step 3: Implement** `replacing.py` and the three edits as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit**: `git add -A devcontainer_setup/project_sync tests && git commit -m "Read the replace
  journal, check a NEW it names, and keep sealed captures"`

### Task 2: The cutover: seal OLD and move without the TUI (runtime)

**Files:**
- Create: `devcontainer_setup/project_sync/cutover.py`
- Modify: `devcontainer_setup/project_sync/herdr.py:215-311` (`open_remote(..., enter=True)`),
  `devcontainer_setup/project_sync/workspace.py:393-501` (`remote(..., enter=True, expected_old=None)`),
  `devcontainer_setup/project_sync/__main__.py:25-66,302-330` (the `replace` subcommand: `cutover` and `check`)
- Test: `tests/test_cutover.py` (new), `tests/test_herdr.py`, `tests/test_workspace_recovery.py`,
  `tests/test_scratch_sync.py`, `tests/test_project_sync_cli.py`

**Interfaces:**
- Consumes: Task 1 (`replacing.read`, `replacing.move_started`), `move.decide`, `move.finish_pending`,
  `deletion.gather`, `verdict.resolve`, `verdict.judge`, `verdict.lines`, `target_scan.scan`, `seal.write`,
  `seal.read`, `workspace.remote`, `workspace._current_worker`, `scratch.status`, `lifecycle.LifecycleAPI.read`,
  `arming.account`, `arming.read`, `transport.CodespaceConnection`.
- Produces, `herdr.py`: `enter: bool = True` as call 12 states. The `prepared` callback still runs before
  `_workspace`.
- Produces, `workspace.py`: `enter` passed through unchanged. `expected_old: str | None = None`: when given,
  `scratch.json` read right after `workspace-control.lock` is taken (before `_finish_pending_registry`, the
  prerequisites and the selection) must name it, else `ValueError('OLD is no longer the paired Codespace; nothing
  moved.')`. `start: bool = True`: with `False`, the selection never POSTs start and raises the stopped line for a
  Codespace that is not `Available`. It takes no lock it does not take today.
- Produces, `__main__.py`: subcommand `replace` with `cutover --operation OP --old OLD --new NEW --receipt PATH` and
  `check --operation OP --receipt PATH` (all required; argparse errors exit 2). Both dispatch before the terminal
  gate, like `delete`. `cutover` calls `cutover.run(..., recovery=lambda primary: _resume_recovery(primary,
  state))`. `check` calls `replace_check.run(...)` (Task 3; imported inside the handler).
- Produces, `cutover.py`: `run(project, state, *, operation, old, new, receipt, recovery, ask=input, tty=None,
  api_factory=lifecycle.LifecycleAPI, connect=transport.CodespaceConnection, now=time.time, sleep=time.sleep) -> int`.
  The receipt: `{"schema": 1, "operation", "step": "cutover", "old", "new", "moved": bool (scratch.json names NEW),
  "sealed_at": float | null, "cycle_id": str | null, "snapshot_id": str | null, "herdr": int | null, "at": float}`,
  written with `atomic_write` on every exit after step 1. Exit 0 only when `moved`.
- `armed` below means `recovery` when `arming.read(state)` is armed, else None.
- The order `run` follows (Copy section messages):
  1. Refuse inside a Codespace, without a TTY, and with an env token. `journal = replacing.read(state)` must be active
     with this `operation`, `old` and `new`, at phase `ready` or later. Otherwise print the "does not match" line and
     exit 1.
  2. **Finish forward** when `scratch.json` names NEW, or `replacing.move_started` holds. Run `move.finish_pending`.
     When the binding still names OLD, run `decision = move.decide(project, state, new, ask=ask)` and
     `workspace.remote(project, state, rebind=new, move=decision, armed=armed, enter=False, expected_old=old)`. This
     replays a stopped retarget the way an ordinary rerun of the rebind does. When it already names NEW, run
     `workspace.remote(project, state, armed=armed, enter=False)`, a reconnect that restarts a keeper or worker that
     failed. Write a receipt with `moved` and the existing seal's fields, exit 0 when moved. No scan, gather or seal.
  3. `scratch.json` must name OLD. `api_factory(old, gh_user=account).read()` must be `ok` and `Available`, else print
     the STATE line and exit 1 (no POST of any kind).
  4. When OLD's worker is not current (call 21): `move.finish_pending`, then `workspace.remote(project, state,
     armed=armed, enter=False, expected_old=old, start=False)`. Then poll `scratch.status` every 2 s, up to 2 minutes, for a
     current worker. If none appears, print `Replace stopped at flush and move: sync with OLD did not start.` and
     exit 1.
  5. `decision = move.decide(project, state, new, ask=ask)` (None when not armed). A `ValueError` prints its message
     and `Nothing moved.`, then exits 1.
  6. Inside one `connect(old, state=state)`: scan with no cutoffs. While it shows a blocking agent, print the agents
     line and `ask('> ')`. EOF or `stop` → `Nothing moved.`, exit 1. Unreadable processes → the stage C UNKNOWN
     text, exit 1.
  7. `evidence = deletion.gather(project, state, target, connection, now=now, sleep=sleep)` and `verdict.judge`.
     Unless `steady` holds and Scratch, History and Agents are SAFE: the "not complete" lines, exit 1, no seal.
  8. `seal.write(state, Store(state), evidence.scan, evidence=evidence, identity=project.identity,
     repository=journal['repository'], gh_user=journal['account'], remote_root=target.remote_root,
     operation=operation, cycle_id=evidence.cycle['cycle_id'], snapshot_id=evidence.receipt['snapshot_id'], now=now())`,
     then at once `workspace.remote(project, state, rebind=new, move=decision, armed=armed if decision is not None
     else None, enter=False, expected_old=old)`. Print the moved line that fits. A `ValueError` from `remote` prints
     its message, and the receipt's `moved` is read from `scratch.json` (`herdr: 1` when the binding names NEW).

- [ ] **Step 1: Write the failing tests** (`tests/test_cutover.py`: the `FakeAPI` and scripted-scan pattern of
  `tests/test_deletion.py`; `workspace.remote`, `move.decide`, `move.finish_pending` and `seal.write` wrapped to record
  call order and arguments; `ask` records each call):

```python
def test_consent_is_decided_before_the_flush(self)        # order: decide, scan, gather, seal.write, remote
def test_decline_moves_nothing_and_writes_no_seal(self)    # decide raises -> no gather, seal or remote; exit 1
def test_agents_are_asked_never_killed(self)               # claude running -> ask; Enter -> rescan clear -> proceeds;
                                                           # 'stop' -> exit 1; the connection ran only scan programs
def test_flush_requires_scratch_history_and_agents_safe(self)  # each UNSAFE/UNKNOWN or not steady -> no seal, no remote
def test_no_prompt_between_seal_and_retarget(self)         # no ask() after seal.write in any path
def test_changed_arming_after_decide_moves_nothing(self)   # arming.json rewritten during the agents prompt ->
                                                           # remote refuses (check_arming); OLD still paired; exit 1
def test_a_rebind_during_the_prompt_is_never_overwritten(self)  # unarmed and armed: scratch.json names another
                                                           # Codespace when remote runs -> expected_old refusal,
                                                           # no retarget, exit 1
def test_a_stale_or_missing_old_worker_is_reconnected_first(self)  # absent worker; worker from a prior runtime id ->
                                                           # one reconnect with expected_old=OLD, start=False, then gather
def test_old_stopping_between_reads_is_not_started(self)   # Available at the first read, Shutdown at selection ->
                                                           # 0 start POSTs, the restart line, exit 1
def test_the_latch_alone_keeps_finishing_forward(self)     # journal moving set, none of the four files -> the
                                                           # finish-forward branch, no gather, no seal
def test_stopped_old_is_not_started(self)                  # Shutdown -> post_start 0, the restart line, exit 1
def test_a_crash_inside_the_move_finishes_forward(self)    # journal 'ready' with this seal and each of: binding NEW;
                                                           # retarget intent naming NEW; recovery-move.json; pending
                                                           # registry -> finish_pending, no gather, no new seal,
                                                           # remote with armed callback; moved true
def test_receipt_names_the_seal_and_herdr_result(self)     # Herdr nonzero or ValueError after the move -> exit 0,
                                                           # receipt herdr != 0, moved true
def test_mismatched_journal_refuses(self)                  # other operation, OLD, NEW, or phase before ready
# tests/test_herdr.py
def test_enter_false_never_runs_the_herdr_client(self)     # fake herdr on PATH logs argv: no bare `herdr` call;
                                                           # prepared still runs once
def test_enter_false_keeps_machine_setup_codes_and_workspace_errors(self)
# tests/test_workspace_recovery.py
def test_expected_old_refuses_before_any_effect(self)      # with a pending registry and a stopped selected
                                                           # destination: no repair, start, retarget or consent
                                                           # effect; binding, registry and arming bytes unchanged
# tests/test_project_sync_cli.py
def test_replace_subcommand_options(self)                  # each missing option -> exit 2; dispatch is headless
```

  Also extend `tests/test_scratch_sync.py:285`'s strict `open_herdr` keyword set (`:354`) with `enter`. Its missing
  `expected_codespace` is a known baseline mismatch; do not fix it in this task.
- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_cutover tests.test_herdr
  tests.test_workspace_recovery tests.test_project_sync_cli -v`. Expected: exit 1.
- [ ] **Step 3: Implement** `cutover.py`, `enter`, `expected_old`, and the subcommand as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit**: `git add -A devcontainer_setup/project_sync tests && git commit -m "Seal the old Codespace
  and move to the new one without opening Herdr"`

### Task 3: Check NEW, conversations by content (runtime)

**Files:**
- Create: `devcontainer_setup/project_sync/replace_check.py`
- Test: `tests/test_replace_check.py` (new), `tests/test_replication.py`

**Interfaces:**
- Consumes: Task 1 (`replacing.read`), `seal.read`, `checkpoint_store.Store` (`load`, `read`, `latest`),
  `forget.forgotten`, `workspace.registry`, `workspace.cycle_scope`, `workspace._conversation_receipt`,
  `scratch.status`, `scratch.inspect_sync`, `sync_cycle.receipt_matches`, `arming.read`, `keeper.status`,
  `restore.Herdr(machine_id).reachable()`.
- Produces: `run(project, state, *, operation, receipt, now=time.time, sleep=time.sleep, wait=360) -> int`. Read-only
  apart from its receipt. Exit 0 only when `ok`. Receipt: `{"schema": 1, "operation", "step": "check", "codespace":
  NEW, "checked_at": float, "sync": "ok" | TEXT, "cycle_completed_at": float | null, "recovery": "ok" | "not armed" |
  TEXT, "herdr": "ok" | TEXT, "conversations": {"on_new": [[agent, id], …], "kept": […], "forgotten": […],
  "missing": […], "pending": […], "diverged": […], "kept_mac_only": […]}, "ok": bool}`. `TEXT` is the Copy text after
  `Not ready: `.
- The rules:
  - The journal must be active with this operation at phase `moved` or later, and `scratch.json` must name NEW.
  - **Sync** (call 20): `t0 = now()`. Every 2 s, up to `wait`, read the registry, `scratch.status` and
    `scratch.inspect_sync` into `cycle_scope` (as `deletion.gather` does, `deletion.py:62-104`). Look for a `current`
    cycle with `started_at > t0` and `completed_at` set, then apply call 20's conditions.
  - **Recovery**: not armed → `not armed`. Armed → `arming['codespace'] == NEW` and `keeper.status(state)` running
    for NEW with the current runtime id.
  - **Herdr**: `herdr-machine.json` names NEW, and `reachable()` is true.
  - **Conversations**: the seal for OLD must have `operation` equal to this one, and its snapshot must `load`. Every
    entry E `(agent, session_id)` in that manifest's `conversations.entries` is classified:
    - `forgotten` when `forget.forgotten(state)` holds it.
    - **Carried by an outbound entry O** of the ready cycle's `desktop_to_codespace` snapshot (loaded from the Mac
      store) when O has the same `(agent, session_id)`, and for **every** file F in E's `files` (the head and every
      ancestor) O has a file with the same `session_id` and `path`, a size of at least F's, and a verified blob whose
      first F.size bytes hash to F's `sha256`. This is stage C's prefix-digest custody rule, so a longer revision
      counts and different bytes anywhere in the closure do not (F2).
    - Let OS be the outbound entries with E's `(agent, session_id)`. An entry of OS is **obsolete** when it is a
      proven prefix of a carrying entry: each of its files is a prefix, by the same digest rule, of that entry's file
      with the same `session_id` and `path`. That is a relay record a crash left behind (`checkpoint_store.py:234-239`).
      - `missing` when no entry of OS carries E;
      - `diverged` when an entry of OS neither carries E nor is obsolete (N5);
      - otherwise E is `on_new` when the row's publication lists that key under `imported` or `unchanged` (by
        `source_session_id`) and no publication issue names it, `kept` when an issue names it, and `pending` when
        neither. Every entry of OS that is not obsolete carries E, so whatever NEW published under that key holds E's
        content.
    - Outbound publication issues naming a key that is in no sealed entry go to `kept_mac_only` (shown on the Kept
      line). They do not block.
  - `ok` = sync `ok`, recovery `ok` or `not armed`, Herdr `ok`, and no `missing`, `pending` or `diverged`.

- [ ] **Step 1: Write the failing tests** (`tests/test_replace_check.py`: a real `Store` in a temporary STATE, a seal
  written through `seal.write`'s path, cycle fixtures in `scratch-runtime.json` as `tests/test_replication.py:509`
  builds them):

```python
def test_old_conversations_on_new_by_content(self)        # imported -> on_new; ok; exit 0
def test_longer_revision_counts(self)                     # outbound head extends the sealed head -> on_new
def test_equal_count_different_content_is_missing(self)   # same agent and id, different head bytes -> missing; exit 1
def test_a_changed_codex_ancestor_is_not_on_new(self)     # equal heads, same-length different ancestor bytes ->
                                                          # missing (the plan check's probe case)
def test_an_obsolete_prefix_does_not_block(self)          # a crash-left shorter relay record beside the carrying
                                                          # one (the real receive interruption) -> on_new
def test_incompatible_content_is_diverged(self)           # a same-key entry that is neither carrying nor a prefix
def test_a_real_refusal_is_kept_and_does_not_fail(self)   # driven through replication.sync_project_data with a real
                                                          # native refusal, so the mirrored cycle issue is present
                                                          # (plan check 2's refusal-cycle probe) -> kept; sync ok
def test_a_refused_carrying_entry_is_kept_even_if_a_prefix_published(self)  # through publish_conversations
                                                          # (conversations.py:1131-1147): the obsolete prefix
                                                          # imported, the carrying entry an issue -> kept
def test_any_other_cycle_issue_blocks_sync(self)           # memory (rpc.py:140-142's remote issue included),
                                                          # capture, transfer and inbound issues
def test_a_mac_only_refusal_does_not_block(self)
def test_forgotten_is_named_and_does_not_fail(self)
def test_carried_but_unpublished_is_pending(self)
def test_an_unbound_receipt_fails_sync(self)
def test_cycle_must_start_after_the_check(self)           # an older complete cycle never counts; wait expires ->
                                                          # 'no sync cycle completed in 6 minutes'
def test_armed_needs_new_keeper(self)                     # arming names OLD, or the keeper serves OLD -> not ok
def test_herdr_must_be_reachable(self)                    # receipt names OLD, or status 'unreachable' -> not ok
def test_a_seal_from_another_operation_is_not_read(self)
def test_check_writes_nothing_but_its_receipt(self)       # every STATE byte equal before and after, except receipt
# tests/test_replication.py
def test_replace_check_passes_after_a_real_relay(self)    # the chain of :164 and :245 with one Claude and one Codex
                                                          # conversation with an ancestor: OLD captured, sealed from
                                                          # its inbound manifest, relayed by the Mac, imported on NEW;
                                                          # then replace_check's rule -> all on_new; a second NEW
                                                          # that refuses one -> kept, still ok
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_replace_check tests.test_replication -v`.
  Expected: exit 1 (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** `replace_check.py` as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit**: `git add -A devcontainer_setup/project_sync tests && git commit -m "Check the new Codespace,
  including every conversation from the old one by content"`

### Task 4: Inspection and creation primitives (setup)

**Files:**
- Create: `devcontainer_setup/replace_inputs.py`
- Modify: `devcontainer_setup/codespaces.py:322-444` (`launch`: the empty-reply fix and `resume_hint`),
  `devcontainer_setup/__main__.py:977-1056` (lift `launch_options` and `record_created` to module level; behavior
  unchanged)
- Test: `tests/test_replace_inputs.py` (new), `tests/test_codespaces.py`, `tests/test_wizard.py`

**Interfaces:**
- Consumes: `Codespaces.get`, `Codespaces.machines`, `git_ops.git`, `git_ops._fetch_default`, `render.validate`,
  `project_sync.state.ensure_setup_worktree_root`, `project_sync.state.discover_project`.
- Produces, `replace_inputs.py`:
  - `@dataclass(frozen=True) class Resources: machine: str; cpus: int; idle_timeout_minutes: int; billable_owner: str`.
  - `read_resources(data: dict, repo: str) -> Resources`: raises `ValueError` with the Copy text naming the first
    unreadable FIELD, or the other-repository refusal. `bool` is not an int here.
  - `offered(machines: list[dict], resources: Resources) -> bool`: an entry with the same `name` and `cpus`.
  - `pin(project: Path, branch: str) -> str`: fetch `branch` the way `_fetch_default` does, then the 40-hex
    `refs/remotes/origin/<branch>`. A failure raises `ValueError` (`could not read BRANCH from GitHub`).
  - `setup_differs(project: Path, sha: str) -> str | None`: call 5's rule. The temporary detached worktree lives under
    `ensure_setup_worktree_root(discover_project(project))` and is removed in a `finally`. It returns None when the
    setup is carried, else the REASON text.
- Produces, setup `__main__.py`, both used by `_launch_on_default` with unchanged behavior:
  - `launch_options(project: Path, selected: dict, preferences: dict) -> dict`, with keys `dotfiles_repo`,
    `machine_profile`, `workspace`, `state_root` and `open_after_create`, computed as `__main__.py:1029-1056` does
    today;
  - `record_created(project: Path, name: str, before: float) -> None`, the nested `_record_created`.
- Produces, `codespaces.py`:
  - An empty create reply raises the existing `ValueError('Creation did not return a codespace name…')`, not
    `IndexError` (origin fix: `splitlines()[-1]` on empty output, `codespaces.py:370`).
  - `launch(..., resume_hint: str | None = None, before_create: Callable[[], None] | None = None)`. A given
    `resume_hint` replaces the `Resume with: task devcontainer:setup -- --resume NAME` sentence in the raised
    message. `before_create` runs immediately before the `codespace create` command, after every preflight. An
    exception before it therefore means no POST was sent.
  - `existing(repo, branch=None, *, complete=False)`: with `complete=True`, every page's `total_count` must be
    present, agree across pages and equal the number of raw rows on all pages, counted before the repository and
    branch filter (`codespaces.py:131-134`), else `ValueError('the Codespace list from GitHub is incomplete')`.
  - `find_receipt(state_root: Path, repo: str, codespace: str) -> Path | None`: only the source search
    `migrate_receipt` makes (`codespaces.py:457-468`: regular non-symlink files, matching repository and codespace,
    each at its own branch's hashed path, the newest by mtime). Not its destination short-circuit (`:453-455`) or
    write (`:469-470`): a receipt at the default branch's path must not hide OLD's.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_replace_inputs.py
def test_premium_120_minutes_is_read_from_the_live_body(self)  # 8 cpus, 120 -> Resources(…, 8, 120, owner)
def test_each_missing_field_is_named(self)          # machine, cpus (absent, 0, '8', True), idle (absent, None, 0),
                                                    # billing owner -> the FIELD text
def test_another_repository_is_refused(self)
def test_offered_needs_the_same_cores(self)
def test_pin_reads_the_fetched_tip(self)            # a local bare origin advanced after clone -> its new tip
def test_setup_differs_uses_the_renderers_rules(self)  # a rendered fixture repository: unchanged tip -> None; a
                                                    # removed managed command or tampered managed version -> the
                                                    # validate error; another sourceRef -> 'a different generator
                                                    # version'; an unrelated user key or a native lock
                                                    # normalization (tests/test_render.py:794's case) -> None;
                                                    # the temporary worktree is gone afterwards
# tests/test_codespaces.py
def test_empty_create_reply_is_a_value_error(self)  # FakeGitHub create prints nothing -> ValueError, no receipt
def test_resume_hint_replaces_the_setup_resume_sentence(self)
def test_before_create_runs_after_preflight_and_before_the_post(self)  # a preflight failure -> not called
def test_complete_inventory_checks_total_count(self)   # two pages, two repositories: total 2, one project row,
                                                    # passes; a missing or unreadable total_count raises
def test_find_receipt_searches_other_branches(self)  # a receipt at the default branch's path does not hide OLD's
# tests/test_wizard.py
def test_launch_options_hold_the_ordinary_values(self)    # a fixture project's dotfiles repository, receipt
                                                          # namespace, workspace, machine profile and configured
                                                          # editor, asserted directly; _launch_on_default passes them
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_replace_inputs tests.test_codespaces
  tests.test_wizard -v`. Expected: exit 1.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit**: `git add -A devcontainer_setup tests && git commit -m "Read a Codespace's resources, pin the
  default tip and check it carries the setup"`

### Task 5: The coordinator (setup)

**Files:**
- Create: `devcontainer_setup/replacement.py`
- Test: `tests/test_replacement.py` (new), `tests/test_continuity_integration.py`

**Interfaces:**
- Consumes: Task 1 (`replacing.*`, the journal and receipt formats), Task 4 (`replace_inputs.*`, `launch(...,
  resume_hint=…)`, `launch_options`, `record_created`), Task 2's and Task 3's receipt formats (as files; never
  imported), `Codespaces` (`get`, `existing`, `machines`, `user`, `launch`), `codespaces.receipt_path`,
  `git_ops.default_branch`, `project_sync.state` (`discover_project`, `state_dir`, `locked`, `atomic_write`,
  `read_json`), `project_sync.evidence.record_start`, `project_sync.arming.read`, `project_sync.seal.read`.
- Produces: `run(project: Path, *, api=None, ui=None, ask=input, env=os.environ, tty=None, runner=subprocess.run,
  now=time.time, sleep=time.sleep) -> int`. `runner` runs each runtime step as `['bash', RUNTIME, '--project',
  str(project), …]` with `cwd=project` and the terminal's stdin, stdout and stderr (F15). `RUNTIME` is
  `PROJECT/.devcontainer/scripts/project-sync.sh` when it exists, else `PROJECT/scripts/project-sync.sh` (this
  repository).
- The order `run` follows (Copy section messages; every phase change is a `replacing.write` before the next effect):
  1. Refuse as in Global Constraints. Take `STATE/replace.lock` non-blocking for the whole run.
  2. `journal = replacing.read(state)`:
     - When `ended_by_fact` names a Codespace, archive the journal `deleted`, print its line and exit 0.
     - An active journal prints `A replace of OLD is at PHASE.`
     - When `replacing.move_started` holds and the phase precedes `moved`, journal the latch (`moving`), print `The
       move to NEW has started; finishing it.` and go to step 7. A phase at `moved` or later resumes its own phase;
       `offered` archives `completed` and exits 0 (N9). Otherwise, before `moved`, `ui.choose` offers `continue` and
       `abandon`. Abandon removes the
       seal for OLD when its `operation` is this one, archives the journal `abandoned`, prints the abandon line that
       fits and exits 0.
     - Continue goes to the journal's phase. `inspected` inspects again: nothing was spent.
  3. New operation (`inspected`):
     - `scratch.json` names OLD.
     - `data = api.get(OLD)`, `read_resources(data, repo)`.
     - `branch = env.get('SETUP_REPLACE_TEST_BRANCH') or git_ops.default_branch(project)`, `commit = pin(project,
       branch)`, then `setup_differs`, `offered(api.machines(repo, commit), resources)` and `account = api.user()`.
     - The recovery lines: when armed, carry is predicted when `arming.gh_user == account`.
     - The receipts as call 11 states.
     - The NEW folder is `launch_options(...)['workspace']` or `/workspaces/<repo name>` (as `launch` resolves it).
     - Write the journal with a fresh `operation` (`secrets.token_hex(6)`), then print `Replacing OLD (…).`
  4. Create:
     - `pin` again. A moved tip re-runs `setup_differs` and `offered` and journals the new commit (call 6).
     - `before = names of api.existing(repo, complete=True)`.
     - Then `api.launch(repo, branch, commit, machine, f'{idle}m', **dict(launch_options(...),
       open_after_create='none'), receipt=…, before_create=…, on_created=…, resume_hint=REPLACE)`, with every option
       passed once by keyword (N3). `before_create()` writes `request = {"at": now(), "before": before, "retries": []}`
       at `create-requested` (a reader treats an absent `retries` as `[]`). `on_created(name, before)` first journals `new = name` at `created`, then calls
       `record_created`.
     - An exception while the phase is still `inspected` (no POST was sent) stops with its message, exit 1.
     - Any exception while `new` is still null → reconciliation (call 10).
     - On a second submission (call 10) the phase is already `create-requested`: `before_create` appends `now()` to
       `request.retries` and keeps `at` and `before`. An exception before `before_create` ran on that submission (a
       preflight failure; no POST was sent) stops with its message, exit 1, before the null-NEW branch above, and
       keeps the first request for the next run's reconciliation.
     - An exception after `new` is set → `Replace stopped at create: REASON. Run REPLACE to continue.`, exit 1.
     - A return → `ready`.
  5. Rerun at `create-requested` with `new` null: reconciliation first (call 10). Never step 4's POST. At `created`
     run `api.launch(..., resume=NEW, …)` with the journal's commit, machine and idle timeout → `ready`.
  6. `ready`: run `replace cutover --operation OP --old OLD --new NEW --receipt <receipt_path(…, 'cutover')>`. A
     receipt with `moved` → `moved`. Without a usable receipt, `move_started` plus `scratch.json` naming NEW also
     → `moved`. After every cutover exit, a true `move_started` journals the latch. Otherwise stop at its phase,
     exit 1.
  7. A started move (step 2) runs the cutover as in step 6 first.
  8. `moved`: `t = now()`, run `work --signin-all`. Then the retry list from `signins.json` (call 13) → `signed-in`.
     Print the retry line when the list is not empty.
  9. `signed-in`: when the journal's last check failed, run the cutover first (it finishes forward, which reconnects).
     Then run `replace check --operation OP --receipt <receipt_path(…, 'check')>`. Print the Setup line from the
     launch receipt (`codespace == NEW`, `validated_commit == commit`, `rebuild_pending is False`). Both pass →
     journal the receipt at `checked`. Otherwise journal it, print the "not ready" line, exit 1.
  10. `checked`: print `NEW is ready. Next, …`, run `delete --codespace OLD`, then `offered` and archive `completed`.
      Print `Replaced OLD with NEW.`, plus `OLD is kept; …` unless `deletions/OLD.json` is `cleaned`, then
      `Connect with task work:cs.` Exit 0.

- [ ] **Step 1: Write the failing tests** (`tests/test_replacement.py`. `api` is a fake with `get`, `existing`,
  `machines`, `user` and a `launch` that delegates to the real `Codespaces.launch` over `FakeGitHub`
  (`tests/test_codespaces.py:22`), so create, rebuild and validation calls are counted. `runner` is a fake that
  records argv and cwd and writes the receipt a scripted step returns. `ui` and `ask` are scripted. STATE is a real
  temporary project state):

```python
def test_premium_120_minutes_beats_a_stale_four_core_receipt(self)  # receipt 4 cores/30m, live 8/120 -> create
                                                                    # argv --machine premium --idle-timeout 120m
def test_unreadable_resources_create_nothing(self)                  # each FIELD -> its line, 0 creates
def test_machine_not_offered_or_setup_missing_creates_nothing(self)
def test_a_tip_that_moves_before_create_is_checked_again(self)      # second pin differs: setup_differs reruns and
                                                                    # the journal holds the new commit before create
def test_journal_holds_the_request_before_create(self)  # FakeGitHub's create reads replace.json from disk:
                                                        # phase create-requested, request.before set
def test_lost_create_reply_adopts_exactly_one(self)     # reply lost, empty, a raise after the POST, and a run killed
                                                        # after the POST (journal left at create-requested): the next
                                                        # run reconciles first; 1 create total; start evidence
                                                        # 'created' at request.at
def test_adoption_ignores_older_or_other_branch_codespaces(self)  # a name missing from before but created before
                                                        # at - 120, on another branch, or on another machine
def test_a_competing_creation_is_a_choice_not_an_adoption(self)  # M1: this request's A (callback failed) and
                                                        # another setup's B on the same branch and machine, both
                                                        # outside before; the branch receipt names B -> the owner
                                                        # chooses; 0 creates
def test_an_uncertain_row_is_never_none(self)           # a new row without ref, machine or created_at, still
                                                        # incomplete after the re-read -> shown in the choice with
                                                        # cancel only; 0 second creates; cancel prints the
                                                        # uncertain stop line; once it reads whole and disqualifies,
                                                        # the next run reaches the recheck
def test_a_known_nonmatch_with_a_missing_field_is_excluded(self)  # another ref, machine or an older created_at with
                                                        # another field missing -> excluded on every rerun; zero
                                                        # candidates reach the recheck and the typed create
def test_a_late_first_codespace_is_named_after_a_second_create(self)  # first reply lost, 2 minutes of nothing,
                                                        # typed create -> 2nd create; the first's Codespace appears
                                                        # -> the Also new line; request.before unchanged
def test_a_preflight_failure_on_the_second_create_keeps_the_first_request(self)  # the first request is written
                                                        # with retries []; the second appends in order; at and
                                                        # before unchanged; the preflight failure stops, exit 1
def test_an_incomplete_inventory_never_offers_create(self)
def test_a_failure_before_the_post_is_not_a_lost_reply(self)  # preflight error -> phase inspected, 0 rechecks
def test_none_found_asks_before_creating_again(self)    # 2 minutes of rechecks; 'create' -> 2nd create; other -> 0
def test_several_found_asks_which_to_keep(self)
def test_another_account_stops_reconciliation(self)     # api.user() differs from the journal -> its line, 0 creates
def test_old_receipt_comes_from_olds_own_branch(self)   # OLD on a feature branch; the default receipt names another
                                                        # Codespace -> old_receipt and branch_receipt both journaled;
                                                        # OLD's receipt saved under a former branch -> find_receipt
def test_each_phase_resumes_on_rerun(self)              # a failure injected at each phase; rerun prints PHASE and
                                                        # continues there; creates <= 1 throughout
def test_dotfiles_retry_never_rebuilds(self)            # fail_personalization once -> rerun: rebuild 0, create 1
def test_runtime_steps_run_in_the_projects_own_runtime(self)  # every runner call: argv[1] the project's
                                                        # project-sync.sh, then --project PROJECT; cwd PROJECT; with
                                                        # scratch.start, keeper.start, workspace.remote and
                                                        # herdr.open_remote patched to raise in this process, the run
                                                        # still completes
def test_decline_or_cancel_before_the_move_keeps_old_paired(self)  # cutover exit 1 -> rerun asks; abandon removes
                                                        # this operation's seal, archives 'abandoned', names NEW's delete
def test_a_started_move_is_never_abandoned(self)        # journal 'ready' + this seal + scratch.json naming NEW ->
                                                        # no Gum choice; the cutover runs; phase moved
def test_a_failed_replay_still_finishes_forward_next_time(self)  # cutover consumes the intents, then fails; the
                                                        # next run still has the latch: no abandon offer
def test_phases_never_go_back(self)                     # move facts at signed-in, checked and offered: no repeated
                                                        # sign-in, check or delete offer; offered -> archived
def test_failed_signin_does_not_block_check_or_offer(self)
def test_new_check_failure_stops_before_the_offer(self) # delete never run; rerun runs cutover, then check
def test_a_delete_of_either_name_ends_the_journal(self) # deletions/NEW.json deleted -> archived 'deleted', 0 API calls
def test_declined_delete_still_completes(self)          # delete child exits 1 -> archived 'completed', the kept line
def test_nothing_gh_printed_reaches_a_journal_or_receipt(self)  # gh failures carrying a URL- and a token-shaped
                                                        # string at each setup call: neither appears in replace.json,
                                                        # replacements/ or replace-receipts/
def test_another_replace_running_refuses(self)
def test_inside_a_codespace_without_a_terminal_or_with_a_token_refuses(self)
def test_test_branch_override_comes_only_from_the_environment(self)
# tests/test_continuity_integration.py
def test_replace_steps_run_the_vendored_runtime(self)   # a rendered generated project; the coordinator's runtime
                                                        # command, run from another cwd with a hostile PYTHONPATH:
                                                        # `replace check` with an unknown operation exits 1 with the
                                                        # no-match line (the vendored subcommand ran), and the
                                                        # vendored runtime_id under the wrapper's bootstrap equals
                                                        # what vendored status computes and differs from the setup
                                                        # package's (the plan check's probes.py:207-219 method).
                                                        # It proves import identity and dispatch only; whether a
                                                        # started worker or keeper is current is the Live check's
                                                        # task work:status step
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_replacement
  tests.test_continuity_integration -v`. Expected: exit 1.
- [ ] **Step 3: Implement** `replacement.py` as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit**: `git add -A devcontainer_setup tests && git commit -m "Replace the paired Codespace in one
  rerunnable operation"`

### Task 6: Entry points, `task work` and the docs

**Files:**
- Modify: `devcontainer_setup/__main__.py`:
  - `:746-860` (`_resolve_launch`, `_ask_launch_resources`, `_codespace_choice`: `replace PAIRED`);
  - `:1195-1210` (the review's Codespace and Launch lines);
  - `:977-1056`, `:1081-1095` and `:1247-1256` (`_launch_on_default` and both publication endings);
  - `:1260-1310` (`--replace` mode).

  Also `devcontainer_setup/project_sync/standing.py` (read the active journal),
  `devcontainer_setup/project_sync/overview.py` (line one, the finish action, `Replace NAME`), `README.md:109-120`,
  and `docs/project-continuity.md`.
- Test: `tests/test_wizard.py`, `tests/test_overview.py`, `tests/test_work_overview.py`, `tests/test_standing.py`

**Interfaces:**
- Consumes: Task 5 (`replacement.run`), Task 1 (`replacing.read`, `replacing.ended_by_fact`).
- Produces, setup `__main__.py`:
  - `--replace` in the mutually exclusive mode group. It needs a terminal and runs `replacement.run(project)` and
    nothing else (no review, no publish).
  - In the wizard, `_codespace_choice` lists `replace PAIRED` when PAIRED is among the listed Codespaces and the
    menu's own read of it succeeded (`__main__.py:755-757`), on any branch (F10, narrowed by call 22). It sits after
    `rebuild PAIRED` when that is listed, else after `none`, and the default choice is unchanged.
  - The choice returns `{'action': 'replace', 'name': PAIRED}`. Every consumer of the launch value checks for
    `replace` before reading resources:
    - `_ask_launch_resources` asks nothing. `_resolve_launch` is unchanged: it never produces `replace`, and the
      rerun default stays `none`;
    - the review prints `Codespace: replace PAIRED (its machine and idle timeout); SSH: …` and no `Launch:` line;
    - `_launch_on_default` does its fetch, merge and checkout update for `replace`, then returns `(None, config)`
      without calling `api.launch`. Its two callers, the open-PR ending (`:1089-1092`) and the normal ending
      (`:1247-1256`), then skip `setup_continuity` for `replace` and return `replacement.run(project)` (N8). The
      coordinator does the create, cutover and sign-ins in its own order.
- Produces, `standing.py`: a `replacing` fact, `{old, new, phase}`, for an active journal that `ended_by_fact` does
  not end, read offline (no API call, no lock).
- Produces, `overview.py`: call 18's order. With `replacing`, line one is the Copy line and the action is `Finish
  replacing OLD` (`REPLACE`). The frame limits hold (6 lines, 78 cells, 3-6 actions). Without a TTY the `Next:` lines
  include `REPLACE` when a replace is unfinished.
- Docs: README's paragraph at `:109-117`, which today describes a manual `--rebind` after setup replaced a
  Codespace, names `task setup -- --replace` and the safe order: replace, check, then the checked delete. Keep
  stage C's text at `:118-120`. `docs/project-continuity.md` gains "Replace a Codespace", covering the commands, each
  phase and what a rerun does, and cancel before the move versus finish forward after it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_wizard.py
def test_paired_menu_lists_replace(self)                    # default stays 'none'; after rebuild when listed
def test_replace_is_listed_for_a_paired_feature_branch_codespace(self)
def test_replace_is_not_listed_for_an_unlisted_or_unreadable_pairing(self)  # pairing not listed (:1379), or its
                                                            # read fails (:802's unavailable case)
def test_replace_review_reads_no_resources(self)            # no KeyError; no resource prompt; the review line
def test_choosing_replace_runs_the_coordinator_after_publishing(self)  # default-branch ending and the open-PR
                                                            # ending: api.launch not called; replacement.run once,
                                                            # after the merge; setup_continuity not called on
                                                            # either ending
def test_replace_mode_runs_only_the_coordinator(self)       # --replace: no review screens; no TTY -> refused
def test_the_test_branch_override_is_never_shown(self)      # env set: menu labels and --help hold no override
# tests/test_standing.py
def test_active_replace_is_read_offline(self)               # 0 API calls; an ended-by-fact journal is not a fact
# tests/test_overview.py
def test_unfinished_replace_comes_first(self)               # line one and action one; recovery-off still above it
def test_replace_is_offered_after_connect_or_start_only(self)  # none in setup, setup-PR and failed-creation situations
def test_ended_replace_is_not_shown(self)
def test_every_overlay_together_keeps_the_ranked_actions(self)  # recovery off, unfinished replace, cleanup pending,
                                                            # a listed seal, a setup PR and a generator update:
                                                            # exact surviving actions in order, 6 lines, 78 cells
# tests/test_work_overview.py
def test_no_terminal_names_the_replace_in_next_lines(self)
```

  Tests whose expectations change on purpose (update them, select actions by name):
  - `tests/test_work_overview.py:290` (`test_menu_runs_the_chosen_command_after_printing_it`: arm is no longer item 1);
  - `tests/test_overview.py:131`, `:204`, `:387` and `:440` (exact action arrays, the second forget action and the
    upgrade position);
  - `tests/test_wizard.py:802` (`test_no_create_with_a_usable_pairing`: Replace listed for the readable pairing on
    `main` and `feature`, absent when its read fails; its other assertions unchanged), `:1361`, `:1414` (the paired
    subtest) and `:1438` (the menu gains `replace`). `:1379`'s unlisted pairing keeps its exact menu (call 22).
- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_wizard tests.test_standing
  tests.test_overview tests.test_work_overview -v`. Expected: exit 1.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit**: `git add -A devcontainer_setup tests README.md docs && git commit -m "Reach replace from
  the wizard, setup and task work, and document the safe order"`

## Existing guards that must stay green

- `tests/test_recovery_move.py` (all, above all `:146`, `:175`, `:187`, `:278`, `:294`, `:360`, `:455`).
- `tests/test_herdr.py`: the replaced-machine test (`:323`), the `test_prepared_*` tests (from `:645`) and the
  workspace-lock regressions (`:231`, `:246`, `:265`).
- `tests/test_workspace_recovery.py::test_remote_prepares_herdr_hooks_for_the_chosen_codespace` (`:458`) and
  `::test_remote_prepares_env_files_after_herdr_hooks` (`:480`).
- `tests/test_wizard.py`: the Codespace-menu tests not listed in Task 6,
  `::test_setup_records_created_evidence_even_when_not_paired` and
  `::test_a_replaced_codespace_ending_names_the_rebind_command`.
- `tests/test_codespaces.py`: the creation, resume, rebuild and personalization tests, above all
  `::test_codespace_created_on_default_branch_at_merge_sha` (`:264`), which ordinary launch keeps.
- `tests/test_render.py::test_managed_runtime_version_tampering_fails_validation` (`:548`),
  `::test_removing_managed_command_stops_update` (`:557`) and
  `::test_legacy_lock_native_cli_normalization_is_safe_to_validate_and_render` (`:794`).
- `tests/test_deletion.py` and `tests/test_seal.py` (all); `tests/test_checkpoint_store.py::test_collect_*` and
  `::test_identical_record_retry_does_not_read_heads_again` (`:246`) and
  `::test_unreadable_head_cannot_prove_retirement` (`:181`);
  `tests/test_replication.py:164-260` (relay chain); `tests/test_signins.py` (all).
- `tests/test_project_sync_cli.py::test_plain_rebind_of_an_armed_project_moves_recovery` and
  `::test_codespace_restart_still_requires_a_terminal`.
- `tests/test_root_tasks.py` (all; no task is added) and `tests/test_standing.py::test_managed_aliases_match_root_tasks`.

## Order and parallelism

At most three builders at once (shared Mac, memory `shared-mac-load-flakes`). Builders start only after the stage C
re-review's fixes (CPLAN Task 8) are merged, because D consumes `deletion.gather`, `deletion.run`, `seal.write` and
`verdict.resolve`.
- **Wave 1:** Tasks 1 and 4. Task 1 owns `replacing.py`, `verdict.py`, `deletion.py` and `checkpoint_store.py`. Task 4
  owns `codespaces.py`, `replace_inputs.py` and the two module-level helpers in setup `__main__.py`.
- **Wave 2,** from the merge of 1 and 4: Tasks 2, 3 and 5 in parallel.
  - Task 2 owns runtime `__main__.py`, `herdr.py`, `workspace.py`, `cutover.py`, and its lines in
    `test_workspace_recovery.py` and `test_scratch_sync.py`.
  - Task 3 owns `replace_check.py` and `tests/test_replication.py`.
  - Task 5 owns `replacement.py` and `tests/test_continuity_integration.py`. It codes against Task 2's and Task 3's
    receipt formats with a fake `runner`, and its integration test needs Task 2's subcommand, so it merges after
    Task 2.
- **Wave 3:** Task 6, from the merge of 2, 3 and 5. It owns setup `__main__.py`'s menu and mode, `standing.py`,
  `overview.py` and the docs.

The lead merges each into `feature/task-work-cd` after its own review and re-gate, one gate at a time. Then comes a
joined smoke: `task setup -- --replace` through the real Taskfile with a fake `gh` on PATH (as stage C's
`verify/joined-smoke.sh` did), expecting the inspection refusal for an unreadable machine and zero creates. A
whole-branch review covers C+D.

## Live check (the C+D release gate)

This merges spec "Tests the plan must include" (Live) with CPLAN's Live check. It runs from a clone of innovate with
its own `XDG_STATE_HOME`, following
`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/keeper-safety/T4-RUNBOOK.md` with INV §12's corrections:
`~/.ssh/config` and the Herdr machine list backed up, real innovate's alias block set aside and restored, a short
private state path, and the test SSH key authorized.
1. Pair a throwaway Codespace from a test branch.
2. Run `SETUP_REPLACE_TEST_BRANCH=<test branch> task setup -- --replace`.
3. Confirm, before the create, that resources and recovery are reported right.
4. Interrupt once during create and rerun: NEW is adopted or continued, and only one Codespace is created.
5. End an agent on OLD when asked.
6. Watch the seal, the move without TUI, the sign-ins and NEW's check, including one conversation from OLD by content.
   Then `task work:status` must show NEW's worker and keeper current for the clone's vendored runtime.
7. **At the coordinator's delete offer, do not type `delete`** (F17). It exits with `OLD is kept`.
8. Run `task work:cs:delete -- --codespace OLD --check --json`: exit code 0 and a deletable JSON verdict.
9. Make one owned scratch edit on OLD (a new file under `.scratch/`) and confirm the check reads Scratch Not safe.
   Remove exactly that file and confirm the check reads SAFE again.
10. Run the typed delete of OLD. NEW's `arming.json` and keeper must be unchanged.
11. Delete the test's NEW as the paired target. A turn written just before the check waits for a later cycle, the
    stop is recorded, and cleanup removes the clone's SSH alias and Herdr profile and nothing else.
12. Confirm on a real Codespace what offline tests cannot:
    - `/proc` readability;
    - the Git options the scan uses;
    - `pending_operation` present in the API body;
    - Git complete under `/workspaces` (CPLAN: any directory the Codespace user cannot read blocks by design; the
      lead decides a named exception on evidence);
    - Claude's resume-append behavior (stage C review P1-1);
    - the created-start evidence for NEW.
13. Restore everything the runbook set aside, then record each step's command and exit code in the live log before
    calling the gate passed.
