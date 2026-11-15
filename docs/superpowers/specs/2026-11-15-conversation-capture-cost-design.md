# Conversation capture cost: design

Date: 2026-11-15. Base: `865eee5` (`main`). Fix branch: `fix/conversation-capture-cost`.
Every `file:line` below is at `865eee5`, under `devcontainer_setup/project_sync/` unless it says otherwise.

Evidence for the problem: `tmp/codespace-lifecycle/task-work/supervisor-profile/FINDINGS.md`.
Evidence for this design (scripts and their output, outside Git):
`tmp/codespace-lifecycle/task-work/capture-fix/design/` (`scripts/`, `out/`). Paths below that start with
`design/` are in that folder.

The owner settled three fixes (2026-11-15): read the conversation-import receipts once per capture; make
`state._parent`'s close-and-advance safe against an interrupt; measure what is left before deciding on a cache.
This design adds one thing the brief left open: the stage alarm repeats (section 3.3).

## 1. Requirements

Reviews copy these word for word.

- **CR1.** One capture reads each `conversation-imports/*/receipt.json` in its state directory exactly once,
  however many Codex and Claude sessions it visits.
- **CR2.** One capture reads `workspace.json` at most twice.
- **CR3.** A capture reads the receipts only after it has listed every Codex session file and every Claude
  project folder it will visit.
- **CR4.** For the same state directory and the same native files, a capture at the fix commit saves a snapshot
  with the same id as a capture at `865eee5`, and leaves the same `conversation-origins.json`.
- **CR5.** A `BaseException` raised in `state._parent` right after any of its `os.close` calls returns leaves
  the context manager as that exception, with no descriptor closed twice and none left open.
- **CR6.** When the stage alarm is raised right after `state._parent` closes a directory descriptor inside a
  capture's per-session handler, `sync_cycle.stage` raises `TimeoutError('Conversation stage timed out')`.
- **CR7.** A `BaseException` raised in `env_files._read_no_symlink` right after any of its `os.close` calls
  returns leaves the function as that exception, with no descriptor closed twice and none left open.
- **CR8.** After a stage's deadline passes, the stage's alarm is raised again every `STAGE_RETRY` seconds (60)
  until the stage ends; leaving the stage disarms the timer and restores the previous handler.

## 2. What was measured

All figures come from the product's own `replication.capture`, called as `sync_project_data` calls it
(`replication.py:315`), on APFS clones of the project state. "Prototype" is this design applied to a scratch copy
of the package (`design/new/`; its diff is `design/out/prototype.diff`, a reference and not the specification);
the builder's code must be measured again (section 5). Loads are the 1-minute load average on this 10-core Mac
when the run started. The prototype also passes the existing tests of `test_native_checkpoints`,
`test_native_publication`, `test_replicated_imports`, `test_replication`, `test_checkpoint_store`, `test_forget`,
`test_project_state`, `test_native_formats`, `test_session_catalog`, `test_native_memory`, `test_env_files` and
`test_ssh_proxy` (283 tests, run in a scratch copy of the tree). Those are every test module that names
`_started_here`, `STAGE_TIMEOUT` or `SIGALRM`, apart from the `render_screens.py` helper.

| Run | Code | CPU | Wall | Receipt reads | `workspace.json` reads | Snapshot |
|---|---|---|---|---|---|---|
| Frozen inputs, strata-mf (586 Codex files, 29 Claude files, 255 receipts), load 22 | `865eee5` | 251.6 s | 289.1 s | 266,063 | 999 | `4e6f77e9…` |
| Same inputs, load 22 | prototype | 26.2 s | 27.3 s | 255 | 2 | `4e6f77e9…` |
| Live homes, strata-mf (4,940 Codex files), load 21 | prototype | 31.8 s | 34.7 s | 255 | 2 | |
| Live homes, strata-mf, inside the real `sync_cycle.stage`, load 24 | prototype | 31.7 s | 34.4 s | 255 | 2 | |
| Live homes, innovate (4,940 Codex files, 255 receipts), load 20 | prototype | 31.7 s | 34.5 s | 255 | 2 | |
| Live homes, strata-mf (FINDINGS) | `865eee5` | over 1,013 s | never finished in 1,200 s | 1,058,713 | | |

Commands: `design/scripts/build_frozen.py` built the frozen inputs; `design/scripts/run_capture.py` made every
row (`design/out/eq-old.json`, `eq-new.json`, `new-live-2.json`, `new-live-3-stage.json`,
`new-live-innovate.json`). In both frozen runs 109 calls of `_receipt_scopes` granted a root beyond the project
root, 76 entries and 371 issues were saved, and the two state clones ended with the same
`conversation-origins.json` (`cmp`) and the same object list.

Where the prototype's 31.8 s goes on strata-mf (`design/out/new-live-2.json`, CPU by function):

| Part | CPU | Share |
|---|---|---|
| Validating the project's own Claude sessions again (`_claude_entry`, 28 files, 194 MB) | 17.1 s | 54% |
| Validating the project's own Codex sessions again (`_codex_entry`, 419 calls) | 5.7 s | 18% |
| Looping over the 255 receipts in memory for each Codex first line (`_receipt_scopes`, 4,444 calls) | 5.2 s | 16% |
| Everything else: the index walk (0.09 s), 4,940 first-line reads, the store | 3.9 s | 12% |

On innovate the first part is 27.1 s of 31.7 s (35 Claude files) and the receipt loop 0.8 s. So after fix 1 the
first-line reads the brief names are the small part. The large part is that every cycle parses and validates
every session of the project again, changed or not. Section 5 turns this into the lead's decision.

## 3. Design

### 3.1 Fix 1: read the receipts once per capture

**Today.** One capture reads the receipts at four kinds of place in `conversations.py`:

| Reader | Where | Times per capture | If a receipt cannot be read |
|---|---|---|---|
| `_created_imports` → `_publication_receipts` (`:410`, `:1012`) | `:472`, before any file, outside every handler | 1 | the capture fails |
| `_receipt_scopes` (`:186`, glob and `read_json` at `:190-191`) | per Codex first line `:527`, per Codex file validated `:274`, per Claude file `:326`; inside the per-session handlers `:535`, `:557` | one per file | a session issue |
| `_unchanged_import` → `_publication_receipts` (`:1022`) | `keep`, `:483`, per kept entry, outside the handlers | one per entry | the capture fails |
| `_import_aliases` (`:400`) | `:540`, before the Claude loop | 1 | the capture fails |

`_receipt_scopes` also reads `workspace.json` on every call (`_scopes(project, state)` at `:187`).
Because `:472` reads every receipt first and outside any handler, an unreadable receipt already fails the whole
capture before a session is visited. The per-session outcome in the second row cannot happen on a state that
does not change during the capture.

**After.** `capture_conversations` (`:467`) does this, in this order:

1. `scopes = _scopes(project, state, historical_roots)` as today (`:469`), then
   `destination_roots = _scopes(project, state)`. They differ only when `historical_roots` is given, which is
   the Codespace side (`rpc.py:58-69`). `_receipt_scopes` uses the second one today (`:187`) and must keep
   doing so: it bounds where an import may land (`:93`, `:1052`).
2. The herd record and the Codex index, as today (`:492-503`).
3. **New:** list the Claude files now. For each root in `scopes`, in order, keep the folder and either `None`
   (the folder is a symlink) or `list(directory.glob('*.jsonl'))`.
4. **New:** `receipts = _import_receipts(project, state)`, the only receipt read of the capture. Then
   `imported = _created_imports(receipts)` (moved here from `:472`).
5. The Codex loop as today, passing `receipts` and `destination_roots` down.
6. `aliases = _import_aliases(receipts)`, then the Claude loop over the lists from step 3. A `None` raises the
   same `Symlink native project store` issue at the same position as `:543-545`.
7. `_record_origins` and the sort, as today.

Functions (all in `conversations.py`):

| Function | New signature | Change |
|---|---|---|
| `_import_receipts` | `(project, state) -> tuple[dict, ...]` | New. `()` when `state is None`. Otherwise `read_json` of each `conversation-imports/*/receipt.json` in glob order, keeping those whose `project_id` and `identity` are this project's. A read error propagates. |
| `_receipt_scopes` | `(project, receipts, destination_roots, path, data, store)` | Loops over `receipts`. No `_scopes` call, no glob, no `read_json`. The loop body from `:194` to `:212` is unchanged. |
| `_codex_entry` | `(project, store, sid, index, *, scopes, receipts, destination_roots)` | Passes them to `_receipt_scopes` (`:274`). `state` is dropped. |
| `_claude_entry` | `(project, store, sid, path, *, scopes, receipts, destination_roots)` | Same (`:326`). |
| `_import_aliases` | `(receipts)` | Keeps the `not receipt.get('publication')` test. |
| `_created_imports` | `(receipts)` | Keeps only receipts with `publication`. |
| `_unchanged_import` | `(receipts, retained, entry)` | Skips receipts without `publication`; the rest of the body is unchanged. |
| `_publication_receipts` | `(project, state)` unchanged | Becomes the `publication` receipts of `_import_receipts(project, state)`. Its one remaining caller, `_owned_staging` (`:1158`), reads all of them anyway. |
| `_started_here` | unchanged | Calls `_created_imports(_import_receipts(project, state))` at `:464`. |

The receipts are shared between calls, so nothing may change them. Nothing does today: `_claude_child_scopes`
copies before it edits (`:150`), and `_preflight` only reads.

Publication (`publish_conversations`, `_owned_staging`, `_supersede_imports`, `_started_here`) keeps reading
receipts per entry. That is outside this round; see section 7.

**Call: fix 1 preserves behavior, and the order in step 3 and 4 is what keeps it so.**

*Who can write a receipt while a capture runs.* Receipts are written only by `_publish_codex` (`:938-939`,
`:989`) and `_publish_claude` (`:1093`, `:1097`), reached only through `publish_conversations` (`:1122`). That
has two callers: `sync_project_data` (`replication.py:336`) and the remote `publish` operation (`rpc.py:141`).

- On the Mac, nothing can. `sync_project_data` is called only by the supervisor (`scratch.py:778`), which holds
  `scratch-supervisor.lock` (`scratch.py:654`) and runs capture, transfer and publication one after another on
  its main thread. The CLI (`task work`) never runs a cycle itself. Another project's supervisor has another
  state directory. Capture takes no receipt lock; `conversation-recovery.lock` (`:854`) only orders publishers.
- On the Codespace, it can. Each remote operation is its own process, started over SSH with a 180-second limit
  (`rpc.py:189`, `transport.py:450`). When the Mac gives up on a `publish`, it kills its local `ssh`; the remote
  process has already read its input and keeps publishing. The same cycle then starts the remote `capture`
  (`replication.py:316`). The fix runs there too, because every call ships the modules (`rpc.py:182`).

*What reading once changes there.* Today each file is judged with receipts read at that moment. Read once at
`:472`, a Claude import published while the capture reads its Codex sessions is found by the Claude loop without
its receipt, passes as an ordinary session, and is sent back to the Mac as a new conversation. Shown through the
real code: with the read placed first, `test_a_claude_import_that_lands_while_codex_sessions_are_read_is_not_sent_back`
fails with the import in `entries`; `865eee5` passes it.

*Why listing first is enough.* A publication records a file in its receipt before the file gets its name:
`_create` calls `journal` at `:791` and publishes the name at `:793`; a Claude receipt already carries its
`baselines` then (`:1084`, `:1093`). So every file the capture lists was already in a receipt when the capture
reads the receipts afterwards.

*What remains, and is accepted.* A Codex receipt gets its `baselines` only at `:976-977`. For the fork that is
just after its name appears (`:973`). For an ancestor the publication creates (`:940-964`) it is after every
remaining file of that publication is written. A capture that lists and reads inside that gap sees the file and
its receipt but no baseline, and sends the file back. `865eee5` has the same gap but reads later, at `keep`
(`:483`), so its gap is narrower. Closing it means writing the baseline with the file's journal entry, which is
publication code and not in this round.

*What else changes.* Three small things, all accepted:

- A Claude file that appears while the Codex sessions are read is captured by the next capture, not this one.
- A Claude file removed in that time becomes a session issue for one capture, as a removed Codex file already
  does.
- An import that lands between `:472` and the listing is no longer recorded as started here. `865eee5` reads
  `imported` before it lists, so it records such an import in `conversation-origins.json` (Task 3's test 2 fails
  there on exactly that). This one is a correction.

**Call: fix 1's scope is the one read, `workspace.json`, and all four readers; no index.**
`_unchanged_import` and `_import_aliases` must share the read or the cost stays entries times receipts (11,318
and 255 reads in the frozen run). Passing `destination_roots` down removes 997 of 999 `workspace.json` reads.
`_claude_child_scopes` stays per receipt per file: it returns at once for a file a receipt does not name. What is
then left of "files times receipts" is the in-memory loop, 5.2 s on strata-mf and 0.8 s on innovate. An index by
file name would remove it, but it changes which receipts are evaluated for a file, so a malformed receipt would
fail differently, and the cache section 5 asks the lead about would remove the same loop. Not in this round.

### 3.2 Fix 2: hand over the descriptor before closing

**The defect.** `state._parent` (`state.py:171-232`) walks with one descriptor in `fd`. At `:228-229` it closes
the old one and then rebinds `fd`. An exception raised by a signal handler between the two (the handler runs as
`os.close` returns) reaches `finally` at `:231-232` with `fd` still naming the closed descriptor. The second
`os.close` raises `OSError` EBADF, which replaces the exception in flight. Two exceptions arrive this way on the
supervisor's main thread: `_StageExpired` (`sync_cycle.py:55`, `:68-69`) and `_Shutdown` (`scratch.py:369`,
`:649-651`). Both are `BaseException` so that per-session handlers cannot swallow them; as `OSError` they are
swallowed (`conversations.py:535`, `:557`; `checkpoint_store.py:302`).

**The fix.**

```python
            previous, fd = fd, next_fd
            os.close(previous)
```

`finally` then always closes a descriptor that is open and that nothing else closed. Swallowing `OSError` in
`finally` instead is not acceptable: the supervisor has other threads (`links.py:525`, `transport.py:183`), and
closing a number twice can close a descriptor one of them just opened.

What stays true after the fix, and is not claimed by CR5: an interrupt that lands as `os.open` returns, before
its result is bound, leaves that one descriptor open. Python gives no way to close that gap. It replaces no
exception.

`env_files._read_no_symlink` (`env_files.py:348-381`) has the same shape (`:361-362`, `finally` at `:381`) and
gets the same two lines. Ctrl-C can land there in the CLI, and its callers turn the resulting `OSError` into a
recorded failure (`env_files.py:470`, `:483`).

**Call: fix 2's scope.** `design/scripts/sweep.py` reads every module with `ast` and lists four shapes
(`design/out/sweep-865eee5.txt`); the rows marked `[capture]` are functions the profiled capture ran.

| Site | Shape | Decision |
|---|---|---|
| `state.py:228` `_parent` | close, then rebind, under `finally` | Fix (CR5, CR6). |
| `env_files.py:361` `_read_no_symlink` | same | Fix (CR7). |
| `env_files.py:184-185` `place` | same, inside the `REMOTE` script text (`:32-230`), so `ast` does not see it | Ruled out. Its `finally` swallows `OSError` (`:196-199`), so nothing is replaced, and the script is one short-lived process with one thread and no handler that raises. |
| `finally` that returns, breaks or continues | would drop any exception | None in the package. |
| `except BaseException` (`state.py:225`, `agents.py:36`, `links.py:197`, `probe.py:160`, `signins.py:216`, `transport.py:344`) | could swallow | All end in a bare `raise`. |
| `finally` that closes the one descriptor it opened: `conversations.py:48`, `:686`, `:787`, `:1225`; `state.py:302`; `lifecycle.py:111`; `env_files.py:376` | a close that could raise | Ruled out: one owner, bound once, closed once. |
| `state.py:300` `locked`, `sync_cycle.py:78` `stage` | calls in `finally` | Ruled out: `flock` on an open descriptor; the timer calls run only on the main thread (`sync_cycle.py:66`). |
| A handler's exception raised inside a finalizer | the interpreter prints "Exception ignored" and drops it | Cannot be fixed at a site. Shown by `design/scripts/finalizer_probe.py`: the body "ran on to its end". Section 3.3 answers it. |

On the capture path (`capture_conversations` and what it calls in `sessions.py`, `state.py`,
`checkpoint_store.py`, `forget.py`), no other handler catches wider than `ValueError`, `OSError`, `KeyError`,
`TypeError` or `UnicodeError`, so after the fix nothing there turns a `BaseException` into one of those.

### 3.3 Lost-alarm defense

**Call: fixing the double close is not enough on its own; the second guard belongs in the timer, not in the
capture loop.**

- The timer is one-shot (`sync_cycle.py:71`). One lost delivery, by any route, leaves the stage unbounded. The
  finalizer route above cannot be closed at a site, and nothing stops a later change from adding another.
- A per-file deadline check in `capture_conversations` would guard one loop of several that have per-item
  handlers (`checkpoint_store.py:302`, `conversations.py:1150`), would need the deadline passed into code that
  also runs with no stage (the remote `capture`), and would guard a pass that now takes about 35 seconds of a
  240-second limit. It is a guard at one symptom.
- The source is the timer. `stage` arms it with an interval, so an expiry that did not end the stage is raised
  again:

```python
# An expiry that does not end the stage is raised again this often. Longer than any
# cleanup the first one starts (closing the Codex client can take 19 seconds).
STAGE_RETRY = 60
...
        previous_timer = signal.setitimer(signal.ITIMER_REAL, STAGE_TIMEOUT, STAGE_RETRY)
```

`stage`'s `finally` already disarms the timer and restores the handler (`sync_cycle.py:79-82`). The interval is
longer than the longest cleanup an expiry starts (`agents.py:70-85`: 5 + 5 + 5 + 2 + 2 seconds), so a second
expiry reaches only a stage that is still stuck a minute after its deadline. A second expiry during a slow
cleanup still releases a `state.locked` lock (probed: `design/scripts/proto_tests.py`,
`test_a_second_expiry_during_cleanup_still_releases_a_state_lock`).

`_Shutdown` needs no such guard: its handler sets `exiting` before it raises (`scratch.py:650`), and the
supervisor's loops check it.

### 3.4 Equivalence proof (CR4)

Snapshot ids are content hashes of the manifest (`checkpoint_store.py:83-84`, `:124-127`), and the manifest holds every
entry, issue and completed file. Equal ids from the two commits on the same inputs prove equal output.

Two things make the comparison real, and both were checked through the code:

- **The inputs must not move.** The live homes change between runs (a third live run gave a different id).
  `build_frozen.py` clones a subset of `~/.codex/sessions` with `cp -c -p` and the project's Claude folder with
  `cp -c -R -p`; clones keep `st_nlink == 1` and the modification time the manifest records.
- **The receipts must still match the files.** Receipts hold absolute paths (`:203`, `:532`, `:548`). In a
  relocated home none would match, and the two commits would agree only on code that did nothing.
  `build_frozen.py` rewrites the home prefix in every receipt of the state clone, and `run_capture.py` reports
  `granted`, the number of `_receipt_scopes` calls that returned more than the project root. It must be above
  zero and equal in both runs (109 in the measured pair).

Each run gets its own clone of the rewritten state, because a capture writes objects, the origin list and the
relay seed. The runs are compared on `snapshot_id`, on `granted`, and with `cmp` on `conversation-origins.json`.

The frozen inputs are the Mac side. They cannot show `scopes` and `destination_roots` being mixed up, which
differ only with `historical_roots`. `test_recheckpointed_codex_child_carries_validated_ancestor_scope_union`
and `ContinuedClaudeTests` in `tests/test_native_checkpoints.py` cover the receipt branches with other roots and
must pass unchanged.

## 4. Build plan

> **For the builder:** one Opus builder, in the worktree and on the branch the lead's brief names. Work task by
> task with superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** make a Mac-side capture read its receipts once and make a stage deadline impossible to lose in
`state._parent`, without changing what a capture saves.

**Architecture:** three small changes in three modules. `conversations.py` reads receipts into one tuple and
passes it down. `state.py` and `env_files.py` hand a descriptor over before closing the old one.
`sync_cycle.py` arms the stage timer with an interval.

**Tech stack:** Python standard library, `unittest`. No new files except tests added to existing test modules.

**Spec:** this document, sections 1 to 3.

### Global constraints

- Product files touched: `conversations.py`, `state.py`, `env_files.py`, `sync_cycle.py`. Nothing else.
- Publication code (`publish_conversations`, `_owned_staging`, `_supersede_imports`) changes only where the
  table in 3.1 says.
- No cache, no index of receipts, no deadline check in the capture loops.
- Existing tests pass unchanged. A test that needs editing is a finding: write it to `BLOCKED.md`.
- Tests run by discovery, `python3 -m unittest discover -s tests -p '<file>'`: several modules import
  `test_native_checkpoints` by its bare name.
- Gates follow `tmp/codespace-lifecycle/task-work/stage-a/RULING-gates.md`: default environment, base `865eee5`.
- One commit per task, imperative sentence case, no AI attribution.
- Red proof for each new test: show it failing with its change removed, and keep the output for the report.

### Review focus

Conditions the requirements imply that are most likely to bite, each pinned by a test below:

1. A publication on the Codespace that outlives its caller and overlaps the next capture (Task 3, tests 2, 3).
2. A session of another project among thousands: it must cost no receipt read (Task 3, test 1).
3. A stop request (`_Shutdown`) landing in the same window as the alarm (Task 1, test 1 uses a plain
   `BaseException`, so it covers both).
4. The walk refusing a directory: its other close (`state.py:226`) must behave the same (Task 1, test 2).
5. A stage that swallowed its first expiry (Task 2).

### Task 1: descriptor handover (CR5, CR6, CR7)

**Files:**
- Modify: `devcontainer_setup/project_sync/state.py:228-229`, `devcontainer_setup/project_sync/env_files.py:361-362`
- Test: `tests/test_project_state.py`, `tests/test_native_checkpoints.py`, `tests/test_env_files.py`

- [ ] **Step 1: write the failing tests.** All four use this shape: replace `os.open` and `os.close` with
  wrappers that call the real ones, track which descriptors are open, and raise after the Nth close.

```python
class Interrupt(BaseException):
    """Stands for the stage alarm or a stop request: raised by a signal handler, not by the code."""

def interrupted(action, interrupt_after_close):
    """Run action(); raise Interrupt as a handler would, right after the Nth os.close returns."""
    real_open, real_close = os.open, os.close
    opened, twice, closes = set(), [], 0
    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.add(fd)
        return fd
    def tracking_close(fd):
        nonlocal closes
        if fd in opened:
            opened.discard(fd)
        else:
            twice.append(fd)
        real_close(fd)
        closes += 1
        if closes == interrupt_after_close:
            raise Interrupt()
    raised = None
    with patch.object(os, 'open', tracking_open), patch.object(os, 'close', tracking_close):
        try:
            action()
        except BaseException as error:
            raised = error
    return raised, twice, opened, closes
```

  1. `tests/test_project_state.py`,
     `test_an_interrupt_after_any_close_in_the_walk_survives_and_no_descriptor_is_closed_twice`: the action is
     `with _parent(directory / 'record.json'): pass` for a file three directories below a temporary directory.
     With `interrupt_after_close=0`: `raised is None`, `closes > 3`. Then for every `number` in
     `range(1, closes + 1)`, as a `subTest`: `assertIsInstance(raised, Interrupt)`, `twice == []`,
     `opened == set()`.
  2. Same file, `test_an_interrupt_after_the_close_of_a_refused_directory_survives`: the path is
     `<tmp>/devcontainer-setup/projects/<24 zeros>/record.json` with that last directory at mode `0o755`, walked
     with `private=True`. With `0`: `assertIsInstance(raised, PermissionError)`, `twice == []`,
     `opened == set()`. Then the same three assertions as test 1 for every close.
  3. `tests/test_env_files.py`, `test_an_interrupt_after_any_close_while_reading_an_env_file_survives`: the
     action is `env_files._read_no_symlink(root, 'a/b/.env')`. With `0` it returns the file's bytes and
     `closes == 4`. Then the same three assertions for every close.
  4. `tests/test_native_checkpoints.py`, in `NativeCheckpointTests`,
     `test_the_stage_deadline_landing_on_the_walk_handover_ends_the_capture_as_a_timeout`:

```python
        path = self.codex()
        real_close, real_safe, armed, fired = os.close, sessions._safe, [], []
        def arming_safe(candidate):
            if Path(candidate) == path:
                armed.append(True)   # the capture is now inside its per-session handler
            return real_safe(candidate)
        def alarm_after_close(fd):
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            real_close(fd)
            if armed and directory and not fired:
                fired.append(True)
                signal.raise_signal(signal.SIGALRM)   # the handler runs as this call returns
        cycle = sync_cycle.begin(self.project, str(self.root))
        with patch.object(sessions, '_safe', arming_safe), patch.object(os, 'close', alarm_after_close):
            with self.assertRaisesRegex(TimeoutError, 'timed out'):
                with sync_cycle.stage(cycle, 'desktop_to_codespace:capture', None):
                    self.api.capture_conversations(self.project, self.store, state=self.root / 'state', env=self.env)
        self.assertEqual(fired, [True])
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))
```

- [ ] **Step 2: run them and see them fail.** Expected at `865eee5`: tests 1 to 3 fail at `close=1` with
  `OSError(9, 'Bad file descriptor') is not an instance of <class '…Interrupt'>`; test 4 fails with
  `TimeoutError not raised`. (Seen on the prototype's tests: `design/scripts/proto_tests.py`, `proto_walk2.py`,
  `proto_env.py`.)
- [ ] **Step 3: change `state.py:228-229` and `env_files.py:361-362`** to the two lines in 3.2, each with a
  comment saying why the order matters. Do not touch `place` in `REMOTE`.
- [ ] **Step 4: run the three test files; all pass.**
- [ ] **Step 5: commit** (`Hand a walk's descriptor over before closing the old one`).

### Task 2: the stage alarm repeats (CR8)

**Files:**
- Modify: `devcontainer_setup/project_sync/sync_cycle.py:17`, `:71`
- Test: `tests/test_replication.py`, beside `test_stage_timeout_interrupts_work_and_restores_signal_handler`

- [ ] **Step 1: write the failing test**, `test_an_expiry_that_does_not_end_the_stage_is_raised_again`:

```python
        before = signal.getsignal(signal.SIGALRM)
        cycle = sync_cycle.begin(self.project, str(self.root))
        swallowed = []
        with patch.object(sync_cycle, 'STAGE_TIMEOUT', .02), patch.object(sync_cycle, 'STAGE_RETRY', 1.0):
            with self.assertRaisesRegex(TimeoutError, 'timed out'):
                with sync_cycle.stage(cycle, 'capture', None):
                    try:
                        threading.Event().wait(5)
                    except BaseException:
                        swallowed.append(signal.getitimer(signal.ITIMER_REAL))   # the first expiry is lost here
                    threading.Event().wait(5)
        self.assertEqual(len(swallowed), 1)
        self.assertGreater(swallowed[0][0], 0)
        self.assertEqual(swallowed[0][1], 1.0)
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))
```

- [ ] **Step 2: add `STAGE_RETRY = 60` with its comment, run the test, see it fail** with
  `TimeoutError not raised` after about 5 seconds (the red proof: the constant exists, the timer is still
  one-shot).
- [ ] **Step 3: pass `STAGE_RETRY` as the interval at `:71`.**
- [ ] **Step 4: run `tests/test_replication.py`; all pass,** the existing stage test included.
- [ ] **Step 5: commit** (`Raise a stage's expiry again until the stage ends`).

### Task 3: one receipt read per capture (CR1, CR2, CR3)

**Files:**
- Modify: `devcontainer_setup/project_sync/conversations.py` (`:186-213`, `:250`, `:274`, `:311`, `:326`,
  `:400-414`, `:464`, `:467-564`, `:1012-1035`)
- Test: `tests/test_native_checkpoints.py`, a new `ReceiptReadTests(NativeFixture)`

**Interfaces:** the table in 3.1. `design/scripts/run_capture.py` wraps `_receipt_scopes`, `_codex_entry`,
`_claude_entry`, `_unchanged_import`, `_finished_workers` and `_record_origins` by name; keep those names.

- [ ] **Step 1: write the tests.** A helper builds what a finished Codex publication leaves:

```python
    def codex_import(self, state, sid):
        path = self.codex(sid)
        data = path.read_bytes()
        receipt = state / 'conversation-imports' / hashlib.sha256(sid.encode()).hexdigest() / 'receipt.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps(dict(
            publication=True, schema=1, status='complete', agent='codex', project_id=self.project.project_id,
            identity=self.project.identity, destination_root=str(self.project.root), source_session_id=str(uuid.uuid4()),
            baselines=[dict(agent='codex', session_id=sid, turn_id=self.turn)],
            files=[dict(destination=str(path), created=True, native_fork=True, session_id=sid, size=len(data),
                        sha256=hashlib.sha256(data).hexdigest(), scope_roots=[str(self.project.root)])])))
        return path
```

  1. `test_a_capture_reads_each_receipt_once_however_many_sessions_it_visits` (CR1, CR2): two Codex sessions of
     the project, one Codex session whose first row names another directory, one Claude session, and three
     `codex_import` receipts. Wrap `self.api.read_json` to record each path. Assert: `issues == []`; the
     entries are the two Codex sessions and the Claude session; the recorded paths named `receipt.json`, sorted,
     equal the sorted glob of the three receipts; at most two recorded paths are named `workspace.json`.
  2. `test_an_import_that_lands_just_before_the_listing_is_seen_with_its_receipt` (CR3): wrap
     `sessions._codex_index` so it first calls `self.codex_import(state, imported)` and then the real index.
     Assert: `issues == []`, `entries == []`, and `self.api._conversation_origins(state)` is
     `dict(claude=set(), codex=set())`.
  3. `test_a_claude_import_that_lands_while_codex_sessions_are_read_is_not_sent_back` (CR3): one Claude session
     and one Codex session with different ids; capture once and take the Claude entry. Wrap `self.api._codex_entry` so its first
     call first runs `self.api._publish_conversation(self.project, self.store, entry, state=state, env=self.env)`.
     Capture again: `issues == []` and the published `session_id` is not among the entries. Capture a third
     time: the same two assertions.

- [ ] **Step 2: run them at the base.** Expected at `865eee5`: test 1 fails (`Lists differ`: each receipt read
  many times); test 2 fails on the origin list (`'codex': {<sid>}`); test 3 passes, because the old code reads
  receipts again for every file.
- [ ] **Step 3: implement 3.1**, steps 1 to 7 and the function table.
- [ ] **Step 4: run the new tests; all pass.** Then the red proof for CR3: move the `_import_receipts` call
  above the Codex index and iterate `directory.glob('*.jsonl')` inside the Claude loop again. Test 2 must fail
  on `entries == []` and test 3 with the published id `unexpectedly found`. Restore, and keep both outputs.
- [ ] **Step 5: run** `test_native_checkpoints.py`, `test_native_publication.py`, `test_replicated_imports.py`,
  `test_replication.py`, `test_checkpoint_store.py`, `test_forget.py` by discovery; all pass unchanged.
- [ ] **Step 6: commit** (`Read the import receipts once per capture`).

### Task 4: gates

- [ ] `task check`, then `task test` once, in the default environment, at the last commit. Report the exit
  codes, the red proofs, and the commits.

## 5. Measurement for fix 3

Run after the build, on the fix branch, before any rollout. It needs no Codespace and does not touch the live
state. `<repo>` is the fix worktree; `<id>` is the project's state id (strata-mf `d0a797e7353981afbf57f032`,
innovate `b93a9cad02201713044b771d`).

```sh
V=/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/capture-fix/verify
S=/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/capture-fix/design/scripts
mkdir -p $V/state-m/devcontainer-setup/projects
cp -cR ~/.local/state/devcontainer-setup/projects/<id> $V/state-m/devcontainer-setup/projects/   # "is a socket (not copied)" is expected
chmod 700 $V/state-m/devcontainer-setup $V/state-m/devcontainer-setup/projects
python3 -I $S/run_capture.py <repo> <project root> $V/state-m/devcontainer-setup/projects/<id> $V/measure-<project>-1.json --stage
python3 -I $S/run_capture.py <repo> <project root> $V/state-m/devcontainer-setup/projects/<id> $V/measure-<project>-2.json --stage
rm -rf $V/state-m
```

It runs the real capture inside the real `sync_cycle.stage` and reports: `outcome` (`complete`, or the stage's
timeout), `cpu_s`, `wall_s`, the load before and after, `read_json_by_caller` (the receipt reads must equal the
number of receipts), and `cpu_s_by_function` with `calls`, which give the split in section 2. The second run is
the steady state: nothing new to store.

**What the lead decides.** A cycle starts 5 seconds after the last one ends (`scratch.py:790`), so a capture of
`C` CPU-seconds in a cycle of `W` wall-seconds holds `C / (W + 5)` of a core for as long as the Codespace is
connected. The other stages took about 88 seconds on strata-mf (`stages.log`, 12:14:33 to 12:15:55, plus
verify).

- **Figure:** plan a cache when the second run costs more than **5 CPU-seconds** on either project. That is
  about 5% of a core per project at the cycle length seen, against 60 to 70% today. The figure is this design's
  proposal; the lead rules on it.
- **Expected:** about 32 CPU-seconds on both projects, a quarter of a core each. So expect to plan the cache.
  A quarter is the floor: the 88 seconds were measured while the Codespace ran the old capture, and the same
  fix ships there, so the cycle gets shorter and the share larger.
- **What a cache could save,** from the split: skipping sessions whose file is unchanged removes the two
  validation rows (22.7 s on strata-mf, 27.1 s on innovate); skipping unchanged first lines removes most of the
  other two (9.1 s on strata-mf). The cache's design is the next round's, not this one's.
- **The other lever:** the 5-second pause (`scratch.py:790`). A longer pause lowers the share in proportion and
  delays every conversation by as much.
- **Growth:** `~/.codex/sessions` has gained between 500 and 1,800 files a month lately (counted from file
  names). The first-line rows grow with it; the validation rows grow with the project's own sessions.

## 6. Done means

The lead runs these at the builder's last commit, before merging. Each is logged under
`tmp/codespace-lifecycle/task-work/capture-fix/verify/`.

1. **Gates.** `task check` and `task test` in the default environment, logged with their exit codes. Only a zero
   exit accepts the branch.
2. **Red proofs.** With `state.py`'s two lines reverted, Task 1's tests 1, 2 and 4 fail as section 4 says; with
   them back, they pass.
3. **Same output (CR4).** Build the frozen inputs once, then run both commits on their own clone:

```sh
python3 -I $S/build_frozen.py <repo> <project root> $V/state-frozen/devcontainer-setup/projects/<id> $V/frozen --others 150
# clone $V/state-frozen twice with cp -cR (state-old, state-new); chmod 700 as in section 5
python3 -I $S/run_capture.py <checkout of 865eee5> <project root> <state-old> $V/eq-old.json --codex-home $V/frozen/codex --claude-home $V/frozen/claude
python3 -I $S/run_capture.py <repo>                <project root> <state-new> $V/eq-new.json --codex-home $V/frozen/codex --claude-home $V/frozen/claude
cmp <state-old>/conversation-origins.json <state-new>/conversation-origins.json
```

   `$V/state-frozen/…/<id>` is a fresh `cp -cR` clone of the live state, made as in section 5 before
   `build_frozen.py` runs. Pass: equal `snapshot_id`; equal `granted`, above zero; `cmp` silent; receipt reads
   equal to the receipt count in the new run. The old run takes about five minutes.
4. **Inside the limit.** The first command of section 5 ends with `outcome: complete` on both projects.
5. **Fix 3.** The section 5 figures are recorded and the lead's ruling on the cache is written down.
6. **Clean.** Every clone under `verify/` is deleted.

The projects get the fix only through the rollout, which waits for the owner. Nothing above depends on it.

## 7. Seen, not in this round

- **Publication reads receipts per entry.** `_supersede_imports` globs and reads every receipt for each entry
  (`conversations.py:1238-1241`, called at `:1148`), `_owned_staging` for each Codex ancestor (`:1158`), and
  `_started_here` reads them and walks the whole Codex index for each relayed entry (`:462-464`). The live
  publication stages took 10 to 23 seconds (`stages.log`), inside the limit.
- **An alarm that lands in `stage`'s own exit.** Between the body's end and the disarm at `sync_cycle.py:79`,
  an expiry leaves `stage` as `_StageExpired`, which nothing in the supervisor catches. The window is a few
  instructions wide and exists today.
