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
  Codespace menu defaults to rebuilding it, never to 'Create a new codespace'."
- Ruling 4: "Keep the merge prompt, made clearer. y/N, default No, with the review-bypass context on its own line."
- Ruling 22: "A rebuild or replacement keeps the cores of the Codespace it replaces."
- #63 (issue text): setup leaves the checkout behind without saying why, then points at the rebind on old code.

## Goals

- Publish and the Codespace action are two independent choices. Publish=merge with Codespace=none pushes, opens the
  PR, merges only on an explicit yes, and updates the Mac checkout. Nothing opens an editor unless the owner set
  `open_after_create` to `browser` or `vscode`.
- An ordinary rerun of a configured project shows Review first, built from the saved answers; it asks a question
  before Review only for a new scan finding.
- With a paired Codespace, the Codespace choice defaults to rebuilding it, on the machine it has now.
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
- **Codespace:** `none`, `create like last time`, `rebuild <paired name>` (only when one is paired; then it is the
  default). Without a pairing the default is `none` on a rerun and `create like last time` on first setup.
  Create and rebuild need a published setup: with `local commit only` they are not offered, and Review says why.
- The editor opens only when the user preference `open_after_create` is `browser` or `vscode` (default `none`),
  and only after a create. Rebuild never opens anything. New receipts drop the `client` field; a legacy receipt's
  `client` never counts as the opt-in.
- `rebuild` forces `gh codespace rebuild` even when the commit has not changed (an explicit request), then runs
  today's validation; today's idempotent retry after an interrupted rebuild is kept.
- SSH stays on whenever continuity is on, whatever the choices (`render.py` invariant).

### Rerun at Review (#81, ruling 3)

- A project whose `devcontainer.json` already has `customizations.devcontainerSetup.selections` opens at Review,
  built from those selections, the remembered dotfiles repository, and the resolved launch values below. First setup
  still walks every screen.
- Before Review, ask only about new scan findings: a runtime, tool, port or manager recommendation, or a
  conflicting declaration, that is neither in the saved selections nor in the recorded scan decisions. A changed
  evidence path alone is not new. Each answer, including a decline, is recorded so it is never asked again.
- Scan decisions are stored beside the selections in the same metadata block (`scanDecisions`: the normalized
  finding and the answer). Old metadata without the record asks each missing positive finding once, then records it.
- Review's Change answers opens the full editor (every screen today's flow has, both advanced gates). View diff and
  Cancel stay. The routine reuse and commit/push/PR confirms are folded into Review's Continue; the merge prompt stays
  separate.
- The project's saved `selections.dotfiles` decides whether dotfiles apply on a rerun, not the user-wide default.

### Machine and idle timeout (ruling 22)

- Rebuild of the paired Codespace: its live machine and idle timeout (`Codespaces.get(name)`), never a receipt.
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

- Generated projects and this repo's own `.devcontainer` keep working; existing receipts and preference files stay
  readable (unknown preference keys are rejected today, so `open_after_create` is added to the schema).
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
