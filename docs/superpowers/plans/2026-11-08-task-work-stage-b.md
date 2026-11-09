# `task work` stage B: where the project stands, and what to do next. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `task work` prints where the project stands in at most 6 lines of 78 columns, then offers the 3-6 actions
that fit as a Gum menu, the likely one first, each showing the command it runs; without a terminal it prints the state
and `Next:` commands and exits 0. A conversation the paired Codespace refused can be forgotten.

**Architecture:** Four small runtime modules, all vendored into generated projects like every `project_sync/*.py`:
`standing.py` reads local facts without side effects; `probe.py` runs every GitHub and Git read as owned child
processes under one 2-second monotonic deadline; `overview.py` turns facts into lines and actions (pure) and, in Task 4,
drives the printer and menu; `forget.py` records a forgotten conversation, and the outbound capture drops it at the
source. Setup facts that generated projects cannot import (metadata marker, launch receipt, root Taskfile, setup PRs,
generator tip, checkout comparison) get small runtime readers, each pinned by a parity test to the setup code it mirrors.

**Tech Stack:** Python 3 standard library (vendored, run as `python3 -I -B`), go-task, `unittest`, Gum (only when
already installed or cached).

**Spec:** `docs/superpowers/specs/2026-11-02-task-work-design.md`, section "B. `task work`", its Anti-goals,
Constraints, Owner answers 2 (forget) and "Tests the plan must include" (B list). Source inventory at 8fa4035:
`tmp/codespace-lifecycle/task-work/stage-b/inventory/INVENTORY.md` (sha256 `4b2590068d9f…`, INV §n). Plan check this
revision answers: `tmp/codespace-lifecycle/task-work/stage-b/plancheck/CHECK.md` (F1-F22) and
`tmp/codespace-lifecycle/task-work/stage-b/plancheck2/CHECK.md` (CHECK2, N1-N6). Line numbers are at 7ecc53c.

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
- "Deleted" needs a complete inventory of the account recorded for the pairing that lacks the paired name, or the
  keeper's own deleted verdict. A 404, an empty, truncated or failed list, a repository-filtered list, or a list read
  under another or unknown account never means deleted (spec Anti-goals, #60).
- Do not name a module `work.py`: it is in `render.RETIRED_HELPERS` (`devcontainer_setup/render.py:26-27`).
- Untrusted text (Codespace, branch, PR and path names, agent and session ids, reasons) shown in a state line goes
  through `overview.fit` (control and format characters dropped). An action whose argv holds a character of category
  `Cc`, `Cf`, `Zl` or `Zp` is not offered; a problem line says so instead. Executed commands are argv tuples; shown
  commands are `shlex.join(argv)`, never truncated.
- Stdlib only. No new key in the user `config.json`; `arming.json` may gain keys (CHECK C7). New state files use
  `state.atomic_write` (private, atomic).
- No client, organization or Codespace names in code, tests, docs or commit messages (the repository is public).
- Times shown are local `HH:MM`, or `YYYY-MM-DD HH:MM` when not today.

## Calls made by the lead (each can be overruled)

1. **Without a terminal, one frame after the join** (CHECK F12). On a terminal the local-only frame shows first and is
   redrawn when the probe joins (spec line 109). A script or agent gains nothing from a provisional frame and two
   frames would be ambiguous; the spec line now says so.
2. **The keeper's deleted verdict stands as dated, attributed evidence** (CHECK F1; CHECK2 Call 2). It is the
   runtime's existing proof (same token's account inventory) and `task work:status` already shows it. It is shown as
   the keeper's finding with its time, a newer account-wide sighting of the name overrides it, and Task 2 fixes its
   validator at the origin (`transport._deleted_from_list` also refuses a `total_count` that disagrees).
3. **A keychain dialog cannot be ruled out by environment** (CHECK F2; CHECK2 Call 3). The printer reads the token
   the way every runtime command does (`gh auth token`), with prompts disabled. The deadline bounds the token
   subprocess, not the lifetime of a dialog the OS may show; a token not handed over in time is `unknown`.
4. **Forget drops the conversation's own entry only** (CHECK F19). Another conversation that depends on its files
   still carries what it needs to resume; dependency validation is unchanged.
5. **A 40-hex `SETUP_DEVCONTAINER_REF` is a pin, not an observation** (CHECK F4): no generator line is shown for it.
   `ls-remote` cannot tell a tag that names a tree or blob from one naming a commit; `fetch_release` refuses such a
   tag when the upgrade runs, so the line may offer an upgrade that then stops with that error.

## Review Focus

1. **A probe child that leaves a grandchild** (gh waiting on the keychain, a git helper), including after its own
   leader exits: `join()` returns by the deadline and nothing in its process groups outlives it (Task 2
   `test_grandchild_is_gone_after_the_deadline`, `test_exited_leader_with_a_live_grandchild`).
2. **Evidence from the previous pairing after a rebind** (keeper runtime, worker cycle or owner stop naming OLD):
   never labels the current pairing (Task 1 `test_records_for_another_codespace_are_ignored`).
3. **Hostile or wide names**: an escape sequence, a newline or CJK/emoji text in a Codespace, branch, path or session
   name neither reaches the terminal raw (state lines, menu items, `Next:`, `Running:`) nor breaks 78 cells (Task 3
   `test_untrusted_text_never_reaches_the_terminal`).
4. **Ctrl-C or Esc**: during the probe, nothing it started survives, exit 130 with `Cancelled.`; at the menu, nothing
   runs, exit 0 with `Nothing ran.` (Task 4 `test_ctrl_c_during_the_probe_leaves_nothing_running`,
   `test_ctrl_c_or_esc_at_the_menu_runs_nothing`).
5. **An owner-defined root `work:cs`** that is not the managed one, or an edited managed block: the menu runs and
   prints `task devcontainer:work:cs`, never the owner's task (Task 3 `test_owner_alias_is_not_assumed_managed`).

---

### Task 1: Local facts

**Files:**
- Create: `devcontainer_setup/project_sync/standing.py`
- Modify: `devcontainer_setup/project_sync/state.py:376-392` (`process_identity`, `owned_process` gain
  `timeout: float = 5`, passed to `ps`), `devcontainer_setup/project_sync/arming.py:110,187-188`
  (`record_owner_stop(state, now, codespace)` also writes `owner_stop_codespace`; `move_consent` retires it with
  `owner_stop_at`), `devcontainer_setup/project_sync/power.py` (passes the Codespace it validated under the lock)
- Test: `tests/test_standing.py` (new); every caller of `record_owner_stop` in `tests/test_power.py`,
  `tests/test_arming*.py` and `tests/test_keeper.py` (442, 628, 638, 982) passes the fixture's paired Codespace;
  `tests/test_scratch_sync.py:274` (its `process_identity` stub becomes `lambda pid, **_: …`)

**Interfaces:**
- Produces, in `standing.py` (all frozen dataclasses):
  - `Checkout`: `default: str`, `branch: str | None`, `relation: str` (one of `current`, `behind`, `ahead`,
    `diverged`, `detached`, `other-branch`, `no-remote-ref`, `unknown`), `behind: int`, `ahead: int`,
    `copied_at: float | None` (the newest reflog time of `refs/remotes/origin/<default>`, else None; never `FETCH_HEAD`).
  - `Launch`: `codespace: str`, `complete: bool` (`commit` is 40 hex, `validated_commit == commit`,
    `rebuild_pending is False`).
  - `Refusal`: `agent: str` (fullmatches `[a-z][a-z0-9_-]{0,31}`), `session_id: str` (fullmatches
    `[A-Za-z0-9._-]{1,128}`), `reason: str`.
  - `Local`: `root: Path`, `state: Path | None` (None when `state_dir` refuses; its message goes to `problems`),
    `repository: str` (origin's `owner/repo`, spelling preserved as setup's `_github_repository` keeps it),
    `generated: bool` (this package runs from `<root>/.devcontainer/scripts/project_sync`), `in_codespace: bool`
    (`CODESPACES == 'true'`), `setup: str` (`installed`, `partial`, `missing`, `unmanaged`, `unknown`, or `source` when
    not generated), `source_ref: str | None`, `taskfile: str | None` (the root Taskfile's name), `managed:
    frozenset[str] | None` (aliases in a valid managed block; None when absent, edited or ambiguous), `pairing: str |
    None`, `armed: bool | None` (None = unreadable), `gh_user: str | None` (arming's, else the keeper runtime's, each
    only when it names the pairing), `owner_stop_at: float | None` (only for the pairing, below), `hold_until: float |
    None`, `started_at: float | None` (the pairing's `started_at` in `start-evidence.json`, read raw), `last_up_at:
    float | None`, `keeper: str` (`running`, `stale`, `stopped`, `unknown`), `keeper_heartbeat_age: float | None`,
    `reading: tuple[str, float] | None`, `deleted_at: float | None`, `down_kind: str | None`, `down_at: float | None`,
    `worker: str` (`running`, `stopped`, `unknown`), `cycle_at: float | None`, `refusals: tuple[Refusal, ...]`,
    `launch: Launch | None`, `checkout: Checkout`, `problems: tuple[str, ...]`.
  - `read(root: Path, *, env: Mapping[str, str] = os.environ, timeout: float = 1.0) -> Local`. Never raises: an
    unreadable file becomes `unknown`/None plus one `problems` entry naming it (`'arming.json cannot be read'`).
  - `refusals(state: Path, project, binding: dict) -> tuple[Refusal, ...]`: the refusal reader Task 5 also uses.
- Rules `read` follows:
  - Every subprocess (git, ps) gets `timeout`; git runs with `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`. A
    timeout makes only that fact `unknown`. No `git status`, `fetch` or `merge`.
  - `setup` (generated only): `missing` without `.devcontainer/devcontainer.json`; `unmanaged` without
    `customizations.devcontainerSetup`; `unknown` on malformed JSON or a `sourceRef` that is not 40 hex; `partial`
    when installed but neither `R/workspace.json` nor `R/scratch.json` exists (spec line 108; test file existence,
    not `workspace.registry()`, which synthesizes defaults); else `installed`.
  - `taskfile`/`managed`: the names and order setup's `integrate_taskfile` accepts (`devcontainer_setup/__main__.py:
    81-87`); more than one present is ambiguous. The managed block is valid only when every check `root_tasks.integrate`
    makes holds (`devcontainer_setup/root_tasks.py:16-30`): the label appears exactly twice, the block matches its
    pattern and sha256, and it sits inside the top-level `tasks:` mapping at that mapping's entry indent (the region
    from a column-0 `tasks:` line to the next column-0 key). Otherwise `managed` is None and a problem says
    `'The managed work tasks in TASKFILE were edited or moved'`.
  - Records count only when they name the current `pairing`: keeper runtime `codespace`; worker `binding` equal to
    `scratch.json`; arming `codespace` for consent fields. The owner stop counts when `owner_stop_codespace` names the
    pairing, or, for a record without that key, when arming's `codespace` does (an unarmed legacy stop is ignored).
  - Keeper liveness: `state.owned_process(..., timeout=timeout)` with `keeper.status`'s flags; `stale` = owned with
    no heartbeat or one older than `keeper.HEARTBEAT_STALE` (as `keeper.py:74-79` treats it). `down_kind`/`down_at`
    come only from a `running` keeper's record with a numeric `down.first_at` and a `down.kind` in `OWNER`,
    `LIFETIME`, `OTHER`; no fallback classification. `reading` is `reading` when `ok` is true and `state` a string,
    else `last_ok`'s `(state, at)`, else None.
  - `refusals`: the worker cycle counts when `schema == 3`, `completed_at` is set, the worker `binding` equals
    `binding`, and the cycle's `project_id`, `identity`, `local_root` and `remote_root` match the project and binding
    (as `workspace.py:359-364` checks them), and that direction's `sync_cycle.receipt_matches(receipt, snapshot_id)`
    holds; then each `directions.desktop_to_codespace.publication_issues` entry
    whose ids match the `Refusal` patterns (others add one problem). Capture issues are not refusals.
  - `launch`: the receipt at `<setup state home>/devcontainer-setup/<sha256(f'{repo}:{default}')[:16]>.json`, the
    setup state home being an absolute `XDG_STATE_HOME`, else `~/.local/state` (setup's resolver, not continuity's;
    INV §1). Kept when `repository == repo`, `branch == default` and `codespace` is a valid name. Never migrated.

- [ ] **Step 1: Write the tests** in `tests/test_standing.py` (temporary Git repos with an `origin`, `XDG_STATE_HOME`
  in a temp dir, fake `ps` through a patched `state.process_identity`):

```python
def test_reading_creates_and_changes_nothing(self):
    # hash every file under the checkout, the state dir and XDG_CACHE_HOME before and after read(); equal;
    # with no state dir at all it still does not exist afterwards
def test_setup_states(self):            # missing, unmanaged, malformed -> unknown, partial, installed, source
def test_records_for_another_codespace_are_ignored(self):
    # scratch.json names 'new-cs'; keeper runtime, worker binding, arming and owner_stop_codespace name 'old-cs'
    self.assertIsNone(local.reading); self.assertEqual(local.refusals, ()); self.assertIsNone(local.owner_stop_at)
def test_unarmed_owner_stop_names_its_codespace(self):
    # power.stop on an unarmed pairing -> arming.json has owner_stop_at and owner_stop_codespace; read() keeps it
def test_stopped_keeper_gives_no_stop_classification(self):   # stale OWNER record, keeper not owned -> down_kind None
def test_failed_reading_falls_back_to_last_ok(self)
def test_unreadable_arming_is_unknown_not_unarmed(self):
    self.assertIsNone(local.armed); self.assertIn('arming.json cannot be read', local.problems)
def test_process_check_timeout_is_unknown(self):   # process_identity raises TimeoutExpired -> keeper == 'unknown'
def test_refusals_need_a_matching_cycle(self):     # foreign project_id or remote_root -> (); capture issues -> ()
def test_checkout_relations(self):     # current, behind 2, ahead 1, diverged, detached, other-branch, no-remote-ref
def test_copy_time_is_the_tracking_refs_own(self):
    # setup's --no-write-fetch-head fetch moves origin/main: copied_at changes; a later fetch of another ref: unchanged
def test_checkout_matches_setup_decision(self):
    # each fixture copied; git_ops.checkout_update on the copy (local bare origin, offline): behind -> 'updated';
    # current -> 'current'; diverged -> 'diverged'; detached -> 'detached'; other-branch -> 'other-branch';
    # read() on the original leaves HEAD, index and refs byte-identical
def test_metadata_matches_setup(self):  # render() output -> source_ref equals __main__._metadata(...)['sourceRef']
def test_launch_receipt_matches_setup(self):
    # path equals codespaces.receipt_path(state_home, repo, default) for absolute XDG_STATE_HOME and the default home,
    # a mixed-case owner/repo and a slash in the branch; complete == bool(__main__._launch_history(...)) for complete,
    # unvalidated, rebuild_pending and wrong-branch fixtures
def test_repository_spelling_matches_setup(self):  # equals __main__._github_repository for mixed-case origins
def test_managed_aliases_match_root_tasks(self):
    # integrate() output under each of the 8 Taskfile names -> managed == set(root_tasks.ALIASES);
    # an owner 'work:cs' outside the block -> not in managed; an edited block, or a hash-valid block placed under
    # 'vars:' -> None plus its problem (agreeing with integrate() raising); two Taskfiles -> None
def test_unresolvable_state_home_is_a_problem(self)  # state_dir raises -> state None, problem names it
```

- [ ] **Step 2: Run them; they fail** (`standing` does not exist).

Run: `python3 -m unittest tests.test_standing -v`
Expected: exit 1, `ModuleNotFoundError: No module named 'devcontainer_setup.project_sync.standing'`.

- [ ] **Step 3: Implement** `standing.py` as in Interfaces, the `timeout` keyword in `state.py`, and the
  `owner_stop_codespace` producer in `arming.py`/`power.py` (the keeper keeps reading `owner_stop_at` project-wide).

- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add -A devcontainer_setup/project_sync tests
git commit -m "Read where a project stands from local state without side effects"
```

### Task 2: One bounded probe for GitHub and the generator

**Files:**
- Create: `devcontainer_setup/project_sync/probe.py`
- Modify: `devcontainer_setup/project_sync/transport.py:48-62` (below)
- Test: `tests/test_probe.py` (new), `tests/test_lifecycle.py`

**Interfaces:**
- Produces, in `probe.py` (frozen dataclasses):
  - `Request`: `repository: str` (`owner/repo`), `default: str`, `want_prs: bool`, `generator: tuple[str, str] | None`
    (repository URL or path, ref). Built by the caller; this task does not import `standing`.
  - `Observation`: `status: str` (`ok`, `unknown` = no answer before the deadline, `failed`), `value: object`,
    `at: float | None` (`time.time()` when answered), `reason: str | None`, one of: `no answer within 2 s`,
    `not signed in to GitHub`, `request failed`, `incomplete list`, `answer too large`, `gh is not installed`,
    `git is not installed`, `pinned`.
  - `Space`: `name: str`, `state: str`, `last_used_at: float | None` (GitHub's `last_used_at`, parsed from ISO
    8601; it moves when the Codespace is started, so a value later than a stop means a later run).
  - Modify `devcontainer_setup/project_sync/transport.py:48-62` (`_deleted_from_list`): when the answer carries an
    integer `total_count`, absence is confirmed only if it equals the number of rows; test it in
    `tests/test_lifecycle.py` beside `test_a_404_with_a_full_first_page_stays_unreadable` (a `total_count` of 101 with
    one row stays unreadable).
  - `Remote`: `account: Observation` (login), `names: Observation` (`frozenset[str]` of every Codespace name in the
    account, `ok` only when complete, below), `spaces: Observation` (`tuple[Space, ...]` of this repository, from the
    same answer), `setup_prs: Observation` (`tuple[int, ...]` PR numbers), `upstream: Observation` (40-hex SHA).
  - `generator_request(env: Mapping[str, str]) -> tuple[str, str]`: `SETUP_DEVCONTAINER_REPO` (default
    `https://github.com/emsilva/devcontainer.git`) and `SETUP_DEVCONTAINER_REF` (default `main`), as `upgrade_handoff`
    reads them (`devcontainer_setup/__main__.py:481-483`).
  - `start(request: Request, *, env: Mapping[str, str] = os.environ, deadline: float = 2.0, clock=time.monotonic,
    popen=subprocess.Popen) -> Probe`, returning at once. `Probe.join() -> Remote` returns by `start + deadline` plus
    reaping; `Probe.cancel() -> None` kills and reaps. Both idempotent; neither raises for a child's failure.
- Children, all `start_new_session=True`, stdin `DEVNULL`, stdout and stderr read through one `selectors` loop (no
  threads), 2 MiB stdout cap each. `FileNotFoundError` from `popen` makes that observation `failed` (`gh is not
  installed` / `git is not installed`).
  1. Token: `GH_TOKEN` from `env` when set; else `gh auth token --hostname github.com` (plus `--user
     $DEVCONTAINER_GH_USER` when set), env `transport.clean_env(env)` plus `GH_PROMPT_DISABLED=1`,
     `GH_NO_UPDATE_NOTIFIER=1`. No token: account, names, spaces and PRs are `failed`, `not signed in to GitHub`.
  2. Once the token is in: `gh api --hostname github.com user`; `gh api --hostname github.com
     'user/codespaces?per_page=100' --paginate --slurp`; when `want_prs`, `gh api --hostname github.com
     'repos/<repo>/pulls?' + urlencode({'state': 'open', 'base': default, 'per_page': 100}) --paginate --slurp`
     (as setup builds it, `devcontainer_setup/codespaces.py:146-153`); env as the token child plus `GH_TOKEN`.
  3. From the start, when `generator` is set and its ref is not 40 hex (a 40-hex ref is `unknown`, `pinned`, and
     spawns nothing): `git -c core.hooksPath=/dev/null -c credential.helper= ls-remote -- <url> <patterns>`, run
     with `cwd='/'` so no repository's own config applies, and the environment
     `releases._git` builds (every `GIT_*` removed, then `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`,
     `GIT_CONFIG_GLOBAL=/dev/null`; `devcontainer_setup/releases.py:15-25`) plus `GIT_SSH_COMMAND='ssh -o
     BatchMode=yes'`, and without `SSH_ASKPASS`. Patterns: for a ref `R` not starting with `refs/`, `refs/heads/R
     refs/tags/R refs/tags/R^{}`; for `refs/…`, `R R^{}`. Answer: the head; else the peeled tag; else the tag. A local
     directory URL resolves as `fetch_release` does. Transports: https, ssh, local path.
- Completeness of `names`: gh exited 0; the answer is a list of pages, each a dict whose `codespaces` is a list; every
  row has a valid name (`transport._name`); and when pages carry `total_count`, every page's agrees and equals the
  number of distinct names; without `total_count`, the last page holds fewer than 100 rows. Otherwise `failed`,
  `incomplete list`. `spaces` filters the same rows by `repository.full_name` (case-insensitive), as
  `Codespaces.spaces()` does (`project_sync/codespaces.py:54-64`); PRs filter as setup's `setup_prs`.
- A child is done when its leader has exited and its pipes reached EOF. At the deadline, on `cancel()`, on any
  exception and after normal completion, `os.killpg(pgid, SIGKILL)` every child's group whether or not its leader is
  still running (`ProcessLookupError` ignored), then `wait(timeout=1)` each leader (one still alive after that is
  left and named in its observation's reason). Descendants that start their own session
  are not owned (gh and git do not); this is the stated limit. Never parse or show stderr text.

- [ ] **Step 1: Write the tests** in `tests/test_probe.py`, with fake `gh` and `git` scripts first on `PATH` (each
  appends its argv and selected env to a calls file, then answers from a fixture or sleeps):

```python
def test_all_reads_finish_inside_the_deadline(self):        # fast fakes -> every Observation 'ok', join < 1 s
def test_slow_read_is_unknown_and_the_rest_are_kept(self):
    # fake 'gh api user/codespaces…' sleeps 30 s
    self.assertEqual(remote.names.status, 'unknown'); self.assertEqual(remote.account.status, 'ok')
    self.assertLess(elapsed, 2.5)
def test_grandchild_is_gone_after_the_deadline(self):
    # fake gh forks a child that ignores SIGTERM and sleeps 60 s, writing both pids to a file
    remote = probe.start(request, env=env, deadline=0.5).join()
    for pid in pids: self.assertFalse(alive(pid))
def test_exited_leader_with_a_live_grandchild(self):   # leader exits at once, grandchild holds stdout open -> killed
def test_cancel_kills_and_reaps(self)
def test_only_reads_are_made(self):
    # calls file: no '--method' other than GET, no 'codespace', no 'auth login', no 'fetch'; no file created under
    # XDG_CACHE_HOME or the state dir
def test_child_environments_disable_prompts(self):
    # token and api children have GH_PROMPT_DISABLED=1 and no GH_TOKEN except the api ones; git has no GIT_* but the
    # four set, no SSH_ASKPASS, GIT_CONFIG_GLOBAL=/dev/null, cwd '/'; a global gitconfig and the project's own
    # .git/config with credential.helper and core.askpass set are not consulted (fake helper records nothing)
def test_list_completeness(self):
    # repository renamed (paired name under another full_name) -> names has it, spaces does not;
    # total_count mismatch, a row without a name, a page without 'codespaces', non-JSON, exit 1, 3 MiB -> names failed
def test_missing_binaries_are_failed_observations(self)    # PATH without gh or git
def test_setup_pr_filter_matches_setup(self):
    # the same multipage slurped fixture through setup Codespaces.setup_prs (command patched) and the probe:
    # same numbers; fork, null head repo, 'setup/devcontainer-0', 'setup/devcontainerx', a numbered suffix; and the
    # query for a base of 'release/one&two+x#y' equals setup's
def test_upstream_tip_matches_fetch_release(self):
    # temporary local generator repo: probe tip == releases.fetch_release(repo, ref, cache=tmp)[1] for a branch, an
    # annotated tag, a lightweight tag and 'refs/heads/…'; a missing ref -> failed; a 40-hex ref -> unknown 'pinned';
    # the probe's own run creates no files anywhere
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
  - `Action` (frozen dataclass): `key: str` (target-qualified: `pair:NAME`, `forget:AGENT:ID`; others plain),
    `label: str`, `argv: tuple[str, ...]`; property `command -> str` (`shlex.join(argv)`); property `item -> str`
    (`f'{fit(label)}  ({command})'`, the menu text).
  - `View` (frozen dataclass): `lines: tuple[str, ...]` (at most 6, each at most 78 cells), `actions: tuple[Action, ...]`.
  - `view(local: Local, remote: Remote | None, *, forgotten: frozenset[tuple[str, str]] = frozenset(),
    now: float | None = None) -> View`. Pure: reads no file. `remote=None` means the probe has not joined: every fact
    that needs it reads `checking GitHub…`.
  - `cells(text: str) -> int` (East Asian Wide and Fullwidth count 2, combining marks 0) and
    `fit(text: str, width: int = 78) -> str` (drops `Cc`, `Cf`, `Zl`, `Zp` characters, then cuts to `width` cells
    ending in `…`).
  - `task_command(local: Local, alias: str, *args: str) -> tuple[str, ...]`: not generated → `('task', alias, *args)`;
    generated and `alias in (local.managed or ())` → `('task', alias, *args)`; otherwise `('task', 'devcontainer:' +
    alias, *args)`. Setup is `task devcontainer:setup` in generated projects, `task setup` otherwise; upgrade is
    `task devcontainer:upgrade:self`, generated only. `args` start with `'--'` when present.
  - `next_lines(view: View) -> list[str]`: `['Next: ' + action.command for action in view.actions]`.

**Codespace state** of the pairing: `normalize(raw: str) -> str` maps `Available` → available; `Shutdown` → stopped;
`ShuttingDown` → stopping; `Starting`, `Provisioning`, `Queued`, `Rebuilding`, `Awaiting`, `Exporting`, `Updating`
→ starting; `Failed`, `Unavailable`, `Archived` → failed; `Deleted` → deleted; anything else → other. Then,
first rule that applies:

| Evidence | State |
|---|---|
| `remote.spaces` ok and lists the pairing | `normalize(state)`; SUFFIX `checked HH:MM` |
| `remote.names` ok and lists the pairing, `remote.spaces` does not | elsewhere (another repository's Codespace) |
| `remote.names` ok without the pairing, `remote.account` ok and equal to `local.gh_user` | deleted, `confirmed HH:MM` |
| `local.deleted_at` (the keeper's own verdict) | deleted by the keeper, at `deleted_at` |
| `remote.names` ok without the pairing (account not proven) | missing |
| `local.reading` | `normalize(state)`; SUFFIX `as of HH:MM` (date when not today) |
| otherwise | unknown, with the observation's reason |

A **later start** is evidence of a run after a moment `t`: `local.started_at > t` (a start the Mac requested on a
stopped Codespace) or the pairing's `Space.last_used_at > t`. An Available sample (`last_up_at`) is not one: a stop
is asynchronous. The stop reason, first that applies: the keeper's `down_kind` at `down_at`, unless a later start
follows `down_at` (then unknown); else **by you** when `owner_stop_at` is set and no later start follows it; else
unknown. `OWNER` reads by you, `LIFETIME` by the limit, `OTHER` not by you.

**Primary situation**, first that applies: in a Codespace; not set up (`setup` missing, unmanaged, unknown or partial);
setup PRs open (`setup_prs` ok and non-empty); failed (state failed); launch incomplete (`launch` not complete, its
Codespace the pairing or no pairing); deleted (either kind); missing; elsewhere; unpaired; then the pairing's state
(available, stopped, starting, stopping, other, unknown). Overlays apply to every situation except in a Codespace.

**Lines.** Each fact is `(rank, text)`; sorted by rank (stable), the first 6 kept, each through `fit`. Exact copy
(`NAME`, `USER`, `N`, `ACCOUNT`, `DEFAULT` substituted; `SUFFIX` is the state's `checked HH:MM` or `as of HH:MM`
from the state table, never `checked` for a cached reading; `DETAILS` is `task_command(local, 'work:status')` joined):

| Rank | When | Text |
|---|---|---|
| 0 | armed, `keeper == 'stopped'`, not deleted, missing or elsewhere | `Recovery is off until you connect` |
| 1 | in a Codespace | `This is the Codespace; task work shows the project's state on your Mac.` |
| 1 | not set up | `This project is not set up yet` |
| 1 | partial | `Setup did not finish; run setup again to complete it` |
| 1 | one setup PR | `Setup pull request #N is open; resume setup to finish it` |
| 1 | several setup PRs | `N setup pull requests are open; close all but one in GitHub` |
| 1 | failed | `Codespace NAME failed on GitHub; see its creation logs` |
| 1 | launch incomplete | `Setup of Codespace NAME did not finish validation` |
| 1 | deleted | `Codespace NAME was deleted (confirmed HH:MM)` |
| 1 | deleted by the keeper | `Codespace NAME was deleted (found by the recovery keeper HH:MM)` |
| 1 | elsewhere | `Codespace NAME is now listed under another repository; pair this one's Codespace` |
| 1 | missing | `Codespace NAME is not among ACCOUNT's Codespaces; it may be deleted or under another account` |
| 1 | each `problems` entry, and each action withheld for unsafe text | the entry + `; DETAILS shows more` |
| 2 | available | `Codespace NAME is available (SUFFIX)` |
| 2 | stopped by you | `Codespace NAME is stopped; you stopped it at HH:MM` |
| 2 | stopped by the limit | `Codespace NAME was stopped by GitHub's 12-hour limit at HH:MM` |
| 2 | stopped, not by you | `Codespace NAME stopped at HH:MM, not by you` |
| 2 | stopped, reason unknown | `Codespace NAME is stopped (SUFFIX)` |
| 2 | starting / stopping | `Codespace NAME is starting (SUFFIX)` / `Codespace NAME is stopping (SUFFIX)` |
| 2 | other | `Codespace NAME is in state STATE (SUFFIX)` |
| 2 | unknown | `Codespace NAME: GitHub did not answer within 2 s` / `Codespace NAME: REASON` / `Codespace NAME: checking GitHub…` |
| 2 | unpaired | `No Codespace is paired with this checkout` |
| 2 | unpaired, spaces ok | `This repository has N Codespaces on GitHub` (`no Codespace`, `1 Codespace`) |
| 3 | refusals not in `forgotten` | `NAME refused N conversations; forget them to clear the sync warning` (1: `conversation`, `it`) |
| 4 | `keeper == 'stale'` | `The recovery keeper has not answered for N min; connect to restart it` (no heartbeat: `has not answered`) |
| 4 | `keeper == 'unknown'` | `Recovery state is unknown (process check timed out)` |
| 4 | paired, `worker == 'stopped'` | `Sync is stopped; connect to start it` |
| 5 | armed, keeper running | `Recovery is armed as USER` + `, on hold until HH:MM` when held |
| 5 | not armed, paired | `Recovery is not armed` |
| 6 | generated, installed, upstream ok, differs from `source_ref` | `Generator REF is at SHORT; this project uses SHORT` |
| 7 | `behind`, on the default branch | `Checkout is N commits behind origin/DEFAULT (local copy from HH:MM)` / `(local copy)` |
| 7 | `diverged` | `Checkout and origin/DEFAULT have diverged; review before merging` |
| 8 | `cycle_at` | `Sync last completed HH:MM` |

**Actions**, as `(key, label, argv)`: the situation's own in order, then overlays, deduplicated by key, at most 5,
then `details`; when fewer than 3 in all, `logs` goes before `details`. In a Codespace only `stop`, nothing added.

| Situation | Its own actions, likely first |
|---|---|
| in a Codespace | `stop` |
| not set up, partial, one setup PR | `setup` (`Set up this project`; with a PR, `Resume setup`) |
| several setup PRs | `pr:N` per open setup PR, lowest number first, at most 3 (`Open setup pull request #N`: `gh pr view N --web`) |
| failed | `creation-logs` (`Show creation logs`: `gh codespace logs -c NAME`), `resume` (`Resume setup of NAME`: setup `-- --resume NAME`) |
| launch incomplete | `resume`, `creation-logs` |
| deleted (either kind), missing, elsewhere | `pair:NAME` for each other Codespace in `remote.spaces`, newest `last_used_at` first, at most 3 (`Pair NAME`: `work:cs -- --rebind NAME`), `create` (`Create a Codespace`: setup) |
| unpaired | `connect` (`Connect`: `work:cs`) when spaces ok and non-empty or not ok, then `create` |
| available | `connect`; `arm` (`Connect and arm recovery`: `work:cs:armed`) when `armed is False`; `stop` (`Stop the Codespace`: `work:cs:stop`) |
| stopped | `start` (`Start and connect`: `work:cs`) |
| starting, stopping, other, unknown | `connect` |
| overlays | `forget:AGENT:ID` per unforgotten refusal, at most 2 (`Forget AGENT conversation ID[:8]`: `work:forget -- --agent AGENT --session ID`); `update-checkout` when behind on the default branch (`Update the checkout`: `git -C ROOT merge --ff-only origin/DEFAULT`); `upgrade` (`Review the generator upgrade`) with the generator line |
| last | `details` (`Show details`: `work:status`); filler `logs` (`Show recovery and sync logs`: `work:logs`) |

Recovery off moves `connect` or `start` to the front when the situation offers it; it adds neither.

- [ ] **Step 1: Write the tests** in `tests/test_overview.py`, building `Local` and `Remote` values directly:

```python
def test_each_situation_has_its_lines_and_actions(self):
    # one subTest per Primary situation and per stop reason: exact lines[0] text and exact ordered argv list
def test_simultaneous_facts_resolve_one_way(self):
    # exact lines and argv for: recovery off + stopped by you + behind + 1 refusal; launch incomplete + failed;
    # missing + armed + keeper stopped (no 'Recovery is off', no connect); several setup PRs + available;
    # unarmed pairing with account unknown and names ok without it -> missing
def test_frame_is_at_most_six_lines_of_78_cells_and_three_to_six_actions(self):
    # every combination of situation x armed True/False/None x keeper x refusals(0, 3) x behind x generator differs
def test_untrusted_text_never_reaches_the_terminal(self):
    # names with '\x1b[2J', '\n', '‮', 'コードスペース', an emoji ZWJ sequence, and a checkout path with '\n':
    # no Cc/Cf/Zl/Zp char in any line, item or Next line; cells(line) <= 78; the path's update-checkout action is
    # withheld with its problem line; a safe long path stays whole in next_lines
def test_bare_404_or_other_account_is_not_deleted(self):
    # names ok without the pairing and account differs or is unknown, names failed, spaces-only absence -> not deleted
def test_old_owner_stop_does_not_label_a_later_run(self):
    # owner_stop_at earlier than started_at or the Space's last_used_at -> reason unknown; a running keeper's OWNER
    # with last_used_at after down_at -> reason unknown
def test_asynchronous_stop_is_still_yours(self):
    # owner stop T+10, Available sample (last_up_at) T+20, Shutdown T+30, last_used_at T -> 'you stopped it at …'
def test_several_setup_prs_offer_each_pull_request(self):
    # PRs 7 and 9, no overlay -> exact argv: gh pr view 7 --web, gh pr view 9 --web, task …work:logs, task …work:status
def test_cached_state_says_as_of(self):
    # probe failed, keeper reading Available from today 09:10 and from yesterday -> '(as of 09:10)' /
    # '(as of YYYY-MM-DD 09:10)', never 'checked'
def test_newer_sighting_overrides_the_keepers_deleted(self):
    # deleted_at set, names ok and listing the pairing (spaces too) -> its state, not deleted
def test_owner_alias_is_not_assumed_managed(self):
    # managed None or without 'work:cs' -> ('task', 'devcontainer:work:cs'); with it -> ('task', 'work:cs');
    # not generated -> ('task', 'work:cs'), setup 'task setup', no upgrade action
def test_unmapped_codespace_state_is_other(self)    # 'Moved', 'Created' -> 'Codespace NAME is in state …'
def test_forgotten_refusals_are_not_shown(self)
def test_pending_remote_reads_checking(self):        # view(local, None) -> 'checking GitHub…', never 'unknown'
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_overview -v`. Expected: exit 1, module missing.

- [ ] **Step 3: Implement** `overview.py` as in Interfaces and the tables.

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
  required; `run` at 295: bare `work` dispatches to `overview.run(args.project.resolve())` first, before
  `discover_project` and `state_dir`; `main` at 394-402: the validation table below)
- Modify: `devcontainer_setup/project_sync/terminal.py` (`installed_gum() -> str | None`: `shutil.which('gum')` or the
  cached binary `gum_binary` would return, never downloading; `Gum.__init__(self, binary: str | None = None)`)
- Modify: `devcontainer_setup/root_tasks.py` (`ALIASES` and `INTERACTIVE` gain `'work'`; its managed stanza carries
  `desc`), `templates/continuity-tasks.yml`, `Taskfile.yml`, `README.md:46` (command table, first row)
- Test: `tests/test_work_overview.py` (new); `tests/test_root_tasks.py`; `tests/test_project_sync_cli.py`;
  `tests/test_continuity_integration.py`

**Interfaces:**
- Consumes: `standing.read` (Task 1); `probe.start`, `probe.Request`, `probe.generator_request` (Task 2);
  `overview.view`, `overview.next_lines` (Task 3); `forget.forgotten`, `forget.Unreadable` (Task 5).
- Produces:
  - `overview.run(root: Path, *, env: Mapping[str, str] = os.environ, stdin=sys.stdin, stdout=sys.stdout,
    deadline: float = 2.0, gum=DEFAULT, call=subprocess.call) -> int` (`DEFAULT` resolves to
    `terminal.installed_gum()` at call time; `None` means no Gum):
    1. `local = standing.read(root, env=env)`. `forgotten = forget.forgotten(local.state)` when `local.state`; on
       `forget.Unreadable`, add its message as a problem and use `frozenset()`. In a Codespace: print `view(local,
       None)` lines and `next_lines`, return 0, start no probe.
    2. Start the probe: `Request(local.repository, local.checkout.default, want_prs=local.generated,
       generator=probe.generator_request(env) if local.generated else None)`.
    3. Terminal (`stdin.isatty() and stdout.isatty()`): print `view(local, None).lines`; join; move up over them
       (`\x1b[{n}F\x1b[J`) and print the joined view. Otherwise: join, then print the lines once (Call 1).
    4. No terminal: a blank line and `next_lines`, return 0. Terminal without Gum: the same, then `Gum is not
       installed, so there is no menu; run one of the commands above.`, return 0.
    5. Gum: `Gum(gum).choose('What next?', [a.item for a in actions] + ['Exit'])`, no `selected` (the cursor starts on
       the likely action). `Exit`, `Cancelled` or `KeyboardInterrupt` during the choice: print `Nothing ran.`, return
       0. Otherwise print `Running: ` + `command`, return `call(list(argv), cwd=root)`.
    6. `KeyboardInterrupt` before the choice: `probe.cancel()`, re-raise (`main` prints `Cancelled.`, returns 130).
       Every return path has joined or cancelled the probe.
  - Validation in `main`, before `run`:

    | Arguments | Result |
    |---|---|
    | `work` alone | `overview.run` |
    | `work` with no action and any of `--follow`, `--json`, `--port`, `--no-port`, `--rebind`, `--then-signin-all`, `--agent`, `--session` | `parser.error('task work takes no options; use task work:status -- --json for detail')` |
    | `--agent` or `--session` without `--forget`; `--forget` without both | `parser.error('--agent and --session go together with task work:forget')` |
    | `--rebind`, `--then-signin-all` with another action | today's errors (`__main__.py:398-401`) |

  - Root alias stanza for `work` (managed block; owner content untouched): `  work:\n    desc: Show where this project
    stands and what to do next\n    interactive: true\n    cmds:\n      - task: devcontainer:work\n        vars:
    {CLI_ARGS: "{{.CLI_ARGS}}"}\n`. The template and the generator's own `Taskfile.yml` gain `work:` with that `desc`,
    `interactive: true`, and `bash .devcontainer/scripts/project-sync.sh work {{.CLI_ARGS}}` / `bash
    scripts/project-sync.sh work {{.CLI_ARGS}}`.
- Pinned behavior that changes on purpose, each edited in this task:
  - `tests/test_project_sync_cli.py::test_agent_launchers_and_implicit_local_recovery_are_removed` (928-934): bare
    `work` now exits 0; the removed agent-launch options it checks stay rejected (exit 2).
  - `tests/test_root_tasks.py::test_native_agent_tasks_retired_from_owned_block_only` (93): the old `work` delegate is
    still retired by its exact text, but the new managed `work` stanza is present (`assertIn` of the stanza above).
  - `tests/test_continuity_integration.py::test_notes_and_root_aliases_preserve_existing_project_operations` (69-79):
    a root `work` task is now expected; every owner task it checks is still preserved.
  - `test_quoted_custom_work_preserved` stays green unchanged: an owner `"work"` keeps its task, no managed duplicate.

- [ ] **Step 1: Write the tests**

`tests/test_work_overview.py` (fake `gh`/`git` on `PATH` as in Task 2, a temp project and state; counting fakes for
`subprocess.Popen`/`call` argv, `terminal.gum_binary` and `Gum.__init__` without a binary):

```python
def test_no_terminal_prints_state_and_next_lines_and_exits_0(self):
    code = overview.run(root, env=env, stdin=NotTTY(), stdout=out, gum=None)
    self.assertEqual(code, 0); self.assertIn('Next: task work:cs', out.getvalue())
    self.assertLessEqual(len(frame_lines(out)), 6)
def test_printing_starts_downloads_and_signs_in_nothing(self):
    # every situation fixture from Task 3, no terminal: argv log has only the probe's reads; gum_binary never called;
    # state, checkout and cache trees byte-identical
def test_failures_still_print_and_exit_0(self):
    # unreadable conversation-forget.json, no gh on PATH, no git on PATH, state_dir refusing: frame + Next, exit 0
def test_deadline_holds_with_a_hung_github(self):       # fake gh sleeps 60 s: returns in < 3 s, 'within 2 s'
def test_terminal_redraws_the_first_frame(self):        # pending frame, then '\x1b[{n}F\x1b[J', then the final frame
def test_menu_runs_the_chosen_command_after_printing_it(self):
    # fake Gum returns actions[1].item -> 'Running: task work:cs:armed', call(['task', 'work:cs:armed'], cwd=root)
def test_ctrl_c_or_esc_at_the_menu_runs_nothing(self):
    # 'Exit', Cancelled, and KeyboardInterrupt raised inside choose -> 'Nothing ran.', 0, call not invoked;
    # and a real SIGINT to a fake gum child process gives the same
def test_ctrl_c_during_the_probe_leaves_nothing_running(self):
    # KeyboardInterrupt from the join: probe children gone; main() returns 130 with 'Cancelled.'
def test_inside_a_codespace_names_the_stop_only(self)
def test_without_gum_prints_next_lines_and_the_reason(self)
```

`tests/test_project_sync_cli.py`:

```python
def test_bare_work_runs_without_a_terminal(self):   # red today: argparse error
    result = self.run_cli('work')
    self.assertEqual(result.returncode, 0); self.assertIn('Next: ', result.stdout)
def test_bare_work_rejects_options(self):           # each option in the table -> 2, its message
```

`tests/test_root_tasks.py`: `test_work_alias_has_a_description` (managed stanza as above; `integrate` idempotent and
migrates an older managed block; `'  work:\n'` with the `desc` in the template and `Taskfile.yml`), plus the edits above.

- [ ] **Step 2: Run them; the new ones fail.**

Run: `python3 -m unittest tests.test_work_overview tests.test_project_sync_cli tests.test_root_tasks tests.test_continuity_integration -v`
Expected: exit 1; the failures are the new tests and the three pinned tests named above, nothing else.

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
- Modify: `devcontainer_setup/project_sync/replication.py` (`capture` at 34-50 gains `exclude`; the outbound
  `capture(project, store, env=env, relay=True)` at 311, inside the per-direction loop, passes
  `forget.forgotten(state)`; the loop's `except` at 334-338 keeps a `forget.Unreadable` message as the row error)
- Modify: `devcontainer_setup/project_sync/__main__.py` (`--forget` action with `--agent`, `--session`; handled before
  the terminal gate at 329), `devcontainer_setup/root_tasks.py` (`ALIASES` gains `'work:forget'`, not interactive),
  `templates/continuity-tasks.yml`, `Taskfile.yml`, `README.md` (command table)
- Test: `tests/test_forget.py` (new), `tests/test_root_tasks.py`

**Interfaces:**
- Consumes: `standing.refusals(state, project, binding)` and `standing.Refusal` (Task 1).
- Produces, in `forget.py`:
  - `FILE = 'conversation-forget.json'`: `{"schema": 1, "conversations": [{"agent", "session_id", "codespace",
    "reason", "forgotten_at"}]}`.
  - `class Unreadable(ValueError)`, message exactly `conversation-forget.json cannot be read; move it aside to send
    every conversation again`.
  - `forgotten(state: Path) -> frozenset[tuple[str, str]]`: `(agent, session_id)` pairs; empty when the file is
    missing; `Unreadable` when malformed. Project-wide: it holds across later rebinds.
  - `forget(project, state: Path, *, agent: str, session_id: str, now=time.time) -> str`, in order:
    1. Already in the record: return `{agent} conversation {session_id} was already forgotten on DATE.` (the
       record's `forgotten_at`), writing nothing.
    2. Take `workspace-control.lock`, blocking (a connect holds it only while it selects and verifies,
       `workspace.py:250-318`, and releases it before Herdr).
    3. Under it: read `scratch.json` (`Pair a Codespace first: run task work:cs` when missing); require
       `standing.refusals(state, project, binding)` to contain this pair, else `ValueError(f'{codespace} refused no
       {agent} conversation {session_id} in the last sync; nothing was forgotten.')`; append the record with
       `state.atomic_write`.
    4. Return `Forgot {agent} conversation {session_id}: it is no longer sent to {codespace} on its own, and nothing
       is deleted. The next sync clears the warning.` (a conversation that depends on it still carries its files).
  - `replication.capture(project, store, *, env=None, historical_roots=(), relay=False, exclude=frozenset())`: drops
    entries and capture issues whose top-level `(agent, session_id)` is in `exclude`, from both the native capture and
    the relayed entries, before `store.save` (Call 4: another entry's files are untouched). The inbound capture in
    `rpc.py:58-69` keeps the default. Relay records, checkpoint objects and native transcripts are not touched.
  - CLI: `work --forget --agent AGENT --session ID`; task `work:forget` with `desc: Stop sending a conversation the
    Codespace refused (task work:forget -- --agent claude --session ID)`, running `… work --forget {{.CLI_ARGS}}`.

- [ ] **Step 1: Write the tests** in `tests/test_forget.py` (the fixtures the existing replication tests use for a
  desktop store, relay records and a local-Python endpoint):

```python
def test_forgotten_conversation_leaves_the_next_outbound_snapshot(self):
    # one native and one relayed entry refused; forget both; run one cycle
    self.assertNotIn(('claude', native_id), manifest_pairs); self.assertNotIn(('codex', relayed_id), manifest_pairs)
    self.assertEqual(outbound_row['publication_issues'], [])        # the warning is cleared
    self.assertIn(('claude', other_id), manifest_pairs)              # everything else is still sent
def test_copies_of_the_forgotten_conversation_are_kept(self):
    # before forget and after the next cycle, equal hashes of: its native transcript files on the Mac, its relay
    # record and the blobs it names, and the remote side's existing native copy of it
def test_a_dependent_conversation_still_carries_its_ancestor(self):
    # child entry depends on the forgotten parent's files: the child's entry and closure still pass preflight
def test_unknown_conversation_is_refused_and_writes_nothing(self)
def test_repeat_after_the_cycle_says_already_forgotten(self)   # after the warning cleared, and after a rebind
def test_rebind_during_forget_is_rechecked_under_the_lock(self):
    # the binding changes while forget waits for workspace-control.lock -> refused, nothing written
def test_unreadable_forget_file_fails_the_outbound_capture_with_its_message(self)
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

Wave 1: Tasks 1 and 2 in parallel, both from the final plan commit. Wave 2, from the merge of 1 and 2: Tasks 3 and 5
in parallel (5 needs only Task 1's `standing.refusals`). Wave 3: Task 4 from the merge of 3 and 5. Tasks 4 and 5 both
edit `__main__.py`, `root_tasks.py`, the template, `Taskfile.yml` and `README.md`, in different stanzas. The lead
merges each into `feature/task-work-ab` after its own re-gate.

## Live check (part of the A+B release gate, not of these tasks)

On innovate with the 2026-10-27 backup runbook: `task work` in a terminal (menu, redraw, a chosen action runs) and
piped (`Next:` lines, exit 0) for an available armed pairing, after `task work:cs:stop` (stopped by you), and with the
network off (bounded, exit 0); no new file in the state dir from the printing alone.

**Correction after the 2026-11-09 run.** With the network off, an armed pairing never shows `GitHub did not answer
within 2 s`: when GitHub does not answer, the view falls back to the keeper's last reading, `(as of HH:MM)`
(`overview._pair`; `standing.read` takes it whenever `keeper-runtime.json` names the pairing), and an owner stop prints
from local facts. The "did not answer" reason appears only with no keeper reading. The run met every other line of
this check; it found the in-Codespace stop's answer arriving after the Codespace went down, fixed in a228b33.
