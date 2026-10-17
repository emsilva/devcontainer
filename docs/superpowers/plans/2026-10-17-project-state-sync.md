# Project continuity implementation plan

> For agentic workers: use subagent-driven-development for independent components,
> with a task review and a final whole-branch review. The owner requested continuous
> autonomous execution, including PR and merge after independent validation.

**Goal:** Ship tested automatic scratch synchronization and project-only agent
memory plus controlled Codex/Claude conversation handoff in the reusable wizard.

**Architecture:** Keep normal native profiles. Python coordinates repository
identity, project notes, private state, native conversation snapshots, ownership,
and a Mutagen supervisor. A read-only GitHub API adapter prevents implicit
Codespace starts. Pure rendering feeds all tracked changes through GitPlan.

**Tech stack:** Python 3.11+ stdlib, go-task, Git/GitHub CLI/OpenSSH, pinned Mutagen,
native Codex and Claude CLIs. Go is used only in the disposable third-party spike.

## Global constraints

- Preserve normal Codex/Claude profiles, credentials, and native databases.
- Share only the selected project's notes and explicitly identified conversations.
- Continue the same conversation with one writer at a time.
- Synchronize only `.scratch/` working contents; tracked markers stay Git-managed.
- Background synchronization must not start a stopped Codespace.
- All user-owned data survives conflicts, interrupted operations, and reruns.
- Do not change global agent model, permissions, notifications, or Herdr settings.
- Do not merge the Mainstage branch. Deliver through `emsilva/devcontainer` PR/main.

## Task 1: Repository state and project-note integration

**Ownership:** `devcontainer_setup/project_sync/__init__.py`, `state.py`, `notes.py`,
`tests/test_project_state.py`, `tests/test_project_notes.py`. No wizard/render edits.

**Interfaces:**

```python
@dataclass(frozen=True)
class Project:
    root: Path
    remote: str
    identity: str
    project_id: str

def discover_project(path: Path) -> Project: ...
def state_dir(project: Project, env: Mapping | None = None) -> Path: ...
def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None: ...
def read_json(path: Path) -> dict: ...
@contextmanager
def locked(path: Path, blocking: bool = False): ...
def host_id(env: Mapping | None = None) -> str: ...
def integrate_notes(project: Path, files: dict[str, bytes], enabled: bool) -> None: ...
```

- [ ] Write failing behavior tests using temporary Git repositories: normalize
  GitHub SSH/HTTPS and worktrees, distinguish same basenames/different remotes,
  reject embedded credentials/control characters/missing origins, validate XDG
  fallback, private atomic writes, symlink/traversal refusal, competing locks.
- [ ] Implement the state interfaces, with filesystem creation only in explicit
  mutation helpers. A project ID is the first 24 hex characters of SHA256 of its
  normalized remote identity. `state_dir` returns an absolute directory below
  `devcontainer-setup/projects/<id>` without creating it.
- [ ] Write failing notes tests: preserve exact existing AGENTS/CLAUDE content,
  idempotent blocks, refuse edited/malformed blocks, `.scratch` marker exclusions,
  correct TOML scope when preexisting tables exist, preserve existing unrelated
  native project options. `integrate_notes` is pure and adds candidate bytes only.
- [ ] Implement managed blocks with embedded checksum ownership for the block
  body. Codex TOML policy explicitly selects `[memories]` using a parse-validated
  candidate; refuse conflicting existing user memory policy rather than override.
  No direct writes and no ignored memory payload in the GitPlan candidate map.
- [ ] Run focused tests RED/GREEN, `task check`, commit only owned paths, report
  exact command/output, interfaces, and any scope constraints.

## Task 2: Scratch transport, tools, and detached lifecycle

**Ownership:** `devcontainer_setup/project_sync/transport.py`, `scratch.py`,
`tools.py`, `templates/sync-tools.json`, `tests/test_sync_transport.py`,
`tests/test_scratch_sync.py`. Do not edit Task 1 files or wizard integration.

**Interfaces:**

```python
class CodespaceConnection:
    def __init__(self, codespace: str, *, state: Path, env: Mapping | None = None): ...
    def __enter__(self): ...
    def __exit__(self, *exc): ...
    def python(self, code: str, payload: bytes = b'', timeout: int = 60) -> bytes: ...
    def ssh_config(self, path: Path) -> None: ...

def configure(root: Path, state: Path, codespace: str, remote_root: str) -> dict: ...
def start(root: Path, state: Path) -> dict: ...
def stop(state: Path) -> dict: ...
def status(state: Path) -> dict: ...
def backup(root: Path, state: Path) -> Path: ...
def supervise(root: Path, state: Path) -> None: ...
```

- [ ] Read transport audit and existing scratch/codex live probe scripts first.
  Establish failing offline tests using actual `gh` against a fake loopback API:
  Shutdown and Available→Shutdown race must reject POST before upstream receives
  it; wrong route/token/name fails; no real account token reaches the child/log.
- [ ] Implement authenticated loopback read-only API forwarding to the exact
  selected Codespace read endpoint. Child gets only adapter token plus process-
  local `GITHUB_API_URL`. Ordinary SSH/private port forwarding uses owned handles.
- [ ] Add checksum-verified lazy Mutagen install for macOS/Linux arm64/amd64,
  isolated daemon state, no global configuration edits, no host Go/Node requirement.
  Consult official release assets and record actual digests in the tools manifest.
- [ ] Implement persisted configuration with repository validation, detached
  supervisor with owned process identity and lock, idempotent start/stop/status,
  bounded rotating logs, pause/wait while stopped and reconnect when available.
- [ ] Configure `two-way-safe`/SHA256 scratch-only sync. Preserve marker files,
  exclude VCS/caches/control payloads, report conflicts/errors without auto-winner.
  Recovery backups use immutable private archives and preserve ignored payloads.
- [ ] Meaningful unit/process tests cover bad config, missing tools, checksum
  failure, no unrelated process/session termination, restart, stop, and backup.
- [ ] Run focused RED/GREEN tests, `task check`, commit owned paths, report.

## Task 3: Native conversation snapshots and ownership

**Ownership:** `devcontainer_setup/project_sync/sessions.py`, `ownership.py`,
`agents.py`, `tests/test_session_handoff.py`, `tests/test_agent_ownership.py`.

- [ ] Complete third-party source and executable spike; record pinned CCT
  revision and actual fork/incremental/refusal behavior. Reject unsafe production
  write dependency; retain its source/test evidence as a spike report.
- [ ] Define strict versioned JSON snapshot manifest: project identity/Git state,
  agent/version/session UUID, native relative paths, file bytes/digests, ancestry,
  source host and ownership generation. Credential/database paths cannot enter it.
- [ ] Tests precede implementation for root/fork/subagent dependencies, complete
  record boundaries, duplicate IDs, wrong project, truncation, unsupported records,
  global-memory scope contamination, absent/identical/prefix/local-ahead/divergent
  imports, interrupted writes and recovery receipts. Use real filesystem fixtures.
- [ ] Implement native Codex/Claude adapters, immutable exports, all-bundle
  preflight, private recovery copies, atomic staging, and native runtime cwd
  selection rather than historical text replacement. Unsupported native state
  fails before target mutation.
- [ ] Implement managed native launches with local exclusive leases and durable
  owner receipts. Exact UUID handoff refuses live/unknown writers, commits a
  transfer nonce, acknowledges verified import before target launch, and keeps
  uncertain state blocked for explicit recovery. Keep ordinary native profiles.
- [ ] Verify complete native Codex pagination after import and source/destination
  identity. Supply current project-note context through a tested native launch
  interface. Claude resumes the same UUID in the destination project namespace.
- [ ] Run focused RED/GREEN tests, native mock-provider round trips including
  fork/roundtrip and project isolation, `task check`, commit owned paths.

## Task 4: Wizard, packaging, tasks, and documentation

**Ownership:** wizard/render/release packaging, runtime `__main__.py`, templates,
Taskfiles, README/docs, and integration tests. Root coordinates interface changes.

- [ ] Add continuity selection with backwards-compatible defaults and safe
  upgrade behavior. Include SSH support when Codespace continuity needs it.
- [ ] Copy canonical Python runtime into generated managed `.devcontainer`
  helpers; use isolated Python bootstrap, no duplicate maintained implementation.
- [ ] Add Taskfile entrypoints `scratch:configure`, `scratch:start`, `scratch:stop`,
  `scratch:status`, `scratch:logs`, `scratch:backup`, `agent:codex`, `agent:claude`,
  `agent:sessions`, and `agent:handoff`. Whole-stack lifecycle targets stay intact.
- [ ] Feed tracked note/config blocks through both interactive and dry-run
  GitPlan paths. Preserve root Taskfile variants, custom configuration, staged
  edits, old release fixtures, and native agent installer behavior.
- [ ] Explain one-time configuration, day-to-day switching, recovery, current
  unsupported cases, version boundaries, and scratch deletion semantics directly.
- [ ] Run focused render/wizard tests, full existing/new suite, ShellCheck and
  host validation. Commit before external publication.

## Task 5: Independent acceptance and delivery

- [ ] Root reruns all gates and actual installed Taskfile smoke on a fresh
  synthetic repo. Validate archive install/update and adjacent existing features.
- [ ] Live Mac/Codespace scratch and native session tests with fixture-only
  payloads: both directions, fresh notes in actual provider requests, minimal
  authenticated turns, same IDs/history, stopped-state race, restart/conflicts,
  malformed/divergent imports and recovery. Record checksums and receipts.
- [ ] Rebuild and verify relevant container lifecycle. Confirm normal host
  profiles/config/auth unchanged and no other project data in transferred state.
- [ ] Request independent whole-branch review against merge-base with origin/main,
  fix findings, rerun affected gates and root smoke.
- [ ] Push branch, open PR, wait for exact-head CI, verify merge message has no
  AI trailers, merge, remove merged worktree first, prune local/remote branches,
  verify remote branch absence. Stop test Codespace and owned background workers.
