> Historical full-checkout recovery design/evidence. Superseded by the scratch-only continuity design in [project continuity](../../project-continuity.md).

# Herdr recovery implementation plan

**Goal:** Make a Mac-local recovery copy sufficient to continue project work when
Codespaces is unreachable, behind `task work` and `task work:cs`.

**Architecture:** Content-addressed checkpoint storage and read-only project/native
collectors feed the existing owned replication worker. Native fork restoration
and Herdr workspace attachment replace transactional conversation handoff.

**Constraints:** Python 3.11+, existing tested gh/Mutagen versions; no credentials,
global DB/profile copies, source checkout overwrites or background Codespace starts.
Keep owner tasks/settings and private historical backups. Isolate native tests.

### Task 1: Durable store and project tree recovery

Own `checkpoint_store.py`, `project_files.py` and their tests.

```python
class Store:
    def __init__(self, state: Path): ...
    def put(self, data: bytes) -> str: ...
    def read(self, digest: str) -> bytes: ...
    def save(self, manifest: dict) -> str: ...
    def load(self, snapshot_id: str) -> dict: ...
    def acknowledge(self, snapshot_id: str, *, source: str) -> dict: ...
    def latest(self, source: str) -> dict | None: ...
def capture_files(project: Project, store: Store) -> dict: ...
def restore_files(store: Store, manifest: dict, destination: Path) -> Project: ...
```

All blob references are dictionaries containing `blob: SHA256`. `save` verifies
referenced content; acknowledgement returns `snapshot_id` and local `received_at`.
`source` is `local`, `codespace` or `desktop`. File manifests retain original
origin, Git HEAD/branch, a HEAD bundle, index entries/blobs and working files.

- [x] Add fail-first corruption/path/collision/partial-write and offline Git tests.
- [x] Implement private immutable store and atomic acknowledgement.
- [x] Implement capture/restore including staged/unstaged/untracked/deleted files.
- [x] Prove source refs/index/files unchanged; commit owned green files.

### Task 2: Live native conversation checkpoints and fork recovery

Own `conversations.py`, reusable parts of `sessions.py`/`agents.py`, native tests.

```python
def capture_conversations(project, store, *, state=None, env=None) -> dict: ...
def restore_conversation(project, store, entry, *, state=None, env=None) -> dict: ...
```

Capture returns `entries` and explicit `issues`; entries reference immutable blobs
and identify agent, source session, root/approved scopes and completed-turn cutoff.
Restore returns a new `session_id`, native `argv`, and source provenance. It may
return Claude fork argv before the native process materializes the child.

- [x] Spike native no-turn Codex path fork and fork-of-fork closure; Claude fork.
- [x] Test stable active prefixes, partial records, dependencies and scope refusals.
- [x] Implement collectors with no native writer/API calls.
- [x] Implement destination-native fork and project-note context, no takeover.
- [x] Prove native history/cwd and unchanged originals; commit green owned files.

### Task 3: Herdr adapter

Own `herdr.py`, `ssh_hosts.py`, corresponding tests.

```python
def configure_ssh(project, state: Path) -> str: ...  # current owned ssh-config
def refresh_ssh(state: Path) -> None: ...          # only an already owned alias
def open_local(project, state: Path) -> int: ...
def open_remote(project, state: Path, remote_root: str) -> int: ...
```

Remote operation consumes the live worker's SSH config. Register by verified
target/session tuple, retain returned opaque IDs, reuse workspace by checkout cwd.
Use a private child config for nested attach and retain global settings unchanged.

- [x] Test owned include/alias idempotence, stale/conflicting ownership and quoting.
- [x] Test machine/workspace reuse and process-scoped nested settings.
- [x] Implement real CLI adapter and explicit remote setup; commit green files.

### Task 4: Controller, replication and retirement

Root owns `workspace.py`, `replication.py`, `rpc.py`, Codespaces discovery extraction,
scratch integration/retargeting, CLI/tasks/render/GitPlan migration, docs and gates.

Snapshot document schema1 has `project`, `files`, `conversations`, and `producer`.
Only allowed RPC operations may capture/read/receive/ack/restore project data.
Validate project identity and SHA256 references, use isolated canonical helpers,
and preserve transport's GET-only background boundary.

- [x] Add fail-first full checkpoint capture/transfer/ack/offline restoration tests.
- [x] Implement transient canonical RPC and content-addressed blob exchange.
- [x] Extend owned worker with periodic capture/pull and SSH alias refresh.
- [x] Implement local/remote workspace switching and active-root registry.
- [x] Replace daily menu/custody CLI with workspace/status/local-agent entrypoints.
- [x] Add reviewed deletions through rendering/GitPlan/hooks and exact alias cleanup.
- [x] Delete obsolete modules/tasks/tests only after replacement gates pass.
- [x] Update README/continuity docs and run full suite + task check.
- [x] Independent whole-branch review, Root outage/native/Herdr smoke and cleanup.
- Delivery: update PR26 to final scope, push, merge, prune merged branch; refresh only
  generated Mainstage files without merging its documentation branch.
