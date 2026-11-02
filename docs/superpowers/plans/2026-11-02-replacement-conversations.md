# A Replacement Codespace Gets Its Predecessor's Conversations: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> In this repo the lead runs it through the herd instead (owner ruling 20): Codex xhigh builders in worktrees, the
> lead reviews the diff and re-gates in the default environment.

**Goal:** After a replacement's first completed sync cycle, the new Codespace lists every conversation its
predecessors delivered to the Mac, and no Codespace ever gets back a conversation that started on it.

**Architecture:** The Mac's checkpoint store keeps a record of every conversation entry it acknowledges from a
Codespace (retired only by an append proof). `replication.capture` adds those records to the Desktop → Codespace
manifest, marked `relayed`. On publish, a relayed entry whose session started on the destination is reported
`unchanged` and nothing is written; every other entry publishes exactly as today.

**Tech Stack:** Python 3 standard library, `unittest`, go-task.

**Spec:** `docs/superpowers/specs/2026-11-02-replacement-conversations-design.md` (calls 1–6, "How it works").
Analysis: `tmp/codespace-lifecycle/sync-fixes/analysis/REPORT.md` and its `evidence/red-tests.log` (gitignored;
the lead has them).

## Global Constraints

- Only entries acknowledged with `source='codespace'` are recorded. A store acknowledging `source='desktop'`
  (a Codespace) records nothing.
- A record retires only when a later received entry for the same `(agent, session_id)` has a head blob whose
  bytes start with the record's head blob bytes. Never by length, age, publication or refusal.
- The head file of an entry is the `files` item whose `session_id` equals the entry's `session_id`.
- Relayed entries carry `"relayed": true`. No other manifest field changes; schema stays 2.
- The started-here check applies only to entries with `relayed` true. Entries without it publish exactly as
  today (Codex ancestors keep their original ids, `conversations.py` `_publish_codex`).
- Never take `conversation-recovery.lock` while holding `checkpoints/store.lock`.
- Files written: mode 600 under the project state dir, via `state.atomic_write`.
- No existing test assertion changes, except a count that grows by exactly the relayed entries; each such change
  is listed in the task's report with the reason. No test is removed or skipped.
- Commit messages are plain imperative sentences with no AI attribution. Gates: `task check` and `task test`.

## Shared contract

- Record file: `<state>/checkpoints/relay/<sha256 of the canonical entry JSON>.json`, content
  `{"schema": 1, "entry": <entry as received>, "received_at": <ISO-8601 UTC>}`. Seeding marker:
  `<state>/checkpoints/relay/seeded`.
- Started-here list: `<state>/conversation-origins.json`, content
  `{"schema": 1, "sessions": {"claude": [<sid>, ...], "codex": [<sid>, ...]}}`, sorted, only ever grows.
- Relay issue reason (exact): `Relayed conversation checkpoint is unreadable`.

## Review Focus

1. A Codespace's own conversation relayed back while it is still the bound Codespace, on every cycle: nothing
   written, reported `unchanged`, `imported` stays empty (Task 2 `test_relayed_entry_that_started_here_is_unchanged`).
2. The same after that Codespace deleted its own transcript: still nothing written (Task 2
   `test_deleted_origin_transcript_is_not_recreated`).
3. A Codex fork published without relay: its ancestor still gets its own new-id copy (Task 2
   `test_unrelayed_codex_ancestor_publishes_as_today`).
4. The Mac acknowledged a snapshot but its publication stopped before the entry: the replacement still gets it
   (Task 3 `test_never_attempted_conversation_reaches_replacement`).
5. A record whose blob is missing or corrupt: one issue, the rest of the manifest still syncs (Task 1
   `test_unreadable_record_is_an_issue_not_a_failure`, Task 3 `test_unreadable_record_does_not_stop_the_cycle`).

---

### Task 1: The Mac keeps a record of every conversation it receives

**Files:**
- Modify: `devcontainer_setup/project_sync/checkpoint_store.py` (`acknowledge`, `collect`, new methods)
- Test: `tests/test_checkpoint_store.py`

**Interfaces:**
- Produces: `Store.relay_entries() -> tuple[list[dict], list[dict]]`: `(entries, issues)`. Entries are the live
  records' entries in `received_at` order, each a copy with `relayed=True`, and only those whose every blob
  reads back verified (`Store.read`). An unreadable one yields `dict(agent=..., session_id=..., reason=<exact
  relay issue reason>)` instead. Seeds first (below) if the marker is absent.
- Produces: `Store.acknowledge(snapshot_id, *, source)` unchanged signature; with `source == 'codespace'` it
  records the manifest's entries before writing the receipt, inside the existing `store.lock`.

- [ ] **Step 1: Write the failing tests** in `tests/test_checkpoint_store.py`, building entries whose head blob
  is put in the store:
  - `test_codespace_acknowledge_records_each_entry`: acknowledge a two-entry manifest with `source='codespace'`
    → two files under `checkpoints/relay/`, `relay_entries()[0]` equals both entries plus `relayed=True`.
  - `test_desktop_acknowledge_records_nothing`: same with `source='desktop'` → no `relay/` records.
  - `test_appended_revision_retires_the_earlier_record`: head bytes `b'A'` then `b'AB'` for one session →
    one live record, the `b'AB'` one.
  - `test_older_revision_arriving_later_is_not_recorded`: `b'AB'` then `b'A'` → one record, `b'AB'`.
  - `test_diverging_revisions_of_one_session_are_both_kept`: `b'AB'` then `b'AC'` → two records.
  - `test_collect_keeps_record_blobs_after_keep_days`: record, age the manifest and objects past `KEEP_DAYS`
    and `GRACE_SECONDS` (`os.utime`), replace the receipt with an empty manifest, `collect(apply=True)` →
    the record's blobs still read back.
  - `test_seeding_uses_current_receipt_and_unsuperseded_journals`: with no marker, a `codespace` receipt naming
    a one-entry manifest and two complete journals under `<state>/conversation-imports/*/receipt.json` (one
    with `superseded.json` beside it) → `relay_entries()` returns the receipt's entry and the unsuperseded
    journal's `source_checkpoint`, and writes the `seeded` marker; a second call does not seed again.
  - `test_unreadable_record_is_an_issue_not_a_failure`: delete one record's blob object → that entry is
    absent, `issues == [dict(agent=..., session_id=..., reason='Relayed conversation checkpoint is unreadable')]`,
    the other entry is returned.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_checkpoint_store -v` → the new tests fail
  (`AttributeError: 'Store' object has no attribute 'relay_entries'`, or no `relay/` dir).
- [ ] **Step 3: Implement** in `checkpoint_store.py`: `_record_received(manifest)` (called from `acknowledge`
  when `source == 'codespace'`, under the lock it already holds), `_seed_relay()` (journals read with
  `read_json`; skip any not `status == 'complete'` or with `superseded.json`), `relay_entries()`, and in
  `collect` add each live record's `_strings` to `live`. The retire rule is the Global Constraints one; apply it
  in both directions on each write (skip the new entry if a live record of the same session starts with it;
  remove live records it starts with).
- [ ] **Step 4: Run** `python3 -m unittest tests.test_checkpoint_store -v` → all pass; then `task check` and
  `task test` → exit 0.
- [ ] **Step 5: Commit** `git commit -m "Keep a record of every conversation received from a Codespace"`.

### Task 2: A Codespace never takes back what started on it

**Files:**
- Modify: `devcontainer_setup/project_sync/conversations.py` (`capture_conversations`, `publish_conversations`)
- Test: `tests/test_native_publication.py`

**Interfaces:**
- Produces: `capture_conversations` (unchanged signature) adds to `<state>/conversation-origins.json`, when
  `state` is not None, every session id it visits whose native file no publication journal on this side records
  with `created: true` — kept or refused, never an unchanged import.
- Produces: `publish_conversations` (unchanged signature): for an entry with `relayed` true whose session started
  here (in the started-here list, or a native file for that id in this project's Claude namespace or Codex index
  that no journal on this side created), appends `dict(agent=agent, source_session_id=sid, session_id=sid)` to
  `unchanged`, writes nothing, and goes on to the next entry.

- [ ] **Step 1: Write the failing tests** in `tests/test_native_publication.py`, using a second native profile
  (env) as the destination where the test needs one, since `NativeFixture` captures and publishes in one:
  - `test_capture_records_sessions_started_here`: capture a native Claude and a native Codex session →
    `conversation-origins.json` lists both ids; an unchanged import in the same profile is not listed.
  - `test_relayed_entry_that_started_here_is_unchanged`: capture a session, publish its entry with
    `relayed=True` into the same profile → `imported == []`, `unchanged == [dict(agent='claude',
    source_session_id=sid, session_id=sid)]`, no new file under the namespace, no journal dir created.
  - `test_deleted_origin_transcript_is_not_recreated`: as above, delete the native file after capture, publish
    again → `unchanged`, the file is still absent.
  - `test_relayed_entry_from_elsewhere_publishes_with_a_new_id`: publish the relayed entry into a second profile
    → one `imported` with a new `session_id`.
  - `test_unrelayed_codex_ancestor_publishes_as_today`: `codex_ancestor_fixture()`, publish leaf then ancestor
    entries without `relayed` into a second profile → the ancestor entry is `imported` with its own new id
    (today's result; this test passes before and after, and must fail if the check is applied to every entry).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_native_publication -v` → the new tests except the last
  fail (no `conversation-origins.json`; relayed entry imported).
- [ ] **Step 3: Implement** `_started_here(project, state, agent, sid, env) -> bool` and the list update in
  `conversations.py`; the list write merges with the file on disk and uses `atomic_write`.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_native_publication tests.test_native_checkpoints -v` →
  all pass; show `test_unrelayed_codex_ancestor_publishes_as_today` failing with the `relayed` condition
  removed from the check; then `task check` and `task test` → exit 0.
- [ ] **Step 5: Commit** `git commit -m "Never publish a relayed conversation back where it started"`.

### Task 3: Desktop → Codespace sends the records along (after Tasks 1 and 2)

**Files:**
- Modify: `devcontainer_setup/project_sync/replication.py` (`capture`)
- Modify: `docs/project-continuity.md` (one paragraph, next to the version-admission paragraph)
- Test: `tests/test_replication.py`

**Interfaces:**
- Consumes: `Store.relay_entries()` (Task 1); the started-here check inside `publish_conversations` (Task 2).
- Produces: `replication.capture(project, store, *, env=None, historical_roots=(), relay=False)`. With
  `relay=True` it returns a manifest whose `conversations.entries` are the native entries followed by
  `relay_entries()[0]` (an entry identical, as canonical JSON, to one already present is left out), and whose
  `issues` include `relay_entries()[1]`; with the default it is today's capture. `sync_project_data` passes
  `relay=True` only for its own outbound capture; the remote capture through `rpc.py` keeps the default, so a
  Codespace never seeds, records or relays (ruling 2026-11-02, BLOCKED-100-3.1; test
  `test_codespace_capture_never_relays`).

- [ ] **Step 1: Write the failing tests** in `tests/test_replication.py` (`ReplicationTests`; a replacement is a
  second `IsolatedPythonConnection` with its own home and `RemoteEndpoint`):
  - `test_published_predecessor_conversation_reaches_replacement`: remote A has conversation X; cycle A; cycle
    replacement B → B's `remote_publication['imported']` has `source_session_id == X` with a new id.
  - `test_refused_predecessor_conversation_reaches_replacement`: as above with the Mac's publication of X
    refused (patch the local publish to report X as an issue) → B still imports X.
  - `test_never_attempted_conversation_reaches_replacement`: as above with the Mac's publication raising
    `TimeoutError` before any entry → B still imports X.
  - `test_relay_to_the_origin_writes_nothing`: three cycles against A → A's `imported` stays empty after the
    first, X is in `unchanged`, A's native namespace holds exactly its original files.
  - `test_replacement_relay_is_idempotent`: three cycles against B → one copy of X on B, no new journal dir after
    the first.
  - `test_chain_delivers_to_the_third_codespace`: A → B → C, A's connection discarded before C → C imports X.
  - `test_interrupted_first_relay_finishes_next_cycle`: two predecessor conversations; in the first B cycle the
    remote `publish` runs and its reply is then replaced by an error envelope, as an RPC timeout would leave it
    (wrap `self.connection.python` the way `test_real_rpc_outbound_fault_preserves_inbound_custody_and_dirty_checkout`
    does, calling the real one first) → the next cycle reports both as `unchanged` and B holds one copy of each.
  - `test_unreadable_record_does_not_stop_the_cycle`: two predecessor conversations, one record's blob object
    deleted on the Mac → B imports the other, the outbound direction lists the exact relay issue, and the
    inbound direction completes.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_replication -v` → the first three, the chain and the
  unreadable-record tests fail (B imports nothing from A); compare with the analysis's `ReplacementRed`
  failures in `red-tests.log`.
- [ ] **Step 3: Implement** the merge in `replication.capture`, then the docs paragraph: a replacement Codespace
  receives what its predecessors delivered to the Mac; a conversation is never copied back to the Codespace it
  started on.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_replication tests.test_checkpoint_store tests.test_native_publication -v`
  → all pass; then `task check` and `task test` → exit 0 (list any existing count assertion that grew, per
  Global Constraints).
- [ ] **Step 5: Commit** `git commit -m "Carry a predecessor's conversations to its replacement (#100)"`.

### Task 4: Definition of done (lead)

Merge Tasks 1–3 into local main, root re-gate, then a live test on a throwaway innovate Codespace pair:
conversation X on Codespace A reaches the Mac, A is replaced by B (`task work:cs:armed -- --rebind <B>`), and
B's first completed cycle lists X under a new id (`claude --resume` on B finds it); A's own list gains no copy
while it is still bound. Then push main (standing OK) and roll out to innovate, mainstage and strata-sap.
