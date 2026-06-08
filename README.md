# Polyglot Devcontainer

An Ubuntu 24.04 dev container for everyday polyglot work (Go, Python, Node, Ruby, Rust)
that prefers official Dev Container features over custom bootstrap scripts, pins versions
for reproducibility, and applies **your own** shell config via [chezmoi](https://chezmoi.io).
Built for **GitHub Codespaces** first; also works locally via the Dev Containers CLI / VS Code.

## What you get

- **Base image:** `mcr.microsoft.com/devcontainers/base:ubuntu-24.04`
- **Languages & runtimes** (pinned major.minor — patch floats, majors are bumped deliberately):
  - Go `1.26`
  - Node.js `24` (Active LTS) + Yarn & pnpm (Corepack)
  - Python `3.13` (+ `uv` for fast envs/deps)
  - Ruby `3.4` (RVM, via the ruby feature) + a pinned Rails
  - Rust (cargo + rustup)
- **CLIs & tooling via features:** AWS / Azure / Google Cloud CLIs, kubectl + Helm,
  GitHub CLI, Docker-in-Docker (compose v2), `go-task`, `fzf`, starship, SSHD,
  Claude Code, OpenAI Codex, `uv`, common-utils (zsh).
- **Shell tools via `apt-packages`:** bat, ripgrep, fd, tldr, tree, jq, yq, httpie, zoxide,
  git, git-lfs, DB clients (postgres/mysql/sqlite/redis), htop, ncdu, shellcheck, …
- **Editor:** neovim (pinned static build) — for chezmoi dotfiles that use it (e.g. LazyVim).
- **Dotfiles manager:** chezmoi (bring your own — see below).
- Default branch `main`, persistent shell-history volume, dynamic port forwarding (notify).

## Reproducibility

Tool versions are pinned in `devcontainer.json`, and feature artifacts are pinned by
sha256 digest in `devcontainer-lock.json` (committed). Rebuilds and Codespaces prebuilds no
longer silently jump versions.

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
  corepack, crane, vivid, Rails, chezmoi, neovim, and (opt-in) man-page restore.
- **`postCreateCommand` → `scripts/post-create.sh`** (per-user, has secrets): cache/volume
  perms, default shell, timezone, git config, gh auth, and the chezmoi dotfiles apply.
- **`postStartCommand` → `scripts/post-start.sh`** (each start): history volume, a
  once-weekly tldr refresh.

Heavy installs live in `onCreate` so Codespaces **prebuilds** snapshot them; per-user work
stays in `postCreate`. Restoring man pages (`apt` + `unminimize`, slow) is **opt-in** via
`DEVCONTAINER_RESTORE_MANPAGES=1`.

## Bootstrap & update

Pull this `.devcontainer` into any clean git repo:

```bash
curl -fsSL https://raw.githubusercontent.com/emsilva/devcontainer/main/install.sh | sh
```

Common tasks:

```bash
task devcontainer:rebuild           # recreate the container (Dev Containers CLI)
task devcontainer:upgrade:self      # refresh .devcontainer from the canonical repo
task devcontainer:upgrade:lock      # bump feature versions in the lockfile (review + commit)
task devcontainer:upgrade:codex     # update the Codex CLI without a rebuild
task devcontainer:setup:dotfiles -- <repo>
```

`task devcontainer:upgrade:codex` installs the latest stable Codex by default; override with
`CODEX_ALLOW_PRERELEASES=true` or `CODEX_FORCE_VERSION=0.42.0`.

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
