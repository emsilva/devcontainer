# `task work` stage A: smarter commands. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `task work:cs:stop` runs headless on the Mac and from inside the Codespace, and `task work:cs -- --rebind NEW`
moves recovery of an armed project, carrying consent when the account and billing owner are unchanged.

**Architecture:** The Mac's stop moves into `project_sync/power.py`, ahead of the terminal gate. Inside a Codespace the
same command drops a stop request that the Mac worker's link watcher claims, as it claims link requests, and answers
with a result file. A rebind of an armed project is decided (account, NEW's billing owner, a typed `arm` only when they
changed) before `workspace.remote` takes its lock, written as a move intent before the retarget, and committed by a
new `arming.move_consent` after it; the next `work:cs` finishes a move a crash interrupted.

**Tech Stack:** Python 3 standard library (the package is vendored into projects and run as `python3 -I -B`), go-task,
`unittest`.

**Spec:** `docs/superpowers/specs/2026-11-02-task-work-design.md`, section "A. Smarter commands", its Anti-goals,
Constraints and "Tests the plan must include" (A list). Builder check: `tmp/codespace-lifecycle/task-work/builder-check/CHECK.md`
(C3, C6, C7, Q8). Read both before Task 1.

## Global Constraints

- Gates: `task check` and `task test` (`python3 -m unittest discover -s tests -v`), both exit 0, at your commit.
- Branches: every builder branch is cut from `feature/task-work-ab` (cut from `main` at `c6ac08a`) and merges back into
  it, never into `main`. Stage A ships together with stage B after a live check of the stop and the move (spec call 1).
- Commit messages: plain imperative sentence case, one concern per commit, no AI attribution of any kind.
- `_consent_on_file` (`devcontainer_setup/project_sync/__main__.py:62-84`) keeps its exact-Codespace check for ordinary
  connects. A move never calls `arming.arm()` to carry consent (it resets hold, owner stop and timestamps).
- `arming.json` may gain keys (CHECK C7). No new key in any file whose loader rejects unknown keys (the user
  `config.json`). New files are written with `state.atomic_write` (private, atomic).
- Restart (`work:cs:restart`) and a first arm stay terminal-only.
- The move's printed line, exactly: `Recovery moved OLD → NEW; consent from DATE kept (ACCOUNT, billing OWNER).`
  DATE is the local date of `consented_at` as `YYYY-MM-DD`.
- Stdlib only; no new dependency. Everything a Codespace runs stays `python3 -I -B` compatible.
- No client, organization or Codespace names in code, tests, docs or commit messages (the repository is public).

## Review Focus

1. **A stop request left behind never stops the Codespace later.** A request older than 60 seconds is discarded by the
   watcher, never acted on (Task 2 test `test_stale_stop_request_is_discarded`).
2. **A sign-in-only watcher leaves stop requests alone** (`SIGNINS_ONLY`), and a stop request never lands in
   `requests/`, where that watcher discards files it cannot parse (Task 2 test
   `test_signins_only_watcher_leaves_stop_requests`).
3. **A stop request for a Codespace that is not the paired one is refused** (an old worker after a rebind): nothing is
   recorded or posted (Task 2 test `test_stop_request_for_another_codespace_is_refused`).
4. **Ctrl-C while the Codespace waits removes an unclaimed request**, and says nothing stops (Task 2 test
   `test_cancel_removes_unclaimed_request`).
5. **NEW never inherits OLD's owner stop or pending restore.** Otherwise NEW's keeper reads its next stop as "stopped
   by you" and never restarts it (spec Anti-goals, CHECK C1) (Task 3 test `test_move_retires_owner_stop_and_pending_restore`).

---

### Task 1: Stop on the Mac runs headless

**Files:**
- Create: `devcontainer_setup/project_sync/power.py`
- Modify: `devcontainer_setup/project_sync/__main__.py` (`_owner_power`, `run`)
- Modify: `devcontainer_setup/root_tasks.py:9` (`INTERACTIVE`), `templates/continuity-tasks.yml:41-45`, `Taskfile.yml:39-43`
- Modify: `README.md`, `docs/project-continuity.md`: any line saying the stop needs a terminal
- Test: `tests/test_project_sync_cli.py`, `tests/test_root_tasks.py:49-70`

**Interfaces:**
- Produces: `power.Stopped` (frozen dataclass: `codespace: str`, `armed: bool`, `api`, `stopped_at: float`,
  `message: str`) and `power.stop(state: Path, *, api=None, now=time.time) -> Stopped`. It is today's
  `_owner_power` up to and including the POST and its status handling: refuses with `CODESPACES=true`, refuses with
  `GH_TOKEN`/`GITHUB_TOKEN` set, records the owner stop under `workspace-control.lock`, posts, raises `ValueError` with
  today's texts. `message` is today's "Stopping …" line. It prints nothing.
- `_owner_power(project, state, *, restart, api=None, now=..., sleep=...)` keeps its signature; it calls `power.stop`,
  prints `message` when `restart` is false, and continues with the restart half otherwise.

- [ ] **Step 1: Write the failing tests**

In `tests/test_project_sync_cli.py`:

```python
def test_codespace_stop_runs_without_a_terminal(self):
    result = self.run_cli('work', '--codespace-stop')
    self.assertEqual(result.returncode, 1)
    self.assertNotIn('require a terminal', result.stderr)
    self.assertIn('Pair a Codespace first', result.stderr)

def test_codespace_restart_still_requires_a_terminal(self):
    result = self.run_cli('work', '--codespace-restart')
    self.assertEqual(result.returncode, 1)
    self.assertIn('Work entrypoints require a terminal', result.stderr)
```

In `tests/test_root_tasks.py::test_new_work_tasks_render_and_alias`, move `work:cs:stop` from the interactive tuple to
the non-interactive one: `('work:cs:armed', 'work:cs:restart')` interactive; `('work:disarm', 'work:hold',
'work:logs', 'work:cs:stop')` not.

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_project_sync_cli tests.test_root_tasks -v 2>&1 | tail -20`
Expected: the two new CLI tests and `test_new_work_tasks_render_and_alias` FAIL (terminal refusal; `interactive: true`
still rendered).

- [ ] **Step 3: Implement**

Create `power.py` with `Stopped` and `stop` as in Interfaces, moving the code out of `_owner_power`. In `run`, handle
`args.codespace_stop` before the `isatty` gate; `args.codespace_restart` stays after it. Drop `interactive: true` from
`work:cs:stop` in the template and the root `Taskfile.yml`, and `'work:cs:stop'` from `INTERACTIVE`. Fix the README and
continuity-doc lines that say otherwise.

- [ ] **Step 4: Run the gates**

Run: `task check && task test`
Expected: exit 0; the existing `_owner_power` tests (`test_codespace_stop_records_the_owner_stop_before_its_post` and
the others calling `cli._owner_power`) pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A devcontainer_setup templates Taskfile.yml README.md docs tests
git commit -m "Let task work:cs:stop run without a terminal on the Mac"
```

### Task 2: Stop from inside the Codespace

**Files:**
- Modify: `devcontainer_setup/project_sync/power.py`
- Modify: `devcontainer_setup/project_sync/links.py` (`REMOTE_WATCHER`, `Watcher._serve`, a new `Watcher._stop_request`)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`run`)
- Test: `tests/test_power.py` (new), `tests/test_links_watcher.py`

**Interfaces:**
- Consumes: `power.stop(state)` (Task 1).
- Produces, all in `power.py`:
  - `STOPS = '.local/state/devcontainer-links/stops'` (relative to the Codespace home), `WINDOW = 60` seconds,
    `LISTENING_AGE = 15` seconds (the age `signins.ready()` accepts for `mac-listening`).
  - `request_from_codespace(*, home: Path, env: Mapping, now=time.time, sleep=time.sleep, wait: float = WINDOW) -> int`
  - `answer_text(result: power.Stopped | ValueError) -> dict` giving `{'ok': bool, 'message': str}`.
- The request file: `stops/<time_ns>-<pid>-<8 hex>.json`, mode 0600, written to a temporary name and renamed:
  `{"schema": 1, "kind": "stop", "id": "<stem>", "created_at": <float>, "codespace": "<CODESPACE_NAME>"}`.
  The answer: `stops/<id>.result`, mode 0600, written atomically by the Mac: `{"ok": …, "message": …}`.

Behavior:
- **Codespace side** (`request_from_codespace`): `mac-listening` missing or older than `LISTENING_AGE` → print
  `No Mac is connected to this Codespace, so nothing stops. Stop it from your Mac with task work:cs:stop; otherwise it
  stops after its idle timeout.` and return 1, writing nothing. Otherwise write the request and poll every 0.5 s for the
  answer until `wait` elapses: print its message prefixed `Your Mac recorded this as your stop. ` when `ok`, return 0
  if `ok` else 1, and remove the answer file. At the deadline, `unlink` the request: if that succeeds, print
  `Your Mac did not take the stop request within 60 seconds, so nothing stops. Check task work:status on your Mac.`;
  if the file is already gone, print `Your Mac took the stop request but has not answered; check task work:status on
  your Mac.` Return 1 either way. `KeyboardInterrupt` unlinks an unclaimed request, prints `Cancelled; nothing stops.`
  (or, when already claimed, `Cancelled, but your Mac already took the stop request.`) and returns 130.
- **Watcher, remote half** (`REMOTE_WATCHER`): creates `stops/` beside `requests/` and `claimed/` (0700, same checks).
  Unless `SIGNINS_ONLY`, each `stops/*.json` is claimed by rename into `claimed/`, read with the same size and
  `O_NOFOLLOW` rules, and printed as one JSON line only when its shape matches the request above and
  `0 <= time.time() - created_at <= 60`; otherwise one stderr line, `Skipped stale or invalid stop request`.
  `.result` files are never touched by the watcher.
- **Watcher, Mac half**: a stdout line whose JSON has `kind == 'stop'` goes to `Watcher._stop_request(value)`, never
  to `parse_request`. It refuses (logged, no stop) unless `value['codespace']` equals both the connection's Codespace
  and the binding's (`read_json(state / scratch.CONFIG)['codespace']`). It runs at most one stop at a time, on its own
  daemon thread so the stream loop never waits on `workspace-control.lock`; a second request while one runs is
  answered `{"ok": false, "message": "A stop is already in progress."}`. The thread calls `power.stop(self.state)`,
  logs one line to the worker log (`Stop requested from the Codespace: <message>`, never a URL or token), and writes
  the answer over `self.connection.python(...)` with a 10 s timeout; a failed write is logged, not raised.
- **CLI**: in `run`, before the terminal gate, `args.codespace_stop` with `CODESPACES=true` calls
  `request_from_codespace(home=Path.home(), env=os.environ)` and returns its code; it never reaches `power.stop`, whose
  `GITHUB_TOKEN` refusal a Codespace login shell would trip (CHECK C3).

- [ ] **Step 1: Write the failing tests**

`tests/test_power.py` (fake home in a `TemporaryDirectory`, a fake clock, `sleep` that advances it):

```python
def test_no_mac_listening_writes_nothing_and_fails(self):
    self.assertEqual(power.request_from_codespace(home=self.home, env=self.env, now=self.now, sleep=self.sleep), 1)
    self.assertEqual(list(self.stops.glob('*')), [])
    self.assertIn('No Mac is connected to this Codespace, so nothing stops.', self.out.getvalue())

def test_answer_is_printed_and_removed(self):          # a sleep hook writes <id>.result {"ok": true, ...}
    self.assertEqual(self.request(), 0)
    self.assertIn('Your Mac recorded this as your stop. Stopping', self.out.getvalue())
    self.assertEqual(list(self.stops.glob('*')), [])

def test_unclaimed_request_is_removed_at_the_deadline(self):
    self.assertEqual(self.request(), 1)
    self.assertEqual(list(self.stops.glob('*.json')), [])
    self.assertIn('did not take the stop request within 60 seconds, so nothing stops', self.out.getvalue())

def test_claimed_without_answer_says_so(self):          # a sleep hook moves the request away
    self.assertEqual(self.request(), 1)
    self.assertIn('took the stop request but has not answered', self.out.getvalue())

def test_cancel_removes_unclaimed_request(self):         # sleep raises KeyboardInterrupt
    self.assertEqual(self.request(), 130)
    self.assertEqual(list(self.stops.glob('*.json')), [])
    self.assertIn('Cancelled; nothing stops.', self.out.getvalue())

def test_request_file_shape(self):                       # captured before the answer
    value = json.loads(captured.read_text())
    self.assertEqual(set(value), {'schema', 'kind', 'id', 'created_at', 'codespace'})
    self.assertEqual((value['kind'], value['codespace'], captured.stat().st_mode & 0o777), ('stop', 'cs-name', 0o600))
```

`tests/test_links_watcher.py`, using the existing `REMOTE_WATCHER` harness at lines 43-45:

```python
def test_watcher_streams_a_fresh_stop_request(self):      # line has kind 'stop'; file gone from stops/
def test_stale_stop_request_is_discarded(self):           # created_at 61 s ago: no stdout line, file gone
def test_signins_only_watcher_leaves_stop_requests(self): # SIGNINS_ONLY: file still in stops/, no line
def test_stop_request_goes_to_power_not_links(self):      # _serve: handle() never; with lifecycle.LifecycleAPI
                                                          # faked, owner_stop_at is written before post_stop
def test_stop_request_for_another_codespace_is_refused(self):  # binding names another: power.stop not called
def test_second_stop_while_one_runs_is_answered_busy(self):
```

In `tests/test_project_sync_cli.py`:

```python
def test_codespace_stop_inside_a_codespace_sends_a_request(self):
    # CODESPACES=true and GITHUB_TOKEN set: request_from_codespace called once, power.stop never
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_power tests.test_links_watcher tests.test_project_sync_cli -v 2>&1 | tail -30`
Expected: the new tests FAIL or ERROR (`request_from_codespace` missing; no `stops/` handling).

- [ ] **Step 3: Implement** as Behavior says.

- [ ] **Step 4: Run the gates**

Run: `task check && task test`
Expected: exit 0, including every existing `tests/test_links_watcher.py` test unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A devcontainer_setup tests
git commit -m "Let an agent inside the Codespace stop it through the Mac"
```

### Task 3: Consent move in arming

**Files:**
- Modify: `devcontainer_setup/project_sync/arming.py`
- Test: `tests/test_arming.py`

**Interfaces:**
- Produces:
  - `carries_over(value: dict, *, project_name: str, gh_user: str, old: str, new_owner: str) -> bool`: true only when
    `value['armed']`, `value['codespace'] == old`, `value['gh_user'] == gh_user`, `value['billable_owner'] ==
    new_owner`, and `value['consent_sha256'] == sha256(consent_text(project_name, old, value['billable_owner']))`.
    Pure; no I/O.
  - `move_consent(state: Path, value: dict, *, project_name: str, new: str, now: float) -> str`: raises `ValueError`
    unless `carries_over(...)` holds for `value` with its own account and owner. Archives `value` to
    `state / 'arming-history' / f'{time.time_ns()}.json'`, then writes `value` with `codespace=new`,
    `consent_sha256` for `consent_text(project_name, new, owner)`, `moved_from=old`, `moved_at=now`, and without
    `owner_stop_at` and `pending_restore`. Keeps `armed_at`, `consented_at`, `gh_user`, `billable_owner`, `ports`,
    `hold_until`. Returns the move line (Global Constraints). The caller holds `workspace-control.lock`; this function
    takes no lock. The restart ledger is a separate file and is not touched.

- [ ] **Step 1: Write the failing tests** in `tests/test_arming.py`:

```python
def test_same_account_and_owner_carries_over(self):
    self.assertTrue(arming.carries_over(self.armed, project_name='proj', gh_user='me', old='old-cs', new_owner='me'))

def test_changed_owner_account_or_policy_does_not_carry(self):
    for change in ({'new_owner': 'org'}, {'gh_user': 'other'}):
        self.assertFalse(arming.carries_over(self.armed, **dict(self.ok, **change)))
    self.assertFalse(arming.carries_over(dict(self.armed, consent_sha256='0' * 64), **self.ok))

def test_move_keeps_timestamps_hold_and_ports(self):
    line = arming.move_consent(self.state, self.armed, project_name='proj', new='new-cs', now=2000.0)
    moved = arming.read(self.state)
    for key in ('armed_at', 'consented_at', 'ports', 'hold_until', 'gh_user', 'billable_owner'):
        self.assertEqual(moved[key], self.armed[key])
    self.assertEqual((moved['codespace'], moved['moved_from'], moved['moved_at']), ('new-cs', 'old-cs', 2000.0))
    self.assertEqual(moved['consent_sha256'],
                     hashlib.sha256(arming.consent_text('proj', 'new-cs', 'me').encode()).hexdigest())
    self.assertRegex(line, r'^Recovery moved old-cs → new-cs; consent from \d{4}-\d{2}-\d{2} kept \(me, billing me\)\.$')

def test_move_retires_owner_stop_and_pending_restore(self):
    value = dict(self.armed, owner_stop_at=1500.0, pending_restore={'rid': 'r1'})
    arming.move_consent(self.state, value, project_name='proj', new='new-cs', now=2000.0)
    self.assertNotIn('owner_stop_at', arming.read(self.state))
    self.assertNotIn('pending_restore', arming.read(self.state))
    archived = [json.loads(p.read_text()) for p in (self.state / 'arming-history').glob('*.json')]
    self.assertEqual([a['owner_stop_at'] for a in archived], [1500.0])

def test_move_refuses_what_does_not_carry(self):          # owner changed in value: ValueError, file unchanged
```

In `tests/test_project_sync_cli.py`:

```python
def test_ordinary_connect_still_refuses_a_mismatched_target(self):
    # arming names old-cs, Codespaces.space() returns new-cs, account mocked: 'the Codespace changed'
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_arming -v 2>&1 | tail -20`
Expected: ERROR, `module ... has no attribute 'carries_over'`.

- [ ] **Step 3: Implement** `carries_over` and `move_consent` as in Interfaces.

- [ ] **Step 4: Run the gates**

Run: `task check && task test`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add devcontainer_setup/project_sync/arming.py tests/test_arming.py tests/test_project_sync_cli.py
git commit -m "Carry recovery consent to a new Codespace when account and billing owner match"
```

### Task 4: A rebind moves recovery

**Files:**
- Create: `devcontainer_setup/project_sync/move.py`
- Modify: `devcontainer_setup/project_sync/workspace.py` (`_choose_pairing`, `remote`)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`run`: remove the armed-rebind refusal at lines 363-368)
- Modify: `README.md:108-109` (one rebind command for both)
- Test: `tests/test_recovery_move.py` (new), `tests/test_project_sync_cli.py:741-753`

**Interfaces:**
- Consumes: `arming.carries_over`, `arming.move_consent` (Task 3); `keeper.stop`, `keeper.start`; `scratch.retarget`.
- Produces, in `move.py`:
  - `INTENT = 'recovery-move.json'`
  - `Decision` (frozen dataclass): `old: str`, `new: str`, `gh_user: str`, `billable_owner: str`, `carry: bool`,
    `consent_sha256: str` (of NEW's consent text), `arming_sha256: str` (of `arming.json`'s bytes when decided),
    `ports: tuple[int, ...]`.
  - `decide(project, state: Path, new: str, *, ask=input, add_ports=(), remove_ports=()) -> Decision | None`: runs
    before any lock. `None` when the project is not armed or `new` is already the armed Codespace. Resolves the
    primary checkout from `workspace.registry` (read only), the account with `arming.account(os.environ)`, and NEW's
    row with one read-only GET (`Codespaces(primary, state)._api('GET', 'user/codespaces/' + new, env)` plus
    `_matches_repository`); never starts NEW. OLD is the binding's Codespace. When `carries_over` fails it prints
    `arming.consent_text(primary.root.name, new, owner)` and reads one line: anything but `arm` raises
    `ValueError('Not moved: the confirmation was not "arm"; recovery stays on OLD.')`, EOF or Ctrl-C raises
    `ValueError('Move cancelled; nothing moved.')`. Ports: the saved ones, adjusted by `--port`/`--no-port`.
  - `begin(state: Path, decision: Decision, now: float) -> None`: under the caller's lock. Raises
    `ValueError('Recovery changed while you were deciding; nothing moved. Run the command again.')` when
    `arming.json`'s sha256 differs from `decision.arming_sha256`; otherwise writes `INTENT` with every `Decision`
    field and `at`.
  - `commit(primary, state: Path, now: float) -> str`: under the caller's lock. Carry: `arming.move_consent`. Fresh:
    `arming.arm(...)` for NEW with the intent's account, owner and ports, after checking that the recomputed consent
    text's sha256 equals the intent's. Removes `INTENT`; returns the line to print (the move line, or
    `Armed as ACCOUNT for NEW; ports …`).
  - `finish_pending(project, state: Path) -> None`: takes `workspace-control.lock` itself. No intent: nothing. Binding
    names the intent's NEW and `arming.json` its OLD: `commit` and print its line. Binding names OLD: remove the
    intent and print `The move to NEW did not finish; recovery stays on OLD.` Anything else: remove the intent.
- `workspace._choose_pairing(project, state, value, rebind=None, seen=None)`: when `seen` is a dict, stores
  `seen['billable_owner']` from the row it already reads. Return value unchanged (24 test call sites mock it).
- `workspace.remote(project, state, armed=None, rebind=None, before_herdr=None, move=None)`: with a `Decision`,
  after `_choose_pairing` it refuses (`ValueError`, nothing moved) unless the chosen name is `move.new` and
  `seen['billable_owner'] == move.billable_owner`. After the root verification and before `scratch.retarget`, in this
  order (spec: intent, stop OLD's keeper, retarget, consent, NEW's keeper, Herdr): `move.begin`, `keeper.stop(state)`,
  `scratch.retarget(...)`, `move.commit(...)` and print its line. NEW's keeper is started by the `armed` callback
  (`_resume_recovery`) after the lock is released, as today.
- `run`: for `--codespace` and `--armed`, call `move.finish_pending(project, state)` first; with `--rebind` on an
  armed project, `decision = move.decide(...)` and `workspace.remote(..., move=decision,
  armed=lambda primary: _resume_recovery(primary, state))`. `--armed --rebind` on an armed project takes this same
  path and never calls `_arm`. Not armed: unchanged (`--armed --rebind` still connects, then asks the first typed
  `arm`).

- [ ] **Step 1: Write the failing tests** in `tests/test_recovery_move.py` (mock `arming.account`, the NEW GET,
`_choose_pairing`, `CodespaceConnection`, `_verify_roots`, `_remote_herdr`, `scratch.start`, `_ready`,
`configure_ssh`, `end_proxies_elsewhere`, `open_remote`; record call order in one list):

```python
def test_same_account_move_carries_consent_with_one_line(self):
    self.assertEqual(self.connect('--rebind', 'new-cs'), 0)
    self.assertEqual(self.order, ['intent', 'keeper.stop', 'retarget', 'consent', 'keeper.start', 'herdr'])
    self.input.assert_not_called()
    self.assertEqual(arming.read(self.state)['consented_at'], self.armed['consented_at'])
    self.assertEqual(self.out.getvalue().count('Recovery moved old-cs → new-cs; consent from '), 1)
    self.assertFalse((self.state / move.INTENT).exists())

def test_changed_owner_asks_and_arms_new(self):            # input returns 'arm': arming.arm for new-cs, then keeper.start

def test_decline_moves_nothing(self):
    before = (self.state / 'arming.json').read_bytes()
    self.input.return_value = 'no'
    self.assertEqual(self.connect('--rebind', 'new-cs'), 1)
    self.retarget.assert_not_called(); self.keeper_stop.assert_not_called()
    self.assertEqual((self.state / 'arming.json').read_bytes(), before)

def test_crash_after_retarget_is_finished_by_plain_connect(self):
    # intent present, binding names new-cs, arming names old-cs; plain connect, no --rebind:
    # no prompt, arming names new-cs, intent removed, keeper.start for new-cs

def test_crash_before_retarget_keeps_old(self):
    # intent present, binding names old-cs: intent removed, arming unchanged, line says recovery stays on old-cs

def test_armed_alias_rebind_takes_the_same_path(self):     # --armed --rebind: no _arm, no arming.arm call

def test_owner_changed_after_deciding_refuses(self):        # seen owner differs: retarget not called

def test_arming_changed_after_deciding_refuses(self):       # arming.json rewritten between decide and begin

def test_unarmed_rebind_is_unchanged(self):                  # decide returns None; arming.account never called
```

Rewrite `tests/test_project_sync_cli.py::test_plain_rebind_of_an_armed_project_is_refused_before_it_connects` as
`test_plain_rebind_of_an_armed_project_moves_recovery`: `workspace.remote` is called once with `rebind='new-space'`
and a `move` that is a `move.Decision` for `new-space`.

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_recovery_move tests.test_project_sync_cli -v 2>&1 | tail -30`
Expected: ERROR on `import ... move`; the rewritten CLI test FAILS on today's refusal.

- [ ] **Step 3: Implement** as in Interfaces. Update `README.md:108-109` to the one command
`task work:cs -- --rebind <codespace>`, which now moves recovery too.

- [ ] **Step 4: Run the gates**

Run: `task check && task test`
Expected: exit 0, with every `tests/test_workspace_recovery.py` and `tests/test_scratch_sync.py` test unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A devcontainer_setup README.md tests
git commit -m "Move recovery with a rebind of an armed project"
```

## Order and parallelism

Tasks 1 and 3 touch different files and run in parallel. Task 2 starts from Task 1's merge (it uses `power.stop`);
Task 4 from Task 3's. The lead merges each into `feature/task-work-ab` after its own re-gate.

## Live check (gate for the A+B release, not part of this plan's tasks)

On the live test project, with the backup runbook of the 2026-10-27 ruling: `task work:cs:stop` from a Mac shell with
no terminal; the same from an agent inside the Codespace (and once with the Mac worker stopped: nothing stops); a
same-account `work:cs -- --rebind` to a throwaway Codespace and back, checking `arming.json` keeps `consented_at` and
NEW's keeper heartbeats.
