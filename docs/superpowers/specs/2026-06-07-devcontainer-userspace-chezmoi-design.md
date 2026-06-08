# Devcontainer User-Space Redesign — chezmoi + workstation dotfiles

**Date:** 2026-06-07
**Status:** Draft (pending user review)

## Goal

Replace the devcontainer's bespoke oh-my-zsh user-space and committed Claude/Codex
config with the user's real workstation dotfiles, delivered via **chezmoi** from a
**public GitHub mirror**. The container should feel "like the workstation": antidote
zsh, tmux, starship, nvim, eza.

## Current state

- `.devcontainer/user/` ships hand-maintained dotfiles (oh-my-zsh `.zshrc`, `.zprofile`,
  `.profile`, `.config/shell/env-base.sh`, `.config/starship.toml`) symlinked into `$HOME`
  by `post-create.sh`, plus `.claude/` (agent + command), `.claude.json.template`, and
  `.codex/config.toml.template` rendered from templates.
- Workstation uses chezmoi (`~/.local/share/chezmoi`, origin = LAN Gitea
  `ssh://git@10.0.5.52:2222/mannu/dotfiles.git`), **antidote** plugins, and a portable
  `.zshrc` (interactive-guarded, `(( $+commands[x] ))` guards, multi-distro).
- Codespaces is the primary target; the LAN Gitea is unreachable from the cloud → a
  public GitHub mirror is required.

## Locked decisions

| Decision | Choice |
|---|---|
| Mechanism | chezmoi apply of a **user-supplied** repo (`DOTFILES_REPO`); the user's own mirror is public on GitHub |
| Mirror scope | curated cross-platform subset (no desktop, no secrets) |
| Neovim | **include** `nvim/**` (LazyVim) + install neovim |
| MCP servers | **drop entirely** (remove configs + `CONTEXT7_API_KEY`/`EXA_API_KEY`) |
| Dotfiles application | **reusable, not hardcoded**: `task setup:dotfiles [REPO]` + `load-dotfiles`; auto-apply in `postCreate` iff `DOTFILES_REPO` is set |
| Dangerous agent aliases | unchanged (kept, per earlier decision); they now live in the dotfiles |

## Part A — A dotfiles repo to point at (`github.com/emsilva/dotfiles`)

This is the user's *personal* repo that the generic template mechanism (Part B) will consume
via `DOTFILES_REPO` — it is **not** referenced by name anywhere in the template.

Curated chezmoi subset. **Include:**
- `dot_zshrc` (+ portable tweaks: `HISTFILE="${HISTFILE:-$HOME/.zsh_history}"`, and
  `txt2zpl` reads `$ZPL_PRINTER` instead of a hardcoded IP)
- `dot_zsh_plugins.txt`
- `dot_config/starship.toml`
- `dot_tmux.conf`, `dot_config/tmux/executable_cheatsheet.sh`
- `dot_config/eza/theme.yml`
- `dot_config/nvim/**` (LazyVim)
- a subset-appropriate `.chezmoiignore`

**Exclude:** all desktop config (hypr, hyprshell, waybar, kitty, mako, gtk-3/4, gtkrc,
paru, yay), `.zprofile` (contains live secrets — already untracked), `.gitconfig` (local),
anything host-specific.

**Security scrub — HARD GATE before any push:**
- Scan the subset for secrets / tokens / `private_*` files (expect none, since `.zprofile`
  is excluded).
- LAN IPs — **resolved**: parameterize `txt2zpl` to read `$ZPL_PRINTER`
  (`nc -N "${ZPL_PRINTER:?set ZPL_PRINTER}" 9100 < "$1"`). The real IP `10.0.10.80` lives in
  the untracked local `~/.zprofile` (`export ZPL_PRINTER=10.0.10.80`), so it never reaches
  the public repo. `10.0.5.52` (Gitea) is not in the subset. Still scan for any other
  secrets / `private_*` before pushing.
- **Explicit user confirmation required before pushing public** (publishing is
  irreversible; content can be cached/indexed even if later deleted).

## Part B — Devcontainer rewire (generic / reusable)

The template must stay personal-data-free. Dotfiles are applied via a reusable mechanism,
never a hardcoded repo.

- **Install the chezmoi binary at build time** (devcontainer feature if available, else
  pinned official installer) so it is present and prebuild-cached. Installing the *binary* is
  generic and cacheable; *applying* dotfiles is per-user and is NOT done in a prebuild.
- **`task setup:dotfiles [REPO]`** (in `.devcontainer/Taskfile.yml`) + a **`load-dotfiles`**
  wrapper script on `PATH`: resolve the repo from (1) the arg, else (2) `$DOTFILES_REPO`,
  else (3) an interactive prompt; then `chezmoi init --apply <repo>`. Reusable by anyone.
  Private repos rely on the gh credential helper configured earlier in `postCreate`.
- **Auto-apply in `postCreate`** (per-user; sees env/secrets — NOT `onCreate`, because
  prebuilds are shared and dotfiles are per-user): if `$DOTFILES_REPO` is set, run the apply
  non-interactively; otherwise print a one-line hint (`run: task setup:dotfiles`). Runs
  after gh auth so private repos resolve.
- `common-utils`: set `installOhMyZsh=false` (keep `installZsh=true`); antidote replaces
  oh-my-zsh.
- Install **neovim** (via `apt-packages` feature list or a feature).
- **Delete** from `.devcontainer/user/`: `.zshrc`, `.zprofile`, `.profile`,
  `.config/shell/env-base.sh`, `.config/starship.toml`, `.claude/`, `.claude.json.template`,
  `.codex/` (the directory may end up empty and be removed).
- Slim `post-create.sh`: remove `process_user_templates`, `render_template_file`, and the
  dotfile-symlink loop (chezmoi owns dotfiles now). Keep container-only steps: volume perms,
  `ensure_login_shell`, `configure_timezone`, git config, gh auth, claude perm repair, and
  (pending evaluation) the `/etc/profile.d` PATH baseline; add the conditional dotfiles
  auto-apply after gh auth.
- `devcontainer.json`: add `DOTFILES_REPO: ${localEnv:DOTFILES_REPO}` to `remoteEnv` (a
  Codespaces secret or local env var drives auto-apply); remove `CONTEXT7_API_KEY` /
  `EXA_API_KEY`; keep `HISTFILE` remoteEnv. Re-evaluate `BASH_ENV=env-base.sh` (removed with
  env-base.sh) — confirm non-interactive PATH still satisfied by feature `containerEnv`.

## Reconciliation details

- **PATH:** the language/CLI features publish their bins via feature `containerEnv` (verified
  during the live build — `go`/`cargo`/`node` resolved). The portable `.zshrc` adds
  `~/.local/bin`, `~/.cargo/bin`, `~/go/bin`. If removing `env-base.sh` causes a
  non-interactive-bash PATH gap (e.g. for lifecycle scripts), retain a minimal
  `/etc/profile.d/00-core-path.sh`.
- **History:** container `remoteEnv` sets `HISTFILE=/commandhistory/.zsh_history`; the
  tweaked `.zshrc` honors a pre-set `HISTFILE`.
- **antidote:** self-bootstraps (clones from GitHub) on first interactive shell; needs
  git + network. Optionally warm it once during the dotfiles apply.
- **per-user, not prebuilt:** dotfiles are applied in `postCreate` (or via native Codespaces
  dotfiles), never baked into a shared prebuild.
- **node:** feature provides node on PATH; the `.zshrc` `fnm` branch no-ops in the container
  (workstation uses fnm, container uses the nvm-based node feature) — harmless.

## Implementation order

1. **Part A** — assemble the curated subset, run the security scrub, get explicit publish
   confirmation, create + push the public repo (`github.com/emsilva/dotfiles`).
2. **Part B** — rewire the devcontainer (generic mechanism, no hardcoded repo); rebuild via
   `devcontainer up`. Validate: (a) with `DOTFILES_REPO` unset, build succeeds and prints the
   hint; (b) `task setup:dotfiles emsilva/dotfiles` and `load-dotfiles` apply correctly; (c)
   with `DOTFILES_REPO=emsilva/dotfiles`, postCreate auto-applies. Confirm zsh + antidote
   plugins load, tmux/starship/nvim present, history persists, tool PATHs resolve.

## Risks / open items

- antidote + nvim/LazyVim first-run cost → warm during the dotfiles apply (`postCreate`);
  otherwise the first interactive shell / first `nvim` pays it. Not prebuild-cached, since
  dotfiles are per-user.
- dotfiles apply runs per-user in `postCreate` (never in a shared prebuild); verify it does
  not measurably slow first-attach, and that `DOTFILES_REPO` actually reaches the container
  in Codespaces (the `${localEnv:...}` → secret caveat from audit items #9/#10 applies).
- Removing `env-base.sh` — confirm no non-interactive PATH regressions in lifecycle scripts.
- Security (separate from this spec, user's call): rotate the four keys currently exposed in
  `~/.zprofile`; never track `.zprofile` in chezmoi.

## Supersedes

This redesign folds in and replaces original audit items #7 (AI-config/secret reach as it
pertains to the committed templates), #10 (dotfiles conflict), and #12 (shell cleanup).
Remaining audit items #8, #9, #11, #13, #14, #15 are unaffected.
