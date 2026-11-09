# `task work` stage C: check a Codespace, then delete it on a typed word. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `task work:cs:delete [-- --codespace NAME]` checks a Codespace's Git, scratch, conversations and agents
over the read-only connection, prints a per-category verdict naming its scope, and deletes only after the owner types
`delete` in a terminal, a full second check agrees, the stop is recorded and the Codespace is confirmed Shutdown. Then
it cleans up what this Mac kept for it, journaled and retried until done. `-- --check --json` only checks.

**Architecture:** Five runtime modules, vendored into generated projects like every `project_sync/*.py`:
`target_scan.py` runs one bounded, read-only inventory on the Codespace (Git, scratch, conversation files and
unfinished turns, agent processes) and fingerprints it; `pushed.py` proves on the Mac that each commit is on GitHub;
`seal.py` reads and writes the record of a final flush (stage D writes it when it replaces a Codespace); `verdict.py`
turns the evidence into four categories and renders them; `deletion.py` gathers the evidence (for the paired
Codespace, a scan, then a sync cycle that starts after it, then a second scan that must match), runs the typed
confirmation, the journaled stop and DELETE, and the cleanup. Cleanup primitives join `ssh_hosts.py`, `herdr.py`,
`workspace.py` and `scratch.py`; `lifecycle.LifecycleAPI` gains DELETE.

**Tech Stack:** Python 3 standard library (vendored, run as `python3 -I -B`), go-task, `unittest`, Git, Mutagen
(already pinned).

**Spec:** `docs/superpowers/specs/2026-11-02-task-work-design.md`, section "C. Checked delete", its Goals,
Anti-goals, Constraints, Owner answers 1-2, "Tests the plan must include" (C list), calls 2 and 4, and builder-check
calls 12-13. Source inventory at b4a8f6c:
`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-c/inventory/INVENTORY.md` (INV §n). Plan
check this revision answers: `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-c/plancheck/CHECK.md`
(F1-F21). Line numbers are at b4a8f6c.

## Global Constraints

- Gates: `task check` and `task test` (`python3 -m unittest discover -s tests -v`), both exit 0, at your commit, run
  in the default environment (no sandbox, no `TMPDIR` override):
  `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-a/RULING-gates.md`.
- Focused runs: run `python3 -m unittest …` unpiped (or redirect to a file and read `$?`); a `| tail` hides the exit code.
- Branches: cut from `feature/task-work-cd` at the commit your brief names; merge back into it, never `main`. C and D
  ship together after one live test of replace end to end (spec call 1).
- Commit messages: plain imperative sentence case, one concern per commit, no AI attribution of any kind.
- **Nothing changes before the typed word.** With `--check`, without a terminal, on EOF or another word, and on every
  refusal, at every journal phase: zero calls to `post_stop`, `delete`, `post_start`, `power.stop`,
  `arming.record_owner_stop`, any cleanup step or primitive, and no capture, acknowledgement or publication of
  conversations. Tests count these calls. `--start` is its own operation (call 10).
- `ReadOnlyAPI` stays GET-only (`transport.py:114-185`). DELETE exists only on `lifecycle.LifecycleAPI`.
- Remote Python is not a sandbox (INV §2): the scan program only reads. It writes, creates, locks and deletes nothing
  on the Codespace (no module files either), and every Git call it makes has `GIT_OPTIONAL_LOCKS=0`.
- "Deleted" needs `LifecycleAPI.confirm_deleted() is True` (a complete same-account inventory without the name). A
  404, a timeout, or an inventory of 100 or more rows (`_deleted_from_list` returns None) never means deleted (#60).
- Never print or store contents, remote URLs, process command lines or credentials. Shown and JSON values are fixed
  texts plus allowlisted paths, counts, ref names, 12-hex commit ids, agent names and session ids; every untrusted
  string shown on a terminal goes through `overview.fit`. Git, gh and Herdr stderr is discarded, never echoed.
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
   Agents are checked, its Scratch and History read UNKNOWN because no final flush was recorded, so it cannot be
   deleted this way. `unpaired`: none of these; nothing connects and every category reads UNKNOWN, "pair it first"
   (CHECK Q9). The exception for a NEW that a replace journal names belongs to stage D.
2. **The final-flush record is defined here, written by D** (`seal.write` and `seal.read` in Task 3). D seals only
   from `deletion.gather` evidence that is steady, has no unfinished turns and whose bracketing cycle was healthy
   inbound; the seal binds that cycle's id and inbound snapshot (F1).
3. **History for the paired Codespace is proved by bracketing** (F1). A healthy cycle alone does not show that the
   files on the Codespace now are the ones it captured. `deletion.gather` scans, waits for the worker to complete a
   cycle that started after the scan began (the worker cycles continuously, `scratch.py:745-758`), then scans again.
   Conversation files are append-only, so equal fingerprints mean the cycle captured exactly what both scans saw.
   History is then SAFE when that cycle's inbound direction (`codespace_to_desktop`) completed with its receipt bound
   to the Mac's, nothing pending and no capture issues, and the second scan found no unfinished turn after each
   captured cutoff (F3: Codex appends nothing after a completed turn but a new turn, `conversations.py:216-237`;
   Claude may append metadata, so its tail counts only `user` or `assistant` records or a partial line). The owner's
   "Not checked" list stays exactly the three items of ruling 29.
4. **`Kept on this Mac only` is a notice, not an exception** (F9). The outbound direction carries the Mac's own
   conversations to the Codespace; its refusals and forgotten conversations already sit on the Mac, and the inbound
   rules above never read the outbound row. For a sealed OLD, the seal's snapshot must load from the Mac with its
   blobs (`Store.load` verifies them), which is the custody proof for everything OLD held at the seal.
5. **Git discovery walks everything under `/workspaces`** (F6) except `.git` internals and, inside a worktree, the
   directories Git ignores there (ruling 29: files Git ignores are "Not checked"). `.devcontainer-work`, `.scratch`
   and `.cache` outside a worktree are walked.
6. **Git proof** (CHECK Q3). The scan returns each repository's identity from `state._identity` (the raw URL never
   leaves the Codespace; an origin `_identity` refuses makes that repository UNKNOWN, F20). A repository whose
   identity is the project's is proved in the Mac checkout: one fetch of every head into a private namespace that
   no configured refspec, tag pruning or maintenance can widen (F7), then each commit must exist and be contained in
   a fetched head; the namespace is removed afterwards. Another GitHub repository is proved only by equality with an
   advertised head, else UNKNOWN. A repository without `origin` that has commits is UNSAFE.
7. **Agents**: a process whose argv has any element whose basename is `claude`, `codex`, `grok` or `opencode` blocks
   (npm installs run as `node …/bin/claude`). A Herdr server alone is reported in JSON and does not block.
8. **The recheck is the whole check again** (F2). After the typed word, `gather` and `judge` run again: the result
   must be deletable and its fingerprint equal to the first one, with the same account, kind, remote root and
   repository, still Available.
9. **Reruns and cleanup.** A journal records each phase. Cleanup runs only in a terminal without `--check`, and only
   when authorized: the journal reached `confirmed` (the owner typed `delete` for this Codespace), or, with no such
   journal, the owner types `delete` again after a line naming what this Mac still keeps (F5). A Codespace GitHub
   confirms deleted (even after a lost reply or a crash before the journal said so) goes to that cleanup for every
   kind (F10). Any other rerun checks from scratch: a target left Shutdown reads UNKNOWN with its start action.
10. **`--start` is its own operation** (F16): `work:cs:delete -- --codespace NAME --start` starts a sealed or moved
    Codespace in a terminal, waits for Available, prints the check command and exits; it never checks or deletes in
    the same run. The paired Codespace's action is `task work:cs:restart`, which records start evidence for the
    keeper (`_owner_power`); `deletion.py` never POSTs start for it.
11. **A `delete` subcommand** of `project-sync.sh` (`delete [--codespace NAME] [--check] [--json] [--start]`), not a
    `work` flag: `work --codespace` is already a boolean action (INV §9). Exit 0 when the check is deletable (with
    `--check`), when the delete and cleanup finished, or after `--start`; 1 otherwise; 2 for usage errors.
12. **`task work` offers delete only for a sealed retired Codespace GitHub still lists** (the oldest seal) and a repair
    action for one pending cleanup (F21 ordering in Task 6). It never offers deleting the current pairing: that is the
    daily Codespace, and the command remains. Ruling 8's "reached from `task work`" is met by the retired one.
13. **Herdr profiles carry their provenance** (F8). `herdr.open_remote` records `created: true` in
    `herdr-machine.json` when its own `machine add` made the profile, and `false` when it adopted one. Cleanup removes
    only a profile whose receipt says `created: true` and whose id matches the catalog row for this project's alias
    and session; an adopted or older receipt keeps the profile and says so.
14. **Paired cleanup holds `workspace-control.lock`** (F4) and first checks that `scratch.json` still names the deleted
    Codespace. If the project was rebound meanwhile, the project-wide steps (keeper, sync worker, pairing, recovery,
    SSH alias, Herdr profile) belong to the new pairing and are skipped as moved; only OLD's archived sync session is
    ended.
15. **No paginated inventory.** `confirm_deleted` stays single-page; an account with 100 or more Codespaces reads
    "not confirmed" and the rerun says so (YAGNI).

## Copy (exact strings, used by Tasks 3, 5 and 6)

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
Grok and OpenCode conversations.
Type delete to delete it.
```

- Singulars: `1 repository`, `1 worktree`, `1 file`. No repositories: `no repositories under /workspaces`. The
  Scratch time is when the second scan finished.
- Sealed SAFE: Scratch `unchanged since the final flush at HH:MM (N files)`; History `no turn newer than the final
  capture at HH:MM`.
- Git UNSAFE items, joined by `; `: `PATH: N changed files (a, b, c and N more)`, `PATH: N stashes`,
  `PATH: BRANCH at 0123456789ab is not on GitHub`, `PATH: no remote, so its commits exist only there`. `PATH` is the
  repository or worktree path relative to `/workspaces` (`.` for `/workspaces` itself), absolute when outside it; a
  detached head is shown as `HEAD`.
- Git UNKNOWN: `could not read every repository under /workspaces`, or items `PATH: BRANCH: REASON` with REASON one
  of `GitHub could not be read from this Mac`, `this Mac has no checkout of OWNER/REPO to compare`,
  `not a GitHub repository`, and `PATH: its origin cannot be read`.
- Scratch: UNSAFE `N files differ from this Mac's copy (…)`, `N conflicts (…)` (paired), `N files changed or added
  since the final flush (…)` (sealed); UNKNOWN `sync is not running; run CMD, then check again` (CMD = `work:cs`),
  `could not read .scratch on NAME`, `moved with --rebind, so no final flush was recorded`.
- History: UNSAFE `N conversations have not reached this Mac yet`, `N conversations could not be read on NAME`,
  `N conversations have a turn that never finished (agent SESSION_ID, …); resume each and let it finish, then check
  again`, `N conversation files changed since the final capture`; UNKNOWN `sync is not running; run CMD, then check
  again`, `sync has not finished a cycle since the check began; check again in a minute`, `the last sync cycle
  failed; check again in a minute`, `the final capture is no longer on this Mac`, the `moved` text above.
- Agents: UNSAFE `claude (2), codex (1) running: end them, then check again`; UNKNOWN `could not read processes on
  NAME`.
- One reason for all four (UNKNOWN): `NAME is stopped`, `NAME is STATE`, `GitHub did not answer about NAME`,
  `could not connect to NAME`, `this Mac has never connected to NAME; pair it first`, `NAME changed while it was
  being checked; check again when nothing is running on it`. After the verdict, one action line: `To check it, start
  it: CMD` (sealed or moved Shutdown; CMD = `work:cs:delete -- --codespace NAME --start`), `To check it, restart it:
  CMD` (paired Shutdown; CMD = `work:cs:restart`), `Check again once it is Available.`
- Not deletable: `Nothing was deleted. Resolve the lines marked Not safe or Unknown, then check again.`
- No terminal: `Nothing was deleted: deleting needs a terminal. Run CMD in one.` EOF or another word:
  `Nothing was deleted.`
- Changed: `NAME changed since the check (WHAT); nothing was deleted. Run the delete again to check it again.` with
  WHAT one of `its files, repositories, conversations or agents`, `the GitHub account`, `its pairing`, `its state`,
  `the connection to it`, `what GitHub or this Mac holds for it` (a category stopped being SAFE).
- `NAME could not be stopped (REASON); nothing was deleted.` · `NAME has not reached Shutdown after 10 minutes;
  nothing was deleted. Run the delete again to check it again.` · `GitHub refused to delete NAME (HTTP N). It stays
  stopped; nothing else changed.` · `GitHub accepted the delete but does not show NAME as gone yet. Run the delete
  again to finish.`
- Done: `Deleted NAME.` · paired and armed: `Recovery is off: NAME was deleted.` · `Cleaned up: STEP, STEP.` with
  STEP from `keeper`, `sync worker`, `pairing`, `recovery`, `SSH alias`, `Herdr profile`, `sync session` ·
  `Herdr profile kept: setup did not create it.` · `The project moved to NEW meanwhile; its keeper, pairing, SSH alias
  and Herdr profile stay.`
- `Deleted NAME, but cleanup is not finished: STEP (REASON). Run CMD in a terminal to finish it.` (also printed,
  exit 1, by `--check` when a journal says `deleted`) · `NAME was deleted at DATE; nothing is left to clean up.` ·
  `NAME is already deleted.` (nothing kept) · `NAME is already deleted. This Mac still keeps: STEP, STEP.` then
  `Type delete to remove them.`
- `--start`: `Started NAME. Check it: CMD` (CMD = `work:cs:delete -- --codespace NAME`) · `--start needs a terminal.`
  · `To start the paired Codespace, use CMD.` (CMD = `work:cs:restart`) · `NAME did not become Available within 10
  minutes.`
- Refusals: `Run CMD on your Mac; it needs the Mac's key and state.` · `NAME was paired as LOGIN, but gh is signed
  in as LOGIN. Switch with gh auth switch, then run the delete again.` · `NAME belongs to another repository;
  nothing was checked.` · `Another delete for this project is running; wait for it to finish.` · `No Codespace is
  paired. Name one: CMD` (CMD = `work:cs:delete -- --codespace NAME`) · `Unset GH_TOKEN before deleting a Codespace;
  it must use a real gh account.` (and `GITHUB_TOKEN`).

## Review Focus

1. **Work in a place nobody looks**: a second worktree, a repository under `/workspaces/.cache`, an old
   `.devcontainer-work` checkout or one inside `.scratch` holds the only copy of a change; it is found and blocks
   (Task 1 `test_every_repository_and_worktree_under_workspaces_is_found`, Task 3
   `test_dirty_second_worktree_or_stash_is_unsafe`).
2. **A turn finished, or left unfinished, a minute before the check**: not SAFE until a cycle that started after the
   scan has carried it to the Mac; an unfinished turn blocks (Task 5
   `test_a_turn_written_just_before_the_check_needs_a_later_cycle`, Task 1 `test_unfinished_turns_after_the_cutoff`).
3. **The world changes during the prompt**: a scratch edit, an agent starting, a remote branch deleted, the Mac's
   capture lost; the full recheck voids the verdict and nothing stops (Task 5
   `test_a_change_during_the_prompt_voids_the_verdict`).
4. **Deleting a retired OLD while NEW is armed**: NEW's `arming.json` is byte-identical and NEW's next lifetime stop
   is still restarted by its keeper (Task 5 `test_retired_delete_keeps_new_recovery_working`).
5. **A crash, a lost reply or a rebind between phases**: a rerun never deletes on a stale verdict, never repeats a
   done cleanup step, never calls a 404 deleted, never cleans up the new pairing, and `--check` never cleans
   (Task 5 `test_each_phase_resumes_safely`, `test_rebind_during_cleanup_leaves_the_new_pairing_alone`).

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
  - `Repository`: `path: str`, `identity: str | None`, `branches: tuple[tuple[str, str], ...]` (short name → 40-hex,
    sorted), `stashes: int`, `worktrees: tuple[Worktree, ...]`, `bare: bool`, `ok: bool`.
  - `Scan`: `codespace: str`, `at: float`, `repositories: tuple[Repository, ...]`, `git_complete: bool`,
    `scratch: tuple[tuple[str, str], ...]` (relative path → sha256), `scratch_complete: bool`,
    `sessions: tuple[tuple[str, str], ...]` (`codex/<path under CODEX_HOME/sessions>` or
    `claude/<path under the Claude home's projects>` → sha256), `sessions_complete: bool`,
    `tails: tuple[tuple[str, str], ...]` ((agent, file basename) with an unfinished turn; empty without `cutoffs`),
    `agents: tuple[tuple[str, int], ...]` (name → count, for `claude codex grok opencode herdr`),
    `agents_complete: bool`, `problems: tuple[str, ...]` (fixed texts).
  - `scan(connection, codespace: str, remote_root: str, *, cutoffs: Mapping[tuple[str, str], int] | None = None,
    limits: Limits = LIMITS, workspaces: str = '/workspaces', proc: str = '/proc', now=time.time) -> Scan`. One
    `connection.python(code, payload=…, timeout=limits.seconds + 30)`. `cutoffs` maps (agent, file basename) to the
    captured byte length. Never raises for a failed or malformed answer: a `ValueError` from the connection, output
    above `limits.output`, or JSON that fails strict validation (types, 40-hex ids, no control characters in paths)
    gives every part `complete=False` and one problem.
  - `fingerprint(scan: Scan) -> str | None`: sha256 hex of `json.dumps(…, sort_keys=True, separators=(',', ':'))` of
    `repositories`, `scratch`, `sessions` and `agents`; None unless all four `*_complete` are true.
- Bootstrap (F17): the payload carries the sources of `state.py`, `scratch_tree.py` and `sessions.py`. The program
  builds an in-memory package (`types.ModuleType('_psync')` with `__path__ = []`, registered in `sys.modules`), then
  creates `_psync.state`, `_psync.scratch_tree`, `_psync.sessions` in that order, each with `__package__ = '_psync'`,
  registered before `exec`, so `sessions.py`'s `from .state import …` resolves. No file is written.
- Rules the program follows on the Codespace:
  - Git walk: `os.scandir` from `workspaces`, never following symlinks. A directory with a `.git` entry (directory or
    file), or a bare repository (`HEAD` file plus `objects/` and `refs/` directories), is a candidate; never descend
    into a `.git` directory or a bare repository's internals. When the walk enters a worktree root, it runs
    `git ls-files -z --others --ignored --exclude-standard --directory` there and does not descend into the
    directories listed (files Git ignores are "Not checked"). Nothing else is pruned.
  - Group candidates by `git rev-parse --path-format=absolute --git-common-dir`. Per repository: `for-each-ref
    --format='%(refname:short) %(objectname)' refs/heads`, stashes by `rev-list --walk-reflogs --count refs/stash`
    (absent ref = 0), `config --get remote.origin.url` passed only to `state._identity` (absent → None; a
    `ValueError` → `ok=False`, problem `its origin cannot be read`), `worktree list --porcelain -z`. Per worktree:
    `status --porcelain=v1 -z --untracked-files=all`, `symbolic-ref -q --short HEAD`, `rev-parse -q --verify HEAD`.
    Every Git call: `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, `GIT_NO_REPLACE_OBJECTS=1`, `LC_ALL=C`, timeout
    15 s; a failure marks only that repository `ok=False`.
  - Scratch: `scratch_tree.entries(<remote_root>/.scratch)`; hash each regular file. Missing `.scratch` = empty map,
    complete. An error from `entries` (symlink, special file, case collision) = incomplete.
  - Conversations: walk `sessions.native_home('codex')/'sessions'` and `sessions.native_home('claude')/'projects'`
    without following links; hash regular files. Missing directory = empty, complete. For a file whose (agent,
    basename) is in `cutoffs`: read the bytes after the cutoff; Codex: any byte is an unfinished turn; Claude: a
    complete line whose JSON `type` is `user` or `assistant`, or a final line without a newline, is one. A file
    shorter than its cutoff is reported as a problem and makes `sessions_complete` false.
  - Agents: for each numeric entry of `proc`, read `cmdline`, split on NUL, take the basename of every element; count
    each of the five names once per process. Skip this process and its ancestors (the `stat` ppid chain). A process
    gone mid-read (`FileNotFoundError`, `ProcessLookupError`) is skipped; any other read error, or an unreadable
    `proc`, makes `agents_complete` false. Only names and counts leave the Codespace.
  - Any limit reached makes that part incomplete. The program writes nothing anywhere.

- [ ] **Step 1: Write the failing tests** in `tests/test_target_scan.py`. A `LocalConnection` fake runs
  `[sys.executable, '-I', '-B', '-c', code]` with the payload on stdin, `HOME` and `CODEX_HOME` in a temp dir, and
  returns stdout (as `tests/test_replication.py:19-38` does). Repositories are real temp Git repos under a temp
  `workspaces`; `proc` is a temp tree of `<pid>/cmdline` and `<pid>/stat` files.

```python
def test_every_repository_and_worktree_under_workspaces_is_found(self):
    # main checkout clean; linked worktree with an untracked file; nested repo; bare repo; .devcontainer-work/old clone;
    # a repo inside .scratch; a dirty repo at .cache/manual-project (no parent repo) -> all found
    # a repo inside a node_modules the main checkout's .gitignore ignores -> not found
def test_stash_and_detached_head(self):        # stashes == 1; detached worktree: branch None, head == oid
def test_origin_with_credentials_is_unreadable_and_never_leaves(self):
    # origin https://user:secret@github.com/o/r.git -> ok False, problem names the path; b'secret' not in raw output;
    # a credential-free https and an ssh origin -> identity equals state._identity(url)
def test_scratch_map_follows_the_sync_policy(self):  # root README.md and node_modules/x excluded; notes/a.md hashed
def test_scratch_symlink_makes_only_scratch_incomplete(self)
def test_conversation_files_are_hashed_for_codex_and_claude(self)
def test_unfinished_turns_after_the_cutoff(self):
    # codex: bytes after the cutoff -> tail; claude: a summary record after end_turn -> no tail;
    # claude: a user record after it -> tail; a partial last line -> tail; no cutoffs -> tails == ()
def test_agents_match_any_argv_basename(self):
    # 'node /usr/lib/bin/claude', '/usr/bin/codex exec', 'herdr server' -> claude 1, codex 1, herdr 1
def test_the_scan_itself_is_not_an_agent(self)
def test_unreadable_process_is_incomplete_but_a_vanished_one_is_skipped(self)
def test_a_limit_makes_its_part_incomplete(self):    # Limits(entries=5) -> git_complete False, others True
def test_failed_connection_is_incomplete_not_empty(self)
def test_fingerprint(self):  # equal for equal scans; differs after an edit, a new branch, a stash, a new agent;
                             # None when any part is incomplete; unaffected by tails
def test_scan_changes_nothing(self):  # every file and ref under workspaces and HOME hashed before and after; no new file
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
- Produces (independent of Task 1, F18; Task 3 converts scan repositories to claims):
  - `Claim` (frozen): `path: str`, `identity: str | None`, `refs: tuple[tuple[str, str], ...]` (ref → 40-hex: every
    branch, plus `HEAD` for each detached worktree, deduplicated).
  - `Proof` (frozen): `path: str`, `pushed: int`, `unpushed: tuple[tuple[str, str], ...]` (ref, 12-hex),
    `unknown: tuple[tuple[str, str], ...]` (ref, REASON from the Copy section), `no_remote: bool`.
  - `prove(project, claims: Sequence[Claim], *, timeout: float = 60) -> tuple[Proof, ...]`, one Proof per claim, in
    order. `CHECK_NAMESPACE = 'refs/project-sync-check/heads/'`.
- Rules:
  - Common environment for every Git call: `GIT_TERMINAL_PROMPT=0`, `GIT_NO_REPLACE_OBJECTS=1`, `LC_ALL=C`, stderr
    discarded, `timeout`.
  - `identity == project.identity`: at most one fetch per call, in `project.root`, keeping the owner's credential
    configuration (a private repository needs it): `git -c credential.interactive=never -c fetch.pruneTags=false
    -c remote.origin.pruneTags=false -c fetch.writeCommitGraph=false -c maintenance.auto=false -c gc.auto=0 fetch
    --quiet --prune --no-tags --refmap= --no-write-fetch-head --no-recurse-submodules origin
    '+refs/heads/*:refs/project-sync-check/heads/*'`. The empty `--refmap=` stops `remote.origin.fetch` from also
    updating `refs/remotes/*` (F7). The fetch adds objects and may write reflogs for the private refs; it moves no
    owner ref, tag, `HEAD`, index or `FETCH_HEAD`. A failed fetch makes every ref of those claims unknown
    (`GitHub could not be read from this Mac`). Per ref: `git cat-file -e <oid>^{commit}` exit 1 → unpushed; other
    nonzero → unknown; success and `git for-each-ref --count=1 --contains <oid> refs/project-sync-check/heads/`
    printing a line → pushed; printing nothing → unpushed; failing → unknown. In `finally`, every ref under the
    namespace is deleted (`update-ref --stdin`).
  - Another identity starting `github.com/`: `git ls-remote --heads <_github_url(identity)>` (cwd `/`); a ref whose
    id equals an advertised head is pushed, otherwise unknown (`this Mac has no checkout of OWNER/REPO to compare`); a
    failed ls-remote is unknown (`GitHub could not be read from this Mac`). Any other identity: unknown (`not a GitHub
    repository`). `_github_url(identity) -> str` builds `https://github.com/OWNER/REPO.git` (tests patch it).
  - `identity is None` with any ref: `no_remote=True`. A claim without refs proves nothing and is fine. No message
    carries a URL.

- [ ] **Step 1: Write the failing tests** in `tests/test_pushed.py` (a local bare `origin`, the Mac clone as
  `project.root` with a normal `remote.origin.fetch` refspec, and a third clone standing in for the Codespace, whose
  ids feed hand-built `Claim` values):

```python
def test_branch_at_a_remote_head_is_pushed(self)
def test_ancestor_of_a_remote_head_is_pushed(self):      # Codespace main two commits behind origin/main
def test_commit_the_mac_cannot_find_is_unpushed(self):   # id only in the third clone -> unpushed (ref, id[:12])
def test_branch_deleted_on_the_remote_is_unpushed_on_a_later_check(self):
    # prove() -> pushed; delete the branch on origin; prove() again -> unpushed
def test_owner_refs_tags_and_fetch_head_are_untouched(self):
    # with remote.origin.fetch set, fetch.pruneTags true and a stale local tag: refs/remotes/*, refs/tags/*, branches,
    # HEAD, index and FETCH_HEAD byte-identical afterwards; nothing left under refs/project-sync-check/
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
- Modify: `devcontainer_setup/project_sync/workspace.py:350-365` (extract the cycle reader below; `status()` keeps
  its output)
- Test: `tests/test_seal.py`, `tests/test_verdict.py` (new); `tests/test_workspace_recovery.py` and
  `tests/test_replication.py` stay green unchanged

**Interfaces:**
- Consumes: `target_scan.Scan`, `target_scan.fingerprint` (Task 1), `pushed.Claim`, `pushed.Proof` (Task 2),
  `standing.refusals` (`standing.py:301-338`), `forget.forgotten`, `sync_cycle.summary`, `sync_cycle.receipt_matches`,
  `checkpoint_store.Store`, `overview.fit`, `overview.task_command`.
- Produces, `workspace.py`: `current_cycle(project, state) -> tuple[dict, dict | None, bool]`: the worker's
  `conversations` cycle, the Mac's inbound receipt (`_conversation_receipt`) and the `current` flag, computed exactly
  as `status()` computes them today (`workspace.py:350-365`, worker, sync phase, project, identity and root scoping);
  `status()` calls it. No network, no lock.
- Produces, `seal.py`:
  - `Seal` (frozen): `codespace`, `identity`, `repository`, `gh_user`, `remote_root`, `operation: str`,
    `sealed_at: float`, `cycle_id: str`, `snapshot_id: str` (64-hex), `scratch: tuple[tuple[str, str], ...]`,
    `sessions: tuple[tuple[str, str], ...]`.
  - `write(state, scan, *, identity, repository, gh_user, remote_root, operation, cycle_id, snapshot_id, now) -> Path`:
    refuses (`ValueError`) a scan whose `fingerprint` is None or whose `tails` is not empty, or an invalid snapshot id;
    writes `STATE/retired/<codespace>.json` (`{"schema": 1, …}`) with `state.atomic_write`, replacing an older seal for
    the same name.
  - `read(state, codespace) -> Seal | None`: None when absent; `ValueError('The final-flush record for NAME cannot be
    read')` when malformed or naming another identity.
  - `names(state) -> tuple[str, ...]`: Codespaces with a valid seal, sorted by `sealed_at`.
- Produces, `verdict.py`:
  - `SAFE, UNSAFE, UNKNOWN = 'safe', 'unsafe', 'unknown'`; `CATEGORIES = ('Git', 'Scratch', 'History', 'Agents')`;
    `NOT_CHECKED = ('files Git ignores', 'home-folder data outside sync', 'Grok and OpenCode conversations')`.
  - `Target` (frozen): `name`, `kind` (`paired`, `sealed`, `moved`, `unpaired`), `remote_root: str | None`,
    `seal: Seal | None`, `archive: Path | None` (the newest `retired-bindings/*` whose `scratch.json` names it).
  - `Evidence` (frozen), built by Task 5's `gather`: `scan: Scan` (the second scan, with cutoffs), `steady: bool`
    (both scans have the same non-None fingerprint; True for sealed and moved, which are scanned once),
    `proofs: tuple[Proof, ...]`, `mac_scratch: tuple[tuple[str, str], ...] | None` (paired: the Mac's eligible
    `.scratch` files → sha256), `conflicts: tuple[str, ...]` (paired: `inspect_sync` conflict paths),
    `sync_running: bool`, `cycle: dict | None` (paired: the bracketing cycle, or None when none completed in time),
    `receipt: dict | None`, `sessions_by_file: tuple[tuple[str, str, str], ...]` ((agent, basename, session id) from the
    cycle's inbound manifest, to name tails), `capture_loads: bool | None` (sealed: `store.load(seal.snapshot_id)`
    succeeded), `kept: tuple[tuple[str, str], ...]`.
  - `Category` (frozen): `name`, `verdict`, `text` (a Copy line without the label), `items: tuple[str, ...]`.
  - `Verdict` (frozen): `target`, `checked_at`, `account`, `repository`, `categories: tuple[Category, ...]`,
    `kept: tuple[tuple[str, str], ...]`, `fingerprint: str | None`, `action: str | None`; property `deletable` = every
    category SAFE and `fingerprint` not None.
  - `resolve(project, state, name: str | None) -> Target`; `ValueError` with the `No Codespace is paired` copy when
    `name` is None and nothing is paired. Order: paired, then sealed, then moved, else unpaired.
  - `claims(scan) -> tuple[Claim, ...]` (one per repository; refs as `Claim.refs` defines).
  - `judge(target, evidence, *, now) -> tuple[Category, ...]` and `unreachable(target, reason) -> tuple[Category, ...]`
    (four UNKNOWN categories with one reason).
  - `kept_conversations(state, project, binding) -> tuple[tuple[str, str], ...]`: the paired Codespace's outbound
    refusals plus forgotten conversations, unique and sorted (call 4: a notice).
  - `lines(verdict, local) -> list[str]` (the Copy section's human verdict, untrusted strings through `overview.fit`,
    each line at most 78 cells) and `as_json(verdict) -> dict` with exactly: `schema` (1), `codespace`, `target`
    (kind), `checked_at` (UTC ISO 8601), `account`, `repository`, `categories` (list of `{name, verdict, text, items}`,
    names lower-case), `kept_on_this_mac_only` (list of `{agent, session_id}`), `not_checked` (`NOT_CHECKED`),
    `deletable`, `fingerprint`.
- Rules `judge` follows:
  - Not `steady` → `unreachable(target, 'NAME changed while it was being checked; …')`.
  - Git: UNKNOWN when `scan.git_complete` is false or any repository is not `ok`; UNSAFE when any worktree has
    changes, any repository has stashes, any proof has `unpushed` or `no_remote`; else UNKNOWN when any proof has
    `unknown`; else SAFE. UNSAFE outranks UNKNOWN (its items first, then the unknown ones).
  - Scratch: `scratch_complete` false → UNKNOWN `could not read .scratch`. Paired: not `sync_running` → UNKNOWN; any
    conflict → UNSAFE; every `(path, sha)` in `scan.scratch` is in `mac_scratch` → SAFE (files only on the Mac are
    fine); else UNSAFE `differ from this Mac's copy`. Sealed: every `(path, sha)` in `scan.scratch` is in
    `seal.scratch` → SAFE (deleted files are fine); else UNSAFE listing new or changed paths. Moved: UNKNOWN.
  - History, paired: not `sync_running` → UNKNOWN; `cycle` None → UNKNOWN `has not finished a cycle since the check
    began`; the cycle's `outcome == 'failed'` → UNKNOWN `the last sync cycle failed`; its `codespace_to_desktop` row:
    pending above 0 → UNSAFE `have not reached`; capture issues → UNSAFE `could not be read`; receipt not bound to the
    row's snapshot or not equal to `evidence.receipt`, or outcome not `complete` → UNKNOWN `has not finished a cycle`;
    any tail → UNSAFE `a turn that never finished`, naming `agent SESSION_ID` from `sessions_by_file`; else SAFE. The
    `desktop_to_codespace` row is never read here. Sealed: `sessions_complete`, every `(path, sha)` in `scan.sessions`
    in `seal.sessions` and `capture_loads` → SAFE; a changed or new file → UNSAFE; `capture_loads` false → UNKNOWN `no
    longer on this Mac`. Moved: UNKNOWN.
  - Agents: `agents_complete` false → UNKNOWN; any of `claude codex grok opencode` above 0 → UNSAFE; else SAFE.

- [ ] **Step 1: Write the failing tests** (`tests/test_seal.py`, `tests/test_verdict.py`; scans, proofs and cycles
  built by hand, a temp state dir, a temp checkpoint `Store` with one saved manifest):

```python
# test_seal.py
def test_write_then_read_round_trips(self)
def test_incomplete_scan_tails_or_bad_snapshot_is_refused(self)
def test_malformed_or_foreign_seal_is_an_error_not_absent(self)
# test_verdict.py
def test_resolve_kinds(self):       # paired; sealed; moved picks the newest of two archives naming it; unpaired
def test_dirty_second_worktree_or_stash_is_unsafe(self)
def test_unpushed_commit_is_unsafe_and_unknown_proof_is_unknown(self)
def test_unsteady_evidence_is_unknown_everywhere(self)
def test_paired_scratch_needs_the_macs_copy(self):   # equal -> SAFE; a differing file -> UNSAFE; conflict -> UNSAFE;
                                                     # worker stopped -> UNKNOWN
def test_paired_history_reads_only_the_inbound_row(self):
    # inbound complete + outbound degraded by a refusal -> SAFE, and the refusal is on the Kept line;
    # inbound pending 1 -> UNSAFE; capture issue -> UNSAFE; no cycle -> UNKNOWN; failed cycle -> UNKNOWN
def test_unfinished_turn_is_unsafe_and_named(self)
def test_sealed_late_scratch_or_newer_turn_is_unsafe(self):   # a new scratch file; a changed codex session file
def test_sealed_lost_capture_is_unknown(self)                  # capture_loads False
def test_moved_target_cannot_be_deleted(self):                 # Git+Agents SAFE, Scratch+History UNKNOWN
def test_failed_process_read_is_unknown(self)
def test_verdict_lines_match_the_copy(self):   # the Copy section's example frame, byte for byte
def test_untrusted_names_never_reach_the_terminal_raw(self):   # escape sequences in a path, branch and session id
def test_json_has_exactly_the_documented_fields_and_no_urls(self)
def test_current_cycle_matches_status(self):   # same fixtures as test_replication's status tests: equal results
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_seal tests.test_verdict -v`. Expected:
  exit 1, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `seal.py`, `verdict.py` and `workspace.current_cycle` as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add -A devcontainer_setup/project_sync tests && git commit -m "Judge a Codespace
  safe to delete per category, against its final flush when it was replaced"`

### Task 4: Cleanup primitives, each owned and replayable

**Files:**
- Modify: `devcontainer_setup/project_sync/ssh_hosts.py` (add `remove`), `devcontainer_setup/project_sync/herdr.py`
  (provenance in `open_remote`, add `remove_profile`), `devcontainer_setup/project_sync/workspace.py` (add `retire`),
  `devcontainer_setup/project_sync/scratch.py` (add `terminate_retired`)
- Test: `tests/test_ssh_hosts.py`, `tests/test_herdr.py`, `tests/test_scratch_sync.py`, `tests/test_retire.py` (new)

**Interfaces:**
- Produces (each idempotent, each returns a result word, each raises `ValueError` with a fixed REASON and changes
  nothing more when ownership cannot be shown):
  - `ssh_hosts.remove(state: Path) -> str` (`'removed'` | `'absent'`), entirely under the global
    `~/.ssh/.devcontainer-setup.lock` that `_configure` takes (`ssh_hosts.py:224-271`), with its safe path, mode and
    symlink checks (F14). Inside the lock: refuse while `herdr-ssh.pending.json` exists (`SSH setup did not finish`);
    check every artifact first (the block equal to `_block(alias, include)` and present once; the include's sha256
    equal to the receipt's), then remove the block from `~/.ssh/config` keeping every other byte and the mode
    (atomic replace), then the include, then the receipt. A replay finds the block gone and finishes the include and
    receipt. A differing block or include: `the SSH alias was edited; remove it by hand`, with zero writes.
  - `herdr.open_remote` (`herdr.py:182-242`) adds `created` to the receipt: `True` when this call ran `machine add`
    for the saved id, `False` when it adopted an existing match, and the previous value when the id is unchanged.
  - `herdr.remove_profile(project, state: Path) -> str` (`'removed'` | `'kept'` | `'absent'`): no receipt → `'absent'`.
    Herdr not installed or its catalog unreadable → `ValueError('Herdr could not be read')`, receipt kept. The catalog
    row for this project's alias and session (`_matching_machines`, compare each row's `id`) with the receipt's
    `machine_id` and `created is True` → `herdr machine remove <id>`, re-read and confirm it is gone (else
    `ValueError('Herdr did not remove the profile')`), then drop the receipt → `'removed'`. `created` missing or false
    → receipt dropped, profile kept → `'kept'`. No such row → receipt dropped → `'absent'`.
  - `workspace.retire(project, state: Path, codespace: str, *, held: bool = False) -> Path` under
    `workspace-control.lock` (taken by `retire` itself unless `held=True`, when the caller already holds it: the lock
    is not reentrant, `state.py:281-302`), with an intent
    file `STATE/retire-intent.json` (`{"schema": 1, "codespace", "archive", "binding_sha256", "registry_sha256",
    "session_sha256"}`) (F12). Refuses while `scratch.status(state)['running']` or `keeper.status(state)` shows a live
    keeper (`stop the keeper and sync worker first`), and refuses to start unless `scratch.json` names `codespace`.
    Steps: write the intent; copy `scratch.json`, `scratch-session.json` (if any) and `workspace.json` into the new
    `retired-bindings/<time_ns>-<8 hex>/` with `deleted.json` (`{"schema": 1, "codespace", "deleted_at"}`); unlink
    each live file only while its sha256 equals the intent's; rewrite `workspace.json` only while it still names
    `codespace`, keeping every key except `codespace`, `remote_root`, `remote_active_root` and
    `verified_remote_historical_roots`, with `verified_historical_roots` set to `verified_local_historical_roots`,
    `active_host='local'` and `local_active_root` the checkout (F13); remove the intent. A replay with the intent
    present finishes the remaining steps under the same equality checks, so a newer pairing's files are never touched.
    Returns the archive.
  - `scratch.terminate_retired(state: Path, receipt: dict) -> str` (`'terminated'` | `'absent'`), under
    `scratch-control.lock`. Refuses when `receipt` names the live session (`that sync session is in use`). Gets the
    pinned binary with `allow_download=False` (missing: `Mutagen is not installed`). Uses a new
    `_retired_session(binary, state, receipt) -> dict | None` (F11): a full private inventory that returns the session
    with the receipt's exact name and identifier, None when a valid inventory proves it absent, and raises on a
    malformed catalog, a duplicate name or a changed identifier. `_session_info` keeps its stricter baseline check.
    Present → `_mutagen(binary, state, 'sync', 'terminate', identifier)`, then `_retired_session` must return None.
    When no worker is running afterwards, `_mutagen(binary, state, 'daemon', 'stop')`.

- [ ] **Step 1: Write the failing tests:**

```python
# tests/test_ssh_hosts.py
def test_remove_keeps_every_other_byte_and_the_mode(self)
def test_remove_replays_after_a_crash_between_steps(self)   # crash after the block; after the include
def test_edited_block_or_include_is_refused_with_zero_writes(self)
def test_remove_waits_for_a_concurrent_configure(self)       # another thread holds the global lock first
def test_remove_keeps_another_projects_block(self)
# tests/test_herdr.py (fake herdr on PATH, as the existing tests do)
def test_receipt_records_whether_setup_created_the_profile(self)   # add -> True; adoption -> False
def test_only_a_created_profile_is_removed(self)
def test_unreadable_herdr_keeps_the_receipt(self)
# tests/test_retire.py
def test_retire_archives_then_clears_the_pairing_and_keeps_local_history(self):
    # afterwards: registry() valid, active_host 'local', verified_local_historical_roots kept;
    # standing.read(root): setup 'installed', pairing None
def test_retire_refuses_another_codespace_or_a_running_worker(self)
def test_retire_replays_after_each_step(self)        # crash after the intent, each copy, each unlink, the registry
def test_replay_never_touches_a_newer_pairing(self)  # crash, then a new scratch.json for another Codespace, replay
# tests/test_scratch_sync.py
def test_terminate_retired_ends_only_that_session(self)      # fake _mutagen; the live session is untouched
def test_terminate_retired_is_idempotent(self)               # a replay after success returns 'absent'
def test_terminate_refuses_the_live_session(self)
def test_actual_terminate_of_a_paused_retired_session(self)  # gated like the other real-Mutagen tests
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_ssh_hosts tests.test_herdr
  tests.test_retire tests.test_scratch_sync -v`. Expected: exit 1, `AttributeError` for each new function.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add -A devcontainer_setup/project_sync tests && git commit -m "Add owned, replayable
  cleanup for a deleted Codespace: SSH alias, Herdr profile, pairing and retired sync session"`

### Task 5: The checked delete

**Files:**
- Create: `devcontainer_setup/project_sync/deletion.py`
- Modify: `devcontainer_setup/project_sync/lifecycle.py:39-47,128-200` (`Reading.repository`, `delete()`),
  `devcontainer_setup/project_sync/__main__.py:25-61,302-312,410-425` (the `delete` subcommand)
- Test: `tests/test_deletion.py` (new), `tests/test_lifecycle.py`, `tests/test_project_sync_cli.py`

**Interfaces:**
- Consumes: Tasks 1-4; `power.stop` (`power.py:88-147`), `arming.account`, `arming.read`, `keeper.stop`,
  `scratch.stop`, `scratch.inspect_sync`, `scratch_tree.entries`, `transport.CodespaceConnection`, `standing.read`,
  `overview.task_command`, `checkpoint_store.Store`.
- Produces, `lifecycle.py`: `Reading.repository: str | None = None` (`repository.full_name` of a valid 200 body);
  `LifecycleAPI.delete() -> int` (`DELETE /user/codespaces/NAME`, no body), docstring "five routes".
- Produces, `__main__.py`: subcommand `delete` with `--codespace NAME`, `--check`, `--json`, `--start`. `--json`
  needs `--check`; `--start` needs `--codespace` and cannot go with `--check` or `--json` (argparse errors, exit 2).
  `run()` dispatches it to `deletion.run(...)` before the terminal gate, like `--codespace-stop`.
- Produces, `deletion.py`:
  - `PHASES = ('confirmed', 'stop-recorded', 'stop-requested', 'shutdown', 'delete-requested', 'deleted',
    'cleaned')`. Journal `STATE/deletions/<name>.json`: `{"schema": 1, "codespace", "kind", "account", "repository",
    "fingerprint", "phase", "at": {phase: epoch}, "archive": path | null, "cleanup": {step: "done" | "kept" |
    "moved" | REASON}}`, written with `state.atomic_write` at every phase. Task 6 reads `phase` and `cleanup`.
  - `STEPS = {'paired': ('keeper', 'sync worker', 'pairing', 'recovery', 'SSH alias', 'Herdr profile',
    'sync session'), 'sealed': ('sync session',), 'moved': ('sync session',)}`.
  - `gather(project, state, target, connection, *, now=time.time, sleep=time.sleep) -> verdict.Evidence`. Sealed and
    moved: one `scan` with no cutoffs, `steady=True`; sealed also `capture_loads`. Paired: `t0 = now()`; scan one; poll
    `workspace.current_cycle` every 2 s, up to 6 minutes, for a `current` cycle with `started_at > t0` and
    `completed_at` set; read its inbound manifest through the Mac `Store` for `cutoffs` and `sessions_by_file` (the
    entry files whose `session_id` is the entry's own); `inspect_sync`; hash the Mac's `.scratch` with
    `scratch_tree.entries`; scan two with `cutoffs`; `steady` = equal non-None fingerprints; proofs from scan two.
    Stage D's seal step calls `gather` for the paired OLD (call 2).
  - `run(project, state, *, codespace: str | None, check: bool, as_json: bool, start: bool, env=os.environ,
    ask=input, tty=None, api_factory=lifecycle.LifecycleAPI, connect=transport.CodespaceConnection, now=time.time,
    sleep=time.sleep) -> int`.
- The order `run` follows (every message from the Copy section; `tty()` defaults to `sys.stdin.isatty() and
  sys.stdout.isatty()`; "authorized" means not `check` and `tty()`):
  1. Refuse inside a Codespace (`CODESPACES == 'true'`) and with `GH_TOKEN`/`GITHUB_TOKEN` set. Take
     `STATE/delete.lock` non-blocking for the whole run.
  2. `account = arming.account(env)`; `target = verdict.resolve(...)`. A paired, armed target whose `gh_user` differs,
     or a sealed one whose `seal.gh_user` differs, is refused. Journal `cleaned` → `was deleted at`, exit 0. Journal
     `deleted` → step 9 when authorized; otherwise the `cleanup is not finished` line, exit 1.
  3. `--start` (call 10): needs a terminal and a sealed or moved target in `Shutdown`; `api.post_start()`, poll
     `read()` every 5 s up to 10 minutes for `Available`, print `Started NAME. Check it: CMD`, exit 0. Paired: refused
     with the `work:cs:restart` copy, no POST.
  4. `api = api_factory(name, gh_user=account)`; `reading = api.read()`. `Deleted` (confirmed): with anything kept
     for it locally (a journal past `confirmed`, a paired binding, an archived session) → step 9 when authorized
     (with no journal past `confirmed`: the `still keeps` line and a typed `delete` first), else the `cleanup is not
     finished` line, exit 1; with nothing kept → `NAME is already deleted.`, exit 0. Not `ok` → unreachable `GitHub
     did not answer`. `reading.repository` not equal to the project's (case-insensitive) → refused, exit 1.
  5. `unpaired`, or a state other than `Available` → `verdict.unreachable(...)` with its action line; no connection.
  6. Otherwise, inside one `connect(name, state=state)` (a `ValueError` entering it → unreachable `could not connect to
     NAME`): `gather`, `judge`, `fingerprint`. Print `as_json(...)` (with `--json`) or `lines(...)`.
  7. `--check`: exit 0 when deletable, else 1. Not deletable: the `Nothing was deleted. Resolve…` line, exit 1. No
     terminal: the no-terminal line, exit 1. Then `ask('> ')`; EOF or anything but `answer.strip() == 'delete'`:
     `Nothing was deleted.`, exit 1.
  8. Recheck (call 8): same connection, `arming.account(env)`, `resolve`, `api.read()`, `gather`, `judge`. Not
     deletable, a different fingerprint, account, kind, remote root, repository or state: the `changed since the
     check (WHAT)` line, exit 1, zero calls. Journal `confirmed`. Stop: paired → `power.stop(state, expected=name,
     api=api)` (a `ValueError`: `could not be stopped`, exit 1); sealed or moved → journal `stop-recorded`, then
     `api.post_stop()` (not 2xx: `could not be stopped`, exit 1). Journal `stop-requested`. Poll `read()` every 5 s up
     to 10 minutes until `ok`, `state == 'Shutdown'`, `pending is False` (None is not proof) and the same repository;
     then journal `shutdown`, else `has not reached Shutdown`, exit 1. Journal `delete-requested`; `status =
     api.delete()`; 4xx/5xx other than 404 → `GitHub refused`, exit 1; otherwise poll `api.confirm_deleted()` every
     5 s up to 2 minutes for `True` → journal `deleted`, else `does not show NAME as gone yet`, exit 1.
  9. Cleanup (call 14). Paired: under `workspace-control.lock` (blocking), if `scratch.json` names another Codespace,
     the project-wide steps are recorded `moved` and the moved line is printed; otherwise each step not yet `done`, in
     order: `keeper.stop`, `scratch.stop`, `workspace.retire(..., held=True)` (its archive path goes into the
     journal), the disarm
     (only when armed: merge `armed=False` as `__main__._arming_edit` does, without retaking the lock, and print the
     `Recovery is off` line), `ssh_hosts.remove`, `herdr.remove_profile` (`'kept'` prints the kept line), then, outside
     that lock, `scratch.terminate_retired` with the archive's `scratch-session.json`. Sealed and moved: only
     `terminate_retired` with the newest archive's receipt naming the target (none → `done`). A `ValueError` records
     its REASON and the run continues with the next step whose inputs exist (after a failed `retire`, `sync session`
     waits). All `done`, `kept` or `moved` → journal `cleaned`, `Deleted NAME.` and `Cleaned up: …`, exit 0;
     otherwise the `cleanup is not finished` line naming the first pending step, exit 1.

- [ ] **Step 1: Write the failing tests** (`tests/test_deletion.py`: a `FakeAPI` that scripts readings and counts
  `post_stop`, `post_start`, `delete`, `confirm_deleted`; `connect` yields a fake connection; `target_scan.scan` and
  `pushed.prove` patched to scripted results; the worker runtime written as fixtures; `ask` and `tty` injected;
  `power.stop` wrapped to record its call order):

```python
def test_check_json_makes_no_destructive_call(self):   # stdout parses; post_stop, delete, post_start, power.stop == 0
def test_check_never_cleans_up_at_any_phase(self):     # journals at each phase, and a confirmed-deleted paired target
                                                       # with no journal: --check, EOF and no terminal -> 0 mutations
def test_unsafe_never_asks_and_never_deletes(self):    # dirty second worktree; stash: ask not called, 0 calls, exit 1
def test_only_the_exact_word_proceeds(self):  # 'Delete', 'yes', 'delete it', '' -> 0 calls; ' delete ' -> deletes
def test_a_turn_written_just_before_the_check_needs_a_later_cycle(self):
    # the last completed cycle started before t0 -> History UNKNOWN until a cycle with started_at > t0 completes;
    # scans that differ -> every category UNKNOWN 'changed while it was being checked'
def test_a_change_during_the_prompt_voids_the_verdict(self):
    # recheck: a new fingerprint; a pushed branch now unpushed; the inbound receipt gone; another account; a rebind;
    # state Shutdown -> each WHAT line, 0 destructive calls
def test_paired_stop_is_recorded_before_the_post(self):  # power.stop called with expected=name before any DELETE
def test_retired_delete_keeps_new_recovery_working(self):
    # NEW paired and armed; delete sealed OLD: NEW's scratch.json, workspace.json, arming.json, SSH receipt and Herdr
    # receipt bytes identical; then KeeperStepTests.test_lifetime_restart_end_to_end_with_fakes's fixture
    # (tests/test_keeper.py FakeAPI, clock, FakeKeepAlive, FakeHerdr) drives Keeper.step for NEW through a lifetime
    # stop: it classifies LIFETIME and posts start once
def test_404_alone_is_not_deleted(self)          # delete() -> 404, confirm_deleted() None -> no cleanup, exit 1
def test_lost_delete_reply_is_confirmed_by_inventory(self)   # delete() -> 0, then confirm_deleted() True -> cleanup
def test_each_phase_resumes_safely(self):
    # stop-requested + Shutdown -> UNKNOWN with the start action, 0 POSTs; delete-requested + confirm None -> fresh
    # check, never a second DELETE on the old verdict; a crash after GitHub deleted a sealed OLD and before the journal
    # said deleted -> its archived session is terminated on the next authorized run; deleted with 'Herdr profile'
    # pending -> only that step runs, no API call
def test_rebind_during_cleanup_leaves_the_new_pairing_alone(self):
    # scratch.json names NEW when cleanup starts: keeper/worker/arming/SSH/Herdr untouched, steps 'moved', OLD's
    # archived session terminated
def test_externally_deleted_target_cleans_up_only_after_typing_delete(self)
def test_paired_target_is_never_started(self)          # --start on paired -> refused, post_start 0
def test_start_is_its_own_operation(self):             # starts, prints the check command, never scans or deletes;
                                                       # needs a terminal
def test_never_paired_target_is_not_connected(self)    # connect not called; four UNKNOWN 'pair it first'
def test_another_repository_is_refused_before_any_check(self)
def test_inside_a_codespace_or_with_an_env_token_refuses(self)
def test_cleanup_failure_is_pending_and_independent_steps_still_run(self)
def test_paired_cleanup_order_and_disarm_line(self)
# tests/test_lifecycle.py
def test_delete_sends_one_DELETE_to_the_named_route(self)
def test_reading_carries_the_repository(self)
# tests/test_project_sync_cli.py
def test_delete_subcommand_options(self):  # --json without --check, --start with --check, --start without
                                           # --codespace -> exit 2; work --codespace unchanged; delete runs headless
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
- Consumes: `seal.names` (Task 3); the journal format in Task 5's Interfaces (files only: this task imports neither
  `deletion.py` nor `verdict.py` and does not edit `__main__.py`).
- Produces:
  - Template stanza after `work:cs:restart`: `work:cs:delete`, `desc: Check a Codespace, then delete it after you type
    delete (-- --check --json only checks)`, `interactive: true` (as `work`: it reads a typed answer and also runs
    headless, plan check 4j), `cmds: [bash .devcontainer/scripts/project-sync.sh delete {{.CLI_ARGS}}]`. In this
    repository's root `Taskfile.yml` the same stanza runs `bash scripts/project-sync.sh delete {{.CLI_ARGS}}` (F15),
    like its neighbours. `root_tasks.ALIASES` gains `work:cs:delete` after `work:cs:restart`; `INTERACTIVE` gains it.
  - `standing.Local` gains `retired: tuple[str, ...] = ()` (`seal.names(state)` minus names whose journal is
    `cleaned`) and `cleanup: tuple[str, ...] = ()` (names whose journal phase is `deleted`, oldest first). Unreadable
    files add one problem each, as `read` does today. Both are local reads (no tool lookup, no network).
  - `overview.view` (F21): candidates are built as today. Then the oldest name in `local.cleanup`, if any, inserts
    `Finish cleaning up after NAME` → `work:cs:delete -- --codespace NAME` at position 0, or position 1 when the
    recovery-off rule put connect or start first, with the line `Cleanup after deleting NAME is not finished.`; other
    pending names are left to `work:status`. Then, if the oldest name in `local.retired` is among `space.name` for
    `remote.spaces.value` (status `ok`), `Check and delete NAME` → `work:cs:delete -- --codespace NAME` takes the last
    of the five candidate slots (replacing the fifth when all five are used). Never an action deleting
    `local.pairing`; nothing new inside a Codespace. Six lines, 78 cells, 3-6 actions hold.
  - Docs: README's "can be deleted at any time" sentence (`README.md:118-119`) becomes the safe order (replace, then
    `task work:cs:delete`, which checks first and deletes only on a typed `delete`); `docs/project-continuity.md`
    gets the command in its table and a short section: what is checked, what is not, `--check --json` for agents, a
    stopped Codespace reads Unknown until started, a Codespace moved with `--rebind` cannot be deleted this way, what
    cleanup removes and what it keeps.

- [ ] **Step 1: Write the failing tests:**

```python
# tests/test_root_tasks.py
def test_work_cs_delete_renders_description_interactive_and_root_alias(self)
def test_work_cs_delete_dispatches_in_both_taskfiles(self):
    # task --dry or a stubbed project-sync.sh: the generated root alias reaches .devcontainer/scripts/project-sync.sh
    # delete -- --check --json; this repository's root task reaches scripts/project-sync.sh with the same arguments
# tests/test_continuity_integration.py: the real task inventory test (:56) lists work:cs:delete
# tests/test_standing.py
def test_retired_and_cleanup_facts(self)     # a seal; a cleaned journal hides it; a deleted journal -> cleanup
# tests/test_overview.py
def test_cleanup_pending_comes_first_or_after_recovery(self)
def test_listed_sealed_retired_codespace_takes_the_last_slot(self):
    # with three pair actions, two refusals, a checkout update and an upgrade: still offered, still 3-6 actions
def test_unlisted_or_unsealed_retired_offers_nothing(self)
def test_the_current_pairing_is_never_offered_for_delete(self)
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_root_tasks tests.test_standing
  tests.test_overview tests.test_continuity_integration -v`. Expected: exit 1 on the new tests.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add -A templates Taskfile.yml devcontainer_setup README.md docs tests && git commit -m
  "Add task work:cs:delete, offer it for a replaced Codespace, and document the safe order"`

## Existing guards that must stay green (plan check, "Existing tests affected")

`tests/test_replication.py::test_status_does_not_reuse_previous_health_for_partial_stale_or_failed_cycle`,
`::test_cycle_progress_is_fresh_and_receipt_bound_with_failed_publication_pending`,
`::test_capture_issues_are_not_double_counted_as_publication_failures`;
`tests/test_native_checkpoints.py::test_completed_prefix_while_writer_appends_partial_next_turn` and
`::test_incomplete_turn_and_unknown_record_are_explicit_issues` (capture itself does not change);
`tests/test_workspace_recovery.py::test_status_is_offline_and_legacy_checkpoint_never_counts_as_conversation_health`,
`::test_repeated_connection_retains_separate_verified_local_historical_roots`;
`tests/test_herdr.py::test_remote_inside_herdr_reuses_machine_without_nested_client_or_settings_changes`,
`::test_a_receipt_from_before_codespace_tracking_adopts_the_current_machine`;
`tests/test_ssh_hosts.py::test_refresh_only_updates_previously_owned_alias`,
`::test_unknown_owner_edits_to_pending_artifacts_are_preserved_and_refused`;
`tests/test_standing.py::test_managed_aliases_match_root_tasks`;
`tests/test_work_overview.py::test_no_terminal_prints_state_and_next_lines_and_exits_0`,
`::test_printing_starts_downloads_and_signs_in_nothing`;
`tests/test_continuity_integration.py::test_real_owner_semantics_and_include_context_survive_full_candidate`;
`tests/test_wizard.py::test_existing_taskfile_variants_keep_real_task_operations`;
`tests/test_forget.py` (all).

## Left for stage D (its plan inherits these)

- Step 3 seals OLD: with OLD still paired and its agents ended, `deletion.gather` for OLD; when the evidence is
  steady, has no tails, and the bracketing cycle's inbound row is complete with nothing pending and no capture
  issues, `seal.write(...)` with that cycle's id, its inbound snapshot id and the replace operation id. Otherwise the
  replace stops before the move and says why.
- `Store.collect` keeps every manifest a seal names, and its blobs (INV §5, `checkpoint_store.py:287-323`), with a
  test; a C+D release dependency.
- A NEW that the replace journal names may be connected with its host key accepted (`accept_new=True`), so its check
  does not read "pair it first" (CHECK Q9).
- Step 7 offers `deletion.run(..., codespace=OLD)`; a delete of either name closes the replace journal (CHECK Q11).
  Cross-stage tests for both before the release.

## Order and parallelism

At most three builders at once (shared Mac, memory `shared-mac-load-flakes`). Wave 1: Tasks 1, 2 and 4, from the
final plan commit (Task 2 does not import Task 1). Wave 2: Task 3, from the merge of 1, 2 and 4 (it edits
`workspace.py` after Task 4's `retire`). Wave 3: Tasks 5 and 6 in parallel, from the merge of 3. They share no file:
Task 5 owns `__main__.py`, `lifecycle.py` and `deletion.py`; Task 6 owns the template, `Taskfile.yml`,
`root_tasks.py`, `standing.py`, `overview.py` and the docs. The lead merges each into `feature/task-work-cd` after its
own re-gate, one gate at a time.

## Live check (part of the C+D release gate, not of these tasks)

With stage D, on a clone of innovate under its own `XDG_STATE_HOME` with the 2026-10-27 backup runbook: after the
replace, `task work:cs:delete -- --codespace OLD --check --json` (exit and JSON), a scratch edit on OLD making it
Not safe, the typed delete of OLD (NEW's `arming.json` and keeper unchanged), then the test's NEW deleted as the
paired target (a turn written just before the check waits for a later cycle; stop recorded; cleanup removes the
clone's SSH alias and Herdr profile and nothing else). It also confirms on a real Codespace what offline tests cannot:
`/proc` readability, the Git options the scan uses, and `pending_operation` being present in the API body.
