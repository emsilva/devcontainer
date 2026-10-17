# Agent work shortcuts implementation plan

**Goal:** Start new Codex/Claude conversations directly on this host or its
Codespace, with an explicit project resume picker, and tolerate delayed startup.

**Architecture:** Extend the existing work CLI with agent, destination and resume
options. Shortcuts share Workflows and existing custody/SSH/scratch authorities.
Generate aliases from the canonical task template; preserve existing task names.

**Constraints:** No global profile/auth changes, no Git synchronization mutations,
no background Codespace starts, no overwrite of existing tasks or transcript data.

- [x] Reproduce stale Shutdown startup failure with a one-POST poll sequence;
  test a permanent Shutdown deadline; fix at `workflows.start_codespace`.
- [ ] Add failing workflow tests for fresh remote launch, Git/sync refusal,
  unambiguous conversation remembering and preserved refresh failures.
- [ ] Add failing CLI/menu/task generation tests for all four shortcuts and
  `--resume`, including cancellation, project/agent filtering, adoption and
  existing task preservation.
- [ ] Implement CLI dispatch in `project_sync/__main__.py`, shortcut flow in
  `work.py`, destination-aware fresh launch in `workflows.py`, generated aliases
  in `templates/continuity-tasks.yml` and wizard integration; update root Taskfile.
- [ ] Update README and continuity docs with new/resume commands.
- [ ] Run full tests and task check, then independently review the whole branch.
- [ ] Smoke real generated Task/Gum/native launches in disposable local and remote
  fixtures; preserve the user's running Codespace and active conversations.
- [ ] Commit green changes, push/PR/merge in emsilva/devcontainer, prune the merged
  branch, and refresh only the generated Mainstage setup without pushing its branch.
