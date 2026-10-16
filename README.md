# Project-aware Devcontainer

A terminal wizard that recommends the tools a project needs, generates a selective
devcontainer, and asks before committing. It can also push the setup branch and
create/open a GitHub Codespace after the same explicit review.

From this checkout, configure another project:

```bash
task setup -- --project /path/to/project
```

For first installation from another project, run:

```bash
curl -fsSL https://raw.githubusercontent.com/emsilva/devcontainer/main/install.sh | sh
```

The wizard uses Python 3.11+, Git, and curl on the host. Gum is reused when
installed, otherwise a pinned, checksum-verified binary is cached under your user
cache directory. GitHub CLI and authentication are needed for Codespaces launch.

## Setup wizard

- Detects nested Python, Ruby, Go, Node, and Rust projects from manifests, runtime
  declarations, source patterns, CI, and workflow commands. Ignore rules are weak
  hints, never sufficient to select another language on their own.
- Includes Python and `uv` in every environment for project and agent scratch work.
- Includes Codex, Claude Code, and OpenCode in every environment.
- Starts with dotfiles enabled. Enter `emsilva/dotfiles` or your own repository; it is prefilled next time you configure another project.
- Stores personal preferences in
  `${XDG_CONFIG_HOME:-$HOME/.config}/devcontainer-setup/config.json`, outside Git.
  Dotfiles apply inside the development container, not on the host.
- Fetches eligible machine sizes for the selected GitHub repository and offers an
  idle timeout, browser/VS Code opening, and optional region/retention settings.
- Shows the exact file diff before approval. Commits only the setup files,
  preserves unrelated work, and stops if hooks change reviewed content.
- Keeps a selected configuration plus its Feature lockfile and small helpers in
  the project. The wizard itself stays in the versioned user cache.

Installed projects use these commands:

```bash
task devcontainer:setup
task devcontainer:check
task devcontainer:upgrade:self
task devcontainer:setup -- --resume <codespace-name>
```

Installed project runners need network access to fetch the pinned source and validate the existing cache; modified cached source is preserved and refused.

Read-only commands work without a terminal and do not change the project or save
preferences:

```bash
task setup -- --project /path/to/project --scan
task setup -- --project /path/to/project --dry-run
task setup -- --project /path/to/project --check
```

Re-running setup uses the recorded selections. Upgrades are deliberate and keep
custom project ports, hooks, and editor settings. Existing unmanaged configurations
require explicit adoption; local edits to managed helpers stop an overwrite.

The wizard honors exact pnpm versions from `packageManager` and `engines.pnpm`
(for example, `pnpm@9.15.4`) and checks that version inside the container.
Incompatible nested declarations require an explicit version choice. Explicit
npm, Yarn, or Bun version overrides currently stop setup with a conflict: the
locked installation does not support those overrides. Review the project
requirement or add a separately verified installer before continuing.

Commit-only mode performs no push or Codespaces allocation. Launch mode explicitly
confirms the branch, push, machine, and opening action; commits and pushes survive
a later launch failure, and the retry reuses the recorded codespace. Personal
settings and launch receipts stay local. Private dotfiles need working repository
access inside the destination container.

## Development

```bash
task test     # fixture tests, including real temporary Git repositories
task check    # Python syntax, ShellCheck, whitespace
task build    # build the canonical development container; needs Dev Containers CLI
task up
task status
task down
```

The canonical `.devcontainer` below is the broad catalog and explicit polyglot
preset. The setup wizard selects a smaller subset for each consuming project.

An Ubuntu 24.04 dev container for everyday polyglot work (Go, Python, Node, Ruby, Rust)
that prefers official Dev Container features over custom bootstrap scripts, pins versions
for reproducibility, and applies **your own** shell config via [chezmoi](https://chezmoi.io).
Built for **GitHub Codespaces** first; also works locally via the Dev Containers CLI / VS Code.

## Polyglot catalog

- **Base image:** `mcr.microsoft.com/devcontainers/base:ubuntu-24.04`
- **Languages & runtimes** (pinned major.minor — patch floats, majors are bumped deliberately):
  - Go `1.26`
  - Node.js `24` (Active LTS) + Yarn & pnpm (Corepack)
  - Python `3.13` (+ `uv` for fast envs/deps)
  - Ruby `3.4` (RVM, via the ruby feature) + a pinned Rails
  - Rust (cargo + rustup)
- **CLIs & tooling via features:** AWS / Azure / Google Cloud CLIs, kubectl + Helm,
  GitHub CLI, Docker-in-Docker (compose v2), `go-task`, `fzf`, starship, SSHD,
  `uv`, common-utils (zsh).
- **Native user coding tools (post-create):** Claude Code, OpenAI Codex, OpenCode.
- **Shell tools via `apt-packages`:** bat, ripgrep, fd, tldr, tree, jq, yq, httpie, zoxide,
  git, git-lfs, git-delta, DB clients (postgres/mysql/sqlite/redis), htop, ncdu, shellcheck, …
- **Extra tooling (pinned, via on-create):** ast-grep (npm), herdr, claude-swap (`cswap`,
  with a login-activated service that keeps Claude Code on the account with the most quota;
  it starts with your first shell — lifecycle-spawned daemons don't survive in Codespaces).
- **Editor:** neovim (pinned static build) — for chezmoi dotfiles that use it (e.g. LazyVim).
- **Dotfiles manager:** chezmoi (bring your own — see below).
- Default branch `main`, persistent shell-history volume, dynamic port forwarding (notify).

## Reproducibility

Feature artifacts are pinned by SHA256 in `devcontainer-lock.json`. Generated
configurations also pin supported installer options. A Feature lock pins its
installer, not everything it downloads: some bundled tools and operating-system
packages can still change. Codex, Claude Code, and OpenCode use official native
user installers during post-create, with complete packages owned by `vscode`.
Their ordinary vendor release channels deliberately move; OpenCode uses its
default installer channel without forcing a major upgrade. Agents cannot be
deselected; legacy false preferences are accepted and ignored. They do not
require the Node runtime. Normal setup keeps valid user installations;
`task devcontainer:upgrade:agents` explicitly updates all three.
`bash .devcontainer/scripts/install-agents.sh --check` verifies native packages
and launchers without downloading or sending model requests. User launcher
paths precede system paths through VS Code's `remoteEnv` and explicit lifecycle
and validation exports. Raw SSH sessions do not inherit `remoteEnv`: run
`export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"` before using the agents,
or invoke their absolute user launcher paths. The helper and validators export
these paths themselves, including when called over SSH. Setup does not rewrite
personal shell profiles or SSH configuration. Adopting a different remote user,
a HOME override outside `/home/vscode`, or a PATH that hides the native launchers
stops with a reviewed-migration error and preserves the existing files.
Generated Feature metadata records supported pinning limitations; native agent
versions are intentionally managed by their vendors.

- Deliberately refresh the pins: `task devcontainer:upgrade:lock` (review the diff, commit).
- Dependabot opens weekly PRs to bump feature + GitHub Action versions (`.github/dependabot.yml`).

## Dotfiles (chezmoi)

The container ships the chezmoi binary but **no personal dotfiles** — apply your own:

```bash
load-dotfiles <user>/<repo>                  # e.g. load-dotfiles emsilva/dotfiles
task devcontainer:setup:dotfiles -- <repo>   # equivalent task form
```

With no argument these use `$DOTFILES_REPO`, else prompt. Under the hood it runs
`chezmoi init --apply <repo>`. Set `DOTFILES_REPO` (Codespaces secret or local
`devcontainer.env`) to **auto-apply** during `postCreate`.

## Secrets & environment

In **Codespaces**, set these as **Codespaces secrets** (Settings → Codespaces → Secrets);
they are injected automatically. For **local** runs, copy
`.devcontainer/devcontainer.env.example` → `.devcontainer/devcontainer.env` (gitignored).

| Variable | Purpose |
|---|---|
| `PERSONAL_PAT` | GitHub PAT for cross-repo git/gh auth (post-create runs `gh auth login`) |
| `DOTFILES_REPO` | chezmoi dotfiles repo to auto-apply on create |
| `DEVCONTAINER_TZ` | Container timezone (default `America/Sao_Paulo`) |
| `SSH_AUTHORIZED_KEYS` | Public key(s) authorized for the in-container sshd on port 2222 — see [SSH access](#ssh-access) |

> Secrets are intentionally **not** wired through `${localEnv:}` — in Codespaces that
> resolves empty and would blank the injected secret.

## Lifecycle

- **`onCreateCommand` → `scripts/on-create.sh`** (prebuild-cacheable, secret-free):
  corepack, crane, vivid, Rails, chezmoi, neovim, ast-grep, herdr, claude-swap (+ its
  login-activated cswap-switch service), a minimal default `~/.zshrc` (replacing the base
  image's oh-my-zsh one), and (opt-in) man-page restore.
- **`postCreateCommand` → `scripts/post-create.sh`** (per-user, has secrets): cache/volume
  perms, default shell, timezone, git config, gh auth, and the chezmoi dotfiles apply.
- **`postStartCommand` → `scripts/post-start.sh`** (each start): history volume, a
  once-weekly tldr refresh.

Heavy installs live in `onCreate` so Codespaces **prebuilds** snapshot them; per-user work
stays in `postCreate`. Restoring man pages (`apt` + `unminimize`, slow) is **opt-in** via
`DEVCONTAINER_RESTORE_MANPAGES=1`.

## Bootstrap & update

Run the wizard from a Git project (unrelated unstaged edits may remain):

```bash
curl -fsSL https://raw.githubusercontent.com/emsilva/devcontainer/main/install.sh | sh
```

Common tasks:

```bash
task devcontainer:rebuild           # recreate the container (Dev Containers CLI)
task devcontainer:upgrade:self      # refresh .devcontainer from the canonical repo
task devcontainer:upgrade:lock      # bump feature versions in the lockfile (review + commit)
task devcontainer:upgrade:agents    # update all native coding tools without a rebuild
task devcontainer:setup:dotfiles -- <repo>
```

The compatibility script `upgrade-codex.sh` also delegates to the shared native
agent update helper; it no longer installs a system-wide standalone binary.

## Git identity

In Codespaces your Git identity is auto-configured. Locally, set it explicitly:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

If `gh` picks the wrong account: `unset GH_TOKEN GITHUB_TOKEN GH_AUTH_TOKEN`, then `gh auth login`.

## SSH access

Two ways to get a shell into the container:

**`gh codespace ssh` (recommended for Codespaces)** — a GitHub-brokered tunnel; auth is your
GitHub login, nothing to configure:

```bash
gh codespace ssh                      # pick a codespace
gh codespace ssh -c <codespace-name>  # a specific one
```

**Raw SSH to the in-container sshd (port 2222)** — for tools/IDEs that need a real SSH
endpoint (JetBrains Gateway, Remote-SSH, rsync, …). Provide your **public** key via
`SSH_AUTHORIZED_KEYS` (a Codespaces secret, or `devcontainer.env` locally); `post-create`
installs it to `~/.ssh/authorized_keys`. Then connect:

```bash
# Codespaces — forward 2222, then connect
gh codespace ports forward 2222:2222 &
ssh -p 2222 vscode@localhost

# Local devcontainer in VS Code — 2222 is auto-forwarded
ssh -p 2222 vscode@localhost
```

If you only ever use `gh codespace ssh`, the `sshd` feature is optional and can be removed.

## CI & validation

- `.github/workflows/ci.yml` runs on PRs: **ShellCheck** over all scripts + a
  `devcontainers/ci` build that verifies the toolchain.
- Inside the container: `bash .devcontainer/scripts/validate-devcontainer.sh` reports tool
  versions and PATH consistency across shells. From the host:
  `bash .devcontainer/scripts/validate-host.sh`.

## Troubleshooting

| Issue | Fix |
|---|---|
| Docker-in-Docker `iptables: Table does not exist` | Some hosts lack legacy `ip_tables`; `modprobe iptable_nat` or switch to the nft backend. Codespaces is fine. |
| Dotfiles not applied | Set `DOTFILES_REPO` (secret/env) or run `load-dotfiles <repo>`. |
| Python deps missing | `uv sync`, or `pip install -r requirements.txt`. |
| Port didn't open a browser | Expected — forwarding only notifies. Use the Ports view. |

## Safety / secrets

- No real secrets are committed — see [`SECURITY.md`](SECURITY.md).
- `.devcontainer/devcontainer.env` (local secrets) is gitignored.

## Future ideas

- Optional Redis/Postgres services via `docker-compose.yml` if databases become standard.
- Trim cloud CLIs / features you don't use to slim the image and speed builds.

---
Licensed under [MIT](LICENSE). Lightweight, batteries included — tweak to taste.
