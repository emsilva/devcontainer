# Devcontainer setup

Configure a Git project with the runtimes and tools it needs, then work smoothly
across your Mac and GitHub Codespaces with native AI coding agents. The terminal
wizard scans the repo, recommends a setup, and lets you review the diff before
committing. It can then push the branch and open a GitHub Codespace.

## 5-minute mental model

| What | Where it lives | How it moves |
|---|---|---|
| **Source code, branches, commits** | Normal Git checkout on Mac and Codespace | **Manual Git** (`commit` / `push` / `pull`) — sync never touches source code |
| **Working notes & scratch files** (`.scratch/`, `.scratch/memory/`) | Main checkout `.scratch/` | **Automatic two-way safe sync** (conflicts preserved & shown in `task work`) |
| **Completed Codex & Claude chats** | Each host's native agent store | **Verified native publication** (`codex resume` / `claude --resume`) |
| **Antigravity (`agy`) chats** | Host-local SQLite + automatic read-only Markdown exports under `.scratch/history/agy/{codespace,mac}/` | Exported automatically into `.scratch/` (synced two-way); `AGENTS.md` directs `agy` to read the other host's `INDEX.md` + `.scratch/memory/` |
| **Grok & OpenCode chats** | Host-local only | Stay on each host; share context via `.scratch/memory/` |
| **Secrets, tokens & global configs** | Host-local / Codespaces Secrets | Never committed or synced |

## Mac prerequisites & bootstrap

Install these on your Mac before setting up a Codespace-paired project:

```sh
brew install python@3.13 git go-task gh
gh auth login --hostname github.com
gh auth refresh -s codespace
curl -fsSL https://herdr.dev/install.sh | sh
```

- **GitHub CLI (`gh`)**: version `2.102.0` or newer is required for project connections.
- **Codespaces SSH key (`~/.ssh/codespaces.auto`)**: run `gh codespace ssh` once against any Codespace so GitHub CLI generates the key pair before your first `task work:cs`.
- **Preflight check**: run `task setup:doctor` (in this repo) or `task devcontainer:doctor` (in a generated project) to verify every local prerequisite and get exact fix commands.

The wizard downloads Gum and pinned Mutagen automatically when needed.

## Install in a project

Run this from your project's Git repository:

```sh
curl -fsSL https://raw.githubusercontent.com/emsilva/devcontainer/main/install.sh | sh -s --
```

The wizard asks you to:

1. Choose additional runtimes (Ruby, Go, Rust) and tools from recommendations based on
   manifests, source files, workflows, and ignore rules. Ignore rules are supporting hints.
2. Apply dotfiles, enabled by default. Enter a repo such as `emsilva/dotfiles` (remembered
   for your next project), or leave blank and press Enter to skip dotfiles.
3. Keep scratch files and project notes synced with your Mac, enabled by default.
   This also synchronizes supported completed Codex and Claude conversations.
4. Choose commit-only setup or a Codespace. For a new Codespace, select its machine
   size and idle timeout, then open it in a browser or VS Code. Reuse shows the
   existing Codespace's actual settings.
5. Review the file diff and approve the commit, plus the push and launch if selected.

A Codespace starts from GitHub's default branch, so setup first checks that the
branch holds this project. When it holds fewer than half of the files tracked in
this checkout (files under `.devcontainer/` are not counted), setup prints a
refusal such as `GitHub's main holds 3 of the 212 files tracked on docs/x.` with
the fix: merge the project into `main`, then run the setup from a checkout on
`main`; or, from this Mac's `main`, push it to GitHub first. It exits without
asking, writing or pushing anything. A checkout that is only behind GitHub's
default branch always passes, and one without a GitHub `origin` is not checked.
To configure a **local-only** devcontainer on a feature branch without GitHub or a
Codespace, pass `--local-only` (or `--offline` to use a cached release without
fetching GitHub).

Every environment includes Python, Node, uv, Codex, Claude Code, OpenCode, Grok, and
Antigravity CLI (`agy`). The five coding tools use their official native installers as
`vscode`. Dotfiles apply inside the container.

Your project receives the selected `.devcontainer` configuration, Feature lockfile,
small helpers, and a Taskfile include. A project without a Taskfile gets a new
`Taskfile.yml` holding only that include and, with sync on, the `work` and
`scratch` tasks.
Setup never adds `up`, `down`, `status` or `build`: those names belong to the
project's own stack, and the container's tasks stay under `task devcontainer:`.
Personal preferences live in
`${XDG_CONFIG_HOME:-$HOME/.config}/devcontainer-setup/config.json`.

## Use an installed project

### Daily workflow

| Command | Action |
|---|---|
| `task work` | Interactive dashboard & menu (primary actions plus `More actions…` for logs, stop, hold, restart, disarm, disconnect); prints `Next:` commands without a terminal, and shows Mac worker reachability inside a Codespace |
| `codex` / `claude` / `opencode` / `grok` / `agy` | Start a native agent in the current checkout |
| `codex resume` / `claude --resume` | Choose a native conversation |
| `task work:cs` | Connect Codespace Herdr and sync scratch and project conversations |
| `task work:signin:all` | Start or retry sign-ins to every tool on the paired Codespace |
| `task work:signin` | Finish waiting Codespace browser sign-ins on this Mac |
| `task work:status` | Read offline sync, conflict, issue, and recovery summary (`-- --json` for full detail) |
| `task work:logs` | Show recent keeper and worker logs, merged by time |

### Codespace lifecycle & recovery

| Command | Action |
|---|---|
| `task work:cs:armed` | Connect as `work:cs`, then arm automatic restart after GitHub's 12-hour stop |
| `task work:hold -- 09:30` / `-- off` | Hold restarts and resume prompts until a local time / end the hold |
| `task work:disarm` | Stop this project's keeper and its keep-alive; sync continues |
| `task work:disconnect` | Disconnect this project's Codespace from the local Herdr UI |
| `task work:stop` | Stop this project's sync worker and its keeper |
| `task work:cs:stop` | Stop the Codespace and keep it stopped, from your Mac or through its connected Mac worker |
| `task work:cs:restart` | Restart the Codespace now; an armed project's lead is told to resume |
| `task work:cs:delete -- --codespace NAME` | Audit Git, Scratch, History, and Agents, then delete after typed `delete` |
| `task work:forget -- --agent claude --session ID` | Stop retrying a refused conversation (in either direction); keep every stored copy |

### Setup & local Docker

| Command | Action |
|---|---|
| `task devcontainer:doctor` | Check local Mac prerequisites and print exact fix commands |
| `task devcontainer:setup` | Review or change the setup (`-- --local-only` for local branch setup) |
| `task devcontainer:setup -- --scan` | Show runtime and tool recommendations |
| `task devcontainer:setup -- --dry-run` | Preview the recommended file changes |
| `task devcontainer:check` | Validate the generated configuration (falls back to cached release offline) |
| `task devcontainer:upgrade:self` | Review an upgrade to the latest wizard |
| `task devcontainer:build` / `up` / `status` / `down` | Build, start, inspect, or stop the local devcontainer with Docker |

Scan, dry-run, doctor, and check leave project files and preferences untouched.
Read-only commands (`--check`, `--scan`, `--dry-run`, `--doctor`) and `--offline`
fall back to the cached generator release if GitHub is unreachable. Setup preserves
existing project tasks, ports, and editor settings. Edits to managed helpers stop
an overwrite and need manual migration.

Inside the container, update all five coding tools without rebuilding:

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

After connecting, run `task work:signin:all` to sign in to Plaud, Codex, Claude,
OpenCode, Grok and Antigravity. Approve each page on this Mac; Claude uses the
account you choose in the browser, while Antigravity (`agy acp --login`) prints
a Google OAuth URL in the terminal to open on this Mac and paste back the code
or callback URL. Existing sign-ins are kept, and a retry starts only the
tools confirmed signed out. To connect and sign in before Herdr opens, run
`task work:cs -- --then-signin-all`. Use `task work:signin` to finish waiting
sign-ins when the worker is disconnected. See [sign-ins](docs/project-continuity.md#sign-ins).

`task work:cs` always reconnects the paired Codespace. To replace it, run
`task setup -- --replace` in this repository, or `task devcontainer:setup -- --replace`
in a generated project. You can also choose `Replace this Codespace` in `task work`,
or `replace NAME` in the setup wizard. Replacement reads its live machine and idle
timeout, then asks `Replace NAME?` with the new one's machine, branch, billing owner
and what you will be asked to do. `Not now` is the default; it, Esc and Ctrl-C
print `Nothing was replaced.` and leave nothing behind. After `Replace it`, it
creates and validates the new Codespace from the default branch, records the old
one's final sync, then moves sync, recovery and Herdr. It signs in and checks the
new one before offering the old one's checked delete. Recovery consent is kept when
the GitHub account and billing owner match; otherwise it asks you to type `arm`
before the move. Rerun the same replace command to continue an unfinished replace:
it does not ask again, and before the move it offers `Continue the replace` or to
abandon it. See [Replace a Codespace](docs/project-continuity.md#replace-a-codespace).

During replacement, Herdr's connection to the old Codespace ends. When the new
one lacks the Herdr version your Mac runs, it asks to install it there. If you
keep another version that Herdr accepts, it does not ask again for that pair.
A plain `task work:cs -- --rebind NAME` moves sync and recovery to that Codespace;
the old binding is paused and kept, not deleted.

Replace a Codespace first, then run `task work:cs:delete -- --codespace <old-codespace>`.
It verifies the GitHub account recorded when the Codespace was connected or armed,
checks the old Codespace and deletes only after you type `delete` in a terminal.
See [checked deletion](docs/project-continuity.md#checked-deletion) for its scope.
Keep anything you would need again in `.scratch/`: working files in `.scratch/tmp/`
and shared notes in `.scratch/memory/`.
Leave regenerable output such as screenshots and builds in `tmp/`, because scratch
sync copies everything it holds. Project instructions tell agents the same,
including agents working in Git worktrees. Scratch sync runs automatically in both directions; conflicts are preserved for
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
