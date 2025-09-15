# Devcontainer Workspace

A handy Ubuntu 24.04 development container setup meant for everyday polyglot work (Go, Python, Node, shell tooling) without a bunch of manual bootstrap steps.

## What You Get

- Base image: `mcr.microsoft.com/devcontainers/base:ubuntu-24.04`
- Languages & runtimes via features:
  - Go 1.24
  - Node.js 22 (with Yarn, pnpm via Corepack)
  - Python 3.12 (plus `uv` for fast env + deps)
- Tooling & CLI goodies:
  - zsh + Oh My Zsh + plugins (autosuggestions, syntax highlighting, zoxide)
  - starship prompt (Catppuccin theme)
  - fzf, fd, ripgrep, bat, tldr, tree, jq, yq, httpie, zoxide, vivid, fastfetch
  - git + git-lfs configured with sensible defaults
  - Task (`go-task`) for optional task automation
- Container tuned for iterative dev: persistent shell history volume, trimmed noise, default branch = `main`.

## Devcontainer UX niceties

- VS Code settings: format on save, Ruff for Python, Prettier/ESLint for JS/TS
- Default terminal: `zsh`
- History persisted across rebuilds (`/commandhistory` volume)
- Symlinked user dotfiles from `.devcontainer/user` into `$HOME`
- Automatic detection + install of dependencies for Node / Python (uv) / Go
- Port forwarding is now fully dynamic: no predeclared list; VS Code will just notify when a process starts listening (configured with `onAutoForward=notify`).

## AI / MCP Configuration (Claude & Codex)

Both Claude and Codex configs are generated from templates stored under `user/` and then symlinked into `$HOME` during provisioning:

- Claude: `user/.claude.json.template` → generates `user/.claude.json` → symlinked to `~/.claude.json`.
- Codex: `user/.codex/config.toml.template` → generates `user/.codex/config.toml` → symlinked to `~/.codex/config.toml`.

Placeholder tokens (`__CONTEXT7_API_KEY__`, `__EXA_API_KEY__`) are replaced at container create time when the corresponding environment variables are set. Generated files are ignored by git. Remove the generated file(s) and rebuild to force regeneration.

## Expected Environment Variables (set as GitHub Codespace / repository secrets or local env when launching)

| Variable | Purpose | Required? |
|----------|---------|-----------|
| `CONTEXT7_API_KEY` | Enables Context7 MCP server integration | Optional |
| `EXA_API_KEY` | Enables Exa search MCP server integration | Optional |

These are referenced in `devcontainer.json` under `remoteEnv` so they can be wired in from your local environment or repository-level secrets.

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

## Rebuild / Update

If you edit features or the provisioning script:
```bash
devcontainer rebuild
```

## Environment Initialization (Baseline PATH Guarantee)

Core tooling (Go, user-local bins) is available in every shell mode (login / non-login, interactive / non-interactive) through a layered, idempotent design:

**Tier 1: System Baseline (/etc/profile.d)**  
`post-create.sh` now ALWAYS writes `/etc/profile.d/00-core-path.sh` (even when the provisioning user is non-root, using sudo fallback). This script:
- Synthesizes `$HOME` if missing (some automation strips it).
- Prepend-adds (idempotently): `/usr/local/go/bin`, `$HOME/go/bin`, `$HOME/.local/bin` (when they exist).
- Exports `GOPATH` defaulting to `$HOME/go`.

**Tier 2: Non-Login Bash**  
`BASH_ENV=/etc/profile.d/00-core-path.sh` in `devcontainer.json` ensures plain `bash -c '...'` inherits the same baseline (bash ignores `BASH_ENV` only for login shells, which source the profile chain).

**Tier 3: User Base (`env-base.sh`)**  
`~/.config/shell/env-base.sh` is a POSIX script sourced by both login shells (`.profile` / `.zprofile`) and non-interactive shells (via `noninteractive-env.sh`). It now explicitly prepends `/usr/local/go/bin` and `$HOME/go/bin` early, then user-local directories (npm-global, cargo, pnpm). Language env variables (GOMODCACHE, Python, npm, uv) live here so they’re universal.

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
Add more universal tool directories only in `/etc/profile.d/00-core-path.sh` (keep it short). Per-user or language-specific additions belong in `env-base.sh`. Avoid duplicating the same path in multiple layers—shorter PATH = faster command lookup.

**Troubleshooting Quick Test**  
If a tool seems missing in automation: `bash -lc 'echo $PATH'` then `bash -c 'echo $PATH'` – they should both contain `/usr/local/go/bin` and `$HOME/go/bin`. If not, rebuild the container (`devcontainer rebuild`).


## Extending

Add extra CLI tools in `post-create.sh` (keep things idempotent). Place new dotfiles or config under `user/` and they’ll get symlinked in on create.

## Safety / Secrets

- No real secrets are committed; only placeholders.
- `.gitignore` excludes the runtime Claude config.
- If you add other generated creds, ignore them explicitly.

## Troubleshooting Quick Hits

| Issue | Fix |
|-------|-----|
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
- Replace `curl | bash` installers with pinned checksums for stricter supply-chain hygiene if higher security posture is required.

---
Lightweight, batteries included. Tweak to taste.
