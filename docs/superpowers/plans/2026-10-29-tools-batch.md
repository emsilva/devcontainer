# Tools on every Codespace: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo the lead runs it through the herd: Codex xhigh builders in worktrees, one brief per stream.

**Goal:** Every generated Codespace has `codex`, `claude`, `opencode` 2.1.4, `ccswap` 0.42.0, `plaud` 0.3.15 and
`node`, reachable from every shell, with checks that fail when they are not, and the scan recommends `az` when
code runs it.

**Architecture:** Two streams. Stream A (Tasks 1–4) changes the generator's install and check scripts and the
renderer: Node as a baseline Feature, OpenCode v2 pinned and linked into `~/.local/bin`, a root post-create step
that puts `~/.local/bin` on the system PATH, a generated-only `install-tools.sh` for ccswap and Plaud from
committed locks, and a fresh-shell PATH check. Stream B (Task 5) teaches `discovery.scan` to read source files
for calls that run `az`. Task 6 is the lead's live demonstration on a throwaway innovate Codespace.

**Tech Stack:** Bash (`set -euo pipefail`), Python 3.11+ stdlib (`unittest`), Dev Container Features, npm
lockfiles, uv.

**Spec:** `docs/superpowers/specs/2026-10-29-tools-batch-design.md` (commit 1061794). Read it first; its
"Calls made overnight" are decisions, not suggestions.

## Global Constraints

- Pins: OpenCode `2.1.4` (v2 installer `https://opencode.ai/v2/install`, args `--no-modify-path --version 2.1.4`);
  ccswap `0.42.0`; Plaud `@plaud-ai/cli` `0.3.15`. Plaud needs Node 18 or newer.
- Node's default version is the canonical catalog's (`.devcontainer/devcontainer.json` features
  `ghcr.io/devcontainers/features/node:1` `version`, `24` today); `RUNTIME_DEFAULTS['node']` must equal it.
- Every tool lands in `/home/vscode/.local/bin` (opencode as a symlink to `~/.opencode/bin/opencode`).
- Nothing writes PATH into `~/.zshrc`, `~/.bashrc` or any dotfile. The generated `remoteEnv.PATH` stays.
- `install-agents.sh` stays shared (canonical + generated, copied byte for byte), runs as `vscode`, refuses root.
  ccswap and Plaud are generated-only and never go into `install-agents.sh`.
- Root work only through `sudo -n`, in its own script. A refused `sudo -n` prints one line naming the command to
  run by hand and exits non-zero.
- Installs are reproducible: Plaud by `npm ci --ignore-scripts` from a committed `package-lock.json`; ccswap by
  `uv tool install` with a committed constraints file of exact versions.
- Tests never touch the network or the real `/etc`: fake `curl`, `npm`, `uv` and `sudo` on PATH, and a test-only
  `DEVCONTAINER_SYSTEM_ROOT` prefix (default empty) for every system path.
- New checks are red first: show each failing on the base commit before the fix.
- Gates: `task check` and `task test` exit 0. Bash style per `AGENTS.md`. Commit per task, imperative sentence
  case, no AI attribution.

## Review Focus

1. **Post-create runs again** (rebuild, `task devcontainer:upgrade:agents`): the system PATH line gains no
   duplicate entry, and ccswap/Plaud at the pin are kept, not reinstalled. Tests in Tasks 3 and 4.
2. **`sudo -n` is refused** (no passwordless sudo): one line naming `sudo bash .devcontainer/scripts/system-path.sh`,
   non-zero exit, nothing half-written. Test in Task 3.
3. **A failed download mid-install** (npm or uv exits non-zero): non-zero exit, no `~/.local/bin/plaud` or
   `ccswap` link left pointing at a partial install, and the next run installs again. Test in Task 4.
4. **A different version is already there** (OpenCode 1.19.4; a global `npm -g` Plaud; ccswap 0.37.0): the
   pinned one is installed and is what a fresh shell resolves. Tests in Tasks 2 and 4.
5. **A project selects Node older than 18:** Plaud is skipped with one line, and not required by the checks.
   Tests in Tasks 1 and 4.

---

### Task 1: Node as a baseline runtime

**Files:**
- Modify: `devcontainer_setup/render.py` (`BASE_FEATURES`, the runtime and command wiring near lines 275–340,
  `validate()` near 503)
- Modify: `devcontainer_setup/__main__.py` (runtime screen label near line 499; summary text near 858)
- Test: `tests/test_render.py`, `tests/test_wizard.py`

**Interfaces:**
- Produces: `render.plaud_supported(node_version: str) -> bool` (major ≥ 18; `'24'`, `'22.3'`, `'lts'`-style
  strings the renderer already accepts → parse the leading integer; non-numeric → True). Task 4 uses it to decide
  whether `plaud` joins `__TOOLS__`.
- Produces: every rendered config has `ghcr.io/devcontainers/features/node:1`, version = selected or catalog.

- [ ] **Step 1: Write the failing tests.**
  - `test_node_is_a_baseline_runtime`: `self.render()` (no runtimes) → `NODE in features`,
    `features[NODE]['version'] == canonical catalog node version`, and `validate()` of the installed render is `[]`.
  - `test_project_node_version_wins`: `runtimes={'node': '22'}` → version `'22'`.
  - `test_wizard_node_default_matches_catalog`: `RUNTIME_DEFAULTS['node'] ==` the catalog's node version.
  - `test_validate_requires_node_feature`: remove `NODE` from an installed config's features and its managed
    snapshot → `validate()` reports `Missing selected feature: ghcr.io/devcontainers/features/node:1`.
  - Change `test_native_agents_are_mandatory_without_node_dependency`: drop `assertNotIn(NODE, config['features'])`
    (Node now comes for Plaud, spec call 1) and keep `assertNotIn('node', metadata['dependencies'])` and the rest.
    Rename it `test_native_agents_are_mandatory_and_record_no_node_dependency`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_render tests.test_wizard -v` → the new tests FAIL.
- [ ] **Step 3: Implement.** Add `FEATURE + 'node:1'` to `BASE_FEATURES` (so `validate()`'s `required` includes it).
  Keep `test_native_agents_do_not_restrict_project_node_version` (Node 16 still renders). Runtime screen label:
  `Project runtimes (Python, Node and uv included)`; keep Node selectable for its version. Add `plaud_supported`.
- [ ] **Step 4: Run** `task test` → PASS (all suites).
- [ ] **Step 5: Commit** `Make Node a baseline runtime for the Plaud CLI`.

### Task 2: OpenCode v2, pinned and linked

**Files:**
- Modify: `.devcontainer/scripts/install-agents.sh`
- Test: `tests/test_agents.py`

**Interfaces:**
- Produces: `~/.local/bin/opencode` → `~/.opencode/bin/opencode` symlink after install; `check_agent opencode`
  fails unless `opencode --version` (last whitespace field, leading `v` stripped) starts with `2.`.

- [ ] **Step 1: Write the failing tests** (extend the fakes: `curl` serves `https://opencode.ai/v2/install`; the
  opencode fake prints a version from `$TEST_OPENCODE_VERSION`, default `2.1.4`).
  - `test_opencode_v2_installer_is_pinned`: install → calls contain `https://opencode.ai/v2/install` and
    `install opencode --no-modify-path --version 2.1.4`; never `https://opencode.ai/install`.
  - `test_opencode_is_linked_into_local_bin`: `~/.local/bin/opencode` is a symlink resolving to
    `~/.opencode/bin/opencode`.
  - `test_opencode_1x_is_replaced`: pre-create a complete 1.x layout printing `1.19.4` → plain install reinstalls
    opencode (one `install opencode` call) and `--check` then passes.
  - `test_check_fails_for_opencode_1x`: a 1.x layout → `--check` exits non-zero.
  - Update `test_install_idempotent_update_and_check` for the new URL and arguments; it must still create no
    `~/.*rc`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_agents -v` → new tests FAIL.
- [ ] **Step 3: Implement** in `install-agents.sh`: URL, arguments, a `OPENCODE_VERSION=2.1.4` constant near the
  top, the major-2 test inside `check_agent` (it is part of installation completeness, so it may drive reinstall),
  and `ln -sfn "$HOME/.opencode/bin/opencode" "$HOME/.local/bin/opencode"` after the opencode install; the layout
  check also requires that symlink.
- [ ] **Step 4: Run** `task check && task test` → PASS.
- [ ] **Step 5: Commit** `Install OpenCode 2.1.4 and link it beside the other agents (#82)`.

### Task 3: `~/.local/bin` on the system PATH, and a check from a fresh shell

**Files:**
- Create: `templates/scripts/system-path.sh` (rendered to `.devcontainer/scripts/system-path.sh`)
- Modify: `templates/scripts/post-create-selected.sh`, `templates/scripts/validate-selected.sh`,
  `devcontainer_setup/render.py` (`helper_names`, `validate()` required scripts)
- Test: `tests/test_render.py` (rendered files), new `tests/test_system_path.py`

**Interfaces:**
- Produces: `system-path.sh` (run as root): ensures `/home/vscode/.local/bin` is the first entry of the `PATH=`
  line in `${DEVCONTAINER_SYSTEM_ROOT}/etc/environment` (creates the line from the running PATH if absent; never
  duplicates), and adds the same prefix in `${DEVCONTAINER_SYSTEM_ROOT}/etc/zsh/zshenv` inside a marked block
  (`# devcontainer-setup PATH` … `# end devcontainer-setup PATH`), replaced in place on reruns. The builder may
  choose different files if the live test (Task 6) shows these are not what SSH sessions read; record why.
- Produces: `validate-selected.sh` function `fresh_shell_lookup <tool>`: with `PATH` taken only from the
  `PATH=` line of `${DEVCONTAINER_SYSTEM_ROOT}/etc/environment`, runs `env -i HOME="$HOME" USER="$USER"
  PATH="<that>" bash -c 'command -v <tool>'` and the same with `zsh -c` (skip zsh with a note when zsh is absent),
  and reports `Not on the PATH of a fresh shell: <tool>` for each miss. It runs for `codex claude opencode
  ccswap plaud node` (Task 4 adds the last three to the list; `plaud` only when Task 1's `plaud_supported`).
- Post-create order: `sudo -n bash .devcontainer/scripts/system-path.sh` → `install-agents.sh` →
  `validate-selected.sh` → dotfiles hook (Task 4 inserts `install-tools.sh` after `install-agents.sh`). `validate-selected.sh` no longer
  prepends PATH (its existing `command -v` loop runs in the post-create environment as it is).

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_system_path.py`: with a temp `DEVCONTAINER_SYSTEM_ROOT`:
    `test_prepends_local_bin_once` (existing `PATH="/usr/local/bin:/usr/bin"` → `PATH="/home/vscode/.local/bin:/usr/local/bin:/usr/bin"`;
    second run identical bytes); `test_creates_path_line_when_absent`; `test_zshenv_block_is_replaced_not_appended`.
  - `test_refused_sudo_names_the_command` (render the post-create script, run it with a fake `sudo` that exits 1
    for `-n`): exit non-zero, stderr contains `sudo bash .devcontainer/scripts/system-path.sh`.
  - `test_fresh_shell_lookup_fails_without_system_path` (red-first proof): a tool only in a temp
    `~/.local/bin`, `/etc/environment` without it → `validate-selected.sh` exits non-zero naming the tool; after
    `system-path.sh` → that lookup passes. Run this test on the base commit too and record the failure.
  - Render tests: `.devcontainer/scripts/system-path.sh` is rendered and registered (validate reports
    `Missing managed script registration` without it); post-create order as above; `validate-selected.sh`
    contains no `export PATH=`; `test_native_agents_are_mandatory_…`'s
    `assertIn('export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"', post)` is updated to the new script.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_system_path tests.test_render -v` → FAIL.
- [ ] **Step 3: Implement** `system-path.sh` (`#!/usr/bin/env bash`, `set -euo pipefail`, refuse non-root unless
  `DEVCONTAINER_SYSTEM_ROOT` is set, atomic write via temp file + `mv`), the post-create line with the refusal
  message, `fresh_shell_lookup` in `validate-selected.sh`, and the render wiring. Canonical container: apply the
  same step from `.devcontainer/scripts/post-create.sh` if it fits in a few lines; otherwise leave a one-line
  comment there saying why not.
- [ ] **Step 4: Run** `task check && task test` → PASS.
- [ ] **Step 5: Commit** `Put ~/.local/bin on the PATH of every shell and check it from a fresh one (#74, #82)`.

### Task 4: ccswap and Plaud in generated containers

**Files:**
- Create: `templates/scripts/install-tools.sh` (→ `.devcontainer/scripts/install-tools.sh`)
- Create: `templates/tools/plaud-cli/package.json`, `templates/tools/plaud-cli/package-lock.json`
  (→ `.devcontainer/tools/plaud-cli/`), `templates/tools/ccswap-constraints.txt`
  (→ `.devcontainer/tools/ccswap-constraints.txt`)
- Modify: `devcontainer_setup/render.py` (files, `__TOOLS__`, `validate()` required scripts),
  `templates/scripts/post-create-selected.sh`, `templates/scripts/validate-selected.sh`,
  `templates/Taskfile.yml` (`upgrade:agents` also runs `install-tools.sh --update`)
- Test: new `tests/test_install_tools.py`, `tests/test_render.py`

**Interfaces:**
- Consumes: `render.plaud_supported` (Task 1); `fresh_shell_lookup` (Task 3).
- Produces: `install-tools.sh [--update|--check]`, as `vscode`, refusing root, same modes as `install-agents.sh`:
  - ccswap: complete when `~/.local/bin/ccswap --version` reports `0.42.0`; else
    `uv tool install --force --constraints .devcontainer/tools/ccswap-constraints.txt ccswap==0.42.0`.
  - Plaud: skipped with `Plaud needs Node 18 or newer; this container has Node <v>` when `node --version` major
    < 18. Complete when `~/.local/share/plaud-cli/node_modules/@plaud-ai/cli/package.json` version is `0.3.15`,
    the `~/.local/bin/plaud` symlink resolves into that tree, and one harmless Plaud command exits 0 (builder
    picks it: `plaud --help` if it prints usage without network or sign-in; confirm by reading
    `dist/index.js`). Else: copy the two lock files into a fresh temp dir, `npm ci --ignore-scripts
    --prefix <temp>`, then replace `~/.local/share/plaud-cli` by `mv`, then link. A failure leaves no link.
- Lock generation (once, committed; the command goes in a comment at the top of each file or in a short
  `templates/tools/README.md`): `npm install --package-lock-only --ignore-scripts` for `package.json` =
  `{"private": true, "dependencies": {"@plaud-ai/cli": "0.3.15"}}`; `uv pip compile --universal
  --python-version 3.12` of `ccswap==0.42.0` with exact `==` pins and no hashes.

- [ ] **Step 1: Write the failing tests** (`tests/test_install_tools.py`, fakes for `uv`, `npm`, `node` like
  `tests/test_agents.py`):
  `test_installs_pinned_ccswap_and_plaud` (calls show `ccswap==0.42.0` with the constraints path and
  `npm ci --ignore-scripts`; both links exist); `test_rerun_keeps_both` (second run: no new install calls);
  `test_other_versions_are_replaced` (ccswap 0.37.0, Plaud 0.3.14 → reinstalled at the pins);
  `test_failed_npm_leaves_no_link` (fake npm exits 22 → non-zero exit, no `~/.local/bin/plaud`, rerun installs);
  `test_old_node_skips_plaud` (fake `node --version` → `v16.20.2` → skip line, exit 0, no npm call);
  `test_refuses_root_and_unknown_arguments`.
  Render tests: the five files are rendered and registered; `__TOOLS__` contains `ccswap`, `plaud`, `node` for
  the default render and omits `plaud` for `runtimes={'node': '16'}`; post-create calls `install-tools.sh` after
  `install-agents.sh` and before `validate-selected.sh`; `validate-selected.sh` calls `install-tools.sh --check`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_install_tools tests.test_render -v` → FAIL.
- [ ] **Step 3: Generate the locks** with the commands above (network is allowed for this step only), then
  implement `install-tools.sh` and the render wiring. Read the lock's resolved versions back and list them in the
  commit message body.
- [ ] **Step 4: Run** `task check && task test` → PASS.
- [ ] **Step 5: Commit** `Install ccswap 0.42.0 and the Plaud CLI 0.3.15 on generated Codespaces (#83)`.

### Task 5: The scan finds `az` in code (stream B)

**Files:**
- Modify: `devcontainer_setup/discovery.py` (`scan`, near lines 162–290)
- Test: `tests/test_discovery.py`

**Interfaces:**
- Produces: `found['tools']['azure']` selected with evidence `"<path>: runs the az CLI"` for source calls and
  `"<path>: agent docs run az login"` for agent docs. No other tool changes.

- [ ] **Step 1: Write the failing tests.**
  - `test_go_exec_of_az_selects_azure`: `tools/mvp/azure/token.go` containing
    `cmd := exec.CommandContext(ctx, "az", "account", "get-access-token")` → selected, evidence names the file.
    Also `exec.Command("az", "login")`.
  - `test_python_and_node_exec_of_az_select_azure`: `subprocess.run(["az", "login"])`,
    `subprocess.check_output(['az', 'account', 'show'])`, `spawn("az", ["login"])`, `execFile('az', [...])`.
  - `test_root_agent_docs_with_az_login_code_select_azure`: `CLAUDE.md` with ``auth is Entra, `az login` `` →
    selected; same for a fenced block line `az login` in `AGENTS.md`.
  - Negatives: `test_az_substrings_and_prose_do_not_select_azure`: `"lazy"`, `exec.Command("azcopy")`,
    `print("run az login")` in a `.py` string that is not an argv, a `CLAUDE.md` line `We use Azure` without code,
    `docs/CLAUDE.md` (discounted directory). The existing
    `test_malformed_manifest_is_visible_and_no_prose_cloud_selection` and
    `test_compose_source_files_and_docs_do_not_recommend_tools` must stay green unchanged.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_discovery -v` → new positives FAIL.
- [ ] **Step 3: Implement.** Read files whose suffix is in the existing `suffix_runtime` map (same
  `MAX_FILE_BYTES`, `DISCOUNTED`, symlink rules) for argv-style calls whose program is exactly `az`; read root
  `CLAUDE.md`/`AGENTS.md` for `az login` inside backticks or a fenced block. Only when the file contains the
  bytes `az` at all (cheap prefilter).
- [ ] **Step 4: Run** `task test`, then `bash scripts/setup.sh --project ~/code/sm-consult-agent-strata-sap --scan
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["tools"]["azure"])'` → selected with `token.go`
  evidence; `git -C ~/code/sm-consult-agent-strata-sap status --porcelain` unchanged before and after.
- [ ] **Step 5: Commit** `Recommend the Azure CLI when code runs az (#89)`.

### Task 6: Live demonstration (lead only)

Run by the lead after Tasks 1–5 are merged into local `main` and re-gated. Spec "Live demonstration" is the
checklist; record every command and result in `tmp/codespace-lifecycle/tools-batch/LIVE.md`.

- [ ] **Step 1:** `git clone https://github.com/kyndryl-agentic-ai/consult-sm-innovate.git
  /Users/mannu/code/devcontainer/tmp/livetest-tools/innovate-tools-test`.
- [ ] **Step 2:** In a Herdr shell pane, in that clone: `export SETUP_DEVCONTAINER_REPO=/Users/mannu/code/devcontainer
  SETUP_DEVCONTAINER_REF=main` (the local `main`; `upgrade_handoff` fetches it, `__main__.py:475-484`) then
  `task devcontainer:upgrade:self`; answer per memory `project-setup-upgrade-recipe` step 2 ("Commit only").
  Check `git diff main setup/devcontainer --stat` shows the new scripts, tool locks and the changed
  `devcontainer.json`, and `sourceRef` equals the main sha.
- [ ] **Step 3:** `git checkout -b test/tools-batch-20261030 setup/devcontainer && git push origin
  test/tools-batch-20261030 && git branch -D setup/devcontainer`.
- [ ] **Step 4:** `gh codespace create --repo kyndryl-agentic-ai/consult-sm-innovate --branch
  test/tools-batch-20261030 -m basicLinux32gb --idle-timeout 120m --devcontainer-path
  .devcontainer/devcontainer.json`; record the name in LEAD-STATE at once. Wait for post-create:
  `gh codespace ssh -c <cs> -- 'test -f /workspaces/consult-sm-innovate/.devcontainer/scripts/install-tools.sh && echo ready'`
  and the creation log (`gh codespace logs -c <cs>`) showing post-create finished, exit 0.
- [ ] **Step 5:** The spec's checks, each recorded with its output: `command -v` in the three shells, versions,
  `validate-selected.sh` and `install-agents.sh --check` exit 0, the Feature tools recorded, the 1.x
  replacement (install 1.x with `curl -fsSL https://opencode.ai/install | bash -s -- --no-modify-path`, then
  `bash .devcontainer/scripts/install-agents.sh`, then `opencode --version` and `grep -c opencode ~/.zshrc`).
- [ ] **Step 6:** Clean up: `gh codespace delete -c <cs> --force`; `git push origin --delete
  test/tools-batch-20261030`; remove the clone; `gh codespace list` and `git ls-remote --heads origin
  test/tools-batch-20261030` show nothing.
- [ ] **Step 7:** Push `main` with the emsilva token (memory `push-generator-fixes-when-green`), then comment on
  #82, #74 and #89 with the evidence and close them; comment on #83 that its install half shipped.
