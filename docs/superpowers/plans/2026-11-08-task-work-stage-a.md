# `task work` stage A: smarter commands. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `task work:cs:stop` runs headless on the Mac and from inside the Codespace, and `task work:cs -- --rebind NEW`
moves recovery of an armed project, carrying consent when the account and billing owner are unchanged.

**Architecture:** The Mac's stop moves into `project_sync/power.py`, ahead of the terminal gate, and validates its
target and writes the owner stop in one step under `workspace-control.lock`. Inside a Codespace the same command drops
a stop request that the Mac worker's link watcher claims, as it claims link requests, and answers with a result file.
A rebind of an armed project is decided (account, NEW's billing owner, a typed `arm` only when they changed) before
`workspace.remote` takes its lock, journaled before the retarget, and committed by a new `arming.move_consent` after
it; the next `work:cs` finishes a move a crash interrupted, registry included.

**Tech Stack:** Python 3 standard library (the package is vendored into projects and run as `python3 -I -B`), go-task,
`unittest`.

**Spec:** `docs/superpowers/specs/2026-11-02-task-work-design.md`, section "A. Smarter commands", its Anti-goals,
Constraints and "Tests the plan must include" (A list). Builder check: `tmp/codespace-lifecycle/task-work/builder-check/CHECK.md`
(C1, C3, C6, C7, Q8). Plan checks this revision answers: `tmp/codespace-lifecycle/task-work/stage-a/plancheck/CHECK.md`
(F1-F11) and `tmp/codespace-lifecycle/task-work/stage-a/plancheck2/CHECK.md` (CHECK2, N1-N5). Read the spec and your task before starting.

## Global Constraints

- Gates: `task check` and `task test` (`python3 -m unittest discover -s tests -v`), both exit 0, at your commit.
- Focused test runs: run the `python3 -m unittest …` command unpiped (or redirect to a file and read `$?`); a `| tail`
  hides the exit code.
- Branches: every builder branch is cut from `feature/task-work-ab` and merges back into it, never into `main`.
  Stage A ships together with stage B after a live check of the stop and the move (spec call 1).
- Commit messages: plain imperative sentence case, one concern per commit, no AI attribution of any kind.
- `_consent_on_file` (`devcontainer_setup/project_sync/__main__.py:65-88`) keeps its exact-Codespace and digest check
  for ordinary connects. A move never calls `arming.arm()` to carry consent (it resets hold, owner stop and timestamps).
- `arming.json` may gain keys (CHECK C7). No new key in any file whose loader rejects unknown keys (the user
  `config.json`). New files are written with `state.atomic_write` (private, atomic).
- Restart (`work:cs:restart`) and a first arm stay terminal-only.
- The move's printed line, exactly: `Recovery moved OLD → NEW; consent from DATE kept (ACCOUNT, billing OWNER).`
  DATE is the local date of `consented_at` as `YYYY-MM-DD`.
- Stdlib only; no new dependency. Everything a Codespace runs stays `python3 -I -B` compatible.
- No client, organization or Codespace names in code, tests, docs or commit messages (the repository is public).

## Review Focus

1. **A stop never lands on a Codespace the project moved to.** A rebind between reading the pairing and writing the
   owner stop must not stamp NEW's `arming.json` (CHECK F1; Task 1 test `test_stop_revalidates_the_target_under_the_lock`).
2. **A late stop request never runs.** One older than 60 seconds is discarded by the remote watcher, and the Mac
   checks the request's own expiry after taking the lock, before any write or POST, however long the line waited
   (Task 2 tests `test_stale_stop_request_is_discarded`, `test_line_delivered_after_expiry_is_refused`).
3. **Only a stop-capable Mac worker is trusted.** A sign-in-only watcher or an older Mac refreshes `mac-listening`
   but never claims `stops/`; the writer then refuses up front instead of leaving a request a later watcher could
   claim (Task 2 test `test_writer_requires_the_stop_capability`).
4. **"Nothing stops" is final.** Cancel or the deadline racing the watcher's rename, in both orders, never reports
   "nothing stops" for a request the Mac took (Task 2 tests `test_cancel_races_the_claim_in_both_orders`).
5. **NEW never inherits OLD's owner stop or pending restore,** and a crash-interrupted move never re-arms a project
   the owner disarmed meanwhile (Task 3 `test_move_retires_owner_stop_and_pending_restore`; Task 4
   `test_replay_after_disarm_does_not_rearm`).

---

### Task 1: Stop on the Mac runs headless and validates its target under the lock

**Files:**
- Create: `devcontainer_setup/project_sync/power.py`
- Modify: `devcontainer_setup/project_sync/__main__.py` (`_owner_power` at 216-299, `run` at 355-374)
- Modify: `devcontainer_setup/root_tasks.py:9` (`INTERACTIVE`), `templates/continuity-tasks.yml:41-45`, `Taskfile.yml:39-43`
- Test: `tests/test_power.py` (new), `tests/test_project_sync_cli.py`, `tests/test_root_tasks.py::test_new_work_tasks_render_and_alias`

**Interfaces:**
- Produces, in `power.py`:
  - `Stopped` (frozen dataclass): `codespace: str`, `armed: bool`, `api`, `stopped_at: float`, `message: str`.
  - `stop(state: Path, *, expected: str | None = None, until: float | None = None, halted=lambda: False, api=None,
    now=time.time, sleep=time.sleep) -> Stopped`. Prints nothing. In order:
    1. Refuse with `CODESPACES=true` (today's stop text) and with `GH_TOKEN`/`GITHUB_TOKEN` set (today's text).
    2. Take `workspace-control.lock`: blocking when `until` is None (the CLI); otherwise poll the non-blocking lock
       every 0.2 s and raise `ValueError('The stop request expired before it could run; nothing stopped.')` once
       `now() >= until`, or `ValueError('The Mac worker is shutting down; nothing stopped.')` once `halted()`.
    3. Under the lock: read the binding (`Pair a Codespace first: run task work:cs` when missing); when `expected` is
       set and differs, `ValueError(f'The stop request names {expected}, but this project is paired with {binding};
       nothing stopped.')`; read arming and refuse an armed mismatch with today's text; check `until` once more; write
       the owner stop for that Codespace.
    4. Release the lock; build `api` (when not given) from the Codespace and arming read under the lock, exactly as
       today; POST and handle its status with today's texts. `message` is today's "Stopping …" line.
- `_owner_power(project, state, *, restart, api=None, now=..., sleep=...)` keeps its signature. It keeps its own
  restart-specific `CODESPACES=true` refusal (`Run task work:cs:restart on your Mac …`) before delegating, calls
  `power.stop(state, api=api, now=now, sleep=sleep)`, prints `message` when `restart` is false, and otherwise
  continues with today's restart half.

- [ ] **Step 1: Write the tests**

`tests/test_power.py`:

```python
def test_stop_revalidates_the_target_under_the_lock(self):
    # hold workspace-control.lock; start power.stop(state, expected='old-cs', until=now+60, api=fake) on a thread;
    # while it waits, rewrite scratch.json and arming.json to name 'new-cs'; release the lock
    self.assertIsInstance(self.error, ValueError)
    self.assertIn('names old-cs, but this project is paired with new-cs', str(self.error))
    self.assertNotIn('owner_stop_at', arming.read(self.state))
    self.fake.post_stop.assert_not_called()

def test_cli_stop_stops_what_is_paired_when_it_gets_the_lock(self):
    # same race with expected=None: the owner stop is written in the arming that names new-cs, and the API is built
    # for new-cs (patch lifecycle.LifecycleAPI and assert its first argument)

def test_owner_stop_is_written_before_the_post(self):          # fake post_stop asserts owner_stop_at present
```

`tests/test_project_sync_cli.py`:

```python
def test_codespace_stop_runs_without_a_terminal(self):          # red today
    result = self.run_cli('work', '--codespace-stop')
    self.assertEqual(result.returncode, 1)
    self.assertNotIn('require a terminal', result.stderr)
    self.assertIn('Pair a Codespace first', result.stderr)

def test_codespace_restart_still_requires_a_terminal(self):     # guard: green today, must stay green
    result = self.run_cli('work', '--codespace-restart')
    self.assertEqual(result.returncode, 1)
    self.assertIn('Work entrypoints require a terminal', result.stderr)
```

`tests/test_root_tasks.py::test_new_work_tasks_render_and_alias`: interactive tuple becomes
`('work:cs:armed', 'work:cs:restart')`; the non-interactive one gains `'work:cs:stop'` (red today).

- [ ] **Step 2: Run them; the new `test_power.py` tests, the stop CLI test and the root-task test fail**

Run: `python3 -m unittest tests.test_power tests.test_project_sync_cli tests.test_root_tasks -v`
Expected: exit 1; `test_codespace_restart_still_requires_a_terminal` and every existing `OwnerPowerTests` test pass.

- [ ] **Step 3: Implement** as in Interfaces. In `run`, handle `args.codespace_stop` before the `isatty` gate;
`args.codespace_restart` stays after it. Drop `interactive: true` from `work:cs:stop` in the template and the root
`Taskfile.yml`, and `'work:cs:stop'` from `INTERACTIVE`.

- [ ] **Step 4: Run the gates**

Run: `task check && task test`
Expected: exit 0, with `OwnerPowerTests.test_owner_commands_inside_a_codespace_say_to_run_them_on_the_mac` and
`test_codespace_stop_records_the_owner_stop_before_its_post` unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A devcontainer_setup templates Taskfile.yml tests
git commit -m "Let task work:cs:stop run without a terminal and check its target under the lock"
```

### Task 2: Stop from inside the Codespace

**Files:**
- Modify: `devcontainer_setup/project_sync/power.py`
- Modify: `devcontainer_setup/project_sync/links.py` (`REMOTE_WATCHER` 377-443, `Watcher._serve` 529-582,
  `Watcher.stop` 617-628, a new `Watcher._stop_request`)
- Modify: `devcontainer_setup/project_sync/transport.py` (`CodespaceConnection.python` 432, `__exit__` 451)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`run`)
- Modify: `README.md:55`, `docs/project-continuity.md:33` and `204-209` (the stop works from inside the Codespace
  through the Mac worker; restart and the token rules stay Mac-only; what the timeout and cancel outcomes print)
- Test: `tests/test_power.py`, `tests/test_links_watcher.py`, `tests/test_project_sync_cli.py`, the transport test file

**Interfaces:**
- Consumes: `power.stop(state, expected=, until=, halted=)` (Task 1).
- Produces, in `power.py`: `LINKS = '.local/state/devcontainer-links'`, `WINDOW = 60`, `LISTENING_AGE = 15`,
  `CAPABILITY = 'stop-listening-v1'`, and
  `request_from_codespace(*, home: Path, env: Mapping, now=time.time, sleep=time.sleep, wait: float = WINDOW) -> int`.
- Files under `~/LINKS` on the Codespace:
  - `stop-listening-v1`: touched every 5 s by the normal watcher only (never under `SIGNINS_ONLY`), next to
    `mac-listening`, which stays as it is.
  - `stops/<time_ns>-<pid>-<8 hex>.json`, mode 0600, written to a temporary name and renamed:
    `{"schema": 1, "kind": "stop", "id": "<stem>", "created_at": <float>, "codespace": "<CODESPACE_NAME>"}`.
  - `stops/<id>.result`, mode 0600, written atomically by the Mac: `{"ok": <bool>, "message": "<text>"}`.

Behavior:
- **Writer** (`request_from_codespace`): `stop-listening-v1` missing or older than `LISTENING_AGE` → print
  `No Mac worker is connected to this Codespace, so nothing stops. Stop it from your Mac with task work:cs:stop;
  otherwise it stops after its idle timeout.`, return 1, write nothing. Otherwise write the request and poll every
  0.5 s until `wait` elapses for the answer: print its message, prefixed `Your Mac recorded this as your stop. ` when
  `ok`; remove the answer; return 0 if `ok` else 1. At the deadline `unlink` the request: success → print
  `Your Mac did not take the stop request within 60 seconds, so nothing stops. Check task work:status on your Mac.`;
  `FileNotFoundError` → print `Your Mac took the stop request but has not answered; check task work:status on your
  Mac.`; return 1 either way. Any other `OSError` on that unlink is reported as the uncertain case, never as "nothing
  stops". `KeyboardInterrupt`: the same unlink arbitration, printing `Cancelled; nothing stops.` or
  `Cancelled, but your Mac already took the stop request.`; return 130.
- **Remote watcher**: creates `stops/` with `requests/` and `claimed/` (0700, same checks). Unless `SIGNINS_ONLY`,
  it touches `stop-listening-v1` with `mac-listening`, and claims each `stops/*.json` by rename into `claimed/`, reads
  it with the same size and `O_NOFOLLOW` rules, and prints the request as one JSON line only when the shape matches
  exactly, `created_at` is finite and `0 <= now - created_at <= 60`; otherwise one stderr line
  `Skipped stale or invalid stop request`. It never touches `.result` files.
- **Watcher, Mac half**: a stdout line whose JSON has `kind == 'stop'` goes to `_stop_request(value)`, never to
  `parse_request`. It refuses (one worker-log line, an answer `{"ok": false, …}`) when `value['codespace']` is not
  the connection's Codespace, or when `created_at` is more than 5 seconds ahead of the Mac's clock (fail closed on
  clock skew). The expiry is the request's own, `until = value['created_at'] + WINDOW` on the Mac's clock, so time
  the line spent buffered or waiting counts against it (CHECK2 N2). Otherwise it starts one thread, kept in
  `self._stopping`, that calls `power.stop(self.state, expected=value['codespace'], until=until,
  halted=self._halt.is_set)`; a second request while `self._stopping` is alive is answered
  `{"ok": false, "message": "A stop is already in progress."}`. The thread logs `Stop requested from the Codespace:
  <message or error>` (never a URL or token), then, unless halted, writes the answer with
  `self.connection.python(..., timeout=10)`; a failed write is logged, not raised.
- **Ownership**: `Watcher.stop` sets the halt, then joins `self._stopping` (at most 90 s, raising
  `ValueError('Codespace stop request has not finished')` like the existing join) before it returns, so the worker
  closes the connection only after the handler ends. A handler still waiting for the lock gives up on the halt without
  writing anything.
- **Transport**: `CodespaceConnection.__exit__` marks the connection closed, and `python()` on a closed connection
  raises `ValueError('The Codespace connection is closed')` instead of re-entering readiness (CHECK F5).
- **CLI**: in `run`, before the terminal gate, `args.codespace_stop` with `CODESPACES=true` returns
  `power.request_from_codespace(home=Path.home(), env=os.environ)`; it never reaches `power.stop`, whose
  `GITHUB_TOKEN` refusal a Codespace login shell would trip (CHECK C3).

- [ ] **Step 1: Write the failing tests**

`tests/test_power.py` (a fake home in a `TemporaryDirectory`, a fake clock, a `sleep` that advances it and can run a
hook):

```python
def test_writer_requires_the_stop_capability(self):   # only mac-listening fresh: return 1, stops/ empty, no-worker text
def test_answer_is_printed_and_removed(self):          # hook writes <id>.result ok: return 0, prefix printed, stops/ empty
def test_unclaimed_request_is_removed_at_the_deadline(self):   # return 1, 'within 60 seconds, so nothing stops'
def test_claimed_without_answer_says_so(self):         # hook renames the request away: 'took the stop request but has not answered'
def test_cancel_races_the_claim_in_both_orders(self):  # KeyboardInterrupt before the rename: 'Cancelled; nothing stops.';
                                                       # after it: 'Cancelled, but your Mac already took the stop request.'; 130
def test_request_file_shape(self):                     # keys exactly {schema, kind, id, created_at, codespace}; mode 0o600
```

`tests/test_links_watcher.py`, with the `REMOTE_WATCHER` harness at lines 43-45 and the `_serve` fakes the file
already uses:

```python
def test_watcher_streams_a_fresh_stop_request(self):        # line is the request (kind 'stop'); file gone from stops/
def test_stale_stop_request_is_discarded(self):              # created_at 61 s ago, a future one, a NaN: no stdout line
def test_signins_only_watcher_leaves_stop_requests(self):    # SIGNINS_ONLY: file stays, stop-listening-v1 not touched
def test_normal_watcher_advertises_the_stop_capability(self):
def test_stop_request_goes_to_power_not_links(self):         # handle() never; power.stop called with expected and until
def test_stop_request_for_another_codespace_is_refused(self): # power.stop never called; answer ok false
def test_stop_expires_while_waiting_for_the_lock(self):       # lock held past until: no owner stop, no POST
def test_line_delivered_after_expiry_is_refused(self):        # created_at 100 s before dispatch: no owner stop, no POST
def test_request_from_the_future_is_refused(self):            # created_at 10 s ahead of the Mac clock
def test_second_stop_while_one_runs_is_answered_busy(self):
def test_watcher_stop_joins_the_stop_handler(self):           # handler waiting on the lock ends on halt, no write,
                                                              # and Watcher.stop returns only after it
```

Transport test file (the one testing `CodespaceConnection`):

```python
def test_python_after_exit_raises_instead_of_reconnecting(self):
```

`tests/test_project_sync_cli.py`:

```python
def test_codespace_stop_inside_a_codespace_sends_a_request(self):
    # CODESPACES=true and GITHUB_TOKEN set: request_from_codespace called once, power.stop never
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_power tests.test_links_watcher tests.test_project_sync_cli -v`
Expected: exit 1; the new tests fail or error, every existing test passes.

- [ ] **Step 3: Implement** as Behavior says, and update the README and continuity-guide lines listed under Files.

- [ ] **Step 4: Run the gates**

Run: `task check && task test`
Expected: exit 0, with `test_supervise_starts_and_stops_watcher_with_connection` and the other existing
`tests/test_links_watcher.py` tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A devcontainer_setup README.md docs tests
git commit -m "Let an agent inside the Codespace stop it through the Mac worker"
```

### Task 3: Consent move in arming

**Files:**
- Modify: `devcontainer_setup/project_sync/arming.py`
- Test: `tests/test_arming.py` only

**Interfaces:**
- Produces:
  - `carries_over(value: dict, *, project_name: str, gh_user: str, old: str, new_owner: str) -> bool`: true only when
    `value['armed']`, `value['codespace'] == old`, `value['gh_user'] == gh_user`, `new_owner` is non-empty and equals
    `value['billable_owner']`, and `value['consent_sha256'] == sha256(consent_text(project_name, old,
    value['billable_owner']))`. Pure.
  - `move_consent(state: Path, value: dict, *, project_name: str, new: str, now: float, ports: list[int] | None = None)
    -> str`: raises `ValueError` unless `carries_over` holds for `value` with its own account and owner. Archives
    `value` exactly as read to `state / 'arming-history' / f'{time.time_ns()}.json'`, then writes it with
    `codespace=new`, `consent_sha256` for `consent_text(project_name, new, owner)`, `moved_from=old`,
    `moved_at=now`, `ports` replaced only when given (sorted), and without `owner_stop_at` and `pending_restore`. Keeps
    `armed_at`, `consented_at`, `gh_user`, `billable_owner`, `hold_until`. Returns the move line. Takes no lock (the
    caller holds `workspace-control.lock`); does not touch the restart ledger.

- [ ] **Step 1: Write the failing tests** in `tests/test_arming.py`, in a new class `ConsentMoveTests` with its own
`setUp` (the existing `ArmingTests` fixture stays an empty directory: `test_read_defaults_when_missing` relies on it).
Fixture: a temporary directory as `self.state`;
`self.armed = {'schema': 1, 'armed': True, 'armed_at': 1000.0, 'consented_at': 1000.0, 'gh_user': 'me',
'codespace': 'old-cs', 'billable_owner': 'me', 'ports': [3000], 'hold_until': 5000.0, 'consent_sha256':
sha256(arming.consent_text('proj', 'old-cs', 'me'))}`, written with `arming.write`; `self.ok = dict(project_name='proj',
gh_user='me', old='old-cs', new_owner='me')`.

```python
def test_same_account_and_owner_carries_over(self):
    self.assertTrue(arming.carries_over(self.armed, **self.ok))

def test_changed_owner_account_policy_or_missing_owner_does_not_carry(self):
    for change in ({'new_owner': 'org'}, {'gh_user': 'other'}, {'new_owner': ''}):
        self.assertFalse(arming.carries_over(self.armed, **dict(self.ok, **change)))
    self.assertFalse(arming.carries_over(dict(self.armed, consent_sha256='0' * 64), **self.ok))

def test_move_keeps_timestamps_hold_and_ports(self):
    line = arming.move_consent(self.state, self.armed, project_name='proj', new='new-cs', now=2000.0)
    moved = arming.read(self.state)
    for key in ('armed_at', 'consented_at', 'ports', 'hold_until', 'gh_user', 'billable_owner'):
        self.assertEqual(moved[key], self.armed[key])
    self.assertEqual((moved['codespace'], moved['moved_from'], moved['moved_at']), ('new-cs', 'old-cs', 2000.0))
    self.assertEqual(moved['consent_sha256'], sha256(arming.consent_text('proj', 'new-cs', 'me')))
    self.assertRegex(line, r'^Recovery moved old-cs → new-cs; consent from \d{4}-\d{2}-\d{2} kept \(me, billing me\)\.$')

def test_move_with_ports_replaces_only_ports(self):           # ports=[3000, 8080]; archive holds [3000]

def test_move_retires_owner_stop_and_pending_restore(self):
    value = dict(self.armed, owner_stop_at=1500.0, pending_restore={'rid': 'r1'})
    arming.move_consent(self.state, value, project_name='proj', new='new-cs', now=2000.0)
    self.assertNotIn('owner_stop_at', arming.read(self.state))
    self.assertNotIn('pending_restore', arming.read(self.state))
    archived = [json.loads(p.read_text()) for p in (self.state / 'arming-history').glob('*.json')]
    self.assertEqual([a['owner_stop_at'] for a in archived], [1500.0])

def test_move_refuses_what_does_not_carry(self):              # owner changed: ValueError, arming.json bytes unchanged

def test_ordinary_connect_still_refuses_a_mismatched_target(self):
    # __main__._consent_on_file with arming naming old-cs, Codespaces.space() mocked to new-cs (owner 'me'),
    # arming.account mocked to 'me': returns 'the Codespace changed'
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_arming -v`
Expected: exit 1, `AttributeError` on `carries_over`; `test_ordinary_connect_still_refuses_a_mismatched_target` and
every existing `ArmingTests` test pass.

- [ ] **Step 3: Implement** `carries_over` and `move_consent` as in Interfaces.

- [ ] **Step 4: Run the gates**

Run: `task check && task test`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add devcontainer_setup/project_sync/arming.py tests/test_arming.py
git commit -m "Carry recovery consent to a new Codespace when account and billing owner match"
```

### Task 4: A rebind moves recovery

**Files:**
- Create: `devcontainer_setup/project_sync/move.py`
- Modify: `devcontainer_setup/project_sync/workspace.py` (`_choose_pairing` 47-112, `remote` 206-295)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`run`: remove the armed-rebind refusal at 378-381)
- Modify: `README.md:108-109` (one rebind command, which now moves recovery too)
- Test: `tests/test_recovery_move.py` (new), `tests/test_project_sync_cli.py:741-753`

**Interfaces:**
- Consumes: `arming.carries_over`, `arming.move_consent` (Task 3); `keeper.stop`; `scratch.retarget`.
- Produces, in `workspace.py` (for every rebind, armed or not; CHECK F2):
  - `PENDING = 'workspace-pending.json'`: `{"codespace": NEW, "predecessor_sha256": <sha256 of workspace.json bytes,
    or null when there is no workspace.json>, "value": <the registry remote() is about to write>}`, written just
    before `scratch.retarget` and removed after the registry is written. A binding without `workspace.json` is a valid
    state (`registry()` returns a default without writing it, `scratch.configure` writes only the binding; CHECK2 N1).
    At the start of `remote()`, under its lock and before `_choose_pairing`: when it exists, the binding names its
    Codespace and `workspace.json` still matches the predecessor (same hash, or still absent for null), archive the
    current registry to `workspace-history/` when there is one and write `value`, as `remote()` does; in every case
    remove the file and re-read the registry.
  - `_choose_pairing(project, state, value, rebind=None, expect_owner=None)`: when `expect_owner` is given, right
    after the GET and the repository check and before any start or root discovery, raise
    `ValueError(f'{name} changed billing owner since you confirmed; nothing moved.')` unless the row's
    `billable_owner.login` is non-empty and equals it. Return value unchanged (15 test call sites mock it).
  - `remote(project, state, armed=None, rebind=None, before_herdr=None, move=None)`: with a `move.Decision`, passes
    `expect_owner=move.billable_owner`, requires the chosen name to be `move.new`, and runs, after the root
    verification: `move.begin`, the `PENDING` write, `keeper.stop(state)`, `scratch.retarget`, the registry write and
    `PENDING` removal, then `move.commit` and print its line. NEW's keeper is started by the `armed` callback
    (`_resume_recovery`) after the lock is released, as today. Without `move`, only the `PENDING` steps are new.
- Produces, in `move.py`:
  - `INTENT = 'recovery-move.json'`.
  - `Decision` (frozen dataclass): `old: str`, `new: str`, `gh_user: str`, `billable_owner: str`, `carry: bool`,
    `consent_sha256: str` (NEW's consent text), `arming_sha256: str` (`arming.json`'s bytes when decided),
    `ports: tuple[int, ...]`.
  - `decide(project, state: Path, new: str, *, ask=input, add_ports=(), remove_ports=()) -> Decision | None`: before
    any lock. `None` when not armed or `new` is the armed Codespace. Primary checkout from `workspace.registry` (read
    only); account from `arming.account(os.environ)`, which refuses `GH_TOKEN`/`GITHUB_TOKEN`, so the GET below,
    made with `Codespaces._api_env()`, selects the same account (`DEVCONTAINER_GH_USER` or gh's active one). NEW's row
    from one read-only GET (`Codespaces(primary, state)._api('GET', 'user/codespaces/' + new, env)` and
    `_matches_repository`); never starts NEW. A missing billing owner raises `ValueError(f'{new} does not name a
    billing owner; check it in GitHub. Nothing moved.')`. OLD is the binding's Codespace. When `carries_over` fails it
    prints `arming.consent_text(primary.root.name, new, owner)` and reads one line: anything but `arm` raises
    `ValueError('Not moved: the confirmation was not "arm"; recovery stays on OLD.')`; EOF or Ctrl-C raises
    `ValueError('Move cancelled; nothing moved.')`. Ports: the saved ones adjusted by `--port`/`--no-port`.
  - `begin(state, decision, now) -> None`: under the caller's lock. `arming.json`'s sha256 must still equal
    `decision.arming_sha256`, else `ValueError('Recovery changed while you were deciding; nothing moved. Run the
    command again.')`. Writes `INTENT` with every `Decision` field and `at`.
  - `commit(primary, state, now) -> str`: under the caller's lock. Carry: `arming.move_consent(..., ports=list(ports))`
    when the ports differ from the saved ones, else without `ports`. Fresh: `arming.arm(...)` for NEW with the intent's
    account, owner and ports, after checking the recomputed consent text's sha256 equals the intent's. Removes
    `INTENT`; returns the line to print (the move line, or `Armed as ACCOUNT for NEW; ports …`).
  - `finish_pending(project, state) -> None`: takes `workspace-control.lock` itself. No intent: nothing. In order:
    `arming.json` already names NEW with the intent's `consent_sha256` (the consent write landed, the intent unlink
    did not; CHECK2 N3): remove the intent and write nothing, whatever the owner changed since. Else replays `commit`
    and prints its line only when the binding names the intent's NEW **and** `arming.json`'s sha256 equals the
    intent's `arming_sha256` (nothing changed it since the move began: no disarm, hold or port edit, CHECK F3).
    Binding names OLD: remove the intent, print `The move to NEW did not finish; recovery stays on OLD.` Binding names
    NEW but arming changed: remove the intent, print `The move to NEW was interrupted and recovery settings changed
    since; run task work:cs:armed to arm NEW.` Anything else: remove the intent.
- `run`: for `--codespace` and `--armed`, call `move.finish_pending(project, state)` first. With `--rebind` on an armed
  project: `decision = move.decide(...)`, then `workspace.remote(..., rebind=..., move=decision,
  armed=lambda primary: _resume_recovery(primary, state))`, keeping `before_herdr` when `--then-signin-all` is given.
  Pass `move=` only when there is a decision (existing tests mock `remote` with four arguments). `--armed --rebind`
  on an armed project takes this same path and never calls `_arm`. Not armed: unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_recovery_move.py`. Fixture: a project and state as
`tests/test_workspace_recovery.py` builds them, a binding naming `old-cs`, the Task 3 `self.armed` consent, and mocks
for `arming.account` ('me'), `Codespaces._api_env` (a fixture env), the NEW GET (owner 'me'),
`keeper.foreign_processes` (`[]`), `_prerequisites`, `CodespaceConnection`, `_verify_roots`,
`_remote_herdr`, `Codespaces.roots`, `scratch.start`, `_ready`, `configure_ssh`, `end_proxies_elsewhere`,
`open_remote`, `keeper.start`, `keeper.wait_ready`; record call order in one list. `setUp` also patches
`subprocess.run` and `subprocess.Popen` to raise `AssertionError('unmocked subprocess')`, so a missed seam fails here
instead of reaching `gh`, `ps` or SSH.

```python
def test_same_account_move_carries_consent_with_one_line(self):
    self.assertEqual(self.connect('--rebind', 'new-cs'), 0)
    self.assertEqual(self.order, ['intent', 'keeper.stop', 'retarget', 'registry', 'consent', 'keeper.start', 'herdr'])
    self.input.assert_not_called()
    self.assertEqual(arming.read(self.state)['consented_at'], 1000.0)
    self.assertEqual(self.out.getvalue().count('Recovery moved old-cs → new-cs; consent from '), 1)
    self.assertFalse((self.state / move.INTENT).exists())

def test_port_flags_reach_the_new_keeper(self):              # --port 8080: arming ports [3000, 8080], timestamps kept
def test_changed_owner_asks_and_arms_new(self):              # input 'arm': arming.arm for new-cs, then keeper.start
def test_decline_moves_nothing(self):                         # input 'no', and EOF, and Ctrl-C: exit 1; retarget, keeper.stop
                                                              # and start_codespace never called; arming, binding, registry bytes unchanged
def test_owner_changed_after_deciding_refuses_before_any_start(self):  # GET owner differs at _choose_pairing:
                                                              # start_codespace, evidence, keeper.stop, retarget never called
def test_arming_changed_after_deciding_refuses(self):         # arming.json rewritten between decide and begin
def test_crash_after_retarget_is_finished_by_plain_connect(self):
    # retarget raises after writing the binding: next plain connect (no --rebind) writes the PENDING registry,
    # commits consent without a prompt, and starts the keeper for new-cs
def test_crash_after_registry_before_consent_is_finished(self):  # no pending file, arming old: consent replayed once
def test_crash_after_consent_before_intent_unlink(self):      # arming names new-cs with the intent's digest: intent
                                                              # removed, arming bytes unchanged, no prompt; also with a
                                                              # later disarm of new-cs, which stays disarmed
def test_unarmed_rebind_without_a_registry_file(self):        # binding only, no workspace.json: rebind works, and a
                                                              # crash after retarget replays the pending registry
def test_crash_before_retarget_keeps_old(self):              # intent present, binding old-cs: intent removed, arming unchanged
def test_replay_after_disarm_does_not_rearm(self):            # crash after retarget, then work:disarm: plain connect leaves
                                                              # armed false, carry and fresh modes both
def test_armed_alias_rebind_takes_the_same_path(self):        # --armed --rebind: _arm and arming.arm never called
def test_unarmed_rebind_is_unchanged(self):                   # decide returns None; arming.account never called
def test_rebind_keeps_then_signin_all(self):                  # --rebind with --then-signin-all: sign-ins before Herdr
```

Rewrite `tests/test_project_sync_cli.py::test_plain_rebind_of_an_armed_project_is_refused_before_it_connects` as
`test_plain_rebind_of_an_armed_project_moves_recovery`: patch `move.finish_pending` and `move.decide` (returning a
`move.Decision` for `new-space`) and assert `workspace.remote` is called once with `rebind='new-space'` and that
decision as `move`.

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_recovery_move tests.test_project_sync_cli -v`
Expected: exit 1, an import error on `move`; the rewritten CLI test fails on today's refusal.

- [ ] **Step 3: Implement** as in Interfaces; update `README.md:108-109`.

- [ ] **Step 4: Run the gates**

Run: `task check && task test`
Expected: exit 0, with every `tests/test_workspace_recovery.py`, `tests/test_scratch_sync.py` and `ArmFlowTests`
test unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A devcontainer_setup README.md tests
git commit -m "Move recovery with a rebind of an armed project"
```

## Order and parallelism

Tasks 1 and 3 touch different files and run in parallel. Task 2 starts from Task 1's merge (it uses `power.stop`);
Task 4 from Task 3's. Tasks 2 and 4 both edit `run` in `__main__.py` and `README.md`, in different places; the lead
merges each into `feature/task-work-ab` after its own re-gate and resolves the overlap there.

## Live check (gate for the A+B release, not part of this plan's tasks)

On the live test project, with the backup runbook of the 2026-10-27 ruling: `task work:cs:stop` from a Mac shell with
no terminal; the same from an agent inside the Codespace, once with the Mac worker running and once with it stopped
(nothing stops); a same-account `work:cs -- --rebind` to a throwaway Codespace and back, checking that `arming.json`
keeps `consented_at` and NEW's keeper heartbeats.
