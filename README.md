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

---
Lightweight, batteries included. Tweak to taste.
