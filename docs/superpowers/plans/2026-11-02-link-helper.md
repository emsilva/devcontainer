# Links From a Codespace Open on the Mac: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> In this repo the lead runs it through the herd instead (owner ruling 20): Codex xhigh builders in worktrees, the
> lead reviews the diff and re-gates in the default environment.

**Goal:** Any `http`/`https` link a Codespace tool opens appears in the Mac's browser, and browser sign-ins with a
loopback callback finish on the Codespace by themselves.

**Architecture:** On the Codespace, `BROWSER` (and an `xdg-open` stand-in) point at `open-on-mac`, which writes a
request file and exits. On the Mac, the scratch worker runs a watcher thread that streams new requests over the
project SSH, opens each link with `/usr/bin/open`, and, for a loopback callback port, runs a same-port `ssh -L`
forward until the sign-in ends. `task work:signin` runs the same handler when the worker's watcher is not live.

**Tech Stack:** Python 3 standard library, bash, OpenSSH, go-task, macOS `open` and `osascript`.

**Spec:** `docs/superpowers/specs/2026-11-01-link-helper-design.md` (calls 1–11, "How it works", test results).
Live-test evidence: `tmp/codespace-lifecycle/link-helper/spikes/RESULTS.md` (gitignored; the lead has it).

## Global Constraints

- Generated containers only. This repo's own `.devcontainer/scripts/post-create.sh:149` keeps calling
  `system-path.sh` with two arguments and must stay byte-for-byte unaffected.
- Accept only `http` and `https`; at most 8192 bytes; any explicit port in 1024–65535.
- Loopback hosts: `localhost`, `127.0.0.1`, `[::1]` (parsed hostname `::1`).
- Port to forward: the port of a loopback `redirect_uri` query value; else, for a link to a loopback host, the
  link's own port; else none. A `redirect_uri` with no explicit port means none.
- The Mac port equals the Codespace port. Never the keep-alive's `+10000` offset; never `keepalive.free_ports`.
- Forward argv exactly: `ssh -F <state>/ssh-config -o ClearAllForwardings=no -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=15 -N -L 127.0.0.1:P:127.0.0.1:P project-sync`, `start_new_session=True`.
- Never send any network request to a callback port (Codex and OpenCode treat `/cancel` as a cancel).
- Logs, notices and terminal messages carry the host, port and program only, never the URL.
- Lifetimes: a plain link 30 s from `created_at`; a sign-in 120 s when the program is `plaud`, else 600 s; a plain
  loopback link while the Codespace listens on it (worker only).
- Heartbeat: the watcher touches `mac-listening` every 5 s; older than 15 s means no Mac. Listener check every 2 s.
  Watcher restart backoff 2 s doubling to 60 s.
- CLIs that show their own link (never written to): `claude`, `codex`, `opencode`, `gh`, `az`.
- Commit messages are plain imperative sentences with no AI attribution. Gates: `task check` and `task test`.

## Shared contract (Tasks 1 and 3 both depend on it)

Codespace state dir `~/.local/state/devcontainer-links/` (mode 700):
- `requests/<id>.json`, mode 600, written to a temporary name in the same directory and renamed. `<id>` is
  `<time_ns>-<pid>-<4 hex>`. Content (schema 1):
  `{"schema": 1, "id": str, "url": str, "created_at": float, "program": str, "pid": int, "start": str, "tty": str|null}`
  where `start` is field 22 of `/proc/<pid>/stat` for the requester and `tty` is the requester's terminal path.
- `claimed/` (the watcher renames a request here before emitting it, then deletes it once emitted).
- `mac-listening`, an empty file whose mtime is the heartbeat.

Requester and program (Codespace side, `open-on-mac`): walk up from the helper's parent; the requester is the
first process with `/dev/pts/*` on fd 2, 1 or 0. `program_name(argv)` is `az` when any argument is `azure.cli`;
otherwise the basename of `argv[1]` when `basename(argv[0])` is `node`, `python`, `python3` or `bun`; otherwise
`basename(argv[0])`. Measured: Plaud is `node ~/.local/bin/plaud login`, two levels above the helper.

## Review Focus

1. A `redirect_uri` that points at a non-loopback host, or is percent-encoded twice: no forward, the page just
   opens (Task 2 test `test_non_loopback_redirect_opens_without_forward`).
2. A request file that is a symlink, larger than 64 KB, not JSON, or schema ≠ 1: skipped with one log line, and the
   watcher keeps serving later requests (Task 3 test `test_watcher_skips_bad_request_files`).
3. The Mac port held by a listener on `0.0.0.0` or `::` that a 127.0.0.1 test bind does not catch: reported busy
   with its holder, as the keep-alive's R19 rule does (Task 2 test `test_wildcard_listener_counts_as_busy`).
4. The forward's ssh exits mid-sign-in: its record is cleared, one notice, no orphan, and a new request for that
   port forwards again (Task 2 test `test_dead_forward_is_cleared_and_port_reusable`).
5. A VS Code terminal with `BROWSER` already set to VS Code's helper: zshenv leaves it, and the `xdg-open` stand-in
   hands the URL to it (Task 1 test `test_xdg_open_defers_to_foreign_browser`).

---

### Task 1: The Codespace helper and its setup (stream A, one builder)

**Files:**
- Create: `templates/tools/open-on-mac` (Python 3, executable)
- Modify: `devcontainer_setup/render.py` (`TOOL_ASSETS` gains `open-on-mac`)
- Modify: `templates/scripts/install-tools.sh` (install, `--check`, `--update`)
- Modify: `templates/scripts/system-path.sh` (optional third argument)
- Modify: `templates/scripts/post-create-selected.sh:3-6` (pass it; recovery message names it)
- Modify: `templates/scripts/validate-selected.sh` (check it)
- Test: `tests/test_open_on_mac.py` (new), `tests/test_system_path.py`, `tests/test_install_tools.py`,
  `tests/test_render.py`

**Interfaces:**
- Produces: the shared contract's request files and heartbeat reader; `BROWSER=/home/vscode/.local/bin/open-on-mac`
  in `/etc/environment`; `~/.local/bin/xdg-open` → `open-on-mac`.
- `open-on-mac` honours two test-only variables: `DEVCONTAINER_LINKS_STATE` (state dir) and
  `DEVCONTAINER_LINKS_PROC` (proc root), so tests run on macOS with a fake `/proc`.

- [ ] **Step 1: Write the failing tests in `tests/test_open_on_mac.py`**
  - `test_writes_one_request_mode_600_and_exits_0_when_listening`: fresh heartbeat; run with an `https` URL; exit 0,
    empty stdout and stderr, exactly one `requests/*.json` with mode 600 and the schema-1 keys and values.
  - `test_exits_1_and_writes_link_to_tty_for_plaud_when_no_mac`: heartbeat 20 s old; fake proc chain
    helper → `sh` (fds `/dev/null`) → `node /home/vscode/.local/bin/plaud login` (fd 2 = a temp file standing in
    for the tty); exit 1; the temp file contains the URL on its own line; a request file is still written.
  - `test_never_writes_to_self_showing_programs`: same, with the requester `claude`; exit 1; the tty file is empty.
  - `test_rejects_non_http_without_writing`: `file:///etc/passwd` and `javascript:x` exit 1, no request.
  - `test_program_name_rules`: `['node','/x/plaud','login']`→`plaud`, `['/opt/az/bin/python3','-Im','azure.cli','login']`
    →`az`, `['codex','login']`→`codex`, `['/usr/bin/gh','auth']`→`gh`.
  - `test_xdg_open_defers_to_foreign_browser`: invoked as `xdg-open` with `BROWSER` set to a stub that records its
    argument; the stub receives the URL; no request file.
  - `test_finishes_within_a_second`: wall time under 1 s with 50 existing request files present.
- [ ] **Step 2: Run them and see them fail**
  Run: `python3 -m unittest tests.test_open_on_mac -v` · Expected: errors, `templates/tools/open-on-mac` missing.
- [ ] **Step 3: Implement `templates/tools/open-on-mac`**
  `main(argv: list[str], env: dict) -> int`. Invoked as `xdg-open` (basename of `argv[0]`) with a `BROWSER` that is
  neither unset nor this file: `os.execv` it with the URL. Exit 1 on a non-`http(s)` or empty argument. Write the
  request (shared contract), then exit 0 if the heartbeat is fresh; else write `\r\nOpen this link on your Mac:
  <url>\r\n` to the requester's tty unless its program is self-showing, and exit 1. Every failure path exits
  without output.
- [ ] **Step 4: Tests for the setup scripts**
  - `tests/test_system_path.py`: `test_third_argument_writes_browser_line_once` (run twice; one `BROWSER=` line in
    `/etc/environment`, an existing one replaced), `test_zshenv_sets_browser_only_when_unset` (block contains
    `: ${BROWSER:=/home/vscode/.local/bin/open-on-mac}` and `export BROWSER`), `test_two_arguments_leave_browser_alone`
    (an existing `BROWSER=x` line and zshenv stay byte-identical), `test_rejects_relative_or_multiline_helper_path`.
  - `tests/test_install_tools.py`: `test_installs_open_on_mac_with_absolute_python` (first line is `#!` plus the
    absolute `python3` found at install time; mode 755; `xdg-open` symlink to it), `test_check_fails_when_helper_missing`,
    `test_rerun_keeps_helper_identical`.
  - `tests/test_render.py`: the generated project contains `.devcontainer/tools/open-on-mac` and its
    `post-create-selected.sh` passes `"$HOME/.local/bin/open-on-mac"` as the third argument; this repo's
    `.devcontainer/scripts/post-create.sh` still passes two.
- [ ] **Step 5: Implement the script changes**
  `system-path.sh <caller-path> <python> [<browser-helper>]`: usage and argument count accept 2 or 3; the helper must
  be absolute, one line, no whitespace or quotes; write `BROWSER="<path>"` (replace an existing `BROWSER=` line or
  append) and add the two lines inside the existing marked zshenv block. `install-tools.sh` installs the helper
  before ccswap and Plaud, and `--check` verifies content, mode and link. `validate-selected.sh` reports a missing
  helper, a wrong link or a missing `BROWSER=` line like its other checks.
- [ ] **Step 6: Run the gates**
  Run: `task check && task test` · Expected: exit 0; the new tests pass; each new test fails with its feature
  reverted (record one line per test).
- [ ] **Step 7: Commit**
  `git commit -m "Open Codespace links through a helper that queues them for the Mac"`

### Task 2: The Mac handler (stream B, builder B, first)

**Files:**
- Create: `devcontainer_setup/project_sync/links.py`
- Test: `tests/test_links.py` (new)

**Interfaces:**
- Produces, in `links.py`:
  - `@dataclass(frozen=True) class Request: id: str; url: str; host: str; port: int | None; kind: str  # 'signin'|'loopback'|'link'; program: str; tty: str | None; created_at: float`
  - `parse_request(raw: bytes) -> Request` (raises `ValueError` with a short reason; never echoes the URL)
  - `deadline(request: Request) -> float | None` (None for `loopback`)
  - `check_port(port: int) -> tuple[bool, str | None]` (free?, holder description)
  - `class Forwards: __init__(self, state: Path, owner: str); start(self, port: int) -> None; stop(self, port: int) -> None; active(self) -> set[int]; end_left_behind(self) -> list[int]`
  - `remote_listening(connection, port: int) -> bool` (reads `/proc/net/tcp` and `/proc/net/tcp6` through `connection.python`)
  - `handle(request: Request, connection, forwards: Forwards, *, project: str, now=time.time, opener=open_url, notifier=notify_link) -> str` (an outcome word for logs: `opened`, `forwarded`, `busy`, `expired`, `rejected`)
  - `open_url(url: str, run=subprocess.run) -> bool` (`/usr/bin/open` with an argument list)
  - `notify_link(project: str, program: str, host: str, detail: str = '') -> None` (calls `notify.notify`, then
    `herdr notification show <title> --body <text>` when `herdr status` exits 0; both failures ignored)
  - `REMEDIES: dict[str, str]` with exactly the spec's busy-port table rows.

- [ ] **Step 1: Write the failing tests in `tests/test_links.py`** (fakes for ssh, `open`, `lsof`, the connection)
  - `test_parse_kinds_and_ports`: Plaud URL with `redirect_uri=http://localhost:8199/auth/callback` → `signin`, 8199;
    `http://localhost:5173/` → `loopback`, 5173; `https://github.com/login/device` → `link`, None; az's
    `redirect_uri=http://localhost:38943` → `signin`, 38943.
  - `test_parse_rejects`: `ftp:`, `file:`, 8193-byte URL, port 80 and 70000, schema 2, missing keys.
  - `test_non_loopback_redirect_opens_without_forward` (Review Focus 1).
  - `test_deadlines`: link 30 s, plaud sign-in 120 s, codex sign-in 600 s, loopback None; an expired request is not
    opened and returns `expired`.
  - `test_busy_port_names_holder_and_remedy`: `lsof` fake reports `plaud-mcp (pid 4242)` on 8199 → no ssh started,
    notice text contains the holder and `REMEDIES['plaud']`, returns `busy`.
  - `test_wildcard_listener_counts_as_busy` (Review Focus 3).
  - `test_forward_argv_and_proof`: the ssh argv equals the Global Constraints string for P; `open` runs only after the
    listen proof; record file holds pid, process identity, owner and its identity.
  - `test_forward_stops_when_remote_listener_goes`: `remote_listening` true twice then false → process group ended,
    record removed.
  - `test_dead_forward_is_cleared_and_port_reusable` (Review Focus 4).
  - `test_end_left_behind_spares_live_owner`: records from a dead owner are ended by fact; a live owner's are kept.
  - `test_second_request_same_port_reuses_forward`: a second sign-in for a port already forwarded opens its URL with
    no second ssh; a different port runs beside it.
  - `test_busy_message_goes_to_tty_only_for_non_self_showing`: on a busy port, `handle` writes one line naming the
    holder and remedy to `request.tty` through `connection.python` for `plaud`, and nothing for `codex`.
  - `test_logs_and_notices_never_contain_url`: capture every log record and notice for a sign-in; none contains the
    URL's query string.
- [ ] **Step 2: Run them and see them fail**
  Run: `python3 -m unittest tests.test_links -v` · Expected: `ModuleNotFoundError: devcontainer_setup.project_sync.links`.
- [ ] **Step 3: Implement `links.py`**
  Reuse `state.process_identity`, `state.atomic_write` and the `keepalive._holder`/`_ours` approach for proof by
  fact (copy the pattern; do not import keep-alive's offset logic). `check_port` test-binds `127.0.0.1:P` and, when
  the host is `localhost`, `::1:P`, and treats any `lsof -nP -iTCP:P -sTCP:LISTEN` hit as busy. Records live in
  `<state>/links-forwards.json`.
- [ ] **Step 4: Run the tests and the gates**
  Run: `task check && task test` · Expected: exit 0.
- [ ] **Step 5: Commit**
  `git commit -m "Open Codespace links on the Mac and forward sign-in callbacks"`

### Task 3: The watcher, the worker hook and `task work:signin` (stream B, builder B, after Task 2)

**Files:**
- Modify: `devcontainer_setup/project_sync/links.py` (watcher and `signin_main`)
- Modify: `devcontainer_setup/project_sync/scratch.py` (inside `supervise`'s `with CodespaceConnection(...)` block)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`--signin` in the `work` action group)
- Modify: `templates/continuity-tasks.yml` (`work:signin`, `interactive: true`)
- Test: `tests/test_links_watcher.py` (new), `tests/test_project_sync_cli.py`, `tests/test_root_tasks.py`

**Interfaces:**
- Consumes: Task 2's `parse_request`, `handle`, `Forwards`; the shared contract.
- Produces:
  - `REMOTE_WATCHER: str` (Python source run as `python3 -I -B -c` on the Codespace: every 250 ms or less, claim new
    requests by rename into `claimed/`, print one JSON line each, flush, delete the claimed file; touch
    `mac-listening` every 5 s; skip and log symlinks, files over 64 KB and non-regular files; discard requests older
    than 600 s; exit when stdin reaches EOF).
  - `class Watcher(threading.Thread): __init__(self, connection, state: Path, project: str, logger); stop(self) -> None`
    (writes `<state>/links-watcher.json` with pid, process identity and `heartbeat_at` every 5 s)
  - `watcher_live(state: Path) -> bool` (heartbeat within 15 s and process identity matches)
  - `signin_main(root: Path, state: Path) -> int`

- [ ] **Step 1: Write the failing tests**
  - `tests/test_links_watcher.py`: run `REMOTE_WATCHER` locally against a temp state dir (it touches only that
    dir through `HOME`): `test_watcher_claims_once_and_streams_json` (two concurrent readers; each request emitted
    exactly once), `test_watcher_skips_bad_request_files` (Review Focus 2), `test_watcher_heartbeat_and_eof_exit`,
    `test_thread_restarts_stream_with_backoff` (fake ssh exits; restarts at 2 s then 4 s, injected clock),
    `test_supervise_starts_and_stops_watcher_with_connection` (patch `CodespaceConnection`; the watcher starts after
    `ssh-config` is written and stops when the block exits by exception).
  - `test_signin_refuses_while_watcher_live`, `test_signin_handles_only_signins_and_exits_when_done`,
    `test_stop_ends_owned_forwards` (`Watcher.stop()` and SIGINT/SIGTERM/SIGHUP in `signin_main` end every forward
    their `Forwards` owns, and only those).
  - `tests/test_project_sync_cli.py`: `work --signin` dispatches to `signin_main`.
  - `tests/test_root_tasks.py`: the rendered Taskfile has `work:signin` with a `desc` and `interactive: true`.
- [ ] **Step 2: Run them and see them fail**
  Run: `python3 -m unittest tests.test_links_watcher tests.test_project_sync_cli tests.test_root_tasks -v`
- [ ] **Step 3: Implement**
  Start the `Watcher` right after `refresh_ssh(state)` in `supervise`, and stop it in a `try/finally` that wraps the
  rest of the block. Its stream is `ssh -F <state>/ssh-config -T project-sync python3 -I -B -c <REMOTE_WATCHER>`.
  `signin_main` opens its own `CodespaceConnection`, refuses with "The connected worker is already handling links."
  while `watcher_live`, serves `signin` requests only, and exits 0 when none is pending or forwarded.
- [ ] **Step 4: Run the gates**
  Run: `task check && task test` · Expected: exit 0.
- [ ] **Step 5: Commit**
  `git commit -m "Watch Codespace link requests while connected and add task work:signin"`

### Task 4: Definition of done (lead)

**Files:** `tmp/codespace-lifecycle/link-helper/LIVE.md` (lead's log, gitignored)

- [ ] **Step 1:** Merge Task 1 and Tasks 2–3 into local main after review and a root re-gate
  (`verify/<name>-<sha>.log`, exit 0).
- [ ] **Step 2:** Render an innovate test branch from local main (memory `live-test-unpushed-generator`), create an
  unpaired basicLinux32gb test Codespace, connect it with `task work:cs` from a fresh clone's state (no arming).
- [ ] **Step 3:** Run the spec's Definition of done and live tests 5–8. The owner approves each browser sign-in
  (Plaud, Codex, Claude, OpenCode ChatGPT, gh); everything else the lead drives.
- [ ] **Step 4:** Delete the test Codespace, branch, clone and state dir. Push main (standing OK), roll out to the
  three projects (ruling 23, memory `project-setup-upgrade-recipe`).
