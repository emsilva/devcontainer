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
   This also enables recovery of supported completed Codex and Claude turns.
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
| `task work` | Open local project work in Herdr |
| `task work:codex` / `task work:claude` | Start a new conversation here |
| `task work:cs` | Open Codespace Herdr and back up work to this Mac |
| `task work:status` / `task work:stop` | Check protection / stop this project’s worker |
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

## Keep working when Codespaces is unavailable

Enable project continuity in setup. On your Mac, run:

```sh
task work:cs
```

This starts the paired Codespace if needed, opens its persistent Herdr session,
and starts a Mac worker. Work in Codex or Claude there. The worker keeps verified
checkpoints of committed history, staged and unstaged code, nonignored new files,
`.scratch/`, project notes, and supported completed conversation turns on your Mac.
The first entry seeds a separate remote checkout from your desktop; existing
checkouts and their edits are preserved.

When credits run out or the Codespace is unreachable, run:

```sh
task work
```

Herdr opens a separate local recovery checkout from the last checkpoint actually
received on this Mac. No working Codespace or remote release is required. Your
original Mac checkout stays intact. Repeated entry reuses the recovery checkout,
preserving new local edits.

Use `task work:codex` or `task work:claude` for a fresh conversation in the active
local workspace. Add `-- --resume` to choose a project conversation. Across hosts,
a native fork continues the checkpoint under a new ID; source histories remain
intact. Native profiles and sign-in stay on each host.

Put temporary files in `.scratch/` and shared project notes in `.scratch/memory/`.
Scratch uses automatic two-way safe sync; conflicts preserve both versions and
are reported. Setup adds project-memory guidance to `AGENTS.md` and `CLAUDE.md`.
Only scratch markers are tracked by Git.

Keep the Mac awake and connected while working remotely. `task work:status`
reports the actual last received checkpoint and any unsupported conversations;
scratch synchronization alone does not certify code or conversation protection.
Use `task work:stop` to stop this project’s worker. See
[recovery details and limitations](docs/project-continuity.md).

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
