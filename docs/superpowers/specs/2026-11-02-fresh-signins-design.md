# Fresh sign-ins on every new Codespace: design (ruling 21)

Date: 2026-11-02. The third step of the owner's program (link helper, wizard, sign-ins, `task work`). Ruling 21
("Everything fresh": after create, every tool's sign-in page opens on the Mac, one approve per tool and per Claude
account, nothing copied) and ruling 13 (ccswap on every Codespace), in
`tmp/codespace-lifecycle/ux-run-2026-10-25/RULINGS.md`. Read-only analysis with source evidence and a red test:
`tmp/codespace-lifecycle/signins/analysis/REPORT.md` (REPORT). It builds on the link helper
(`docs/superpowers/specs/2026-11-01-link-helper-design.md`), whose live test has not run yet.

**Status: draft, three owner inputs pending** (section "Owner inputs"). Everything else is decided below.

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

1. **When.** A post-ready hook in `Codespaces.launch`, after validation and dotfiles, on a fresh create and on an
   explicit retry (never on the early creation callback). Cancelling a sign-in keeps the Codespace and its receipt.
2. **The Mac side.** Before the first login, the sequence borrows the running worker's link watcher when it is live,
   or starts its own temporary watcher that serves every link kind (not only callbacks; gh and device links too), and
   waits for the remote heartbeat. It never runs two consumers of the same queue.
3. **The order** (each with its status check first, REPORT table "Commands"):
   Plaud (`plaud login`; `plaud me`), Codex (`codex login`; `codex login status` showing ChatGPT), each Claude account
   (`claude auth login --claudeai --email <account>`, then `claude auth status --json` must show that email, then
   `ccswap add --alias <label>`; finally `ccswap switch <default>`), OpenCode
   (`opencode auth login openai --method chatgpt-token-sharing`; `opencode auth list --format json` has an `openai`
   connection), then GitHub and Azure per the owner inputs. Each login runs on the Codespace in a PTY (OpenCode opens
   its browser only with a TTY; the link helper finds the requester through its terminal).
4. **Status.** A check that cannot run (network) is "unknown", never "signed out". Only identity and state are
   shown or kept; no URL, code or token is written on the Mac.
5. **Failures.** A failed, declined or timed-out tool is reported by name and the sequence goes on; the end prints a
   one-line summary and the retry command. A busy Mac callback port is named with its remedy (link helper rule); the
   holder is never stopped. A lost connection stops owned children and forwards and says "reconnect and retry".

## Owner inputs (pending; each changes the build)

1. **Claude accounts:** which email (and org) per account, the ccswap label for each, and which is the default.
2. **GitHub:** is the Codespace's own GitHub identity enough, or must each Codespace sign in gh separately through
   the browser (device code), and as which account?
3. **Azure:** all three projects install the Azure CLI. Sign in on every Codespace? If yes, which tenant/subscription,
   and the browser path (preferred in the spike) or device code?

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
