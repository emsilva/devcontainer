# Devcontainer Workspace

A handy Ubuntu 24.04 development container setup for everyday polyglot work (Go, Python, Node) that prefers official Dev Container features over custom bootstrap scripts.

## What You Get

- Base image: `mcr.microsoft.com/devcontainers/base:ubuntu-24.04`
- Languages & runtimes via features:
  - Go (latest from feature)
  - Node.js (latest from feature, with Yarn & pnpm via Corepack)
  - Python (latest from feature, with the `uv` feature for fast env + deps)
- Tooling & CLI via features:
  - Common utils + zsh/Oh My Zsh (`common-utils` feature)
  - `fzf` via `ghcr.io/devcontainers-extra/features/fzf`
  - Claude Code CLI via `ghcr.io/anthropics/devcontainer-features/claude-code`
  - OpenAI Codex CLI via `ghcr.io/jsburckhardt/devcontainer-features/codex`
  - Pulumi CLI via `ghcr.io/devcontainers-extra/features/pulumi`
  - Azure CLI (`ghcr.io/devcontainers/features/azure-cli`)
  - Google Cloud CLI (`ghcr.io/dhoeric/features/google-cloud-cli`)
  - Shell tools via `apt-packages`: fd, ripgrep, bat, tldr, tree, jq, yq, httpie, zoxide, git, git-lfs, DB clients, etc.
  - Starship prompt (`starship` feature) with Catppuccin config
  - kubectl + Helm (`kubectl-helm` feature)
  - AWS CLI and GitHub CLI features
  - Task (`go-task`) CLI via `ghcr.io/devcontainers-extra/features/go-task`
  - Container tuned for iterative dev: persistent shell history volume, trimmed noise, default branch = `main`.

## Devcontainer UX niceties

- VS Code settings: format on save, Ruff for Python, Prettier/ESLint for JS/TS
- Default terminal: `zsh`
- History persisted across rebuilds (`/commandhistory` volume)
- Symlinked user dotfiles from `.devcontainer/user` into `$HOME`
- Unified environment setup: a single `env-base.sh` shared by login/non-login shells keeps PATH consistent
- Port forwarding is now fully dynamic: no predeclared list; VS Code will just notify when a process starts listening (configured with `onAutoForward=notify`).

## AI / MCP Configuration (Claude & Codex)

Both Claude and Codex configs are generated from templates stored under `.devcontainer/user/` and then symlinked into `$HOME` during provisioning:

- Claude: `.devcontainer/user/.claude.json.template` → generates `.devcontainer/user/.claude.json` → symlinked to `~/.claude.json`.
- Codex: `.devcontainer/user/.codex/config.toml.template` → generates `.devcontainer/user/.codex/config.toml` → symlinked to `~/.codex/config.toml`.

Placeholder tokens (`__CONTEXT7_API_KEY__`, `__EXA_API_KEY__`) are replaced at container create time when the corresponding environment variables are set. Generated files are ignored by git. Remove the generated file(s) and rebuild to force regeneration.

## Expected Environment Variables (set as GitHub Codespace / repository secrets or local env when launching)

| Variable | Purpose | Required? |
|----------|---------|-----------|
| `CONTEXT7_API_KEY` | Enables Context7 MCP server integration | Optional |
| `EXA_API_KEY` | Enables Exa search MCP server integration | Optional |

These are referenced in `.devcontainer/devcontainer.json` under `remoteEnv` so they can be wired in from your local environment or repository-level secrets.

## Git Identity

In GitHub Codespaces your Git identity (user.name / user.email) is auto-configured by the platform. Locally (or if you want to override the defaults) you can set or adjust them explicitly:
```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```
To view the current values:
```bash
git config --get user.name
git config --get user.email
```

### Troubleshooting GitHub CLI authentication

If the GitHub CLI (`gh`) has been preconfigured with environment tokens (such as `GH_TOKEN`, `GITHUB_TOKEN`, or `GH_AUTH_TOKEN`), interactive authentication may fail or pick up the wrong credentials. Clearing those variables before logging in ensures `gh` prompts you correctly:

```bash
unset GH_TOKEN GITHUB_TOKEN GH_AUTH_TOKEN
printf %s "$PERSONAL_PAT" | gh auth login --hostname github.com --with-token
gh auth setup-git
gh auth status
```

The final `gh auth status` should confirm the desired account, token scopes, and that Git operations are configured to use the CLI's credential helper.

## Quick Bootstrap

To pull the latest `.devcontainer` from `emsilva/devcontainer` into any clean Git repo:
```bash
curl -fsSL https://raw.githubusercontent.com/emsilva/devcontainer/main/install.sh | sh
```

The installer:
- Requires `git` and a clean working tree (no staged/untracked changes, no active rebases).
- Clones `https://github.com/emsilva/devcontainer.git` (override via `SETUP_DEVCONTAINER_REPO` or `SETUP_DEVCONTAINER_REF`).
- Replaces the local `.devcontainer` directory and commits the change as `setting up devcontainer <source-sha>`.

## Tooling Highlights

- Rust toolchain (cargo, rustup) via the devcontainer Rust feature.
- Post-create hook auto-installs [`vivid`](https://github.com/sharkdp/vivid) for richer `LS_COLORS` support when `cargo` is available.

## Rebuild / Update

If you edit features or the provisioning script:
```bash
devcontainer rebuild
```

## Environment Initialization (Baseline PATH Guarantee)

Core tooling (Go, user-local bins) is available in every shell mode (login / non-login, interactive / non-interactive) through a layered, idempotent design:

**Tier 1: System Baseline (/etc/profile.d)**  
`.devcontainer/scripts/post-create.sh` writes `/etc/profile.d/00-core-path.sh` (using sudo when available). The script synthesizes `$HOME` when needed and sources `~/.config/shell/env-base.sh` when present, falling back to a minimal PATH otherwise.

**Tier 2: Non-Login Bash**  
`BASH_ENV=/home/vscode/.config/shell/env-base.sh` in `.devcontainer/devcontainer.json` sends every `bash -c` through the same script.

**Tier 3: User Base (`env-base.sh`)**  
`~/.config/shell/env-base.sh` (single source of truth) prepends `/usr/local/go/bin`, `$HOME/go/bin`, `$HOME/.local/bin`, pnpm, cargo, npm-global, Pulumi, and resolves NVM’s current Node shim. It exports GOPATH, Python, npm, uv, pnpm, Pulumi, and editor locale settings.

**Tier 4: Interactive Layer (`.zshrc`)**  
Adds only interactive conveniences: oh-my-zsh, starship, lazy nvm, completion caching, aliases, functions.

**Why This Matters**  
Previously, non-login shells depending on a missing `/etc/profile.d/00-core-path.sh` plus an absent Go path in the base layer caused `go version` failures in automation contexts. Now every entry path converges on the same minimal baseline before interactive customization.

**Extended Verification Matrix**
```bash
# 1. Minimal login bash (ensures /etc/profile + user base)
env -i HOME="$HOME" TERM=xterm-256color bash -lc 'echo login_bash: $(command -v go)';

# 2. Non-login bash (BASH_ENV path inject)
env -i HOME="$HOME" TERM=xterm-256color bash -c  'echo nonlogin_bash: $(command -v go)';

# 3. Explicit non-login with stripped PATH to prove reconstruction
env -i HOME="$HOME" PATH=/usr/bin:/bin TERM=xterm-256color bash -c 'command -v go';

# 4. Zsh login
zsh -lc 'echo zsh_login: $(command -v go)';

# 5. Zsh interactive
zsh -ic 'echo zsh_interactive: $(command -v go)';

# 6. Direct script execution (simulating automation)
/usr/bin/env bash -c 'command -v go';
```
All lines should resolve to `/usr/local/go/bin/go` (or a valid path under `$HOME/go/bin` if you later install tools there).

**Extending Baseline**  
Add universal tool directories in `env-base.sh` so all entry points share them. Keep `/etc/profile.d/00-core-path.sh` minimal since it simply bootstraps into the same script.

**Troubleshooting Quick Test**  
If a tool seems missing in automation: `bash -lc 'echo $PATH'` then `bash -c 'echo $PATH'` – they should both contain `/usr/local/go/bin` and `$HOME/go/bin`. If not, rebuild the container (`devcontainer rebuild`).


## Extending

Prefer adding tools via features (e.g. the rocker-org `apt-packages` feature, language features). Keep `.devcontainer/scripts/post-create.sh` for dotfile linking and the minimal PATH baseline. When `gh` is available, the script only performs the non-interactive login/`gh auth setup-git` flow when `PERSONAL_PAT` is populated; otherwise it simply wires `gh auth setup-git` against whatever credentials are already configured.

## Safety / Secrets

- No real secrets are committed; only placeholders.
- `.gitignore` excludes the runtime Claude config.
- If you add other generated creds, ignore them explicitly.

## Troubleshooting Quick Hits

| Issue | Fix |
|-------|-----|
| Docker-in-Docker fails with `iptables: Table does not exist` | Some hosts (e.g. Fedora 41+) don't ship legacy `ip_tables` modules. Load them via `/lib/modules` + `modprobe iptable_nat`, or switch to the nft backend (install `iptables-nft`). Codespaces already exposes the modules, so the feature works there without changes. |
| Zsh not default | Rebuild; post-create enforces `chsh` if possible |
| Missing Node global tools | Run `corepack enable` or reinstall via `npm -g` |
| Slow first prompt | fzf + starship caching warms after first run |
| Python deps missing | Run `uv sync` or fall back to `pip install -r requirements.txt` |
| Port didn't open browser | Expected—only notifications now. Click the toast or use Ports view. |

## Future Ideas / Nice-to-Haves

- Add a lightweight `Taskfile.yml` with common flows (test, lint, build) if project work begins.
- Provide a sample `.zshrc.local` placeholder (ignored) for truly local overrides.
- Add a `LICENSE` if this is going public.
- Consider neutralizing the devcontainer `name` if publishing (currently includes a personal identifier).
- Add optional Redis/Postgres services via `docker-compose.yml` if databases become standard.
- Introduce a minimal `SECURITY.md` clarifying no secrets should be committed and environment variables are injected at runtime.
- Favor features over `curl | bash` installs. If adding anything custom, pin versions and checksums.

---
Lightweight, batteries included. Tweak to taste.
