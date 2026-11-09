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
`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-d/inventory/INVENTORY.md` (INV §n). The
inventory predates Task 7; **line numbers in this plan are at c4088a3**, and builders re-derive any line a brief does
not repeat.

## Global Constraints

- Gates: `task check` and `task test` (`python3 -m unittest discover -s tests -v`), both exit 0, at your commit, in the
  default environment (no sandbox, no `TMPDIR` override):
  `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-a/RULING-gates.md`.
- Focused runs: `python3 -m unittest …` unpiped (or redirected to a file, then read `$?`).
- Branches: cut from `feature/task-work-cd` at the commit your brief names, merged back into it, never `main`. C and D
  ship together after one live test of replace end to end (spec call 1).
- Commit messages: plain imperative sentence case, one concern per commit, no AI attribution of any kind.
- **Nothing is spent before inspection passes.** Zero `codespace create` calls unless: OLD's machine name, core count,
  idle timeout and billing owner have been read from its live API body and are valid; OLD's machine is offered for the
  pinned commit with the same core count; the pinned commit carries the checkout's installed setup; and the journal
  on disk already holds the request. Tests count create calls.
- **One Codespace per operation.** At most one `codespace create` per operation. The only exception is the owner
  typing `create` after the bounded recheck found no new Codespace (call 10). No path deletes NEW. OLD is deleted
  only through `deletion.run` and its typed word.
- **A dotfiles retry never rebuilds.** Zero `codespace rebuild` calls when only personalization failed.
- `arming.json` is written only by the existing move path (`move.commit`, `__main__._arm`), never by
  `replacement.py`, `cutover.py` or `replace_check.py` (CHECK C1 still holds: a retired OLD's delete leaves NEW's bytes
  identical).
- **One writer per record.** Only `replacement.py` writes `STATE/replace.json` and `STATE/replacements/`. Runtime
  subprocesses write only their receipt file (the path they are given), plus what the subsystems they call already
  write. No runtime code takes `replace.lock`.
- **The coordinator starts nothing in-process.** `replacement.py` never calls a function that starts, stops, adopts or
  retargets a worker, keeper, Mutagen session, SSH alias or Herdr machine. Those run only in the subprocesses
  (spec "Where it runs"; `runtime_id` hashes the package path, `state.py:310-322`).
- No prompt between the final flush and the retarget: once `seal.write` returns, `workspace.remote` runs with no
  `ask`, `input` or `Gum` call in between (call 7).
- Never print or store contents, remote URLs, credentials or tokens. gh, Git and Herdr stderr is discarded, never
  echoed. Every untrusted string shown on a terminal goes through `overview.fit`. Journals and receipts hold names,
  logins, 40-hex commit ids, epochs, counts, tool keys, agent names and session ids only.
- Replace runs on the Mac, in a terminal, with a real gh account: it refuses inside a Codespace
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
   archives such a journal on its next run. `standing` and `verdict.resolve` treat it as ended at once.
3. **Only the paired Codespace is replaced.** No pairing, or a pairing for another project, refuses before any API call.
4. **Resources come only from OLD's live API body** (`Codespaces.get`): `machine.name`, `machine.cpus` (an int above
   0), `idle_timeout_minutes` (an int above 0) and `billable_owner.login`. Anything missing stops the operation
   before anything is spent. The launch receipt, the wizard's four-core default and the rebuild fallback are never
   consulted (INV §2, §12). OLD's machine must appear in `machines(repo, pinned commit)` with the same `cpus`.
   Location and retention are not copied: the spec names machine and idle timeout.
5. **NEW is built from the default branch's tip, pinned at inspection.** `git fetch` the default branch
   (`git_ops._fetch_default`), then pin the 40-hex `refs/remotes/origin/<default>` in the journal. Every create and
   resume of this operation uses that commit. The pinned commit carries the checkout's installed setup when the
   `customizations.devcontainerSetup` object in its `.devcontainer/devcontainer.json` equals the checkout's, and each
   `managedFiles` path at that commit hashes to its recorded SHA-256. Otherwise the replace refuses and names
   `task setup` (CHECK Q4). The test-only override `SETUP_REPLACE_TEST_BRANCH=<branch>` replaces the default branch
   with that branch for the fetch, the pin and the create. It is read only from the environment, and no menu or help
   text shows it (spec call 10).
6. **Replace never starts OLD.** Inspection reads the API only, so a stopped OLD is never started to read it. The
   cutover needs OLD `Available`. Otherwise it prints the restart line (`task work:cs:restart` records the keeper's
   start evidence) and stops at its phase.
7. **The typed `arm` comes before the final flush.** The spec runs stage A's consent at step 4. Here `move.decide`
   (which asks any typed `arm`) runs first inside the cutover, then the agents prompt, then the flush, the seal and
   the retarget. A prompt between the seal and the retarget would let OLD change after its final flush, with no way
   to flush again once paired elsewhere. `move.check_arming` still refuses inside `workspace.remote` if
   `arming.json` changed meanwhile. A rerun then decides again and seals again.
8. **Agents are the owner's to end.** Before the flush, the cutover scans OLD. While a `claude`, `codex`, `grok` or
   `opencode` process runs (the stage C rule: a Herdr server alone does not block), it prints them and asks the owner
   to press Enter to check again, or to type `stop`. Nothing is ever killed.
9. **What the final flush requires.** Paired `deletion.gather` evidence for OLD that is `steady`, where
   `verdict.judge` gives SAFE for Scratch, History and Agents. Git is not required: OLD keeps its commits until the
   checked delete proves them pushed. Then `seal.write(..., evidence=evidence, operation=<operation>)` runs, and the
   retarget follows. A rerun before the move seals again (`seal.write` overwrites). Abandoning removes a seal whose
   `operation` is this operation's.
10. **A lost create reply is resolved from the inventory, never by creating again.** Before `codespace create`, the
    journal records the request time and the names of every Codespace on the repository. NEW is adopted only when
    exactly one Codespace on the branch is absent from that set. With none, the coordinator rechecks every 10 s for up
    to 2 minutes, then asks the owner to type `create` (anything else stops). With several, the owner chooses one in
    Gum or cancels. An adopted NEW gets its start evidence from the coordinator (`evidence.record_start(...,
    'created')` with the journaled request time). Its first resume rebuilds once, because no receipt validated it.
    That rebuild is accepted on this rare path.
11. **The ordinary launch receipt carries NEW.** OLD's receipt bytes (the receipt for repository and branch, when it
    names OLD) are copied into the journal before the create, and `launch` then rewrites that receipt for NEW (spec
    step 2, CHECK Q5). A retry after a validation or personalization failure is `launch(resume=NEW)` with the
    journal's pinned commit. With `validated_commit` equal to it, `launch` skips the rebuild
    (`codespaces.py:379-385`). `launch` gains `resume_hint` so its failure names the replace rerun.
12. **No TUI during the replace.** `herdr.open_remote` and `workspace.remote` gain `enter=True`. The cutover passes
    `False`, so Herdr's machine and workspace are set up but the `herdr` client never runs. A nonzero Herdr result is
    recorded, not called connected. The replace ends with `Connect with task work:cs.`
13. **Sign-ins are recorded, not required** (spec call 3). The coordinator runs `work --signin-all`, then reads
    `STATE/signins.json`. The retry list is every tool whose state is not `signed-in`, `other-method` or `absent`, when
    the record names NEW and was updated after the step began, or `["unknown"]` otherwise. Steps 6 and 7 go ahead
    either way.
14. **NEW's check must pass before the delete offer.** Ruling 8 says "checks it, then offers to delete the old one".
    NEW passes when all five hold:
    - the launch receipt shows the pinned commit validated;
    - a sync cycle on NEW that started after the check began has completed healthy;
    - recovery points at NEW (when armed);
    - Herdr reachable;
    - every conversation in OLD's final flush is on NEW, kept on this Mac only (NEW refused it, ruling 30), or
      forgotten (#100 call 5).

    A missing or still-pending conversation fails the check. OLD stays, and a rerun checks again.
15. **A NEW the journal names and that was never paired is a `replacement` target.** Its first host key is accepted
    (`connection.accept_new = True`, `transport.py:312,363`). Its Git and Agents are checked as for any target. Its
    Scratch is SAFE only when the scan finds no file in `.scratch`, and its History only when it finds no conversation
    file. Anything present there exists only on NEW, so it reads UNSAFE. Its delete has no cleanup steps.
16. **Step 7 ends the operation whatever the owner answers.** The coordinator runs `delete --codespace OLD` in the
    terminal. Afterwards the journal is archived `completed`. A declined delete leaves OLD sealed and listed, and
    `task work` keeps offering it (stage C's slot for the oldest listed seal). An unfinished delete keeps its own
    journal and repair action.
17. **Cancel before the move, finish forward after it** (spec call 8). Ctrl-C or EOF at any prompt stops the run with
    its phase kept. A rerun before `moved` asks `continue` or `abandon`. Abandon removes this operation's seal, leaves
    OLD paired and NEW listed, and archives the journal `abandoned`. From `moved` on there is no abandon. A rerun whose
    last check failed runs the cutover again first (a plain reconnect, which repairs the worker, SSH and Herdr), then
    the check.
18. **`task work` shows an unfinished replace first** (line one and the first action, which reruns the replace). It
    offers `Replace NAME` for the paired Codespace after its connect action. The menu never shows the test branch
    override.

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
Created NEW.
Found NEW, created by the interrupted run.
Replace stopped at PHASE: REASON. Run REPLACE to continue.
A replace of OLD is at PHASE.
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
  `BRANCH at SHA12 does not carry this checkout's setup (PATH differs); nothing was created. Run task setup first.`
  (PATH = `.devcontainer/devcontainer.json` or the first differing managed file).
- Lost reply: `GitHub did not confirm the new Codespace. Looking for it for up to 2 minutes.` ·
  `No new Codespace appeared. Type create to create one, or anything else to stop.` ·
  `Several new Codespaces appeared: NAMES. Choose the one this replace keeps; delete the others with work:cs:delete.`
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
- Not armed: `Moved sync and Herdr to NEW.` · Herdr failed: `Moved sync to NEW; Herdr could not connect it (exit N).
  The replace checks it again.`
- `OLD is STATE. Start it with task work:cs:restart, then run the replace again.` · `Nothing moved.` (on `stop`, EOF
  or a refused consent) · `This replace record does not match; run the replace again.`
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
  minutes` · `Not ready: the last cycle on NEW was not healthy` · `Not ready: recovery still points at OLD` ·
  `Not ready: its keeper is not running` · `Not ready: Herdr cannot reach NEW` · `Not ready: N conversations from
  OLD are not on NEW (agent SESSION_ID, …)` · `Not ready: N conversations from OLD are still being published` ·
  `Not ready: the final flush of OLD cannot be read`.
- The Setup line is the coordinator's (it reads the launch receipt). The runtime check prints the other four.
- Lists keep the first 3 items, then `and N more`. JSON and receipts keep up to 100.

`task work` (`overview.py`): line one `Replacing OLD: stopped at PHASE. Finish it: REPLACE`, cut to 78 cells by
`overview.fit`. Its action label is `Finish replacing OLD`. The paired action label is `Replace NAME`.

## Review Focus

1. **A crash or Ctrl-C at every phase, including between `codespace create` and its reply.** A rerun continues the
   phase it is in, creates at most once, never rebuilds after a personalization failure and never deletes NEW
   (Task 5 `test_each_phase_resumes_on_rerun`, `test_lost_create_reply_adopts_exactly_one`,
   `test_dotfiles_retry_never_rebuilds`).
2. **OLD changes around its final flush**: an agent restarted during the prompt, a scratch edit, or `arming.json`
   rewritten by the keeper during the wait. Nothing moves on a stale flush. After the move, OLD's delete check reads
   anything late as changed (Task 2 `test_flush_requires_scratch_history_and_agents_safe`,
   `test_no_prompt_between_seal_and_retarget`, `test_changed_arming_after_decide_moves_nothing`; Task 1
   `test_sealed_old_changed_after_the_flush_is_unsafe`).
3. **Recovery across the move.** The same account carries consent with one line. A new account or billing owner asks
   before the flush. A decline moves nothing. After the move NEW's keeper runs, and deleting OLD leaves NEW's
   `arming.json` byte-identical (Task 2 `test_consent_is_decided_before_the_flush`, Task 3
   `test_armed_needs_new_keeper`, Task 1 `test_delete_of_sealed_old_after_a_replace_keeps_new_recovery`).
4. **Conversations that moved on.** A conversation continued after the flush, refused by NEW or forgotten is named,
   not counted missing. Equal counts with different content are missing (Task 3 `test_longer_revision_counts`,
   `test_equal_count_different_content_is_missing`, `test_refused_is_kept_on_this_mac_and_does_not_fail`; the real
   relay chain in `test_replace_check_passes_after_a_real_relay`).
5. **Readers during a replace.** `task work` puts the unfinished replace first. A delete of either name ends the
   journal. A never-paired NEW is checkable, and its files read UNSAFE. No runtime path takes `replace.lock`
   (Task 1 `test_delete_never_takes_the_replace_lock`, `test_replacement_with_files_is_unsafe`; Task 6
   `test_unfinished_replace_comes_first`).

---

### Task 1: The journal reader, the `replacement` target, and seal-pinned GC (runtime)

**Files:**
- Create: `devcontainer_setup/project_sync/replacing.py`
- Modify: `devcontainer_setup/project_sync/checkpoint_store.py:287-323` (`collect`),
  `devcontainer_setup/project_sync/verdict.py:101-134` (`resolve`) and `:342-355` (`judge`),
  `devcontainer_setup/project_sync/deletion.py:24-27` (`STEPS`), `:52-117` (`gather`), `:340-400` (connect, `--start`)
- Test: `tests/test_replacing.py` (new), `tests/test_checkpoint_store.py`, `tests/test_verdict.py`,
  `tests/test_deletion.py`

**Interfaces:**
- Consumes: `state.atomic_write`, `state.read_json`. It reads `deletions/<name>.json`'s `phase` with `read_json`
  itself and imports neither `deletion` nor `verdict` (`deletion` imports `verdict`, which imports `replacing`).
- Produces, `replacing.py`:
  - `JOURNAL = 'replace.json'`, `ARCHIVE = 'replacements'`, `LOCK = 'replace.lock'`, `RECEIPTS = 'replace-receipts'`.
  - `PHASES = ('inspected', 'create-requested', 'created', 'ready', 'moved', 'signed-in', 'checked', 'offered')`;
    `ENDED = ('completed', 'abandoned', 'deleted')`.
  - Journal `{"schema": 1, "operation": 12 lowercase hex, "old": str, "new": str | null, "account": str,
    "repository": "OWNER/REPO", "branch": str, "commit": 40 hex, "remote_root": str (NEW's folder), "resources":
    {"machine": str, "cpus": int, "idle_timeout_minutes": int, "billable_owner": str}, "old_receipt": dict | null,
    "request": {"at": float, "before": [str, …]} | null, "phase": PHASE, "at": {PHASE: float}, "signins": {"retry":
    [str, …], "at": float} | null, "check": dict | null, "ended": null | ENDED, "ended_at": float | null}`.
  - `read(state) -> dict | None`: the active journal. None when absent, or when an archive with the same `operation`
    exists (a crash between `archive` writing and unlinking). Malformed → `ValueError('The replace record cannot
    be read')`.
  - `write(state, journal) -> None`: validates, then `atomic_write` (sorted keys). Only `replacement.py` calls it.
  - `archive(state, journal, ended, now) -> Path`: writes `STATE/replacements/<operation>.json` with `ended` and
    `ended_at`, then unlinks `replace.json`. Idempotent.
  - `ended_by_fact(state, journal) -> str | None`: the name (OLD or NEW) whose `deletions/<name>.json` phase is
    `deleted` or `cleaned`, else None.
  - `naming_new(state, name) -> dict | None`: the newest journal, active or archived, whose `new == name`, unless
    `ended_by_fact` names `name`.
  - `receipt_path(state, operation, step) -> Path` (`STATE/replace-receipts/<operation>-<step>.json`, `step` in
    `cutover`, `check`). `read_receipt(path, operation, step) -> dict | None`: the decoded receipt when its
    `schema == 1`, `operation` and `step` match, else None.
- Produces, `verdict.py`: in `resolve`, after the seal and archive lookups and before `unpaired`,
  `naming_new(state, name)` gives `Target(name, 'replacement', journal['remote_root'], None, None)`. In `judge`,
  for `replacement`: Git and Agents as for `moved`. Scratch is SAFE `no files in .scratch` when the scan holds no
  scratch file, else UNSAFE `N files exist only on NAME (a, b, c)`. History is SAFE `no conversations on NAME` when
  it holds no session file, else UNSAFE `N conversation files exist only on NAME`. An incomplete scan part reads
  UNKNOWN, as for every kind.
- Produces, `deletion.py`: `STEPS['replacement'] = ()`. `gather` treats `replacement` like `moved` (one scan, no
  cutoffs, `steady=True`). In `run`, the connection for a `replacement` target gets `accept_new = True` before it is
  entered. `--start` accepts `replacement` like `sealed` and `moved`.
- Produces, `checkpoint_store.py`: `collect` adds every `STATE/retired/*.json` `snapshot_id` to `named` before
  computing `kept`. A seal file that cannot be read, or whose `snapshot_id` is not 64 hex, raises `ValueError` before
  anything is deleted (as an unreadable kept manifest does today).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_replacing.py
def test_round_trip_and_malformed(self)             # write/read equal; a bad phase, schema or commit -> ValueError
def test_archive_is_idempotent_and_hides_the_active_copy(self)   # crash after archive write: read() -> None
def test_ended_by_fact_follows_either_deletion_journal(self)     # deletions/NEW.json 'deleted' -> 'NEW'; 'confirmed' -> None
def test_naming_new_survives_an_abandon_until_new_is_deleted(self)
def test_receipt_must_match_operation_and_step(self)
# tests/test_checkpoint_store.py
def test_collect_keeps_what_a_seal_names(self)      # manifest older than keep_days and its blobs survive apply=True
def test_collect_deletes_nothing_when_a_seal_cannot_be_read(self)
# tests/test_verdict.py
def test_journal_named_new_is_a_replacement_target(self)
def test_unnamed_new_stays_unpaired(self)           # no journal, or the journal names another NEW
def test_replacement_with_files_is_unsafe(self)     # one .scratch file; one claude session file -> UNSAFE each
def test_empty_replacement_is_safe_for_scratch_and_history(self)
def test_sealed_old_changed_after_the_flush_is_unsafe(self)   # pins the existing sealed rule for D's Review Focus 2
# tests/test_deletion.py
def test_replacement_target_accepts_its_first_host_key(self)  # connect() result has accept_new True when entered
def test_replacement_delete_cleans_nothing_and_ends_the_journal(self)  # cleaned; ended_by_fact names NEW
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
  `devcontainer_setup/project_sync/workspace.py:393-501` (`remote(..., enter=True)`),
  `devcontainer_setup/project_sync/__main__.py:25-66,302-330` (the `replace` subcommand: `cutover` and `check`)
- Test: `tests/test_cutover.py` (new), `tests/test_herdr.py`, `tests/test_project_sync_cli.py`

**Interfaces:**
- Consumes: Task 1 (`replacing.read`, `replacing.read_receipt`), `move.decide`, `deletion.gather`, `verdict.resolve`,
  `verdict.judge`, `verdict.lines`, `target_scan.scan`, `seal.write`, `seal.read`, `workspace.remote`,
  `lifecycle.LifecycleAPI.read`, `arming.account`, `transport.CodespaceConnection`.
- Produces, `herdr.py` and `workspace.py`: `enter: bool = True`. With `False`, `open_remote` returns after
  `_workspace` without running the `herdr` client: 0 when the workspace opened, its nonzero result otherwise, and
  the `Codespace connected…` line only on 0. `workspace.remote` passes it through unchanged.
- Produces, `__main__.py`: subcommand `replace` with `cutover --operation OP --old OLD --new NEW --receipt PATH` and
  `check --operation OP --receipt PATH` (all required; argparse errors exit 2). Both dispatch before the terminal
  gate, like `delete`. `cutover` calls `cutover.run(..., recovery=lambda primary: _resume_recovery(primary,
  state))`. `check` calls `replace_check.run(...)` (Task 3; imported inside the handler).
- Produces, `cutover.py`: `run(project, state, *, operation, old, new, receipt, recovery, ask=input, tty=None,
  api_factory=lifecycle.LifecycleAPI, connect=transport.CodespaceConnection, now=time.time, sleep=time.sleep) -> int`.
  The receipt: `{"schema": 1, "operation", "step": "cutover", "old", "new", "moved": bool (scratch.json names NEW),
  "sealed_at": float | null, "cycle_id": str | null, "snapshot_id": str | null, "herdr": int | null, "at": float}`,
  written with `atomic_write` on every exit after step 1. Exit 0 only when `moved`.
- The order `run` follows (Copy section messages):
  1. Refuse inside a Codespace, without a TTY, and with an env token. `journal = replacing.read(state)` must be active
     with this `operation`, `old` and `new`, at phase `ready` or later. Otherwise print the "does not match" line and
     exit 1.
  2. When `scratch.json` names NEW already: `workspace.remote(project, state, enter=False)` (a plain reconnect), a
     receipt with `moved` true and the existing seal's fields, exit 0. No scan, gather or seal.
  3. `scratch.json` must name OLD. `api_factory(old, gh_user=account).read()` must be `ok` and `Available`, else print
     the STATE line and exit 1 (no POST of any kind).
  4. `decision = move.decide(project, state, new, ask=ask)` (None when not armed). A `ValueError` prints its message
     and `Nothing moved.`, then exits 1.
  5. Inside one `connect(old, state=state)`: scan with no cutoffs. While it shows a blocking agent, print the agents
     line and `ask('> ')`. EOF or `stop` → `Nothing moved.`, exit 1. Unreadable processes → the stage C UNKNOWN
     text, exit 1.
  6. `evidence = deletion.gather(project, state, target, connection, now=now, sleep=sleep)` and `verdict.judge`.
     Unless `steady` holds and Scratch, History and Agents are SAFE: the "not complete" lines, exit 1, no seal.
  7. `seal.write(state, Store(state), evidence.scan, evidence=evidence, identity=project.identity,
     repository=journal['repository'], gh_user=journal['account'], remote_root=target.remote_root,
     operation=operation, cycle_id=evidence.cycle['cycle_id'], snapshot_id=evidence.receipt['snapshot_id'], now=now())`,
     then at once `workspace.remote(project, state, rebind=new, move=decision, armed=recovery if decision is not None
     else None, enter=False)`. Print the moved line that fits. A `ValueError` from `remote` prints its message and
     writes the receipt with `moved` read from `scratch.json`.

- [ ] **Step 1: Write the failing tests** (`tests/test_cutover.py`: the `FakeAPI` and scripted-scan pattern of
  `tests/test_deletion.py`; `workspace.remote`, `move.decide` and `seal.write` wrapped to record call order; `ask`
  records each call):

```python
def test_consent_is_decided_before_the_flush(self)        # order: decide, scan, gather, seal.write, remote
def test_decline_moves_nothing_and_writes_no_seal(self)    # decide raises -> no gather, seal or remote; exit 1
def test_agents_are_asked_never_killed(self)               # claude running -> ask; Enter -> rescan clear -> proceeds;
                                                           # 'stop' -> exit 1; the connection ran only scan programs
def test_flush_requires_scratch_history_and_agents_safe(self)  # each UNSAFE/UNKNOWN or not steady -> no seal, no remote
def test_no_prompt_between_seal_and_retarget(self)         # no ask() after seal.write in any path
def test_changed_arming_after_decide_moves_nothing(self)   # arming.json rewritten during the agents prompt ->
                                                           # remote refuses (check_arming); OLD still paired; exit 1
def test_stopped_old_is_not_started(self)                  # Shutdown -> post_start 0, the restart line, exit 1
def test_rerun_after_the_move_reconnects_without_resealing(self)
def test_receipt_names_the_seal_and_herdr_result(self)     # Herdr exit 3 -> exit 0, receipt herdr 3, moved true
def test_mismatched_journal_refuses(self)                  # other operation, OLD, NEW, or phase before ready
# tests/test_herdr.py
def test_enter_false_never_runs_the_herdr_client(self)     # fake herdr on PATH logs argv: no bare `herdr` call
def test_enter_false_returns_nonzero_when_the_workspace_fails(self)
# tests/test_project_sync_cli.py
def test_replace_subcommand_options(self)                  # each missing option -> exit 2; dispatch is headless
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_cutover tests.test_herdr
  tests.test_project_sync_cli -v`. Expected: exit 1.
- [ ] **Step 3: Implement** `cutover.py`, `enter`, and the subcommand as in Interfaces.
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
  `scratch.status`, `scratch.inspect_sync`, `sync_cycle.summary`, `arming.read`, `keeper.status`,
  `restore.Herdr(machine_id).reachable()`.
- Produces: `run(project, state, *, operation, receipt, now=time.time, sleep=time.sleep, wait=360) -> int`. Read-only
  apart from its receipt. Exit 0 only when `ok`. Receipt: `{"schema": 1, "operation", "step": "check", "codespace":
  NEW, "checked_at": float, "sync": "ok" | TEXT, "cycle_completed_at": float | null, "recovery": "ok" | "not armed" |
  TEXT, "herdr": "ok" | TEXT, "conversations": {"on_new": [[agent, id], …], "kept": […], "forgotten": […],
  "missing": […], "pending": […]}, "ok": bool}`. `TEXT` is the Copy text after `Not ready: `.
- The rules:
  - The journal must be active with this operation at phase `moved` or later, and `scratch.json` must name NEW.
  - **Sync**: `t0 = now()`. Every 2 s, up to `wait`, read the registry, `scratch.status` and `scratch.inspect_sync`
    into `cycle_scope` (as `deletion.gather` does, `deletion.py:62-104`). Look for a `current` cycle with
    `started_at > t0` and `completed_at` set, then require `sync_cycle.summary(cycle, current=True,
    local_receipt=latest)['healthy']`.
  - **Recovery**: not armed → `not armed`. Armed → `arming['codespace'] == NEW` and `keeper.status(state)` running
    for NEW with the current runtime id.
  - **Herdr**: `herdr-machine.json` names NEW, and `reachable()` is true.
  - **Conversations**: the seal for OLD must have `operation` equal to this one, and its snapshot must `load`. Every
    entry `(agent, session_id)` in that manifest's `conversations.entries` is classified:
    - `forgotten` when `forget.forgotten(state)` holds it.
    - Otherwise it is **carried** when the outbound row of the same healthy cycle (`desktop_to_codespace`, its
      `snapshot_id` loaded from the Mac store) holds an entry with the same `(agent, session_id)` whose head blob
      starts with the sealed entry's head blob. The head is the file whose `session_id` is the entry's, as
      `Store._head` reads it, so a longer revision counts and different bytes do not.
    - A carried entry is `on_new` when that row's `publication` lists it under `imported` or `unchanged` (by
      `source_session_id`), and `kept` when the publication issues name it.
    - A carried entry that is neither is `pending`. An entry that is not carried is `missing`.
  - `ok` = sync `ok`, recovery `ok` or `not armed`, Herdr `ok`, and no `missing` or `pending`.

- [ ] **Step 1: Write the failing tests** (`tests/test_replace_check.py`: a real `Store` in a temporary STATE, a seal
  written through `seal.write`'s path, cycle fixtures in `scratch-runtime.json` as `tests/test_replication.py:509`
  builds them):

```python
def test_old_conversations_on_new_by_content(self)        # imported -> on_new; ok; exit 0
def test_longer_revision_counts(self)                     # outbound head extends the sealed head -> on_new
def test_equal_count_different_content_is_missing(self)   # same agent and id, different bytes -> missing; exit 1
def test_refused_is_kept_on_this_mac_and_does_not_fail(self)
def test_forgotten_is_named_and_does_not_fail(self)
def test_carried_but_unpublished_is_pending(self)
def test_cycle_must_start_after_the_check(self)           # an older healthy cycle never counts; wait expires ->
                                                          # 'no sync cycle completed in 6 minutes'
def test_armed_needs_new_keeper(self)                     # arming names OLD, or the keeper serves OLD -> not ok
def test_herdr_must_be_reachable(self)                    # receipt names OLD, or status 'unreachable' -> not ok
def test_a_seal_from_another_operation_is_not_read(self)
def test_check_writes_nothing_but_its_receipt(self)       # every STATE byte equal before and after, except receipt
# tests/test_replication.py
def test_replace_check_passes_after_a_real_relay(self)    # the chain of :164 and :245: OLD captured, sealed from its
                                                          # inbound manifest, relayed by the Mac, imported on NEW;
                                                          # then replace_check's conversation rule -> all on_new
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
- Modify: `devcontainer_setup/codespaces.py:322-444` (`launch`: the empty-reply fix and `resume_hint`)
- Test: `tests/test_replace_inputs.py` (new), `tests/test_codespaces.py`

**Interfaces:**
- Consumes: `Codespaces.get`, `Codespaces.machines`, `git_ops.git`, `git_ops._fetch_default`, `render` provenance
  layout (`customizations.devcontainerSetup`, `managedFiles` path → SHA-256, `render.py:394-406`).
- Produces, `replace_inputs.py`:
  - `@dataclass(frozen=True) class Resources: machine: str; cpus: int; idle_timeout_minutes: int; billable_owner: str`.
  - `read_resources(data: dict, repo: str) -> Resources`: raises `ValueError` with the Copy text naming the first
    unreadable FIELD, or the other-repository refusal. `bool` is not an int here.
  - `offered(machines: list[dict], resources: Resources) -> bool`: an entry with the same `name` and `cpus`.
  - `pin(project: Path, branch: str) -> str`: fetch `branch` the way `_fetch_default` does, then the 40-hex
    `refs/remotes/origin/<branch>`. A failure raises `ValueError` (`could not read BRANCH from GitHub`).
  - `setup_differs(project: Path, sha: str) -> str | None`: None when the checkout's installed
    `customizations.devcontainerSetup` equals the one at `sha`, and every `managedFiles` path at `sha` (`git show
    sha:PATH`, bytes) hashes as recorded. Otherwise the first differing PATH.
- Produces, `codespaces.py`:
  - An empty create reply raises the existing `ValueError('Creation did not return a codespace name…')`, not
    `IndexError` (origin fix: `splitlines()[-1]` on empty output, `codespaces.py:370`).
  - `launch(..., resume_hint: str | None = None)`: when given, it replaces the `Resume with: task
    devcontainer:setup -- --resume NAME` sentence in the raised message.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_replace_inputs.py
def test_premium_120_minutes_is_read_from_the_live_body(self)  # 8 cpus, 120 -> Resources(…, 8, 120, owner)
def test_each_missing_field_is_named(self)          # machine, cpus (absent, 0, '8', True), idle (absent, None, 0),
                                                    # billing owner -> the FIELD text
def test_another_repository_is_refused(self)
def test_offered_needs_the_same_cores(self)
def test_pin_reads_the_fetched_tip(self)            # a local bare origin advanced after clone -> its new tip
def test_setup_differs_names_the_first_file(self)   # equal -> None; edited managed file -> its path;
                                                    # different provenance block -> '.devcontainer/devcontainer.json'
# tests/test_codespaces.py
def test_empty_create_reply_is_a_value_error(self)  # FakeGitHub create prints nothing -> ValueError, no receipt
def test_resume_hint_replaces_the_setup_resume_sentence(self)
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_replace_inputs tests.test_codespaces -v`.
  Expected: exit 1.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit**: `git add -A devcontainer_setup tests && git commit -m "Read a Codespace's resources, pin the
  default tip and check it carries the setup"`

### Task 5: The coordinator (setup)

**Files:**
- Create: `devcontainer_setup/replacement.py`
- Test: `tests/test_replacement.py` (new)

**Interfaces:**
- Consumes: Task 1 (`replacing.*`, the journal and receipt formats), Task 4 (`replace_inputs.*`, `launch(...,
  resume_hint=…)`), Task 2's and Task 3's receipt formats (as files; never imported), `Codespaces` (`get`,
  `existing`, `machines`, `user`, `launch`), `codespaces.receipt_path`, `git_ops.default_branch`,
  `project_sync.state` (`discover_project`, `state_dir`, `locked`, `atomic_write`, `read_json`),
  `project_sync.evidence.record_start`, `project_sync.arming.read`, `project_sync.seal.read`, and the launch options
  `_launch_on_default` passes (`__main__.py:1029-1056`: dotfiles repository, machine profile, workspace folder,
  `state_root`; imported, not copied).
- Produces: `run(project: Path, *, api=None, ui=None, ask=input, env=os.environ, tty=None, runner=subprocess.run,
  now=time.time, sleep=time.sleep) -> int`. `runner` runs each runtime step as `[ 'bash', RUNTIME, … ]` with the
  terminal's stdin, stdout and stderr. `RUNTIME` is `PROJECT/.devcontainer/scripts/project-sync.sh` when it exists,
  else `PROJECT/scripts/project-sync.sh` (this repository).
- The order `run` follows (Copy section messages; every phase change is a `replacing.write` before the next effect):
  1. Refuse as in Global Constraints. Take `STATE/replace.lock` non-blocking for the whole run.
  2. `journal = replacing.read(state)`:
     - When `ended_by_fact` names a Codespace, archive the journal `deleted`, print its line and exit 0.
     - An active journal prints `A replace of OLD is at PHASE.` Before `moved`, `ui.choose` offers `continue` and
       `abandon`. Abandon removes the seal for OLD when its `operation` is this one, archives the journal
       `abandoned`, prints the abandon line that fits and exits 0.
     - Continue goes to the journal's phase.
  3. New operation (`inspected`): `scratch.json` names OLD. `read_resources(api.get(OLD), repo)` and
     `branch = env.get('SETUP_REPLACE_TEST_BRANCH') or git_ops.default_branch(project)` follow, then
     `commit = pin(project, branch)`, `setup_differs`, `offered(api.machines(repo, commit), resources)` and
     `account = api.user()`. Then the recovery lines (armed: carry predicted when `arming.gh_user == account`), and
     `old_receipt` = the receipt's JSON when it names OLD. The NEW folder is `config.workspaceFolder` or
     `/workspaces/<repo name>` (as `launch` resolves it). Write the journal with a fresh `operation`
     (`secrets.token_hex(6)`), then print `Replacing OLD (…).`
  4. `create-requested`: `request = {"at": now(), "before": names of api.existing(repo)}`, written. Then
     `api.launch(repo, branch, commit, machine, f'{idle}m', 'none', dotfiles, receipt, …, on_created=…,
     resume_hint=REPLACE)`. `on_created(name, before)` first journals `new = name` at `created`, then records start
     evidence as `_record_created` does. Any exception from `launch` while `new` is still null → the lost-reply rule
     (call 10). An exception after `new` is set → `Replace stopped at create: REASON. Run REPLACE to continue.`,
     exit 1. A return → `ready`.
  5. `created` (a rerun): `api.launch(..., resume=NEW, …)` with the journal's commit, machine and idle timeout → `ready`.
  6. `ready`: run `replace cutover --operation OP --old OLD --new NEW --receipt <receipt_path(…, 'cutover')>`. A
     receipt with `moved` → `moved`. Without a usable receipt, `scratch.json` naming NEW together with
     `seal.read(OLD).operation == OP` also → `moved`. Otherwise stop at its phase, exit 1.
  7. `moved`: `t = now()`, run `work --signin-all`. Then the retry list from `signins.json` (call 13) → `signed-in`.
     Print the retry line when the list is not empty.
  8. `signed-in`: when the journal's last check failed, run the cutover first (a reconnect). Then run `replace check
     --operation OP --receipt <receipt_path(…, 'check')>`. Print the Setup line from the launch receipt (`codespace
     == NEW`, `validated_commit == commit`, `rebuild_pending is False`). Both pass → journal the receipt at
     `checked`. Otherwise journal it, print the "not ready" line, exit 1.
  9. `checked`: print `NEW is ready. Next, …`, run `delete --codespace OLD`, then `offered` and archive `completed`.
     Print `Replaced OLD with NEW.`, plus `OLD is kept; …` unless `deletions/OLD.json` is `cleaned`, then
     `Connect with task work:cs.` Exit 0.

- [ ] **Step 1: Write the failing tests** (`tests/test_replacement.py`. `api` is a fake with `get`, `existing`,
  `machines`, `user` and a `launch` that delegates to the real `Codespaces.launch` over `FakeGitHub`
  (`tests/test_codespaces.py:22`), so create, rebuild and validation calls are counted. `runner` is a fake that
  records argv and writes the receipt a scripted step returns. `ui` and `ask` are scripted. STATE is a real temporary
  project state):

```python
def test_premium_120_minutes_beats_a_stale_four_core_receipt(self)  # receipt 4 cores/30m, live 8/120 -> create
                                                                    # argv --machine premium --idle-timeout 120m
def test_unreadable_resources_create_nothing(self)                  # each FIELD -> its line, 0 creates
def test_machine_not_offered_or_setup_missing_creates_nothing(self)
def test_journal_holds_the_request_before_create(self)  # FakeGitHub's create reads replace.json from disk:
                                                        # phase create-requested, request.before set
def test_lost_create_reply_adopts_exactly_one(self)     # reply lost, empty, or a raise after the POST; inventory +1 ->
                                                        # adopted, 1 create total, start evidence 'created' at request.at
def test_none_found_asks_before_creating_again(self)    # 2 minutes of rechecks; 'create' -> 2nd create; other -> 0
def test_several_found_asks_which_to_keep(self)
def test_each_phase_resumes_on_rerun(self)              # a failure injected at each phase; rerun prints PHASE and
                                                        # continues there; creates <= 1 throughout
def test_dotfiles_retry_never_rebuilds(self)            # fail_personalization once -> rerun: rebuild 0, create 1
def test_runtime_steps_run_in_the_projects_own_runtime(self)  # runner argv[1] is the project's project-sync.sh; with
                                                        # scratch.start, keeper.start, workspace.remote, herdr.open_remote
                                                        # patched to raise in this process, the run still completes
def test_decline_or_cancel_before_the_move_keeps_old_paired(self)  # cutover exit 1 -> rerun asks; abandon removes
                                                        # this operation's seal, archives 'abandoned', names NEW's delete
def test_after_the_move_there_is_no_abandon(self)
def test_failed_signin_does_not_block_check_or_offer(self)
def test_new_check_failure_stops_before_the_offer(self) # delete never run; rerun reconnects, then checks
def test_a_delete_of_either_name_ends_the_journal(self) # deletions/NEW.json deleted -> archived 'deleted', 0 API calls
def test_declined_delete_still_completes(self)          # delete child exits 1 -> archived 'completed', the kept line
def test_another_replace_running_refuses(self)
def test_inside_a_codespace_without_a_terminal_or_with_a_token_refuses(self)
def test_test_branch_override_comes_only_from_the_environment(self)
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_replacement -v`. Expected: exit 1.
- [ ] **Step 3: Implement** `replacement.py` as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit**: `git add -A devcontainer_setup tests && git commit -m "Replace the paired Codespace in one
  rerunnable operation"`

### Task 6: Entry points, `task work` and the docs

**Files:**
- Modify: `devcontainer_setup/__main__.py:811-860` (`_codespace_choice`: `replace PAIRED`), the interactive flow
  after publishing (the replace choice calls `replacement.run` instead of `_launch_on_default`), `:1260-1310`
  (`--replace` mode); `devcontainer_setup/project_sync/standing.py` (read the active journal);
  `devcontainer_setup/project_sync/overview.py` (line one, the finish action, `Replace NAME`); `README.md:106`;
  `docs/project-continuity.md`
- Test: `tests/test_wizard.py`, `tests/test_overview.py`, `tests/test_work_overview.py`, `tests/test_standing.py`

**Interfaces:**
- Consumes: Task 5 (`replacement.run`), Task 1 (`replacing.read`, `replacing.ended_by_fact`).
- Produces, setup `__main__.py`: `--replace` in the mutually exclusive mode group. It needs a terminal and runs
  `replacement.run(project)` and nothing else (no review, no publish). In the wizard, `_codespace_choice` lists
  `replace PAIRED` after `rebuild PAIRED` when the pairing is usable. It returns `{'action': 'replace', 'name':
  PAIRED}`, and after publishing the flow calls `replacement.run(project)`. The default choice is unchanged (`none`
  with a usable pairing).
- Produces, `standing.py`: a `replacing` fact, `{old, new, phase}`, for an active journal that `ended_by_fact` does
  not end, read offline (no API call).
- Produces, `overview.py`: with `replacing`, line one is the Copy line and the first action is `Finish replacing
  OLD` (`REPLACE`). It ranks below `Recovery is off until you connect` and above everything else. With a pairing and
  no active replace, `Replace NAME` (`REPLACE`) comes right after the connect action. The frame limits hold (6 lines,
  78 cells, 3-6 actions). Without a TTY the `Next:` lines include `REPLACE` when a replace is unfinished.
- Docs: README's "can be deleted at any time" line (`README.md:106`) names the safe order. `docs/project-continuity.md`
  gains "Replace a Codespace", covering replace, check, then the checked delete, with the commands, each phase and
  what a rerun does, and cancel before the move versus finish forward after it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_wizard.py
def test_paired_menu_lists_replace_after_rebuild(self)      # default stays 'none'
def test_choosing_replace_runs_the_coordinator_after_publishing(self)  # api.launch not called; replacement.run once
def test_replace_mode_runs_only_the_coordinator(self)       # --replace: no review screens; no TTY -> refused
def test_the_test_branch_override_is_never_shown(self)      # env set: menu labels and --help hold no override
# tests/test_standing.py
def test_active_replace_is_read_offline(self)               # 0 API calls; an ended-by-fact journal is not a fact
# tests/test_overview.py
def test_unfinished_replace_comes_first(self)               # line one and action one; recovery-off still above it
def test_replace_is_offered_for_the_paired_codespace(self)
def test_ended_replace_is_not_shown(self)
def test_frame_limits_hold_with_a_replace(self)             # 6 lines, 78 cells, 3-6 actions, long names cut by fit
# tests/test_work_overview.py
def test_no_terminal_names_the_replace_in_next_lines(self)
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_wizard tests.test_standing
  tests.test_overview tests.test_work_overview -v`. Expected: exit 1.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit**: `git add -A devcontainer_setup tests README.md docs && git commit -m "Reach replace from
  the wizard, setup and task work, and document the safe order"`

## Existing guards that must stay green

`tests/test_recovery_move.py` (all, above all `:146`, `:175`, `:187`, `:278`, `:294`, `:360`, `:455`);
`tests/test_herdr.py::test_remote_inside_herdr_reuses_machine_without_nested_client_or_settings_changes`,
`::test_a_replaced_codespace_gets_a_fresh_machine_and_retires_the_old_profile` and the `test_prepared_*` tests;
`tests/test_wizard.py` Codespace-menu tests (INV §11 table, `:747-1438`), `::test_setup_records_created_evidence_even_when_not_paired`,
`::test_a_replaced_codespace_ending_names_the_rebind_command`; `tests/test_codespaces.py` creation, resume, rebuild
and personalization tests (`:229-434`); `tests/test_overview.py` and `tests/test_work_overview.py` (all);
`tests/test_deletion.py` (all); `tests/test_seal.py` (all); `tests/test_checkpoint_store.py::test_collect_*`;
`tests/test_replication.py:164-260` (relay chain); `tests/test_signins.py` (all);
`tests/test_project_sync_cli.py::test_plain_rebind_of_an_armed_project_moves_recovery`,
`::test_codespace_restart_still_requires_a_terminal`; `tests/test_root_tasks.py` (all; no task is added);
`tests/test_standing.py::test_managed_aliases_match_root_tasks`.

## Order and parallelism

At most three builders at once (shared Mac, memory `shared-mac-load-flakes`). Builders start only after the stage C
re-review's fixes are merged, because D consumes `deletion.gather`, `deletion.run`, `seal.write` and
`verdict.resolve`. Wave 1: Tasks 1 and 4 (Task 1 owns `replacing.py`, `verdict.py`, `deletion.py` and
`checkpoint_store.py`; Task 4 owns `codespaces.py` and `replace_inputs.py`). Wave 2, from the merge of 1 and 4: Tasks
2, 3 and 5 in parallel. Task 2 owns runtime `__main__.py`, `herdr.py`, `workspace.py` and `cutover.py`. Task 3 owns
`replace_check.py` and `tests/test_replication.py`. Task 5 owns `replacement.py`, and it codes against Task 2's and
Task 3's receipt formats with a fake `runner`. Wave 3: Task 6, from the merge of 2, 3 and 5 (it owns setup
`__main__.py`, `standing.py`, `overview.py` and the docs). The lead merges each into `feature/task-work-cd` after its
own review and re-gate, one gate at a time. Then a joined smoke runs `task setup -- --replace` through the real Taskfile with a fake `gh` on PATH (as stage C's
`verify/joined-smoke.sh` did) and expects the inspection refusal for an unreadable machine with zero creates; a
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
7. Run `task work:cs:delete -- --codespace OLD --check --json` (exit code and JSON).
8. Make a scratch edit on OLD and confirm it reads Not safe.
9. Run the typed delete of OLD. NEW's `arming.json` and keeper must be unchanged.
10. Delete the test's NEW as the paired target. A turn written just before the check waits for a later cycle, the
    stop is recorded, and cleanup removes the clone's SSH alias and Herdr profile and nothing else.
11. Confirm on a real Codespace what offline tests cannot:
    - `/proc` readability;
    - the Git options the scan uses;
    - `pending_operation` present in the API body;
    - Git complete under `/workspaces` (CPLAN: any directory the Codespace user cannot read blocks by design; the
      lead decides a named exception on evidence);
    - Claude's resume-append behavior (stage C review P1-1);
    - the created-start evidence for NEW.
12. Restore everything the runbook set aside.
