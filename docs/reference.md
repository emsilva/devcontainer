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
- **Native user coding tools (post-create):** Claude Code, OpenAI Codex, OpenCode, Grok, Antigravity CLI (`agy`).
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
packages can still change. Codex, Claude Code, OpenCode, Grok, and Antigravity CLI
(`agy`) use official native user installers during post-create, with complete
packages owned by `vscode`. Their ordinary vendor release channels deliberately
move; OpenCode uses its pinned v2 release channel. Agents cannot be deselected;
legacy false preferences are accepted and ignored. They do not require the Node
runtime. Normal setup keeps valid user installations;
`task devcontainer:upgrade:agents` explicitly updates all five.
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

## Lifecycle: Canonical Catalog vs. Generated Projects

### Canonical catalog (this repository's `.devcontainer/`)

- **`onCreateCommand` → `scripts/on-create.sh`** (prebuild-cacheable, secret-free):
  corepack, crane, vivid, Rails, chezmoi, neovim, ast-grep, herdr, claude-swap (`cswap` + its
  login-activated `cswap-switch` service), a minimal default `~/.zshrc` (replacing the base
  image's oh-my-zsh one), and (opt-in) man-page restore.
- **`postCreateCommand` → `scripts/post-create.sh`** (per-user, has secrets): native coding tools
  (Codex, Claude Code, OpenCode, Grok, Antigravity CLI), `open-on-mac`, cache/volume permissions,
  default `zsh` shell, timezone, git config, `gh` auth via `PERSONAL_PAT`, SSH authorized keys,
  and chezmoi dotfiles.
- **`postStartCommand` → `scripts/post-start.sh`** (each start): history volume, a
  once-weekly tldr refresh.

Heavy installs live in `onCreate` so Codespaces **prebuilds** snapshot them; per-user work
stays in `postCreate`. Restoring man pages (`apt` + `unminimize`, slow) is **opt-in** via
`DEVCONTAINER_RESTORE_MANPAGES=1`.

### Generated projects (rendered via `task setup` / `install.sh`)

- **`postCreateCommand` → `.devcontainer/scripts/post-create-selected.sh`**: loads local
  `.devcontainer/devcontainer.env` when present, sets `zsh` login shell, applies `DEVCONTAINER_TZ`
  when set, authenticates `gh` with `PERSONAL_PAT` when provided, configures system `PATH` and
  `BROWSER` (`system-path.sh`), installs the five native agents (`install-agents.sh`), installs
  `open-on-mac` / `ccswap` / `plaud` (`install-tools.sh`), preinstalls `herdr` if absent, runs
  `validate-selected.sh`, and applies chezmoi dotfiles when enabled.
- Generated projects intentionally omit the heavy canonical-only `on-create.sh` / `post-start.sh`
  extras (neovim, Rails, crane, vivid, ast-grep, `cswap-switch`) unless added via polyglot/dotfiles.

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

| Issue | Cause & Fix |
|---|---|
| Missing `task`, `gh`, `herdr`, or `~/.ssh/codespaces.auto` | Run `task setup:doctor` (or `task devcontainer:doctor`). For `~/.ssh/codespaces.auto`, run `gh codespace ssh` once against any Codespace to bootstrap the key. |
| `GitHub CLI 2.102.0 or newer is required` | Upgrade `gh` on your Mac with `brew upgrade gh`. |
| `HTTP 401` / `HTTP 403` in `task work:status` or keeper logs | Token expired or missing Codespaces scope. Run `gh auth login --hostname github.com` and `gh auth refresh -s codespace`, and unset ambient `GH_TOKEN` / `GITHUB_TOKEN` if overriding your keyring login. |
| `.scratch/` sync conflicts | `task work:cs` still opens Herdr with a warning banner; `task work` and `task work:status` list the exact conflicting paths under `.scratch/`. Resolve the files and Mutagen resumes automatically. |
| Post-restart `no single lead` or `waiting for an answer` | Ensure exactly one primary orchestrator pane is named `lead-<project>` in Herdr. If a restored agent sits on a workspace trust/approval dialog, answer it in its pane. For `agy` `/teamwork`, record the top-level entry-root conversation ID and service ports in `.scratch/restart-resume.json`. |
| `task devcontainer:check` while offline | Read-only commands (`--check`, `--scan`, `--dry-run`, `--doctor`) and `--offline` automatically fall back to your cached release under `${XDG_CACHE_HOME:-$HOME/.cache}/devcontainer-setup/releases/`. |
| Setup refused on a feature branch | Codespaces boot from GitHub's default branch. Merge first for a Codespace, or pass `--local-only` to configure a local devcontainer on the current branch. |
| `Mutagen socket path is too long` on macOS | Automatically falls back to a private `0700` directory under `/tmp/dc-m<uid>/`. |
| Docker-in-Docker `iptables: Table does not exist` | Some hosts lack legacy `ip_tables`; `modprobe iptable_nat` or switch to the nft backend. Codespaces is fine. |
| Dotfiles not applied | Set `DOTFILES_REPO` (secret/env) or run `load-dotfiles <repo>`. |
| Python deps missing | `uv sync`, or `pip install -r requirements.txt`. |
| Port didn't open a browser | Expected, forwarding only notifies. Use the Ports view. |

## Safety / secrets

- Read the [security guidance](../SECURITY.md) before adding secrets.
- `.devcontainer/devcontainer.env` (local secrets) is gitignored.
