# Setup wizard step: publish without an editor, rerun at Review, update the checkout: design (#80, #81, #63)

Date: 2026-11-02. The second step of the owner's program (ruling 14 order: link helper, wizard, sign-ins, `task
work`). Owner rulings 2, 3, 4 (2026-10-25) and 22 (2026-11-01), in
`tmp/codespace-lifecycle/ux-run-2026-10-25/RULINGS.md`; ruling 20 ("Only for real decisions") lets the lead write
this, have it checked, plan and build it. Issue texts (offline): `tmp/codespace-lifecycle/wizard/issues.md`. Read-only
analysis with file:line evidence and three red tests: `tmp/codespace-lifecycle/wizard/analysis/REPORT.md` (REPORT).

## The owner's words

- Ruling 2: "Publish is split from the editor. Two choices: Publish (merge into main / local commit only) and
  Codespace (none / create like last time / rebuild the paired one / replace it). Setup never opens an editor; an
  opt-in preference keeps it for anyone who wants it."
- Ruling 3: "A rerun opens at Review. It uses the recorded selections, the remembered dotfiles, and last time's
  machine and idle timeout, and asks only about what the scan finds that's new. With a Codespace paired, the
  Codespace menu defaults to rebuilding it, never to 'Create a new codespace'." The rebuild default is superseded by
  ruling 28 (2026-11-02): "Leave it as it is"; see the [fresh sign-ins design](2026-11-02-fresh-signins-design.md).
- Ruling 4: "Keep the merge prompt, made clearer. y/N, default No, with the review-bypass context on its own line."
- Ruling 22: "A rebuild or replacement keeps the cores of the Codespace it replaces."
- #63 (issue text): setup leaves the checkout behind without saying why, then points at the rebind on old code.

## Goals

- Publish and the Codespace action are two independent choices. Publish=merge with Codespace=none pushes, opens the
  PR, merges only on an explicit yes, and updates the Mac checkout. Nothing opens an editor unless the owner set
  `open_after_create` to `browser` or `vscode`.
- An ordinary rerun of a configured project shows Review first, built from the saved answers; it asks a question
  before Review only for a new scan finding.
- With a usable paired Codespace, the Codespace choice defaults to `none` (ruling 28). An explicit rebuild keeps its
  live machine and idle timeout (ruling 22).
- After a merge, the owner's default-branch checkout is fast-forwarded when that is safe; when it is not, setup says
  exactly why and what to run, before any connection or rebind instruction.

## Non-goals

- Replacing a Codespace. It needs the `task work` step's transaction (create, move sync, recovery and Herdr, check,
  then the checked delete, rulings 8 and 6). This step does not show "replace"; the `task work` step adds it here and
  in its own menu.
- Fresh sign-ins after create (the sign-ins step, ruling 21), arming, the keeper and `task work` itself.
- Changing what the scan detects.

## Anti-goals

- Opening an editor, or `gh codespace code`, without the opt-in preference (this is #80's bug; the browser open is
  also the suspected cause of the keeper's late anchor, #61).
- Re-asking every screen on a rerun (#81's bug), or silently dropping a new runtime/tool the scan found.
- Moving the owner's checkout in any unsafe way: no stash, force, branch switch, rebase or merge into a feature branch.
- A rebuild that changes the machine, or a default of four cores for a paired premium Codespace.

## How it works

### Publish and Codespace (#80, ruling 2)

`After setup` is replaced by two choices, both on Review:
- **Publish:** `merge into <default>` (default with a GitHub origin) or `local commit only` (default otherwise).
  Push, PR and merge depend on Publish only, on the normal path and on the resumed-open-PR path alike.
- **Codespace:** `none` (the default when the pairing is usable); `rebuild <paired name>` (an explicit choice when
  the pairing is usable); `reuse <name>`
  for every other Codespace of the repository, as today, so a second one is never created silently (ruling 14.1,
  `test_existing_codespace_menu_never_defaults_to_create`); `create like last time` only when no usable pairing exists.
  Default without a pairing: reuse an existing Codespace on this branch if there is one, else `none` on a rerun and
  `create like last time` on first setup. Create, rebuild and reuse need the setup on the default branch: with
  `local commit only` and unpublished changes they are not offered, and Review says why.
- The editor opens only when the user preference `open_after_create` is `browser` or `vscode` (default `none`),
  and only after a create. Rebuild never opens anything. New receipts drop the `client` field; a legacy receipt's
  `client` never counts as the opt-in.
- `rebuild` (menu) passes a new force flag to `launch()`: it rebuilds even when the receipt shows this commit
  already validated. `reuse` and `--resume NAME` keep today's exact behaviour (the receipt decides: a pending rebuild
  is redone, a completed one is skipped), for any Codespace of the repository, paired or not.
- SSH: `selections.ssh` or continuity or any Codespace action other than `none`. A rerun with `none` keeps the saved
  value (never drops sshd/2222 silently). When the Codespace choice turns SSH on, Review re-renders and shows it.

### Rerun at Review (#81, ruling 3)

- A project whose `devcontainer.json` already has `customizations.devcontainerSetup.selections` opens at Review,
  built from those selections, the remembered dotfiles repository, and the resolved launch values below. First setup
  still walks every screen.
- Before Review, apply `_select`'s non-interactive normalization exactly as today (adopt a declared exact package
  manager version, drop managers of unselected runtimes): those show as changes on Review, not questions.
- Before Review, ask only about new scan findings: a runtime, tool, port or manager recommendation, or a
  conflicting declaration, that is neither in the saved selections nor in the recorded scan decisions. A changed
  evidence path alone is not new. Each answer, including a decline, is recorded so it is never asked again.
- Scan decisions are stored beside the selections in the same metadata block (`scanDecisions`: the normalized
  finding and the answer). Old metadata without the record asks each missing positive finding once, then records it.
- Review's Change answers opens the full editor (every screen today's flow has, both advanced gates). View diff and
  Cancel stay. The routine reuse and commit/push/PR confirms are folded into Review's Continue; the merge prompt stays
  separate.
- The project's saved `selections.dotfiles` decides whether dotfiles apply on a rerun, everywhere a gate reads it
  today (including `_preferences_after_commit` and the launch path), not the user-wide default; a rerun never writes
  it back to the user preferences. If it is on and this machine has no remembered repository, ask for the repository
  before Review (a missing answer, not a new finding).

### Machine and idle timeout (ruling 22)

- Explicit rebuild of the paired Codespace: its live machine and idle timeout (`Codespaces.get(name)`), never a
  receipt. A usable paired Codespace stays as it is on a rerun unless a rebuild is chosen (ruling 28).
- Create like last time without a pairing: the last successful launch receipt for this repository's default branch.
- No history: ask once (today's machine and idle screens), then remember through the receipt.
- A recorded machine that is no longer offered, or a paired Codespace whose machine cannot be read: say so on Review
  and ask; never fall back to four cores silently. Review and the launch line show the resolved values.

### Merge prompt (ruling 4)

The review-bypass context is printed on its own line, then `Merge PR #<n> into <default> as <user>? (y/N)`, default
No. Enter, No and Ctrl-C leave the PR open; only an explicit yes merges.

### The checkout (#63)

- After a merge (with any Codespace choice, including none), setup fast-forwards the owner's original checkout when it
  is on the default branch and has no tracked changes. Untracked files no longer block it; Git's own fast-forward
  refuses a real collision.
- The update returns a result: updated, already current, or refused with a reason (fetch failed, tracked changes,
  another branch, detached, diverged, a file in the way), with the short HEAD and how many commits behind.
- The ending prints, on stdout and in this order: the checkout result (and for a refusal the exact command to run);
  then, when continuity is bound elsewhere, the rebind command. On a feature branch it says to update the default-branch
  checkout, never to merge into the feature branch. A Codespace is called deleted only on the existing deletion
  evidence (a full inventory), otherwise "unavailable".

## Constraints

- Generated projects and this repo's own `.devcontainer` keep working; existing receipts stay readable.
- Preferences: every older generator pin rejects unknown keys in `~/.config/devcontainer-setup/config.json`
  (`preferences.py:30-31`), and all three projects run older pins until rolled out. So `open_after_create` lives in
  its own file, `~/.config/devcontainer-setup/open-after-create.json`, which old pins never read. And, at the origin,
  the new loader keeps unknown keys on save and ignores them on read, so the next new key does not repeat this.
- Every existing safety check stays: reviewed-head and default-branch containment before launch, dirty/branch/divergence
  refusals on reuse, receipt-before-validation ordering.
- Commit messages plain imperative, no AI attribution. Gates: `task check`, `task test`.

## Tests the plan must include

REPORT's three red tests turn green (#80 no editor with `none`; #81 Review first with no new finding; #63 unrelated
untracked notes do not block the fast-forward). Plus the cases REPORT lists under "Tests to pin" for each issue, and:
Publish=merge + Codespace=none on the resumed-open-PR path; a paired premium machine with a stale standard receipt
rebuilds on premium; one new runtime asks exactly one question and a recorded decline never repeats; the existing
untracked-collision, dirty, feature-branch, detached and diverged refusals still hold.

## Left to the builder

Names and shapes of the publish/Codespace option objects and the checkout result; the `scanDecisions` encoding; how
Review lays the two choices out; how the full editor is reused under Change answers.

## Calls made by the lead (each can be overruled)

1. **"Replace" is not shown until the `task work` step ships.** Ruling 2 lists it, but a replace that only prints a
   pointer would be a dead option; the `task work` step adds it with its transaction (REPORT "Rebuild and replace").
2. **Explicit rebuild always rebuilds**, even at the same commit; an interrupted rebuild still resumes idempotently.
3. **`open_after_create` is set in the launch editor under Change answers** and saved in the user preferences.
4. **Create and rebuild are not offered with `local commit only`**, because a Codespace builds from the published
   default branch; Review says so.
5. **Scan decisions live in the project's metadata**, so every machine that reruns setup sees the same answers.
6. **Untracked files no longer block the checkout update**; tracked changes still do (REPORT #63 controls).

## Builder check (2026-11-02) and the calls it settled

A fresh reader checked this design against the code and found ten questions and five contradictions; the lead
verified the key ones in source (`preferences.py:30-31` rejects unknown keys; `__main__.py:615-631` lists every
Codespace of the repository). The sections above now carry the answers. The calls:

7. **`open_after_create` gets its own file**, and the preferences loader stops rejecting unknown keys (C1).
8. **`--resume NAME` and `reuse` keep today's meaning; only the menu's `rebuild` forces a rebuild** (Q2, C3).
9. **Every Codespace of the repository stays listed** as `reuse <name>`; `create` is offered only without a usable
   pairing (a second Codespace for a paired project is a replacement, the `task work` step) (Q3, Q10).
10. **SSH = saved value, or continuity, or a Codespace action**; never dropped by choosing `none` (Q4, C2).
11. **The open-PR path asks the two choices as two plain questions** (it has no render to review) (Q5, C4).
12. **A rerun with nothing to publish** shows Publish as "nothing to publish (already on <default>)"; the checkout
    update runs whenever the run ends with the setup on the default branch, merged now or already there (Q6).
13. **A decline is recorded in the project metadata and published like any change** (one line on Review's diff);
    with `local commit only` it is asked again next run, and Review says so (Q7).
14. **The project's dotfiles choice governs every gate; a missing repository on this machine is asked** (Q8).
15. **Today's silent package-manager normalization still runs before Review** and shows as a change (Q9, C5).
16. **With an unusable pairing** (404, other branch), Review says why and offers `none`, `reuse` of others and
    `create like last time` using the last receipt's machine (Q10).
17. **REPORT's red tests are analysis scaffolding**: the builder writes its own red tests in the suite (Q11).
