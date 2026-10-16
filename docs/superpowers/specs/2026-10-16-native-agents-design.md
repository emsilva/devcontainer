# Native coding tools

The baseline includes Codex, Claude Code, and OpenCode in every generated environment. There is no TUI or noninteractive opt-out. The user explicitly requested official user installations and mandatory OpenCode after the locked Codex Feature selected a prerelease and installed an incomplete standalone executable.

Run the vendors' official native installers as the remote user (`vscode` by default) during post-create, before selected-tool validation. Keep their complete packages and launchers under the user's home directory, owned by that user. The installers select their ordinary current release channels; the generator does not pin daily CLI releases. Python and uv remain baseline; Node is selected only by project/runtime/package-manager requirements.

Use one checked-in agent helper as the producer for both canonical and generated containers. Ordinary provisioning is idempotent and does not redownload a valid managed user installation. An explicit `task devcontainer:upgrade:agents` updates all three through the same helper. The helper must fail on download/install/validation failures, refuse root execution, and must not treat an unrelated system executable as a completed user installation. Downloads are checked before execution, and failures must not fall through to a system binary.

Remove the Codex and Claude Features from canonical features and generated managed config/locks. Retain unrelated user Features and adopted fields through the existing ownership merge. Ensure fresh terminals, VS Code sessions, lifecycle hooks, and SSH commands resolve the user launchers: `~/.local/bin` and `~/.opencode/bin` precede system locations. Use devcontainer environment wiring rather than rewriting user shell dotfiles to establish PATH. Preserve the existing dotfiles behavior and remembered repository.

Validation must exercise the installed agents beyond command existence. In particular, Codex must have a complete package capable of normal daemon-backed launch, and a smoke check must reject the bare executable from the old Feature even though its `--version` succeeds. Do not send model requests or copy authentication tokens to test installation. Verify real launches up to the authentication screen in the existing Codespace, with clean exit and no prompt submission.

Keep reading old selections/preferences, including `agents:false` and `agentic_tools:false`, without honoring those obsolete opt-outs. Remove misleading Node dependency and locked agent version-option notes from review output. Document the deliberately moving vendor releases and the update task.

The existing Codespace receives the same helper and generated configuration through a hash-checked, backed-up repair. Source and consumer commits are local durability checkpoints. Publishing still requires the user's outward-action confirmation; Mainstage has unrelated documentation commits ahead of the last approved push.
