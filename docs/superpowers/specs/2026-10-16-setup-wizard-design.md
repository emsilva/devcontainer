# Project-aware devcontainer setup wizard

Date: 2026-10-16

Status: approved by owner on 2026-10-16; implementation in progress.

## Goal

Run a short terminal wizard from a project checkout, accept recommendations about
its tools and Codespaces resources, review the generated changes, then approve a
commit and, optionally, a push and Codespaces launch. Each project receives only
the configuration and helpers needed by its selections.

Implement this in `emsilva/devcontainer`, where the current installer originates.
Mainstage is the first consuming project, not the owner of a separate installer.

## Current behavior and origin of the problem

The source inspected is `ec987b5a09f97c86e7f4d55b410bcf82cced9bc9`.
Its `.devcontainer` contains 11 files totaling 56,133 bytes, but selects 20 Features,
five language runtimes, three cloud CLIs, Docker, and personal tooling. The source
files' size understates the resulting installation work. Image size and build
time have not been measured.

`install.sh` removes the target's existing `.devcontainer`, copies the entire
canonical directory, stages it, and commits immediately. `upgrade:self` calls
that installer again. Fix selection and update behavior at this producer rather
than copying everything and asking individual projects to prune it afterward.

## Agreed product requirements

- A small terminal UI asks only the questions needed for the project.
- Repository discovery preselects recommendations and explains the evidence.
- Python and `uv` are always included for agent scratch work as well as projects.
- Dotfiles application and coding-agent tools initially default to Yes.
- Selecting dotfiles asks for a repository, accepting `emsilva/dotfiles` directly.
- User preferences persist across projects outside their repositories.
- Codespaces machine choices are fetched for the actual target repository.
- A final review shows the changes and precisely names the approved actions.
- Commit-only and commit/push/create/open flows are available.
- The wizard preserves existing work and project customizations.

## Entrypoints and implementation boundaries

Use Gum for terminal selectors, multi-select lists, confirmations, and progress.
Bash orchestrates Gum, Git, and GitHub CLI; Python standard-library code handles
repository inventory, structured configuration, preferences, and rendering.
The host wizard requires Python 3.11 or newer for TOML parsing. The bootstrap
checks prerequisites and gives installation instructions for missing tools.
Tool downloads, if offered, must use pinned, checksum-verified releases and a
user-owned location; do not change global runtime or agent configuration.

There is one setup implementation, with these entrypoints:

- Initial installation: `install.sh` delegates to the wizard instead of copying
  the complete template and committing automatically. Interactive input uses the
  terminal, so a downloaded installer does not consume its script stream as input.
- Existing consumers: `task devcontainer:setup` invokes the same versioned wizard
  through a small Taskfile include.
- Canonical checkout: `task setup -- --project /path/to/project` supports first use
  before a consumer has the Taskfile include.
- `task devcontainer:check` validates the project configuration without changing it.
- `task devcontainer:upgrade:self` proposes a deliberate version update through the
  same review flow, preserving the selected tools and project settings.

Keep shared generator code in the canonical release/cache. Consumers receive a
small Taskfile include and only helpers their container actually needs. Do not
copy the wizard implementation, its tests, or unrelated management scripts into
every consumer. Use immutable source revisions for installed runners.

Keep Taskfile as the operations entrypoint. Preserve consumers' existing
`up`/`down`/`status`, build, test, and deploy tasks. Provide corresponding
devcontainer lifecycle and status targets in the canonical project. Any selected
service launch must survive the invoking shell, log to an ignored file, and have
an idempotent stop/status operation based on the running service.

## Wizard flow

1. Identify the checkout, its GitHub repository, branch, and current changes.
2. Read user preferences and scan the project without executing project code.
3. Present recommended runtimes and tools with reasons; allow changes.
4. Ask about dotfiles, their repository, and coding-agent tools.
5. Choose commit-only, or launch in the browser or desktop VS Code after commit.
6. For launch, select an available machine and an idle timeout. Put region and
   retention choices under Advanced. Initially suggest a 30-minute idle timeout.
7. Generate a temporary candidate and validate it. Show a summary, changed-file
   list, diff, destination branch, and actions; allow editing the answers.
8. Ask the final confirmation, apply the reviewed changes, run validation and
   normal commit hooks, and commit the exact reviewed setup files.
9. Save the confirmed personal preferences. If selected, push the branch,
   create the codespace, personalize it, and open the selected client.

The commit-only confirmation is `Commit these setup files?`. The launch
confirmation additionally names pushing the displayed branch and creating and
opening a codespace with the selected resources. One confirmation authorizes
those displayed actions; no hidden push or resource creation is implied by a
commit-only answer. Cancelling before confirmation changes neither the checkout
nor saved preferences.

If a codespace for the chosen repository and branch already exists, offer reuse
or creation. Reuse must update to the reviewed remote commit and rebuild if the
devcontainer configuration changed; never discard remote uncommitted work.

## Repository discovery and recommendations

Use Git's tracked and untracked, unignored file inventory, including nested apps.
Do not read ignored scratch directories, dependency trees, or caches. Discount
tracked vendor/generated files and examples/fixtures so they do not establish a
production stack on their own. Inspect manifests, version declarations, CI,
Dockerfiles, Taskfiles, source paths, and `.gitignore` as data, never by sourcing
or executing them.

| Strength | Signals | Recommendation behavior |
| --- | --- | --- |
| Strong | Manifests, explicit runtime pins, CI setup, Docker base/runtime declarations, actual build/run commands | Preselect the relevant runtime/tool and show the file evidence |
| Supporting | First-party source extensions and recognizable framework structure | Suggest when corroborated; explain weaker evidence |
| Weak | Ignore patterns such as `.venv/`, `node_modules/`, `.bundle/` | Show as hints; never select a runtime from these alone |

Recognize at least these language families:

- Python: `pyproject.toml`, requirements files, `Pipfile`, Poetry/uv locks,
  `.python-version`, Python source, and Python CI/container declarations.
- Ruby: `Gemfile`, `Gemfile.lock`, `.ruby-version`, gemspecs, Rails structure,
  Ruby source, and Ruby CI/container declarations.
- Go: `go.mod`, `go.work`, `go.sum`, Go source, and Go CI/container declarations.
- Node: nested `package.json`, npm/pnpm/Yarn/Bun lockfiles, package `engines`,
  `.node-version`, `.nvmrc`, JS/TS sources, and CI/container declarations.

Recommend package managers from the project's manifests and locks. Conflicting
locks or incompatible runtime declarations require a visible selection rather
than an arbitrary version or installing every manager. Version declarations
outrank source extensions. Distinguish development/build stages from runtime
stages when explaining Docker evidence.

For Python, use the project's compatible declared runtime when present; otherwise
use the canonical supported baseline. Make an additional interpreter an explicit
choice only when incompatible project constraints require one. The baseline
does not imply scientific, document, or application packages are installed.
Agents can create isolated scratch environments with `uv`.

Cloud/container/tool recommendations follow declared workflows as well as
language detection: for example, Mainstage's `az`, `devtunnel`, and Docker-backed
policy tasks support those selections. A vendor name in prose is insufficient.
Language alone does not determine an optimal Codespaces machine size.

The initial common base includes Python/uv, Git, Task, curl, jq, ripgrep, and the
small process/network utilities needed by selected project tasks. Optional
bundles cover Node, Ruby/Rails, Go, Rust, cloud CLIs, Docker, database clients,
and SSH access. The agent bundle includes Claude Code and Codex. Additional
orchestration and account-rotation tooling stays under Advanced. Show transitive
runtime dependencies of selected tools in the review summary.

## User preferences

Read `${XDG_CONFIG_HOME:-$HOME/.config}/devcontainer-setup/config.json`.
An unset, empty, or relative `XDG_CONFIG_HOME` falls back to `$HOME/.config`.
Keep executables, cached releases, and preferences in their appropriate separate
user directories; configuration does not belong in a project or cache.

The initial persisted preference schema is:

```json
{
  "version": 1,
  "apply_dotfiles": true,
  "dotfiles_repo": "emsilva/dotfiles",
  "agentic_tools": true
}
```

On first use the booleans default to true, and the repository field asks for
input; the value above illustrates a user's answer, not a hardcoded owner.
On later runs, prefill confirmed saved values and allow editing. Explicit answers
for this run take precedence over saved preferences, which take precedence over
built-in defaults. Recompute language/tool recommendations for each project.

Write preferences atomically after successful local setup/commit, preserving
unrelated recognized fields. Create the preference directory with mode 0700 and
the file with mode 0600. A launch failure after the commit does not lose valid
preferences. A malformed file receives an actionable explanation and is not
silently overwritten. A failed preference write does not conceal a completed
commit; report the result and the path the user can repair.

Do not persist tokens in this file. Personal dotfiles locations, launch-client
preferences, and selected machine SKUs stay outside generated shared project
configuration. Project features and minimum resource requirements belong in Git;
an individual's preferred machine SKU is passed when creating their codespace.

## Dotfiles application

When enabled, require a nonempty repository and accept GitHub `owner/repo`
shorthand or a Git URL. Prefill the saved value on later runs. Check syntax and,
when possible, access before the final review; show access failures instead of
claiming the repository is usable. Apply through the existing chezmoi helper,
equivalent to `chezmoi init --apply -- <repo>`.

Install chezmoi and its reusable helper in selected environments, but do not bake
personal dotfiles or repository URLs into shared images, prebuilds, or project
commits. Apply only inside the user's development container, never to the host
as a side effect of the setup wizard.

For local container creation, pass the choice through gitignored local runtime
configuration. For the wizard's Codespaces launch, pass it explicitly to the
remote personalization step after creation using GitHub CLI. The launch path
must include a working SSH capability for this step and show that dependency in
the selected configuration. Do not assume that a host-side preference file is
present in the codespace. Existing `DOTFILES_REPO` users retain their supported
per-user flow; avoid repeating an already successful application of the same
repository during one launch.

Private dotfiles use the configured authentication available to the operation.
Missing remote access is a personalization failure with a retry path, not a
reason to replace global credentials, unset unrelated auth tokens, or falsely
report complete setup. The retry command includes the chosen repository.

## Generation, reruns, and updates

Generate standard `devcontainer.json`, a matching `devcontainer-lock.json`, and
only the chosen container helpers and Taskfile integration. Record generator
revision and nonpersonal project selections in a namespaced customization entry
inside the JSON, not a second competing project configuration format.

Preserve user-owned ports, commands, settings, and files. Compare managed content
against its recorded source before replacement; stop and show conflicts when an
update would overwrite local changes. Never recursively remove `.devcontainer`
as the update strategy. Cleanup of obsolete managed files must be explicit in
the reviewed diff and must not remove modified or user-owned files.

Repeated setup with the same inputs and pinned dependencies produces no diff.
Ordinary setup reuses existing resolved locks; refreshing versions is an explicit
upgrade. Pin actual installed tool versions where supported as well as Feature
artifacts, since a locked installer can still download a tool's `latest` release.
Validation checks the resolved selected surface, not every tool in the old
polyglot template. Retain the current broad selection as an explicit polyglot
preset for users who want it.

Do not switch Docker modes merely to reduce apparent weight. Mainstage's OPA
tasks bind-mount the workspace: the selected Docker mode must support those
paths on the target platform. Keep Docker-in-Docker available and verify any
host-socket alternative against that workflow before recommending it.

## Commit, push, and Codespaces launch

Allow unrelated existing edits to remain in the checkout. Refuse a commit when
unrelated changes are already staged, and stop on edits to files the wizard
would need to own. Stage only the exact reviewed setup paths. Never stash,
reset, or include somebody else's changes to make the flow pass.

On a default branch, propose a setup branch before committing. Display the chosen
branch in the review and preserve its existing history. A diverged remote/local
branch is a stop with an explanation and a plan; do not rebase, merge, or force
push automatically. Run configured normal commit hooks. If hooks alter reviewed
files, show the resulting diff and request renewed confirmation for that changed
content. A hook failure blocks push and launch and leaves the changes available.

Fetch Codespaces machine types for the target repository with GitHub's API.
Show CPU, RAM, and storage from the response, using returned identifiers rather
than a static SKU list. Recheck with the selected remote ref/configuration after
push. Filter choices against the generated project's real minimum requirements
and honor repository/organization restrictions. Missing permission or no eligible
machines permits commit-only setup and explains why launch is unavailable.
Use account defaults and the available middle-sized machine as suggestions;
do not present an unmeasured optimum or a guessed monetary price.

After successful commit and explicit launch approval, push the selected branch
and verify the remote head matches the local setup commit. Create from that
repository, branch, configuration path, machine, and idle timeout. The selected
size also needs a compatible generated `hostRequirements`; copying the existing
16 GB minimum must not silently exclude an intentionally chosen 8 GB machine.

Use `gh codespace create` and `gh codespace code --web` or `gh codespace code`
for the selected client. Show creation/provisioning progress, apply the selected
personalization, and report readiness separately from merely receiving a
codespace identifier. If desktop VS Code is unavailable, offer the browser URL.
If launch/personalization fails, preserve the successful commit/push, report the
codespace identifier if one exists, and print a retry command that reuses it
rather than allocating another machine. Never delete an existing codespace as
failure cleanup.

## Verification and acceptance

The implementation must demonstrate these behaviors before being called done:

1. A Mainstage scan recommends Node 24 from `web/package.json`, CI, and Docker;
   Python/uv remains baseline; no Ruby/Go selection comes from generic ignores.
2. Fixtures cover each supported language, nested mixed-language apps, conflicting
   versions, template ignores, vendor/examples, and ignored scratch/dependencies.
3. A first confirmed setup with `emsilva/dotfiles` prefills that repository on a
   different project's next run; XDG overrides and cancellation are covered.
4. Generated minimal selections do not contain unrelated polyglot features,
   lifecycle installs, validators, or cache mounts. A repeat run has no diff.
5. A pseudo-terminal smoke exercises Gum selection, editing answers, diff review,
   and cancellation. Nonterminal execution fails helpfully rather than hanging.
6. Temporary Git repositories prove exact commit contents, preservation of other
   edits, staged-change refusal, hook failures/rewrites, update conflicts, and
   remote divergence handling. No global Git configuration is changed.
7. Mocked GitHub responses prove available-machine selection, unavailable launch,
   push-before-create ordering, remote-head verification, and failure/resume
   without duplicate codespace creation.
8. Selected configurations build; local hooks and validation complete; Python/uv,
   agent tools when selected, dotfiles apply, and Mainstage's `task ci`, detached
   app commands, ports, and Docker-backed policy workflow are smoke-tested.
9. A real commit/push/create/personalize/open demonstration runs through the
   wizard's explicit confirmation before launch is declared verified. Automated
   tests use fixtures and mocks; this final demonstration creates live resources
   only with the displayed runtime approval.

Measure cold setup time and resulting disk use for the slim and polyglot
selections. Publish observations with environment details; no numerical reduction
is promised by this design.

## Scope limits

The first implementation delivers selective generation, terminal interaction,
preferences, validation, and the optional launch flow. Publishing per-preset
prebuilt images can follow measured usage. Do not build a plugin framework,
change global agent defaults, add application functionality to Mainstage, or
provision cloud infrastructure beyond an explicitly approved codespace.

## References

- [Current installer](https://github.com/emsilva/devcontainer/blob/ec987b5a09f97c86e7f4d55b410bcf82cced9bc9/install.sh)
- [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/)
- [Gum](https://github.com/charmbracelet/gum)
- [Git file inventory](https://git-scm.com/docs/git-ls-files)
- [Feature lockfile specification](https://github.com/devcontainers/spec/blob/main/docs/specs/devcontainer-lockfile.md)
- [Codespaces machine API](https://docs.github.com/en/rest/codespaces/machines)
- [Create a codespace](https://cli.github.com/manual/gh_codespace_create)
- [Open a codespace](https://cli.github.com/manual/gh_codespace_code)
