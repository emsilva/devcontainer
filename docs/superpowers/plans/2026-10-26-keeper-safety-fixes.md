# Keeper Safety Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo the plan runs through the herd: one builder per task, one task after another.

**Goal:** Recovery is on whenever the owner believes it is, and every surface names the real cause and the real fix (#61, #60, #57).

**Architecture:** Part A adds this package's own record of each Codespace start (`start-evidence.json`) and lets the keeper weigh several plausible start times when GitHub's `last_used_at` can't be trusted. Part B carries the HTTP status on every state read, confirms a 404 with one list call, and gives the keeper, worker and owner commands a truthful "deleted" path. Part C makes plain `task work:cs` start the keeper on an armed project, using the consent on file.

**Tech Stack:** Python 3 standard library, `unittest`, go-task. Gates: `task check` (compileall, shellcheck, `git diff --check`) and `task test` (`python3 -m unittest discover -s tests -v`).

**Spec:** `docs/superpowers/specs/2026-10-25-keeper-safety-fixes-design.md` (owner-approved 2026-10-26). Read it with this plan; where they differ, the "Spec corrections" section below wins.

## Global Constraints

- Python standard library only. Fixed-text errors with no secrets. Private state through `state.atomic_write(path, data, mode=0o600)`. Arming writes under `locked(state / 'workspace-control.lock')`.
- Each task ships alone: its own tests, `task check` and `task test` exit 0, one commit. Order A → B → C; never in parallel (all three touch `keeper.py` and status).
- No change to the restart budget (`MAX_RESTARTS=2`, `RESTART_PERIOD`, `MIN_GAP`), `lifecycle.WINDOW=15*60`, `lifecycle.LIFETIME=12*3600`, holds, or the owner-stop rule (R33).
- A new `devcontainer_setup/project_sync/*.py` module is vendored into projects automatically (`render.py:360` globs the package). No list to edit.
- Test names are snake-case sentences (`test_a_…`); the first comment line cites the rule or issue (`# #61: …`).
- Anti-goals from the spec: no restart the owner didn't ask for, no second billed Codespace, no consent prompt the owner didn't trigger.

## Spec corrections (found by the code survey, 2026-10-26)

- `scratch.explain` is the method `_Notices.explain(self, error, message)` (`project_sync/scratch.py:353`). Its `clock` is `time.monotonic`; epochs for evidence come from `time.time()`.
- `Codespaces.start_codespace` (`project_sync/codespaces.py:126`) discards `posted_at`. Task 1 makes it return it.
- Setup's create (`devcontainer_setup/codespaces.py:365`, inside `Codespaces.launch`) has no project state dir, and `setup_continuity` runs only when continuity is chosen. The `created` writer is a callback that `_launch_on_default` (`devcontainer_setup/__main__.py:725`) passes into `launch`.
- `'Deleted'` is already in `lifecycle.TERMINAL`, so the worker's `notify_stop` already fires for it with the wrong copy. Task 2 replaces that copy.
- `LifecycleAPI` is documented and tested as "exactly three routes". Task 2 adds a fourth (the list) and updates `test_lifecycle_api_reads_and_posts_only_its_routes`.
- `HEARTBEAT_STALE=300` is both the R16 gap and the hung-keeper limit (`keeper.start`, `_Notices.watch_keeper`, `workspace.recovery_lines`). In phase `deleted` the keeper therefore keeps beating every `POLL` (60 s) and reads the API only every 15 minutes. The hung-keeper checks stay as they are.
- The spec's worker log line says "was deleted", but the spec also rules that only the keeper says "deleted" outright. The worker uses the hedged wording (Task 2).
- A stop the keeper saw (previous reading not Available) is not an "accept either" candidate: `_up`'s first branch never keeps the old anchor, and drops start evidence at or before that reading when the state is in `keeper.DOWN`. The worker records `stopped_at` only on a fresh state read. Residual, accepted: a first read that still shows Shutdown just after an owner's POST can stamp the new start; its evidence is then lost and the keeper falls back to `last_used_at`.

## Review Focus

- A corrupt, truncated or unreadable `start-evidence.json` (or a record for another Codespace) is treated as no evidence: the keeper anchors from `last_used_at` as today and never crashes. Test in Task 1.
- A state dir the evidence write can't reach (read-only, missing parent) never fails or delays a start; the error is logged. Test in Task 1.
- A 404 whose confirming list fails, or returns exactly 100 rows without the name (a second page could hold it), is not Deleted: it stays the auth reading. Test in Task 2.
- A Deleted reading followed by the Codespace answering 200 again (a wrong confirm, or GitHub hiccup) returns the keeper to watching, re-takes caffeinate and restarts the keep-alive. Test in Task 2.
- `work:cs` on an armed project when `arming.account()` raises (for example `GH_TOKEN` set, network down) still connects, prints the reconfirm line with that reason, and never prompts. Test in Task 3.

---

### Task 1: Start evidence and multi-candidate anchors (#61)

**Files:**
- Create: `devcontainer_setup/project_sync/evidence.py`
- Modify: `devcontainer_setup/project_sync/keeper.py` (`_up` 428-448, `_classify` 450-472, the continued-restart kind at 366, `_restart` 603)
- Modify: `devcontainer_setup/project_sync/lifecycle.py` (`classify` 195)
- Modify: `devcontainer_setup/project_sync/codespaces.py` (`start_codespace` 126)
- Modify: `devcontainer_setup/project_sync/workspace.py` (`_choose_pairing` callers at 65 and 78; `recovery_lines` Available branch 366-373)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`_owner_power`, after the restart's `lifecycle.start` at 204)
- Modify: `devcontainer_setup/project_sync/scratch.py` (`_Notices.explain` 353)
- Modify: `devcontainer_setup/codespaces.py` (`Codespaces.launch` 321, create at 365) and `devcontainer_setup/__main__.py` (`_launch_on_default` 725)
- Test: `tests/test_evidence.py` (new), `tests/test_keeper.py`, `tests/test_lifecycle.py`, `tests/test_codespaces.py`, `tests/test_wizard.py`, `tests/test_project_sync_cli.py`, `tests/test_scratch_sync.py`, `tests/test_workspace_codespaces.py`

**Interfaces:**
- Produces, in `evidence.py`:
  - `FILE = 'start-evidence.json'`; content `{"<codespace>": {"started_at": float, "source": "created"|"started"|"keeper", "stopped_at": float|None}}`.
  - `record_start(state: Path, codespace: str, started_at: float, source: str) -> None`: replaces that Codespace's record with `stopped_at: None`. Read-modify-write under `locked(state / 'start-evidence.lock', blocking=True)`, written with `atomic_write`. Never raises: `OSError`/`ValueError` are logged (module logger) and swallowed.
  - `record_stop(state: Path, codespace: str, stopped_at: float) -> None`: sets `stopped_at` when a record exists, `stopped_at >= started_at`, and the current `stopped_at` is None or earlier than `started_at`. Never raises.
  - `usable_start(state: Path, codespace: str, reading_at: float) -> float | None`: `started_at` when the record is this Codespace's, `stopped_at` is None or `< started_at`, and `reading_at - LIFETIME - WINDOW <= started_at <= reading_at`. A missing, corrupt or non-dict file is None.
- Produces, in `lifecycle.py`: `classify(*, last_up_at, first_down_at, anchors: list[float], owner_stop_at) -> str`. OWNER compares `owner_stop_at` with `min(anchors)`; LIFETIME when any anchor fits today's window test; an empty list behaves like today's `anchor_at=None`.
- Produces, in the keeper runtime: `anchor = {'at': float, 'source': 'api'|'observed'|'keeper'|'evidence'|'previous'}` plus `'also': [float, …]` (ascending) only when more than one candidate survives. A single-candidate anchor keeps exactly `{'at', 'source'}`, so existing equality assertions stand.
- Produces: `Codespaces.start_codespace(self, name=None) -> float | None` (the accepted POST's `posted_at`, None when it came back without a POST).
- Produces: `devcontainer_setup.codespaces.Codespaces.launch(..., on_created: Callable[[str, float], None] | None = None)`, called right after a successful create with `(name, before)`, where `before = time.time()` is taken just before `self.command(*args)` at 365. Not called on a resumed Codespace.
- Produces: `arming.short_time(epoch: float) -> str` = local `'%H:%M'`.

- [ ] **Step 1: Write the evidence unit tests** in `tests/test_evidence.py`: `test_a_recorded_start_is_usable_inside_the_window`, `test_a_start_older_than_twelve_hours_and_the_window_is_not_usable`, `test_a_stop_seen_after_the_start_makes_it_unusable`, `test_a_newer_start_after_a_stop_is_usable_again`, `test_another_codespaces_record_is_ignored`, `test_a_corrupt_evidence_file_reads_as_no_evidence` (file contains `b'{not json'`; `usable_start` is None, `record_start` then rewrites a valid file), `test_a_write_error_is_logged_and_never_raised` (state is a file, not a dir; `record_start` returns None and logs), `test_the_file_is_private` (`stat` mode `0o600`).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_evidence -v`. Expected: ImportError / failures.
- [ ] **Step 3: Implement `evidence.py`** with the three functions above, importing `LIFETIME` and `WINDOW` from `lifecycle` and `atomic_write`, `read_json`, `locked` from `state`.
- [ ] **Step 4: Run** the same command. Expected: OK.
- [ ] **Step 5: Change `lifecycle.classify` to `anchors`** and update the 8 `ClassifyTests` (`tests/test_lifecycle.py:416-471`) to pass `anchors=[…]`. Add `test_lifetime_fits_any_candidate_anchor` (anchors `[A, A+6h]`, stop at `A+6h+12h` → LIFETIME; stop at `A+12h` → LIFETIME) and `test_owner_is_judged_against_the_earliest_anchor` (owner stop between `A` and `A+6h`, anchors `[A, A+6h]` → OWNER). Run `python3 -m unittest tests.test_lifecycle -v`; expected OK after the change.
- [ ] **Step 6: Write the keeper tests** in `tests/test_keeper.py`, using `KeeperStepTests` helpers (`keeper()`, `run_steps`, `runtime()`, `up()`, `down()`, `failed()`) and `evidence.record_start(self.state, 'cs-1', …)`:
  - `test_a_fresh_keeper_counts_from_recorded_start_evidence_not_a_later_last_used_at` (the spec's first test): evidence `started` at `A`; a fresh keeper reads `up(A+6h+60, last_used_at=A+6h)`, then downs from `A+12h`. Assert `kind == 'LIFETIME'` and the anchor is `{'at': A, 'source': 'evidence', 'also': [A+6h]}`.
  - `test_evidence_older_than_an_unseen_stop_is_ignored_and_last_used_at_still_counts` (the mirror): evidence at `A-3h` with `stopped_at=A-1h`; fresh keeper reads `up(A+60, last_used_at=A)`; downs at `A+12h`. Assert LIFETIME and anchor `{'at': A, 'source': 'api'}`.
  - Rewrite `test_a_start_the_keeper_never_saw_is_anchored_from_last_used_at` (970) as `test_a_start_the_keeper_never_saw_counts_both_its_evidence_and_last_used_at`: add evidence at the real start; expect LIFETIME. Comment: the owner ruled "accept either" (2026-10-25); missing a real 12-hour stop costs a night down, a false restart is budgeted.
  - Rewrite `test_a_reading_gap_that_could_hide_a_restart_anchors_from_last_used_at` (1011) as `test_a_reading_gap_keeps_the_earlier_anchor_as_a_candidate`: same readings; expect anchor `{'at': A, 'source': 'previous', 'also': [s]}` and LIFETIME, same comment.
  - `test_candidates_within_the_window_count_as_one`: evidence at `A`, `last_used_at = A + 10 min`; anchor has no `'also'`.
  - `test_without_a_gap_a_newer_last_used_at_is_still_ignored`: continuous `up` readings whose `last_used_at` moves; anchor unchanged (guard for the spec's "nothing changes").
  - `test_a_classified_stop_records_stopped_at` and `test_the_keepers_own_restart_records_keeper_evidence` (read `start-evidence.json`; `source == 'keeper'`, `started_at == posted_at`).
  - `test_a_corrupt_evidence_file_never_stops_the_keeper` (Review Focus 1).
- [ ] **Step 7: Run** `python3 -m unittest tests.test_keeper -v`. Expected: the new and rewritten tests FAIL.
- [ ] **Step 8: Implement the keeper changes.** In `_up`, wherever an `'api'` anchor is taken today (both branches), build candidates: `('evidence', evidence.usable_start(self.state, codespace, reading.at))`, `('api', reading.last_used_at)`, and, in the second branch's gap case only, `('previous', old_anchor['at'])` when an old anchor exists and isn't `'keeper'`. The first branch saw the stop, so the old anchor is never a candidate; when that previous state is in `keeper.DOWN`, drop every candidate at or before that reading's time (`last_used_at` is later, so the api candidate remains). Drop None values, sort by time, merge any within `WINDOW` of the earliest kept value, take the earliest as `at`/`source`, the rest as `also`. In `_classify` pass `anchors=[anchor['at']] + anchor.get('also', [])`. At line 471 (and the direct `watch['kind']='LIFETIME'` at 366) call `evidence.record_stop(self.state, codespace, watch['first_at'])`. At 603 call `evidence.record_start(self.state, codespace, result['posted_at'], 'keeper')`. Keep the keeper's own POST as the single anchor of its run.
- [ ] **Step 9: Run** `python3 -m unittest tests.test_keeper tests.test_lifecycle -v`. Expected: OK.
- [ ] **Step 10: Write the writer tests:**
  - `tests/test_codespaces.py`: `test_launch_reports_a_new_codespace_and_the_time_before_create` (fake runner; `on_created` gets `('new-space', t)` with `t` ≤ the create call's time) and `test_launch_does_not_report_a_resumed_codespace`.
  - `tests/test_wizard.py` (near `_launch_on_default`, 1469): `test_setup_records_created_evidence_even_when_not_paired`, which reads `start-evidence.json` under `state_dir(discover_project(project))` and finds `source == 'created'`.
  - `tests/test_project_sync_cli.py` `OwnerPowerTests`: `test_an_owner_restart_records_started_evidence`, and `test_a_refused_owner_restart_records_no_evidence`.
  - `tests/test_workspace_recovery.py` or `tests/test_workspace_codespaces.py`: `test_work_cs_starting_a_stopped_codespace_records_started_evidence`.
  - `tests/test_scratch_sync.py`: `test_the_worker_records_stopped_at_when_it_learns_a_down_state`.
- [ ] **Step 11: Run** those files. Expected: FAIL.
- [ ] **Step 12: Implement the writers:** `on_created` in `launch`; `_launch_on_default` passes a closure writing `record_start(state_dir(discover_project(project)), name, before, 'created')`; `start_codespace` returns `result['posted_at']` and both `_choose_pairing` call sites record `'started'` when it is not None; `_owner_power` records `'started'` from `result['posted_at']` when not None; `_Notices.explain` calls `record_stop(state, codespace, time.time())` only when this call read the state (`error.state` or `read_codespace_state`) and that state is in `keeper.DOWN`, stamped with that read's wall-clock time; a cached `known` records nothing.
- [ ] **Step 13: Status copy.** Add `arming.short_time`. In `recovery_lines`, the Available line becomes `'Codespace    Available; 12-hour stop expected ~' + short_time(at + LIFETIME) + ''.join(' (or ~' + short_time(a + LIFETIME) + ')' for a in also) + ', counted from its last start at ' + short_time(at)`. Update `tests/test_workspace_codespaces.py:193` to assert the new wording, computing expected times with `arming.short_time` so the test doesn't depend on the time zone. Add `test_status_shows_each_kept_start_as_an_alternative`.
- [ ] **Step 14: Gates.** `task check` exit 0, then `task test` exit 0. Record the final `Ran N tests` line.
- [ ] **Step 15: Commit** `Count the 12-hour stop from this package's own start record too (#61)`.

### Task 2: A deleted Codespace is named as deleted (#60)

**Files:**
- Modify: `devcontainer_setup/project_sync/lifecycle.py` (`Reading` 39-45, `LifecycleAPI.read` 172, docstring "three routes")
- Modify: `devcontainer_setup/project_sync/transport.py` (`codespace_state` 48)
- Modify: `devcontainer_setup/project_sync/keeper.py` (constructor 216, `_pass` 321, `supervise` 762-813, new phase `deleted`)
- Modify: `devcontainer_setup/project_sync/scratch.py` (`_Notices.explain` 353, `notify_stop` 363, retry backoff near 708)
- Modify: `devcontainer_setup/project_sync/workspace.py` (`recovery_lines`; `_choose_pairing` 60)
- Modify: `devcontainer_setup/project_sync/codespaces.py` (`_api` failure path near 41)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`_owner_power` 188-196)
- Test: `tests/test_lifecycle.py`, `tests/test_ssh_proxy.py`, `tests/test_keeper.py`, `tests/test_scratch_sync.py`, `tests/test_workspace_codespaces.py`, `tests/test_project_sync_cli.py`

**Interfaces:**
- Consumes: Task 1's evidence module (unchanged).
- Produces: `Reading(..., http: int = 0)`, the new last field (positional constructions stay valid). `LifecycleAPI.read()` sets `http` to the response status (0 on a network failure).
- Produces: `LifecycleAPI.confirm_deleted(self) -> bool | None`: one `GET https://api.github.com/user/codespaces?per_page=100` with the same account. True when 200 and fewer than 100 rows and none named `self.name`; False when the name is listed; None when the call fails or returns exactly 100 rows without the name. On a 404, `read()` calls it: True → `Reading(at, True, 'Deleted', None, None, 404)`; otherwise today's unreadable reading with `http=404`. 401/403 stay auth; 0 stays network.
- Produces: `transport.codespace_state(name, token, *, upstream=None) -> str | None` returns `'Deleted'` under the same confirm rule (its own GET through `upstream(url, token)`).
- Produces: `Keeper(..., caffeinate=None)`, an object with `hold() -> None` and `release() -> None`; `supervise` passes a small wrapper around today's `_caffeinate()` process that also keeps `runtime['caffeinate']` truthful (pid or None).
- Produces: keeper constant `DELETED_POLL = 15 * 60`; phase string `'deleted'`.

- [ ] **Step 1: Write the read tests** in `tests/test_lifecycle.py` (`LifecycleAPITests`, fake `upstream(method, url, token)`): `test_a_404_confirmed_by_the_list_reads_as_deleted`, `test_a_404_whose_list_fails_stays_unreadable`, `test_a_404_with_a_full_first_page_stays_unreadable` (Review Focus 3), `test_a_401_stays_an_auth_reading_without_a_list_call`, `test_readings_carry_their_http_status`. Update `test_lifecycle_api_reads_and_posts_only_its_routes` to allow the list route. In `tests/test_ssh_proxy.py`: `test_codespace_state_names_a_confirmed_deletion`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_lifecycle tests.test_ssh_proxy -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `Reading.http`, `confirm_deleted`, the 404 path in `read()`, the docstring, and the `codespace_state` change.
- [ ] **Step 4: Run** the same command. Expected: OK.
- [ ] **Step 5: Write the keeper tests** (`FakeAPI` gains a way to return a `'Deleted'` reading; a `FakeCaffeinate` with `hold`/`release` counters):
  - `test_a_deleted_codespace_notifies_once_stops_the_keepalive_and_releases_caffeinate`: notification key `deleted-cs-1`, text exactly `<project>'s Codespace cs-1 was deleted. Recovery is paused and this Mac can sleep. Pair a new one with task work:cs:armed -- --rebind <name>.` (the second `<name>` is literal), `FakeKeepAlive` stopped, `release` called once, `runtime['caffeinate']` None, phase `deleted`.
  - `test_in_phase_deleted_the_keeper_beats_every_poll_but_reads_every_fifteen_minutes`: over 30 fake minutes, heartbeat age stays ≤ 60 s and `api.reads` grows by 2.
  - `test_a_codespace_that_answers_again_after_deleted_returns_to_watching` (Review Focus 4): `hold` called again and the keep-alive restarted.
  - `test_a_deleted_keeper_is_not_replaced_by_keeper_start_for_the_same_codespace`.
- [ ] **Step 6: Run** `python3 -m unittest tests.test_keeper -v`. Expected: FAIL.
- [ ] **Step 7: Implement** the `caffeinate` handle (constructor and `supervise`) and the `deleted` path in `_pass`. While `deleted`, keep calling `beat()` every `POLL` and call `api.read()` only once `DELETED_POLL` has passed since the last read. Never exit; a rebind or disarm replaces the keeper as today.
- [ ] **Step 8: Run** `python3 -m unittest tests.test_keeper -v`. Expected: OK.
- [ ] **Step 9: Write the status, worker and command tests:**
  - `tests/test_workspace_codespaces.py`: `test_status_names_a_deleted_codespace_for_an_armed_project` (exact line `Codespace    cs-1 no longer exists on GitHub (deleted). Pair another with task work:cs:armed -- --rebind <name>, or run task work:disarm.`); `test_status_names_a_deleted_codespace_for_an_unarmed_project` (`… Pair another with task work:cs -- --rebind <name>.`).
  - `tests/test_scratch_sync.py`:
    - `test_the_worker_logs_a_deleted_codespace_once_and_retries_every_fifteen_minutes`: log line `Scratch paused: cs-1 is not visible to octo: it was deleted, or it belongs to another account. Your .scratch/ is intact (last synchronized HH:MM).`
    - `test_an_unarmed_projects_worker_notifies_a_deleted_codespace_with_the_hedged_copy`.
    - `test_an_armed_projects_worker_leaves_the_deleted_notice_to_the_keeper`.
    - `test_the_keeper_watch_accepts_a_beating_keeper_in_phase_deleted`.
  - `tests/test_project_sync_cli.py`:
    - `test_work_cs_on_a_missing_pairing_lists_this_repositorys_codespaces`: exact copy `cs-1 is not visible to octo: it was deleted, or it belongs to another account. This repository's Codespaces: a, b. Move to one with task work:cs -- --rebind <name>, or create one with task devcontainer:setup.` For an armed project the move hint is `task work:cs:armed -- --rebind <name>`. With none listed: `This repository has no Codespaces.`
    - `test_work_cs_stop_on_a_confirmed_deletion_says_nothing_to_stop`: `cs-1 no longer exists on GitHub; there is nothing to stop.`, and the owner-stop record is still written first (R33).
    - `test_work_cs_restart_on_a_confirmed_deletion_says_nothing_to_stop`.
- [ ] **Step 10: Run** those files. Expected: FAIL.
- [ ] **Step 11: Implement.**
  - `recovery_lines`: phase `deleted` gets the status line above.
  - `_Notices.explain` and `notify_stop`:
    - the hedged copy, with the login read once per episode through `GET /user` with the same token; `this Mac's GitHub account` when that fails;
    - at most one retry every 15 minutes while the state is Deleted, instead of the 60 s backoff ceiling;
    - armed projects leave the notification to the keeper.
  - `Codespaces._api` keeps gh's stderr on failure and raises a distinct error for `HTTP 404`.
  - `_choose_pairing` turns that into the listing message using `spaces()` and the active login.
  - `_owner_power`: on a 404 from `post_stop`, if `api.confirm_deleted()` is True, raise the "nothing to stop" text; otherwise keep today's 4xx text.
- [ ] **Step 12: Gates.** `task check` and `task test`, both exit 0.
- [ ] **Step 13: Commit** `Name a deleted Codespace as deleted and let the Mac sleep (#60)`.

### Task 3: Recovery comes back on connect (#57)

**Files:**
- Modify: `devcontainer_setup/project_sync/__main__.py` (`_arm` 61-78, `_armed` 103, `run()` `--codespace` 301-307)
- Modify: `devcontainer_setup/project_sync/workspace.py:346`, `devcontainer_setup/project_sync/scratch.py:410`
- Modify: `docs/project-continuity.md:174-175`
- Test: `tests/test_project_sync_cli.py`, `tests/test_workspace_recovery.py`, `tests/test_workspace_codespaces.py:234`, `tests/test_scratch_sync.py` (`KEEPER_NOTE`, 1177)

**Interfaces:**
- Consumes: `keeper.start`, `keeper.wait_ready`, `keeper.foreign_processes`, `workspace.recovery_lines` (unchanged).
- Produces: `_consent_on_file(project, state) -> str | None`. It returns None when `_arm`'s short-circuit holds (same consent hash, Codespace and gh account), otherwise a short reason: `the gh account changed`, `the Codespace changed`, `the consent text changed`, or the fixed text of the `ValueError`/`OSError` that `arming.account` or `Codespaces.space()` raised. `_arm` reuses it for its short-circuit.
- Produces: `_resume_recovery(project, state) -> None`. It never calls `input`, and is passed as `armed=` to `workspace.remote` from the `--codespace` path when `arming.read(state).get('armed')`.

- [ ] **Step 1: Write the tests** (`ArmFlowTests` helpers in `tests/test_project_sync_cli.py`; ordering helpers in `tests/test_workspace_recovery.py:57`):
  - `test_work_cs_on_an_armed_project_starts_the_keeper_without_a_prompt`: `input` patched to fail if called; `keeper.start` called once; recovery lines printed.
  - `test_work_cs_starts_the_keeper_after_the_worker_and_before_herdr`.
  - `test_work_cs_with_stale_consent_connects_without_the_keeper_and_says_why`: exact `Recovery is armed but needs your confirmation again (the gh account changed); run task work:cs:armed.`
  - `test_work_cs_when_the_account_check_fails_still_connects_and_says_why` (Review Focus 5).
  - `test_a_foreign_gh_process_blocks_the_keeper_not_the_connection`: today's foreign-process message printed, `keeper.start` not called, Herdr still opened.
  - `test_work_cs_on_an_unarmed_project_starts_no_keeper`.
  - Keep `test_…rebind…` at 412: plain `--rebind` on an armed project still refuses.
  - Copy: update `tests/test_workspace_codespaces.py:234` to `'Keeper       not running; run task work:cs'` and `KEEPER_NOTE` to `Recovery for {project} is not running; run task work:cs`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_project_sync_cli tests.test_workspace_recovery tests.test_workspace_codespaces tests.test_scratch_sync -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `_consent_on_file`, `_resume_recovery` and the `--codespace` wiring, plus the two copy changes, plus the docs line, replaced with: `Run task work:cs after a Mac restart; for an armed project it starts recovery again. Until then recovery is off.`
- [ ] **Step 4: Run** the same command. Expected: OK.
- [ ] **Step 5: Gates.** `task check` and `task test`, both exit 0.
- [ ] **Step 6: Commit** `Start recovery again on a plain task work:cs (#57)`.

### Task 4: Live verification (lead, not a builder)

Run after Tasks 1–3 are on main and pushed. It creates a billed Codespace, so the lead confirms with the owner before Step 2.

- [ ] **Step 1: Isolate.** Use `XDG_STATE_HOME=/Users/mannu/code/devcontainer/tmp/livetest-keeper/state` for every command (`state._state_home` honours it), and a fresh clone of innovate at `/Users/mannu/code/devcontainer/tmp/livetest-keeper/innovate-keeper-test` on a new branch `test/keeper-safety-<date>`. The distinct basename keeps the Herdr machine label apart from the real innovate.
- [ ] **Step 2: Create** with `gh codespace create --repo kyndryl-agentic-ai/consult-sm-innovate --branch test/keeper-safety-<date>` (billed to kyndryl-agentic-ai, never paired with the real checkout). Then `task work:cs` (records `started` evidence) and `task work:cs:armed`.
- [ ] **Step 3: Check, recording each command and its output in `tmp/livetest-keeper/REPORT.md`:**
  - a fresh keeper anchors on `evidence`;
  - `last_used_at` before and after one browser open;
  - delete the Codespace: the Deleted status line, one notification, caffeinate released, the worker paused;
  - after `task work:stop`, plain `task work:cs` brings the keeper back. Rebind first, or use a second test Codespace for this step.
- [ ] **Step 4: Clean up.** Delete the test Codespace and branch, then `task work:stop` and `task work:disconnect` under the test `XDG_STATE_HOME`. Comment the results on #61, #60 and #57, and close each one whose live check passed.
