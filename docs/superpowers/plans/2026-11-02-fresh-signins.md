# Fresh sign-ins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo the lead runs it through the herd (global CLAUDE.md "Herd layout"): two Codex builders in parallel, one per part below.

**Goal:** After a Codespace is created or rebuilt and paired, one foreground command signs in Plaud, Codex, Claude (one account), OpenCode and Grok on it, each page opening on the Mac; setup reruns stop defaulting to a rebuild.

**Architecture:** A new vendored module `project_sync/signins.py` borrows the worker's live link watcher (it already opens every Codespace link on the Mac), checks each tool's status on the Codespace with one SSH call that returns only a state word, and runs each missing login in an interactive `ssh -t` session. `task work:signin:all` runs it alone; `work --codespace --then-signin-all` runs it after the worker is ready and before Herdr opens, and the wizard calls that as a subprocess after it pairs a created or rebuilt Codespace.

**Tech Stack:** Python 3 stdlib only (the runtime is vendored and run with `python3 -I -B`), go-task, unittest.

**Spec:** `docs/superpowers/specs/2026-11-02-fresh-signins-design.md` (owner answers: rulings 25-28 in `tmp/codespace-lifecycle/ux-run-2026-10-25/RULINGS.md`). Evidence: `tmp/codespace-lifecycle/signins/analysis/REPORT.md` (REPORT).

## Global Constraints

- Nothing is copied: no token, auth file, setup-token or ccswap export. The sequence runs no ccswap command (design call 10).
- The Mac never prints, logs or stores a URL, device code, token or a CLI's raw status output. Only a state word and, for Claude, the email reach the Mac.
- A tool already signed in, or whose status is unknown, is never logged in again (`codex login` revokes the existing login first, REPORT "Codex").
- No login starts before both heartbeats are fresh: the Mac's `watcher_live(state)` (`links.py:447-454`) and the Codespace's `~/.local/state/devcontainer-links/mac-listening` modified within 15 s (`templates/tools/open-on-mac:100`).
- `signin_main` and `task work:signin` keep their callback-only contract; `tests/test_links_watcher.py:355-373,419-427` stay unchanged.
- The wizard never imports `project_sync.workspace` to connect; it runs the checkout's own `project-sync.sh` (a worker started from the release cache reads as not current: `state.py:310-322`, `workspace.py:143-146`).
- New Mac files are written with `state.atomic_write` inside the private state directory.
- Commits: plain imperative sentence case, one concern each, no AI attribution. Gates: `task check` and `task test` green.

## Review Focus

1. **Ctrl-C during a login** → the remote tool gets it through the PTY, `ssh` exits 130; the sequence prints the summary, writes the record with what finished, leaves no `ssh` child, and exits 130. (Task 2: `test_cancel_stops_sequence_and_keeps_record`.)
2. **Worker connected but the Codespace heartbeat is stale** (the watcher is reconnecting) → wait up to 30 s, then refuse naming `task work:cs`; zero logins started. (Task 2: `test_stale_remote_heartbeat_refuses_after_wait`.)
3. **A status check hangs** (network) → the Codespace-side 30 s timeout makes it `unknown`; it is skipped and listed, never logged in. (Task 1: `test_probe_timeout_is_unknown`.)
4. **Codex signed in with an API key** → `other-method`, left as is, no `codex login`. (Task 1: `test_codex_api_key_is_other_method`; Task 2: `test_skips_signed_in_other_method_and_unknown`.)
5. **The wizard offers sign-in but the checkout was not fast-forwarded** (the vendored runtime has no `signins.py`) → no subprocess runs; it prints the update and the two commands. (Task 5: `test_offer_without_updated_checkout_prints_commands`.)

---

## Part 1, builder 1: the runtime (Tasks 1-3, one branch)

### Task 1: Tool table, Codespace-side status probe and the record

**Files:**
- Create: `devcontainer_setup/project_sync/signins.py`
- Test: `tests/test_signins.py`

**Interfaces:**
- Produces:
  - `Tool` (frozen dataclass): `key: str`, `label: str`, `binary: str`, `status: tuple[str, ...]`, `login: tuple[str, ...]`, `deadline: int` (seconds).
  - `TOOLS: tuple[Tool, ...]`, in this order and with these exact argv:
    - `plaud` / `Plaud`: status `('plaud', 'me')`, login `('plaud', 'login')`, deadline 150
    - `codex` / `Codex`: status `('codex', 'login', 'status')`, login `('codex', 'login')`, deadline 600
    - `claude` / `Claude`: status `('claude', 'auth', 'status', '--json')`, login `('claude', 'auth', 'login', '--claudeai')` (no `--email`, ruling 25), deadline 600
    - `opencode` / `OpenCode`: status `('opencode', 'auth', 'list', '--format', 'json')`, login `('opencode', 'auth', 'login', 'openai', '--method', 'chatgpt-token-sharing')`, deadline 600
    - `grok` / `Grok`: status `('grok', 'models')`, login `('grok', 'login', '--device-auth')`, deadline 600
  - States: `SIGNED_IN = 'signed-in'`, `SIGNED_OUT = 'signed-out'`, `OTHER_METHOD = 'other-method'`, `UNKNOWN = 'unknown'`, `ABSENT = 'absent'`.
  - `_classify(key: str, code: int, out: str, err: str) -> tuple[str, str | None]`: a module-level function with no imports beyond `json`, so its source can be shipped.
  - `REMOTE_PROBE: str`: `inspect.getsource(_classify)` plus a driver. The driver takes tool keys and their status argv as JSON in `sys.argv[1]`. For each tool it runs `shutil.which` (missing → `absent`), then `subprocess.run(argv, capture_output=True, text=True, timeout=30)` (timeout or OSError → `unknown`). It prints one JSON line `{key: {"state": ..., "identity": ...}}`.
  - `probe(state: Path, keys: Sequence[str]) -> dict[str, dict]`: runs `ssh -F <state>/ssh-config -T project-sync bash -lc <quoted "python3 -I -B -c <REMOTE_PROBE> <json>">` with a 120 s timeout. An SSH failure, a timeout or unparsable output makes every key `{'state': 'unknown', 'identity': None}`.
  - `write_record(state: Path, codespace: str, results: dict[str, dict], now: float) -> None` → `state/signins.json`: `{"schema": 1, "codespace": codespace, "updated_at": now, "tools": {key: {"state": s, "checked_at": now, "identity": email-or-absent}}}`.

`_classify` rules, exact:
- plaud: code 0 → signed-in; code 2 → signed-out; anything else → unknown.
- codex (its text is on stderr): code 0 and the line `Logged in using ChatGPT` → signed-in; code 0 otherwise → other-method; code 1 and the line `Not logged in` → signed-out; else unknown.
- claude: parse stdout JSON; `authMethod == "claude.ai"` and `loggedIn` true → signed-in with identity `email`; `loggedIn` false → signed-out; `loggedIn` true with another `authMethod` → other-method; bad JSON → unknown.
- opencode: code 0 and stdout parses to a list → signed-in if any element has `id == "openai"`, else signed-out; else unknown.
- grok (exit 0 in both cases): output contains `You are logged in with grok.com.` → signed-in; contains `You are not authenticated.` → signed-out; else unknown.

- [ ] **Step 1: Write the failing tests** in `tests/test_signins.py`:
  - `test_tools_order_and_argv`: `[t.key for t in TOOLS] == ['plaud', 'codex', 'claude', 'opencode', 'grok']`, each argv exactly as listed, and `'--email' not in` Claude's login.
  - `test_classify_table`: one `subTest` per row above (incl. `('plaud', 3, '', '')` → unknown; `('grok', 0, 'You are not authenticated.\n', '')` → signed-out; `('opencode', 0, '[]', '')` → signed-out; Claude `{"loggedIn": true, "authMethod": "claude.ai", "email": "a@b.c"}` → `('signed-in', 'a@b.c')`).
  - `test_codex_api_key_is_other_method`: `_classify('codex', 0, '', 'API key configured - sk-…\n')` → `('other-method', None)`.
  - `test_probe_script_runs_locally_with_fake_tools`: put executable fakes for `plaud` (exit 2) and `grok` (prints the signed-in line) in a temp dir. Run `python3 -I -B -c REMOTE_PROBE <json>` with `PATH` set to that dir and the interpreter's own dir; `codex` is missing. Expect plaud signed-out, grok signed-in, codex absent.
  - `test_probe_timeout_is_unknown`: a fake `plaud` that sleeps past a patched 1 s timeout → unknown.
  - `test_probe_ssh_failure_is_unknown`: patch `subprocess.run` to return code 255 → every key unknown.
  - `test_probe_argv`: the SSH argv starts `['ssh', '-F', str(state / 'ssh-config'), '-T', 'project-sync', 'bash', '-lc']`.
  - `test_record_holds_only_state_and_identity`: after `write_record` with a Claude identity, the file has only the keys listed above, its text contains no `http` and nothing matching `[A-Za-z0-9_-]{32,}`, and its mode is private.

- [ ] **Step 2: Run** `python3 -m unittest tests.test_signins -v`. Expected: ERROR, `ModuleNotFoundError: devcontainer_setup.project_sync.signins`.
- [ ] **Step 3: Implement** the interfaces above in `devcontainer_setup/project_sync/signins.py`. The driver discards each tool's raw output on the Codespace, after `_classify`.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_signins -v`. Expected: all pass.
- [ ] **Step 5: Commit** `git add devcontainer_setup/project_sync/signins.py tests/test_signins.py && git commit -m "Check each tool's sign-in on the Codespace and keep only its state"`

### Task 2: The sequence

**Files:**
- Modify: `devcontainer_setup/project_sync/signins.py`
- Test: `tests/test_signins.py`

**Interfaces:**
- Consumes: Task 1's `TOOLS`, `probe`, `write_record`, the states; `links.watcher_live(state)`; `state.locked`.
- Produces:
  - `ready(state: Path, *, wait: float = 30) -> str | None`: None when ready, else the refusal text. It polls every 1 s until `watcher_live(state)` is true and the Codespace's `mac-listening` is fresh (one SSH `python3 -I -B -c` read of its age, ≤ 15 s).
  - `login(state: Path, tool: Tool) -> int`: `subprocess.Popen(['ssh', '-F', str(state / 'ssh-config'), '-t', 'project-sync', 'bash', '-lc', shlex.quote(shlex.join(tool.login))], start_new_session=False)`, inheriting the terminal. It waits up to `tool.deadline`; on the deadline it sends SIGTERM, then SIGKILL after 5 s, and returns 124. It returns ssh's exit code otherwise.
  - `run(project, state: Path, codespace: str) -> int`: the whole sequence; 0 when every installed tool ends signed-in or other-method, 1 otherwise, 130 on cancel.

Order in `run`: take `state / 'signins.lock'` without blocking. If it is held, print `Another sign-in is already running for this project.` and return 1. Then call `ready()`; on a refusal, print it and return 1. Then `probe` all keys. For each tool in `TOOLS`: signed-in, other-method, unknown or absent → print its line and skip it. Signed-out → print the before line, run `login`, then `probe([key])` again (the probe is the truth, not the exit code). A login exit of 130 → stop the sequence. At the end, `write_record` and the summary.

Copy, exact (`{T}` is the label):
- Not connected: `Sign-in needs this project connected to its Codespace. Run task work:cs, then task work:signin:all.`
- Codespace not listening after the wait: `The Codespace is not reaching this Mac's link helper yet. Run task work:cs again, then task work:signin:all.`
- Before a login: `Signing in to {T}. Approve the page that opens on this Mac.` (Grok adds ` Enter the code shown here.`)
- Already signed in: `{T}: already signed in` (Claude: `Claude: already signed in as {email}`)
- After a login that worked: `{T}: signed in` (Claude: `Claude: signed in as {email}`)
- Login not finished: `{T}: not signed in (timed out or callback port busy: see the Mac notice)` (design §6)
- Other method: `{T}: signed in another way; left as is`
- Unknown: `{T}: could not check sign-in; skipped`
- Absent: `{T}: not installed; skipped`
- Summary: `Signed in: {labels}.`; when any are missing, also `Not signed in: {labels}. Retry with task work:signin:all.` On cancel, first `Sign-in cancelled during {T}.`

- [ ] **Step 1: Write the failing tests** (patch `probe`, `login`, `ready`, `watcher_live`; capture stdout):
  - `test_not_connected_refuses_without_ssh`: `watcher_live` false and `wait=0` → the not-connected text, exit 1, `login` never called.
  - `test_stale_remote_heartbeat_refuses_after_wait`: `watcher_live` true and the remote age 40 s → the not-listening text after the patched wait; `login` never called.
  - `test_signs_in_only_signed_out_tools_in_order`: probe says plaud signed-out and the rest signed-in → `login` called once with Plaud; the second probe covers `['plaud']` only; exit 0.
  - `test_skips_signed_in_other_method_and_unknown`: codex other-method, opencode unknown → neither logged in; their lines printed; exit 1 because opencode is unknown.
  - `test_failed_login_continues_and_lists_retry`: grok's re-probe still signed-out → the not-finished line; later tools still run; the summary contains `Not signed in: Grok. Retry with task work:signin:all.`; exit 1.
  - `test_cancel_stops_sequence_and_keeps_record`: `login` returns 130 for codex → claude is not attempted; `Sign-in cancelled during Codex.` printed; the record holds plaud's result; exit 130.
  - `test_deadline_terminates_login`: a real child (`sleep 30`) with a 1 s deadline → returns 124 and the process is gone.
  - `test_login_argv`: the argv is exactly `['ssh', '-F', str(state / 'ssh-config'), '-t', 'project-sync', 'bash', '-lc', "'grok login --device-auth'"]` for Grok.
  - `test_term_ends_current_login`: SIGTERM or SIGHUP to the sequence while `login` runs a real child → the child is gone, the summary prints, and the exit is 128 + the signal number. A `KeyboardInterrupt` outside a login (during a probe) takes the cancel path.
  - `test_second_run_is_refused_while_locked`: holding `signins.lock` → `Another sign-in is already running for this project.`, exit 1.
  - `test_signin_main_still_callback_only`: run the existing `tests/test_links_watcher.py` and confirm it is untouched (`git diff --quiet -- tests/test_links_watcher.py`). The device link Grok opens is served by the worker's `Watcher`, whose ordinary-link tests already pin it. This plan adds no second dispatcher.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_signins -v`. Expected: the new tests FAIL (`run`/`ready`/`login` missing).
- [ ] **Step 3: Implement** `ready`, `login`, `run` with the copy above. Signal handling follows `signin_main` (`links.py:673-723`): handlers set for SIGINT, SIGTERM and SIGHUP, restored on exit.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_signins tests.test_links_watcher -v`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -am "Sign in each missing tool on the paired Codespace through the live link watcher"`

### Task 3: Commands, task and docs

**Files:**
- Modify: `devcontainer_setup/project_sync/__main__.py:28-46` (parser), `:353-356` (dispatch), `main()` (modifier check)
- Modify: `devcontainer_setup/project_sync/workspace.py:205` (`remote`), `:274-277`
- Modify: `templates/continuity-tasks.yml` (after `work:signin`)
- Modify: `devcontainer_setup/root_tasks.py:6-9` (`ALIASES`, `INTERACTIVE`)
- Modify: `docs/project-continuity.md` (the sign-in section), `README.md` (where `task work:signin` is named)
- Test: `tests/test_project_sync_cli.py`, `tests/test_root_tasks.py`, `tests/test_continuity_integration.py` if it pins the task list

**Interfaces:**
- Consumes: Task 2's `signins.run(project, state, codespace) -> int`.
- Produces (Part 2 depends on this exact argv): `bash .devcontainer/scripts/project-sync.sh work --codespace --then-signin-all`, run with cwd = the project checkout. It connects as `task work:cs` does (resuming recovery when armed). Once the worker is ready, after the `armed` callback and before `open_remote`, it runs `signins.run`. A sign-in result never fails the connect: Herdr opens either way.
  - `workspace.remote(project, state, armed=None, rebind=None, before_herdr=None)`: `before_herdr(primary, name)` runs right after `armed(primary)` (`workspace.py:274-277`), outside `workspace-control.lock`; for `--then-signin-all` it calls `signins.run(primary.root, state, name)`.
  - Action `--signin-all` (help `Sign in to every tool on the paired Codespace`), dispatched after the terminal gate (`__main__.py:350`): `signins.run(project.root, state, <binding codespace>)`. With no binding, it prints the not-connected text and returns 1.
  - Modifier `--then-signin-all`, valid only with `--codespace`. Otherwise it fails the same way `--rebind` does: `commands.error('--then-signin-all works only with task work:cs')`.
  - Task `work:signin:all`: desc `Sign in to every tool on the paired Codespace`, `interactive: true`, cmd `bash .devcontainer/scripts/project-sync.sh work --signin-all {{.CLI_ARGS}}`. It is a root alias and interactive.

- [ ] **Step 1: Write the failing tests:**
  - `test_project_sync_cli.py::test_work_signin_all_dispatches_to_signins_run`, modeled on `:549-559`: `run` is called once with `(self.root, self.state, 'paired')`.
  - `::test_then_signin_all_requires_codespace`: `work --stop --then-signin-all` → exit 2 with the error text.
  - `::test_codespace_then_signin_all_runs_before_herdr`: patch `open_remote` and `signins.run` to append to one list → order `['signins', 'herdr']`. Armed: `['armed', 'signins', 'herdr']`. `signins.run` returning 1 still reaches `open_remote`.
  - `test_root_tasks.py::test_work_signin_all_renders_description_interactive_and_root_alias`, modeled on `:7-21`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_project_sync_cli tests.test_root_tasks -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the parser, dispatch, `remote(ready=)`, task and aliases. In the docs, name `task work:signin:all` as the start/retry command, and keep `task work:signin` as "finish waiting sign-ins".
- [ ] **Step 4: Run** `task check && task test`. Expected: exit 0 for both.
- [ ] **Step 5: Commit** `git commit -am "Add task work:signin:all and sign in before Herdr opens on request"`

## Part 2, builder 2: the wizard (Tasks 4-5, one branch)

### Task 4: Reruns leave a paired Codespace as it is (ruling 28)

**Files:**
- Modify: `devcontainer_setup/__main__.py:745-758` (`_resolve_launch`), `:853-854` (`_codespace_choice` default)
- Modify: any doc line saying a rerun defaults to rebuilding (`grep -rn -i "default.*rebuild\|rebuild.*default" README.md docs/`)
- Test: `tests/test_wizard.py`

**Interfaces:** none new. `_resolve_launch` returns `_no_codespace()` for a usable paired Codespace, before its reuse loop. A paired Codespace on another branch or repository, or an unreadable one, keeps today's fallback. `_codespace_choice` selects `'none'` when the paired one is usable; `rebuild <paired>` stays listed.

- [ ] **Step 1: Change the tests that pin ruling 3 on purpose, and add ruling 28's:**
  - `test_paired_defaults_to_rebuild_with_force` (`:512`) becomes two tests. `test_paired_defaults_to_none_with_rebuild_listed`: `menus[0][2] == 'none'`, `'rebuild paired' in menus[0][1]`. `test_choosing_rebuild_forces_rebuild_on_paired`: the UI picks `rebuild paired` → `force_rebuild is True`, `resume == 'paired'`, machine `premiumLinux`, idle `60m`.
  - `test_existing_codespace_menu_defaults_to_the_paired_codespace` (`:1100`): selected becomes `('none',)`; options unchanged (`['none', 'rebuild second', 'reuse first']`).
  - `test_paired_premium_rebuilds_on_premium_despite_stale_receipt` (`:218`): re-point from `_resolve_launch` to the explicit choice (`_reuse_options(api, 'paired', 'main', 'rebuild', space)`), keeping its premium/60m assertion and `api.machines.assert_not_called()` (ruling 22 stays covered).
  - New `test_resolve_launch_keeps_usable_paired_as_none`: `_resolve_launch(api, root, 'main', 'paired')['action'] == 'none'`, and `api.existing` is not consulted for another reuse.
  - New `test_resolve_launch_unusable_paired_keeps_fallback`: paired on another branch → today's warning plus reuse of the matching unpaired one.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_wizard -v`. Expected: the changed and new tests FAIL.
- [ ] **Step 3: Implement** the two default changes.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_wizard -v`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -am "Leave a paired Codespace as it is by default when setup reruns"`

### Task 5: Offer to connect and sign in after create or rebuild

**Files:**
- Modify: `devcontainer_setup/__main__.py:930-931` (`setup_continuity`, right after `service.pair`)
- Test: `tests/test_wizard.py`

**Interfaces:**
- Consumes (from Part 1, as text only): argv `['bash', '.devcontainer/scripts/project-sync.sh', 'work', '--codespace', '--then-signin-all']`, cwd = `project`, and the marker file `project / '.devcontainer/scripts/project_sync/signins.py'`.
- Produces: after `service.pair(codespace, root)` the wizard runs `ui.confirm('Connect and sign in now?', True)`. On yes, and when the marker exists, it runs `subprocess.call(argv, cwd=project)`, which prints its own outcome. A nonzero code adds `Setup is preserved. Run task work:cs, then task work:signin:all.` On no it prints `Codespace paired. Run task work:cs to connect, then task work:signin:all to sign in.` Without the marker it prints `Update this checkout (git -C {project} pull --ff-only), then run task work:cs and task work:signin:all.` A `Cancelled` from the confirm is treated as no.

- [ ] **Step 1: Write the failing tests:**
  - `test_pairing_offers_connect_and_sign_in`: create path, confirm yes, marker present → `subprocess.call` called once with the exact argv and `cwd=project`.
  - `test_rebuild_of_paired_offers_sign_in`: binding on the same Codespace (the rebuild path, `root = binding['remote_root']`) → the same offer and argv.
  - `test_failed_connect_prints_recovery`: `subprocess.call` returns 1 → `Setup is preserved. Run task work:cs, then task work:signin:all.`
  - `test_declining_prints_both_commands`: confirm no → no subprocess; output contains `task work:cs` and `task work:signin:all`.
  - `test_offer_without_updated_checkout_prints_commands`: marker absent → no subprocess; output contains `pull --ff-only`.
  - `test_no_offer_when_pairing_stays_elsewhere`: binding on `other` (`setup_continuity:915-922`) → `ui.confirm` is never called with the sign-in prompt.
  - `test_no_offer_inside_a_codespace`: `CODESPACES=true` → no prompt.
  - `test_wizard_does_not_import_workspace_to_connect`: the offer path never touches `devcontainer_setup.project_sync.workspace.remote` (patch it to raise).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_wizard -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the offer.
- [ ] **Step 4: Run** `task check && task test`. Expected: exit 0 for both.
- [ ] **Step 5: Commit** `git commit -am "Offer to connect and sign in after setup pairs a new or rebuilt Codespace"`

## After both parts (lead)

1. Review each branch's diff against `git merge-base origin/main HEAD`. Merge both into main. Re-run `task check` and `task test` in the default environment, logged to `tmp/codespace-lifecycle/signins/verify/<name>-<sha>.log` with the exit code.
2. Live test (rulings 20, 32; recipe: memory `live-test-unpushed-generator`; backups as `tmp/codespace-lifecycle/keeper-safety/T4-RUNBOOK.md`, the way `tmp/codespace-lifecycle/link-helper/LIVE.md` did):
   - a fresh clone of innovate on a test branch and a throwaway Codespace;
   - the wizard's offer yes → each tool's page on the Mac, Claude as the account the owner picks, Grok's device page;
   - a second `task work:signin:all` prints only "already signed in" lines;
   - Ctrl-C during one login;
   - a rerun opens with `Codespace: none`;
   - cleanup per LIVE.md's last line.
3. Push (standing OK after full gates), then roll out to innovate, mainstage and strata-sap (ruling 23, memory `project-setup-upgrade-recipe`).
