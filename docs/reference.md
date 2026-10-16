# Full catalog reference

This describes the full Ubuntu 24.04 preset in this repository. Generated projects
use a selected subset. See the [README](../README.md) for installation and project commands.

- **Base image:** `mcr.microsoft.com/devcontainers/base:ubuntu-24.04`
- **Languages & runtimes** (pinned major.minor; patches float and major upgrades are deliberate):
  - Go `1.26`
  - Node.js `24` + Yarn & pnpm (Corepack)
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
  it starts with your first shell because lifecycle-spawned daemons do not survive in Codespaces).
- **Editor:** neovim (pinned static build) for dotfiles such as LazyVim.
- **Dotfiles manager:** chezmoi (bring your own, see below).
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
stops with a migration error and preserves the existing files.
Generated Feature metadata records supported pinning limitations; native agent
versions are intentionally managed by their vendors.

- Deliberately refresh the pins: `task devcontainer:upgrade:lock` (review the diff, commit).
- Dependabot opens weekly PRs to bump feature + GitHub Action versions (`.github/dependabot.yml`).

## Dotfiles (chezmoi)

The catalog includes chezmoi. Supply your dotfiles repository:

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
| `SSH_AUTHORIZED_KEYS` | Public key(s) authorized for the in-container sshd on port 2222, see [SSH access](#ssh-access) |

Codespaces injects these secrets directly. `${localEnv:}` resolves empty there
and can blank their values.

## Lifecycle

- **`onCreateCommand` → `scripts/on-create.sh`** (prebuild-cacheable, secret-free):
  corepack, crane, vivid, Rails, chezmoi, neovim, ast-grep, herdr, claude-swap (+ its
  login-activated cswap-switch service), a minimal default `~/.zshrc` (replacing the base
  image's oh-my-zsh one), and (opt-in) man-page restore.
- **`postCreateCommand` → `scripts/post-create.sh`** (per-user, has secrets): native coding tools, cache/volume
  permissions, default shell, timezone, git config, gh auth, and chezmoi dotfiles.
- **`postStartCommand` → `scripts/post-start.sh`** (each start): history volume, a
  once-weekly tldr refresh.

Heavy installs live in `onCreate` so Codespaces **prebuilds** snapshot them; per-user work
stays in `postCreate`. Restoring man pages (`apt` + `unminimize`, slow) is **opt-in** via
`DEVCONTAINER_RESTORE_MANPAGES=1`.

## Git identity

In Codespaces your Git identity is auto-configured. Locally, set it explicitly:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

If `gh` picks the wrong account: `unset GH_TOKEN GITHUB_TOKEN GH_AUTH_TOKEN`, then `gh auth login`.

## SSH access

Two ways to get a shell into the container:

Use your GitHub CLI login for a Codespaces shell:

```bash
gh codespace ssh                      # pick a codespace
gh codespace ssh -c <codespace-name>  # a specific one
```

For raw SSH on port 2222 (JetBrains Gateway, Remote-SSH, or rsync), provide your public key via
`SSH_AUTHORIZED_KEYS` (a Codespaces secret, or `devcontainer.env` locally); `post-create`
installs it to `~/.ssh/authorized_keys`. Then connect:

```bash
# Codespaces, forward 2222, then connect
gh codespace ports forward 2222:2222 &
ssh -p 2222 vscode@localhost

# Local devcontainer in VS Code, 2222 is auto-forwarded
ssh -p 2222 vscode@localhost
```

If you only ever use `gh codespace ssh`, the `sshd` feature is optional and can be removed.

## CI & validation

- [ci.yml](../.github/workflows/ci.yml) runs ShellCheck and the wizard tests.
- [devcontainer-build.yml](../.github/workflows/devcontainer-build.yml) builds the full
  catalog image and verifies its toolchain when `.devcontainer` changes.
- Inside the container: `bash .devcontainer/scripts/validate-devcontainer.sh` reports tool
  versions and PATH consistency across shells. From the host:
  `bash .devcontainer/scripts/validate-host.sh`.

## Troubleshooting

| Issue | Fix |
|---|---|
| Docker-in-Docker `iptables: Table does not exist` | Some hosts lack legacy `ip_tables`; `modprobe iptable_nat` or switch to the nft backend. Codespaces is fine. |
| Dotfiles not applied | Set `DOTFILES_REPO` (secret/env) or run `load-dotfiles <repo>`. |
| Python deps missing | `uv sync`, or `pip install -r requirements.txt`. |
| Port didn't open a browser | Expected, forwarding only notifies. Use the Ports view. |

## Safety / secrets

- Read the [security guidance](../SECURITY.md) before adding secrets.
- `.devcontainer/devcontainer.env` (local secrets) is gitignored.
