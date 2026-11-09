# `task work` stage C: check a Codespace, then delete it on a typed word. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `task work:cs:delete [-- --codespace NAME]` checks a Codespace's Git, scratch, conversations and agents
over the read-only connection, prints a per-category verdict naming its scope, and deletes only after the owner types
`delete` in a terminal, the check repeats unchanged, the stop is recorded and the Codespace is confirmed Shutdown.
Then it cleans up what this Mac kept for it, journaled and retried until done. `-- --check --json` checks without
any destructive call.

**Architecture:** Five runtime modules, vendored into generated projects like every `project_sync/*.py`:
`target_scan.py` runs one bounded, read-only inventory on the Codespace (Git, scratch, conversation files, agent
processes) and fingerprints it; `pushed.py` proves on the Mac that each commit it found is on GitHub; `seal.py`
reads and writes the record of a final flush (stage D writes it when it replaces a Codespace); `verdict.py` turns
scan, proof, sync state and seal into four categories and renders them; `deletion.py` runs the check, the typed
confirmation, the journaled stop and DELETE, and the cleanup. Small cleanup primitives join `ssh_hosts.py`,
`herdr.py`, `workspace.py` and `scratch.py`; `lifecycle.LifecycleAPI` gains DELETE.

**Tech Stack:** Python 3 standard library (vendored, run as `python3 -I -B`), go-task, `unittest`, Git, Mutagen
(already pinned).

**Spec:** `docs/superpowers/specs/2026-11-02-task-work-design.md`, section "C. Checked delete", its Goals,
Anti-goals, Constraints, Owner answers 1-2, "Tests the plan must include" (C list), calls 2 and 4, and builder-check
calls 12-13. Source inventory at b4a8f6c:
`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-c/inventory/INVENTORY.md` (sha256
`0637bb21f40b…`, cited as INV §n). Line numbers are at b4a8f6c.

## Global Constraints

- Gates: `task check` and `task test` (`python3 -m unittest discover -s tests -v`), both exit 0, at your commit, run
  in the default environment (no sandbox, no `TMPDIR` override):
  `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-a/RULING-gates.md`.
- Focused runs: run `python3 -m unittest …` unpiped (or redirect to a file and read `$?`); a `| tail` hides the exit code.
- Branches: cut from `feature/task-work-cd` at the commit your brief names; merge back into it, never `main`. C and D
  ship together after one live test of replace end to end (spec call 1).
- Commit messages: plain imperative sentence case, one concern per commit, no AI attribution of any kind.
- **Nothing destructive before the typed word.** The check path (`--check`, EOF, a wrong word, no terminal, any
  refusal) makes zero calls to `post_stop`, `delete`, `post_start`, `power.stop`, `arming.record_owner_stop`, any
  cleanup primitive, and never acknowledges, publishes or captures conversations. Tests count these calls.
- `ReadOnlyAPI` stays GET-only (`transport.py:114-185`). DELETE exists only on `lifecycle.LifecycleAPI`.
- Remote Python is not a sandbox (INV §2): the scan program only reads. It never writes, creates, locks or deletes on
  the Codespace, and never runs a Git command that writes (`GIT_OPTIONAL_LOCKS=0` on every Git call).
- "Deleted" needs `LifecycleAPI.confirm_deleted() is True` (a complete same-account inventory without the name). A
  404, a timeout, or an inventory of 100 or more rows (`_deleted_from_list` returns None) never means deleted (#60).
- Never print or store contents, remote URLs, process command lines or credentials. Shown and JSON values are fixed
  texts plus allowlisted paths, counts, ref names, 12-hex commit ids, agent names and session ids; every untrusted
  string shown on a terminal goes through `overview.fit`. Git and gh stderr is discarded, never echoed.
- `arming.json` is written only for the paired target (the owner stop through `power.stop`, then the disarm). For any
  other target its bytes stay identical (CHECK C1).
- Stdlib only. New state files use `state.atomic_write` (private, atomic). No new key in the user `config.json`.
- Do not name a module `work.py` (`render.RETIRED_HELPERS`, `devcontainer_setup/render.py:26-27`).
- No client, organization or Codespace names in code, tests, docs or commit messages (the repository is public).
- Times shown are local `HH:MM`, or `YYYY-MM-DD HH:MM` when not today (as `overview` shows them).

## Calls made by the lead (each can be overruled)

1. **Four kinds of target** (INV §1). `paired`: the name in `scratch.json`. `sealed`: a retired Codespace with a
   final-flush record `STATE/retired/<name>.json` (stage D writes it). `moved`: a retired Codespace named only by a
   `retired-bindings/*/scratch.json` archive (a plain `--rebind`; the newest archive naming it wins); its Git and
   Agents are checked, its Scratch and History read UNKNOWN because no final flush was recorded. `unpaired`: none of
   these; nothing connects and every category reads UNKNOWN, "pair it first" (CHECK Q9). The exception for a NEW
   that a replace journal names belongs to stage D.
2. **The final-flush record is defined here, written by D** (`seal.write` and `seal.read` both in Task 3), so D only
   calls `target_scan.scan` and `seal.write` after a completed cycle with nothing pending and no capture issues.
3. **History for the paired target** is `sync_cycle.summary(...)['healthy']` plus no capture issues. A capture keeps
   the completed-turn prefix and does not report a turn left unfinished after it (INV §5,
   `tests/test_native_checkpoints.py::test_completed_prefix_while_writer_appends_partial_next_turn`), so "a turn an
   agent never finished" joins the owner's "Not checked" list (ruling 29: list them, allow delete). Conversations the
   paired Codespace refused, and forgotten ones, are named on `Kept on this Mac only` and do not block (ruling 30).
4. **Git proof** (CHECK Q3). The scan returns each repository's credential-free identity (`state._identity` runs on
   the Codespace; the raw URL never leaves it). A repository whose identity is the project's is proved in the Mac
   checkout: one fetch of every head into a private namespace with `--prune`, then each commit must exist and be
   contained in a fetched head; the namespace is removed afterwards. Another GitHub repository is proved only by
   equality with a head `git ls-remote` advertises, else UNKNOWN. A repository without `origin` that has commits is
   UNSAFE. `.devcontainer-work` is walked (older remote checkouts of the same repository; `roots()` skips it).
5. **Agents**: a process whose argv has any element whose basename is `claude`, `codex`, `grok` or `opencode` blocks
   (npm installs run as `node …/bin/claude`). A Herdr server alone is reported in JSON and does not block.
6. **Fingerprint** = sha256 of the scan's canonical JSON (every part except its time and problems), None unless every
   part is complete. The typed word is followed by a fresh scan while the Codespace is still Available; any change,
   a changed account, a changed target kind or a state other than Available voids the verdict.
7. **Stopping.** Paired: `power.stop(state, expected=NAME, api=api)`, which records the owner stop in `arming.json`
   before the POST (`power.py:88-147`). Sealed or moved: the delete journal records the stop, then
   `api.post_stop()`; `arming.json` is not touched.
8. **Reruns.** A journal records each phase. A rerun whose journal says `deleted` resumes cleanup only. Any other
   rerun checks from scratch: a target left Shutdown reads UNKNOWN with its start action (spec: "needs an explicit
   start to check again"). `--start` exists for sealed and moved targets only, needs a terminal, and waits for
   Available; the paired target's action is `task work:cs:restart`, which records start evidence for the keeper
   (`_owner_power`), so `deletion.py` never POSTs start for it.
9. **A `delete` subcommand** of `project-sync.sh` (`delete [--codespace NAME] [--check] [--json] [--start]`), not a
   `work` flag: `work --codespace` is already a boolean action (INV §9). Exit 0 when the check is deletable (with
   `--check`) or the delete and cleanup finished; 1 otherwise; 2 for usage errors.
10. **`task work` offers delete only for a sealed retired Codespace GitHub still lists** (one action, the oldest
    seal), plus a repair action when a cleanup is pending. It never offers deleting the current pairing: that is the
    daily Codespace, and the command remains. Ruling 8's "reached from `task work`" is met by the retired one.
11. **A Herdr profile is removed** when `herdr-machine.json` names a machine id that `herdr._matching_machines`
    lists for this project's alias and session (`devcontainer-<project_id>`, `project-<project_id>`, names only setup
    uses); otherwise only the receipt is dropped (INV §8).
12. **No paginated inventory.** `confirm_deleted` stays single-page; an account with 100 or more Codespaces reads
    "not confirmed" and the rerun says so (YAGNI).

## Copy (exact strings, used by Tasks 3 and 5)

`NAME` is the Codespace name, `HH:MM` local time, `CMD` a command built by `overview.task_command` and shown with
`shlex.join`. A category line is two spaces, the category padded to 8 columns, then the text. UNSAFE text starts with
`Not safe: `, UNKNOWN text with `Unknown: `; SAFE text has no prefix. Lists in a line keep the first 3 items, then
`and N more`; JSON keeps up to 100.

```
Checked NAME at HH:MM:
  Git      N repositories, M worktrees: no changes, stashes or unpushed commits
  Scratch  synchronized at HH:MM (N files)
  History  nothing waiting to reach this Mac
  Agents   none running
Kept on this Mac only: codex SESSION_ID, claude SESSION_ID
Not checked: files Git ignores, home-folder data outside sync,
Grok and OpenCode conversations, and a turn an agent never finished.
Type delete to delete it.
```

- Singulars: `1 repository`, `1 worktree`, `1 file`. No repositories: `no repositories under /workspaces`.
- Sealed SAFE: Scratch `unchanged since the final flush at HH:MM (N files)`; History `no turn newer than the final
  capture at HH:MM`.
- Git UNSAFE items, joined by `; `: `PATH: N changed files (a, b, c and N more)`, `PATH: N stashes`,
  `PATH: BRANCH at 0123456789ab is not on GitHub`, `PATH: no remote, so its commits exist only there`. `PATH` is the
  repository or worktree path relative to `/workspaces` (`.` for `/workspaces` itself), absolute when outside it;
  a detached head is shown as `HEAD`.
- Git UNKNOWN: `could not read every repository under /workspaces`, or items `PATH: BRANCH: REASON` with REASON one
  of `GitHub could not be read from this Mac`, `this Mac has no checkout of OWNER/REPO to compare`,
  `not a GitHub repository`.
- Scratch: UNSAFE `N conflicts (…)` (paired), `N files changed or added since the final flush (…)` (sealed); UNKNOWN
  `sync is not running; run CMD, then check again` (CMD = `work:cs`), `sync has not caught up yet; check again in
  a minute`, `could not read .scratch on NAME`, `moved with --rebind, so no final flush was recorded`.
- History: UNSAFE `N conversations have not reached this Mac yet`, `N conversations could not be read on NAME`,
  `N conversation files changed since the final capture`; UNKNOWN `sync has not finished a cycle in the last 5
  minutes; run CMD, then check again`, `the final capture is no longer on this Mac`, the `moved` text above.
- Agents: UNSAFE `claude (2), codex (1) running: end them, then check again`; UNKNOWN `could not read processes on
  NAME`.
- Unreachable (all four UNKNOWN with one reason): `NAME is stopped`, `NAME is STATE`, `GitHub did not answer about
  NAME`, `could not connect to NAME`, `this Mac has never connected to NAME; pair it first`. After the verdict, one action line:
  `To check it, start it: CMD` (sealed or moved Shutdown; CMD = `work:cs:delete -- --codespace NAME --start`),
  `To check it, restart it: CMD` (paired Shutdown; CMD = `work:cs:restart`), `Check again once it is Available.`
- Not deletable: `Nothing was deleted. Resolve the lines marked Not safe or Unknown, then check again.`
- No terminal: `Nothing was deleted: deleting needs a terminal. Run CMD in one.` EOF or another word:
  `Nothing was deleted.`
- Changed: `NAME changed since the check (WHAT); nothing was deleted. Run the delete again to check it again.` with
  WHAT one of `its files, repositories or agents`, `the GitHub account`, `its pairing`, `its state`, `the connection
  to it` (the rescan could not complete).
- `NAME could not be stopped (REASON); nothing was deleted.` · `NAME has not reached Shutdown after 10 minutes;
  nothing was deleted. Run the delete again to check it again.` · `GitHub refused to delete NAME (HTTP N). It stays
  stopped; nothing else changed.` · `GitHub accepted the delete but does not show NAME as gone yet. Run the delete
  again to finish.`
- Done: `Deleted NAME.` · paired and armed: `Recovery is off: NAME was deleted.` · `Cleaned up: STEP, STEP.` with
  STEP from `keeper`, `sync worker`, `pairing`, `SSH alias`, `Herdr profile`, `sync session`.
- `Deleted NAME, but cleanup is not finished: STEP (REASON). Run CMD to finish it.` · `NAME was deleted at DATE;
  nothing is left to clean up.` · `NAME is already deleted.` · `NAME is already deleted; cleaning up what this Mac
  kept for it.`
- Refusals: `Run CMD on your Mac; it needs the Mac's key and state.` · `NAME was paired as LOGIN, but gh is signed
  in as LOGIN. Switch with gh auth switch, then run the delete again.` · `NAME belongs to another repository;
  nothing was checked.` · `Another delete for this project is running; wait for it to finish.` · `No Codespace is
  paired. Name one: CMD` (CMD = `work:cs:delete -- --codespace NAME`) · `--start needs a terminal.` · `To start the
  paired Codespace, use CMD.` (CMD = `work:cs:restart`) · `NAME did not become Available within 10 minutes; nothing
  was checked.` · `Unset GH_TOKEN before deleting a Codespace; it must use a real gh account.` (and `GITHUB_TOKEN`).

## Review Focus

1. **A second worktree, a nested repository or an old `.devcontainer-work` checkout** holding the only copy of a
   change: it is found and blocks (Task 1 `test_every_repository_and_worktree_under_workspaces_is_found`, Task 3
   `test_dirty_second_worktree_or_stash_is_unsafe`).
2. **A remote branch deleted after an earlier check**: a stale private ref never proves it pushed (Task 2
   `test_branch_deleted_on_the_remote_is_unpushed_on_a_later_check`).
3. **The owner edits a scratch file or an agent starts during the prompt**: the recheck voids the verdict and nothing
   stops (Task 5 `test_a_change_during_the_prompt_voids_the_verdict`).
4. **Deleting a retired OLD while NEW is armed**: NEW's `arming.json` is byte-identical and NEW's next lifetime stop
   is still restarted by its keeper (Task 5 `test_retired_delete_keeps_new_recovery_working`).
5. **A crash or timeout between phases** (after the stop, after the DELETE request, between cleanup steps): the
   rerun never deletes on a stale verdict, never repeats a done cleanup step, and never calls a 404 deleted (Task 5
   `test_each_phase_resumes_safely`).

---

### Task 1: One read-only inventory of the Codespace

**Files:**
- Create: `devcontainer_setup/project_sync/target_scan.py`
- Test: `tests/test_target_scan.py` (new)

**Interfaces:**
- Produces (frozen dataclasses):
  - `Limits`: `entries: int = 200_000` (directory entries walked under `/workspaces`), `repositories: int = 64`,
    `worktrees: int = 256`, `files: int = 100_000` (scratch plus conversation files), `hashed_bytes: int = 4 * 1024**3`,
    `seconds: float = 120`, `output: int = 16 * 1024 * 1024`. `LIMITS = Limits()`.
  - `Worktree`: `path: str`, `head: str | None` (40-hex, None when unborn), `branch: str | None` (short name, None
    when detached), `changes: int`, `changed: tuple[str, ...]` (at most 20 paths), `ok: bool`.
  - `Repository`: `path: str`, `identity: str | None` (`state._identity(origin)` computed on the Codespace; None
    without `origin`), `branches: tuple[tuple[str, str], ...]` (short name → 40-hex, sorted), `stashes: int`,
    `worktrees: tuple[Worktree, ...]`, `bare: bool`, `ok: bool`.
  - `Scan`: `codespace: str`, `at: float`, `repositories: tuple[Repository, ...]`, `git_complete: bool`,
    `scratch: tuple[tuple[str, str], ...]` (relative path → sha256), `scratch_complete: bool`,
    `sessions: tuple[tuple[str, str], ...]` (`codex/<path under CODEX_HOME/sessions>` or
    `claude/<path under the Claude home's projects>` → sha256), `sessions_complete: bool`,
    `agents: tuple[tuple[str, int], ...]` (name → count, for `claude codex grok opencode herdr`),
    `agents_complete: bool`, `problems: tuple[str, ...]` (fixed texts).
  - `scan(connection, codespace: str, remote_root: str, *, limits: Limits = LIMITS, workspaces: str = '/workspaces',
    proc: str = '/proc', now=time.time) -> Scan`. One `connection.python(code, payload=…, timeout=limits.seconds +
    30)`. The payload carries `state.py`, `scratch_tree.py` and `sessions.py` sources base64-encoded, as
    `scratch._remote` ships them (`scratch.py:82-112`), plus the limits and paths. Never raises for a failed or
    malformed answer: a `ValueError` from the connection, output above `limits.output`, or JSON that fails strict
    validation (types, 40-hex ids, no control characters in paths) gives every part `complete=False` and one problem.
  - `fingerprint(scan: Scan) -> str | None`: sha256 hex of `json.dumps(…, sort_keys=True, separators=(',', ':'))` of
    every field except `at` and `problems`; None unless all four `*_complete` are true.
- Rules the program follows on the Codespace:
  - Git walk: `os.scandir` from `workspaces`, never following symlinks. A directory with a `.git` entry (directory
    or file), or a bare repository (`HEAD` file plus `objects/` and `refs/` directories), is a candidate; never descend
    into a `.git` directory or an `objects/` of a bare repository. Prune at any depth the non-anchored names in
    `scratch_tree.MUTAGEN_IGNORES` (`node_modules`, `.venv`, `.cache`, `__pycache__`, …): they hold tool output, not
    work. Do not prune `.devcontainer-work` or `.scratch` (Mutagen ignores VCS directories, so a repository in
    `.scratch` exists only there).
  - Group candidates by `git rev-parse --path-format=absolute --git-common-dir`. Per repository: `for-each-ref
    --format='%(refname:short) %(objectname)' refs/heads`, stashes by `rev-list --walk-reflogs --count refs/stash`
    (absent ref = 0), `config --get remote.origin.url` passed only to `state._identity`, `worktree list --porcelain
    -z`. Per worktree: `status --porcelain=v1 -z --untracked-files=all`, `symbolic-ref -q --short HEAD`, `rev-parse
    -q --verify HEAD`. Every Git call: `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, timeout 15 s; a
    failure marks only that repository `ok=False`.
  - Scratch: `scratch_tree.entries(<remote_root>/.scratch)`; hash each regular file. Missing `.scratch` = empty map,
    complete. An error from `entries` (symlink, special file, case collision) = incomplete.
  - Conversations: walk `sessions.native_home('codex')/'sessions'` and `sessions.native_home('claude')/'projects'`
    without following links; hash regular files. Missing directory = empty, complete.
  - Agents: for each numeric entry of `proc`, read `cmdline`, split on NUL, take the basename of every element;
    count each of the five names once per process. Skip this process and its ancestors (`stat` ppid chain). A
    process gone mid-read is skipped; an unreadable `proc` = incomplete. Only names and counts leave the Codespace.
  - Any limit reached makes that part incomplete. The program writes nothing anywhere.

- [ ] **Step 1: Write the failing tests** in `tests/test_target_scan.py`. A `LocalConnection` fake runs
  `[sys.executable, '-I', '-B', '-c', code]` with the payload on stdin, `HOME` and `CODEX_HOME` in a temp dir, and
  returns stdout (as `tests/test_replication.py:19-38` does). Repositories are real temp Git repos under a temp
  `workspaces`; `proc` is a temp tree of `<pid>/cmdline` and `<pid>/stat` files.

```python
def test_every_repository_and_worktree_under_workspaces_is_found(self):
    # main checkout clean; linked worktree with an untracked file; nested repo; bare repo; .devcontainer-work/old clone;
    # a repo inside .scratch; a repo inside node_modules (pruned)
    self.assertEqual(sorted(r.path for r in scan.repositories), [...expected, node_modules one absent])
    self.assertEqual(second_worktree.changes, 1)
def test_stash_and_detached_head(self):        # stashes == 1; detached worktree: branch None, head == oid
def test_scratch_map_follows_the_sync_policy(self):  # root README.md and node_modules/x excluded; notes/a.md hashed
def test_scratch_symlink_makes_only_scratch_incomplete(self)
def test_conversation_files_are_hashed_for_codex_and_claude(self)
def test_agents_match_any_argv_basename(self):
    # 'node /usr/lib/bin/claude', '/usr/bin/codex exec', 'herdr server' -> claude 1, codex 1, herdr 1
def test_the_scan_itself_is_not_an_agent(self)
def test_a_limit_makes_its_part_incomplete(self):    # Limits(entries=5) -> git_complete False, others True
def test_failed_connection_is_incomplete_not_empty(self)
def test_remote_url_never_leaves_the_codespace(self):
    # origin https://user:secret@github.com/o/r.git -> identity equals state._identity(...); b'secret' not in raw output
def test_fingerprint(self):  # equal for equal scans; differs after an edit, a new branch, a stash, a new agent;
                             # None when any part is incomplete
def test_scan_changes_nothing(self):  # every file and every ref under workspaces and HOME hashed before and after
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_target_scan -v`. Expected: exit 1,
  `ModuleNotFoundError: No module named 'devcontainer_setup.project_sync.target_scan'`.
- [ ] **Step 3: Implement** `target_scan.py` as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add devcontainer_setup/project_sync/target_scan.py tests/test_target_scan.py &&
  git commit -m "Inventory a Codespace's repositories, scratch, conversations and agents read-only"`

### Task 2: Prove on the Mac that commits are on GitHub

**Files:**
- Create: `devcontainer_setup/project_sync/pushed.py`
- Test: `tests/test_pushed.py` (new)

**Interfaces:**
- Consumes: `target_scan.Repository`, `target_scan.Worktree` (Task 1; the test builds them directly).
- Produces:
  - `Proof` (frozen): `path: str`, `pushed: int`, `unpushed: tuple[tuple[str, str], ...]` (ref, 12-hex),
    `unknown: tuple[tuple[str, str], ...]` (ref, REASON from the Copy section), `no_remote: bool`.
  - `prove(project, repositories, *, timeout: float = 60) -> tuple[Proof, ...]`, one Proof per repository, in order.
    The refs proved are every branch plus each detached worktree head (`HEAD`).
  - `CHECK_NAMESPACE = 'refs/project-sync-check/heads/'`.
- Rules:
  - `identity == project.identity`: at most one fetch per call, in `project.root`: `git -c credential.interactive=never
    fetch --quiet --prune --no-tags --no-write-fetch-head --no-recurse-submodules origin
    '+refs/heads/*:refs/project-sync-check/heads/*'`, with `GIT_TERMINAL_PROMPT=0` and `timeout`. The owner's own
    credential configuration is kept (a private repository needs it). A failed fetch makes every ref of those
    repositories unknown (`GitHub could not be read from this Mac`). A ref is pushed when `git cat-file -e
    <oid>^{commit}` succeeds and `git for-each-ref --count=1 --contains <oid> refs/project-sync-check/heads/` prints a
    line; otherwise unpushed. In `finally`, every ref under the namespace is deleted (`update-ref --stdin`).
  - Another identity starting `github.com/`: `git ls-remote --heads <_github_url(identity)>` (cwd `/`, same env and
    timeout); a ref whose id equals an advertised head is pushed, otherwise unknown (`this Mac has no checkout of
    OWNER/REPO to compare`); a failed ls-remote is unknown (`GitHub could not be read from this Mac`). Any other
    identity: unknown (`not a GitHub repository`). `_github_url(identity) -> str` builds
    `https://github.com/OWNER/REPO.git` (tests patch it to a local path).
  - `identity is None` with any branch or head: `no_remote=True`. A repository without commits proves nothing and
    is fine. stderr is discarded; no message carries a URL.

- [ ] **Step 1: Write the failing tests** in `tests/test_pushed.py` (a local bare `origin`, the Mac clone as
  `project.root`, and a third clone standing in for the Codespace, whose ids feed hand-built `Repository` values):

```python
def test_branch_at_a_remote_head_is_pushed(self)
def test_ancestor_of_a_remote_head_is_pushed(self):      # Codespace main two commits behind origin/main
def test_commit_the_mac_cannot_find_is_unpushed(self):   # id only in the third clone -> unpushed (ref, id[:12])
def test_branch_deleted_on_the_remote_is_unpushed_on_a_later_check(self):
    # prove() -> pushed; delete the branch on origin; prove() again -> unpushed
def test_check_refs_are_removed_and_owner_refs_untouched(self):
    # refs/remotes/*, branches, HEAD, index, FETCH_HEAD byte-identical; no ref under refs/project-sync-check/ left
def test_failed_fetch_is_unknown_not_unpushed(self)
def test_no_remote_with_commits_is_flagged(self)
def test_other_repository_needs_an_equal_head(self):     # equal -> pushed; ancestor -> unknown with its reason
def test_detached_head_is_proved_as_HEAD(self)
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_pushed -v`. Expected: exit 1,
  `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `pushed.py` as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add devcontainer_setup/project_sync/pushed.py tests/test_pushed.py && git commit -m
  "Prove on the Mac that a Codespace's commits are on GitHub"`

### Task 3: The final-flush record and the verdict

**Files:**
- Create: `devcontainer_setup/project_sync/seal.py`, `devcontainer_setup/project_sync/verdict.py`
- Modify: `devcontainer_setup/project_sync/workspace.py:185-197` (extract the cycle reader below; `status()` keeps
  its output)
- Test: `tests/test_seal.py`, `tests/test_verdict.py` (new); `tests/test_workspace*.py` stays green unchanged

**Interfaces:**
- Consumes: `target_scan.Scan`, `pushed.Proof` (Tasks 1-2), `standing.refusals` (`standing.py:301-338`),
  `forget.forgotten` (`forget.py`), `sync_cycle.summary` (`sync_cycle.py:117-141`), `scratch.inspect_sync`
  (`scratch.py:252-263`), `scratch.status`, `checkpoint_store.Store.load`, `overview.fit`, `overview.task_command`.
- Produces, `seal.py`:
  - `Seal` (frozen): `codespace`, `identity`, `repository`, `gh_user`, `remote_root`, `operation: str`,
    `sealed_at: float`, `scratch: tuple[tuple[str, str], ...]`, `sessions: tuple[tuple[str, str], ...]`,
    `snapshot_id: str` (64-hex), `capture_issues: int`.
  - `write(state, scan, *, identity, repository, gh_user, remote_root, operation, snapshot_id, capture_issues, now)
    -> Path`: refuses (`ValueError`) a scan whose `scratch_complete` or `sessions_complete` is false, or an invalid
    snapshot id; writes `STATE/retired/<codespace>.json` (`{"schema": 1, …}`, the fields above) with
    `state.atomic_write`; replaces an older seal for the same name.
  - `read(state, codespace) -> Seal | None`: None when absent; `ValueError('The final-flush record for NAME cannot be
    read')` when malformed or naming another identity.
  - `names(state) -> tuple[str, ...]`: Codespaces with a valid seal, sorted by `sealed_at`.
- Produces, `workspace.py`: `cycle(project, state, binding) -> tuple[dict, dict | None]`: the worker's
  `conversations` cycle scoped exactly as `status()` scopes it today (`workspace.py:350-365`) and the Mac's local
  receipt (`_conversation_receipt`), with no network and no lock; `status()` calls it.
- Produces, `verdict.py`:
  - `SAFE, UNSAFE, UNKNOWN = 'safe', 'unsafe', 'unknown'`; `CATEGORIES = ('Git', 'Scratch', 'History', 'Agents')`.
  - `Target` (frozen): `name`, `kind` (`paired`, `sealed`, `moved`, `unpaired`), `remote_root: str | None`,
    `seal: Seal | None`, `archive: Path | None` (the newest `retired-bindings/*` whose `scratch.json` names it).
  - `Category` (frozen): `name`, `verdict`, `text` (a Copy line without the label), `items: tuple[str, ...]`.
  - `Verdict` (frozen): `target`, `checked_at`, `account`, `repository`, `categories: tuple[Category, ...]`,
    `kept: tuple[tuple[str, str], ...]`, `fingerprint: str | None`, `action: str | None` (the unreachable action
    line); property `deletable` = every category SAFE and `fingerprint` not None.
  - `resolve(project, state, name: str | None) -> Target`; `ValueError` with the `No Codespace is paired` copy when
    `name` is None and nothing is paired. Order: paired, then sealed, then moved, else unpaired.
  - `judge(target, scan, proofs, *, sync: dict | None, cycle: tuple[dict, dict | None] | None, kept, store, now)
    -> tuple[Category, ...]`: the rules below; `sync` is `inspect_sync`'s result plus `scratch.status`, both for the
    paired target only.
  - `unreachable(target, reason: str) -> tuple[Category, ...]`: four UNKNOWN categories with that reason.
  - `kept_conversations(state, project, binding) -> tuple[tuple[str, str], ...]`: the paired Codespace's refusals
    plus forgotten conversations, unique and sorted; `run` passes it to `judge(kept=…)` and `Verdict.kept`.
  - `lines(verdict, local) -> list[str]` (the Copy section's human verdict, untrusted strings through `overview.fit`,
    each line at most 78 cells) and `as_json(verdict) -> dict` with exactly: `schema` (1), `codespace`, `target`
    (kind), `checked_at` (UTC ISO 8601), `account`, `repository`, `categories` (list of `{name, verdict, text,
    items}`, names lower-case), `kept_on_this_mac_only` (list of `{agent, session_id}`), `not_checked` (the four
    texts), `deletable`, `fingerprint`.
- Rules `judge` follows:
  - Git: UNKNOWN when `scan.git_complete` is false or any repository is not `ok`; UNSAFE when any worktree has
    changes, any repository has stashes, any proof has `unpushed` or `no_remote`; else UNKNOWN when any proof has
    `unknown`; else SAFE. UNSAFE outranks UNKNOWN (its items are listed first, then the unknown ones).
  - Scratch: paired: `conflicts` phase → UNSAFE; `synchronized` with a worker that `sync_cycle.fresh_worker` accepts →
    SAFE with its `last_synchronized_at` and `len(scan.scratch)`; stopped → UNKNOWN `sync is not running`; else
    UNKNOWN `has not caught up`. Sealed: `scratch_complete` and every `(path, sha)` in `scan.scratch` is in
    `seal.scratch` → SAFE (deleted files are fine); else UNSAFE listing the new or changed paths. Moved: UNKNOWN.
    `scratch_complete` false → UNKNOWN `could not read .scratch`.
  - History: paired: `sync_cycle.summary(cycle, current=True, local_receipt=receipt)` healthy and the
    `codespace_to_desktop` row has no `capture_issues` → SAFE; pending > 0 → UNSAFE `have not reached`; capture
    issues → UNSAFE `could not be read`; otherwise UNKNOWN `has not finished a cycle`. Sealed: `sessions_complete`,
    `seal.capture_issues == 0`, every `(path, sha)` in `scan.sessions` is in `seal.sessions`, and
    `store.load(seal.snapshot_id)` succeeds → SAFE; a changed or new file → UNSAFE; a failed load → UNKNOWN `no
    longer on this Mac`. Moved: UNKNOWN.
  - Agents: `agents_complete` false → UNKNOWN; any of `claude codex grok opencode` above 0 → UNSAFE; else SAFE.

- [ ] **Step 1: Write the failing tests** (`tests/test_seal.py`, `tests/test_verdict.py`; scans and proofs built by
  hand, a temp state dir, a temp checkpoint `Store` with one saved manifest):

```python
# test_seal.py
def test_write_then_read_round_trips(self)
def test_incomplete_scan_or_bad_snapshot_is_refused(self)
def test_malformed_or_foreign_seal_is_an_error_not_absent(self)
# test_verdict.py
def test_resolve_kinds(self):       # paired; sealed; moved picks the newest of two archives naming it; unpaired
def test_dirty_second_worktree_or_stash_is_unsafe(self)
def test_unpushed_commit_is_unsafe_and_unknown_proof_is_unknown(self)
def test_paired_scratch_and_history(self):   # synchronized+fresh -> SAFE; conflicts -> UNSAFE; stopped -> UNKNOWN
def test_paired_history_needs_a_healthy_cycle_without_capture_issues(self)
def test_sealed_late_scratch_or_newer_turn_is_unsafe(self):   # a new scratch file; a changed codex session file
def test_sealed_lost_capture_is_unknown(self)                  # manifest removed from the store
def test_moved_target_cannot_be_deleted(self):                 # Git+Agents SAFE, Scratch+History UNKNOWN
def test_failed_process_read_is_unknown(self)
def test_kept_conversations_are_named_and_do_not_block(self):  # a refusal and a forgotten entry; deletable True
def test_verdict_lines_match_the_copy(self):   # the spec example frame, byte for byte, for a SAFE paired verdict
def test_untrusted_names_never_reach_the_terminal_raw(self):   # escape sequences in a path, branch and session id
def test_json_has_exactly_the_documented_fields_and_no_urls(self)
def test_cycle_reader_matches_status(self):   # workspace.cycle(...) equals what status() reported for the same fixture
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_seal tests.test_verdict -v`. Expected:
  exit 1, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `seal.py`, `verdict.py` and `workspace.cycle` as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add -A devcontainer_setup/project_sync tests && git commit -m "Judge a Codespace
  safe to delete per category, against its final flush when it was replaced"`

### Task 4: Cleanup primitives

**Files:**
- Modify: `devcontainer_setup/project_sync/ssh_hosts.py` (add `remove`), `devcontainer_setup/project_sync/herdr.py`
  (add `remove_profile`), `devcontainer_setup/project_sync/workspace.py` (add `retire`),
  `devcontainer_setup/project_sync/scratch.py` (add `terminate_retired`)
- Test: `tests/test_ssh_hosts.py`, `tests/test_herdr.py`, `tests/test_scratch_sync.py`, `tests/test_retire.py` (new)

**Interfaces:**
- Produces (each idempotent, each returns `'removed'`/`'terminated'` or `'absent'`, each raises `ValueError` with a
  fixed REASON and changes nothing when ownership cannot be shown):
  - `ssh_hosts.remove(state: Path) -> str`: refuses while `herdr-ssh.pending.json` exists (`SSH setup did not
    finish`). Removes the exact `# devcontainer-setup:<alias>` block (the bytes `_block(alias, include)` produces,
    present once) from `~/.ssh/config`, keeping every other byte and the file mode, atomically; then the include file
    when its sha256 equals the receipt's; then the receipt and context. A block or include that differs:
    `the SSH alias was edited; remove it by hand`. The order makes a crash replay-safe (block, include, receipt).
  - `herdr.remove_profile(project, state: Path) -> str`: no `herdr-machine.json` → `'absent'`. When the receipt's
    `machine_id` is among `_matching_machines(alias, session)` → `herdr machine remove <id>` (nonzero: `Herdr did not
    remove the profile`, receipt kept), then the receipt is removed → `'removed'`; otherwise only the receipt is
    removed → `'absent'`. Herdr not installed → receipt removed, `'absent'`.
  - `workspace.retire(project, state: Path, codespace: str) -> Path`: under `workspace-control.lock`. Refuses unless
    `scratch.json` names `codespace`, and while `scratch.status(state)['running']` or `keeper.status(state)` shows a
    live keeper (`stop the keeper and sync worker first`). Copies `scratch.json`, `scratch-session.json` (if any) and
    `workspace.json` into a new `retired-bindings/<time_ns>-<8 hex>/` with `deleted.json` (`{"schema": 1,
    "codespace", "deleted_at"}`), then removes `scratch.json` and `scratch-session.json` and rewrites `workspace.json`
    keeping only `schema`, `project_id`, `identity`, `local_root`, `local_active_root`, `active_host`. Returns the
    archive. A rerun after a done retire (no `scratch.json`, an archive with `deleted.json` naming it) returns that
    archive.
  - `scratch.terminate_retired(state: Path, receipt: dict) -> str`: refuses when `receipt` names the live session
    (`_saved_session(state)`), `that sync session is in use`. Gets the pinned binary with `allow_download=False`
    (missing: `Mutagen is not installed`), reads `_session_info(..., persist=False)`; absent → `'absent'`; present with
    the exact name and identifier → `_mutagen(binary, state, 'sync', 'terminate', identifier)`, re-list, confirm
    absent → `'terminated'`. `_mutagen` may start the private daemon; when no worker is running afterwards, stop the
    daemon again (`_mutagen(binary, state, 'daemon', 'stop')`).

- [ ] **Step 1: Write the failing tests:**

```python
# tests/test_ssh_hosts.py
def test_remove_keeps_every_other_byte_and_the_mode(self)
def test_remove_is_idempotent_and_replays_after_a_crash_between_steps(self)  # crash after the block, after the include
def test_edited_block_or_include_is_refused_and_nothing_changes(self)
def test_pending_configure_is_refused(self)
# tests/test_herdr.py (fake herdr on PATH, as the existing tests do)
def test_profile_removed_only_when_the_receipt_matches_our_alias_and_session(self)
def test_failed_remove_keeps_the_receipt(self)
# tests/test_retire.py
def test_retire_archives_then_clears_the_pairing_and_keeps_the_local_registry(self):
    # afterwards standing.read(root): setup == 'installed', pairing is None
def test_retire_refuses_another_codespace_or_a_running_worker(self)
def test_retire_rerun_returns_the_same_archive(self)
# tests/test_scratch_sync.py
def test_terminate_retired_ends_only_that_session(self)      # fake _mutagen; the live session is untouched
def test_terminate_refuses_the_live_session(self)
def test_actual_terminate_of_a_paused_retired_session(self)  # gated like the other real-Mutagen tests
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_ssh_hosts tests.test_herdr
  tests.test_retire tests.test_scratch_sync -v`. Expected: exit 1, `AttributeError` for each new function.
- [ ] **Step 3: Implement** the four functions as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add -A devcontainer_setup/project_sync tests && git commit -m "Add owned cleanup
  for a deleted Codespace: SSH alias, Herdr profile, pairing and retired sync session"`

### Task 5: The checked delete

**Files:**
- Create: `devcontainer_setup/project_sync/deletion.py`
- Modify: `devcontainer_setup/project_sync/lifecycle.py:39-47,128-200` (`Reading.repository`, `delete()`),
  `devcontainer_setup/project_sync/__main__.py:25-61,302-312,410-425` (the `delete` subcommand)
- Test: `tests/test_deletion.py` (new), `tests/test_lifecycle.py`, `tests/test_project_sync_cli.py`

**Interfaces:**
- Consumes: Tasks 1-4; `power.stop` (`power.py:88-147`), `arming.account`, `arming.read`, `keeper.stop`,
  `scratch.stop`, `transport.CodespaceConnection`, `standing.read`, `overview.task_command`.
- Produces, `lifecycle.py`: `Reading.repository: str | None = None` (`repository.full_name` of a valid 200 body);
  `LifecycleAPI.delete() -> int` (`DELETE /user/codespaces/NAME`, no body), docstring "five routes".
- Produces, `__main__.py`: subcommand `delete` with `--codespace NAME`, `--check`, `--json`, `--start`. `--json`
  needs `--check`; `--start` cannot go with `--check` or `--json` (argparse errors, exit 2). `run()` dispatches it to
  `deletion.run(...)` before the terminal gate, like `--codespace-stop`.
- Produces, `deletion.py`:
  - `JOURNAL = 'deletions'`; `PHASES = ('confirmed', 'stop-recorded', 'stop-requested', 'shutdown',
    'delete-requested', 'deleted', 'cleaned')`. Journal `STATE/deletions/<name>.json`: `{"schema": 1, "codespace",
    "kind", "account", "repository", "fingerprint", "phase", "at": {phase: epoch}, "cleanup": {step: "done" |
    REASON}}`, written with `state.atomic_write` at every phase. Task 6 reads `phase` and `cleanup`.
  - `STEPS = {'paired': ('keeper', 'sync worker', 'pairing', 'recovery', 'SSH alias', 'Herdr profile',
    'sync session'), 'sealed': ('sync session',), 'moved': ('sync session',)}`.
  - `run(project, state, *, codespace: str | None, check: bool, as_json: bool, start: bool, env=os.environ,
    ask=input, tty=None, api_factory=lifecycle.LifecycleAPI, connect=transport.CodespaceConnection, now=time.time,
    sleep=time.sleep) -> int`.
- The order `run` follows (every message from the Copy section):
  1. Refuse inside a Codespace (`CODESPACES == 'true'`) and with `GH_TOKEN`/`GITHUB_TOKEN` set. Take
     `STATE/delete.lock` non-blocking for the whole run.
  2. `account = arming.account(env)`; `target = verdict.resolve(...)`. A paired, armed target whose `gh_user` differs,
     or a sealed one whose `seal.gh_user` differs, is refused. A journal in `cleaned` prints `was deleted at` and
     exits 0; in `deleted` it goes to step 9.
  3. `api = api_factory(name, gh_user=account)`; `reading = api.read()`. `Deleted` (confirmed): paired → journal
     `deleted`, `already deleted; cleaning up`, step 9; otherwise `NAME is already deleted.`, exit 0. Not `ok` →
     unreachable `GitHub did not answer`. `reading.repository` not equal to the project's (case-insensitive) →
     refused, exit 1.
  4. `--start`: needs a terminal and a sealed or moved target in `Shutdown`; `api.post_start()`, then poll `read()`
     every 5 s up to 10 minutes for `Available`. Paired: refused with the `work:cs:restart` copy, no POST.
  5. `unpaired`, or a state other than `Available` → `verdict.unreachable(...)` with the action line; no connection.
  6. Otherwise, inside one `connect(name, state=state)`: `scan`, `pushed.prove`, the sync readers (paired only),
     `verdict.judge`, `fingerprint`. A `ValueError` entering the connection → unreachable `could not connect to NAME`.
     Print `as_json(...)` (with `--json`) or `lines(...)`.
  7. `--check`: exit 0 when deletable, else 1. Not deletable: the `Nothing was deleted. Resolve…` line, exit 1. No
     terminal (`tty()` false; default `sys.stdin.isatty() and sys.stdout.isatty()`): the no-terminal line, exit 1.
     Then `ask('> ')`; EOF or anything but `answer.strip() == 'delete'`: `Nothing was deleted.`, exit 1.
  8. Recheck, same connection: `arming.account(env)` unchanged, `resolve` gives the same kind and remote root,
     `api.read()` still `Available` in the same repository, a new `scan` with an equal `fingerprint`. Any difference:
     the `changed since the check (WHAT)` line, exit 1. Journal `confirmed`. Stop: paired → `power.stop(state,
     expected=name, api=api)`; sealed or moved → journal `stop-recorded`, then `api.post_stop()` (not 2xx: `could not
     be stopped`, exit 1). Journal `stop-requested`. Poll `read()` every 5 s up to 10 minutes until `state ==
     'Shutdown'`, `pending is not True` and the same repository; then journal `shutdown`, else `has not reached
     Shutdown`, exit 1. Journal `delete-requested`; `status = api.delete()`; a 4xx/5xx other than 404 → `GitHub
     refused`, exit 1; otherwise poll `api.confirm_deleted()` every 5 s up to 2 minutes for `True` → journal
     `deleted`, else `does not show NAME as gone yet`, exit 1.
  9. Cleanup, each step of `STEPS[kind]` not yet `done`, in order: `keeper.stop`, `scratch.stop`,
     `workspace.retire` (its archive's `scratch-session.json` is the receipt for `sync session`), the disarm (only when
     armed; merge `armed=False` as `__main__._arming_edit` does, print the `Recovery is off` line),
     `ssh_hosts.remove`, `herdr.remove_profile`, `scratch.terminate_retired` (sealed and moved: the newest archive's
     receipt naming the target; none → `done`). A `ValueError` records its REASON and the run continues with the next
     step. All done → journal `cleaned`, `Deleted NAME.` and `Cleaned up: …`, exit 0; otherwise the `cleanup is not
     finished` line naming the first pending step, exit 1.

- [ ] **Step 1: Write the failing tests** (`tests/test_deletion.py`: a `FakeAPI` that scripts readings and counts
  `post_stop`, `post_start`, `delete`, `confirm_deleted`; `connect` yields a fake connection; `target_scan.scan` and
  `pushed.prove` patched to scripted results; `ask` and `tty` injected; `power.stop` wrapped to record its call order):

```python
def test_check_json_makes_no_destructive_call(self):   # stdout parses; post_stop, delete, post_start, power.stop == 0
def test_unsafe_never_asks_and_never_deletes(self):     # dirty second worktree; stash: ask not called, 0 calls, exit 1
def test_only_the_exact_word_proceeds(self):  # 'Delete', 'yes', 'delete it', '' -> 0 calls; ' delete ' -> deletes
def test_eof_or_no_terminal_makes_no_destructive_call(self)
def test_a_change_during_the_prompt_voids_the_verdict(self):
    # recheck: new fingerprint; another account; a rebind (kind changes); state Shutdown -> each WHAT line, 0 calls
def test_paired_stop_is_recorded_before_the_post(self):  # power.stop called with expected=name before any DELETE
def test_retired_delete_keeps_new_recovery_working(self):
    # NEW paired and armed; delete sealed OLD: arming.json bytes identical; then drive a Keeper for NEW
    # (tests/test_keeper.py's FakeClient) through a lifetime stop: it classifies LIFETIME and posts start once
def test_404_alone_is_not_deleted(self):       # delete() -> 404, confirm_deleted() None -> journal delete-requested,
                                               # no cleanup, exit 1
def test_lost_delete_reply_is_confirmed_by_inventory(self)   # delete() -> 0, then confirm_deleted() True -> cleanup
def test_each_phase_resumes_safely(self):
    # journal at stop-requested + reading Shutdown -> UNKNOWN with the start action, 0 POSTs;
    # at delete-requested + confirm None -> check from scratch, never a second DELETE on the old verdict;
    # at deleted with 'Herdr profile' pending -> only that step runs, no API call
def test_paired_target_is_never_started(self)          # --start on paired -> refused, post_start 0
def test_start_needs_a_terminal_and_a_retired_target(self)
def test_never_paired_target_is_not_connected(self)    # connect not called; four UNKNOWN 'pair it first'
def test_another_repository_is_refused_before_any_check(self)
def test_inside_a_codespace_or_with_an_env_token_refuses(self)
def test_cleanup_failure_is_pending_and_the_rest_still_runs(self)
def test_paired_cleanup_order_and_disarm_line(self)
# tests/test_lifecycle.py
def test_delete_sends_one_DELETE_to_the_named_route(self)
def test_reading_carries_the_repository(self)
# tests/test_project_sync_cli.py
def test_delete_subcommand_options(self):  # --json without --check, --start with --check -> exit 2;
                                           # work --codespace is unchanged; delete reaches deletion.run headless
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_deletion tests.test_lifecycle
  tests.test_project_sync_cli -v`. Expected: exit 1 (`ModuleNotFoundError` for `deletion`; the lifecycle and CLI
  tests fail on the missing route and subcommand).
- [ ] **Step 3: Implement** `deletion.py`, the lifecycle route and the subcommand as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add -A devcontainer_setup/project_sync tests && git commit -m "Delete a Codespace
  after a clean check and a typed delete, then clean up"`

### Task 6: `work:cs:delete`, `task work` and the docs

**Files:**
- Modify: `templates/continuity-tasks.yml:50-62`, `Taskfile.yml:48-60`, `devcontainer_setup/root_tasks.py:6-9`,
  `devcontainer_setup/project_sync/standing.py` (two facts), `devcontainer_setup/project_sync/overview.py:150-322`,
  `README.md:109-119`, `docs/project-continuity.md:21-35` and a new section after the replacement text (`:144-147`)
- Test: `tests/test_root_tasks.py`, `tests/test_continuity_integration.py`, `tests/test_standing.py`,
  `tests/test_overview.py`

**Interfaces:**
- Consumes: `seal.names` (Task 3); the journal format in Task 5's Interfaces (files only; this task does not import
  `deletion.py` and does not edit `__main__.py`).
- Produces:
  - Template stanza after `work:cs:restart`: `work:cs:delete`, `desc: Check a Codespace, then delete it after you type
    delete (-- --check --json only checks)`, `interactive: true` (as `work`: it reads a typed answer and also runs
    headless), `cmds: [bash .devcontainer/scripts/project-sync.sh delete {{.CLI_ARGS}}]`; the same in `Taskfile.yml`
    for this repository. `root_tasks.ALIASES` gains `work:cs:delete` after `work:cs:restart`; `INTERACTIVE` gains it.
  - `standing.Local` gains `retired: tuple[str, ...] = ()` (`seal.names(state)` minus names whose journal is
    `cleaned`) and `cleanup: tuple[str, ...] = ()` (names whose journal phase is `deleted`). Unreadable files add one
    problem each, as `read` does today.
  - `overview.view`: for each name in `local.cleanup`, a line `Cleanup after deleting NAME is not finished.` and the
    first action `Finish cleaning up after NAME` → `work:cs:delete -- --codespace NAME`. For the oldest name in
    `local.retired` that `remote.spaces` (status `ok`) lists, the action `Check and delete NAME` → `work:cs:delete --
    --codespace NAME`, placed right after the situation's own actions. Never an action deleting `local.pairing`, and
    nothing in a Codespace. The six-line, 78-cell and 3-6 action rules hold.
  - Docs: README's "can be deleted at any time" sentence becomes the safe order (replace, then
    `task work:cs:delete`, which checks first and deletes only on a typed `delete`); `docs/project-continuity.md` gets
    the command in its table and a short section: what is checked, what is not, `--check --json` for agents, a
    stopped Codespace reads Unknown until started, what cleanup removes and what it keeps.

- [ ] **Step 1: Write the failing tests:**

```python
# tests/test_root_tasks.py
def test_work_cs_delete_renders_description_interactive_and_root_alias(self)
# tests/test_continuity_integration.py: the real task inventory test (:56) lists work:cs:delete
# tests/test_standing.py
def test_retired_and_cleanup_facts(self)     # a seal; a cleaned journal hides it; a deleted journal -> cleanup
# tests/test_overview.py
def test_cleanup_pending_comes_first(self)
def test_listed_sealed_retired_codespace_offers_check_and_delete(self)
def test_unlisted_or_unsealed_retired_offers_nothing(self)
def test_the_current_pairing_is_never_offered_for_delete(self)
# the existing test_frame_is_at_most_six_lines_of_78_cells_and_three_to_six_actions gains these fixtures
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_root_tasks tests.test_standing
  tests.test_overview tests.test_continuity_integration -v`. Expected: exit 1 on the new tests.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add -A templates Taskfile.yml devcontainer_setup README.md docs tests && git commit -m
  "Add task work:cs:delete, offer it for a replaced Codespace, and document the safe order"`

## Left for stage D (its plan inherits these)

- Step 3 seals OLD: after a final cycle that completes with nothing pending and no capture issues, `target_scan.scan`
  then `seal.write(...)` with that cycle's inbound snapshot id and the replace operation id.
- `Store.collect` keeps every manifest a seal names (and its blobs) (INV §5, `checkpoint_store.py:287-323`).
- A NEW that the replace journal names may be connected with its host key accepted (`accept_new=True`), so its
  check does not read "pair it first" (CHECK Q9).
- Step 7 offers `deletion.run(..., codespace=OLD)`; a delete of either name closes the replace journal (CHECK Q11).

## Order and parallelism

At most three builders at once (shared Mac, memory `shared-mac-load-flakes`). Wave 1: Tasks 1, 2 and 4, from the
final plan commit. Wave 2: Task 3, from the merge of 1 and 2 (it needs their types; 4 may merge meanwhile). Wave 3:
Tasks 5 and 6 in parallel, from the merge of 3 and 4. They share no file: Task 5 owns `__main__.py`, `lifecycle.py`
and `deletion.py`; Task 6 owns the template, `Taskfile.yml`, `root_tasks.py`, `standing.py`, `overview.py` and the
docs. The lead merges each into `feature/task-work-cd` after its own re-gate, one gate at a time.

## Live check (part of the C+D release gate, not of these tasks)

With stage D, on a clone of innovate under its own `XDG_STATE_HOME` with the 2026-10-27 backup runbook: after the
replace, `task work:cs:delete -- --codespace OLD --check --json` (exit and JSON), a scratch edit on OLD making it
Not safe, the typed delete of OLD (NEW's `arming.json` and keeper unchanged), then the test's NEW deleted as the
paired target (stop recorded, cleanup removes the clone's SSH alias and Herdr profile and nothing else).
