# Devcontainer Workspace

A handy Ubuntu 24.04 development container setup meant for everyday polyglot work (Go, Python, Node, shell tooling) without a bunch of manual bootstrap steps.

## What You Get

- Base image: `mcr.microsoft.com/devcontainers/base:ubuntu-24.04`
- Languages & runtimes via features:
  - Go 1.23
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

## Claude / MCP Integration

There is a `.claude.json.template` that gets copied and token placeholders replaced at container create time if env vars are present. The actual generated `.claude.json` is ignored by git.

## Expected Environment Variables (set as GitHub Codespace / repository secrets or local env when launching)

| Variable | Purpose | Required? |
|----------|---------|-----------|
| `CONTEXT7_API_KEY` | Enables Context7 MCP server integration | Optional |
| `EXA_API_KEY` | Enables Exa search MCP server integration | Optional |
| `DEVCONTAINER_SKIP_WELCOME` | Suppress first-shell banner (set to `1`) | Optional |

These are referenced in `devcontainer.json` under `remoteEnv` so they can be wired in from your local environment or repository-level secrets.

## Adding Git Identity

The setup intentionally does **not** hard-code `git config user.name` or `user.email`. Set them once inside the container if Git complains:
```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

## Rebuild / Update

If you edit features or the provisioning script:
```bash
devcontainer rebuild
```

## Environment Initialization (Baseline PATH Guarantee)

The container ensures core tooling (Go, user local bins) is available in **all** shell modes (login, non-login, interactive, non-interactive) via a two-tier strategy:

**Tier 1: System Profile Baseline**  
`post-create.sh` installs `/etc/profile.d/00-core-path.sh` which:
- Synthesizes `$HOME` if missing (fallback via `getent passwd`).
- Prepend-only adds: `/usr/local/go/bin`, `$HOME/go/bin`, `$HOME/.local/bin` (if they exist).
- Exports `GOPATH` (defaults to `$HOME/go`).
- Is idempotent and safe to source multiple times.

**Tier 2: Non-Login Shell Coverage**  
`devcontainer.json` sets `BASH_ENV=/etc/profile.d/00-core-path.sh`, so even plain `bash -c '...'` shells inherit the baseline. (Note: bash ignores `BASH_ENV` for login shells, which is fine—login shells already source `/etc/profile`.)

**User Layer**  
Higher-level personalization and language/tool extras live in symlinked dotfiles under `.devcontainer/user`:
- `~/.zprofile` / `env-base.sh`: lightweight user-level PATH additions (cargo, pnpm, npm-global) + language env vars.
- `~/.zshrc`: interactive features (oh-my-zsh, starship, lazy nvm, caching helpers).

**Why This Matters**  
Previously `go` (and other tools) could be “missing” when automation launched a non-login shell with no `$HOME`. The new layering removes that fragility.

**Verification Commands**
```bash
bash -lc 'echo LOGIN: $PATH | cut -d: -f1-5; command -v go'
bash -c  'echo NONLOGIN: $PATH | cut -d: -f1-5; command -v go'
zsh -lc  'echo ZSH_LOGIN: $(command -v go)'
```
All should report a valid path for `go`.

**Extending Baseline**  
To add another always-on tool path, append an `add_path` call in `/etc/profile.d/00-core-path.sh` (keeping it minimal and idempotent).

**Avoid** duplicating those directories in user PATH logic—only append user-only dirs in `env-base.sh`.


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
