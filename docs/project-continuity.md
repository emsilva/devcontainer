# Project continuity

Run `task work:cs` on the Mac to work in a Codespace through Herdr. Run `task work`
to work locally, including when GitHub is unreachable. Use one host at a time for
the same conversation. Herdr, GitHub CLI, Python, and each native agent must be
available on their respective hosts; setup installs the container tools.

## Daily commands

| Command | Result |
| --- | --- |
| `task work:cs` | Explicitly start/connect Codespace, back it up to Mac, attach Herdr |
| `task work` | Open local Herdr; recover remote work when switching back |
| `task work:codex` / `task work:claude` | Fresh native conversation on the current host |
| `task work:codex -- --resume` | Choose a scoped conversation, including recoverable checkpoints |
| `task work:claude -- --resume` | Choose a Claude conversation |
| `task work:status` | Actual Mac receipt time, worker state, scratch conflicts, conversation issues |
| `task work:stop` | Stop only this project’s owned sync/backup worker |

If a project already has a custom `work` task, use `task devcontainer:work` and the
other `devcontainer:work:*` tasks. Setup preserves custom root tasks.

## What gets protected

The Mac worker checkpoints the remote Git HEAD and its reachable history, exact
index (including staged bytes), tracked and nonignored working files, deletions,
file modes, safe links, and ignored `.scratch/` contents. A checkpoint is acknowledged
only after every referenced object has arrived and passed its SHA256 check.
Interrupted transfers leave the last good receipt intact.

Checkpoints live in private host-local state, normally
`~/.local/state/devcontainer-setup/projects/<project-id>/checkpoints/`. They are
immutable, deduplicated by content, and currently retained without automatic
pruning. Large repositories and long conversations consume local disk and take
longer to checkpoint. Individual objects above 256 MiB refuse transfer. Do not treat the five-second polling delay as a guaranteed
recovery-point interval: collection and transfer take additional time.

Ignored build outputs outside `.scratch/` are excluded. Unborn repositories,
submodules, special files, split indexes, unsafe/colliding paths and unstable file
reads can refuse a checkpoint. Sparse checkout support is unproven. Inspect the
reported checkpoint failure before relying on protection.

## Conversations and memory

Collectors read native transcripts without changing the running conversation.
They retain only a verified stable prefix ending at a supported completed turn,
including required native ancestors. Native formats that cannot be validated are
reported explicitly; originals remain untouched. Unfinished turns can be lost
if the Codespace disappears before a supported completed prefix reaches the Mac.

Cross-host continuation creates a native fork with a new ID and verifies retained
history. No source writer-release protocol is needed. Authentication, native global
SQLite files, profiles, and personal memory libraries are never replicated.
Existing source histories are never resumed just to collect a checkpoint.

Shared project memory belongs in `.scratch/memory/`. Managed launch reads those
notes, and generated project Codex policy disables global memory for this workflow.
A historical conversation containing global-memory or unrelated-project context
may be refused rather than silently stripped or exported.
Codex child-agent histories and Claude Agent/Task sidecars currently refuse
continuation until their full dependency closure is supported. Conversations
started from nested subdirectories are not currently discovered; launch the
managed commands from the checkout root. These limits do not block code backups.

## Recovery preserves working copies

First Codespace entry preserves its existing work as a checkpoint, then creates a
separate desktop-seeded checkout under `/workspaces/.devcontainer-work/<project-id>/`.
Herdr focuses that exact checkout. Repeated remote entry reuses it.

Switching back creates a new private local checkout under the project state’s
`recovered/` directory and focuses it in Herdr. Existing Mac code and index are
preserved. Once working locally, later remote receipts cannot overwrite the active
local checkout. Switching to Codespace again creates another remote checkout from
the active desktop work. Push normal project changes through Git as usual.

Recovery uses the last checkpoint received on this Mac. A sleeping, disconnected,
or powered-off Mac receives nothing. `task work:status` is the authority for the
last receipt; a green scratch connection alone is insufficient.

## Transport and advanced scratch controls

The foreground command can start a stopped Codespace. The background worker has
an enforced GET-only GitHub API capability and cannot start it or spend credits.
It reconnects to an already running space. Run `task work:cs` after a Mac reboot.
Connections wait up to 45 seconds for a successful SSH handshake after the local
forward opens. Only this harmless probe is retried; project operations run once.

An owned SSH alias in `~/.ssh/config` points Herdr at the worker’s dedicated live
forward. Its host key and private include stay in project state. Existing SSH
settings and Herdr configuration are preserved; nested remote attach uses only a
child-specific Herdr configuration.

Scratch synchronization remains two-way safe. Deletions propagate; simultaneous
conflicting changes remain for explicit resolution. The owned session’s identity
and endpoints are verified before it can be stopped or retargeted. Old bindings,
backups, and retired handoff data are retained privately.

Advanced commands remain available: `task scratch:configure -- --codespace NAME
--remote-root /workspaces/PROJECT`, `scratch:start`, `scratch:status`, `scratch:logs`,
`scratch:backup`, and `scratch:stop`. Prefer the daily `work` entrypoints.

See [validation evidence](project-continuity-validation.md) for the exercised live
workflow and failure cases.
