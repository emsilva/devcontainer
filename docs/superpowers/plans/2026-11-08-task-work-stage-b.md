# `task work` stage B: where the project stands, and what to do next. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `task work` prints where the project stands in at most 6 lines of 78 columns, then offers the 3-6 actions
that fit as a Gum menu, the likely one first, each showing the command it runs; without a terminal it prints the state
and `Next:` commands and exits 0. A conversation the paired Codespace refused can be forgotten.

**Architecture:** Four small runtime modules, all vendored into generated projects like every `project_sync/*.py`:
`standing.py` reads local facts without side effects; `probe.py` runs every GitHub and Git read as owned child
processes under one 2-second monotonic deadline; `overview.py` turns facts into lines and actions (pure) and, in Task 4,
drives the printer and menu; `forget.py` records a forgotten conversation, and the outbound capture drops it at the
source. Setup facts that generated projects cannot import (metadata marker, launch receipt, setup PRs, generator tip,
checkout comparison) get small runtime readers, each pinned by a parity test to the setup function it mirrors.

**Tech Stack:** Python 3 standard library (vendored, run as `python3 -I -B`), go-task, `unittest`, Gum (only when
already installed or cached).

**Spec:** `docs/superpowers/specs/2026-11-02-task-work-design.md` (sha256 `b443ce89…`), section "B. `task work`", its
Anti-goals, Constraints, Owner answers 2 (forget) and "Tests the plan must include" (B list). Source inventory with
file:line evidence at 8fa4035: `tmp/codespace-lifecycle/task-work/stage-b/inventory/INVENTORY.md` (sha256
`4b2590068d9f…`, cited below as INV §n). Line numbers below are at 7ecc53c, the stage A tip.

## Global Constraints

- Gates: `task check` and `task test` (`python3 -m unittest discover -s tests -v`), both exit 0, at your commit, run
  in the default environment (no sandbox, no `TMPDIR` override): `tmp/codespace-lifecycle/task-work/stage-a/RULING-gates.md`.
- Focused runs: run `python3 -m unittest …` unpiped (or redirect to a file and read `$?`); a `| tail` hides the exit code.
- Branches: cut from `feature/task-work-ab` at the commit your brief names; merge back into it, never `main`. A and B
  ship together after the live stop and move check (spec call 1).
- Commit messages: plain imperative sentence case, one concern per commit, no AI attribution of any kind.
- **Nothing on the printing path starts, downloads, signs in, spends, writes, or takes a lock.** It never calls
  `workspace.status()`, `workspace.recovery()`, `workspace._choose_pairing()`, `Codespaces.space()`/`spaces()`,
  `lifecycle.LifecycleAPI`, `transport.github_token()`, `arming.account()`, `Store.latest()`/`load()`/`present()`/
  `relay_entries()`, setup's `checkout_update()`/`fetch_release()`, `terminal.gum_binary()` or `terminal.Gum()` without
  a binary (INV §2). No file under the state directory, the checkout or the cache is created or changed.
- A bare HTTP 404, an empty answer, a truncated or failed list, or a list read under another account never means
  "deleted" (spec Anti-goals, #60). Only a complete inventory from the account recorded for the pairing does.
- Do not name a module `work.py`: it is in `render.RETIRED_HELPERS` (`devcontainer_setup/render.py:26-27`).
- Every untrusted string shown (Codespace, branch and PR names, agent and session ids, reasons) is stripped of
  control and format characters before display. Executed commands are argv tuples; shown commands are `shlex.join`.
- Stdlib only. No new key in the user `config.json`. New state files use `state.atomic_write` (private, atomic).
- No client, organization or Codespace names in code, tests, docs or commit messages (the repository is public).
- Times shown are local `HH:MM`, or `YYYY-MM-DD HH:MM` when not today.

## Review Focus

1. **A probe child that ignores SIGTERM or leaves a grandchild** (gh waiting on a keychain prompt, git on a
   credential helper): `join()` still returns by the deadline and nothing it started outlives it (Task 2
   `test_grandchild_is_gone_after_the_deadline`).
2. **Evidence from the previous pairing after a rebind** (keeper runtime, worker cycle or arming naming OLD): never
   labels the current pairing (Task 1 `test_records_for_another_codespace_are_ignored`).
3. **Hostile or wide names**: an escape sequence, a newline or CJK/emoji text in a Codespace, branch or session name
   neither reaches the terminal raw nor breaks 78 cells (Task 3 `test_untrusted_text_is_sanitized_and_fits`).
4. **Ctrl-C or Esc at any point**: during the probe, nothing it started survives and the exit is 130 with
   `Cancelled.`; at the menu, nothing runs and the exit is 0 with `Nothing ran.` (Task 4 tests).
5. **An owner-defined root `work:cs`** (or any alias) that is not the managed one: the menu runs and prints
   `task devcontainer:work:cs`, never the owner's task (Task 3 `test_owner_alias_is_not_assumed_managed`).

---

### Task 1: Local facts

**Files:**
- Create: `devcontainer_setup/project_sync/standing.py`
- Modify: `devcontainer_setup/project_sync/state.py:376-392` (`process_identity`, `owned_process` gain
  `timeout: float = 5`, passed through to `ps`; callers unchanged)
- Test: `tests/test_standing.py` (new)

**Interfaces:**
- Produces, in `standing.py` (all frozen dataclasses):
  - `Checkout`: `default: str`, `branch: str | None`, `relation: str` (one of `current`, `behind`, `ahead`,
    `diverged`, `detached`, `other-branch`, `no-remote-ref`, `unknown`), `behind: int`, `ahead: int`,
    `fetched_at: float | None` (mtime of `git rev-parse --git-path FETCH_HEAD`, else None).
  - `Launch`: `codespace: str`, `complete: bool` (`commit` is 40 hex, `validated_commit == commit`,
    `rebuild_pending is False`).
  - `Refusal`: `agent: str`, `session_id: str`, `reason: str`.
  - `Local`: `root: Path`, `state: Path`, `repository: str` (origin's `owner/repo`, spelling preserved as setup's
    `_github_repository` keeps it), `generated: bool` (this package runs from `<root>/.devcontainer/scripts/project_sync`),
    `in_codespace: bool` (`CODESPACES == 'true'`), `setup: str` (`installed`, `partial`, `missing`, `unmanaged`,
    `unknown`, or `source` when not generated), `source_ref: str | None`, `pairing: str | None`,
    `armed: bool | None` (None = unreadable), `gh_user: str | None` (arming's `gh_user`, else the keeper runtime's,
    each only when it names the pairing), `owner_stop_at: float | None`, `started_at: float | None` (the pairing's
    latest `started_at` in `start-evidence.json`, read raw),
    `hold_until: float | None`, `keeper: str` (`running`, `stale`, `stopped`, `unknown`),
    `keeper_heartbeat_age: float | None`, `reading: tuple[str, float] | None` (keeper's last `(state, at)`),
    `deleted_at: float | None` (keeper `phase == 'deleted'`), `down_kind: str | None` (`OWNER`, `LIFETIME`, `OTHER`),
    `down_at: float | None`, `worker: str` (`running`, `stopped`, `unknown`), `cycle_at: float | None`,
    `refusals: tuple[Refusal, ...]`, `launch: Launch | None`, `checkout: Checkout`, `problems: tuple[str, ...]`.
  - `read(root: Path, *, env: Mapping[str, str] = os.environ, timeout: float = 1.0) -> Local`. Never raises for bad
    state: an unreadable file becomes `unknown`/None plus one `problems` entry naming the file
    (`'arming.json cannot be read'`).
- Rules `read` follows:
  - Every subprocess (git, ps) gets `timeout`; git runs with `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`. A
    timeout makes only that fact `unknown`. No `git status`, `fetch` or `merge`.
  - `setup` (generated only): `missing` without `.devcontainer/devcontainer.json`; `unmanaged` without
    `customizations.devcontainerSetup`; `unknown` on malformed JSON or a `sourceRef` that is not 40 hex; `partial`
    when installed but neither `R/workspace.json` nor `R/scratch.json` exists (spec line 108; test file existence,
    not `workspace.registry()`, which synthesizes defaults); else `installed`.
  - Keeper, worker and arming records count only when they name the current `pairing` (keeper runtime `codespace`,
    worker `binding` equal to `scratch.json`, arming `codespace`); otherwise they are ignored (Review Focus 2).
    Process liveness uses `state.owned_process(..., timeout=timeout)` with the flags `keeper.status` and
    `scratch.status` pass today; `stale` means owned with heartbeat age over `keeper.HEARTBEAT_STALE`.
  - `down_kind`: the keeper record's `down.kind` when present; else `lifecycle.classify()` with the inputs
    `keeper._classify` uses (`keeper.py:513-518`: `last_up_at`, `down.first_at`, `anchor.at` plus `anchor.also`,
    `owner_stop_at`), called directly (the method logs and notifies).
  - `refusals`: from `scratch-runtime.json` `conversations` when `schema == 3`, `completed_at` is set and the worker
    binding matches: `directions.desktop_to_codespace.publication_issues` entries with string `agent` and
    `session_id`. Capture issues are not refusals.
  - `launch`: the receipt at `<setup state home>/devcontainer-setup/<sha256(f'{repo}:{default}')[:16]>.json`, where the
    setup state home is an absolute `XDG_STATE_HOME`, else `~/.local/state` (setup's resolver, not continuity's;
    INV §1). Kept only when `repository == repo`, `branch == default` and `codespace` is a valid name. Never migrated.

- [ ] **Step 1: Write the tests** in `tests/test_standing.py` (temporary Git repos with an `origin`, `XDG_STATE_HOME`
  in a temp dir, fake `ps` through a patched `state.process_identity`):

```python
def test_reading_creates_and_changes_nothing(self):
    # hash every file under the checkout, the state dir and XDG_CACHE_HOME before and after read(); equal;
    # also with no state dir at all (it must still not exist afterwards)

def test_setup_states(self):            # missing, unmanaged, malformed -> unknown, partial, installed, source
def test_records_for_another_codespace_are_ignored(self):
    # scratch.json names 'new-cs'; keeper runtime, worker binding and arming name 'old-cs'
    self.assertIsNone(local.reading); self.assertEqual(local.refusals, ()); self.assertIsNone(local.down_kind)
def test_unreadable_arming_is_unknown_not_unarmed(self):
    self.assertIsNone(local.armed); self.assertIn('arming.json cannot be read', local.problems)
def test_process_check_timeout_is_unknown(self):   # process_identity raises TimeoutExpired -> keeper == 'unknown'
def test_refusals_come_from_publication_issues_only(self)
def test_checkout_relations(self):     # current, behind 2, ahead 1, diverged, detached, other-branch, no-remote-ref
def test_checkout_matches_setup_decision(self):
    # for each fixture, run git_ops.checkout_update on a copy (with a local bare origin so its fetch is offline):
    # behind -> 'updated'; current -> 'current'; diverged -> refused 'diverged'; detached -> 'detached';
    # other-branch -> 'other-branch'; and read() on the original left HEAD, index and refs byte-identical
def test_metadata_matches_setup(self):  # render() output -> source_ref equals __main__._metadata(...)['sourceRef']
def test_launch_receipt_matches_setup(self):
    # path equals codespaces.receipt_path(state_home, repo, default) for absolute XDG_STATE_HOME and the default
    # home, a mixed-case owner/repo and a slash in the branch; complete == bool(__main__._launch_history(...))
    # for complete, unvalidated, rebuild_pending and wrong-branch fixtures
def test_repository_spelling_matches_setup(self):  # equals __main__._github_repository for mixed-case origins
```

- [ ] **Step 2: Run them; they fail** (`standing` does not exist).

Run: `python3 -m unittest tests.test_standing -v`
Expected: exit 1, `ModuleNotFoundError: No module named 'devcontainer_setup.project_sync.standing'`.

- [ ] **Step 3: Implement** `standing.py` as in Interfaces, and the `timeout` keyword in `state.py`.

- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add devcontainer_setup/project_sync/standing.py devcontainer_setup/project_sync/state.py tests/test_standing.py
git commit -m "Read where a project stands from local state without side effects"
```

### Task 2: One bounded probe for GitHub and the generator

**Files:**
- Create: `devcontainer_setup/project_sync/probe.py`
- Test: `tests/test_probe.py` (new)

**Interfaces:**
- Produces, in `probe.py` (frozen dataclasses):
  - `Request`: `repository: str` (`owner/repo`), `default: str`, `want_prs: bool`, `generator: tuple[str, str] | None`
    (repository URL or path, ref). Built by the caller; this task does not import `standing`.
  - `Observation`: `status: str` (`ok`, `unknown` = no answer before the deadline, `failed`), `value: object`,
    `at: float | None` (`time.time()` when answered), `reason: str | None` (short and fixed: `no answer within 2 s`,
    `not signed in to GitHub`, `request failed`, `incomplete list`, `answer too large`).
  - `Space`: `name: str`, `state: str`, `last_used_at: str | None`.
  - `Remote`: `account: Observation` (login), `spaces: Observation` (`tuple[Space, ...]` of this repository; `ok`
    only when every page parsed and, where `total_count` is present, the row count equals it), `setup_prs:
    Observation` (`tuple[int, ...]` PR numbers), `upstream: Observation` (40-hex SHA).
  - `generator_request(env: Mapping[str, str]) -> tuple[str, str]`: `SETUP_DEVCONTAINER_REPO` (default
    `https://github.com/emsilva/devcontainer.git`) and `SETUP_DEVCONTAINER_REF` (default `main`), as
    `upgrade_handoff` reads them (`devcontainer_setup/__main__.py:481-483`).
  - `start(request: Request, *, env: Mapping[str, str] = os.environ, deadline: float = 2.0, clock=time.monotonic,
    popen=subprocess.Popen) -> Probe`, returning at once. `Probe.join() -> Remote` returns no later than
    `start + deadline` plus reaping; `Probe.cancel() -> None` kills and reaps. Both are idempotent.
- Children, all with `start_new_session=True`, stdin `DEVNULL`, stdout and stderr read through one `selectors` loop
  (no threads), 2 MiB stdout cap each:
  1. Token: `GH_TOKEN` from `env` when set; else `gh auth token --hostname github.com` (plus `--user
     $DEVCONTAINER_GH_USER` when set), env `transport.clean_env(env)`. No token: account, spaces and PRs are `failed`,
     `not signed in to GitHub`.
  2. Once the token is in: `gh api --hostname github.com user`, `gh api --hostname github.com
     'user/codespaces?per_page=100' --paginate --slurp`, and when `want_prs`, `gh api --hostname github.com
     'repos/<repo>/pulls?state=open&base=<default>&per_page=100' --paginate --slurp`; env `clean_env(env)` plus
     `GH_TOKEN`, `GH_PROMPT_DISABLED=1`, `GH_NO_UPDATE_NOTIFIER=1`. Rows filter like `Codespaces.spaces()`
     (`repository.full_name` case-insensitive, `transport._name` valid); PRs like setup's `setup_prs`
     (`devcontainer_setup/codespaces.py:146-153`: head ref fullmatches `setup/devcontainer(?:-[1-9][0-9]*)?`, head repo
     is this repository).
  3. From the start, when `generator` is set: `git -c credential.helper= ls-remote -- <url> <ref>` with
     `GIT_TERMINAL_PROMPT=0` and `GIT_SSH_COMMAND='ssh -o BatchMode=yes'`; a 40-hex `ref` is its own answer and
     spawns nothing; take `refs/heads/<ref>`, else `refs/tags/<ref>^{}`, else `refs/tags/<ref>`. A local directory
     URL resolves as `fetch_release` does.
- At the deadline, on `cancel()` and on any exception: `os.killpg(pgid, SIGKILL)` every child still running, then
  `wait()` each. Finished observations are kept; unfinished ones are `unknown`. Never parse or show stderr text.

- [ ] **Step 1: Write the tests** in `tests/test_probe.py`, with fake `gh` and `git` scripts first on `PATH` (each
  appends one line to a calls file, then answers from a fixture or sleeps):

```python
def test_all_reads_finish_inside_the_deadline(self):        # fast fakes -> every Observation 'ok', join < 1 s
def test_slow_read_is_unknown_and_the_rest_are_kept(self):
    # fake 'gh api user/codespaces…' sleeps 30 s
    self.assertEqual(remote.spaces.status, 'unknown'); self.assertEqual(remote.account.status, 'ok')
    self.assertLess(elapsed, 2.5)
def test_grandchild_is_gone_after_the_deadline(self):
    # fake gh forks a child that ignores SIGTERM and sleeps 60 s, writing both pids to a file
    remote = probe.start(request, env=env, deadline=0.5).join()
    for pid in pids: self.assertFalse(alive(pid))       # os.kill(pid, 0) raises ProcessLookupError
def test_cancel_kills_and_reaps(self)
def test_only_get_requests_are_made(self):
    # calls file: no '--method POST', no 'codespace start', no 'auth login', no 'fetch'; and no file created
    # under XDG_CACHE_HOME or the state dir
def test_incomplete_or_malformed_list_is_not_ok(self):     # total_count mismatch, page without 'codespaces',
                                                            # non-JSON, exit 1, 3 MiB answer -> never 'ok'
def test_setup_pr_filter_matches_setup(self):
    # the same multipage slurped fixture through setup Codespaces.setup_prs (command patched) and through the probe:
    # same PR numbers; fork, null head repo, 'setup/devcontainer-0', 'setup/devcontainerx', a numbered suffix
def test_upstream_tip_matches_fetch_release(self):
    # temporary local generator repo; probe tip == releases.fetch_release(repo, ref, cache=tmp)[1] for a branch,
    # an annotated tag and a 40-hex ref; the probe's own run creates no files anywhere
def test_no_token_is_failed_without_api_calls(self)
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_probe -v`. Expected: exit 1, module missing.

- [ ] **Step 3: Implement** `probe.py` as in Interfaces.

- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add devcontainer_setup/project_sync/probe.py tests/test_probe.py
git commit -m "Read Codespaces, setup pull requests and the generator tip under one two-second deadline"
```

### Task 3: Lines and actions

**Files:**
- Create: `devcontainer_setup/project_sync/overview.py`
- Test: `tests/test_overview.py` (new)

**Interfaces:**
- Consumes: `standing.Local`, `standing.Refusal` (Task 1); `probe.Remote`, `probe.Observation`, `probe.Space` (Task 2).
- Produces, in `overview.py`:
  - `Action` (frozen dataclass): `key: str`, `label: str`, `argv: tuple[str, ...]`; property `command -> str`
    (`shlex.join(argv)`); property `item -> str` (`f'{label}  ({command})'`, the menu text).
  - `View` (frozen dataclass): `lines: tuple[str, ...]` (at most 6, each at most 78 cells), `actions: tuple[Action, ...]`.
  - `view(local: Local, remote: Remote | None, *, forgotten: frozenset[tuple[str, str]] = frozenset(),
    now: float | None = None) -> View`. Pure. `remote=None` means the probe has not joined yet: every fact that needs
    it reads `checking GitHub…`.
  - `cells(text: str) -> int` (East Asian Wide and Fullwidth count 2, combining marks 0) and
    `fit(text: str, width: int = 78) -> str` (drops `Cc`/`Cf` characters, then cuts to `width` cells ending in `…`).
  - `task_command(local: Local, alias: str, *args: str) -> tuple[str, ...]`: not generated → `('task', alias, *args)`;
    generated and the root `Taskfile.yml`'s managed block (`# devcontainer-setup:work-tasks begin … end`) defines
    `alias` → `('task', alias, *args)`; otherwise `('task', 'devcontainer:' + alias, *args)`. Setup and upgrade are
    `task devcontainer:setup` / `task devcontainer:upgrade:self` in generated projects and `task setup` (no upgrade)
    otherwise. `args` start with `'--'` when present.
  - `next_lines(view: View) -> list[str]`: `['Next: ' + action.command for action in view.actions]`.

**Codespace state** (the paired one; first rule that applies):

| Evidence | State |
|---|---|
| `remote.spaces` ok and lists the pairing | its `state`, `checked HH:MM` |
| `remote.spaces` ok, pairing absent, `remote.account` ok and equal to `local.gh_user` | `deleted`, `confirmed HH:MM` |
| `local.deleted_at` (the keeper's own confirmed inventory) | `deleted`, `confirmed HH:MM` |
| `remote.spaces` ok, pairing absent, account not proven equal | `missing` |
| `local.reading` | its state, `as of HH:MM` |
| otherwise | `unknown`, with the observation's reason |

`Shutdown` is stopped by you when `down_kind == 'OWNER'`, or when `owner_stop_at` is set and `started_at` is not
later than it. `Starting`, `Provisioning`, `Queued`,
`Rebuilding`, `Awaiting`, `Exporting`, `Updating` read starting; `ShuttingDown` reads stopping; `Failed`,
`Unavailable`, `Archived` read failed.

**Lines.** Each fact is a `(rank, text)`; sorted by rank (stable), the first 6 kept, each passed through `fit`.
Exact copy (`NAME`, `USER`, `N` substituted; `(checked HH:MM)` / `(as of HH:MM)` is the state's suffix):

| Rank | When | Text |
|---|---|---|
| 0 | armed, `keeper == 'stopped'`, pairing not deleted | `Recovery is off until you connect` |
| 1 | `setup` missing, unmanaged or unknown | `This project is not set up yet` |
| 1 | `setup` partial | `Setup did not finish; run setup again to complete it` |
| 1 | one setup PR open | `Setup pull request #N is open; resume setup to finish it` |
| 1 | several setup PRs | `N setup pull requests are open; close all but one in GitHub` |
| 1 | `launch` not complete, for the pairing or unpaired | `Setup of Codespace NAME did not finish validation` |
| 1 | deleted | `Codespace NAME was deleted (confirmed HH:MM)` |
| 1 | missing | `Codespace NAME is not among ACCOUNT's Codespaces; it may be deleted or under another account` |
| 1 | failed | `Codespace NAME failed on GitHub; see its creation logs` |
| 1 | each `problems` entry | the entry, then `; task work:status shows more` |
| 2 | available | `Codespace NAME is available (checked HH:MM)` |
| 2 | stopped by you | `Codespace NAME is stopped; you stopped it at HH:MM` |
| 2 | stopped, `LIFETIME` | `Codespace NAME was stopped by GitHub's 12-hour limit at HH:MM` |
| 2 | stopped, `OTHER` | `Codespace NAME stopped at HH:MM, not by you` |
| 2 | stopped, reason unknown | `Codespace NAME is stopped (checked HH:MM)` |
| 2 | starting / stopping | `Codespace NAME is starting (checked HH:MM)` / `… is stopping (…)` |
| 2 | unknown | `Codespace NAME: GitHub did not answer within 2 s` / `…: REASON` / `…: checking GitHub…` |
| 2 | unpaired | `No Codespace is paired with this checkout` |
| 2 | unpaired, spaces ok | `This repository has N Codespaces on GitHub` (`no Codespace`, `1 Codespace`) |
| 3 | refusals not in `forgotten` | `NAME refused N conversations; forget them to clear the sync warning` (1: `conversation`, `it`) |
| 4 | `keeper == 'stale'` | `The recovery keeper has not answered for N min; connect to restart it` |
| 4 | `keeper == 'unknown'` | `Recovery state is unknown (process check timed out)` |
| 4 | paired, `worker == 'stopped'` | `Sync is stopped; connect to start it` |
| 5 | armed, keeper running | `Recovery is armed as USER` + `, on hold until HH:MM` when held |
| 5 | not armed, paired | `Recovery is not armed` |
| 6 | generated, installed, upstream ok and differs from `source_ref` | `Generator update available: SHORT → SHORT` |
| 7 | `behind` on the default branch | `Checkout is N commits behind origin/DEFAULT (fetched HH:MM)` / `(fetch time unknown)` |
| 7 | `diverged` | `Checkout and origin/DEFAULT have diverged; review before merging` |
| 8 | `cycle_at` | `Sync last completed HH:MM` |

**Actions.** State actions in this order, deduplicated by `key`, at most 5, then `details`; when fewer than 3 in all,
`logs` is added before `details`. In a Codespace (`local.in_codespace`): one line, `This is the Codespace; task work
shows the project's state on your Mac.`, and the single action `stop` (no `details`, no filler; Task 4 shows no menu).

| State | Actions, likely first |
|---|---|
| not set up, partial, or a setup PR open | `setup` (`Set up this project`, or `Resume setup` with a PR) |
| launch not complete | `resume` (`Resume setup of NAME`: setup `-- --resume NAME`), `creation-logs` (`Show creation logs`: `gh codespace logs -c NAME`) |
| failed | `creation-logs`, `resume` |
| deleted, missing | `pair` per other Codespace of this repository from `remote.spaces`, newest `last_used_at` first, at most 3 (`Pair NAME`: `work:cs -- --rebind NAME`), `create` (`Create a Codespace`: setup) |
| unpaired | `connect` (`Connect`: `work:cs`) when spaces ok and non-empty or unknown, `create` |
| available | `connect`; then `arm` (`Connect and arm recovery`: `work:cs:armed`) when not armed; `stop` (`Stop the Codespace`: `work:cs:stop`) |
| stopped (any reason), starting, stopping, unknown | `start` (`Start and connect`: `work:cs`) for stopped, else `connect` |
| overlays, after the state's own | `forget` per refusal, at most 2 (`Forget AGENT conversation SHORT`: `work:forget -- --agent AGENT --session ID`); `update-checkout` when behind (`Update the checkout`: `git -C ROOT merge --ff-only origin/DEFAULT`); `upgrade` (`Upgrade the generator`) |
| always last | `details` (`Show details`: `work:status`) |

`logs` is `Show recovery and sync logs`: `work:logs`. Recovery off puts `connect` (or `start`) first.

- [ ] **Step 1: Write the tests** in `tests/test_overview.py`, building `Local` and `Remote` values directly:

```python
def test_each_state_has_its_line_and_first_action(self):
    # one subTest per row of the State table above, plus 'recovery off' -> lines[0] == 'Recovery is off until you connect'
def test_frame_is_at_most_six_lines_of_78_cells_and_three_to_six_actions(self):
    # every combination of: state x armed/unarmed x keeper x refusals(0, 3) x behind x generator update
def test_untrusted_text_is_sanitized_and_fits(self):
    # names containing '\x1b[2J', '\n', '‮', 'コードスペース' and an emoji ZWJ sequence: no Cc/Cf char in any line,
    # cells(line) <= 78, and the action argv keeps the raw validated name (only display text is sanitized)
def test_bare_404_or_other_account_is_not_deleted(self):
    # spaces ok without the pairing but account differs, or account unknown, or spaces failed -> not 'deleted'
def test_old_owner_stop_does_not_label_a_later_run(self):
    # owner_stop_at earlier than a recorded start for the pairing -> not 'stopped by you'
def test_owner_alias_is_not_assumed_managed(self):
    # root Taskfile defines its own 'work:cs' outside the managed block -> ('task', 'devcontainer:work:cs')
    # managed block defines it -> ('task', 'work:cs'); not generated -> ('task', 'work:cs'); and the managed-block
    # parser agrees with root_tasks.integrate() output for every name in root_tasks.ALIASES
def test_forgotten_refusals_are_not_shown(self)
def test_pending_remote_reads_checking(self):        # view(local, None) -> 'checking GitHub…', never 'unknown'
def test_next_lines_are_never_truncated(self):       # a 200-character checkout path stays whole in next_lines
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_overview -v`. Expected: exit 1, module missing.

- [ ] **Step 3: Implement** `overview.py` as in Interfaces and the three tables.

- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add devcontainer_setup/project_sync/overview.py tests/test_overview.py
git commit -m "Turn a project's state into six lines and the actions that fit"
```

### Task 4: `task work` prints, asks and runs

**Files:**
- Modify: `devcontainer_setup/project_sync/overview.py` (add `run`)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`parser` at 25-55: the `work` action group stops being
  required; `run` at 296: bare `work` dispatches to `overview.run(args.project.resolve())` first, before
  `discover_project` and `state_dir`, so unreadable state is a line in the frame, not exit 1)
- Modify: `devcontainer_setup/project_sync/terminal.py` (`installed_gum() -> str | None`; `Gum.__init__(self,
  binary: str | None = None)`)
- Modify: `devcontainer_setup/root_tasks.py` (`ALIASES` and `INTERACTIVE` gain `'work'`; its managed stanza carries
  `desc`), `templates/continuity-tasks.yml`, `Taskfile.yml`, `README.md:46` (command table, first row)
- Test: `tests/test_work_overview.py` (new), `tests/test_root_tasks.py`, `tests/test_project_sync_cli.py`

**Interfaces:**
- Consumes: `standing.read` (Task 1), `probe.start`, `probe.Request`, `probe.generator_request` (Task 2),
  `overview.view`, `overview.next_lines` (Task 3), `forget.forgotten` (Task 5).
- Produces:
  - `overview.run(root: Path, *, env: Mapping[str, str] = os.environ, stdin=sys.stdin, stdout=sys.stdout,
    deadline: float = 2.0, gum: str | None | object = <installed_gum()>, call=subprocess.call) -> int`:
    1. `local = standing.read(root, env=env)`; in a Codespace: print `view(local, None)` lines and its
       `next_lines`, return 0, start no probe.
    2. Start the probe (`want_prs` and `generator` only when `local.generated`).
    3. Terminal (`stdin.isatty() and stdout.isatty()`): print `view(local, None).lines`; join; move the cursor up
       over them (`\x1b[{n}F\x1b[J`) and print the joined view. Otherwise: join, then print the lines.
    4. No terminal: print a blank line and `next_lines`, return 0. Terminal without Gum (`gum is None`): the same,
       then `Gum is not installed, so there is no menu; run one of the commands above.`, return 0.
    5. Gum: `Gum(gum).choose('What next?', [a.item for a in actions] + ['Exit'])`, with no `selected` (the cursor
       starts on the likely action; a preselected item containing a comma is refused by `Gum.choose`).
       `Exit` or `Cancelled`: print `Nothing ran.`, return 0. Otherwise print `Running: ` + `command`, return
       `call(list(argv), cwd=root)`.
    6. `KeyboardInterrupt` anywhere: `probe.cancel()`, re-raise (`main` prints `Cancelled.`, returns 130).
  - Bare `work` with any of `--follow`, `--json`, `--port`, `--no-port`, `--rebind`, `--then-signin-all`:
    `parser.error('task work takes no options; use task work:status -- --json for detail')`.
  - Root alias stanza for `work` (in the managed block, owner content untouched):
    `  work:\n    desc: Show where this project stands and what to do next\n    interactive: true\n    cmds:\n
    - task: devcontainer:work\n        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n`. The template and the generator's own
    `Taskfile.yml` gain `work:` with the same `desc`, `interactive: true`, and `bash .devcontainer/scripts/project-sync.sh
    work {{.CLI_ARGS}}` / `bash scripts/project-sync.sh work {{.CLI_ARGS}}`.
- Pinned behavior that changes on purpose: bare `work` was a parser error; the legacy `work` stanza is still retired by
  its exact text (`LEGACY`) but a managed `work` alias with the new meaning is added back, so
  `tests/test_root_tasks.py:93` (`assertNotIn('task: devcontainer:work\n', changed)`) becomes an `assertIn` of the new
  stanza. `test_quoted_custom_work_preserved` (owner's `"work"`) must stay green: an owner `work` keeps its task and gets
  no managed duplicate.

- [ ] **Step 1: Write the tests**

`tests/test_work_overview.py` (fake `gh`/`git` on `PATH` as in Task 2, a temp project and state; counting fakes for
every mutating path: `subprocess.Popen`/`call` argv log, `terminal.gum_binary`, `Gum.__init__` without a binary):

```python
def test_no_terminal_prints_state_and_next_lines_and_exits_0(self):
    code = overview.run(root, env=env, stdin=NotTTY(), stdout=out, gum=None)
    self.assertEqual(code, 0); self.assertIn('Next: task work:cs', out.getvalue())
    self.assertLessEqual(len(frame_lines(out)), 6)
def test_printing_starts_downloads_and_signs_in_nothing(self):
    # every state fixture from Task 3, no terminal: calls log has only 'gh auth token', 'gh api … user',
    # 'gh api … user/codespaces…', 'gh api … pulls…' and 'git … ls-remote'; gum_binary never called;
    # state, checkout and cache trees byte-identical
def test_deadline_holds_with_a_hung_github(self):       # fake gh sleeps 60 s: returns in < 3 s, line says 2 s
def test_terminal_redraws_the_first_frame(self):        # output has the pending frame, then '\x1b[3F\x1b[J', then final
def test_menu_runs_the_chosen_command_after_printing_it(self):
    # fake Gum returns actions[1].item -> 'Running: task work:cs:armed' printed, call(['task','work:cs:armed'], cwd=root)
def test_exit_and_esc_run_nothing(self):                 # 'Exit' and Cancelled -> 'Nothing ran.', 0, call not invoked
def test_ctrl_c_during_the_probe_leaves_nothing_running(self):
    # KeyboardInterrupt raised from the join: probe children gone; main() returns 130 with 'Cancelled.'
def test_inside_a_codespace_names_the_stop_only(self)
def test_without_gum_prints_next_lines_and_the_reason(self)
```

`tests/test_project_sync_cli.py`:

```python
def test_bare_work_runs_without_a_terminal(self):   # red today: argparse error
    result = self.run_cli('work')
    self.assertEqual(result.returncode, 0); self.assertIn('Next: ', result.stdout)
def test_bare_work_rejects_options(self):           # run_cli('work', '--json') -> 2, 'task work takes no options'
```

`tests/test_root_tasks.py`: `test_work_alias_has_a_description` (managed stanza as in Interfaces; `integrate` idempotent;
`'  work:\n'` present in the template and `Taskfile.yml` with the `desc`), and the edit to line 93 above.

- [ ] **Step 2: Run them; the new ones fail.**

Run: `python3 -m unittest tests.test_work_overview tests.test_project_sync_cli tests.test_root_tasks -v`
Expected: exit 1; every existing test except the one at `test_root_tasks.py:93` passes.

- [ ] **Step 3: Implement** as in Interfaces. README: a first row `| \`task work\` | Show where this project stands
and pick the next step from a menu; without a terminal it prints the commands |`.

- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0, with
`tests.test_continuity_integration` (every runtime module vendored byte-for-byte) green for the new modules.

- [ ] **Step 5: Smoke it from this checkout, offline.** Run:
`mkdir -p tmp/fakebin && printf '#!/bin/sh\nexit 1\n' > tmp/fakebin/gh && chmod +x tmp/fakebin/gh && PATH="$PWD/tmp/fakebin:$PATH" bash scripts/project-sync.sh work < /dev/null > tmp/work-smoke.txt; echo "exit=$?"; cat tmp/work-smoke.txt; git status --porcelain`.
Expected: `exit=0`, at most 6 lines before the blank line, then `Next:` lines; no `git status` output. The lead runs
the real-network smoke itself.

- [ ] **Step 6: Commit**

```bash
git add -A devcontainer_setup templates Taskfile.yml README.md tests
git commit -m "Add task work: the project's state in six lines and a menu of next steps"
```

### Task 5: Forget a conversation the paired Codespace refused

**Files:**
- Create: `devcontainer_setup/project_sync/forget.py`
- Modify: `devcontainer_setup/project_sync/replication.py` (`capture` at 34-50 gains `exclude`; the outbound call at
  313 passes `forget.forgotten(state)`)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`--forget` action with `--agent`, `--session`; handled before
  the terminal gate), `devcontainer_setup/root_tasks.py` (`ALIASES` gains `'work:forget'`, not interactive),
  `templates/continuity-tasks.yml`, `Taskfile.yml`, `README.md` (command table)
- Test: `tests/test_forget.py` (new), `tests/test_root_tasks.py`

**Interfaces:**
- Produces, in `forget.py`:
  - `FILE = 'conversation-forget.json'`: `{"schema": 1, "conversations": [{"agent", "session_id", "codespace",
    "reason", "forgotten_at"}]}`.
  - `forgotten(state: Path) -> frozenset[tuple[str, str]]`: `(agent, session_id)` pairs; empty when the file is
    missing; `ValueError('conversation-forget.json cannot be read; move it aside to send every conversation again')`
    when malformed. Project-wide: it holds across later rebinds.
  - `forget(state: Path, *, agent: str, session_id: str, now=time.time) -> str`. Under its own
    `conversation-forget.lock` (`state.locked`, blocking; a connected session never holds it): the current
    `scratch.json` names a Codespace; the worker's cycle for that binding (as Task 1 reads it) lists this pair among
    `desktop_to_codespace.publication_issues`; else `ValueError(f'{codespace} refused no {agent} conversation
    {session_id} in the last sync; nothing was forgotten.')`. Appends one record with `state.atomic_write`. Already
    forgotten: returns the same message without writing. Message: `Forgot {agent} conversation {session_id}. It stays on
    this Mac and is no longer sent to {codespace}; the next sync clears the warning.`
  - `replication.capture(project, store, *, env=None, historical_roots=(), relay=False, exclude=frozenset())`: drops
    entries and capture issues whose `(agent, session_id)` is in `exclude`, from both the native capture and the
    relayed entries, before `store.save`. Nothing else changes; relay records, checkpoint objects and native
    transcripts are not touched.
  - CLI: `work --forget --agent AGENT --session ID`; task `work:forget` with `desc: Stop sending a conversation the
    Codespace refused (task work:forget -- --agent claude --session ID)`, running `… work --forget {{.CLI_ARGS}}`.
- An unreadable forget file fails the outbound capture with that message (the cycle's existing per-direction error
  path); it never silently sends everything again.

- [ ] **Step 1: Write the tests** in `tests/test_forget.py` (use the fixtures the existing replication tests build a
  desktop store, relay records and a fake endpoint with):

```python
def test_forgotten_conversation_leaves_the_next_outbound_snapshot(self):
    # one native and one relayed entry refused; forget both; run one cycle against the fake endpoint
    self.assertNotIn(('claude', native_id), manifest_pairs); self.assertNotIn(('codex', relayed_id), manifest_pairs)
    self.assertEqual(outbound_row['publication_issues'], [])        # cleared
    self.assertIn(('claude', other_id), manifest_pairs)              # everything else still sent
def test_mac_copy_is_byte_identical(self):
    # hash the native transcript dirs, store relay/ and objects/ before forget and after the next cycle; equal
def test_unknown_conversation_is_refused_and_writes_nothing(self)
def test_forget_is_idempotent(self)
def test_unreadable_forget_file_fails_the_outbound_capture(self)
def test_cli_forget_runs_without_a_terminal(self)   # run_cli('work', '--forget', '--agent', 'claude', '--session', 'x')
```

`tests/test_root_tasks.py`: `work:forget` in `ALIASES`, the template and `Taskfile.yml`, not interactive.

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_forget tests.test_root_tasks -v`.
Expected: exit 1, module missing.

- [ ] **Step 3: Implement** as in Interfaces.

- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add -A devcontainer_setup templates Taskfile.yml README.md tests
git commit -m "Let task work:forget stop sending a conversation the Codespace refused"
```

## Order and parallelism

Tasks 1, 2 and 5 touch different files (5 and 4 both edit `__main__.py`, `root_tasks.py`, the template, `Taskfile.yml`
and `README.md`, in different stanzas) and run in parallel from 7ecc53c. Task 3 starts from the merge of 1 and 2.
Task 4 starts from the merge of 3 and 5. The lead merges each into `feature/task-work-ab` after its own re-gate.

## Live check (part of the A+B release gate, not of these tasks)

On innovate with the 2026-10-27 backup runbook: `task work` in a terminal (menu, redraw, a chosen action runs) and
piped (`Next:` lines, exit 0) for an available armed pairing, after `task work:cs:stop` (stopped by you), and with the
Mac offline (`GitHub did not answer within 2 s`); no new file in the state dir from the printing alone.
