# Project-scoped session and memory synchronization

Owner requirements: make Codex and Claude conversations and project memory
available on the Mac and a Codespace for any repository using this devcontainer.
Continue the same conversation, with one machine writing at a time. Share only
the selected project's memory, never the user's complete cross-project library.

This is a proposed design with a local Codex compatibility probe. It is not an
implemented sync feature or a completed cross-host continuation demonstration.

## Identity and paths

Derive a stable project identity from the normalized Git remote, including its
host, owner, and repository. Resolve the current checkout and registered
worktrees at setup time. Store each machine's absolute path mappings locally.
Validate the destination's Git identity before importing state.

Use that project identity to partition scratch files, session snapshots, and
memory. A checkout's folder name is not its identity. Independent repositories
must not share a state directory even when their final folder names match.

Select the runtime working directory and workspace roots for the destination.
Preserve historical conversation text and tool output: replacing path strings
throughout a transcript would rewrite the record of what happened. Structural
metadata changes require a tool-specific, versioned adapter and validation.

## Sessions

Treat the transfer of an active conversation as a handoff:

1. Quiesce its source writer and record the last complete turn and Git state.
2. Export an immutable, checksummed snapshot for that repository and session.
3. Transfer it through the private sync transport.
4. Import it on the destination, rebuild the destination's own indexes, and
   verify the session ID, history, working directory, and workspace roots.
5. Transfer ownership and resume the same session ID on the destination.

Background backup snapshots may be copied while a source is active, but must not
become a second writable conversation. An unreachable source cannot be assumed
to have stopped writing. Preserve divergent histories and stop a handoff if
ownership or ancestry cannot be established.

Keep credentials and native database files on their respective machines. Move
scoped snapshots rather than live profiles or SQLite files.

### Codex compatibility probe

Tested with Codex CLI `0.163.0`, using an explicitly identified root rollout
snapshot in a temporary local Codex home. No source database or memory library
was copied. No `turn/start` was submitted and no copy was sent to a Codespace.

- A copied paginated rollout was discoverable through `thread/read`.
- Metadata-only discovery did not populate paginated turns or items.
- Native `thread/resume` then succeeded with the original conversation ID and a
  different `cwd` and `runtimeWorkspaceRoots`.
- After resume, `thread/turns/list` returned 17 turns, and `thread/items/list`
  returned a first page of 100 items with a continuation cursor.
- The temporary server and copy were removed after the probe.

This supports a snapshot plus native-resume adapter. Complete item pagination,
source/destination history fingerprints, round-trip continuity, and the actual
CLI experience on both operating systems still require verification. A
successful metadata read alone is not the acceptance gate.

References: [thread APIs](https://learn.chatgpt.com/docs/app-server#threads),
[resume working directory](https://learn.chatgpt.com/docs/developer-commands#codex-resume).
The installed CLI's generated schema also exposes `cwd` and
`runtimeWorkspaceRoots` on `ThreadResumeParams`.

### Claude compatibility route

Claude Code documents `CLAUDE_CODE_PROJECT_DIR_NAME` together with
`CLAUDE_CONFIG_DIR` to give transcripts and auto memory a stable project storage
name independent of the working directory. A repository-specific launcher could
set these per process while keeping the host's existing configuration directory.
Only that project's state would be transferred.

Claude also documents resuming from a transcript path. Its JSONL entry format is
internal and can change between versions, so the adapter needs version checks
and a real resume test. This route has been researched, not live-tested here.

Reference: [Claude sessions and storage](https://code.claude.com/docs/en/sessions).

## Memory

- Repository instruction files such as `AGENTS.md` and `CLAUDE.md` remain normal
  Git-managed project files.
- Claude's per-repository auto memory is a direct candidate for scoped Markdown
  sync. Use a stable project directory or configure `autoMemoryDirectory` with
  the appropriate host-local absolute path.
- Codex's local memory store combines generated material across conversations
  and projects. Its documented configuration does not provide a per-repository
  memory-export interface. The proposed adapter must build a project-only memory
  pack from positively identified project inputs, or use an explicit project
  notes layer. This adapter and its isolation guarantees remain unproven.
- Keep each tool's memory format separate. Provide project-relative references
  in new portable notes, preserve source evidence, and resolve host paths in a
  generated view. Conflicting edits must remain recoverable.

References: [Claude auto memory](https://code.claude.com/docs/en/memory#storage-location),
[Codex local memories](https://learn.chatgpt.com/docs/customization/memories),
[Codex memory configuration](https://learn.chatgpt.com/docs/config-file/config-reference).

## Gates before rollout

Demonstrate Mac-to-Codespace-to-Mac continuation for each tool, retaining the
same conversation ID and complete history. Verify destination path resolution,
source-writer exclusion, refusal of divergent snapshots, version compatibility,
and stopped-Codespace behavior. Use two unrelated synthetic projects with
identical folder basenames to prove session and memory isolation. Prove memory
is loaded by the destination tool, not merely present on disk, and test recovery
from interrupted transfer without replacing either host's existing state.
