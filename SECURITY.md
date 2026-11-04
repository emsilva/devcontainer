# Security

## Secrets

This repository contains **no real secrets** — only placeholders and examples.

- In **GitHub Codespaces**, provide credentials as **Codespaces secrets** (Settings →
  Codespaces → Secrets). They are injected as environment variables automatically.
  Codespaces itself exports them only to VS Code terminals and bash login shells over SSH;
  the setup's blocks in `/etc/zsh/zshenv` and `/etc/bash.bashrc` also load them into
  interactive zsh and bash (Herdr panes and the agents in them), once per process tree and
  never over a value already set.
- For **local** `devcontainer up`, copy `.devcontainer/devcontainer.env.example` to
  `.devcontainer/devcontainer.env` (gitignored) and fill it in.
- Never commit `.devcontainer/devcontainer.env`, a real `PERSONAL_PAT`, or any token.
- The dotfiles applied via chezmoi (`DOTFILES_REPO`) should likewise keep secrets out of
  tracked files (e.g. keep `~/.zprofile` untracked).

## Reporting

This is a personal template; if you spot a security issue, please open an issue on the
repository (omit any sensitive details) or contact the maintainer directly.
