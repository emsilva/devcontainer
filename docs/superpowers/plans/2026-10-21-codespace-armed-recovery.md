# Codespace Recovery After the 12-Hour Stop: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Armed projects keep their Codespace up, come back by themselves after GitHub's 12-hour stop, and tell their lead to resume. Herdr no longer sticks on a dead SSH proxy, and setup creates Codespaces on main.

**Architecture:**
- One detached **keeper** process per armed project, launched and adopted like today's sync worker. It reads the Codespace once a minute over direct HTTPS and holds a K4 keep-alive child that carries the port forwards. After a classified 12-hour stop, and within the budget, it runs one shared start routine, then restores the Mac side and prompts the lead through Herdr.
- The Herdr proxy gains its own liveness check.
- Setup commits in a temporary worktree cut from `origin/<default>`, merges a PR, and creates the Codespace on the default branch.

**Tech Stack:** Python 3.11+ standard library only. `unittest`, with fake `gh`, `herdr`, `osascript` and `ps` executables on PATH. go-task. gh 2.102.0. Herdr CLI.

**Spec:** `docs/superpowers/specs/2026-10-21-codespace-armed-recovery-design.md`. Read it with this plan; section numbers below refer to it.

## Global Constraints

- **Language and tests:** Python standard library only. Tests use `unittest`, are offline, and run with `python3 -m unittest discover -s tests -v` (`task test`).
- **Gates:** `task check` and `task test` both pass before every commit.
- **gh:** stays pinned to `SUPPORTED_GH = '2.102.0'` (`project_sync/transport.py`). Never call `gh auth switch`.
- **Starting a Codespace:**
  - Only the keeper's start routine (Task 2 `lifecycle.start`) may POST start in the background.
  - The worker, the K4 child and `herdr_stdio` stay GET-only (`ReadOnlyAPI`).
  - The keeper's POST goes over direct HTTPS, never through gh.
- **Secrets:**
  - Tokens are read per call and never written to disk, logs, argv or exception text.
  - Logs and ledger lines hold times, states and HTTP status codes, never response bodies.
  - Consent is stored as sha256 of its text plus a time.
- **State files** live in the project's private state dir (`state_dir(project)`, `~/.local/state/devcontainer-setup/projects/<id>/`). Write them only with `state.atomic_write` and read them with `state.read_json`.
- **Times:**
  - Stored as UTC epoch floats.
  - Shown to people in both the Mac's local zone and UTC, for example `22:29 (01:29Z)`.
  - Parsed from the API with both `Z` and `±HH:MM` offsets.
- **Fixed values** from the spec:
  - lifetime 12 h; classification window ±15 min; 3 down readings;
  - 2 restarts per rolling 24 h, at least 30 min apart;
  - wait for Shutdown up to 20 min; re-POST after 2 min still in Shutdown; at most 3 POSTs per rid; wait for Available up to 45 min;
  - wait for Herdr up to 10 min; resume delivery proof within 2 min;
  - keeper GET every 60 s; proxy GET every 30 s, 3 down reads to exit;
  - keeper heartbeat stale after 5 min; merge waits up to 15 min for checks.
- **Resume sentence**, verbatim: `Codespace restarted, we are back. You need to bring the services that you require up and continue with the work`.
- **Commits:** imperative sentence case (`AGENTS.md`), no AI attribution, one concern per commit. Code reads like the surrounding code: terse comments, `verb_object` names, `ValueError` with actionable fixed text.

## Review Focus

1. **The Mac sleeps or is offline across the stop.** Readings fail, or come back hours later. Expected: a stop whose possible interval `[last_up_at, first_down_at]` overlaps the 12-hour window is LIFETIME. One that lies wholly outside the window is OTHER. Failed reads never count. Pinned in Task 4 (`test_late_detection_overlapping_window_is_lifetime`, `test_gap_outside_window_is_other`).
2. **The keeper dies between the ledger `intent` line and the POST result.** Expected: after a restart the keeper reads the state first. If the Codespace is already Queued, Starting or Available, it does not POST again. The interrupted rid still counts toward the budget. Pinned in Task 8 (`test_interrupted_intent_is_not_reposted`).
3. **Herdr hangs**, as with the stale master, **or prints non-JSON.** Expected: every herdr call is bounded at 60 s and maps to "not reachable" or "no lead", and the keeper loop keeps its heartbeat. Pinned in Task 8 (`test_hanging_herdr_is_bounded_and_not_fatal`).
4. **API timestamps arrive as `Z` or as `-03:00`.** Expected: both parse to the same epoch. Pinned in Task 2 (`test_parse_time_accepts_z_and_offsets`).
5. **`work:cs:armed` runs twice at once, or after a generator upgrade.** Expected: never two keepers. A keeper with a different `runtime_id` is stopped and replaced; the same one is adopted. Pinned in Task 5 (`test_concurrent_arms_leave_one_keeper`, `test_new_runtime_replaces_old_keeper`).

---

## File Structure

| File | Responsibility |
|---|---|
| `devcontainer_setup/project_sync/transport.py` (modify) | `codespace_state()`; liveness loop in `herdr_stdio` |
| `devcontainer_setup/project_sync/lifecycle.py` (create) | Pure lifecycle logic: `Reading`, `parse_time`, `classify`, `gate`, `Ledger`, `LifecycleAPI`, the shared `start()` routine |
| `devcontainer_setup/project_sync/notify.py` (create) | `notify()` through osascript, never raising |
| `devcontainer_setup/project_sync/arming.py` (create) | `arming.json`, consent, account, ports, hold, owner stop |
| `devcontainer_setup/project_sync/keepalive.py` (create) | The K4 child: argv, bind check, respawn |
| `devcontainer_setup/project_sync/restore.py` (create) | Stale-proxy cleanup, the `Herdr` adapter, lead discovery, resume text and delivery |
| `devcontainer_setup/project_sync/keeper.py` (create) | Keeper process start/adopt/stop/status, the `Keeper` core loop |
| `devcontainer_setup/project_sync/state.py` (modify) | `owned_process()`, shared by worker and keeper |
| `devcontainer_setup/project_sync/scratch.py` (modify) | Worker: stop notification for unarmed projects, keeper watch |
| `devcontainer_setup/project_sync/replication.py` (modify) | #48 message classification |
| `devcontainer_setup/project_sync/codespaces.py` (modify) | `start_codespace` delegates to `lifecycle.start` |
| `devcontainer_setup/project_sync/workspace.py` (modify) | `remote(..., armed=False)`, the status Recovery block |
| `devcontainer_setup/project_sync/__main__.py` (modify) | New `work` actions |
| `templates/continuity-tasks.yml`, `devcontainer_setup/root_tasks.py`, `Taskfile.yml` (modify) | New tasks |
| `devcontainer_setup/git_ops.py`, `devcontainer_setup/__main__.py`, `devcontainer_setup/codespaces.py` (modify) | Setup on main (#47) |
| `README.md`, `docs/project-continuity.md` (modify) | Who can start a Codespace, the new commands |

Tests: one new file per new module (`tests/test_lifecycle.py`, `tests/test_notify.py`, `tests/test_arming.py`, `tests/test_keepalive.py`, `tests/test_restore.py`, `tests/test_keeper.py`). Existing files gain cases (`tests/test_ssh_proxy.py`, `tests/test_scratch_sync.py`, `tests/test_replication.py`, `tests/test_workspace_codespaces.py`, `tests/test_project_sync_cli.py`, `tests/test_root_tasks.py`, `tests/test_continuity_integration.py`, `tests/test_git_ops.py`, `tests/test_wizard.py`, `tests/test_codespaces.py`).

Task order:
- Tasks 1, 9 and 10 are independent of the rest.
- Tasks 2→3→4→5→(6, 7)→8 are sequential.

---

### Task 1: Herdr proxy liveness (#46)

**Files:**
- Modify: `devcontainer_setup/project_sync/transport.py` (add near `_upstream`; change `herdr_stdio`, currently lines 194-228)
- Test: `tests/test_ssh_proxy.py`

**Interfaces:**
- Produces:
  - `transport.codespace_state(name: str, token: str, *, upstream=None) -> str | None`. One GET of `/user/codespaces/<name>`. Returns `state` on 200 with a matching `name`, else `None`. Never raises for HTTP, JSON or network errors.
  - `transport.LIVENESS_INTERVAL = 30`
  - `transport.LIVENESS_DOWN = 3`

- [ ] **Step 1: Write the failing tests** in `SSHProxyTests`. They call `transport.herdr_stdio('fixture', self.state)` in-process, with:
  - `mock.patch.dict(os.environ, self.env, clear=True)`;
  - `mock.patch.object(transport, 'LIVENESS_INTERVAL', 0.05)`;
  - `mock.patch.object(transport, '_upstream', fake)`, where `fake(url, token)` returns the next item of a list as `(200, json bytes)`, or `(500, b'{}')` for an `'error'` item;
  - a fake gh in a new `TEST_MODE=hang` that writes `child.json` and sleeps forever without reading stdin.

```python
def test_proxy_exits_after_three_consecutive_down_reads(self):
    # reads: Available, ShuttingDown, ShuttingDown, Shutdown
    code = transport.herdr_stdio('fixture', self.state)          # returns, does not hang
    self.assertNotEqual(code, 0)
    child = json.loads((self.state / 'child.json').read_text())
    with self.assertRaises(ProcessLookupError): os.kill(child['pid'], 0)
    self.assertEqual(len(reads_made), 4)

def test_failed_read_resets_the_down_count(self):
    # reads: Shutdown, 'error', Shutdown, Shutdown, Shutdown -> exits only after the 5th read
    self.assertEqual(len(reads_made), 5)

def test_available_reads_never_end_a_live_proxy(self):
    # 20 Available reads, then the fake gh exits by itself (TEST_MODE=hang with a 1 s limit)
    # -> herdr_stdio returns gh's own exit code, not a liveness exit
```

  Also assert that `codespace_state` returns `None` for a 200 whose `name` differs, and for `'error'`.

- [ ] **Step 2: Run them and confirm they fail.**
  Run: `python3 -m unittest tests.test_ssh_proxy -v`
  Expected: FAIL. `codespace_state` is missing, and `herdr_stdio` waits on gh forever. Give each test a `signal.alarm(10)` guard, so a hang fails instead of blocking.

- [ ] **Step 3: Implement.**
  - `codespace_state` reuses `_upstream`.
  - `herdr_stdio` replaces `child.wait()` with a loop: `child.wait(timeout=LIVENESS_INTERVAL)`; on timeout, read the state. A non-`None` state other than `Available` increments a counter; `Available` or `None` resets it. At `LIVENESS_DOWN`, `_terminate(child)` and return 1.
  - The token is the one `herdr_stdio` already resolves (`github_token()`).
  - Keep the existing `finally` reaping and the signal handling.

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run: `python3 -m unittest tests.test_ssh_proxy -v`
  Expected: all PASS, including the existing byte-stream and signal tests.

- [ ] **Step 5: Gates and commit.**
  Run `task check && task test` (both exit 0).
  `git commit -m "End the Herdr SSH proxy when its Codespace stops"`, with the body `Fixes #46.`

---

### Task 2: Lifecycle core and the shared start routine

**Files:**
- Create: `devcontainer_setup/project_sync/lifecycle.py`
- Modify: `devcontainer_setup/project_sync/codespaces.py:127-157` (`start_codespace` delegates)
- Test: `tests/test_lifecycle.py`, `tests/test_workspace_codespaces.py`

**Interfaces:**
- Produces:

```python
LIFETIME = 12 * 3600; WINDOW = 15 * 60; DOWN_READINGS = 3
MAX_RESTARTS = 2; RESTART_PERIOD = 24 * 3600; MIN_GAP = 30 * 60
SHUTDOWN_WAIT = 20 * 60; REPOST_AFTER = 120; MAX_POSTS = 3; AVAILABLE_WAIT = 45 * 60

@dataclass(frozen=True)
class Reading:
    at: float; ok: bool; state: str | None; pending: bool | None; last_used_at: float | None

def parse_time(value: str | None) -> float | None
def new_rid(now: float) -> str                       # 'r-HHMMSSZ' (UTC)

class Ledger:                                        # keeper-ledger.jsonl, append-only, 0o600
    def __init__(self, path: Path)
    def rows(self) -> list[dict]                     # skips unparsable lines
    def append(self, kind: str, rid: str, **fields) -> None   # kind 'intent' | 'result'; adds 'at'

class LifecycleAPI:
    def __init__(self, name: str, gh_user: str | None = None, *, upstream=None, token_reader=None)
    def read(self) -> Reading
    def post_start(self) -> int                      # HTTP status; 0 on network failure
    def post_stop(self) -> int

def start(client, *, ledger: Ledger | None = None, rid: str | None = None,
          now=time.time, sleep=time.sleep, say=None, tick=None) -> dict
# returns {'outcome': 'available' | 'failed' | 'timeout', 'posted_at': float | None,
#          'posts': int, 'last_state': str | None, 'status': int | None}
```

- [ ] **Step 1: Write the failing tests** in `tests/test_lifecycle.py`. A `FakeClient` replays a scripted list of `Reading`s and POST statuses, and `now`/`sleep` are a fake clock.

```python
def test_parse_time_accepts_z_and_offsets(self):
    self.assertEqual(lifecycle.parse_time('2026-10-21T14:34:47Z'),
                     lifecycle.parse_time('2026-10-21T11:34:47-03:00'))
    self.assertIsNone(lifecycle.parse_time(None))

def test_start_waits_for_shutdown_before_posting(self):
    # readings: ShuttingDown x3, Shutdown(pending=True), Shutdown(pending=False), Queued, Available
    self.assertEqual(client.posts, 1)
    self.assertEqual(result['outcome'], 'available')
    self.assertEqual(result['posted_at'], time_of_the_post)

def test_409_waits_for_shutdown_and_posts_again(self):
    # first POST 409, readings ShuttingDown, Shutdown(pending False) -> second POST 200 -> Available
    self.assertEqual(client.posts, 2)

def test_still_shutdown_two_minutes_after_accepted_post_reposts_once(self):
    self.assertEqual(client.posts, 2)

def test_posts_are_capped_per_rid(self):
    # 5xx forever -> exactly MAX_POSTS posts, outcome 'failed'

def test_other_4xx_stops_without_retry(self):   # 403 -> posts == 1, outcome 'failed', status 403

def test_available_wait_times_out_at_45_minutes(self):   # outcome 'timeout'

def test_ledger_gets_intent_before_and_result_after_each_post(self):
    kinds = [row['kind'] for row in ledger.rows()]
    self.assertEqual(kinds, ['intent', 'result'])
    self.assertNotIn('token', json.dumps(ledger.rows()))

def test_lifecycle_api_reads_and_posts_only_its_routes(self):
    # upstream fake records (method, url); read -> GET /user/codespaces/<name>;
    # post_start -> POST /user/codespaces/<name>/start; nothing else
    # a token_reader that fails after one success -> second read still uses the in-memory token
```

  In `tests/test_workspace_codespaces.py`: `start_codespace` during ShuttingDown posts only after Shutdown, and a 409 no longer raises "check GitHub authentication".

- [ ] **Step 2: Run and confirm they fail.**
  Run: `python3 -m unittest tests.test_lifecycle tests.test_workspace_codespaces -v`
  Expected: FAIL, because `lifecycle` does not exist.

- [ ] **Step 3: Implement `lifecycle.py`.**
  - `LifecycleAPI` uses the `transport._upstream` pattern (no redirects, 20 s timeout) with methods GET and POST.
  - Its token comes from `token_reader()`, which defaults to `transport.github_token(env)`, with `DEVCONTAINER_GH_USER=gh_user` when `gh_user` is set. Keep the last successful token in memory as the fallback (spec D10).
  - `read()` returns `ok=False` on any non-200, non-JSON or name mismatch.
  - `start()` follows spec 6.4 step 5, with the constants above. It calls `tick()` on every poll (at most every 10 s), and `say(text)` on each state change.
- [ ] **Step 4: Make `Codespaces.start_codespace(name=None)`** call `lifecycle.start(LifecycleAPI(name), say=…)` with no ledger. Raise `ValueError` with the outcome on `failed` or `timeout`.
- [ ] **Step 5: Run the tests and confirm they pass**, with the same command. Expected: PASS.
- [ ] **Step 6: Gates and commit.**
  `git commit -m "Start Codespaces only from Shutdown, through one start routine"`

---

### Task 3: Arming state, notifications, and the new commands

**Files:**
- Create: `devcontainer_setup/project_sync/arming.py`, `devcontainer_setup/project_sync/notify.py`
- Modify: `devcontainer_setup/project_sync/__main__.py`, `templates/continuity-tasks.yml`, `devcontainer_setup/root_tasks.py:6`, `Taskfile.yml`
- Test: `tests/test_arming.py`, `tests/test_notify.py`, `tests/test_project_sync_cli.py`, `tests/test_root_tasks.py`, `tests/test_continuity_integration.py`

**Interfaces:**
- Consumes: `lifecycle.parse_time`.
- Produces:

```python
# arming.py
ARMING = 'arming.json'
def read(state: Path) -> dict          # {'schema': 1, 'armed': False} when missing
def write(state: Path, value: dict) -> None
def default_ports(root: Path) -> list[int]      # .devcontainer/devcontainer.json forwardPorts minus 2222, sorted
def resolve_ports(defaults: list[int], add: list[int], remove: list[int]) -> list[int]
def consent_text(project_name: str, codespace: str, billable_owner: str) -> str
def account(env: Mapping) -> str       # refuses GH_TOKEN/GITHUB_TOKEN; DEVCONTAINER_GH_USER or the active login;
                                       # requires the 'codespace' scope (X-OAuth-Scopes of GET /user)
def hold_until(value: str, now: float) -> float | None   # 'HH:MM' local -> next occurrence; 'off' -> None
def record_owner_stop(state: Path, now: float) -> None

# notify.py
def notify(title: str, message: str, *, run=subprocess.run) -> bool   # osascript; False on any failure, never raises
```

- `arming.json` keys: `schema, armed, armed_at, gh_user, consent_sha256, consented_at, billable_owner, ports, hold_until, owner_stop_at`.
- New `work` actions in `__main__.parser()`, in the existing mutually exclusive group:

| Flag | Task |
|---|---|
| `--armed` | `work:cs:armed`, needs a TTY |
| `--disarm` | `work:disarm` |
| `--hold VALUE` | `work:hold` |
| `--codespace-stop` | `work:cs:stop`, needs a TTY |
| `--codespace-restart` | `work:cs:restart`, needs a TTY |
| `--logs` | `work:logs`, with `--follow` |
| (hidden) `--port N` / `--no-port N` | repeatable, for `--armed` |

  In this task, `--armed`, `--disarm` and `--hold` only read and write `arming.json`. Task 5 wires the keeper and Task 8 the restart.
- Task names: `work:cs:armed`, `work:disarm`, `work:hold`, `work:cs:stop`, `work:cs:restart`, `work:logs`. Each is added to `templates/continuity-tasks.yml`, `root_tasks.ALIASES`, and the generator's own `Taskfile.yml`. `work:cs:armed`, `work:cs:stop` and `work:cs:restart` get `interactive: true`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_default_ports_drop_2222(self):            # forwardPorts [2222, 3570, 8787] -> [3570, 8787]
def test_resolve_ports_adds_and_removes(self):     # ([3570, 8787], add=[3571], remove=[8787]) -> [3570, 3571]
def test_account_refuses_environment_tokens(self): # GH_TOKEN set -> ValueError naming GH_TOKEN, value never in message
def test_account_requires_codespace_scope(self):   # fake gh returns scopes 'repo' -> ValueError
def test_consent_is_stored_as_hash(self):          # arming.json has consent_sha256, not the text
def test_hold_until_next_local_occurrence(self):   # 09:30 at 23:00 local -> tomorrow 09:30; 'off' -> None
def test_notify_never_raises(self):                # osascript missing or exit 1 -> False
def test_new_work_tasks_render_and_alias(self):    # each new task present in the template and ALIASES;
                                                   # the root alias block integrates idempotently
def test_armed_requires_a_terminal(self):          # non-TTY -> 'Work entrypoints require a terminal'
```

- [ ] **Step 2: Run and confirm they fail.**
  Run: `python3 -m unittest tests.test_arming tests.test_notify tests.test_project_sync_cli tests.test_root_tasks tests.test_continuity_integration -v`
  Expected: FAIL.
- [ ] **Step 3: Implement** `arming.py`, `notify.py`, the parser actions, and the task entries.
- [ ] **Step 4: Run and confirm they pass.** Also fix any existing test that pinned the exact alias tuple or task list. Expected: PASS.
- [ ] **Step 5: Gates and commit.**
  `git commit -m "Add arming state, notifications and the recovery commands"`

---

### Task 4: Classification and the restart gate

**Files:**
- Modify: `devcontainer_setup/project_sync/lifecycle.py`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Produces:

```python
def classify(*, last_up_at: float | None, first_down_at: float,
             anchor_at: float | None, owner_stop_at: float | None) -> str   # 'OWNER' | 'LIFETIME' | 'OTHER'
def restart_rids(rows: list[dict], now: float) -> list[str]   # rids with an 'intent' row within RESTART_PERIOD
def gate(*, arming: dict, rows: list[dict], now: float) -> str | None   # None = allowed, else the reason text
```

- **Rules for `classify`:**
  1. `OWNER` when `owner_stop_at` is set, is not earlier than `anchor_at` (or `anchor_at` is `None`), and is not later than `first_down_at`.
  2. `LIFETIME` when `anchor_at` is set and the interval `[last_up_at or first_down_at, first_down_at]` overlaps `[anchor_at + LIFETIME - WINDOW, anchor_at + LIFETIME + WINDOW]`. Spec 6.4 step 3 reads only `first_down_at`. The interval extends that for readings missed while the Mac slept, and is the same when no reading was missed.
  3. Otherwise `OTHER`.
- **Rules for `gate`**, in this order, returning the first reason:
  1. not armed: `'not armed'`;
  2. `hold_until > now`: `'on hold until <local> (<UTC>Z)'`;
  3. `len(restart_rids) >= MAX_RESTARTS`: `'2 restarts already in 24 hours'`;
  4. the latest intent is less than `MIN_GAP` ago: `'last restart was less than 30 minutes ago'`.

- [ ] **Step 1: Write the failing tests**, built from the measured stops (spec section 3):

```python
def test_measured_lifetime_stops_classify_as_lifetime(self):
    # probe: anchor 01:37:32Z, down 13:37:44Z; mainstage: anchor 02:21:51Z, down 14:22:12Z;
    # innovate: anchor 22:08:24Z (creation), down 10:08:53Z next day; last_up_at 60 s before each
def test_stop_at_six_hours_is_other(self):
def test_owner_stop_after_anchor_is_owner(self):
def test_late_detection_overlapping_window_is_lifetime(self):
    # last_up 11:50 after anchor, first_down 14:55 after anchor (Mac asleep) -> LIFETIME
def test_gap_outside_window_is_other(self):
    # last_up 12:20 after anchor, first_down 15:00 -> OTHER
def test_no_anchor_is_other(self):
def test_gate_reasons_in_order(self):               # each rule above, one subtest each
def test_budget_counts_rids_not_posts(self):        # one rid with 3 intent rows counts once
```

- [ ] **Step 2: Run and confirm they fail.**
  Run: `python3 -m unittest tests.test_lifecycle -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `classify`, `restart_rids` and `gate`.
- [ ] **Step 4: Run and confirm they pass.** Expected: PASS.
- [ ] **Step 5: Gates and commit.**
  `git commit -m "Classify Codespace stops and gate restarts by budget and hold"`

---

### Task 5: The keeper process, watching in notify mode

**Files:**
- Create: `devcontainer_setup/project_sync/keeper.py`
- Modify:
  - `devcontainer_setup/project_sync/state.py`: add `owned_process`.
  - `devcontainer_setup/project_sync/scratch.py:165-171`: `_owned` delegates to it, with unchanged behavior.
  - `devcontainer_setup/project_sync/workspace.py`: `remote(..., armed=False)`, the status Recovery block.
  - `devcontainer_setup/project_sync/__main__.py`: wire `--armed`, `--disarm`, `--hold`, `--logs`, and `work:stop` also stopping the keeper.
- Test: `tests/test_keeper.py`, `tests/test_workspace_codespaces.py`, `tests/test_project_sync_cli.py`

**Interfaces:**
- Consumes:
  - `lifecycle.LifecycleAPI`, `Reading`, `classify`, `gate`, `Ledger`, `new_rid` (Tasks 2 and 4);
  - `arming.*` and `notify.notify` (Task 3).
- Produces:

```python
# state.py
def owned_process(runtime: dict, state: Path, *, flag: str, module: str) -> bool

# keeper.py
RUNTIME = 'keeper-runtime.json'; LOG = 'keeper.log'; LEDGER = 'keeper-ledger.jsonl'
POLL = 60; HEARTBEAT_STALE = 300
def start(root: Path, state: Path) -> dict      # adopt the same runtime_id; replace a different one; launch otherwise
def stop(state: Path) -> dict                   # by fact: owned_process + SIGTERM, waits up to 15 s
def status(state: Path) -> dict                 # runtime + 'running' + 'heartbeat_age'
def supervise(root: Path, state: Path, nonce: str) -> None   # entry for `--keep ROOT STATE NONCE`

class Keeper:
    def __init__(self, state: Path, *, api, notifier, keepalive=None, herdr=None,
                 now=time.time, sleep=time.sleep)   # herdr: restore.Herdr (Task 8)
    def step(self) -> None                      # one pass; never raises
```

- **Launch:** like `scratch.start`, with `start_new_session=True`, a nonce, argv marker `--keep`, and module `project_sync.keeper`.
- **Environment allow-list:** `HOME, PATH, LANG, TMPDIR, USER, LOGNAME`, plus `DEVCONTAINER_GH_USER` from `arming.json`.
- **Locks:** a single-instance lock on `keeper.lock` while supervising, and `keeper-control.lock` around start/stop.
- **Mac sleep:** `caffeinate -i -w <pid>` is started by `supervise` and ended in its `finally`.
- **`keeper-runtime.json` keys:** `pid, nonce, process_identity, runtime_id, heartbeat_at, phase, reading {at, state, pending}, last_up_at, down {count, first_at}, anchor {at, source}, keepalive {...}, last_restart {...}, notified {key: at}`.
- **`Keeper.step` rules** (spec 6.4 steps 1-4):
  - **Anchor:**
    - at first start, if Available and there is no anchor: `last_used_at` with source `api`;
    - on a not-Available → Available transition the keeper did not cause: reading time, source `observed`.
  - **Down readings:** 3 consecutive successful readings that are not Available, then classify with `last_up_at`, `down.first_at`, the anchor and `owner_stop_at`.
  - **OWNER:** log only.
  - **OTHER:** notify once (spec 6.4 step 3 text).
  - **LIFETIME:** in this task, notify once "would restart \<project\> (\<rid-to-be\>)" whatever the gate says, or name the refusing gate reason. Task 8 replaces this branch.
  - **Heartbeat:** written every pass.
  - **Exceptions:** logged with type and fixed text; the loop continues with 2 s→60 s backoff.
- **Logs:** `keeper.log` and the worker's `scratch.log` both get a formatter with a UTC time and the pid on every line (`%(asctime)sZ %(process)d %(message)s`, UTC converter). `--logs` prints the last 80 lines of both, merged by that time, and `--follow` tails both.
- **Status block lines**, following spec 6.8's example: `Recovery`, `Keeper`, `Codespace` (with "12-hour stop due"), `Keep-alive`, `Last restart`. Each line that is not fine names the command to run.

- [ ] **Step 1: Write the failing tests.**

```python
def test_concurrent_arms_leave_one_keeper(self):        # two threads call keeper.start -> one pid, one nonce
def test_repeat_start_adopts_same_keeper(self):         # same pid/nonce, no relaunch
def test_new_runtime_replaces_old_keeper(self):         # runtime_id differs -> old SIGTERMed, new pid
def test_stop_by_fact_ignores_a_reused_pid(self):       # foreign process at recorded pid -> not killed
def test_keeper_env_is_allow_listed(self):              # GH_TOKEN / HERDR_PANE_ID absent in child env
def test_step_sets_api_anchor_then_observed_anchor(self):
def test_three_down_readings_classify_once(self):       # 5 Shutdown readings -> exactly 1 notification
def test_failed_reading_resets_down_count(self):
def test_lifetime_in_notify_mode_never_posts(self):     # api.posts == 0, notification says 'would restart'
def test_step_never_raises(self):                       # api.read raises -> heartbeat written, phase 'waiting'
def test_status_block_shows_due_time_in_both_zones(self):
```

- [ ] **Step 2: Run and confirm they fail.**
  Run: `python3 -m unittest tests.test_keeper tests.test_workspace_codespaces tests.test_project_sync_cli -v`
  Expected: FAIL.
- [ ] **Step 3: Implement** `owned_process`, `keeper.py`, and the CLI and status wiring.
  - `work:cs:armed` runs `workspace.remote(project, state, armed=True)`.
  - Arming happens after the worker is ready: refuse foreign gh processes for this Codespace, consent once, record the account and ports, start the keeper, wait up to 60 s for its first heartbeat, then print the status block.
  - The foreign-process scan is `ps -axo pid=,command=` for `gh codespace` lines naming this Codespace whose pid is not the keeper's, the K4 child's, the worker's forward or a `herdr_stdio` gh.
- [ ] **Step 4: Run and confirm they pass**, with the same command plus the whole suite. Expected: PASS.
- [ ] **Step 5: Gates and commit.**
  `git commit -m "Add the per-project keeper that watches an armed Codespace"`

---

### Task 6: K4 keep-alive child with port forwards (#36)

**Files:**
- Create: `devcontainer_setup/project_sync/keepalive.py`
- Modify: `devcontainer_setup/project_sync/keeper.py` (`Keeper.step` calls `keepalive.ensure`; `supervise` builds `KeepAlive`)
- Test: `tests/test_keepalive.py`, `tests/test_sync_transport.py` (real-gh boundary)

**Interfaces:**
- Consumes: `transport.ReadOnlyAPI`, `transport.github_token`, `transport._terminate`, `arming.read(state)['ports']`.
- Produces:

```python
def argv(name: str, key: Path, ports: list[tuple[int, int]]) -> list[str]
def free_ports(ports: list[int]) -> tuple[list[tuple[int, int]], list[tuple[int, int | None]]]
# -> ([(local, remote) bindable], [(local, holder_pid or None) in use]); local = remote + 10000
class KeepAlive:
    def __init__(self, name: str, state: Path, gh_user: str | None, ports: list[int], *,
                 popen=subprocess.Popen, now=time.monotonic)
    def ensure(self, available: bool) -> dict   # {'pid', 'ports_up', 'ports_skipped', 'restarts'}
    def stop(self) -> None
```

- **`argv` returns exactly:**
  `['gh', 'codespace', 'ssh', '-c', name, '--', '-i', str(key), '-o', 'ServerAliveInterval=30', '-o', 'ExitOnForwardFailure=yes', '-L', '127.0.0.1:<local>:localhost:<remote>', …, 'exec sleep infinity']`
- **Process:** the child runs with `env=api.child_env(...)`, `start_new_session=True`, and stdio to DEVNULL. `KeepAlive` holds one `ReadOnlyAPI` open while a child runs.
- **`ensure(available)`:**
  - Available with no live child: start one, after `free_ports`.
  - The child exited while Available: start again after a backoff of 2 s doubling to 60 s.
  - Not Available: stop the child, with no respawn.
- **Holder pid** for a port in use comes from `lsof -nP -t -iTCP:<port> -sTCP:LISTEN`, or is `None` when lsof finds nothing.

- [ ] **Step 1: Write the failing tests.**

```python
def test_argv_is_exact(self):
def test_port_in_use_is_skipped_and_reported(self):     # bind a socket on 13570 first
def test_child_respawns_with_backoff_while_available(self):
def test_child_stops_and_stays_stopped_when_not_available(self):
def test_child_env_has_capability_not_token(self):      # fake gh records env: GH_TOKEN is the child token
# tests/test_sync_transport.py, skipUnless(shutil.which('gh')):
def test_actual_gh_keepalive_argv_never_posts_start(self):   # Shutdown upstream; no '/start' call
```

- [ ] **Step 2: Run and confirm they fail.**
  Run: `python3 -m unittest tests.test_keepalive tests.test_sync_transport -v`
  Expected: FAIL.
- [ ] **Step 3: Implement** `keepalive.py`. Wire `Keeper.step` to call `ensure(reading.ok and reading.state == 'Available')`, and record the result in `keeper-runtime.json` under `keepalive`.
- [ ] **Step 4: Run and confirm they pass.** Expected: PASS.
- [ ] **Step 5: Gates and commit.**
  `git commit -m "Hold armed Codespaces with a read-only keep-alive that carries the ports"`, with the body `Fixes #36.`

---

### Task 7: Worker stop reasons, unarmed notifications, keeper watch (#48)

**Files:**
- Modify:
  - `devcontainer_setup/project_sync/replication.py:212-223` (`verify()`);
  - `devcontainer_setup/project_sync/scratch.py` (the `supervise` except branch and loop).
- Test: `tests/test_replication.py`, `tests/test_scratch_sync.py`

**Interfaces:**
- Consumes:
  - `transport.codespace_state` (Task 1);
  - `arming.read` and `notify.notify` (Task 3);
  - `keeper.status` and `keeper.HEARTBEAT_STALE` (Task 5).
- Produces: worker runtime keys `notified_stop` (bool) and `notified_keeper` (bool).

- **`verify()` failure messages**, after reading the state with `codespace_state(connection.codespace, github_token(connection.env))`:

| State | `ConnectionVerificationError` message |
|---|---|
| not `Available` | `Codespace is <state>; no start request was allowed.` |
| `None` | `Project connection verification failed; Codespace state unknown.` |
| `Available` | `Project connection verification failed; identity/authentication must be valid` (unchanged) |

- **Worker, on entering `waiting`:**
  - If the project is not armed, the state is known and not `Available`, and `notified_stop` is false: notify once (spec 6.7 unarmed text) and set `notified_stop`.
  - Reaching `syncing` clears it.
- **Keeper watch**, at most once a minute in both the syncing loop and the waiting backoff:
  - If the project is armed and the keeper is not running, or its heartbeat is older than `HEARTBEAT_STALE`: notify once, "Recovery for \<project\> is not running; run task work:cs:armed", and set `notified_keeper`.
  - A healthy keeper clears it.

- [ ] **Step 1: Write the failing tests.**

```python
def test_verify_reports_shutdown_not_authentication(self):
def test_verify_reports_unknown_state_when_read_fails(self):
def test_verify_keeps_identity_message_when_available(self):
def test_unarmed_stop_notifies_once_until_syncing(self):
def test_armed_stop_leaves_notifications_to_the_keeper(self):
def test_dead_keeper_notifies_once(self):
```

- [ ] **Step 2: Run and confirm they fail.**
  Run: `python3 -m unittest tests.test_replication tests.test_scratch_sync -v`
  Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run and confirm they pass**, plus the whole suite. Expected: PASS.
- [ ] **Step 5: Gates and commit.**
  `git commit -m "Report a stopped Codespace by its state and notify once per stop"`, with the body `Fixes #48.`

---

### Task 8: Recovery on (restart, restore, resume, planned restart)

**Files:**
- Create: `devcontainer_setup/project_sync/restore.py`
- Modify:
  - `devcontainer_setup/project_sync/keeper.py` (the LIFETIME branch; startup recovery of an interrupted rid);
  - `devcontainer_setup/project_sync/__main__.py` (`--codespace-stop`, `--codespace-restart`);
  - `README.md:117-118`, `docs/project-continuity.md:140-142`, and their command tables.
- Test: `tests/test_restore.py`, `tests/test_keeper.py`, `tests/test_project_sync_cli.py`

**Interfaces:**
- Consumes:
  - Task 2: `lifecycle.start`, `Ledger`, `new_rid`;
  - Task 4: `gate`;
  - Task 6: `KeepAlive.ensure`;
  - Task 3: `arming.record_owner_stop`;
  - Task 5: `keeper-runtime.json`.
- Produces:

```python
# restore.py
def stale_proxies(codespace: str, state: Path, before: float, *, run=subprocess.run) -> list[int]
def end_stale_proxies(codespace: str, state: Path, before: float, *, run=subprocess.run) -> list[int]
class Herdr:
    def __init__(self, machine_id: str, *, run=subprocess.run, timeout: float = 60)
    def reachable(self) -> bool
    def agents(self) -> list[dict]
    def prompt(self, name: str, text: str) -> bool
    def read(self, name: str, lines: int = 80) -> str
def lead(agents: list[dict]) -> dict | None
def resume_text(rid: str, stopped_at: float, back_at: float) -> str
def resume(herdr: Herdr, rid: str, text: str, *, now=time.time, sleep=time.sleep) -> str
# -> 'delivered' | 'resent' | 'no-lead' | 'busy' | 'undelivered'
```

- **`stale_proxies`** parses `ps -axo pid=,lstart=,command=`. It matches processes whose command contains `herdr_stdio`, the Codespace name and the state path, and whose start time (lstart, local time) is earlier than `before`.
- **`end_stale_proxies`** sends SIGTERM to each. Its code comment labels it defensive and links #46.
- **`Herdr` commands:**
  - `reachable`: `herdr machine status <id> --json`, then `[0]['status'] == 'reachable'`.
  - `agents`: `herdr --machine <id> agent list`, then `['result']['agents']`.
  - `prompt`: `herdr --machine <id> agent prompt <name> <text>`.
  - `read`: `herdr --machine <id> agent read <name> --source recent-unwrapped --lines N`.
  - Every call has `timeout`. A timeout, non-zero exit or bad JSON means `False` or `[]` or `''`.
  - The environment drops `HERDR_*` variables, so the commands never target a pane.
- **`lead`:** exactly one agent whose `name` starts with `lead-`, else `None`.
- **`resume_text`:** the verbatim sentence from Global Constraints, then ` (Automatic restart <rid>: GitHub's 12-hour stop at HH:MMZ, back at HH:MMZ. Restored agents came back with --resume and without their launch flags, so check each worker's model, effort and sandbox before dispatching to it.)`
- **`resume` rules** (spec 6.6):
  1. Wait until the lead is `idle` on two reads 10 s apart, for up to 10 min, else `'busy'`.
  2. Send the prompt.
  3. Within 2 min, delivered if the rid is in `read()` or the status is `working`.
  4. Otherwise, if it is idle, send once more (`'resent'` when that is delivered); else `'undelivered'`.
- **Keeper LIFETIME branch:**
  1. If `gate` refuses, notify the reason.
  2. Otherwise run `lifecycle.start` with the ledger and a new rid, `tick` writing the heartbeat. On `available`, the anchor is `posted_at` with source `keeper`.
  3. Run `keepalive.ensure(True)`.
  4. Run `end_stale_proxies(before=first_down_at)`.
  5. Wait for `Herdr.reachable()` for up to 10 min, polling every 10 s. If it never answers, notify (spec 6.4 step 6) and stop there.
  6. Run `resume`, write `last_restart`, and notify the outcome.
- **Interrupted rid at keeper start:** if the last ledger row is an `intent` with no `result`, read the state first. If it is Queued, Starting or Available, record a `result` row with `interrupted: true` and do not POST. If it is Shutdown, continue with the same rid while it has POSTs left.
- **`--codespace-stop`:** `arming.record_owner_stop`, then `LifecycleAPI.post_stop()`.
- **`--codespace-restart`:**
  1. Record owner stop, then POST stop.
  2. `lifecycle.start` with no budget and the rid prefixed `o-`.
  3. If armed, write `keeper-runtime.json` `pending_restore: {rid, stopped_at}`; the keeper's next step runs restore and resume for it. If unarmed, print the times and stop.

- [ ] **Step 1: Write the failing tests.**

```python
def test_stale_proxies_match_only_this_codespace_and_older(self):   # fake ps output, 4 rows
def test_herdr_calls_drop_herdr_env(self):
def test_hanging_herdr_is_bounded_and_not_fatal(self):              # fake herdr sleeps 5 s, timeout 0.2
def test_lead_requires_exactly_one(self):
def test_resume_text_is_verbatim(self):
def test_resume_resends_once_when_rid_absent(self):
def test_resume_waits_for_idle_then_gives_up_busy(self):
def test_lifetime_restart_end_to_end_with_fakes(self):
    # fake api: Shutdown x3 -> start -> Available; fake herdr reachable; one lead idle
    # -> ledger intent+result, anchor source 'keeper', resume 'delivered', one 'restarted' notification
def test_gate_refusal_never_posts(self):
def test_interrupted_intent_is_not_reposted(self):
def test_owner_stop_is_never_restarted(self):
def test_planned_restart_skips_budget_and_resumes(self):
```

- [ ] **Step 2: Run and confirm they fail.**
  Run: `python3 -m unittest tests.test_restore tests.test_keeper tests.test_project_sync_cli -v`
  Expected: FAIL.
- [ ] **Step 3: Implement** `restore.py`, the keeper branch, the startup recovery, the CLI actions, and the docs.
  - The README and continuity doc state that the keeper of an armed project is the one background process that can start a Codespace, and only after a classified 12-hour stop within 2 restarts in 24 h, 30 min apart.
  - Add the new commands to their tables.
- [ ] **Step 4: Run and confirm they pass**, plus the whole suite. Expected: PASS.
- [ ] **Step 5: Gates and commit.**
  `git commit -m "Restart armed Codespaces after the 12-hour stop and resume the lead"`

---

### Task 9: Setup commits from origin's default branch in a temporary worktree (#47, part 1)

**Files:**
- Modify: `devcontainer_setup/git_ops.py` (replace `proposed_branch`; add the worktree, PR-cleanup and fast-forward helpers), `devcontainer_setup/__main__.py` (`interactive`, `main --dry-run`)
- Test: `tests/test_git_ops.py`, `tests/test_wizard.py`

**Interfaces:**
- Produces:

```python
def default_branch(project: Path) -> str                     # origin/HEAD short name, else 'main'
def next_setup_branch(project: Path) -> str                   # 'setup/devcontainer[-N]' free locally and on origin
def setup_worktree(project: Path, root: Path) -> tuple[Path, str]   # fetch origin <default>; worktree at
                                                              # root/<branch-with-slashes-as-dashes> on a new branch
                                                              # cut from origin/<default>; returns (path, branch)
def remove_setup_worktree(project: Path, path: Path, branch: str, *, remote: bool) -> None
# git worktree remove, then delete the branch locally and (if remote) on origin, then verify with ls-remote
def fast_forward_checkout(project: Path, default: str) -> bool   # only on <default>, clean, ff-only; else False
```

- **Worktree root:** `<XDG_STATE_HOME or ~/.local/state>/devcontainer-setup/setup-worktrees/<project_id>`.
- **`interactive` with a GitHub origin:**
  - Create the worktree first.
  - Run `scan`, `_metadata`, `render`, `integrate_taskfile`, `integrate_continuity`, `check_candidate` and `GitPlan` against the worktree path. `GitPlan(worktree, files)` is on the setup branch already, so `branch=None`.
  - The review header shows `Branch: <setup branch> (from origin/<default>)`.
- **"Commit only"** commits in the worktree and keeps the local branch. It removes the worktree without `remote` and prints the branch name.
- **No GitHub origin:** today's flow, unchanged.
- **`--dry-run`** renders from a temporary worktree too, and always removes it.

- [ ] **Step 1: Write the failing tests**, with fixture repos and a bare `origin`:

```python
def test_setup_branch_is_cut_from_origin_default_not_current(self):
    # checkout on feature branch with an unpushed commit and an uncommitted file
    # -> worktree HEAD == origin/main; feature branch, HEAD and the file unchanged
def test_rendered_files_come_from_the_worktree(self):   # a file only on the feature branch is absent
def test_next_setup_branch_skips_local_and_remote_names(self):
def test_remove_setup_worktree_deletes_branch_after_worktree(self):
def test_fast_forward_only_when_clean_on_default(self):  # dirty or on feature -> False, nothing changed
def test_no_origin_keeps_current_flow(self):
```

- [ ] **Step 2: Run and confirm they fail.**
  Run: `python3 -m unittest tests.test_git_ops tests.test_wizard -v`
  Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run and confirm they pass**, plus the whole suite. Expected: PASS.
- [ ] **Step 5: Gates and commit.**
  `git commit -m "Build setup commits from origin's default branch in a temporary worktree"`

---

### Task 10: Setup reaches main by PR, then creates the Codespace on it (#47, part 2)

**Files:**
- Modify: `devcontainer_setup/codespaces.py` (PR helpers; `launch` takes branch and sha; receipts), `devcontainer_setup/__main__.py:600-675`
- Test: `tests/test_codespaces.py`, `tests/test_wizard.py`

**Interfaces:**
- Consumes (Task 9): `default_branch`, `remove_setup_worktree`, `fast_forward_checkout`, and `GitPlan.push()` (unchanged).
- Produces:

```python
# Codespaces (devcontainer_setup/codespaces.py)
def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> int   # reuses an open PR for head
def merge_pr(self, repo: str, number: int, title: str, message: str, *,
             wait: float = 900, sleep=time.sleep) -> str                         # returns the merge commit sha
def launch(self, repo: str, branch: str, sha: str, machine, idle_timeout, client, dotfiles_repo,
           receipt: Path, *, resume=None, location=None, retention=None, workspace=None,
           machine_profile=None) -> str                                         # no plan.push() inside
def receipt_path(state_root: Path, repo: str, branch: str) -> Path              # sha256(f'{repo}:{branch}')[:16]
def migrate_receipt(state_root: Path, repo: str, default: str, codespace_ref: str) -> Path | None
```

- **`merge_pr`:**
  - Makes the `PUT repos/{repo}/pulls/{n}/merge` call with `merge_method=merge`, `commit_title=title` and `commit_message=message`, verbatim and with no trailers.
  - While the head commit's combined status is `pending`, or any check run is not `completed`, it waits 30 s and retries, up to `wait`.
  - Any other refusal raises `ValueError(f'PR #{n} is open: {github_message}. Merge it, then run task devcontainer:setup again to create the Codespace.')`.
- **The flow in `interactive`**, after commit with launch selected:
  1. `plan.push()`.
  2. `open_pr`, with title `Configure project devcontainer` and the review summary as body.
  3. Confirm `Merge PR #<n> into <default> as <user>? Required reviews are bypassed if your account is allowed to.`
  4. `merge_pr`.
  5. `remove_setup_worktree(remote=True)`.
  6. `fast_forward_checkout`.
  7. `launch(repo, default, merge_sha, …)` with `receipt_path(state_root, repo, default)`.
- **Resume and reuse** check `git_status.ref == default`. A Codespace on another ref raises `ValueError(f'Move {name} to {default} first; it is on {ref}.')`. `migrate_receipt` copies an old receipt for this repo whose Codespace now reports `default` to the new path, and leaves the old file in place.

- [ ] **Step 1: Write the failing tests**, with a fake `run` recording gh argv:

```python
def test_merge_uses_rest_with_verbatim_message(self):    # argv has PUT .../merge, merge_method=merge; no 'Co-authored-by'
def test_merge_waits_for_pending_checks_then_merges(self):
def test_merge_refusal_reports_github_reason_and_rerun(self):
def test_open_pr_reuses_existing(self):
def test_codespace_created_on_default_branch_at_merge_sha(self):   # 'codespace create ... --branch main'
def test_reuse_refuses_codespace_on_other_branch(self):
def test_receipt_migrates_from_retired_setup_branch(self):
def test_cancelled_merge_leaves_pr_open_and_no_codespace(self):
```

- [ ] **Step 2: Run and confirm they fail.**
  Run: `python3 -m unittest tests.test_codespaces tests.test_wizard -v`
  Expected: FAIL.
- [ ] **Step 3: Implement.** Move `plan.push()` out of `launch`. Update every `launch` caller and its tests.
- [ ] **Step 4: Run and confirm they pass**, plus the whole suite. Expected: PASS.
- [ ] **Step 5: Gates and commit.**
  `git commit -m "Merge setup into the default branch and create Codespaces there"`, with the body `Fixes #47.`

---

### Task 11: Live verification (lead only, not a subagent)

These steps touch real Codespaces. Run them in order and record each result in `tmp/codespace-lifecycle/tests/TEST-STATE.md` with the command that produced it.

- [ ] **Step 1: Push.** Re-run `task check` and `task test` in the default environment, logged to `verify/recovery-<sha>.log` with the exit code. Fast-forward `main` and push it with emsilva's token helper. Stop and ask if it has diverged.
- [ ] **Step 2: Measure the K4 child with ports.** Ask the owner first, because it costs credits. Use a throwaway Codespace with a 5-min idle timeout, held only by `keepalive.argv(...)` with 2 forwards, for at least 30 min. If it stops, stop here and report.
- [ ] **Step 3: innovate upgrade.** Run `task devcontainer:upgrade:self` in `~/code/consult-sm-innovate`. It goes through Tasks 9-10: a PR merged into innovate's main, the worktree and branch pruned, and the checkout fast-forwarded. Then run `task work:cs:armed`.
- [ ] **Step 4: Slice D DoD on innovate.**
  - A second arm leaves the same keeper pid, nonce and K4 pid, and no ledger rows.
  - `kill -9` of the keeper produces the worker's notification within 6 min.
  - `task work:disarm` leaves no keeper and no K4 child.
  - Re-arm afterwards.
- [ ] **Step 5: Slice F DoD on innovate.**
  - `task work:cs:restart` ends with the Codespace back, Herdr reachable and the rid in a test lead's screen, with no keystroke after the command.
  - `task work:cs:stop` is not restarted. After it, run `task work:cs:armed` to bring it back.
- [ ] **Step 6: One real 12-hour stop on innovate, recovered unattended.** The due time is in `task work:status`. Check that the ledger has one intent and one result, status shows the rid, and the lead's screen has the rid.
- [ ] **Step 7: Report to the owner.** Mainstage waits for #45 and the owner's go-ahead (spec section 7).
