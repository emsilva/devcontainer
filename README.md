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
   This also enables Codex and Claude conversation handoff.
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
| `task work` | Continue a conversation here or in its Codespace |
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

## Keep project work across Mac and Codespace

Keep **Sync scratch files and project notes with your Mac** enabled in setup. Put temporary
project files in `.scratch/` and shared working notes in `.scratch/memory/`.
Only the folder's README and ignore rules are committed. Setup adds instructions
to `AGENTS.md` and `CLAUDE.md` while preserving your existing guidance.

Run this on your Mac for daily work:

```sh
task work
```

Choose **Continue on this Mac**, **Continue in Codespace**, or start a new Codex
or Claude conversation. The menu remembers your selection and shows conversations
by title and last activity. Moving to the Codespace opens an interactive remote
terminal; exit the agent to return to the menu. Use the Mac menu to bring it back.

Setup connects scratch sync to the Codespace you selected. With commit-only
setup, the menu helps you connect one later. It discovers matching checkouts and
asks when a choice is needed. A stopped Codespace starts from an explicitly named
menu action. If your project already defines `work`, use `task devcontainer:work`.

Sync runs in the background, waits when the Codespace stops, and restarts when
you run `task work` after a Mac reboot. Deletions sync too; conflicts preserve
both versions and need resolution before switching. Private state stays outside Git.

Source code still moves through Git. Before switching machines, keep both
checkouts on the same branch and commit, with clean working trees. The launcher
checks this, waits for scratch synchronization, and transfers the selected
conversation after its writer exits. Native profiles and authentication stay on
each host. Existing conversations need explicit adoption; unsupported histories
remain preserved. See [advanced commands and recovery](docs/project-continuity.md).

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
