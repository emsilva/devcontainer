# Fresh sign-ins on every new Codespace: design (ruling 21)

Date: 2026-11-02. The third step of the owner's program (link helper, wizard, sign-ins, `task work`). Ruling 21
("Everything fresh": after create, every tool's sign-in page opens on the Mac, one approve per tool and per Claude
account, nothing copied) and ruling 13 (ccswap on every Codespace), in
`tmp/codespace-lifecycle/ux-run-2026-10-25/RULINGS.md`. Read-only analysis with source evidence and a red test:
`tmp/codespace-lifecycle/signins/analysis/REPORT.md` (REPORT). It builds on the link helper
(`docs/superpowers/specs/2026-11-01-link-helper-design.md`), whose live test has not run yet.

**Status: draft, four owner inputs pending** (section "Owner inputs"). Everything else is decided below.

## Goals

- After the wizard creates a Codespace and it passes validation and dotfiles, setup runs one sign-in sequence in the
  foreground: each tool's sign-in page opens on the Mac, the owner approves it, the tool finishes on the Codespace.
- A tool already signed in with the intended identity is skipped; a rerun signs in only what is missing.
- The same sequence can be started again on demand (a new `task work:signin:all`, and later from `task work`).
- Nothing is copied from the Mac or from an old Codespace; a replacement signs in again.

## Non-goals

- Copying tokens or auth files (rulings 10-12, superseded by 21). `claude setup-token` and token pasting.
- The `task work` menu itself (next step); it will call the same sequence.
- Changing what `task work:signin` does today (finish queued callbacks); it stays as the recovery action.

## Anti-goals

- Revoking or logging out a working identity just to make a browser appear (`codex login` clears existing auth
  first, REPORT "Codex"): a tool already signed in is never re-run.
- A sign-in that half-opens with nothing on the Mac listening: the sequence never starts a login before the Mac's
  link watcher reports ready on the Codespace (a fresh heartbeat).
- One failed sign-in stopping the rest, or leaving a forward or child process behind.

## How it works

1. **Where it runs.** Against the paired Codespace only, through the live project connection: the worker's link
   watcher serves that Codespace and its host key is already known (pairing is the sole point that accepts a new key,
   `scratch.py:144`). The sequence is a foreground terminal command, `task work:signin:all` (later also a `task work`
   action). It refuses, saying what to run, when the worker is not connected to the paired Codespace. It lives in
   `devcontainer_setup/project_sync/signins.py`, the package projects ship (`render.py:402-407`).
2. **When.** The wizard ends a create or a rebuild that leaves the Codespace paired by offering "Connect and sign in
   now? (Y/n)", which connects (as `task work:cs`) and runs the sequence in the same terminal. A rebuild resets
   everything outside `/workspaces`, so every sign-in is lost and the same offer follows it. A replacement (an unpaired
   create) signs in after its move, in the `task work` step. It never runs inside `Codespaces.launch`.
3. **The terminal.** Each CLI runs on the Codespace through an interactive `ssh -t` session, so the owner sees its own
   prompts, device codes and paste fallback; the sequence prints a status line before and after each tool. `BROWSER`
   reaches SSH-started processes (link-helper live test 1, `tmp/codespace-lifecycle/link-helper/spikes/RESULTS.md`).
4. **The order** (each with its status check first, REPORT table "Commands"):
   Plaud (`plaud login`; `plaud me`), Codex (`codex login`; `codex login status` showing ChatGPT), each Claude account
   (`claude auth login --claudeai --email <account>`, then `claude auth status --json` must show that email, then
   `ccswap add --alias <label>`; finally `ccswap switch <default>`; an account counts as signed in when ccswap's own
   account list holds that identity), OpenCode (`opencode auth login openai --method chatgpt-token-sharing`;
   `opencode auth list --format json` has an `openai` connection), Grok (`grok login --device-auth`; `grok models`
   prints `You are logged in with grok.com.` when signed in and `You are not authenticated.` when not, exit 0 both),
   then GitHub and Azure per the owner inputs.
5. **Status.** A check that cannot run (network) is "unknown", never "signed out". The Mac keeps only identity and
   state per Codespace, never a URL, code or token.
6. **Failures and cancel.** A failed, declined or timed-out tool is reported by name and the sequence goes on; the end
   prints a one-line summary and the retry command. The borrowed watcher's result is not visible to the sequence, so a
   tool that did not finish is reported as "not signed in (timed out or callback port busy: see the Mac notice)". Ctrl-C
   or a cancel stops the current tool's session, prints the summary and exits; nothing else is undone.
7. **Before building: a spike.** That `claude auth login` for a second account, after `ccswap add` saved the first,
   keeps the first account usable on ccswap 0.42.0 (REPORT marks it unverified). Run in the live session; if it fails,
   the Claude part is redesigned before the plan.

## Live findings (2026-11-02, test Codespaces on innovate; log `tmp/codespace-lifecycle/link-helper/LIVE.md`)

- **Ruling 24 (owner, 13:56Z):** "now grok cli as well pls ... needs to be part of the installation and login." Grok is
  installed with the other agents (builder grok-xhigh-9005) and joins this sequence.
- Every CLI below signed in on a fresh Codespace through the link helper, the owner approving in the Mac browser:
  `plaud login`; `codex login` (the first attempt left no callback forward and never finished, cause unrecorded
  because the link helper's log lines were dropped, fixed by builder linklog-xhigh-2594; the retry worked);
  `claude auth login --claudeai` (localhost redirect over the IPv4-only forward); `opencode auth login openai
  --method chatgpt-token-sharing`; `env -u GITHUB_TOKEN gh auth login --hostname github.com --git-protocol https
  --web` (answer `n` to "Authenticate Git", device code shown in the Codespace terminal); `grok login --device-auth`
  (device page opened on the Mac, code shown in the terminal).
- `grok login --oauth` did not finish: its redirect is `http://127.0.0.1:<port>/callback` and the forward was up for
  the whole 10 minutes, but after approval x.ai showed a code page instead of redirecting. The sequence uses
  `--device-auth` until that is understood.
- A second `claude auth login --claudeai` signed the browser's current claude.ai account in again; a second account
  needs `--email <second>` and the owner switching account in the browser. The ccswap two-account spike was skipped
  by the owner and stays unverified.
- `plaud me` reports `[AUTH_FAILED]` when signed out; `opencode auth logout` needs a terminal (account picker).
- **A rebuild erases everything outside `/workspaces`:** after `gh codespace rebuild` every tool above was signed out
  and the Codespace's Claude conversation files were gone (measured 14:54Z). This sharpens owner input 4.

## Owner inputs (pending; each changes the build)

1. **Claude accounts:** which email (and org) per account, the ccswap label for each, and which is the default.
2. **GitHub:** is the Codespace's own GitHub identity enough, or must each Codespace sign in gh separately through
   the browser (device code), and as which account?
3. **Azure:** all three projects install the Azure CLI. Sign in on every Codespace? If yes, which tenant/subscription,
   and the browser path (preferred in the spike) or device code?
4. **The wizard's rerun default.** Ruling 3 makes "rebuild the paired one" the default on every rerun. A rebuild
   erases every sign-in, so each rerun would ask for all of them again. Keep rebuild as the default, or default to
   leaving the Codespace as it is (rebuild only when chosen)?

## Constraints

- Every project in scope has continuity on (checked 2026-11-02 in the three checkouts), so a standalone sign-in task
  without continuity is not built; the wizard's retry covers that case.
- No installer change unless a tool's login interface is missing in the pinned version; never make an interactive
  sign-in a lifecycle-hook gate.
- Commit messages plain imperative, no AI attribution. Gates: `task check`, `task test`.

## Tests the plan must include

REPORT's red test (an all-tools session dispatches a gh device link; today's `signin_main` dispatches none) turns
green in the new session while `signin_main` keeps its callback-only contract. Plus REPORT's proposed tests:
status unknown versus signed out, wrong Claude identity refused, OpenCode's empty-but-0 inventory, no Codex re-login
when signed in, the watcher-ready gate, PTY for OpenCode, cancellation keeps the receipt, cleanup on INT/TERM.

## Left to the builder

Module layout (REPORT proposes `devcontainer_setup/signins.py`), the PTY runner, the per-Codespace status record.

## Calls made by the lead (each can be overruled)

1. **Continue past a failed tool, with a retry list** (REPORT owner question 5): Plaud's 120-second window and
   several Claude approvals make a stop-and-wait sequence worse.
2. **No standalone task without continuity** (REPORT question 4): no project in scope turns it off.
3. **Run after create in the wizard first**, not only from `task work`, which ships later.

## Builder check (2026-11-02) and the calls it settled

A fresh reader found four questions and five contradictions; the lead verified them. The first draft ran the sequence
inside `Codespaces.launch`, where the Codespace is not yet paired, its host key is unknown, and a borrowed watcher
would serve the old Codespace; it also missed that a rebuild erases sign-ins. "How it works" now answers each:

4. **Sign-ins run against the paired Codespace through the live connection**, never inside `launch`, and never accept
   a new host key (Q1, A).
5. **The wizard offers connect-and-sign-in after a create or a rebuild that leaves the Codespace paired**; replacement
   signs in from the `task work` step (Q2, B).
6. **Each CLI gets an interactive terminal on the Codespace** (Q3).
7. **The shared sequence lives in `project_sync`** so the project task can import it (D).
8. **Unfinished tools are reported without guessing the cause**; the Mac notice names a busy port (E).
9. **A spike on ccswap multi-account sign-in comes before the plan** (Q4).
