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
(F1-F21), and the second check `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-c/plancheck2/CHECK.md`
(CHECK2 N1-N13), and the third, `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-c/plancheck3/CHECK.md`
(CHECK3 M1-M10). Line numbers are at b4a8f6c.

## Global Constraints

- Gates: `task check` and `task test` (`python3 -m unittest discover -s tests -v`), both exit 0, at your commit, run
  in the default environment (no sandbox, no `TMPDIR` override):
  `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-a/RULING-gates.md`.
- Focused runs: run `python3 -m unittest …` unpiped (or redirect to a file and read `$?`); a `| tail` hides the exit code.
- Branches: cut from `feature/task-work-cd` at the commit your brief names; merge back into it, never `main`. C and D
  ship together after one live test of replace end to end (spec call 1).
- Commit messages: plain imperative sentence case, one concern per commit, no AI attribution of any kind.
- **Nothing changes before the typed word.** With `--check`, without a terminal, on EOF or another word at a prompt,
  and on every refusal, at every journal phase: zero calls to `post_stop`, `delete`, `post_start`, `power.stop`,
  `arming.record_owner_stop`, any cleanup step or primitive, and no capture, acknowledgement or publication of
  conversations. Tests count these calls. Cleanup runs only on an authorized terminal run (call 9); `--start` is its
  own operation (call 10).
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
2. **The final-flush record is defined here, written by D** (`seal.write` and `seal.read` in Task 3). `seal.write`
   loads the named snapshot from the Mac store and refuses unless the scan compared exactly that snapshot's entry
   files, every prefix digest equal, no unfinished turn (CHECK2 N1); D calls it with `deletion.gather` evidence.
3. **History for the paired Codespace is proved twice** (F1, CHECK2 N1). `deletion.gather` scans once, waits for the
   worker to complete a cycle that started after that scan **finished** (the worker cycles continuously,
   `scratch.py:712-758`), then scans again with that cycle's inbound manifest: for every entry file it compares the
   sha256 of the file's first `size` bytes with the manifest's digest (custody by digest, not by stability), and reads
   what follows each entry's own cutoff. The two scans must have equal fingerprints, so a session the capture's scope
   rules left out was seen by that capture unchanged. History is then SAFE when the cycle's inbound direction
   (`codespace_to_desktop`) completed with its receipt bound to the Mac's, nothing pending, no capture issues, no
   digest differs, and no unfinished turn follows a cutoff (F3). Codex roots are `sessions/` and `archived_sessions/`
   (`sessions.py:221-239`, CHECK2 N2). Tails: Codex keeps trailing metadata inside the cutoff
   (`conversations.py:233-237`), so under the bracket any byte after it is a turn the capture left out; Claude counts a
   `user` record, an `assistant` record, or a final line without a newline; other record types are metadata
   (CHECK2 N11). A `user` record is metadata only when `isMeta` is `true`, or when its `message.content` (a string, or
   a list whose items are all `{"type": "text", "text": str}`, joined) starts with `<command-message>`,
   `<command-name>` or `<local-command-` (CHECK3 M7); any other content, including tool results and interruption
   markers, counts, and so does a shape the rule cannot decode. An interrupted turn (Esc) therefore blocks until it is resumed and finishes. The owner's "Not
   checked" list stays exactly the three items of ruling 29.
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
   when authorized: the journal's phase is `confirmed` or later (the owner typed `delete` for this Codespace; that
   authorization lasts across runs, CHECK2 N12), or, with no such journal, the owner types `delete` again after a
   line naming what this Mac still keeps (F5). Cleanup uses the journal's recorded `kind` and `archive`, never a
   fresh `resolve`, which a finished retirement would turn into `moved` (CHECK2 N4). A Codespace GitHub confirms
   deleted (even after a lost reply or a crash before the journal said so) goes to that cleanup for every kind (F10).
   Any other rerun checks from scratch: a target left Shutdown reads UNKNOWN with its start action.
10. **`--start` is its own operation** (F16): `work:cs:delete -- --codespace NAME --start` starts a sealed or moved
    Codespace in a terminal, after the same account, repository and state checks as a delete (CHECK2 N7), waits for
    Available, prints the check command and exits; it never checks, deletes or cleans up in the same run. The paired
    Codespace's action is `task work:cs:restart`, which records start evidence for the keeper (`_owner_power`);
    `deletion.py` never POSTs start for it.
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
16. **Filter drivers are switched off, not refused** (Task 1 build, 2026-11-09). The owner's dotfiles set a global
    git-lfs `clean`/`process` filter and the image installs git-lfs, so refusing any configured filter would make every
    worktree unprovable. With filters off, `status` never runs a helper; a changed file still reads changed, and a
    stat-dirty but unchanged LFS file reads changed (raw content against the pointer blob): that blocks a delete it
    need not, never passes one it should not. Evidence:
    `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-c/verify/scan-filter-probe.log`.
17. **A repository inside a directory Git ignores is still a repository** (stage C review, 2026-11-09). Ruling 29
    lets files Git ignores pass; a clone in an ignored `tmp/` or a repository inside `.scratch/` (generated projects
    ignore all of `.scratch`) holds commits, not ignored files, and spec C asks for every repository under
    `/workspaces`. So the walk enters ignored directories to look for repositories (Task 7 R2). It blocks a delete
    it might not need to (a throwaway clone with unpushed commits), never passes one it should not. Evidence:
    `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-c/review-fable/REVIEW.md` P1-2.

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
  `not a GitHub repository`, `this Mac's checkout is shallow or partial`, and `PATH: its origin cannot be read`.
- Scratch: UNSAFE `N files differ from this Mac's copy (…)`, `N conflicts (…)` (paired), `N files changed or added
  since the final flush (…)` (sealed); UNKNOWN `sync is not running; run CMD, then check again` (CMD = `work:cs`),
  `could not read .scratch on NAME`, `moved with --rebind, so no final flush was recorded`.
- History: UNSAFE `N conversations have not reached this Mac yet`, `N conversations could not be read on NAME`,
  `N conversations differ from this Mac's copy (agent SESSION_ID, …)`,
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
  STEP from `keeper`, `sync worker`, `pairing`, `Herdr connections`, `recovery`, `SSH alias`, `Herdr profile`,
  `sync session` · cleanup REASONs include `a Herdr connection to NAME is still open` ·
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
    `cutoffs_sha256: str | None`, `checked: tuple[tuple[str, str], ...]` ((agent, basename) of every `cutoffs` key
    found on the Codespace),
    `mismatched: tuple[tuple[str, str], ...]` (found, but its first `size` bytes differ from the digest or it is
    shorter), `tails: tuple[tuple[str, str], ...]` (own files with an unfinished turn); all three empty without
    `cutoffs`,
    `agents: tuple[tuple[str, int], ...]` (name → count, for `claude codex grok opencode herdr`),
    `agents_complete: bool`, `problems: tuple[str, ...]` (fixed texts).
  - `Cutoff` (frozen): `size: int`, `sha256: str`, `own: bool` (the entry's own file; False for a dependency).
  - `scan(connection, codespace: str, remote_root: str, *, cutoffs: Mapping[tuple[str, str], Cutoff] | None = None,
    limits: Limits = LIMITS, workspaces: str = '/workspaces', proc: str = '/proc', now=time.time) -> Scan`. One
    `connection.python(code, payload=…, timeout=limits.seconds + 30)`. `cutoffs` maps (agent, file basename) to what
    a manifest's entry files record (`files[].size`, `files[].sha256`; when a basename is both an own file and a
    dependency, the own file's record wins). Never raises for a failed or malformed answer: a `ValueError` from the connection, output
    above `limits.output`, or JSON that fails strict validation (types, 40-hex ids, no control characters in paths)
    gives every part `complete=False` and one problem.
  - `fingerprint(scan: Scan) -> str | None`: sha256 hex of `json.dumps(…, sort_keys=True, separators=(',', ':'))` of
    `repositories`, `scratch`, `sessions` and `agents`; None unless all four `*_complete` are true.
  - `cutoffs_digest(cutoffs) -> str`: sha256 hex of the canonical JSON of the cutoffs map (keys `agent/basename`,
    values `[size, sha256, own]`); `scan` stores it in `Scan.cutoffs_sha256` (None without cutoffs), so a record made
    from the scan can prove which manifest it was compared with (CHECK3 M1).
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
  - Filter drivers (call 16): `status` runs configured `clean`/`process` filters on stat-dirty files, and a filter can
    write (git-lfs stores objects). Before each worktree's `status`, read the driver names with
    `git config -z --name-only --get-regexp '^filter\..*\.(clean|process)$'` (exit 1 = none), strip `filter.` and the
    trailing `.clean`/`.process`, and pass `-c filter.N.clean= -c filter.N.process= -c filter.N.required=false` for
    each name to that `status`. A name that is empty or contains `=` or a control character marks the worktree
    `ok=False` (spliced into `-c`, `=` would assign a command). No filter ever runs; an unused driver (a global git-lfs
    setup) costs nothing.
  - Scratch: `scratch_tree.entries(<remote_root>/.scratch)`; hash each regular file. Missing `.scratch` = empty map,
    complete. An error from `entries` (symlink, special file, case collision) = incomplete.
  - Conversations: walk `sessions.native_home('codex')` `sessions/` and `archived_sessions/`, and
    `sessions.native_home('claude')/'projects'`, without following links; hash regular files (keys `codex/sessions/…`,
    `codex/archived_sessions/…`, `claude/…`). Missing directory = empty, complete. For a file whose (agent, basename)
    is in `cutoffs`: compare the sha256 of its first `size` bytes with the given digest (a shorter file or a different
    digest goes to `mismatched`); for an entry's own file (`own=True`), read the bytes after `size`: Codex: any byte is
    an unfinished turn; Claude: call 3's rule. A basename found twice under one agent makes `sessions_complete` false.
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
def test_conversation_files_are_hashed_for_codex_and_claude(self)   # including codex archived_sessions/
def test_prefix_digests_are_compared(self):   # equal -> checked; a changed byte inside size -> mismatched;
                                              # a file shorter than size -> mismatched; a dependency file -> no tail read
def test_unfinished_turns_after_the_cutoff(self):
    # codex (sessions/ and archived_sessions/): bytes after the cutoff -> tail;
    # claude after end_turn: system, last-prompt, cost-state, an isMeta user record, '<command-message>',
    # '<command-name>' and '<local-command-' wrappers as a string and as a list of text blocks -> no tail; a prompt
    # user record (string or list), an interruption marker (list), a tool result, an undecodable content shape, an
    # assistant record, a partial line -> tail; no cutoffs -> checked, mismatched, tails all () and cutoffs_sha256 None
def test_cutoffs_digest_is_recorded(self):   # same map -> same digest; one changed sha256 -> a different digest
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
  - Common environment for every Git call (CHECK3 M6): the inherited environment minus `GIT_DIR`, `GIT_WORK_TREE`,
    `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_INDEX_VERSION`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`,
    `GIT_QUARANTINE_PATH`, `GIT_NAMESPACE`, `GIT_SHALLOW_FILE`, `GIT_REPLACE_REF_BASE`, `GIT_GRAFT_FILE`, `GIT_CONFIG`,
    `GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`, `GIT_CURL_VERBOSE` and every `GIT_TRACE*` (the
    owner's authentication and configuration variables, such as `GIT_SSH_COMMAND`, `GIT_ASKPASS`, `GIT_CONFIG_COUNT`,
    stay), then `GIT_TERMINAL_PROMPT=0`, `GIT_NO_REPLACE_OBJECTS=1`, `GIT_GRAFT_FILE=/dev/null` (a local graft can
    otherwise turn an unpushed commit into a contained one, CHECK2 N3), `GIT_OPTIONAL_LOCKS=0`, `GIT_NO_LAZY_FETCH=1`,
    `LC_ALL=C`; no preserved value is logged; stderr discarded; `timeout`. A shallow
    (`rev-parse --is-shallow-repository` prints `true`) or partial (`config --get remote.origin.promisor` or
    `extensions.partialClone` set) Mac checkout cannot prove ancestry: every ref of those claims is unknown
    (`this Mac's checkout is shallow or partial`).
  - `identity == project.identity`: at most one fetch per call, in `project.root`, keeping the owner's credential
    configuration (a private repository needs it): `git -c credential.interactive=never -c fetch.pruneTags=false
    -c remote.origin.pruneTags=false -c fetch.writeCommitGraph=false -c maintenance.auto=false -c gc.auto=0 fetch
    --quiet --prune --no-tags --refmap= --no-write-fetch-head --no-recurse-submodules origin
    '+refs/heads/*:refs/project-sync-check/heads/*'`. The empty `--refmap=` stops `remote.origin.fetch` from also
    updating `refs/remotes/*` (F7). The fetch adds objects and may write reflogs for the private refs; it moves no
    owner ref, tag, `HEAD`, index or `FETCH_HEAD`. A failed fetch makes every ref of those claims unknown
    (`GitHub could not be read from this Mac`). All ids go through one `git cat-file --batch-check` (stdin one id per
    line; CHECK2 N8: `cat-file -e <oid>^{commit}` exits 128 for a missing id): `<oid> missing` → unpushed;
    `<oid> commit <size>` → then `git for-each-ref --count=1 --contains <oid> refs/project-sync-check/heads/`: a line
    → pushed, exit 0 with no line → unpushed, nonzero → unknown; any other type, or the batch failing → unknown. In `finally`, every ref under the
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
def test_owner_authentication_variables_are_kept(self):   # GIT_SSH_COMMAND and GIT_CONFIG_COUNT entries pointing at
                                                          # fixture stubs reach the fetch; GIT_DIR and GIT_TRACE do not
def test_a_local_graft_proves_nothing(self):   # an unrelated local-only commit grafted onto a fetched head -> unpushed,
                                               # also with an inherited GIT_GRAFT_FILE pointing at that graft
def test_shallow_or_partial_checkout_is_unknown(self)
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
- Consumes: `target_scan.Scan`, `target_scan.Cutoff`, `target_scan.fingerprint` (Task 1), `pushed.Claim`, `pushed.Proof` (Task 2),
  `standing.refusals` (`standing.py:301-338`), `forget.forgotten`, `sync_cycle.summary`, `sync_cycle.receipt_matches`,
  `checkpoint_store.Store`, `overview.fit`, `overview.task_command`.
- Produces, `workspace.py`: `cycle_scope(project, value: dict, runtime: dict, sync: dict) -> tuple[dict, bool]`, a
  pure function returning the worker's `conversations` cycle and the `current` flag from observations the caller
  already read (registry `value`, `scratch.status` runtime, `inspect_sync` result), computed exactly as `status()`
  computes them today (`workspace.py:358-363`); `status()` calls it with its own reads, unchanged in output
  (CHECK2 N10). `inspect_sync` stays the caller's read (it takes `tools.lock`).
- Produces, `seal.py`:
  - `Seal` (frozen): `codespace`, `identity`, `repository`, `gh_user`, `remote_root`, `operation: str`,
    `sealed_at: float`, `cycle_id: str`, `snapshot_id: str` (64-hex), `scratch: tuple[tuple[str, str], ...]`,
    `sessions: tuple[tuple[str, str], ...]`.
  - `write(state, store, scan, *, identity, repository, gh_user, remote_root, operation, cycle_id, snapshot_id, now)
    -> Path`: loads `snapshot_id` from `store` (`Store.load`, which verifies its blobs) and, with `K =
    verdict.cutoffs(manifest)` and `F` the (agent, basename) of every `scan.sessions` key, refuses (`ValueError`)
    unless the scan's fingerprint is not None, `scan.cutoffs_sha256 == target_scan.cutoffs_digest(K)`,
    `set(scan.checked) == set(K) & F`, and `mismatched` and `tails` are empty (CHECK2 N1, CHECK3 M1); writes
    `STATE/retired/<codespace>.json`
    (`{"schema": 1, …}`) with `state.atomic_write`, replacing an older seal for the same name.
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
  - `cutoffs(manifest) -> dict[tuple[str, str], Cutoff]` and `sessions_by_file(manifest) -> tuple[tuple[str, str,
    str], ...]` from a loaded snapshot manifest's `conversations.entries` (each entry's `files`: own when the file's
    `session_id` is the entry's); used by Task 5's `gather` and by `seal.write`, so both read a manifest one way.
  - `judge(target, evidence, *, now) -> tuple[Category, ...]` and `unreachable(target, reason) -> tuple[Category, ...]`
    (four UNKNOWN categories with one reason).
  - `kept_conversations(state, project, binding) -> tuple[tuple[str, str], ...]`: the paired Codespace's outbound
    refusals plus forgotten conversations, unique and sorted (call 4: a notice).
  - `lines(verdict, local) -> list[str]` (the Copy section's human verdict, untrusted strings through `overview.fit`,
    each line at most 78 cells) and `as_json(verdict, *, outcome='checked', message=None, phase=None, cleanup=None)
    -> dict` with exactly: `schema` (1), `outcome` (`checked`, `unreachable`, `cleanup-pending`, `cleaned`,
    `already-deleted`, `refused`), `message` (the Copy line for any outcome but `checked`, else null), `codespace`,
    `target` (kind, or null), `checked_at` (UTC ISO 8601), `account`, `repository`, `categories` (list of `{name,
    verdict, text, items}`, names lower-case; four UNKNOWN for `unreachable`, empty for the journal and refusal
    outcomes), `kept_on_this_mac_only` (list of `{agent, session_id}`), `not_checked` (`NOT_CHECKED`), `deletable`,
    `fingerprint`, `phase` (journal phase or null), `cleanup` (the journal's map or null). Every `--check --json` run
    prints exactly one such object (CHECK2 N9).
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
    any `scan.mismatched` → UNSAFE `differ from this Mac's copy`; any tail → UNSAFE `a turn that never finished`,
    each naming `agent SESSION_ID` from `sessions_by_file`; else SAFE. The
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
def test_a_scan_not_bound_to_the_snapshot_is_refused(self):  # a valid unrelated snapshot; a mismatched prefix;
                                                             # an own session file the scan did not check
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
def test_unfinished_turn_or_differing_prefix_is_unsafe_and_named(self)
def test_cutoffs_read_own_and_dependency_files_one_way(self)
def test_json_for_every_outcome(self):   # checked, unreachable, cleanup-pending, cleaned, already-deleted, refused
def test_sealed_late_scratch_or_newer_turn_is_unsafe(self):   # a new scratch file; a changed codex session file
def test_sealed_lost_capture_is_unknown(self)                  # capture_loads False
def test_moved_target_cannot_be_deleted(self):                 # Git+Agents SAFE, Scratch+History UNKNOWN
def test_failed_process_read_is_unknown(self)
def test_verdict_lines_match_the_copy(self):   # the Copy section's example frame, byte for byte
def test_untrusted_names_never_reach_the_terminal_raw(self):   # escape sequences in a path, branch and session id
def test_json_has_exactly_the_documented_fields_and_no_urls(self)
def test_cycle_scope_matches_status(self):   # status() before and after the extraction, same fixtures: equal
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_seal tests.test_verdict -v`. Expected:
  exit 1, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `seal.py`, `verdict.py` and `workspace.cycle_scope` as in Interfaces.
- [ ] **Step 4: Run the gates.** Run: `task check && task test`. Expected: exit 0.
- [ ] **Step 5: Commit** — `git add -A devcontainer_setup/project_sync tests && git commit -m "Judge a Codespace
  safe to delete per category, against its final flush when it was replaced"`

### Task 4: Cleanup primitives, each owned and replayable

**Files:**
- Modify: `devcontainer_setup/project_sync/ssh_hosts.py` (add `remove`), `devcontainer_setup/project_sync/herdr.py`
  (provenance and the lock in `open_remote`, add `remove_profile`), `devcontainer_setup/project_sync/workspace.py`
  (add `retire`; pass `expected_codespace` at `:347`), `devcontainer_setup/project_sync/scratch.py` (add
  `terminate_retired`; `start` re-reads the binding under its lock), `devcontainer_setup/project_sync/keeper.py`
  (`start` re-reads the binding under its lock), `devcontainer_setup/project_sync/restore.py` (add
  `end_target_proxies`; exact `_served`)
- Test: `tests/test_ssh_hosts.py`, `tests/test_herdr.py`, `tests/test_scratch_sync.py`, `tests/test_restore.py`,
  `tests/test_retire.py` (new)

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
    for the saved id (the no-match and replacement branches), `False` when it adopted an existing match, and the
    previous value when the saved id is unchanged; a legacy receipt is never promoted. It gains a keyword
    `expected_codespace: str`, which `workspace.remote:347` passes as its selected `name` (CHECK3 M3), and takes
    `workspace-control.lock` (blocking) around reading the binding, `configure_ssh`, the catalog match or `machine
    add`, and the receipt write, refusing with `ValueError('No Codespace is paired; run task work:cs')` when
    `scratch.json` is absent, or `ValueError('The project moved to another Codespace meanwhile; run task work:cs')`
    when it names another Codespace, another identity or another remote root; it releases the lock before `prepared`
    and before opening Herdr (CHECK2 N5). Its only production caller already released the lock. Ten existing tests in
    `tests/test_herdr.py::HerdrTests` call it without a binding (CHECK3 M9): seed a matching `scratch.json` in each
    and pass `expected_codespace`, keeping their assertions, and change
    `test_a_missing_binding_never_counts_as_a_replaced_codespace` to expect the refusal with zero writes.
  - `restore.end_target_proxies(codespace: str, state: Path, *, wait: float = 120, run=subprocess.run,
    now=time.time, sleep=time.sleep) -> str` (`'ended'` | `'absent'`): this project's Herdr proxies serving
    `codespace` (`_proxies`); for each, `state.process_identity(pid)` is read again just before SIGTERM and must equal
    the identity read when it was listed (else it is skipped as gone); then poll `_proxies` every 2 s until none
    serves `codespace`. Origin fix in `restore._served` (CHECK3 M2), used by every caller: a command serves Codespace
    `C` only when it ends with exactly `" -I -B -c " + BOOTSTRAP + " " + C + " " + str(state)`, where `BOOTSTRAP` is
    the bootstrap string `ssh_hosts._proxy_command` builds for this package and `C` is a valid Codespace name; the
    executable before it is not compared (the Python path may have moved). Another project's proxy whose state path
    ends with this one, or a stray command with the marker, no longer matches. `ps` unreadable: `ValueError('the process list cannot be
    read')`; survivors after `wait`: `ValueError('a Herdr connection to NAME is still open')` (CHECK2 N6).
  - `herdr.remove_profile(project, state: Path) -> str` (`'removed'` | `'kept'` | `'absent'`): no receipt → `'absent'`.
    Herdr not installed or its catalog unreadable → `ValueError('Herdr could not be read')`, receipt kept. The catalog
    row for this project's alias and session (`_matching_machines`, compare each row's `id`) with the receipt's
    `machine_id` and `created is True` → `herdr machine remove <id>`, re-read and confirm it is gone (else
    `ValueError('Herdr did not remove the profile')`), then drop the receipt → `'removed'`. `created` missing or false
    → receipt dropped, profile kept → `'kept'`. No such row → receipt dropped → `'absent'`.
  - `workspace.retire(project, state: Path, codespace: str, *, held: bool = False) -> Path` under
    `workspace-control.lock` (taken by `retire` itself unless `held=True`, when the caller already holds it: the lock
    is not reentrant, `state.py:281-302`), with an intent
    file `STATE/retire-intent.json` (`{"schema": 1, "codespace", "archive", "binding_sha256", "session_sha256",
    "registry_sha256", "registry_after_sha256"}`, written with the rewritten registry already computed) (F12). From
    its liveness check to its last unlink it also holds `keeper-control.lock`, then `scratch-control.lock`, and
    `scratch.start` and `keeper.start` re-read `scratch.json` under their own lock and refuse when it is absent
    (CHECK3 M8; the builder confirms no existing path takes those two locks in the other order, and reports it).
    Refuses while `scratch.status(state)['running']` or `keeper.status(state)` shows a live keeper (`stop the keeper
    and sync worker first`), and refuses to start unless `scratch.json` names `codespace`.
    Steps: write the intent; copy `scratch.json`, `scratch-session.json` (if any) and `workspace.json` into the new
    `retired-bindings/<time_ns>-<8 hex>/` with `deleted.json` (`{"schema": 1, "codespace", "deleted_at"}`); rewrite
    `workspace.json` in place (it is never unlinked) when its sha256 is `registry_sha256`, keeping every key
    except `codespace`, `remote_root`, `remote_active_root` and `verified_remote_historical_roots`, with
    `verified_historical_roots` set to `verified_local_historical_roots`, `active_host='local'` and
    `local_active_root` the checkout (F13); unlink exactly `scratch-session.json` and then `scratch.json`, each when
    its sha256 is the intent's; remove the intent. A replay with the intent present first checks every artifact
    against its allowed states (CHECK3 M4): archive files equal to the intent's digests; `workspace.json` at
    `registry_sha256` (rewrite it) or `registry_after_sha256` (done); `scratch-session.json` and `scratch.json` at their
    digest (unlink) or absent (done). Any other state refuses (`the pairing changed since the delete began`) before
    touching anything, so a newer pairing's files are never touched. A call with no intent and no
    `scratch.json` naming `codespace` returns the newest archive whose `deleted.json` names it (the retirement already
    finished, CHECK2 N4), else raises `no pairing for NAME to retire`. Returns the archive.
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
def test_receipt_records_whether_setup_created_the_profile(self)   # add, replacement -> True; adoption -> False;
                                                                    # same id -> kept; legacy -> not promoted
def test_only_a_created_profile_is_removed(self)
def test_unreadable_herdr_keeps_the_receipt(self)
def test_a_connect_resumed_after_retirement_writes_nothing(self):  # binding gone when open_remote takes the lock:
                                                                    # no SSH alias, no machine add, no receipt
def test_a_connect_resumed_after_a_rebind_writes_nothing(self):    # binding names NEW, expected_codespace is OLD
# tests/test_restore.py
def test_end_target_proxies_ends_and_waits_for_only_that_codespace(self)
def test_another_projects_proxy_with_a_suffix_state_path_is_not_ours(self)   # CHECK3 M2's two counterexamples
def test_a_reused_pid_is_not_signalled(self)
def test_a_surviving_or_unlisted_proxy_keeps_the_step_pending(self)
# tests/test_retire.py
def test_retire_archives_then_clears_the_pairing_and_keeps_local_history(self):
    # afterwards: registry() valid, active_host 'local', verified_local_historical_roots kept;
    # standing.read(root): setup 'installed', pairing None
def test_retire_refuses_another_codespace_or_a_running_worker(self)
def test_retire_replays_after_each_step(self)        # crash after the intent, each copy, the registry, each unlink
def test_finished_retire_is_found_again(self)        # intent removed, caller crashed: returns that archive
def test_retire_replay_accepts_its_own_successor_states(self)  # crash after the rewrite, after each unlink
def test_a_start_during_retirement_waits_and_then_refuses(self)
def test_replay_never_touches_a_newer_pairing(self)  # crash, then a new scratch.json for another Codespace, replay
# tests/test_scratch_sync.py
def test_terminate_retired_ends_only_that_session(self)      # fake _mutagen; the live session is untouched
def test_terminate_retired_is_idempotent(self)               # a replay after success returns 'absent'
def test_terminate_refuses_the_live_session(self)
def test_actual_terminate_of_a_paused_retired_session(self)  # gated like the other real-Mutagen tests
```

- [ ] **Step 2: Run them; they fail.** Run: `python3 -m unittest tests.test_ssh_hosts tests.test_herdr
  tests.test_restore tests.test_retire tests.test_scratch_sync -v`. Expected: exit 1, `AttributeError` for each new function.
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
  - `STEPS = {'paired': ('keeper', 'sync worker', 'pairing', 'Herdr connections', 'recovery', 'SSH alias',
    'Herdr profile', 'sync session'), 'sealed': ('Herdr connections', 'sync session'), 'moved': ('Herdr
    connections', 'sync session')}`.
  - `gather(project, state, target, connection, *, now=time.time, sleep=time.sleep) -> verdict.Evidence`. Sealed and
    moved: one `scan` with no cutoffs, `steady=True`; sealed also `capture_loads`. Paired: scan one; `t1 = now()` when
    it returns (CHECK2 N1); poll every 2 s, up to 6 minutes, reading `workspace.registry`, `scratch.status` and
    `scratch.inspect_sync` and passing them to `workspace.cycle_scope`, for a `current` cycle with `started_at > t1`
    and `completed_at` set; load its inbound snapshot (the Mac's `codespace` receipt, `workspace._conversation_receipt`)
    for `verdict.cutoffs` and `verdict.sessions_by_file`; hash the Mac's `.scratch` with `scratch_tree.entries`; scan
    two with those cutoffs; `steady` = equal non-None fingerprints; proofs from scan two. Stage D's seal step calls
    `gather` for the paired OLD (call 2).
  - `run(project, state, *, codespace: str | None, check: bool, as_json: bool, start: bool, env=os.environ,
    ask=input, tty=None, api_factory=lifecycle.LifecycleAPI, connect=transport.CodespaceConnection, now=time.time,
    sleep=time.sleep) -> int`.
- The order `run` follows (every message from the Copy section; `tty()` defaults to `sys.stdin.isatty() and
  sys.stdout.isatty()`; "authorized" means not `check` and `tty()`). With `--json`, every exit below prints exactly
  one `verdict.as_json` object with its `outcome` instead of the line (CHECK2 N9):
  1. Refuse inside a Codespace (`CODESPACES == 'true'`) and with `GH_TOKEN`/`GITHUB_TOKEN` set (`refused`). Take
     `STATE/delete.lock` non-blocking for the whole run.
  2. `account = arming.account(env)`; `target = verdict.resolve(...)`; `journal` = `STATE/deletions/<name>.json` if
     any. A journal whose `account` or `repository` differs from now is refused (`NAME's delete was started as LOGIN
     for REPO; …`). A paired, armed target whose `gh_user` differs, or a sealed one whose `seal.gh_user` differs, is
     refused. Journal `cleaned` → `was deleted at` (`cleaned`), exit 0. Journal `deleted`: `--start` is refused
     (`NAME was deleted`); otherwise go to step 5 with no API call (CHECK3 M5).
  3. `api = api_factory(name, gh_user=account)`; `reading = api.read()`. Not `ok` → unreachable `GitHub did not
     answer` (no further step). `Deleted` (confirmed; a 404 body has no repository) → step 5, identity from the
     journal or the target's own records. Any other valid reading whose `repository` differs from the project's
     (case-insensitive) → refused, exit 1.
  4. `--start` (call 10, CHECK2 N7): needs a terminal and a sealed or moved target in `Shutdown`; `api.post_start()`,
     poll `read()` every 5 s up to 10 minutes for `Available`, print `Started NAME. Check it: CMD`, exit 0. Paired:
     refused with the `work:cs:restart` copy, no POST. Any other state: its unreachable line, exit 1.
  5. Journal `deleted`, or `reading.state == 'Deleted'` (confirmed) with anything kept for it locally (a journal at
     `confirmed` or later, a paired binding, an archived session): not authorized → `cleanup is not finished`
     (`cleanup-pending`), exit 1; authorized with a journal at `confirmed` or later → step 9; authorized without one →
     the `still keeps` line and a typed `delete` (EOF or another word: `Nothing was deleted.`, exit 1), then a journal
     at `deleted` with the kind `resolve` gives now, then step 9. Confirmed deleted with nothing kept → `NAME is
     already deleted.` (`already-deleted`), exit 0.
  6. `unpaired`, or a state other than `Available` → `verdict.unreachable(...)` with its action line (`unreachable`);
     no connection. Otherwise, inside one `connect(name, state=state)` (a `ValueError` entering it → unreachable
     `could not connect to NAME`): `gather`, `judge`, `fingerprint`; print `lines(...)` or `as_json(...)`.
  7. `--check`: exit 0 when deletable, else 1. Not deletable: the `Nothing was deleted. Resolve…` line, exit 1. No
     terminal: the no-terminal line, exit 1. Then `ask('> ')`; EOF or anything but `answer.strip() == 'delete'`:
     `Nothing was deleted.`, exit 1.
  8. Recheck (call 8): same connection, `arming.account(env)`, `resolve`, `api.read()`, `gather`, `judge`. Not
     deletable, a different fingerprint, account, kind, remote root, repository or state: the `changed since the
     check (WHAT)` line, exit 1, zero calls. Journal `confirmed` with `kind`. Stop: paired → `power.stop(state,
     expected=name, api=api)` (a `ValueError`: `could not be stopped`, exit 1); sealed or moved → journal
     `stop-recorded`, then `api.post_stop()` (not 2xx: `could not be stopped`, exit 1). Journal `stop-requested`. Poll
     `read()` every 5 s up to 10 minutes until `ok`, `state == 'Shutdown'`, `pending is not True` (as
     `lifecycle.py:289` reads it for a restart; a pending operation the API reports makes DELETE fail visibly instead)
     and the same repository; then journal `shutdown`, else `has not reached Shutdown`, exit 1. Journal
     `delete-requested`; `status = api.delete()`; 4xx/5xx other than 404 → `GitHub refused`, exit 1; otherwise poll
     `api.confirm_deleted()` every 5 s up to 2 minutes for `True` → journal `deleted`, else `does not show NAME as gone
     yet`, exit 1.
  9. Cleanup (calls 9, 14), with the journal's `kind` and `archive`. Paired: under `workspace-control.lock`
     (blocking), if `scratch.json` names another Codespace, the project-wide steps not yet `done` are recorded `moved`
     and the moved line is printed; otherwise each step not yet `done`, in order: `keeper.stop`, `scratch.stop`,
     `workspace.retire(..., held=True)` (its archive path goes into the journal before the next step),
     `restore.end_target_proxies`, the disarm (only when armed: merge `armed=False` as `__main__._arming_edit` does,
     without retaking the lock, and print the `Recovery is off` line), `ssh_hosts.remove`, `herdr.remove_profile`
     (`'kept'` prints the kept line); then, outside that lock, `scratch.terminate_retired` with the archive's
     `scratch-session.json`. Sealed and moved: `restore.end_target_proxies`, then `terminate_retired` with the newest
     archive's receipt naming the target (none → `done`). A `ValueError` records its REASON and the run continues with
     the next step whose inputs exist (after a failed `retire`, `sync session` waits). All `done`, `kept` or `moved` →
     journal `cleaned`, `Deleted NAME.` and `Cleaned up: …`, exit 0; otherwise the `cleanup is not finished` line
     naming the first pending step, exit 1.

- [ ] **Step 1: Write the failing tests** (`tests/test_deletion.py`: a `FakeAPI` that scripts readings and counts
  `post_stop`, `post_start`, `delete`, `confirm_deleted`; `connect` yields a fake connection; `target_scan.scan` and
  `pushed.prove` patched to scripted results; the worker runtime written as fixtures; `ask` and `tty` injected;
  `power.stop` wrapped to record its call order):

```python
def test_check_json_makes_no_destructive_call(self):   # stdout parses; post_stop, delete, post_start, power.stop == 0
def test_check_json_prints_one_object_for_every_outcome(self):
    # Available, Shutdown, unknown state, unpaired, GitHub silent, refused (repository, account, env token),
    # cleanup pending, cleaned, already deleted: stdout parses as one object with that outcome; 0 mutations
def test_check_never_cleans_up_at_any_phase(self):
    # journals at each phase, and a confirmed-deleted paired target with no journal: --check and no terminal -> 0
    # mutations; EOF where a prompt is asked (no journal past confirmed) -> 0 mutations; a journal at confirmed or
    # later in a terminal resumes without asking (CHECK2 N12)
def test_unsafe_never_asks_and_never_deletes(self):    # dirty second worktree; stash: ask not called, 0 calls, exit 1
def test_only_the_exact_word_proceeds(self):  # 'Delete', 'yes', 'delete it', '' -> 0 calls; ' delete ' -> deletes
def test_a_turn_written_just_before_the_check_needs_a_later_cycle(self):
    # a cycle that started before scan one finished does not count -> History UNKNOWN until one with started_at > t1
    # completes; a session written during scan one after an earlier capture -> not SAFE;
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
    # pending -> only that step runs, no API call; a crash after retire finished and before the journal held its
    # archive -> the next run keeps kind 'paired' and finishes recovery, SSH alias and Herdr profile (CHECK2 N4)
def test_rebind_during_cleanup_leaves_the_new_pairing_alone(self):
    # scratch.json names NEW when cleanup starts: keeper/worker/arming/SSH/Herdr untouched, steps 'moved', OLD's
    # archived session terminated
def test_externally_deleted_target_cleans_up_only_after_typing_delete(self)
def test_paired_target_is_never_started(self)          # --start on paired -> refused, post_start 0
def test_start_is_its_own_operation(self):
    # starts, prints the check command, never scans, deletes or cleans up; needs a terminal; a wrong repository, a
    # state other than Shutdown, or a journal at deleted -> no POST (CHECK2 N7)
def test_never_paired_target_is_not_connected(self)    # connect not called; four UNKNOWN 'pair it first'
def test_another_repository_is_refused_before_any_check(self)
def test_a_deleted_journal_resumes_without_an_api_call_and_checks_its_own_account(self)   # CHECK3 M5
def test_inside_a_codespace_or_with_an_env_token_refuses(self)
def test_cleanup_failure_is_pending_and_independent_steps_still_run(self)
def test_paired_cleanup_order_and_disarm_line(self)    # Herdr connections ended after the pairing is retired
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
    pending names are left to `work:status`. Then, when `remote.spaces` has status `ok`, the oldest name in
    `local.retired` that is also a `space.name` in its value (intersect first, then pick the oldest, CHECK2 N13) gets
    `Check and delete NAME` → `work:cs:delete -- --codespace NAME`, which takes the last of the five candidate slots
    (replacing the fifth when all five are used). A seal whose Codespace is not listed is never taken as deleted. Never an action deleting
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
    # real `task` with a stubbed project-sync.sh in disposable layouts (plan check 2l): `task work:cs:delete --
    # --check --json` through the generated root alias reaches .devcontainer/scripts/project-sync.sh with exactly
    # ['delete', '--check', '--json'] (go-task consumes the separator); this repository's root task reaches
    # scripts/project-sync.sh with the same list; both headless, exit 0
# tests/test_continuity_integration.py: the real task inventory test (:56) lists work:cs:delete
# tests/test_standing.py
def test_retired_and_cleanup_facts(self)     # a seal; a cleaned journal hides it; a deleted journal -> cleanup
# tests/test_overview.py
def test_cleanup_pending_comes_first_or_after_recovery(self)
def test_the_oldest_listed_seal_is_offered_even_when_an_older_one_is_unlisted(self)
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

### Task 7: Fixes from the stage C review (Fable 5.2 at 5133a32)

The review `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-c/review-fable/REVIEW.md` (sha256
87a2e96d7a48…) found three false-SAFE cases and more; the lead re-ran every reproduction
(`/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-c/verify/fable-review-5133a32.verification.md`).
The review is evidence; this task is what to build. Its reproduction scripts (`review-fable/repro/`) may be turned
into suite tests.

**Files:** `target_scan.py`, `conversations.py` (capture), `verdict.py` (`_manifest_files` only), `seal.py`,
`restore.py`, `herdr.py`, `workspace.py` (`retire`), and their tests. **Frozen** (Task 5 builds on them in
parallel): the fields of `Scan`, `Evidence`, `Category`, `Verdict`, and the signatures of `verdict.resolve`,
`judge`, `unreachable`, `claims`, `cutoffs`, `sessions_by_file`, `lines`, `as_json`. Only `seal.write` may change
(nothing calls it yet). Task 5 and 6 files are not touched.

One commit per item, in this order, each with a test shown failing first:

- **R1 (P1-1). An unfinished turn on any conversation file blocks, imported or not.** A Claude or Codex file on the
  Codespace that the Mac imported and the Codespace then resumed, with bytes after its last completed turn, reads
  History UNSAFE `a turn that never finished`, naming it, and `seal.write` refuses. Capture records the completed
  prefix (`size`, `sha256`) of every in-scope conversation file in the inbound manifest, unchanged imports included,
  and `verdict._manifest_files` reads those rows as own cutoffs. Mixed versions (spec Constraints): the new rows go
  where `replication.validate_manifest` and every other manifest reader at 5133a32 ignore them, a manifest without
  them reads as today, and they add no blob references; the report names each reader checked. Tests: Claude and
  Codex, real cycles as in `review-fable/repro/repro_import_tail.py`. Message: `Check the tail of every
  conversation file, including one imported from the Mac`.
- **R2 (P1-2, call 17). Repositories inside ignored directories are found.** The walk enters directories Git
  ignores to look for repository markers only, skipping `node_modules`, `.venv` and `__pycache__`, under an entry
  budget of its own (over it: Git incomplete, fixed problem text). The `.git` internals of a repository found inside
  `.scratch` are not scratch files (Mutagen's `--ignore-vcs`, `scratch.py:743`). Rebuild
  `test_every_repository_and_worktree_under_workspaces_is_found`'s `.scratch` repository in the generated layout
  (`.scratch/.gitignore` from `notes._IGNORE`), plus a clone in an ignored `tmp/`. Message: `Find repositories
  inside directories Git ignores`.
- **R3 (P1-3). Changes `git status` hides are counted.** Each index entry flagged skip-worktree or assume-unchanged
  (`git ls-files -v`) whose file exists and whose content (`git hash-object`, never `-w`, filters off as call 16)
  differs from the index counts one change; a file that does not exist does not (sparse checkouts). An operation in
  progress (`rebase-merge`, `rebase-apply`, `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, any `*AUTOSTASH`)
  counts one change. Tests: the review's S3 and S4. Message: `Count edits Git status hides and work left in a
  stopped rebase or merge`.
- **R4 (P2-1). The final flush proves scratch reached the Mac.** `seal.write` takes a required keyword `evidence`
  (the paired `verdict.Evidence` whose `scan` is the scan being sealed) and refuses unless `sync_running`, no
  `conflicts`, and every `scan.scratch` pair is in `mac_scratch`. Update "Left for stage D" to say so. Tests in
  `test_seal.py`. Message: `Seal a final flush only when scratch reached the Mac`.
- **R5 (P2-2). A repeated basename blocks only where it matters.** A repeated (agent, basename) makes conversations
  incomplete only when that key is in the cutoffs map; the review's S1 (two `relay/<date>/checklist.json`) reads
  complete. Message: `Let repeated file names outside the compared conversations pass the scan`.
- **R6 (P2-3). A proxy is recognized from any copy of the package.** `restore._served` matches the bootstrap's
  shape with the package path free, keeps the exact ` NAME STATE` ending, and still rejects CHECK3 M2's
  counterexamples (existing tests). Test: two copies of the package at two paths, one state dir. Message:
  `Recognize this project's Herdr proxy whichever checkout started it`.
- **R7 (P2-5).** `test_scan_changes_nothing` makes a tracked file stat-dirty first; show it failing with
  `GIT_OPTIONAL_LOCKS=0` removed, then restore. Message: `Make the scan's no-write test fail on an index refresh`.
- **R8 (P3s).** A prunable linked worktree (directory gone) is skipped, not unreadable (S5); `refs/stash` that
  resolves counts at least one stash without a reflog (S6); a commit held only by a local tag is claimed (S7);
  `herdr.open_remote` runs `herdr machine add` outside `workspace-control.lock`, then re-reads the binding under the
  lock before its receipt write (existing refusals keep their tests); a `retire-intent.json` left by a crash with a
  newer pairing is resolved or reported with a fixed reason, never blocks every later retire; if a safe resolution
  is not clear, report it instead of guessing. One commit each.

Not here: P2-4 (rescan after the next cycle when the Mac published between the scans) and binding a paired scan to
the manifest's `cutoffs_digest` in `Evidence` go to Task 5, which builds `gather`.

Gates once, at the final commit: `task check && task test`, exit 0.

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
`tests/test_forget.py` (all);
`tests/test_herdr.py::test_missing_machine_uses_official_setup_then_reads_generated_id`,
`::test_a_replaced_codespace_gets_a_fresh_machine_and_retires_the_old_profile`,
`::test_a_codespace_without_herdr_gets_a_fresh_machine_whatever_the_receipt_says` and the `test_prepared_*` tests;
`tests/test_workspace_recovery.py::test_moving_to_another_codespace_drops_its_remote_history_and_keeps_local_history`,
`::test_mixed_legacy_history_preserves_local_scope_without_remote_identity_bypass`,
`::test_changed_verified_local_history_identity_refuses_connection`;
`tests/test_project_sync_cli.py::test_bare_work_rejects_options`, `::test_codespace_stop_runs_without_a_terminal`,
`::test_codespace_restart_still_requires_a_terminal`;
`tests/test_lifecycle.py::test_lifecycle_api_reads_and_posts_only_its_routes` and its 404, inventory and token tests
(an absent or non-boolean `pending_operation` still reads `None`);
`tests/test_ssh_proxy.py::test_an_unreadable_binding_does_not_end_a_live_proxy`, `::test_failed_read_resets_the_down_count`.

## Left for stage D (its plan inherits these)

- Step 3 seals OLD: with OLD still paired and its agents ended, `deletion.gather` for OLD; when the evidence is
  steady, has no tails, and the bracketing cycle's inbound row is complete with nothing pending and no capture
  issues, `seal.write(..., evidence=evidence)` with that cycle's id, its inbound snapshot id and the replace
  operation id. The required paired `verdict.Evidence` must name the very scan being sealed, have `sync_running`,
  no conflicts, and every `scan.scratch` pair in `mac_scratch`. Otherwise the replace stops before the move and says
  why.
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
Before the delete steps, `--check --json` on a real Codespace must show Git complete: any directory under
`/workspaces` the Codespace user cannot read (a volume's `lost+found`, a root-only `.codespaces` folder) makes the
whole Git part incomplete by design, so it would block every delete. If one appears, the lead decides an explicit,
named exception before release; it is not widened in code ahead of that evidence.
