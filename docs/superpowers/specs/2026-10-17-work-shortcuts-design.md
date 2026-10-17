# Agent work shortcuts

The owner requested `work:codex`, `work:claude` and their `:cs` variants. Each
starts a new conversation. `--resume` opens a project-only conversation picker for
the selected agent, newest first, with New conversation and Back actions. Closing
the agent returns to the shell. The existing `task work` dashboard stays intact.

Local shortcuts use the host running Task. The `:cs` variants explicitly choose
the paired Codespace. Pairing/start prompts, clean matching Git checkouts, scratch
reconciliation, adoption and one-writer custody remain authoritative. Existing
project task definitions take precedence over generated aliases.

New remote conversations launch through the existing remote managed `agent
<name> --new` command. A unique launch request records the native-created session
ID in private destination state before opening the agent. Read that exact receipt
afterward to remember the conversation; never infer identity from concurrent
catalog activity. No transcript, credential, native index or profile manipulation
is introduced. Missing remote authentication remains the native tool's sign-in
flow. A failed post-launch catalog refresh preserves the conversation and explains
how to find it in the resume picker.

Codespace startup issues one POST and polls GET until Available or the existing
five-minute deadline. Shutdown is allowed while that request takes effect. Failed,
Unavailable, Deleted and Archived remain terminal failures. The deadline reports
the last observed state. Background sync retains its no-start boundary.
