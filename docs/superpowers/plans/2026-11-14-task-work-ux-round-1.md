# task work and setup wizard: UX round 1 Implementation Plan

**Goal:** The owner sets up a project, connects, arms and replaces by reading `task work` and the setup wizard
alone, and the four failures of 2026-11-13 cannot recur.

**Architecture:** Six groups, each on its own branch and worktree. The wizard gains a read-only gate
(`default_holds.py`) and question-shaped prompts with an answer trail. `standing.py` gains a few read-only facts and
`overview.view` turns them into one title line plus labelled rows. The replace command asks before it writes its
journal. Nothing in this round can create, bill or delete a Codespace.

**Tech stack:** Python 3.11+ standard library, Gum 2.0.2 through `project_sync/terminal.py`, go-task, Git,
`unittest`.

**Spec:** `docs/superpowers/specs/2026-11-14-task-work-ux-round-1-design.md` (approved by the owner 2026-11-14).
Read it with this plan. Every `file:line` below is at `0c52227`.

**Execution:** through the herd. One Opus builder per group, at most three at once. Skip nothing in a group's
"Verify" list: the lead re-runs it.

## Global Constraints

Copied word for word from the spec. Review briefs copy them again from here.

### Anti-goals (a failure even if every test passes)

- Anything that lets a Codespace be created, rebuilt or deleted with fewer confirmations than today.
- A `task work` screen over 6 lines or 78 cells, or a menu without the command it will run printed once chosen.
- A test that passes on a short fixture name but cuts a real one.
- A refusal (A1) that fires on a checkout that is an ancestor of `origin/<default>`, or on a project with no
  `origin`.
- Losing a line from a project's own Taskfile.

### Constraints

- The owner's #87 rulings: at most 6 lines of 78 columns, a Gum menu of 3-6 actions, the likely one first;
  without a terminal, print the state and the `Next:` commands and exit 0. Menu items show labels only (B3, owner
  2026-11-14); the command prints once chosen. Ruling 5: only a connect turns recovery on.
- Never rebuild a paired Codespace; replacing one needs its own yes (C1 is that yes).
- Gates: `task check` and `task test` green on the branch, in the default environment.
- Copy follows the `ux-writing` skill. No AI attribution in commits or PRs.

### Rules this plan adds for every group

- **Do not touch `arming.consent_text` (`project_sync/arming.py:88-92`).** Its text is hashed into every armed
  project's consent (`_consent_on_file`, `project_sync/__main__.py:95-104`). One changed character makes every
  armed project ask for `arm` again.
- **Every menu action keeps its argv** (spec, Non-goals). Only labels, order and rows change.
- **Names in tests and drawings.** The paired Codespace is `bookish-space-meme-v66pqg6wpvvqcpw7j` (36
  characters), the second one `literate-pancake-jjjrq4j9rqwrc5qp6` (34), the project folder
  `consult-sm-innovate`, the GitHub account `mannu_kyndryl`, the billing owner `kyndryl-agentic-ai`. The spec calls
  `literate-pancake-…` the 36-character name; `python3 -c "print(len('literate-pancake-jjjrq4j9rqwrc5qp6'))"`
  prints 34. Both are run 3's names; the width tests use the longer one.
- **Widths** are measured with `devcontainer_setup.project_sync.overview.cells`, never `len`. The `task work`
  screen is bound to 78 cells by #87. Wizard and replace lines are drawn at 78 or less here with the names above,
  but they carry refs and names of any length and are not cut, except where a step says `fit`.
- **Gates.** Follow `/Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/stage-a/RULING-gates.md`,
  reading "the base commit" as your group's base (the commit your worktree was cut from). `task test` takes 23 to
  31 minutes on this Mac; `python3 -m unittest tests.test_wizard` alone took 6 min 54 s at `0c52227`. Run one
  module while you work and the full gates once before you report.
- **Evidence.** For each new or changed test, show it failing with its change removed (stash the product change,
  run the one test, restore). Put the output in your report, not in Git.

## Groups

| # | Group | Spec items | Product files | Base | Runs beside |
|---|---|---|---|---|---|
| 1 | Wizard gate and Taskfile | A1, A2 | `devcontainer_setup/default_holds.py` (new), `devcontainer_setup/__main__.py` (`integrate_taskfile`, `main`) | `0c52227` | 3, 5, 6 |
| 2 | Wizard prompts | A3, E | `devcontainer_setup/__main__.py` (every prompt, `interactive`) | group 1 merged | 4, 5, 6 |
| 3 | The `task work` screen | B1-B8, D3 (row), E | `project_sync/standing.py`, `overview.py`, `lifecycle.py`, `signins.py`, `replacing.py`, `cutover.py:207` | `0c52227` | 1, 5, 6 |
| 4 | Starting a replace | C1, C2, C3, E | `devcontainer_setup/replacement.py`, `project_sync/cutover.py:225-248` | group 3 merged | 2, 5, 6 |
| 5 | Connect, sign-in, Show details | D1, D2, D3 (line), B9, E | `project_sync/__main__.py`, `links.py`, `workspace.py` | `0c52227` | 1, 2, 3, 4, 6 |
| 6 | `ready.py` agrees with the wizard | A1 | dotfiles repo: `private_dot_claude/skills/codespace-ready/scripts/ready.py` | dotfiles `main` | all |
| R | Render file | Done means 2 | `tests/render_screens.py` (new) | all merged | none |

**Waves.** First 1, 3 and 5. Then 2 (needs 1), 4 (needs 3) and 6. Then R.

**Merge order** into `feature/task-work-ux-round-1`: 1, 3, 5, then 2, 4. Group 6 merges in the dotfiles repo.
Groups 1 and 2 share a file but no function; 2 starts after 1 because both rewrite parts of
`tests/test_wizard.py`. Groups 3 and 4 both edit `cutover.run`, so 4 starts after 3. Group 3 is the single owner
of `replacing.PHASE_WORDS`.

## Calls made by this plan

Each is marked **(Call)** where it is used.

1. **C1 split.** `inspect()` becomes `survey()` (reads only) and `begin()` (writes the journal, prints). The
   question sits between them.
2. **C1 from the wizard.** The wizard's `replace <paired>` choice asks the C1 question again. The Review screen
   reads no machine, billing owner or commit by design (`tests/test_wizard.py:120`), and the commit does not
   exist until the merge, so Review cannot be "its own yes". One question in one place also leaves no parameter
   that could skip it.
3. **D2 callers.** `_arm` has one product caller, `_armed` (`project_sync/__main__.py:159`), reached only from
   `task work:cs:armed` (`:402-404`). `_arm` keeps refusing a wrong word; only `_armed` catches that refusal and
   goes on. Ctrl-C and end of input still end the connect.
4. **A1 and `ready.py`.** One case table, byte-identical in both repos, run by both test suites, with one sha256
   pinned in both.
5. **B5 and B9 fields.** Listed in group 3 task 1 and group 5 task 4, with what prints when a field is missing.
6. **B3 ranking.** A row has a place (where it is drawn) and a keep rank (who gives way). Table in group 3.
7. **A1 placement.** In `main()`, after the terminal check and before `upgrade_handoff`, so nothing is fetched
   into the cache, no Gum is downloaded and no pull request is looked up first.
8. **Spec wording changed for a measured reason** (also listed in the designer's report):
   the create label has no comma and no machine tail; C1's `New one` row is split in two; C2's names move from
   the option to the title; the Checkout row cuts the branch, not the sentence.

## Review Focus

Inputs the spec implies and no drawing shows. Each has a test in the group named.

1. A folder plus Codespace name over 78 cells: line one cuts the folder, never the name (group 3,
   `test_line_one_cuts_the_folder_never_the_codespace`).
2. Two menu items that would read the same (two refused conversations whose ids share 8 characters): labels stay
   unique, or the menu would run the wrong one (group 3, `test_menu_labels_are_unique`).
3. A1 while GitHub does not answer, or before `origin/<default>` was ever fetched: the last fetch is used, and
   with no ref at all the gate passes and today's flow decides (group 1, `test_gate_needs_positive_evidence`).
4. Ctrl-C while the replace is still checking, before the question: no journal, `Nothing was replaced.`, exit 0
   (group 4, `test_cancel_before_the_question_writes_nothing`).
5. A wrong word at `arm` while an older consent is on file and recovery is running: nothing is stopped or started
   (group 5, `test_wrong_word_leaves_a_running_recovery_alone`).

---

## Group 1: Wizard gate and Taskfile (A1, A2)

**Files:**
- Create: `devcontainer_setup/default_holds.py`
- Create: `tests/test_default_holds.py`, `tests/fixtures/default-holds-cases.json`
- Modify: `devcontainer_setup/__main__.py:80-103` (`integrate_taskfile`), `:1327-1335` (`main`)
- Modify: `tests/test_wizard.py:1009` (`test_fresh_taskfile_has_working_lifecycle_aliases`), `:2067`
  (`test_owner_branch_config_neither_blocks_nor_shapes_a_github_run`)

**Interfaces:**
- Produces: `default_holds.measure(project: Path) -> Holding | None`, `default_holds.refusal(project: Path) ->
  str | None`, `Holding(default: str, branch: str | None, held: int, tracked: int)` with property
  `refused: bool`.
- Consumes: `git_ops._fetch_default`, `git_ops.default_branch`, `git_ops.git`, `git_ops.is_ancestor`
  (`devcontainer_setup/git_ops.py:74`, `:41`, `:21`, `:215`).

### Task 1.1: the gate

The rule, in order:

1. `default = default_branch(project)`. Call `_fetch_default(project, default)` and ignore its result: when
   GitHub does not answer, the last fetch serves.
2. `tip` is `refs/remotes/origin/<default>^{commit}`, `head` is `HEAD^{commit}`. If either does not resolve,
   return `None` (nothing to compare; today's flow decides).
3. If `head` is an ancestor of `tip`, return `None`: the Codespace gets everything this checkout has.
4. `here` and `there` are the paths of `git ls-tree -r --name-only -z` at `head` and `tip`, each without paths
   that start with `.devcontainer/`. `tracked = len(here)`, `held = len(here & there)`.
5. Refuse when `tracked` is not zero and `held * 2 < tracked`.

`measure` returns `Holding` after step 4 and `None` from steps 2 and 3. `refusal` returns the three lines or
`None`. It is called only when `_github_repository(project)` is true, so a project with no GitHub `origin` never
reaches it.

Screens. On a branch (and `on this detached checkout` when `branch` is `None`):

```screen
GitHub's main holds 3 of the 212 files tracked on docs/design-20261023.
A Codespace starts from main, so it would hold almost none of this project.
Merge the project into main first, then run the setup from a checkout on main.
```

On the default branch itself (this Mac's `main` holds the project, GitHub's does not):

```screen
GitHub's main holds 2 of the 12 files tracked on this Mac's main.
A Codespace starts from GitHub, so it would hold almost none of this project.
Push this Mac's main to GitHub, or merge it there, first.
```

`main` is the default branch's real name in every line.

- [ ] **Step 1: add the shared case table.** Create `tests/fixtures/default-holds-cases.json` with exactly these
  bytes (one trailing newline). Its sha256 is
  `723a5b9988f497d883225fd1ff8cab3e13521e1fc232a1347c16b1c0c5404293`
  (`shasum -a 256`). Group 6 adds the same bytes to the dotfiles repo.

```json
{
  "shared_with": "Byte-identical in emsilva/devcontainer and in the dotfiles repo, at tests/fixtures/default-holds-cases.json. Change both copies together.",
  "notation": "A path ending in *N stands for N paths: the text before * followed by 0 to N-1.",
  "cases": [
    {"name": "project on an unrelated branch, template on main",
     "main_commits": [["README.md", ".pre-commit-config.yaml", "Taskfile.yml", ".devcontainer/devcontainer.json"]],
     "local": {"start": "orphan", "branch": "develop", "add": ["src/module_*10"]},
     "expect": "refuse", "held": 0, "tracked": 10},
    {"name": "this Mac's main holds the project, GitHub's main only the template",
     "main_commits": [["README.md", ".pre-commit-config.yaml"]],
     "local": {"start": "tip", "branch": "main", "add": ["src/module_*10"]},
     "expect": "refuse", "held": 2, "tracked": 12},
    {"name": "branch that merged the template in, main still lacks the project",
     "main_commits": [["README.md", ".pre-commit-config.yaml"]],
     "local": {"start": "tip", "branch": "feature/project", "add": ["src/module_*10"]},
     "expect": "refuse", "held": 2, "tracked": 12},
    {"name": "setup files on both sides do not count as the project",
     "main_commits": [["README.md", ".devcontainer/file_*60"]],
     "local": {"start": "orphan", "branch": "develop", "add": [".devcontainer/file_*60", "src/module_*10"]},
     "expect": "refuse", "held": 0, "tracked": 10},
    {"name": "one file under half",
     "main_commits": [["src/module_*4"]],
     "local": {"start": "tip", "branch": "feature/more", "add": ["lib/part_*5"]},
     "expect": "refuse", "held": 4, "tracked": 9},
    {"name": "exactly half is enough",
     "main_commits": [["src/module_*5"]],
     "local": {"start": "tip", "branch": "feature/more", "add": ["lib/part_*5"]},
     "expect": "pass", "held": 5, "tracked": 10},
    {"name": "feature branch with a small addition",
     "main_commits": [["src/module_*10"]],
     "local": {"start": "tip", "branch": "feature/small", "add": ["docs/note_*2"]},
     "expect": "pass", "held": 10, "tracked": 12},
    {"name": "main, only behind, after GitHub's main renamed every file",
     "main_commits": [["old/module_*10"], ["new/module_*10"]],
     "local": {"start": "first", "branch": "main", "add": []},
     "expect": "pass", "held": null, "tracked": null},
    {"name": "up to date on main",
     "main_commits": [["src/module_*10"]],
     "local": {"start": "tip", "branch": "main", "add": []},
     "expect": "pass", "held": null, "tracked": null}
  ]
}
```

  How a case becomes repositories: a bare repository stands in for GitHub, reached through
  `url.<bare>.insteadOf https://github.com/acme/project.git` as `ready.py`'s tests do
  (`~/.local/share/chezmoi/tests/test_codespace_ready.py:449-456`). Each list in `main_commits` is one commit
  on `main` whose tree is exactly those paths; all are pushed, then `git fetch` and `git remote set-head origin
  main`. `local.start` is `tip` (stay on the pushed tip; create `branch` when it is not `main`), `first` (reset
  local `main` to the first commit, so it is only behind) or `orphan` (`git checkout --orphan <branch>`, empty
  tree). Paths in `local.add` are then committed on top. `held` and `tracked` are `null` where the ancestor rule
  returns before counting.

- [ ] **Step 2: write the failing tests** in `tests/test_default_holds.py`.

| Test | Asserts |
|---|---|
| `test_shared_cases` | For each case (one `subTest` each): `refusal()` is `None` exactly when `expect` is `pass`; when `held` is not `null`, `measure()` returns those two numbers; when it is `null`, `measure()` is `None`. |
| `test_cases_file_is_the_shared_copy` | sha256 of the fixture file equals the constant above. The failure message says the dotfiles copy must change too. |
| `test_refusal_names_the_fix_for_a_branch` | Case 1's text equals the first screen with `0 of the 10`, `develop`. |
| `test_refusal_names_the_fix_for_main` | Case 2's text equals the second screen. |
| `test_gate_needs_positive_evidence` | (a) origin URL pointed at a missing path after one fetch: verdict unchanged from the last fetch. (b) a repository whose `origin/main` was never fetched and cannot be: `None`. (c) no commit yet: `None`. |
| `test_refused_setup_asks_writes_and_pushes_nothing` | Through `main(['--project', root])` on a terminal, in `tests/test_wizard.py`'s `SetupFromDefaultBranchTests` fixture after `checkout_feature()`: exit 1; stderr is the three lines and nothing else; `wizard.Gum` (patched to raise) was never built; the fake `gh` recorded no call; `assert_nothing_left()`; the state directory was not created; `upgrade_handoff` (patched) was not called. |
| `test_no_github_origin_is_not_gated` | With `origin` removed, and with a non-GitHub `origin`: `main()` reaches `interactive` (patched) and `default_holds.refusal` (patched) is not called. |
| `test_read_only_modes_are_not_gated` | `--dry-run`, `--scan`, `--check` and `--replace` on case 1's checkout never call `refusal`. |

  Import the fixture class from `tests.test_wizard` for the two `main()` tests; do not copy it.

- [ ] **Step 3: run them.** `python3 -m unittest tests.test_default_holds -v`. Expected: errors, because
  `devcontainer_setup.default_holds` does not exist.

- [ ] **Step 4: implement `default_holds.py`** as specified above, and call it from `main()`. Insert between
  the terminal check (`:1327-1328`) and `if args.upgrade and not args.upgrade_loaded:` (`:1329`):

```python
        if github:
            from .default_holds import refusal
            refused = refusal(project)
            if refused:
                print(refused, file=sys.stderr)
                return 1
```

  **(Call 7.)** `github` is already set at `:1308`. The install one-liner reaches this point: `install.sh`
  ends in `exec bash …/scripts/setup.sh --project …` (`install.sh:13`, `:48`, `:50`), and `scripts/setup.sh:9`
  runs the `devcontainer_setup` module, so its `main()`. On that path the installer has already downloaded
  itself to the user's cache; "written nothing" means nothing in the project, its Git state or GitHub.

- [ ] **Step 5: repair the one existing test the gate now refuses.**
  `test_owner_branch_config_neither_blocks_nor_shapes_a_github_run` (`tests/test_wizard.py:2067`) runs `main()`
  from `checkout_feature()`, where `main` holds 1 of the branch's 3 files. Measured with a prototype of this
  gate: it fails with `1 != 130 : GitHub's main holds 1 of the 3 files tracked on feature/local.`; the other two
  tests that pass through `main()` (`:2338`, `:2345`) still pass. In that test only, call
  `self.push_to_main({'src/a.txt': 'a\n', 'src/b.txt': 'b\n'})` and `self.git('pull', '--quiet', '--ff-only')`
  before `checkout_feature()`, so `main` holds 3 of 5. Do not change `checkout_feature()`.

- [ ] **Step 6: run** `python3 -m unittest tests.test_default_holds tests.test_wizard -v`. Expected: OK.

- [ ] **Step 7: commit.** `Refuse the setup when GitHub's default branch lacks the project`

### Task 1.2: no `up`, `down`, `status` or `build` (A2)

- [ ] **Step 1: replace the test** at `tests/test_wizard.py:1009` with
  `test_fresh_taskfile_claims_no_project_task_names`: after `integrate_taskfile(self.root, files)` then
  `integrate_continuity(self.root, files, True)`, write the files and the template include as the old test does
  and assert (a) `task --list-all --json` exits 0, (b) none of `up`, `down`, `status`, `build` is in the listed
  names, (c) `work` and `devcontainer:up` are, (d) `files['Taskfile.yml']` starts with `b"version: '3'\ntasks:\n"`.
  Add `test_existing_taskfile_keeps_every_line`: a project Taskfile that already has the four aliases plus a
  task of its own comes back with each of its original lines still present, in order.

- [ ] **Step 2: run them.** Expected: the first fails on (b).

- [ ] **Step 3: change `integrate_taskfile`.** `:91-93` becomes:

```python
    text = path.read_text() if existing else "version: '3'\ntasks:\n"
```

  Measured through the wizard's own `--dry-run` on a project with no Taskfile: exit 0, and `task --list-all`
  accepts the empty `tasks:` with continuity on and off. `tasks: {}` is refused by `taskfile_mapping.mapping`
  (`Expand the inline Taskfile tasks mapping before setup`), so keep the bare key. No generated text tells the
  owner to run one of the four names: `git grep -n -E "task (up|down|status|build)\b" -- . ':!docs/superpowers'
  ':!tests'` prints nothing at `0c52227`.

- [ ] **Step 4: run** `python3 -m unittest tests.test_wizard tests.test_root_tasks tests.test_continuity_integration -v`.
  Expected: OK.

- [ ] **Step 5: commit.** `Stop writing up, down, status and build into a new Taskfile`

**Verify (reviewer):** run `tests.test_default_holds`; then, in a scratch repository built from case 2, run
`python3 -m devcontainer_setup --project <repo>` in a terminal and read the three lines; confirm `git -C <repo>
worktree list` shows one entry and no `setup/*` branch exists.

---

## Group 2: Wizard prompts (A3, E)

**Files:**
- Modify: `devcontainer_setup/__main__.py`: `_answer_findings` (`:544-583`), `_dotfiles_answer` (`:586-597`),
  `_select` (`:600-664`), `CREATE_CODESPACE` (`:667`), `_publish_choice` (`:682-688`), `_ask_launch_resources`
  (`:770-784`), `_LaunchEditor` (`:787-810`), `_codespace_choice` (`:813-892`), `_launch_options`
  (`:895-907`), `interactive` (`:1112`, `:1179`, `:1218-1226`)
- Modify: `tests/test_wizard.py` (every prompt title used as a key or asserted)

**Interfaces:**
- Produces (module constants in `__main__.py`): `PUBLISH_Q`, `CODESPACE_NEW_Q`, `CODESPACE_WHICH_Q`,
  `MACHINE_Q`, `IDLE_Q`, `EDITOR_Q`, `REVIEW_Q`, `CREATE_NEW = 'create a new Codespace'`,
  `create_label(history: dict) -> str`, class `_Trail`.
- Consumes: nothing from other groups.

### Task 2.1: every title is a question

| Line | Today | Round 1 |
|---|---|---|
| `:565`, `:621` | `{name} version (resolve declarations above)` | `Which {name} version? (the declarations above disagree)`; at `:621` with no conflict: `Which {name} version?` |
| `:569`, `:638` | `{family} package manager` | `Which {family} package manager?` |
| `:578`, `:643` | `{manager} version (resolve declarations above; exact x.y.z)` | `Which {manager} version? (exact x.y.z; the declarations above disagree)` |
| `:589` | `Dotfiles repository` | `Which dotfiles repository?` |
| `:592` | `Could not verify access. Continue with this repository?` | `Could not verify access. Use this repository anyway?` |
| `:612` | `Project runtimes (Python, Node and uv included)` | `Which other runtimes? (Python, Node and uv are included)` |
| `:626` | `Project tools` | `Which tools does the project need?` |
| `:655` | `Advanced environment options?` | `Change advanced environment options?` |
| `:657` | `{name} version` | `Which {name} version?` |
| `:658` | `Forwarded ports (comma-separated)` | `Which ports should be forwarded? (comma-separated)` |
| `:687` | `Publish` | `PUBLISH_Q = 'How should the setup reach GitHub?'` |
| `:781`, `:884` | `Codespaces machine` | `MACHINE_Q = 'Which machine size?'` |
| `:783`, `:885` | `Stop when idle for` | `IDLE_Q = 'Stop it after how long idle?'` |
| `:818`, `:854`, `:900` | `Codespace` | `CODESPACE_NEW_Q = 'Create a Codespace now?'` when the only options are `none` and a create; else `CODESPACE_WHICH_Q = 'Which Codespace?'` (`:818` and `:900` always use the second) |
| `:888` | `Region` | `Which region?` |
| `:891` | `Delete stopped codespace after` | `Delete it after how long stopped?` |
| `:1179` | `Editor after create` | `EDITOR_Q = 'Open an editor once it is created?'` |
| `:1226` | `Review setup` | `REVIEW_Q = 'Review: go ahead with these answers?'` |

Unchanged, already questions: `:551`, `:555`, `:559`, `:628`, `:650`, `:663`, `:718`, `:887`, `:943`, `:946`,
`:1075`, `:1249`. Option labels do not change, `none` included: the roll-out recipe names it.

`_LaunchEditor.choose` picks its history defaults by comparing the title with `'Codespaces machine'` (`:797`)
and `'Stop when idle for'` (`:806`). Compare with `MACHINE_Q` and `IDLE_Q`, or the defaults stop silently.

**The create option.** `CREATE_CODESPACE = 'create like last time'` (`:667`) goes. In `_codespace_choice`, read
`history = _launch_history(project, branch)` before building `choices` and use `create_label(history)`:
`create one like <codespace>` when the record has `codespace` and `machine` and no `warning`, else
`CREATE_NEW`. **(Call 8.)** The spec's `(<machine>, idle <timeout>)` tail is left out: `Gum.choose` refuses to
preselect a label with a comma (`project_sync/terminal.py:89-91`), and this label is the preselected one
(`:852-854`); with a semicolon it is 83 cells with the names above. The machine and idle timeout it copies show
preselected in the next two questions, as today.

- [ ] **Step 1: write the failing tests** in `tests/test_wizard.py`.

| Test | Asserts |
|---|---|
| `test_every_wizard_prompt_is_a_question` | In `ReviewRerunTests`, a first setup with `Change advanced environment options?` answered yes, and a `SetupFromDefaultBranchTests` launch run: every recorded title contains `?`. Fails today on `Publish`. |
| `test_first_setup_offers_a_new_codespace` | No launch receipt, no Codespaces: the title is `Create a Codespace now?`, the options are `['none', 'create a new Codespace']`, and no option contains `last time` or `like`. |
| `test_create_names_the_codespace_it_copies` | With the receipt of `:373-375` (`codespace` set to the 36-character name): the option is `create one like bookish-space-meme-v66pqg6wpvvqcpw7j`, it has no comma, and `terminal.Gum('gum').choose(...)` builds its argv with it as `--selected` without raising (patch `subprocess.run`). |
| `test_several_codespaces_ask_which` | With one listed Codespace the title is `Which Codespace?`. |

  Then rename the titles in every existing test that uses one as a key or asserts it (`grep -n "'Publish'\|'Codespace'\|'Codespaces machine'\|'Stop when idle for'\|'Editor after create'\|'Review setup'\|'Project tools'\|'Project runtimes\|create like last time" tests/test_wizard.py`).
  `test_create_like_last_time_uses_history_and_asks_for_missing_machine` (`:365`) keeps its assertions under
  the new titles: it is the test that the history defaults survive the rename.

- [ ] **Step 2: run** `python3 -m unittest tests.test_wizard -v`. Expected: the four new tests fail.
- [ ] **Step 3: implement** the table and `create_label`.
- [ ] **Step 4: run** the module again. Expected: OK.
- [ ] **Step 5: commit.** `Ask every setup question as a question`

### Task 2.2: the answers stay on screen

`_Trail` wraps the Gum adapter for the wizard only. `terminal.Gum` itself is shared with `task work`'s menu
(`overview.py:406`), the replace (`replacement.py:69-72`) and the connect (`workspace.py:314`), which print
their own outcome; do not change it.

```python
class _Trail:
    """Gum clears a prompt once answered; leave one line with the question and the answer."""
    def __init__(self, ui): ...
    def choose(self, title, options, selected=None) -> str: ...
    def multi(self, title, options, selected) -> list[str]: ...
    def input(self, title, value='') -> str: ...
    def confirm(self, title, default=True) -> bool: ...
    def __getattr__(self, name): ...   # anything else goes to the wrapped adapter
```

Each method calls the wrapped one, prints `f'{title}  {answer}'` (two spaces) with `flush=True` and returns the
answer unchanged. `answer` is the chosen label; `yes` or `no`; the typed text, or `(empty)`; the chosen items
joined with `, `, or `none`. A cancelled prompt prints nothing. In `interactive`, `:1112` becomes
`ui, api = _Trail(Gum()), Codespaces()`.

```screen
Which other runtimes? (Python, Node and uv are included)  go
Which tools does the project need?  none
Sync scratch files and project notes with your Mac?  yes
How should the setup reach GitHub?  merge into main
Create a Codespace now?  create a new Codespace
Which machine size?  4 cores / 16 GB RAM / 32 GB disk (standardLinux32gb)
Stop it after how long idle?  30m
Which Codespace?  create one like bookish-space-meme-v66pqg6wpvvqcpw7j
Review: go ahead with these answers?  Continue
```

**The replace choice.** When `launch['action'] == 'replace'`, print one more line after the `Codespace:` line
(`:1221`). **(Call 2.)**

```screen
The replace asks once more, with its details, before anything is created.
```

- [ ] **Step 1: write the failing tests.**

| Test | Asserts |
|---|---|
| `test_answers_stay_on_screen` | In the first-setup run of `:352`: the output holds one line `<title>  <answer>` per recorded prompt, in order, for a choose, a confirm (`yes`/`no`), a multi (`none`) and an input. |
| `test_a_cancelled_prompt_leaves_no_line` | Ctrl-C at Review: no line starts with `Review: go ahead`. |
| `test_review_says_the_replace_asks_again` | In `test_replace_review_reads_no_resources`'s run (`:120`): the output contains the line above; with any other action it does not. |
| `test_trail_wraps_the_wizard_only` | `terminal.Gum.choose` prints nothing itself (patch `subprocess.run`, capture stdout). |

- [ ] **Step 2: run them.** Expected: fail (no trail lines).
- [ ] **Step 3: implement `_Trail`** and the Review line.
- [ ] **Step 4: run** `python3 -m unittest tests.test_wizard -v`. Expected: OK.
- [ ] **Step 5: commit.** `Leave each setup answer on screen`

**Verify (reviewer):** `python3 -m unittest tests.test_wizard`; then read the wizard section of the render file
(group R) for a first setup: each title a question, each followed later by its trail line, no `last time`.

---

## Group 3: The `task work` screen (B1-B8, D3 row, E)

**Files:**
- Modify: `devcontainer_setup/project_sync/standing.py`: `Local` (`:41-73`), `_setup` (`:260-283`), `_cycle`
  (`:304-312`), `_state_facts` (`:395-459`)
- Modify: `devcontainer_setup/project_sync/lifecycle.py` (new function after `restart_rids`, `:236-242`)
- Modify: `devcontainer_setup/project_sync/overview.py`: `Action.item` (`:57-59`), `view` (`:150-361`), `run`
  (`:364-418`)
- Modify: `devcontainer_setup/project_sync/replacing.py:22-31` (`PHASE_WORDS`),
  `devcontainer_setup/project_sync/cutover.py:207`
- Modify: `devcontainer_setup/project_sync/signins.py` (new `missing`; `_summary`, `:260-271`)
- Modify: `tests/test_standing.py`, `tests/test_lifecycle.py`, `tests/test_signins.py`,
  `tests/test_overview.py`, `tests/test_work_overview.py`, `tests/test_cutover.py:942`,
  `tests/test_replacement.py` (phase words only)

**Interfaces:**
- Produces: the `Local` fields below; `lifecycle.budget_free_at(rows: list[dict], now: float) -> float | None`;
  `signins.missing(tools: dict) -> tuple[str, ...]`; new `replacing.PHASE_WORDS` values.
- Consumes: nothing from other groups.

### Task 3.1: the facts (`standing.py`, `lifecycle.py`)

New `Local` fields, all with these defaults, added before `problems`:

| Field | Type, default | Read from | When it stays at its default |
|---|---|---|---|
| `keeper_phase` | `str \| None = None` | `keeper-runtime.json` `phase` | record is for another Codespace; recovery is not `running` |
| `next_stop_at` | `float \| None = None` | `keeper-runtime.json` `anchor.at` plus `lifecycle.LIFETIME` | recovery not `running`; `reading.ok` is not `True`; `reading.state` is not `Available`; `anchor.at` is not a number |
| `hold_at_stop` | `float \| None = None` | `keeper-runtime.json` `down.hold_at_stop` | recovery not `running`; not a number |
| `restarts_free_at` | `float \| None = None` | `lifecycle.budget_free_at(lifecycle.Ledger(state / keeper.LEDGER).rows(), time.time())` | not armed for this pairing; the budget has room; the ledger is missing or raises `OSError` (no problem is recorded) |
| `signed_out` | `tuple[str, ...] = ()` | `signins.json`: `signins.missing(record['tools'])` | no file; `record['codespace']` is not the pairing; `tools` is not an object |
| `history_pending_since` | `float \| None = None` | `scratch-runtime.json` `conversations.started_at` | sync not `running` for this binding; the cycle is not this project's; `completed_at` is set; `outcome` is not `pending` |
| `logs` | `bool = False` | `keeper.log` or `scratch.log` exists in the state directory | neither exists |

`next_stop_at` uses the condition `task work:status` already uses for `12-hour stop expected`
(`workspace.py:664-670`). An unreadable `signins.json` goes through `reader.json(..., private=True)` and so adds
the problem `signins.json cannot be read`.

`budget_free_at`: among intent rows that `_budgeted` (`lifecycle.py:232-233`) accepts and that are within
`RESTART_PERIOD` of `now`, take each rid's latest time. With fewer than `MAX_RESTARTS` rids return `None`. Else
return `sorted(times)[len(times) - MAX_RESTARTS] + RESTART_PERIOD`: the moment `gate` (`:245-258`) next has
room.

`signins.missing(tools: dict, *, unlisted: bool) -> tuple[str, ...]`: the labels of `signins.TOOLS`
(`signins.py:30-38`), in that order, whose row state is not `signed-in`, `other-method` or `absent`. A tool with
no row counts only when `unlisted` is true. `_summary` (`signins.py:260-271`) uses it with `unlisted=True` for
its `missing` list: today it counts a tool with no result as not signed in (`:263`), and that must not change.
The screen uses `unlisted=False`: it never claims a tool the record does not name. With one rule the screen and
the summary cannot disagree on a tool both know. `standing` imports
`signins` inside the function that reads the record, as `_retirement_facts` does for `seal` (`:358`): importing
it at module level costs about 90 ms (`python3 -X importtime`).

`_cycle` gains `completed: bool = True`; with `False` it skips the `completed_at` test, so the pending cycle can
be read with the same project and binding checks. `standing` must not call `scratch.inspect_sync`: it starts
Mutagen (`scratch.py:297-308`).

**B1.** `_setup` (`:280-283`) returns `('installed', ref)` whenever the managed metadata is valid, with or
without a state directory and its files. The value `partial` no longer exists.

- [ ] **Step 1: write the failing tests.**

| Test (file) | Asserts |
|---|---|
| `test_setup_states` (`tests/test_standing.py:212`, changed) | With valid metadata and no `workspace.json` or `scratch.json`: `installed`. The string `partial` is never returned. |
| `test_a_set_up_project_without_a_pairing_is_unpaired` (`test_standing.py`) | `standing.read` then `overview.view`: line one ends `· no Codespace paired`, no line contains `did not finish`, and with one Codespace listed the first action is `connect`. |
| `test_recovery_facts_come_from_the_recovery_record` | A record with `anchor.at = T`, an `Available` reading, `phase = 'restarting'`, `down.hold_at_stop = H` and a live process gives `next_stop_at == T + 43200`, `keeper_phase == 'restarting'`, `hold_at_stop == H`. Each "stays at its default" cell of the table gives `None` (one `subTest` each). |
| `test_restart_budget_is_read_from_the_ledger` | Two keeper rids at `now - 7200` and `now - 3600`: `restarts_free_at == now - 7200 + 86400`. One rid: `None`. An owner `o-` rid does not count. No ledger: `None` and no problem. |
| `test_budget_free_at_names_when_the_oldest_restart_leaves` (`tests/test_lifecycle.py`) | The same three cases on the pure function, plus: `gate(...)` returns the budget refusal at `free_at - 1` and not at `free_at + 1`. |
| `test_signed_out_tools_come_from_the_paired_record` | `plaud` signed-out, `codex` signed-in, `claude` unknown, `grok` absent: `('Plaud', 'Claude')`. A record for another Codespace, and no file: `()`. A file that is not JSON: `()` and the problem `signins.json cannot be read`. |
| `test_summary_and_screen_share_the_signed_out_rule` (`tests/test_signins.py`) | `_summary` prints `Not signed in: ` plus `', '.join(missing(tools, unlisted=True))` for the same record. |
| `test_summary_still_counts_a_tool_with_no_result` (`tests/test_signins.py`) | `_summary({})` (a cancel before the probe) still prints `Not signed in: Plaud, Codex, Claude, OpenCode, Grok. Retry with task work:signin:all.`; `missing({}, unlisted=False)` is `()`. |
| `test_pending_history_is_read_from_the_sync_record` | A pending cycle started at `T` for this binding with a live sync: `T`. Completed, another project's, or sync stopped: `None`. |
| `test_logs_fact_needs_a_log_file` | `False` in a fresh state directory, `True` once `scratch.log` exists. |

  `test_reading_creates_and_changes_nothing` (`:198`) and `test_subprocesses_are_bounded_and_read_only` (`:477`)
  must still pass with all the new files present: add `signins.json` and `keeper-ledger.jsonl` to their fixtures.

- [ ] **Step 2: run** `python3 -m unittest tests.test_standing tests.test_lifecycle tests.test_signins -v`.
  Expected: the new tests fail (`Local` has no such field; `partial` is returned).
- [ ] **Step 3: implement.**
- [ ] **Step 4: run** the three modules. Expected: OK. `tests.test_overview` still fails on `partial`
  (`:242`, `:332`, `:342`, `:555`) until task 3.2; do not run the full gates yet.
- [ ] **Step 5: commit.** `Read recovery, sign-in, history and log facts for task work`

### Task 3.2: the screen and the menu (`overview.py`)

**Line one** is `<folder> · <Codespace>`, or `<folder> · no Codespace paired` without a pairing. `<folder>` is
`local.root.name`. When the line would pass 78 cells, cut the folder with
`fit(folder, 78 - cells(' · ' + name))`, then pass the whole line through `fit`. Inside a Codespace the frame
stays today's single line (`:163-165`).

**Rows** are `f'{label:<9}  {text}'`, passed through `fit`. **(Call 6.)** Each row has a *place* (rows are drawn
in place order) and a *keep rank*. When more than five rows apply, the five with the lowest keep rank stay; line
one always stays.

| Place | Keep | Label | Text | When |
|---|---|---|---|---|
| 1 | 1 | `Recovery` | `off until you connect` | armed, recovery `stopped`, and the pairing is not deleted, missing or elsewhere (today's `recovery_off`, `:184-185`) |
| 2 | 2 | `Replace` | `stopped at {PHASE_WORDS[phase]}; finish it` | `local.replacing` |
| 3 | 3 | `Cleanup` | `not finished for {target}` | `local.cleanup` |
| 4 | 4 | `Problem` | `{first problem}; Show details has more`; with more than one: `{first problem} (+{n} more); Show details has more` | `local.problems`, or an action withheld for unsafe text (`an action was withheld for unsafe text`) |
| 5 | 5 | `Setup` or `Codespace` | see "The situation row" | always, except unpaired with no answer from GitHub |
| 6 | 6 or 12 | `Recovery` | see "The Recovery row" | paired, and place 1 does not apply |
| 7 | 7 | `Sync` | `stopped; connect to start it` | paired and `local.worker == 'stopped'` |
| 7 | 14 | `Sync` | `last completed {time}` | `local.cycle_at`, and the row above does not apply |
| 8 | 8 | `History` | `{n} conversation(s) refused; forget it/them to clear the sync warning` | refusals not forgotten |
| 8 | 8 | `History` | `not synced yet, {minutes} min so far` | no refusal row, and `now - history_pending_since > 300` |
| 9 | 9 | `Sign-ins` | `Plaud is not signed in`; `Plaud and Codex are not signed in`; `Plaud, Codex and Claude are not signed in` | `local.signed_out` |
| 10 | 10 | `Replaced` | `still on GitHub; check and delete it when ready` | the retired-Codespace delete is offered (`:336-340`) |
| 11 | 11 | `Checkout` | `on {branch}; Codespaces start from {default}` | relation `other-branch`; `detached` reads `on a detached HEAD; …` |
| 11 | 11 | `Checkout` | `{n} commits behind origin/{default} (local copy from {time})` | as today (`:300-302`) |
| 11 | 11 | `Checkout` | `diverged from origin/{default}; review before merging` | as today (`:305-306`) |
| 12 | 13 | `Generator` | `upstream is at {sha7}; this project uses {sha7}` | as today (`:307-310`) |

In the other-branch row the branch alone is cut, so the sentence survives: **(Call 8)**
`fit(branch, 78 - cells('Checkout   on ; Codespaces start from ' + default))`.

In the Problem row the problem text alone is cut, so the pointer survives:
`fit(problem, 78 - cells('Problem    ' + tail))`, where `tail` is `; Show details has more` or
` (+{n} more); Show details has more`. `standing.py` emits problems far longer than the 44 cells that leaves:
`retired/bookish-space-meme-v66pqg6wpvvqcpw7j.json cannot be read` (`:365`) makes a 98-cell row uncut.

**The situation row** (place 5). The Codespace is named on line one only.

| Situation (`:171-182`) | Row |
|---|---|
| `setup` (`missing`, `unmanaged`, `unknown`) | `Setup      not done yet` |
| one setup pull request | `Setup      pull request #7 is open; resume setup to finish it` |
| several | `Setup      2 pull requests are open; close all but one in GitHub` |
| `failed` | `Codespace  failed on GitHub; see its creation logs` |
| `launch`, target is the pairing | `Codespace  setup did not finish validation; resume it` |
| `launch`, not paired | `Setup      {target} did not finish validation` |
| `deleted` / `keeper-deleted` | `Codespace  deleted (confirmed 09:10)` / `Codespace  deleted (found by Recovery 09:10)` |
| `missing` | `Codespace  not listed for {account}; deleted, or under another account` |
| `elsewhere` | `Codespace  now listed under another repository; pair this one's Codespace` |
| `unpaired`, GitHub answered | `Codespace  GitHub lists none for this repository` / `lists 1` / `lists {n}` |
| `stopped` | `stopped; you stopped it at 07:55` / `stopped by GitHub's 12-hour limit at 07:55` / `stopped at 07:55, not by you` / `stopped (checked 09:10)` |
| `available`, `starting`, `stopping` | `Codespace  available (checked 09:10)`; a cached reading says `(as of 09:10)` |
| `other` | `Codespace  in state Moved (checked 09:10)` |
| `unknown` | `Codespace  checking GitHub…` / `Codespace  GitHub did not answer within 2 s` / `Codespace  {reason}` |

**The Recovery row** (place 6). **(Call 5.)** First match wins. "Stopped" means the situation is `stopped`; the
stop kind is `_stop(local, pair)[0]` (`:137-147`).

| # | Condition | Text | Keep |
|---|---|---|---|
| 1 | `armed is None` | no row (the Problem row carries `arming.json cannot be read`) | |
| 2 | `armed is False` | `not armed` | 12 |
| 3 | recovery `stale` | `not answering for {n} min; connect to restart it`; without an age: `not answering; connect to restart it` | 6 |
| 4 | recovery `unknown` | `state unknown (process check timed out)` | 6 |
| 5 | recovery `stopped`, pairing deleted, missing or elsewhere | no row | |
| 6 | `hold_until > now` | `armed; on hold until {time}` | 6 if stopped, else 12 |
| 7 | stopped, `keeper_phase == 'restarting'` | `armed; restarting it now` | 6 |
| 8 | stopped, kind `OWNER` | `armed; not restarting: you stopped it` | 12 |
| 9 | stopped, `hold_at_stop` set | `armed; not restarting: it stopped during your hold` | 6 |
| 10 | stopped, `restarts_free_at > now` | `armed; out of restarts until {time}` | 6 |
| 11 | stopped, kind `OTHER` | `armed; not restarting: this was not the 12-hour stop` | 6 |
| 12 | stopped, kind `LIFETIME` | `armed; not restarted yet; Show details has the reason` | 6 |
| 13 | stopped, no kind yet | `armed; checking why it stopped` | 6 |
| 14 | not stopped, `next_stop_at > now` | `armed; next 12-hour stop about {HH:MM}` | 12 |
| 15 | not stopped, `next_stop_at <= now` | `armed; the 12-hour stop was due about {HH:MM}` | 12 |
| 16 | not stopped, `next_stop_at is None` | `armed; next 12-hour stop not known yet` | 12 |

`{time}` is `_when` (`:94-96`): `HH:MM` today, else the date too. Rows 9 to 16 are what a missing field prints:
no phase, no hold, no ledger and no stop kind end at 13; no anchor ends at 16.

```screen
consult-sm-innovate · bookish-space-meme-v66pqg6wpvvqcpw7j
Codespace  available (checked 08:30)
Recovery   armed; next 12-hour stop about 17:45
Sync       last completed 08:29
```

```screen
consult-sm-innovate · bookish-space-meme-v66pqg6wpvvqcpw7j
Codespace  stopped by GitHub's 12-hour limit at 07:55
Recovery   armed; restarting it now
Sync       last completed 07:54
Sign-ins   Plaud is not signed in
Checkout   on docs/design-20261023; Codespaces start from main
```

```screen
consult-sm-innovate · bookish-space-meme-v66pqg6wpvvqcpw7j
Recovery   off until you connect
Replace    stopped at the final sync; finish it
Cleanup    not finished for literate-pancake-jjjrq4j9rqwrc5qp6
Problem    arming.json cannot be read (+2 more); Show details has more
Codespace  stopped; you stopped it at 2026-11-13 07:55
```

```screen
consult-sm-innovate · no Codespace paired
Codespace  GitHub lists 1 for this repository
Checkout   on docs/design-ux1-high-5e4c-20261114; Codespaces start from main
```

```screen
consult-sm-innovate · no Codespace paired
Setup      not done yet
```

```screen
consult-sm-innovate · bookish-space-meme-v66pqg6wpvvqcpw7j
Codespace  stopped by GitHub's 12-hour limit at 07:55
Recovery   armed; out of restarts until 2026-11-15 20:12
Sync       stopped; connect to start it
History    12 conversations refused; forget them to clear the sync warning
Replaced   still on GitHub; check and delete it when ready
```

**`PHASE_WORDS`** (`replacing.py:22-31`), the one place the words live. `inspected`: `the first checks`;
`create-requested` and `created`: `creating the new Codespace`; `ready`: `the final sync`; `moved`: `the
sign-ins`; `signed-in`: `the last check`; `checked` and `offered`: `the delete question`. `cutover.py:207`
becomes `Replace stopped at the final sync: sync with {old} did not start.` Tests read the words from
`replacing.PHASE_WORDS`; none repeats them.

**The menu.** `Action.item` (`:57-59`) returns `fit(self.label)`. `run` still matches the choice by `item`
(`:412`) and still prints `Running: ` plus the command (`:413`); `next_lines` is unchanged.

| Key | Label |
|---|---|
| `connect` / `start` / `arm` / `stop` | `Connect` / `Start and connect` / `Connect and arm recovery` / `Stop the Codespace` |
| `signin` (new) | `Sign in`, argv `task_command(local, 'work:signin:all')`; offered when `local.signed_out`, paired and `local.worker == 'running'`; placed after the situation's own actions |
| `replace` | `Replace this Codespace` |
| `finish-replace` | `Finish the replace` |
| `cleanup:{target}` | `Finish cleaning up after the delete` |
| `delete:{retired}` | `Check and delete the replaced Codespace` |
| `resume` | `Resume the Codespace setup` |
| `pair:{name}`, `forget:…`, `pr:{n}`, `setup`, `create`, `creation-logs`, `update-checkout`, `upgrade`, `logs`, `details` | as today (`Pair {name}` keeps the name: it tells the choices apart) |

Labels in one frame are unique: when a label repeats, append ` (2)`, ` (3)` to the later ones.

Order and cut (replaces `:267-271` and `:352-359`):

1. `others` is `priority + candidates` as today (`:315-332`), without `replace` and without the retired delete.
2. `replace` exists under today's condition (`:269`): paired, a `connect` or `start` candidate exists, no active
   replace. **(B4.)**
3. `slots = 5 - (replace exists) - (delete exists)`; keep `others[:slots]`.
4. **(B8.)** If `local.logs` and `len(kept) + (replace exists) + (delete exists) < 3`, append `logs`. Nothing
   pads a menu without a log.
5. Then `replace`, then the delete, then `details`. `run` adds `Exit`, which counts toward #87's three.

- [ ] **Step 1: rewrite the tests** in `tests/test_overview.py`. The fixture (`:23-33`) changes to
  `root=Path('/fixture/consult-sm-innovate')`, `pairing='bookish-space-meme-v66pqg6wpvvqcpw7j'` and that name in
  the remote; every existing test keeps its intent under the new lines. New or changed:

| Test | Asserts |
|---|---|
| `test_each_situation_has_its_lines_and_actions` (`:224`, changed) | For every case of `:232-292`: `lines[0]` is the title line and `lines[1]` the situation row of the table above. Argv lists are today's, with `replace` moved to directly before `work:status` (or the delete), and `work:logs` present only in cases given `logs=True`. The `partial` case is gone. |
| `test_recovery_row_says_what_to_plan_around` | One `subTest` per row of the Recovery table: the exact text. |
| `test_rows_give_way_by_keep_rank` | With all twelve places applying: six lines, the rows are places 1 to 5 in order. With recovery armed and fine and no replace, cleanup or problem: `Codespace`, `Sync stopped`, `History`, `Sign-ins`, `Replaced` stay, and `Recovery`, `Checkout`, `Generator` go. |
| `test_frame_is_at_most_six_lines_of_78_cells_and_two_to_six_actions` (`:337`, renamed) | Today's product, extended with `signed_out` (0 or 5 labels), `history_pending_since`, `next_stop_at`, `restarts_free_at`, a 34-character branch on `other-branch`, and `logs`: at most 6 lines; every line at most 78 cells; no line ends in `…`; 2 to 6 actions; the last is `details`. |
| `test_line_one_cuts_the_folder_never_the_codespace` | Folder `consult-sm-strata-mainframe-modernisation`: line one is 78 cells or less and ends with the full Codespace name. A 100-character name: 78 cells, ends with `…`. |
| `test_a_long_problem_keeps_its_pointer` | Problem `retired/bookish-space-meme-v66pqg6wpvvqcpw7j.json cannot be read`, alone and with two more: the row is 78 cells or less and ends `; Show details has more` (and ` (+2 more); Show details has more`). |
| `test_menu_items_show_labels_only` | No `item` contains `(` plus a command; `item == fit(label)`; `next_lines` still equals `'Next: ' + command` for each. |
| `test_menu_labels_are_unique` | Two refusals whose session ids share their first 8 characters: two different items. |
| `test_replace_sits_above_the_delete_and_details` (replaces `:106`) | For each state of `:108`: `replace` is the last key before `delete:*` or `details`; with a full menu (`connect`, `arm`, `stop`, `signin`, two forgets, an upgrade) `replace` is still there and has `('task', 'devcontainer:setup', '--', '--replace')`. |
| `test_logs_appear_only_with_a_log` | `setup='missing'`: keys are `['setup', 'details']`. A stopped pairing with `logs=True`: `['start', 'logs', 'replace', 'details']`; with `logs=False`: `['start', 'replace', 'details']`. |
| `test_other_branch_checkout_is_named` | `Checkout   on docs/x; Codespaces start from main`; with a 60-character branch the line is 78 cells or less and still ends `; Codespaces start from main`. |
| `test_sign_in_is_offered_only_while_connected` | `signed_out=('Plaud',)`: the row is present with sync running and stopped; the `signin` action only when running, with argv `('task', 'work:signin:all')`. |
| `test_history_row_waits_five_minutes` | No row at 299 s, `History    not synced yet, 5 min so far` at 301 s. |
| `test_replaced_codespace_is_still_on_github` | With a listed retired Codespace: the `Replaced` row and the action `Check and delete the replaced Codespace`; unlisted: neither. |

  In `tests/test_work_overview.py`: `test_menu_runs_the_chosen_command_after_printing_it` (`:302`) picks by
  label and still sees `Running: ` plus the command; `:154` reads the phase words from `PHASE_WORDS`; `:178`
  drops `partial`. `tests/test_cutover.py:942` takes the new sentence.

- [ ] **Step 2: run** `python3 -m unittest tests.test_overview tests.test_work_overview -v`. Expected: failures
  on every changed line.
- [ ] **Step 3: implement** `view`, `Action.item`, `PHASE_WORDS`, `cutover.py:207`.
- [ ] **Step 4: run** `python3 -m unittest tests.test_overview tests.test_work_overview tests.test_standing
  tests.test_cutover tests.test_replacement tests.test_replacing -v`. Expected: OK. `tests.test_replacement`
  asserts phase words in `Replace stopped at …` lines; update those to read `PHASE_WORDS`.
- [ ] **Step 5: time it.** Run `task work </dev/null` in this repository ten times before and after; report both
  medians. The lazy `signins` import must not cost the 2-second budget.
- [ ] **Step 6: commit.** `Name the Codespace once on task work and show labelled rows`

**Verify (reviewer):** call `overview.view` on `tests/test_overview.py`'s fixture for each state, print every
line with `overview.cells`, and compare with the drawings. Then `task work </dev/null` in a set-up project:
state lines, a blank line, `Next:` commands, exit 0.

---

## Group 4: Starting a replace (C1, C2, C3)

**Files:**
- Modify: `devcontainer_setup/replacement.py`: `_Coordinator.__init__` (`:59-67`), `check_setup` (`:123-130`),
  `inspect` (`:132-175`), `run` (`:404-473`)
- Modify: `devcontainer_setup/project_sync/cutover.py:225-248` (the agents gate)
- Modify: `tests/test_replacement.py`, `tests/test_cutover.py`

**Interfaces:**
- Produces: `replacement.NOT_NOW = 'Not now'`, `REPLACE_IT = 'Replace it'`, `CONTINUE = 'Continue the replace'`,
  `ABANDON_KEEPING`, `ABANDON_NOTHING`; `_Coordinator.survey() -> dict`, `.preview(journal: dict) -> str`,
  `.confirm(journal: dict) -> bool`, `.begin(journal: dict) -> None`.
- Consumes: `replacing.PHASE_WORDS` from group 3; `overview.fit`.

### Task 4.1: ask first (C1)

**(Call 1.)** `inspect` (`:132-175`) splits:

- `survey()` is `:133-172` without the two consent prints (`:150-158`) and without `replacing.write`. It sets
  `self.old`, returns the journal dict and keeps the consent line in `self.consent_line` (or `None`). It reads
  only: the pairing file, GitHub (`api.get`, `api.machines`, `api.user`), the branch tip (`replace_inputs.pin`)
  and the setup check, exactly the checks `inspect` makes today.
- `begin(journal)` sets `self.journal`, calls `replacing.write`, prints `self.consent_line` when set, then the
  `Replacing …` line (`:174-175`). In the consent line, `Recovery on NEW will need` becomes `Recovery on the new
  Codespace will need`.
- `confirm(journal)` returns `self.choose(self.preview(journal), [NOT_NOW, REPLACE_IT]) == REPLACE_IT`.
  `Cancelled`, `KeyboardInterrupt` and `EOFError` raised by the prompt count as `Not now`.

In `run` (`:446-450`):

```python
            if self.journal is None:
                journal = self.survey()
                if not self.confirm(journal):
                    print('Nothing was replaced.', flush=True)
                    return 0
                self.begin(journal)
                result = self.launch()
                if result is not None:
                    return result
            elif self.journal['phase'] == 'inspected':
                self.begin(self.survey())   # confirmed before the interruption; C2 came first
                result = self.launch()
                if result is not None:
                    return result
```

The handler at `:470-471` returns `stop('cancelled')`, exit 1. Add before it: when a cancel arrives while
`self.journal is None`, print `Nothing was replaced.` and return 0. Refusals (`ValueError`, `:472-473`) keep
their text and exit 1.

`preview` builds the title; each line goes through `overview.fit(line, 78)`. **(Call 8.)** The spec draws the
machine, idle timeout and commit on one row; with `standardLinux32gb (4 cores)` that row is 81 cells, so the
commit gets its own row.

```screen
Replace bookish-space-meme-v66pqg6wpvvqcpw7j?
  New one  standardLinux32gb (4 cores), stops after 240 min idle
  From     main at 9f2fae2
  Billing  kyndryl-agentic-ai, from the moment it is created
  You      end agents when asked, approve the sign-ins, decide the delete
> Not now
  Replace it
```

Drawn through the product's `Gum.choose` with the cached Gum 2.0.2 in a pseudo-terminal: the four-line title
renders as shown, the cursor starts on `Not now`, Enter returns `Not now`, Down then Enter returns `Replace it`,
Esc and Ctrl-C raise `Cancelled`.

**The wizard path.** `interactive` and `_resume_setup_pr` call `replacement.run(project)` after the merge
(`__main__.py:1102-1104`, `:1271-1273`). They gain no parameter: the question is asked there too. **(Call 2.)**

- [ ] **Step 1: change the test fixture and write the failing tests** in `tests/test_replacement.py`. `UI.choose`
  (`:42-49`) answers `'continue'` when no answer is queued, which now means `Not now` for a fresh replace. Make
  the default `REPLACE_IT` when `options == [NOT_NOW, REPLACE_IT]`, `CONTINUE` otherwise.

| Test | Asserts |
|---|---|
| `test_fresh_replace_asks_before_writing_anything` | When `choose` is called for the preview: no `replace.json`, nothing under `replacements/`, no `codespace create` call, and the output so far has no `Replacing` and no `Recovery` line. The title equals the drawing built from the fixture's resources (`premium (8 cores)`, 120 min, `main at aaaaaaa`, billing `owner`). |
| `test_not_now_esc_and_ctrl_c_replace_nothing` | For `NOT_NOW`, `Cancelled()`, `KeyboardInterrupt()`, `EOFError()` (one `subTest` each): exit 0, output ends `Nothing was replaced.`, no journal, no archive, `creates() == []`. |
| `test_cancel_before_the_question_writes_nothing` | `api.get` raises `KeyboardInterrupt`: exit 0, `Nothing was replaced.`, no journal, `ui.calls == []`. |
| `test_a_refusal_before_the_question_still_exits_1` | `test_machine_not_offered_or_setup_missing_creates_nothing` (`:334`) still gets its reason and exit 1, and `ui.calls == []`. |
| `test_replace_it_starts_as_today` | The journal after the yes equals the one `inspect` wrote at `0c52227` for the same fixture, key by key except `operation` and `at`. |
| `test_a_rerun_does_not_ask_again` | For each phase of `test_each_phase_resumes_on_rerun` (`:629`), `inspected` included: no `choose` call offers `REPLACE_IT`. |
| `test_preview_fits_real_names` | `old` is the 36-character name, machine `premiumLinux256gb` with 32 cores, idle 1440: every title line is 78 cells or less. |

- [ ] **Step 2: run** `python3 -m unittest tests.test_replacement -v`. Expected: the new tests fail; many old
  ones still pass because the fixture default answers yes.
- [ ] **Step 3: implement.**
- [ ] **Step 4: run** `python3 -m unittest tests.test_replacement tests.test_wizard -v`. Expected: OK.
- [ ] **Step 5: commit.** `Ask before a replace writes or creates anything`

### Task 4.2: continue or abandon (C2), and small copy (C3)

`:429` becomes a title with the facts and two options. **(Call 8.)** The spec puts both names in the abandon
option; with the names above that is 125 cells, so they go in the title.

```screen
The replace is unfinished: it stopped at creating the new Codespace.
  Old one  bookish-space-meme-v66pqg6wpvvqcpw7j, still paired
  New one  literate-pancake-jjjrq4j9rqwrc5qp6, created
> Continue the replace
  Abandon it: the old one stays paired; the new one stays until you delete it
```

When `journal['new']` is `None`, the third line is `  New one  not created` and the second option is
`ABANDON_NOTHING = 'Abandon it: the old one stays paired; nothing was created'`. `:430` compares with the two
abandon constants and `:444` with `CONTINUE`. The lines printed after an abandon (`:439-442`) are unchanged.

C3:

- `:66` also sets `self.setup = 'task devcontainer:setup' if generated else 'task setup'`. `:127` ends `Run
  {self.setup} first.` and `:135` ends `Create one with {self.setup}.`
- `cutover.py:232` prints two lines instead of one: `On {old}: {agents} running.` and `End them in their Herdr
  panes, then press Enter to check again, or type stop.` As one line it would be 137 cells with the name above
  (today's line is 116).
- `cutover.py:236-248`: an answer that is not empty, not `stop`, and not `end` while only services run, prints
  one line before the loop goes on: `Not understood: press Enter, or type stop.` (agents) or `Not understood:
  press Enter, or type end or stop.` (services).

```screen
On bookish-space-meme-v66pqg6wpvvqcpw7j: codex (1) running.
End them in their Herdr panes, then press Enter to check again, or type stop.
> yes
Not understood: press Enter, or type stop.
```

- [ ] **Step 1: write the failing tests.**

| Test (file) | Asserts |
|---|---|
| `test_abandon_says_what_it_does` (`test_replacement.py`) | With `new` set: the options are `[CONTINUE, ABANDON_KEEPING]` and the title's three lines name the phase words, old and new. With `new` `None`: `ABANDON_NOTHING` and `New one  not created`. Every line 78 cells or less with the two real names. |
| `test_decline_or_cancel_before_the_move_keeps_old_paired` (`:684`, changed) | Same outcomes, answering with the constants. |
| `test_refusals_name_the_projects_setup_command` (`:341`, changed) | `Run task devcontainer:setup first.` in the generated fixture; with the wrapper file removed, `Run task setup first.` Same pair for the unpaired refusal. |
| `test_agents_gate_says_where_to_end_them` (`tests/test_cutover.py:323`, `:383`, changed) | The two new lines, each 78 cells or less with the 36-character name. |
| `test_an_unknown_word_at_the_gate_is_answered` (`tests/test_cutover.py`) | Answers `yes`, then `stop`: the output has `Not understood: press Enter, or type stop.` once and then `Nothing moved.`; with services only, the `end or stop` variant; Enter alone prints no `Not understood`. |

- [ ] **Step 2: run** `python3 -m unittest tests.test_replacement tests.test_cutover -v`. Expected: fail.
- [ ] **Step 3: implement.**
- [ ] **Step 4: run** the two modules. Expected: OK.
- [ ] **Step 5: commit.** `Say what abandoning a replace does and where to end agents`

**Verify (reviewer):** `python3 -m unittest tests.test_replacement`; then, in the test fixture, run
`replacement.run` with a UI that answers `Not now` and list the state directory: no `replace.json`, no
`replacements/`.

---

## Group 5: Connect, sign-in, Show details (D1, D2, D3, B9)

**Files:**
- Modify: `devcontainer_setup/project_sync/__main__.py`: `_arm` (`:108-146`), `_armed` (`:149-166`)
- Modify: `devcontainer_setup/project_sync/links.py:784`, `:797`
- Modify: `devcontainer_setup/project_sync/workspace.py`: `:314`, `remote` (`:532-534`), `recovery_lines`
  (`:618-706`), `status_text` (`:709-742`)
- Modify: `tests/test_project_sync_cli.py`, `tests/test_links_watcher.py`, `tests/test_workspace_codespaces.py`,
  `tests/test_workspace_recovery.py`, and any test that asserts a changed status row
  (`grep -rn "Keeper \|Keep-alive\|Sync worker\|Desktop" tests/`)

**Interfaces:** none shared with other groups. Tasks 5.1 to 5.4 touch four different functions.

### Task 5.1: `task work:signin` while connected (D1)

Both prints of `The connected worker is already handling links.` (`links.py:784`, `:797`) become these two
lines, and both returns become `return 0`:

```screen
Nothing to do: the connected sync already opens sign-in pages on this Mac.
To sign in to the tools, run task work:signin:all.
```

- [ ] **Step 1: tests.** `test_signin_while_connected_points_to_signin_all` (replaces
  `tests/test_links_watcher.py:420`): returns 0, the output is exactly the two lines, no connection is opened.
  `test_signin_refused_by_the_codespace_side_points_there_too`: with `_signin_config` yielding `False`, the same
  two lines and 0. `test_signin_refusal_preserves_worker_ssh_config` (`:428`) keeps passing.
- [ ] **Step 2: run** `python3 -m unittest tests.test_links_watcher -v`. Expected: the two fail (exit 1, old text).
- [ ] **Step 3: implement. Step 4: run again**, expected OK.
- [ ] **Step 5: commit.** `Point task work:signin to work:signin:all while connected`

### Task 5.2: a wrong word at `arm` (D2)

**(Call 3.)** Add `class _NotConfirmed(ValueError)` above `_arm`. `:139-140` raises
`_NotConfirmed('Not armed: the confirmation was not "arm"')`. In `_armed`, `:159` becomes:

```python
    try:
        _arm(project, state, args)
    except _NotConfirmed:
        print('Not armed. Connecting without recovery; arm later from task work.')
        return
```

The return is before `keeper.stop` and `keeper.start` (`:160-164`): `keeper.start` raises when the project is
not armed (`keeper.py:69-70`). Ctrl-C and end of input keep `Arming cancelled` (`:137-138`) and end the connect,
exit 1. `_resume_recovery` (`:169-194`) never prompts and does not change. Known and left as it is: when an
older consent is on file and recovery already runs, the line still says `Connecting without recovery`; nothing is
stopped, and the next `task work` shows the true state.

- [ ] **Step 1: tests** in `tests/test_project_sync_cli.py`, beside `ArmFlowTests` (`:73`).

| Test | Asserts |
|---|---|
| `test_wrong_word_goes_on_without_recovery` | `_armed` with the answer `yes` (and with an empty answer): returns, prints exactly the line, `keeper.start` and `keeper.stop` are not called, `arming.read(state)['armed']` is false. |
| `test_wrong_word_reaches_herdr` | `cli.run` for `work --armed` with `workspace.remote` patched to call its `armed` callback and return 0: returns 0. |
| `test_wrong_word_leaves_a_running_recovery_alone` | Consent on file for another account (so the prompt shows), recovery running: the same line; `keeper.stop` not called; `arming.json` byte-identical. |
| `test_ctrl_c_at_arm_still_ends_the_connect` | `EOFError` and `KeyboardInterrupt` from `input`: `_armed` raises `ValueError('Arming cancelled')`. |
| `test_wrong_answer_cancel_pairing_and_owner_refuse` (`:107`, unchanged) | `_arm` itself still raises on a wrong word: any future caller refuses by default. |
| `test_consent_text_is_unchanged` | sha256 of `arming.consent_text('p', 'c', 'o')` equals its value at `0c52227` (compute it in the test's first run and pin it). |

- [ ] **Step 2: run** the module. Expected: the first three fail with `ValueError: Not armed`.
- [ ] **Step 3: implement. Step 4: run again**, expected OK.
- [ ] **Step 5: commit.** `Connect without recovery after a wrong word at arm`

### Task 5.3: no pending-history line on connect (D3)

Delete `workspace.py:532-534`. `runtime = _ready(primary, state)` (`:522`) becomes `_ready(primary, state)`.
`:314`'s title `Codespace` becomes `Which Codespace should this project connect to?`.

- [ ] **Step 1: test** `test_connect_prints_no_history_line`, in the test class that already drives
  `workspace.remote` (`grep -n "workspace.remote(" tests/`): with `_ready` returning a pending cycle, the output
  of a successful connect has no `history`.
- [ ] **Step 2: run it**, expected fail. **Step 3: implement. Step 4: run**, expected OK.
- [ ] **Step 5: commit.** `Drop the pending-history line from a healthy connect`

### Task 5.4: `Show details` leads with the one fix (B9, E)

**(Call 5.)** `status_text` and `recovery_lines` read the same values as today (`recovery()`, `:583-600`): the
arming record, `keeper.status(state)` and the ledger count. Only words, hints and widths change. `--json` output
does not change.

Fields read, all as today. From `arming.json`: `armed`, `armed_at`, `gh_user`, `hold_until`, `ports`, and
`issue` with `path` when it cannot be read. From `keeper-runtime.json` through `keeper.status`: `running`,
`phase`, `heartbeat_age`, `pid`, `caffeinate`, `error`, `codespace`, `reading.{ok,state,at}`,
`anchor.{at,also}`, `down.{first_at,kind}`, `keepalive.{pid,ports_up,ports_skipped,error}`,
`last_restart.{at,outcome}` (its `rid` is no longer printed). From the ledger: the restart count. A missing
field prints what it prints today: no reading, `not read yet`; no anchor, `next 12-hour stop not known yet`; no
last restart, `Last restart none`.

1. **Lead line.** When the arming record says armed, recovery is not running and its phase is not `deleted`,
   `status_text` starts with:

```screen
Recovery is off: connect with task work:cs
```

2. **One fix.** In that state no other row names a command: the three hints `; run task work:cs` (`:642`),
   `; run task work:cs:armed` (`:660`) and `; run task work:cs:armed` (`:691`) are not printed.
3. **Words.** One name per thing:

| Today | Round 1 |
|---|---|
| `Sync worker: running` (`:716`) | `Sync: running` |
| `History: {outcome} ({stage})` (`:718`) | `History: {outcome}` (the stage stays in `--json`) |
| `Desktop → Codespace`, `Codespace → Desktop` (`:719`) | `Mac → Codespace`, `Codespace → Mac` |
| `Previous verified receipts retained; availability reflects the current cycle.` (`:725`) | not printed |
| `Keeper       not running` (`:642`) | `Running      no` |
| `Keeper       refused: {error}` (`:644`) | `Running      refused: {error}` |
| `Keeper       running (pid N) but its heartbeat is {missing \| N s old}; run task work:cs:armed` (`:646-647`) | `Running      yes (pid N), but silent {for N s \| since it started}; run task work:cs:armed` |
| `Keeper       running (pid N), heartbeat N s, caffeinate held \| not held` (`:649-650`) | `Running      yes (pid N), last check N s ago, this Mac is kept awake \| is not kept awake` |
| `; waiting: {error}` (`:652`) | unchanged |
| `; restarting {rid}` / `; restoring {rid}` / `; resuming {rid}` (`:654`) | `; restarting the Codespace` / `; reconnecting Herdr` / `; telling the agents to resume` |
| `Codespace    Available; 12-hour stop expected ~T (or ~T), counted from its last start at T` (`:668-670`) | `Codespace    Available; next 12-hour stop about T (or T), counted from its last start at T` |
| `Codespace    Available, no anchor yet` (`:672`) | `Codespace    Available; next 12-hour stop not known yet` |
| `; the keeper is restarting it ({rid}), follow task work:logs` (`:680`) | `; Recovery is restarting it, follow task work:logs` |
| `Keep-alive   holding (pid N); ports …` (`:687-689`) | `Kept up      yes (pid N); ports …` |
| `Keep-alive   not holding; ports …` (`:691`, `:694-695`, `:698-699`) | `Kept up      no; ports …`, with today's reason after it |
| `Last restart {rid} {time}: {outcome}` (`:702-703`) | `Last restart {time}: {outcome}` |

   Rows not listed keep their text. Paths print home-relative (`~/…`).
4. **Width.** Every row is wrapped at spaces to 78 cells: the text after the 13-cell label column goes through
   `textwrap.wrap(text, 65, break_long_words=False, break_on_hyphens=False)` and continuation lines are indented
   13 spaces (rows without the label column wrap at 78 with a 2-space indent). A single word longer than the
   room, such as a path, is not broken. The two drawings below are that rule's output.

```screen
Recovery is off: connect with task work:cs
Sync: stopped
Scratch: stopped
History: pending
Mac → Codespace: 0 received, 0 available, 0 pending, 0 unsupported
Codespace → Mac: 0 received, 0 available, 0 pending, 0 unsupported
Recovery     armed since 2026-11-11 11:19 (UTC 2026-11-11 14:19) as
             mannu_kyndryl, no hold; restarts 0 of 2 in 24 h
Running      no
Codespace    not read yet
Kept up      no; ports 13570 18787
Last restart none
```

```screen
Recovery     armed since 2026-11-11 11:19 (UTC 2026-11-11 14:19) as
             mannu_kyndryl, on hold until 2026-11-14 12:19 (UTC 2026-11-14
             15:19) (task work:hold -- off ends it); restarts 2 of 2 in 24 h
Running      yes (pid 51234), last check 3 s ago, this Mac is kept awake;
             restarting the Codespace
Codespace    Shutdown since 2026-11-14 11:13 (UTC 2026-11-14 14:13) (GitHub's
             12-hour stop); Recovery is restarting it, follow task work:logs
Kept up      no while the Codespace is Shutdown; ports 13570 18787
Last restart 2026-11-14 11:18 (UTC 2026-11-14 14:18): restarting
```

   Today the same two states print rows of 115, 155 and 193 cells (measured by calling `recovery_lines`).

- [ ] **Step 1: tests** in `tests/test_workspace_codespaces.py` (it already calls `recovery_lines`, `:196`).

| Test | Asserts |
|---|---|
| `test_status_leads_with_the_one_fix_after_a_restart` | Armed, recovery not running: line one is the lead line; `task work:cs` appears once in the whole text; `work:cs:armed` does not appear. |
| `test_no_lead_line_when_recovery_runs_or_is_not_armed` | Running, not armed, and phase `deleted`: line one is `Sync: …`. |
| `test_no_status_row_is_wider_than_78_cells` | For four states (after a Mac restart; armed and running with two stop candidates and a last restart; restarting during a hold; not armed) built as `recovery()` returns them: every line of `status_text` is 78 cells or less by `overview.cells`. |
| `test_status_uses_one_name_per_thing` | None of `Keeper`, `Keep-alive`, `heartbeat`, `caffeinate`, `anchor`, `Desktop`, `Sync worker`, `r-` plus digits appears in the four texts; `status(...)`'s JSON keys are unchanged. |
| `test_a_wrapped_row_loses_no_word` | For each state: the words of the wrapped text equal the words of the unwrapped rows. |

  Then update every test that asserts an old row (the `grep` above).

- [ ] **Step 2: run** `python3 -m unittest tests.test_workspace_codespaces tests.test_workspace_recovery
  tests.test_project_sync_cli tests.test_agent_hooks tests.test_env_files -v`. Expected: the new tests fail.
- [ ] **Step 3: implement. Step 4: run again**, expected OK.
- [ ] **Step 5: commit.** `Lead Show details with the one fix and keep its rows to 78 cells`

**Verify (reviewer):** call `workspace.status_text` on the four states and print each line with `overview.cells`;
run `task work:status` in this repository and compare its words with the table.

---

## Group 6: `ready.py` agrees with the wizard (A1)

Repository: `~/.local/share/chezmoi` (the dotfiles source), on a new branch
`feat/codespace-ready-default-holds-20261114` in a worktree of that repository. Never commit on its `main`; the
lead merges it and runs `chezmoi apply`. Gates there: `task test` and `task scan`.

**Files:**
- Modify: `private_dot_claude/skills/codespace-ready/scripts/ready.py:80-92` (check 1)
- Create: `tests/fixtures/default-holds-cases.json` (the bytes of group 1 step 1)
- Modify: `tests/test_codespace_ready.py` (`ReadyTests`, `:437`)

**(Call 4.)** How the two stay in step: the case table is one file with the same bytes in both repositories;
both suites run every case through their own implementation; both pin the same sha256, so a one-sided edit must
change a line whose failure message names the other copy; and the lead runs `cmp` on the two copies at
acceptance (group R). The table uses neutral names only (`acme/project`, `src/module_0`), so `task scan`'s
deny-list has nothing to match. A project with no GitHub `origin` is not in the table on purpose: `ready.py`
fails there (`:63-65`) and the wizard goes on with a commit-only setup.

Check 1 changes in two ways, matching `default_holds.measure`:

1. `here` and `there` (`:81-82`) leave out paths that start with `.devcontainer/`.
2. Before counting: if `git merge-base --is-ancestor HEAD <tip>` succeeds, print
   `ok    Code on {default}: {default} already holds every commit of {branch}.` and do not count.

The `FAIL` and `ok` lines otherwise keep their text.

- [ ] **Step 1: add the fixture file** and check `shasum -a 256` prints
  `723a5b9988f497d883225fd1ff8cab3e13521e1fc232a1347c16b1c0c5404293`.
- [ ] **Step 2: write the failing tests** in `ReadyTests`.

| Test | Asserts |
|---|---|
| `test_shared_cases` | For each case, built as group 1 step 1 describes: the `Code on main` line starts with `FAIL` exactly when `expect` is `refuse`; when `held` is not `null`, it contains `holds {held} of the {tracked} files`. |
| `test_cases_file_is_the_shared_copy` | The pinned sha256; the message names `emsilva/devcontainer tests/fixtures/default-holds-cases.json`. |

- [ ] **Step 3: run** `python3 -m unittest tests.test_codespace_ready -v`. Expected: `test_shared_cases` fails
  on two cases. Measured against today's `ready.py`: `setup files on both sides do not count as the project`
  passes with `holds 60 of the 70`, and `main, only behind, after GitHub's main renamed every file` fails with
  `holds 0 of the 10`. The other seven already agree.
- [ ] **Step 4: implement. Step 5: run** `task test` and `task scan`. Expected: green.
- [ ] **Step 6: commit.** `codespace-ready: leave setup files out and pass a checkout that is only behind`

**Verify (reviewer):** `python3 -m unittest tests.test_codespace_ready`, then
`cmp ~/.local/share/chezmoi/tests/fixtures/default-holds-cases.json
/Users/mannu/code/devcontainer/tests/fixtures/default-holds-cases.json` on the merged branches.

---

## Task R: the render file (Done means 2)

After groups 1 to 5 are merged. One builder.

**Files:**
- Create: `tests/render_screens.py` (a script, not collected as a test), `tests/test_render_screens.py`

`tests/render_screens.py` exposes `situations() -> list[tuple[str, Local, Remote | None]]` and
`render() -> str`, and prints `render()` when run as `python3 -m tests.render_screens`. It builds nothing of its
own: screens come from `overview.view` on `tests/test_overview.py`'s fixture, and wizard text from a run of
`devcontainer_setup.__main__.main` in `tests/test_wizard.py`'s `SetupFromDefaultBranchTests` fixture (a bare
repository behind a GitHub URL, a fake `gh`).

`situations()` covers, with the names of "Rules this plan adds": every row of the situation table, every row of
the Recovery table, the three busy frames drawn in group 3, inside a Codespace, and GitHub not answering. For
each, `render()` prints a heading, each line prefixed by its `overview.cells` width, then the menu labels and
the `Next:` lines. Then: a first setup's prompts (title and options, as the recording UI sees them) and its
trail; a rerun with launch history; the two A1 refusals captured from `main()`; the C1 and C2 titles from
`replacement._Coordinator.preview` and the continue-or-abandon title; the four `Show details` states.

- [ ] **Step 1: test.** `tests/test_render_screens.py::test_every_task_work_line_fits`: for every situation,
  at most 6 lines, each 78 cells or less, none ending in `…` except the two long-name cases, 2 to 6 actions.
  `test_render_names_every_situation`: `render()` has one heading per situation and no traceback text.
- [ ] **Step 2: run it**, expected fail (no module). **Step 3: implement. Step 4: run**, expected OK.
- [ ] **Step 5: write the file.**
  `python3 -m tests.render_screens > /Users/mannu/code/devcontainer/tmp/codespace-lifecycle/task-work/ux-round-1/render/RENDER.md`
  (outside Git). Report its path, sha256 and the widest `task work` line.
- [ ] **Step 6: commit.** `Render every round 1 screen for review`

## Acceptance (lead)

1. `task check` and `task test` at the merged tip, default environment, logged to `verify/<name>-<sha>.log` with
   the exit code. In the dotfiles repo: `task test` and `task scan`.
2. `cmp` of the two case tables; both pinned hashes equal.
3. The render file read line by line against this plan's drawings.
4. No Codespace is created, rebuilt or deleted to prove round 1. Roll-out and the owner's three live checks follow
   the spec's "Done means" 3 and 4.
