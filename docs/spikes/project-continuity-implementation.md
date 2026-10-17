# Project continuity implementation evidence

Measured on 2026-10-17 with disposable repositories and selected test UUIDs.
The reusable implementation is in `devcontainer_setup/project_sync/`; the
generator packages the same source when project continuity is selected.
See [user commands and recovery](../project-continuity.md).

## Transport and scratch

Actual macOS arm64 ↔ Codespace Linux amd64 tests used checksum-pinned Mutagen
0.18.1 and GitHub CLI 2.102.0, through the generated Taskfile entrypoints.

| Check | Result |
|---|---|
| Detached start, repeated start, stop/restart | Passed; launching shell exits |
| Create, edit, rename, delete | Passed automatically in both directions |
| Nested Unicode and spaces | Passed |
| 8 MiB binary | SHA256 equal at both endpoints |
| Delete while stopped, then restart | Passed; retained session ID, no resurrection |
| Simultaneous different edits | Both versions preserved; conflict visible in status |
| Explicit conflict resolution | Passed |
| Kill the owned tunnel, then edit | Reconnected automatically and delivered the edit |
| Tracked markers and ignored payloads | Markers preserved; only markers tracked |
| Private recovery archive | Mode 0400 and SHA256 verified |
| Real stopped Codespace | Worker stayed waiting through five observations; no wake |
| Actual CLI Shutdown/start race | Offline fake API received GET only; POST rejected locally |

The Codespace reached its configured 30-minute idle shutdown during the tests.
The explicit restart was a separate authorized controller action. The background
transport uses an exact-route read-only API adapter and receives no real GitHub
token; the controller retains that token only in memory. Existing SSH and
Mutagen global configuration are untouched.

## Native conversations and project memory

Actual versions: Codex 0.163.0 on macOS and 0.164.0-alpha.1 on Linux; Claude Code
2.1.302 on macOS and 2.1.301 on Linux. Minimal authenticated macOS turns in both
tools answered the project-note canary `ORCHID`. Their actual native transcripts
completed same-ID Mac → Linux → Mac transfers without copying authentication,
native SQLite, or global memory stores.

Codex fork probes include ancestors, drain every native history page, compare
all turn/item rows in both directions, verify original byte prefixes, and check
destination runtime roots. Fresh project notes appear in actual foreground
model requests. Native mocks used a local synthetic provider; they are separate
from the minimal authenticated turns.

The Linux probe caught global memory consolidation despite setting use/generate
to false. Disabling `features.memories` for the process eliminated those requests.
Both the generated project policy and managed launches now disable it, alongside
the use/generate flags. A real native guardian probe verified that killing the
launcher or guardian cannot grant a second writer while its native child lives.

Claude native resumes verify original conversation content and fresh notes in
actual requests to a synthetic loopback provider. Historical bytes remain an
exact prefix. Current native request shape/blob/message references and complete
request digests are validated, including repeated cross-host continuations.
Claude auto memory is disabled only for the managed process; prompt snapshots
are rebuilt so newly synchronized notes take effect on resume.

Review regressions cover interrupted transfer responses, concurrent source
launches, private namespace replacement, initialized state permission drift,
native option aliases selecting another UUID, malformed retained suffixes,
unrelated runtime scope, and unsupported dependency/record formats. Unknown
ownership and uncertain transfer state never grant implicit takeover.

## Third-party transfer spike

`codex-claude-transfer` was evaluated at
`13a84383a5e00db61d527e782f36f4bbf0c707d2`. Its full Go test suite passed.
An actual native inherited-fork export contained both parent and child. Mapped
import refused before modifying the target; unchanged initial import succeeded;
incremental inherited-history merge refused and preserved the target bytes.
The source audit also identified writer-race and partial memory-selection
boundaries. CCT is not a production write dependency; the implemented adapter
uses exact native snapshots and custody receipts instead.

## Distribution and container lifecycle

A fresh archive without Git metadata passed the piped installer preview, selected
continuity generation, original project-task preservation, generated status,
and installed configuration check. The preview changed neither project files
nor saved preferences.

A real generated container built successfully in the Codespace. `task up`,
repeated up, `task status`, `task down`, repeated down, and final empty status
passed. Inside it, the user was `vscode`, Python was 3.13.16, and the official
installers provided Codex 0.163.0, Claude Code 2.1.302, and OpenCode 1.19.3.
The packaged Codex app-server initialized successfully without a model request.

## Boundaries

Physical Mac sleep and destructive Codespace rebuild were not automated.
The disposable `/tmp` checkout was cleared by Codespaces during idle shutdown;
its fixture Git metadata was recreated. Real paired checkouts belong under
`/workspaces`. Native transcripts and private custody receipts survived restart. Tunnel
loss, stopped-state waiting, explicit restart, and private state recovery were
tested. Native Codex subagent trees, compressed histories, and unrecognized
native records/sidecars are explicit refusals. Managed locks are advisory: a
plain native CLI can bypass the managed workflow. No claim is made that future
daily CLI releases preserve the tested formats or option semantics.

Receipts, private fixture scripts, full logs, and payload checksums remain in the
local ignored project-state lab. No private transcript or credential is checked
into this repository.
