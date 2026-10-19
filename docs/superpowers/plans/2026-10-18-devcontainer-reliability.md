# Devcontainer reliability implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Execute task-by-task with fail-first tests, local durability commits and independent task reviews.

**Goal:** Fix the five reproduced Astra audit findings at their producer and deliver the validated update to Mainstage.

**Architecture:** Preserve the current immutable transcript/checkpoint protocol and ordinary-checkout model. Repair Taskfile integration and scratch eligibility at their shared generators. Move conversation exchange entirely into the worker with independent direction results and a coherent, fresh cycle receipt; connection startup depends on prepared transport/scratch rather than successful transcript publication.

**Tech stack:** Python 3.11+ standard library, Bash, go-task, pinned Mutagen 0.18.1, GitHub CLI, Herdr.

## Global constraints

- Source authority is `emsilva/devcontainer`; consumer is Mainstage `docs/model-resilience-design`. Do not merge Mainstage to main.
- Runtime Git operations remain manual. Never synchronize source files, indexes, Git bundles, dirty work, credentials, global agent databases/settings or personal memory libraries.
- Use existing ordinary checkouts and plain native agents/resume commands. Preserve legacy generated checkouts, retained checkpoints and paused Mutagen baselines.
- Preserve strict host keys, the GET-only no-start background transport, owned process identities, immutable objects, native publication journals, project identity/scope checks and owner task/settings bytes.
- No permissive blanket exception handling around root identity/authentication. Individual conversation-direction failures may degrade history synchronization without disabling verified scratch/Herdr connectivity.
- No external Python dependencies. Unsupported YAML layouts must fail before candidate mutation, never silently change owner command meaning.
- Tests must first reproduce each audited failure; Root independently runs the final gates and installed smoke. Commit green scoped work immediately, no AI attribution.
- No global model/permission/notification/Herdr/GitHub-account changes. No provider/model turns are required for this repair.
- Push source branch, open PR and merge after gates; upgrade/push consumer docs branch after review; live Codespace tests authorized with shutdown afterward.

## Task 1: Preserve Task command semantics through setup and upgrade

**Ownership:** `devcontainer_setup/root_tasks.py`, Task integration/candidate validation portions of `devcontainer_setup/__main__.py`, `tests/test_root_tasks.py`, `tests/test_continuity_integration.py`, relevant integration tests in `tests/test_wizard.py`. A small standard-library Taskfile mapping helper is allowed if needed; no general YAML framework.

**Interfaces:** Keep `integrate_taskfile(project, files)`, `integrate_continuity(project, files, enabled)`, `check_candidate(files, project=None)` signatures. Extend `root_tasks.integrate(text, enabled=True)` as needed; disabling continuity retires only verified owned aliases. Preserve edited/custom owner aliases and reject malformed managed blocks.

- [ ] Add fail-first real Task fixtures: two/four-space task and include mappings, quoted owner keys, existing includes, comment merely mentioning `.devcontainer/Taskfile.yml`, owner `work:cs` override, malformed/inline unsupported mappings, repeated enable/disable/enable. Full `render -> integrate -> check_candidate` must preserve runnable harmless owner commands and their inventory.
- [ ] Implement block-aware indentation and include-namespace detection. The insertion must adopt the actual task/include mapping indentation; comments and scalar values cannot prove an include exists. Refuse ambiguous duplicate mappings/unsupported structure rather than inventing semantics. Preserve unrelated text and instructions.
- [ ] Make the candidate gate use real Task semantic inventory where available/required, without executing owner commands as part of setup. Compare prior owner task names to candidate names while accounting only for checksum-proven managed retirement. The tests execute harmless fixture owner commands before/after; candidate validation never launches user services. Do not reject legitimately conditional commands due missing project files in the temporary validation tree: provide enough Taskfile/include context or scope checks correctly.
- [ ] Disable continuity without dangling generated aliases, retain custom commands, preserve idempotence and atomic candidate-map refusal.
- [ ] Run `python3 -B -m unittest discover -s tests -p test_root_tasks.py -v`, `test_continuity_integration.py`, `test_wizard.py`; run `task check` and `git diff --check`; commit only owned paths. Report exact RED/GREEN commands and real owner execution/inventory evidence.

## Task 2: Apply one scratch eligibility policy locally, remotely and in Mutagen

**Ownership:** `devcontainer_setup/project_sync/scratch.py`, optional small `scratch_tree.py` module, scratch tests in `tests/test_scratch_sync.py`, `.github/workflows/ci.yml` and focused continuity documentation about excluded caches/backups. Do not edit worker conversation logic reserved for Task 3; source edits are sequential.

**Interfaces:** Preserve `_scratch`, `_remote`, `configure`, `retarget`, backup and baseline ownership contracts. A shared eligibility module must be shipped by render's runtime glob and embedded into the remote validation bootstrap. Produce one constant/predicate for current Mutagen excludes `/.gitignore`, `/README.md`, `.DS_Store`, `__pycache__`, `.venv`, `node_modules`, `.cache`, `.project-sync`, `.handoff`, `.mutagen`.

- [ ] Add fail-first local and actual canonical-remote validation fixtures containing `.scratch/memory/keep.md` plus excluded `node_modules/.bin/tool` and `.venv/bin/python` symlinks. Included symlinks, case collisions and special files must still fail. Do not follow links in excluded payload or permit a linked `.scratch` root.
- [ ] Prune excluded entries before descendant validation; generate Mutagen ignore arguments from the same policy. Define backup behavior explicitly: retain ignored marker files and included payloads, exclude the same cache directories so excluded links do not disable normal startup or backup. No silent omission of unreadable included directories.
- [ ] Add an actual pinned two-endpoint Mutagen fixture proving included note create/change/delete still propagates with excluded cache links present, the same baseline survives restart, and included-link refusal remains enforced. Use private daemon/state and bounded cleanup; never touch production sessions.
- [ ] Make CI obtain the already checksum-pinned Linux Mutagen binary from the repository tool manifest, expose `MUTAGEN_TEST_BINARY`, and run real boundary tests as part of the full suite. Fail CI if artifact/checksum/setup is missing instead of silently skipping those tests. Retain Task and no-start GH pins.
- [ ] Run focused scratch tests with the pinned official binary, `task check`, diff check and commit scoped work. Preserve existing actual baseline migration/deletion tests.

## Task 3: Separate connection readiness, transfer directions and truthful cycle status

**Ownership:** `workspace.py`, `replication.py`, worker lifecycle sections of `scratch.py`, `project_sync/__main__.py`, optional small `sync_cycle.py`, associated workspace/replication/CLI/scratch tests, current continuity docs/README. Native publication/capture validation itself remains unchanged unless a proven integration need is reported.

**Interfaces:** Keep `sync_project_data(connection, project, state, remote_root, *, env=None, historical_roots=())` callable; allow an optional progress callback. Keep manifest schema 2 and immutable Store acknowledgement semantics. Add a coherent cycle result with fresh cycle ID, started/completed timestamps, stage and snapshot/receipt IDs plus independent desktop-to-Codespace and Codespace-to-desktop outcomes. `workspace.status` stays offline and must use this result, not infer native availability from a manifest length. Worker source hash/version must invalidate incompatible old runtime results.

- [ ] Add fail-first regressions for the audit fixtures: outbound publish refusal must not suppress inbound capture/receipt, reverse direction failure must not discard outbound success; transcript failure must not prevent verified scratch start/Herdr open; old receipt/live PID and waiting scratch must not satisfy readiness/health; received entry with failed native publication must never count as available.
- [ ] Remove the duplicate foreground full conversation exchange from `workspace.remote`. Verify selected root/identity/transport, persist verified historical scopes, retarget/start the worker, prepare SSH, and open/reuse Herdr when the current scratch connection is prepared. Do not require conversation publication. Report in-progress/degraded history explicitly, preserve normal failure for invalid root/auth/transport. Check local Python/GH/SSH-key/Herdr prerequisites before any stopped-Codespace start where it can reuse existing canonical validators safely. Avoid changing global account selection.
- [ ] Make each direction run independently with bounded controlled errors and retained existing receipts. A direction must include source snapshot ID, receiver receipt when acquired, publication result/counts and a stage outcome. Include capture issues separately from publication failures. Never acknowledge invalid identities or unsupported transfer schemas. A total malformed identity/auth error cannot be swallowed as successful connectivity.
- [ ] Publish cycle start/progress/completion through the worker as one authoritative state contract, including current heartbeat/update and completed cycle/snapshot binding. Use explicit constants for freshness and bounded stage timing. New/partial/failed/stale cycles report pending/degraded, with the previous verified receipt retained separately. Acknowledged-but-not-published history cannot inherit a previous healthy result. Clear resolved current errors when their stage succeeds.
- [ ] Status distinguishes received-byte custody, current verified native availability, pending publication, unsupported history and stale data per direction. Counts come from the receipt-bound result, not all manifest entries or duplicate issues. Remove/redefine the misleading `protected` field. Default `task work:status` is a short readable offline summary; expose full stable detail via `task work:status -- --json` and/or `--details`, without a new top-level command. Update CLI tests and documentation together.
- [ ] Keep disconnect/stop semantics, machine/workspace reuse, restart-baseline retention and the native memory migration intact. Maintain existing supported-history and refusal tests; do not loosen formats or hide unsupported histories.
- [ ] Run focused workspace/replication/CLI/scratch tests and actual local RPC directional fixtures (one failure plus opposite receipt advancement); `task check`, diff check; commit scoped changes. Root will run full suite + live installed smoke after independent review.

## Root acceptance and delivery

- [ ] Review each task against its brief/diff and run relevant real proofs personally. Final whole-branch Astra review uses the complete merge-base diff; resolve all Important findings.
- [ ] Full Python suite with pinned Mutagen, `task check`, source host validator (record local Docker limitations accurately), CI green and generated Taskfile/selected-consumer command smoke.
- [ ] Disposable dirty ordinary checkouts prove source/index unchanged; excluded cache links permit scratch transfer; failure injection proves opposite-direction receipt and Herdr readiness survive transcript faults; stale/partial cycle truthfulness demonstrated.
- [ ] Push/PR/merge exact reviewed tree, copy retained reports before worktree removal, prune worktree first then merged local/remote branch, verify remote deletion. Fast-forward canonical main.
- [ ] Render consumer preserving selections, owner tasks, instruction/settings bytes and all build/features/lifecycle settings. Review/apply/check/commit/push docs branch; explicitly deliver committed helper update to ordinary Codespace checkout without source sync.
- [ ] Installed `task work:cs`, repeat/reuse, readable and JSON offline status, scratch roundtrip/cache-link fixture, memory migration idempotence, disconnect and stopped-state status. Leave old checkout/baselines intact and stop Codespace afterward. Verify clean local/remote source+consumer heads.
