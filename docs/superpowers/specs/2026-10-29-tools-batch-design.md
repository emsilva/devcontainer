# Tools on every Codespace: design

Date: 2026-10-29. Ruling 14 step 2 ("tools"). Issues: #82 (OpenCode v2 and PATH), #74 (PATH in
non-interactive SSH), the install half of #83 (ccswap), #89 (the scan misses `az`), and the Plaud CLI.

The owner approved the order (tool installs first, then a separate design for the link and sign-in helper)
and handed the work over for the night: "yeah, go ahead. I need to go to bed, can you drive yourself on this
now?" The calls below were made by the lead without the owner. Each one can be overruled. A builder check
(a fresh reader, 2026-10-29) raised six questions and five contradictions; they are settled here.

## Calls made overnight (most consequential first)

1. **Node becomes a baseline runtime, like Python.** Plaud is an npm package, and the owner agreed that Node
   comes with it ("yeah, every one" to "Plaud should come on every Codespace … Node comes with it"). Every
   generated container gets the Node Feature. A project's chosen Node version wins; otherwise the canonical
   catalog's Node version (`.devcontainer/devcontainer.json`, 24 today), which the renderer already falls back
   to (`render.py:282, 297`). The wizard's `RUNTIME_DEFAULTS['node']` (`__main__.py:32`, also 24) must agree,
   and a test pins that. Blast radius today: none of the three projects changes, because all three already
   select Node (innovate 22, mainstage 24, strata-sap 24, read from each `.devcontainer/devcontainer.json`
   `customizations.devcontainerSetup.selections.runtimes` on 2026-10-29). Rejected: a private pinned Node just
   for Plaud. It is a second Node to pin and checksum, and every project in use already has Node.
2. **The system-level PATH is written at post-create, as root through `sudo -n`.** A build-time step would
   force projects without a Dockerfile from `image` to `build` (`render.py:384-388`, and the rule at
   `render.py:526-529`). Generated scripts already use `sudo -n` (`templates/scripts/apply-dotfiles.sh:17`).
   The step is its own small script, idempotent, and says plainly what to run if `sudo -n` is refused.
   `install-agents.sh` keeps refusing root.
3. **ccswap and Plaud go only into generated containers,** through a new generated-only script, not the shared
   `install-agents.sh`. The canonical container runs claude-swap 0.19.0 and a `cswap-switch` loop
   (`on-create.sh:269-335`) over the same `~/.local/share/claude-swap`; two switchers there could fight over the
   active Claude login. Retiring claude-swap in the canonical container is left for the owner.
4. **OpenCode is pinned to 2.1.4,** passed to the v2 installer as `--version`. v2 releases come from the
   installer's own feed, not npm's `opencode-ai` (whose `latest` is 1.19.5): `@opencode/cli` shipped 2.1.0 to
   2.2.2 between 2026-10-20 and 2026-10-29 (npm registry, read 2026-10-29). A floating "latest" with a
   major-2 check would stop every new Codespace's post-create the day 3.0 ships. 2.1.4 is the version the
   owner installed and ran on innovate on 2026-10-25. Moving the pin is a one-line change. The check requires
   major 2.
5. **Plaud is installed per user from a lockfile in this repo.** Plaud 0.3.15 has eight dependencies with
   caret ranges (its npm tarball's `package.json`, read 2026-10-29), so `npm install @plaud-ai/cli@0.3.15`
   alone is not reproducible. This repo ships a small `package.json` + `package-lock.json` that pins the CLI
   and every transitive dependency with integrity hashes. Setup runs `npm ci --ignore-scripts` into
   `~/.local/share/plaud-cli` and links `~/.local/bin/plaud`.
6. **ccswap is pinned to 0.42.0, with its dependencies held by a committed constraints file.** 0.42.0 is the
   version validated on strata-sap (memory `ccswap-on-codespaces`: it fixed a token-login stall in
   `ccswap auto`). Not the Mac's 0.37.0, and not 0.43.0, which PyPI lists as uploaded 2026-10-29T11:02. Its
   dependencies float (`textual<9,>=8.2.8`, `truststore>=0.10.4` in 0.37.0's metadata), so `uv tool install`
   gets a constraints file with exact versions, generated once and committed.
7. **Plaud needs Node 18 or newer.** When a project's Node is older, the renderer leaves `plaud` out of the
   required tools and the install step skips it with one line saying why. None of the three projects is
   affected.
8. **Out of this batch:** #96–#99 (proposed, never confirmed), the link and sign-in helper (its own design,
   inputs `tmp/codespace-lifecycle/ux-run-2026-10-25/login-helper-research/REPORT.md` and rulings 18–19), the
   sign-in copy (step 4), Herdr's agent integrations, claude-swap in the canonical container, and the roll-out
   to the three projects (deferred by the owner).
9. **Main is pushed once,** after the live demonstration passes, under the standing OK (memory
   `push-generator-fixes-when-green`). No project is upgraded.

## Intent and why

A new Codespace should come up with every tool the owner and the agents use, reachable from every shell they
use, without hand installs. On 2026-10-25 the owner had to install OpenCode by hand (it was installed but not on
PATH, and on the wrong major line) and install `az` by hand on strata-sap; Plaud and ccswap were never there.
Herdr's install also warns on connect, because `~/.local/bin` is not on the PATH of a non-interactive SSH
session (#74). Our checks passed through all of it, because they set PATH themselves before checking (#82).

## Goals

- `codex`, `claude`, `opencode` (2.1.4), `ccswap` (0.42.0), `plaud` (0.3.15) and `node` resolve, and run, in:
  a Herdr pane's interactive zsh; a non-interactive `gh codespace ssh -c <cs> -- '<cmd>'`; `bash -c` inside
  such a session; and a VS Code terminal.
- An existing OpenCode 1.x is replaced on the next setup or `task devcontainer:upgrade:agents`.
- The checks test what a fresh shell sees, and each new check fails on today's code before the fix (red first).
- The scan recommends the Azure CLI when source code runs the `az` binary or the project's agent docs tell
  you to run `az login`, and shows the evidence line.

## Non-goals

- Signing any tool in. Plaud's sign-in stays manual until the link and sign-in helper ships.
- Copying sign-ins from the Mac (step 4, #83's other half).
- Pinning Claude or Codex (#53).
- Fixing PATH for tools that container Features install (`go`, `cargo`, `az` …) if they turn out to be missing
  over non-interactive SSH. The live test records what it sees for them; a gap becomes its own issue.
- Upgrading mainstage, innovate or strata-sap.

## Anti-goals

- A check that sets PATH itself and so cannot fail on the bug it guards (the #82 failure).
- A fix that lives in the dotfiles or `~/.zshrc`, which `chezmoi apply` rewrites, or that works only in VS
  Code (`remoteEnv`).
- Floating transitive dependencies for Plaud or ccswap, or install scripts run from npm packages.
- Anything that changes the Mac's `~/.ssh/config`, Herdr profiles, or a real project Codespace.

## Constraints

- `install-agents.sh` is the shared authority for the canonical container and generated ones, runs as
  `vscode`, and refuses root. It is copied byte for byte into generated projects (`render.py:354`).
- `post-create-selected.sh` runs `install-agents.sh`, then `validate-selected.sh`, then the dotfiles hook. The
  base image ships a stock `~/.zshrc` with oh-my-zsh, and generated containers keep it until the dotfiles
  apply. So an interactive `zsh -ic` check can pass on that file: the red-first checks use non-interactive
  shells.
- `install-agents.sh --check` runs each launcher by absolute path (`install-agents.sh:63-67`), and the same
  `check_agent` decides whether to keep or reinstall (`:83`). A PATH lookup is a new, separate step: it must
  not drive the reinstall decision, or a good binary on an unfixed PATH would be reinstalled on every run.
- Bash style per `AGENTS.md`: `set -euo pipefail`, two-space indents. `task check` (compileall, shellcheck,
  `git diff --check`) and `task test` (unittest) must pass.
- Tests never download: `tests/test_agents.py` fakes `curl` and the vendor installers. The new installs need
  fakes for `uv`, `npm` and `sudo` the same way.
- Live tests run on an innovate test branch, with a Codespace created directly and not paired, billed to
  kyndryl-agentic-ai, and deleted afterwards (ruling 15).

## The design, part by part

### 1. PATH at the origin (#74, part of #82)

- `opencode` is linked into `~/.local/bin`, as `claude` and `codex` already are. ccswap (`uv tool` links into
  `~/.local/bin`) and Plaud join them, so one directory serves every tool.
- A post-create step run as root (call 2) puts `/home/vscode/.local/bin` on the PATH every shell starts with.
  The builder picks the files (candidates: `/etc/environment`, which sshd sessions read through PAM;
  `/etc/zsh/zshenv`; `/etc/profile.d`) and proves them in the live test. The generated `remoteEnv.PATH` stays.
- The canonical container gets the same step if the same mechanism applies; otherwise the builder says why not.

### 2. OpenCode v2 (#82)

- The installer URL becomes `https://opencode.ai/v2/install`, run as `--no-modify-path --version 2.1.4`. Read
  2026-10-29 without running it: the same `INSTALL_DIR=$HOME/.opencode/bin`, `--no-modify-path`, and
  `-v, --version <version>` (lines 19, 30-45, 178-191 of the 492-line script).
- The check requires major 2. A 1.x fails it and is reinstalled at the pin.

### 3. ccswap (install half of #83), generated containers only

- `uv tool install ccswap==0.42.0` with the committed constraints file, as `vscode`, landing in
  `~/.local/bin`. `uv` is a base Feature. ccswap needs Python 3.12 or newer (PyPI `requires_python`).
- The check confirms the launcher and that `ccswap --version` reports 0.42.0; any other version is reinstalled
  at the pin.

### 4. Plaud and Node, generated containers only

- Node is a baseline runtime in the renderer and the wizard, as Python is (call 1).
- The install step runs `npm ci --ignore-scripts` from the shipped lockfile into `~/.local/share/plaud-cli`
  and links `~/.local/bin/plaud` to the package's `bin`, unless Node is older than 18 (call 7).
- The check reads the installed package's `package.json` version (expected 0.3.15) and runs one harmless Plaud
  command. Plaud may have no `--version` flag (2026-10-25 notes); the builder confirms which command is safe.
- Changing the Plaud version means regenerating the lockfile (`npm install --package-lock-only`).

### 5. The scan finds `az` (#89)

- Today the tool patterns are applied only to workflow files: Taskfiles, Dockerfiles, compose files, `.sh`
  scripts, GitHub workflows and `package.json` scripts (`discovery.py:269-290`). Source files are counted but
  never read.
- Add: in source files of the supported languages, a call that runs the `az` binary is evidence, for example
  `exec.Command("az", …)` and `exec.CommandContext(ctx, "az", …)` (Go; strata-sap's `token.go:55` uses the
  second), `subprocess.run(["az", …])` or `"az"` as the first element of an argv list (Python),
  `spawn("az", …)` / `execFile("az", …)` (Node). In agent docs (`CLAUDE.md`, `AGENTS.md`), a line with
  `az login` is evidence.
- The evidence string names the file and says why, as other evidence does. `MAX_FILE_BYTES` and the
  discounted directories apply.
- Red today (builder check, read-only `--scan` of strata-sap): azure is not selected, though its `CLAUDE.md:69`
  mentions `az login`. A managed project reuses its recorded selections (`__main__.py:58-61`), so for
  strata-sap the visible effect is the evidence the scan prints; new projects get the recommendation.

### 6. Checks that can fail

- A new PATH step in `install-agents.sh --check` and in `validate-selected.sh`: look each tool up from a fresh
  non-interactive shell under a clean environment (`env -i` with only `HOME`, `USER` and what the system files
  give), in both `zsh -c` and `bash -c`. The tools: the agents, `ccswap`, `plaud` and `node`. The builder picks
  the exact form.
- `validate-selected.sh` stops prepending PATH, and adds `ccswap`, `plaud` (call 7) and `node` to the commands
  it requires.
- Red first: each new check is shown failing against today's code before the fix lands.

## Live demonstration (definition of done)

Render an innovate test branch from the feature commit:

- `SETUP_DEVCONTAINER_REPO=<this repo or the feature worktree>` and `SETUP_DEVCONTAINER_REF=<feature sha>`
  (a local directory is accepted, `releases.py:99-101`), then `task devcontainer:upgrade:self` in a fresh
  innovate clone. Without them the wizard fetches GitHub `main` and renders today's generator
  (`__main__.py:475-484, 965-967`).
- The wizard renders in a worktree cut from `origin/main` (`__main__.py:406-418, 838`). Choose "Commit only"
  (never a Codespace or PR from the wizard), then push the setup branch as `test/tools-batch-<date>` and delete
  the local `setup/devcontainer` branch.

Create a Codespace directly with `gh codespace create` on that branch, not paired. Then:

- `gh codespace ssh -c <cs> -- 'command -v codex claude opencode ccswap plaud node'` finds all six, and so do
  `… -- bash -c '…'` and `… -- zsh -ic '…'`. Record, without gating on it, what the same lookups give for the
  Feature tools (`az`, `go`, `docker`, `task`).
- `opencode --version` is 2.1.4; `ccswap --version` is 0.42.0; Plaud's package version is 0.3.15, and Plaud runs.
- `bash .devcontainer/scripts/validate-selected.sh` and `install-agents.sh --check` exit 0 on the Codespace.
- Replacing a 1.x: install OpenCode 1.x into `~/.opencode/bin` by hand, run `install-agents.sh`, and see 2.1.4
  back, with no PATH line added to `~/.zshrc`.
- The test Codespace and the test branch are deleted, and `gh codespace list` no longer shows it.

On the Mac: `bash scripts/setup.sh --project ~/code/sm-consult-agent-strata-sap --scan` cites the `az`
evidence (`token.go` and `CLAUDE.md`) and changes nothing.

## Left to the builder

- Which system files carry the PATH, and the exact clean-environment form of the checks.
- The Node baseline's wiring in `render.py` and the wizard, mirroring Python, and its tests.
- Where the Plaud lockfile and the ccswap constraints live (for example `templates/tools/`), and how they reach
  generated projects (copied like `install-agents.sh`).
- The new generated-only script's name, and how `post-create-selected.sh` calls it.
- Which Plaud command the check runs.
- The exact `az` call patterns per language, each with a test showing a positive and a negative case.
