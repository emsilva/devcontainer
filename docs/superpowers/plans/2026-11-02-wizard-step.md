# Setup Wizard Step: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> In this repo the lead runs it through the herd instead (owner ruling 20): Codex xhigh builders in worktrees, the
> lead reviews the diff and re-gates in the default environment.

**Goal:** Setup publishes without opening an editor, a rerun opens at Review, and after setup the owner's checkout
is updated or the reason it was not is said first.

**Architecture:** Lower layers first: `git_ops.fast_forward_checkout` returns a reasoned result (Task 1);
`preferences` tolerates unknown keys and `codespaces.launch` opens an editor only on an explicit opt-in and can force
a rebuild (Task 2). Then `__main__` splits Publish from the Codespace action (Task 3) and opens reruns at Review
(Task 4). Tasks 1 and 2 are independent; Task 3 needs both; Task 4 follows Task 3 in the same files.

**Tech Stack:** Python 3 standard library, `unittest`, Gum (through `project_sync.terminal`), `gh`, Git.

**Spec:** `docs/superpowers/specs/2026-11-02-wizard-step-design.md` (calls 1–17). Analysis with red tests:
`tmp/codespace-lifecycle/wizard/analysis/REPORT.md` (gitignored; the lead has it).

## Global Constraints

- Never stash, force, switch branches, rebase or merge into a feature branch in the owner's checkout.
- No editor command (`gh codespace code`) unless `~/.config/devcontainer-setup/open-after-create.json` holds
  `{"open_after_create": "browser"}` or `"vscode"`; default `none`. Only after a create; never after rebuild/reuse.
- `~/.config/devcontainer-setup/config.json` keeps exactly today's keys when written by this version, so older
  generator pins (which reject unknown keys) still read it.
- Keep every existing safety check: reviewed-head and default-branch containment, receipt before validation,
  dirty/branch/divergence refusals on reuse.
- No existing test assertion is weakened except where the spec changes the behaviour it pins; each such change is
  listed in the task's report with the spec line that changes it. No test removed or skipped.
- Commit messages plain imperative, no AI attribution. Gates: `task check` and `task test`.

## Review Focus

1. A checkout with only untracked notes, one untracked file that collides, and a tracked edit: updated, refused
   (collision) and refused (tracked changes), bytes intact in both refusals (Task 1
   `test_fast_forward_reports_each_refusal_reason`).
2. A legacy receipt with `client: browser` and no opt-in file: rebuild/reuse/create open nothing (Task 2
   `test_legacy_client_never_opens_an_editor`).
3. A config.json written by an older pin plus one unknown key: the new loader reads it and a save keeps the key (Task 2
   `test_unknown_preference_keys_survive_load_and_save`).
4. A continuity-off project with `ssh: true` rerun with Codespace `none`: no diff, sshd stays (Task 3
   `test_codespace_none_keeps_saved_ssh`).
5. A rerun with one new runtime found and one recorded decline: exactly one question, then Review (Task 4
   `test_rerun_asks_only_the_new_finding`).

---

### Task 1: The checkout update returns its reason (#63)

**Files:** Modify `devcontainer_setup/git_ops.py` (`fast_forward_checkout`); `devcontainer_setup/__main__.py`
(`_launch_on_default` ending and `setup_continuity` messages only). Test: `tests/test_git_ops.py`,
`tests/test_wizard.py`.

**Interfaces:**
- Produces: `checkout_update(project: Path, default: str) -> dict` in `git_ops.py`, returning
  `{"state": "updated" | "current" | "refused", "reason": None | "fetch" | "tracked-changes" | "other-branch" |
  "detached" | "diverged" | "collision", "head": <short sha>, "behind": int | None}`.
  `fast_forward_checkout` stays as `checkout_update(...)["state"] in ("updated", "current")` for existing callers.

- [ ] **Step 1: Failing tests** in `tests/test_git_ops.py`: `test_untracked_notes_do_not_block_the_update` (state
  `updated`, notes' bytes unchanged); `test_fast_forward_reports_each_refusal_reason` (tracked edit →
  `tracked-changes`; colliding untracked file → `collision`; feature branch → `other-branch`; detached →
  `detached`; local commit not on origin → `diverged`; unreachable origin → `fetch`; each leaves HEAD and bytes).
  Change the existing untracked-file expectation (`tests/test_git_ops.py:725-760`) to success, per spec #63.
  In `tests/test_wizard.py`: `test_ending_prints_checkout_result_before_rebind` (stdout order; refusal prints the
  exact command, e.g. `git -C <path> pull --ff-only`); `test_feature_branch_is_never_told_to_merge`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_git_ops tests.test_wizard -v` → the new tests fail.
- [ ] **Step 3: Implement** with `git status --porcelain --untracked-files=no` for the tracked guard, then today's
  fetch and `merge --ff-only --no-autostash --no-overwrite-ignore`; read Git's stderr for the collision case.
  `setup_continuity`'s different-pairing text moves to stdout after the checkout line.
- [ ] **Step 4: Run** the focused modules, then `task check` and `task test` → exit 0.
- [ ] **Step 5: Commit** `git commit -m "Say why setup could not update the checkout, and update it when untracked files are the only change"`.

### Task 2: Preferences and the editor opt-in; a forced rebuild (#80 lower layer)

**Files:** Modify `devcontainer_setup/preferences.py`, `devcontainer_setup/codespaces.py` (`launch`, `open`, receipt
writing). Test: `tests/test_preferences.py`, `tests/test_codespaces.py`.

**Interfaces:**
- Produces: `load_preferences` ignores unknown keys (keeps validating known ones); `save_preferences` keeps unknown
  keys already in the file. `load_open_after_create(env=None) -> str` (`none` default, `browser`, `vscode`; an invalid
  file is `none` plus one warning on stderr) and `save_open_after_create(value: str, env=None) -> Path`.
- Produces: `Codespaces.launch(repo, branch, sha, machine, idle_timeout, open_after_create, dotfiles_repo, receipt,
  *, resume=None, location=None, ..., force_rebuild=False)`: `open_after_create` (`none` | `browser` | `vscode`)
  takes `client`'s positional slot (`codespaces.py:322-324`); new receipts have no `client`;
  `open()` is called only when `open_after_create != 'none'` and the Codespace was just created. With
  `force_rebuild=True` the reuse path runs `gh codespace rebuild` even when the receipt shows this commit validated.

- [ ] **Step 1: Failing tests:** `test_unknown_preference_keys_survive_load_and_save`; `test_open_after_create_defaults_to_none_and_validates`;
  `test_create_with_none_opens_nothing` (no `gh codespace code` in the recorded commands);
  `test_create_with_browser_opens_once_with_web`; `test_legacy_client_never_opens_an_editor`;
  `test_force_rebuild_rebuilds_a_validated_codespace`; keep `test_retry_of_current_codespace_skips_rebuild` passing.
  Update the tests that require an editor command (`tests/test_codespaces.py:200`, `tests/test_wizard.py:1045-1053`)
  to the opt-in, listing each in the report.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_preferences tests.test_codespaces -v` → new tests fail.
- [ ] **Step 3: Implement**; callers in `__main__.py` pass the loaded `open_after_create` where they passed `client`
  (no behaviour change there beyond the opt-in; Task 3 reshapes the menu).
- [ ] **Step 4: Run** focused modules, then `task check` and `task test` → exit 0.
- [ ] **Step 5: Commit** `git commit -m "Open an editor after setup only when asked, and let a rebuild be forced"`.

### Task 3: Publish and Codespace are two choices (#80, ruling 4) — after Tasks 1 and 2

**Files:** Modify `devcontainer_setup/__main__.py` (`_launch_options`, `interactive`, `_resume_setup_pr`,
`_merge_and_launch`, `_launch_on_default`). Test: `tests/test_wizard.py`.

**Interfaces:**
- Consumes: `checkout_update` (Task 1); `launch(..., open_after_create, force_rebuild=...)` and
  `load_open_after_create` (Task 2).
- Produces: `_publish_choice(ui, has_origin: bool, changed: bool) -> str` (`merge` | `local` | `nothing`) and
  `_codespace_choice(ui, api, project, branch, *, published: bool) -> dict` with
  `{"action": "none" | "rebuild" | "reuse" | "create", "name": str | None, "machine": ..., "idle_timeout": ...}`.
  Menu and defaults exactly as the spec's "Publish and Codespace" section (calls 9, 16).

- [ ] **Step 1: Failing tests** (`tests/test_wizard.py`): `test_publish_merge_with_codespace_none_merges_and_updates_checkout`
  (push, PR, merge only on yes, checkout updated, no allocation/rebuild/open); `test_local_only_offers_no_codespace_action`;
  `test_paired_defaults_to_rebuild_with_force`; `test_existing_unpaired_codespace_is_listed_and_default_reused`
  (keep `test_existing_codespace_menu_never_defaults_to_create` green); `test_no_create_with_a_usable_pairing`;
  `test_open_pr_path_asks_two_questions`; `test_codespace_none_keeps_saved_ssh`; `test_merge_prompt_is_y_n_with_bypass_line`
  (bypass context on its own line, prompt ends `(y/N)`, Enter/No/Ctrl-C leave the PR open);
  `test_nothing_to_publish_still_updates_checkout`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_wizard -v` → new tests fail.
- [ ] **Step 3: Implement**; the SSH rule is `saved or continuity or action != "none"`, re-rendering before Review
  when it turns SSH on.
- [ ] **Step 4: Run** focused module, then `task check` and `task test` → exit 0.
- [ ] **Step 5: Commit** `git commit -m "Split publishing from the Codespace choice in setup (#80)"`.

### Task 4: A rerun opens at Review (#81, ruling 3, ruling 22) — after Task 3

**Files:** Modify `devcontainer_setup/__main__.py` (`interactive`, `_select` split, `defaults`,
`_preferences_after_commit`), `devcontainer_setup/render.py` (metadata `scanDecisions`). Test:
`tests/test_wizard.py`, `tests/test_render.py`.

**Interfaces:**
- Produces: `_normalize_saved(found: dict, chosen: dict) -> dict` (today's non-interactive part of `_select`);
  `_new_findings(found: dict, chosen: dict, decisions: list) -> list[dict]`; metadata key
  `customizations.devcontainerSetup.scanDecisions`, a sorted list of `{"kind", "value", "answer"}`;
  `_resolve_launch(api, project, branch, paired: str | None) -> dict` per the spec's machine rules.

- [ ] **Step 1: Failing tests:** `test_saved_answers_with_no_new_finding_open_at_review` (no prompt before Review);
  `test_rerun_asks_only_the_new_finding`; `test_recorded_decline_is_not_asked_again`;
  `test_evidence_path_change_is_not_a_finding`; `test_declared_manager_version_is_adopted_without_a_question`;
  `test_project_dotfiles_off_overrides_user_default`; `test_missing_dotfiles_repo_is_asked_when_project_wants_dotfiles`;
  `test_paired_premium_rebuilds_on_premium_despite_stale_receipt`; `test_unreadable_paired_machine_is_asked_not_defaulted`;
  `test_change_answers_opens_full_editor`; `test_first_setup_still_walks_every_screen`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_wizard tests.test_render -v` → new tests fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** focused modules, then `task check` and `task test` → exit 0.
- [ ] **Step 5: Commit** `git commit -m "Open a setup rerun at Review with the saved answers (#81)"`.

### Task 5: Definition of done (lead)

Merge Tasks 1–4 into local main, root re-gate, then on the throwaway innovate test branch (ruling 15): an upgrade
rerun opens at Review; Publish=merge + Codespace=none merges and updates the clone's checkout with untracked notes
present; `--resume <unpaired throwaway>` and the menu's rebuild keep the machine; no editor opens. Delete the test
Codespace and branch; push main and roll out (ruling 23).
