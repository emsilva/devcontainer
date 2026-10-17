# Codex session and project memory spike

The native Codex engine completed a Mac → Codespace → Mac round trip with the
same conversation IDs, complete history, destination working directories, and
project-specific memory. This establishes a viable integration route. The
devcontainer does not yet provide session synchronization or a handoff command.

## Experiment

Two synthetic Git repositories had different remotes but the same directory
basename, `same-name`. Each had its own temporary `CODEX_HOME`, conversation,
memory summary, and unique memory canary. The fixture assigned the homes
explicitly; automatic remote normalization and project selection were not tested.

The Mac used Codex `0.163.0`; the Codespace used `0.164.0-alpha.1`. No version
was installed or changed for this experiment. These results establish
compatibility for this particular pair, not for arbitrary future versions.

The test ran real native `codex app-server` processes and completed turns using
a scripted Responses API provider bound to loopback on each machine. No real
model generation was requested. Only synthetic transcripts and memory crossed
hosts. Existing credentials, native databases, personal memory, and project
files were excluded.

Every native source process exited before its snapshot was imported elsewhere.
This enforced sequential writers in the experiment. A product ownership
protocol was not implemented or tested.

| Stage, per project | Turns | Visible items | Turn pages | Item pages |
| --- | ---: | ---: | ---: | ---: |
| Initial Mac session | 4 | 7 | 2 | 4 |
| Continued in Codespace | 6 | 10 | 3 | 5 |
| Continued back on Mac | 8 | 13 | 4 | 7 |

The fixture used a page size of two and compared every returned turn and item
before continuing each imported conversation. Each stage included a native
`pwd` command, which creates a separate turn. Both original IDs survived:

- Project A: `01a147a7-fd91-7291-b0f9-9719ae66eecf`.
- Project B: `01a147a8-0f53-70f3-b17a-0dc050862d90`.

## What passed

- Native resume reconstructed usable history from the transferred JSONL files
  without copying the source SQLite databases. Entire turn and item responses
  matched after each import, before new activity.
- Each incoming transcript remained an exact byte prefix of the outgoing
  transcript. Historical messages and their original paths were preserved.
- `cwd` and `runtimeWorkspaceRoots` selected the new checkout. Native `pwd`
  output matched that checkout on both operating systems.
- Destination `AGENTS.md` appeared in the native response's instruction sources.
- Project A's model requests and exported state excluded project B's memory
  canary, and vice versa, despite identical folder basenames.
- A new memory canary added at each destination appeared in its actual provider
  request after the explicit context refresh described below. Merely finding a
  file on disk was not the memory acceptance gate.
- The normal Mac terminal UI reopened the returned conversation with
  `codex resume --no-daemon <id> -C <destination>`, displayed both Linux and Mac
  history, reached its input prompt, and exited cleanly without a new turn.

The remote terminal UI was not exercised; remote continuation used the native
app-server API. Provider inspection proves context delivery, not whether a real
model would obey that context.

## The memory catch

A resumed conversation retained the memory summary originally injected into its
history. Updating the destination's `memories/memory_summary.md` did not refresh
that context by itself. Passing `developerInstructions` to `thread/resume` also
did not put the new canary into the next provider request in this test.

The successful route was to resume normally, verify history, and append a fresh
environment and project-memory note through `thread/inject_items`. Its developer
message carried the destination checkout, the destination's scoped memory path,
and the current scoped summary. The subsequent request contained the fresh
canary. This added context without editing earlier messages.

This is an app-server integration route. The plain `codex resume -C` command
does not perform that additional refresh. A launcher must provide equivalent
current context, with a tested interface, before we promise fresh memory during
an ordinary CLI handoff.

Native Codex also initialized an internal Git repository under the synthetic
memory directory. That metadata was excluded from the final transfer bundles,
as were native database and credential files. Memory artifact selection must be
explicit; recursively synchronizing an entire profile is not the adapter.

The memory files were synthetic fixtures and background generation was disabled.
Automatic extraction, consolidation, and generated-memory recovery were not
tested. Nothing here establishes a safe way to split the existing shared
personal memory library into project packs.

## Recommended implementation

Use a repository-scoped Codex home for new work, keyed by the normalized Git
remote rather than the checkout name. A possible host-local location is:

```text
${XDG_DATA_HOME:-~/.local/share}/devcontainer-setup/projects/<repository-id>/codex/
```

The launcher selects that home per process and preserves the host's normal
settings and login through an explicit local integration. Configuration and
authentication reuse still need a separate compatibility test; this experiment
used an unauthenticated test provider and empty fixture profiles.

Export only the selected conversation snapshot and that project's permitted
memory artifacts. Keep SQLite, authentication, caches, and memory Git metadata
local. On import, use native resume to build destination indexes, verify all
history pages, select the runtime paths, and add fresh current context.

Scratch files can use the automatic two-way transport investigated in
[the scratch spike](scratch-sync.md). Conversations need controlled ownership:
quiesce the source, validate checksums and ancestry, acknowledge the transfer,
then permit the destination to write. A lock file replicated through eventual
two-way synchronization cannot by itself establish exclusive ownership during a
network partition.

Possible Taskfile entrypoints are `task agent:codex` for a project-scoped launch
and `task agent:handoff -- codespace` or `-- mac` for an explicit transfer.
These commands are design suggestions; they do not exist yet.

## Remaining rollout gates

Implement and test ownership acquisition, uncertain/offline source refusal,
divergent snapshot refusal, interrupted transfers, and recovery copies. Prove
project identity checks, worktree mapping, real host authentication/settings
reuse, native memory generation, and current-context delivery in the chosen
terminal launcher. Add version compatibility checks and an ordinary remote UI
smoke test. Reuse the scratch spike's lifecycle work so synchronization does not
silently start a stopped Codespace or extend its lifetime.

## Evidence and cleanup

The private local experiment directory is
`.superpowers/codex-sync-spike/` in the canonical repository checkout. It retains
`native_probe.py`, `remote_probe.py`, `results.json`, the three synthetic stage
bundles, and cleanup receipts. It is ignored by Git. The stage bundles contain
full synthetic histories; the tracked report contains no personal transcript.

An early fixture tried to page a new thread before its first user message;
Codex correctly rejected that request. Another fixture closed immediately after
submitting `pwd`, interrupting it; the final fixture awaited command and turn
completion before exporting history. The failed memory-refresh trials are
distinct from the successful explicit-context round trip.

Direct `gh codespace ssh` timed out during the initial connection attempt. The
successful transport used a private loopback port forward for remote SSH port
2222 and ordinary OpenSSH, with no global SSH configuration changes.

The remote fixture directory and owned forwarder were removed. The existing
Codespace checkout stayed clean at
`83b0989ed40a7e0ada3f8fad30ee6568969ef0a2`. The Codespace was returned to its
initial stopped state. Local temporary fixture homes were removed after the UI
smoke; the private evidence bundles remain available for inspection.

Repository validation passed: `task check` and `task test` (165 tests).
The native round trip and terminal UI resume were the experiment's smoke tests.

References: [native thread resume and pagination](https://learn.chatgpt.com/docs/app-server#threads),
[Codex memory storage](https://learn.chatgpt.com/docs/customization/memories#local-memory-storage),
[Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference).
The installed binaries' generated experimental JSON schemas supplied the exact
API fields exercised by the probes.
