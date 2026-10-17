# Scratch, notes, and conversation handoff

Select project continuity in `task devcontainer:setup`. Review and commit the
generated changes, push them, and rebuild the Codespace if setup adds SSH support.
Both checkouts need the generated helpers and `.scratch` markers.

## Scratch files and project notes

`.scratch/` is for experiments, temporary inputs, investigation output, and notes
that belong to the project but should stay out of commits. Its `.gitignore` and
README are tracked; working contents are ignored even by `git add --all`.
Keep shared agent notes under `.scratch/memory/`. Promote lasting documentation
into the repository when it is ready for review.

The Mac runs the sync controller. Pair one existing, running Codespace with the
exact remote checkout under persistent `/workspaces`:

```sh
task scratch:configure -- --codespace NAME --remote-root /workspaces/PROJECT
task scratch:start
task scratch:status
task scratch:logs -- --follow
task scratch:backup
task scratch:stop
```

Start is idempotent and survives closing the launching terminal. It reconnects
after a network interruption and waits when the Codespace is stopped. It does
not start Codespaces or install a login service. Start it again after restarting
the Mac. The first connection records the selected Codespace's SSH host key;
later connections require that key.

Requirements on the Mac: Python 3.11+, Git, Task, OpenSSH, and GitHub CLI 2.102.0.
The CLI version is guarded because background no-start enforcement was tested
against its actual forwarding behavior. Select a GitHub account with the
process environment `DEVCONTAINER_GH_USER=USERNAME` if your active account cannot
access the Codespace. The existing `~/.ssh/codespaces.auto` key must already be
authorized by Codespaces, and sshd must listen on port 2222 as configured by the
wizard. Sync never edits your global SSH configuration.

Mutagen 0.18.1 installs lazily into private project state, authenticated with
platform-specific SHA256 pins. It uses `two-way-safe`, so conflicting changes
are reported rather than automatically selecting a winner. Resolve a conflict
by preserving the desired contents on both endpoints. Deletions propagate.
Tracked root markers, VCS files, and common caches are excluded. Symlinks,
special files, and names that collide by case are refused; check status/logs.

State lives in
`${XDG_STATE_HOME:-~/.local/state}/devcontainer-setup/projects/<repository-id>/`.
When the default `.local/state` ancestors are group-writable or unsafe (as in
a standard Codespace), private state instead uses `~/.devcontainer-state/`
without changing those directories. That fallback remains stable after creation.
Explicit absolute `XDG_STATE_HOME` overrides remain authoritative and must have
safe ancestors. Identity uses the normalized origin host and owner/repository, including across
SSH/HTTPS spellings. Repositories with the same directory name stay separate.
The configured checkout path is also checked. One project identity has one
configured sync pair; a different worktree must not silently rebind that pair.
An initialized registry whose ancestors become unsafe requires explicit repair
or migration. The command refuses to create a replacement identity. Ambiguous
existing registries and symlink ancestors also require review.
Long custom state paths can exceed the operating system's Unix socket limit;
select a shorter absolute `XDG_STATE_HOME` before pairing if the command reports
that limit. Keep the same override for subsequent commands.

`scratch:backup` writes a private, immutable local archive outside Git. Initial
sync and handoff also preserve recovery snapshots. There is no automatic backup
retention policy; remove old backups deliberately after reviewing their contents.

## Continue a conversation on the other host

Use managed entrypoints on both hosts:

```sh
task agent:codex                         # select a local conversation or start one
task agent:claude -- --new
task agent:sessions -- --agent codex
task agent:codex -- --session UUID
```

Sign in to each native CLI separately on each host; handoff does not transfer
authentication. Keep your code and branch commits synchronized through Git.

Supported native options go after a second separator, for example
`task agent:codex -- --session UUID -- --no-daemon`. Identity, cwd, fork, and
background overrides are rejected because they would break session ownership.
Generic config, profile, settings, and prompt overrides are also refused so they
cannot bypass the project memory policy. Use the native CLI directly for workflows
outside managed custody.

Exit the agent before transferring. Run handoff from the Mac, using its existing
scratch pairing. The Codespace must already be running:

```sh
task agent:handoff -- --agent codex --session UUID --to codespace --dry-run
task agent:handoff -- --agent codex --session UUID --to codespace
```

In the Codespace run `task agent:codex -- --session UUID`. Exit there before
returning ownership with `--to mac` from the Mac. Substitute `claude` for Claude
Code. Scratch notes sync separately; wait for a clean scratch status before
switching if you need the latest notes immediately.

Handoff copies the selected native transcript and supported dependencies, with
byte digests and recovery receipts. It retains the same conversation UUID.
Historical messages and paths are preserved; the native runtime receives the
destination checkout path. Codex indexes are rebuilt with its native API and all
history pages compared. Credentials, native databases, global settings, and
personal memory libraries do not cross hosts. Native profiles stay in their
usual directories; this does not require a project-specific `CODEX_HOME`.

Current project notes are supplied on managed launch. The generated project
Codex configuration disables native global memory generation/use for this
workflow. Managed Codex commands also disable the memory feature for their
process, preventing queued global consolidation. Managed Claude commands disable
auto memory for their process and rebuild the system prompt so current notes
apply when resuming. Existing historical memory cannot be removed without rewriting the
conversation; strict export refuses detected global-memory context. Normal
personal instructions and tool settings still apply on each host.

## Adoption and recovery

Existing conversations have unknown ownership until explicitly adopted. Check
their UUID and project before adopting on the host that currently owns them:

```sh
task agent:recover -- --agent codex --session UUID --adopt
```

Adoption validates scope and checks for active writers. It cannot replace an
existing ownership receipt. Managed launches hold an OS lease for their duration;
handoff transfers durable custody through matching source/destination receipts.
Do not bypass these commands with a plain native CLI for a managed conversation.
Advisory locks cannot prevent an independent tool from opening the same history.

If a local managed process crashed, inspect it and then use
`task agent:recover -- --agent codex --session UUID --crashed-local`. A live
process or uncertain remote transfer blocks recovery. For an interrupted handoff,
rerun the same handoff command: matching receipts can complete it without
selecting a new owner. Preserve state/backups when investigating a refusal.
There is no force takeover command.

## Format boundaries

Native formats change frequently. Unsupported formats fail before acceptance
and preserve existing data. The adapters recognize Codex 0.163/0.164 and Claude
Code 2.1 native records; detailed executed versions and fixtures are recorded in
the [spike reports](spikes/project-agent-state.md).

Codex inherited forks include their required ancestors. Codex subagent trees,
compressed histories, unknown records, and histories with unrelated project
scope are refused until their dependency closure is proven. Claude supports
parent transcripts, recognized subagent JSONL files, and numeric task JSON;
unrecognized task locks or sidecars are refused. Divergent histories are never
merged automatically. Compatible destination-ahead bytes are preserved, but
handoff requires verified matching conversation history before granting custody.
