# Devcontainer setup

Configure a Git project with the runtimes and tools it needs. The terminal wizard
scans the repo, recommends a setup, and lets you review the diff before committing.
It can then push the branch and open a GitHub Codespace.

## Install in a project

Run this from your project's Git repository:

```sh
curl -fsSL https://raw.githubusercontent.com/emsilva/devcontainer/main/install.sh | sh -s --
```

You need Python 3.11+, Git, curl, and Bash. For Codespaces, also install the
GitHub CLI and sign in with `gh auth login`. The wizard downloads Gum when needed.
Install [Task](https://taskfile.dev) to use the `task` commands below.

The wizard asks you to:

1. Choose runtimes and tools from recommendations based on manifests, source files,
   workflows, and ignore rules. Ignore rules are supporting hints.
2. Apply dotfiles, enabled by default. Enter a repo such as `emsilva/dotfiles`;
   the wizard remembers it for your next project.
3. Keep scratch files and project notes synced with your Mac, enabled by default.
   This also synchronizes supported completed Codex and Claude conversations.
4. Choose commit-only setup or a Codespace. For a new Codespace, select its machine
   size and idle timeout, then open it in a browser or VS Code. Reuse shows the
   existing Codespace's actual settings.
5. Review the file diff and approve the commit, plus the push and launch if selected.

Every environment includes Python, uv, Codex, Claude Code, and OpenCode. The three
coding tools use their official native installers as `vscode`; Node is selected
when the project needs it. Dotfiles apply inside the container.

Your project receives the selected `.devcontainer` configuration, Feature lockfile,
small helpers, and a Taskfile include. Personal preferences live in
`${XDG_CONFIG_HOME:-$HOME/.config}/devcontainer-setup/config.json`.

## Use an installed project

| Command | Action |
|---|---|
| `codex` / `claude` | Start a native agent in the current checkout |
| `codex resume` / `claude --resume` | Choose a native conversation |
| `task work:cs` | Connect Codespace Herdr and sync scratch and project conversations |
| `task work:disconnect` | Disconnect this project's Codespace from the local Herdr UI |
| `task work:status` / `task work:stop` | Read offline sync summary / stop this project’s worker and its keeper |
| `task work:status -- --json` | Inspect cycle, receipt and publication detail |
| `task work:cs:armed` | Connect as `work:cs`, then arm restart after GitHub's 12-hour stop |
| `task work:hold -- 09:30` / `-- off` | Hold restarts and resume prompts until a local time / end the hold |
| `task work:disarm` | Stop this project's keeper and its keep-alive; sync continues |
| `task work:cs:stop` | Stop the Codespace and keep it stopped |
| `task work:cs:restart` | Restart the Codespace now; an armed project's lead is told to resume |
| `task work:logs` | Show recent keeper and worker logs, merged by time |
| `task devcontainer:setup` | Review or change the setup |
| `task devcontainer:setup -- --scan` | Show runtime and tool recommendations |
| `task devcontainer:setup -- --dry-run` | Preview the recommended file changes |
| `task devcontainer:check` | Validate the generated configuration |
| `task devcontainer:upgrade:self` | Review an upgrade to the latest wizard |
| `task devcontainer:build` | Build locally with Docker and the Dev Containers CLI |
| `task devcontainer:up` | Start the local devcontainer |
| `task devcontainer:status` | Show its container and ports |
| `task devcontainer:down` | Stop the local devcontainer |

Scan, dry-run, and check leave project files and preferences untouched. Installed
runners need network access to validate their pinned source cache. Setup preserves
existing project tasks, ports, and editor settings. Edits to managed helpers stop
an overwrite and need manual migration.

Inside the container, update all three coding tools without rebuilding:

```sh
task devcontainer:upgrade:agents
```

Fresh containers install the current vendor releases. Normal setup keeps valid
installations; the update command refreshes them explicitly.

## Work locally or in Codespaces

Use your normal checkout on each machine. Commit, push, pull, and change branches
with Git yourself. Project sync transfers no source files or Git index, and creates
no working copies.

On your Mac, connect with:

```sh
task work:cs
```

This starts the paired Codespace if needed, connects its saved machine in local
Herdr, and starts scratch and conversation sync. Select its Codespace group, then
run `codex` or `claude` in the existing project checkout. On your Mac, run the same
commands in your local checkout. Use `codex resume` or `claude --resume` to choose
a conversation through the native picker.

`task work:cs` always reconnects the paired Codespace. After setup replaces it, move
sync to the new one with `task work:cs -- --rebind <codespace>` (or
`task work:cs:armed -- --rebind <codespace>`); setup prints the exact command. The
old binding is paused and kept, not deleted.

Put temporary project files in `.scratch/` and shared notes in `.scratch/memory/`.
Scratch sync runs automatically in both directions; conflicts are preserved for
resolution. Project instructions tell agents to read shared notes. Claude's
project-local auto-memory directory also lives under scratch; global profiles,
settings, sign-in, databases, and personal memory libraries stay on each host.
Only scratch markers are tracked by Git.

Supported completed conversation turns are verified and published into the other
host's native session store with destination paths and new session IDs. Unchanged
imports are deduplicated. Continue a given conversation on one host at a time.
The worker exchanges each direction independently. Conversation publication
failures are reported while prepared scratch/SSH connections can open Herdr.
Unsupported native history formats are reported without altering their originals.

Keep this Mac awake and connected while working remotely. If Codespaces becomes
unavailable, your received scratch files, notes, and imported conversations remain
available locally. Code edits reach your Mac through Git, so commit and push the
code you want to keep. Transcripts do not restore uncommitted source files.

`task work:status` gives a short offline summary; `task work:status -- --json`
includes cycle and per-direction receipt/publication evidence. Received bytes,
verified native availability, pending and unsupported history are separate.
Partial or stale cycles cannot inherit earlier healthy status. Previous verified
receipts remain visible separately. `task work:disconnect` disconnects this project's saved
Codespace machine from Herdr while sync keeps running. `task work:stop` stops the
sync worker. Background reconnects cannot start a stopped Codespace; `task work:cs`
is the explicit start command. The one exception is the keeper of a project armed
with `task work:cs:armed`: it is the only background process that can start a
Codespace, and only after a stop it classified as GitHub's 12-hour stop, at most
2 restarts in 24 hours and 30 minutes apart. It then tells the project's `lead-*`
agent to resume. A stop you make with `task work:cs:stop`, or any other stop, is
never restarted. Arming refuses while another gh process, such as a project's own
`cs:ports` forward or a mirror script, holds this Codespace's link; stop it
first. Reconnects reuse the saved machine and workspace.

Older generated recovery checkouts and stored checkpoints remain untouched during
migration. Inspect them and move any desired code through Git before removing
them yourself. See [continuity details](docs/project-continuity.md).

## Resume a Codespace setup

If launch or provisioning fails after the push, use the Codespace name printed by
the wizard:

```sh
task devcontainer:setup -- --resume <codespace-name>
```

The retry reuses that Codespace. Your commit and push remain available. Private
dotfiles need repository access inside the destination container.

## Work on this repository

Configure another project from this checkout:

```sh
task setup -- --project /path/to/project
```

Run the development checks:

```sh
task check
task test
```

This repository's `.devcontainer` is the full polyglot catalog. `task build`,
`task up`, `task status`, and `task down` operate on that local container.
The [catalog reference](docs/reference.md) covers its tools, lifecycle, environment
variables, dotfiles, and SSH access. See [SECURITY.md](SECURITY.md) for secrets.

[MIT license](LICENSE).
