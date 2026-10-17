# Daily project work launcher

Approved in conversation: offer `task work` as the daily entrypoint, remember the
last conversation, and integrate verified Mac/Codespace pairing into setup.

The audience is a developer returning to a project or switching machines. The
main action is to continue the last Codex or Claude conversation on this machine
or the paired Codespace. A compact terminal menu uses the existing Gum style.
Conversations show a title or first user prompt, agent, and last activity; UUIDs
are private identifiers. The menu remembers the last selection outside Git.

First use discovers matching Codespaces and verifies the remote Git identity and
checkout. One match can be selected automatically; ambiguity requires a choice.
A stopped Codespace starts only from an explicitly named start action. Setup
reuses its selected Codespace, pairs it, and starts the detached scratch worker.
Commit-only setup leaves pairing for `task work`. New setups default sync to yes;
upgrades retain an existing explicit choice.

Mac runs the controller and detached scratch worker. On Codespaces the launcher
offers local managed conversations and directs a move back to the Mac launcher.
Selecting a conversation on the other host uses existing transactional handoff
and native managed launch. Remote launch uses a real interactive SSH terminal.
An active or uncertain writer blocks a move. Existing conversations require
explicit adoption after scope validation. No global CLI alias or profile change.

Scratch status is human-readable and distinguishes stopped, connecting, waiting,
conflicts, and synchronized. A transfer waits for verified scratch reconciliation;
conflicts show paths and preserve both sides. Git branch, HEAD, and dirty working
tree mismatches stop switching with actionable advice; Git is never changed by
this launcher. Existing low-level commands remain available.

Cancellation returns cleanly without transferring or starting a Codespace. A
failed launch preserves its selected conversation and handoff receipts for retry.
Terminal text from repository files or transcripts is sanitized and bounded.
Task integration preserves existing operations and a pre-existing `work` task;
the namespaced `task devcontainer:work` is always available when enabled.

Acceptance: generated consumer task, real Taskfile dispatch and terminal menu;
remembered selection; titles and recency; fresh pairing and setup pairing;
explicit stopped-space start; same-ID round trip; active writer and Git mismatch
refusals; conflict guidance; cancellation and interrupted-transfer retry; full
existing tests, ShellCheck, independent review, and a real Mac/Codespace smoke.
