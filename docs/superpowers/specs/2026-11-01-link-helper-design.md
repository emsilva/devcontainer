# Links from a Codespace open on the Mac: design

Date: 2026-11-01. Ruling 14, the step the owner placed after the tools batch on 2026-10-29 ("tool installs
first and ship, then the link helper as its own design"). Rulings 18 ("any cli that logs in through the
browser") and 19 ("it would be cool if every single one of them"), in
`tmp/codespace-lifecycle/ux-run-2026-10-25/RULINGS.md`. Research, with file and line evidence for every claim
about a CLI: `tmp/codespace-lifecycle/ux-run-2026-10-25/login-helper-research/REPORT.md` (REPORT below).

The owner approved this playback on 2026-11-01 ("thats it"): links always open on the Mac, whether the owner
is at it, busy or away ("well, if I'm doing that its fine"); sign-ins finish by themselves; each link shows a
notice naming the project; it works only while the project is connected; a busy Mac port is named with the
way around it. The owner did not answer whether the link may be printed when no Mac is connected; call 1
below takes that as yes. A builder check (a fresh reader, 2026-11-01) raised six questions and six
contradictions with the cited code; each was checked against the source and is settled below (calls 1, 2,
4, 6, 8, 10, 11 and "How it works"). The owner approved the design on 2026-11-01 (ruling 20: "Only for real
decisions"; the link helper design counts as approved).

## Calls made by the lead (each can be overruled)

1. **When no Mac is listening, the link is shown in the terminal where the command ran.** The helper then
   exits 1, so each CLI's own fallback runs: Codex, Claude, OpenCode, gh and az already show their link.
   The helper writes the link itself only for the rest (Plaud, whose opener ignores the exit code, and CLIs
   not in the REPORT catalog), never into a full-screen agent's screen. Only the link the tool asked to
   open, never a callback. The link carries a client id, a `state` value and a PKCE challenge, all public
   by design; no password or token. Ruling 10 ("never printed") is read as covering sign-ins themselves,
   not this link. Accepted with it: the Mac's browser history keeps the callback URL, whose one-time code
   is already spent and bound to PKCE.
2. **No "allow this?" step.** Every `http`/`https` link opens, to any host. Each one posts a notice.
3. **Notices:** a macOS notice through `project_sync/notify.py` for every link, plus a Herdr toast
   (`herdr notification show <title> --body …`, present in the Mac's Herdr) when a Herdr server on the Mac
   answers. Ruling 16's Herdr half for the keeper's notices stays out of this batch.
4. **The callback forward runs over the project SSH,** `ssh -F <state>/ssh-config -o ClearAllForwardings=no`,
   not `gh codespace ports forward`. On 2026-10-26 `gh codespace ssh` broke on mainstage while the project
   SSH kept working, and Plaud signed in through this kind of forward to `127.0.0.1:8199`
   (PLAN-agents-and-signin.md:140-146). The Codespace end is `127.0.0.1:P`: every catalogued CLI listens
   there (Plaud also on `::1`).
5. **A loopback link with no sign-in in it** (a dev server's `--open`, say `http://localhost:5173/`) also
   opens, through a forward of the same port that lasts while the Codespace still listens on it. Without
   this, ruling 19's "every link" would open pages that cannot load.
6. **`task work:signin`** finishes waiting sign-ins when the worker's link watcher is not live, with the same
   code. It is also the harness the first live tests use. It starts nothing on the Codespace (see dead ends).
7. **The Codespace half ships only in generated containers** (`templates/`), like ccswap and Plaud in the
   tools batch. This repo's own container is unchanged.
8. **The `BROWSER` line joins the existing root step** (`templates/scripts/system-path.sh`), so setup keeps
   one `sudo -n` step with the same idempotence. This repo's own container runs the same script
   (`.devcontainer/scripts/post-create.sh:149`) and never installs the helper, so the line is written only
   when a third argument names the helper's path. Generated post-create passes it, and its recovery message
   (`templates/scripts/post-create-selected.sh:6`) gains it; this repo's call keeps two arguments.
9. **The `xdg-open` stand-in ships only if live test 3 shows OpenCode needs it.**
10. **Lifetimes.** A sign-in request (one with a loopback `redirect_uri`) waits until its deadline (120 s for
    Plaud, 10 minutes otherwise) or until its requester exits. Any other link opens only if the Mac takes it
    within 30 seconds; after that it is dropped, so a stale link never opens at the next connect.
    `task work:signin` handles sign-ins only.
11. **If live test 1 shows an agent's tool shell does not see `BROWSER`, the lead stops and brings the result
    to the owner.** The remaining places to set it are the agents' own settings files, which the owner's
    dotfiles may manage, so the generator does not write them unasked.

## Intent and why

Signing a tool in on a Codespace today means a hand-made forward and a hand-copied link: Plaud on mainstage
needed a capture script and an `ssh -L` (2026-10-26); OpenCode's ChatGPT sign-in on strata-sap failed in the
browser until its random port 45495 was forwarded by hand (2026-10-25). Links that are not sign-ins (an
agent's `gh pr view --web`) go nowhere. The owner wants every such link to behave as if the tool ran on the
Mac, for the owner's commands and for agents alike.

## Goals

- Any process on the Codespace that opens an `http`/`https` link through the usual openers (`$BROWSER`,
  `xdg-open`, npm `open`, Python `webbrowser`, Go `browser`) makes it open in the Mac's default browser
  within about 2 seconds, while the project is connected (`task work:cs`, or `task work:signin`).
- Browser sign-ins with a loopback callback finish on the Codespace with no other step: Plaud (fixed 8199),
  Codex (1455, then 1457), OpenCode ChatGPT (random port) and OpenCode MCP (19876 by default), Claude
  `/login` and `setup-token` (random port), az auth code (random port). Device-code and paste-back flows (gh,
  `codex --device-auth`, az device code, Claude's paste field) need only the page opened, and get it.
- Each link opened shows a macOS notice: project name, the requesting command's name and the link's host.
- A busy Mac port is never worked around: the notice and the Codespace terminal name the Mac process that
  holds it and the way around it (table below).
- With no Mac listening, the link shows in the terminal where the command ran (call 1).
- The sign-in itself (token exchange, token files) stays on the Codespace. Nothing about a sign-in is
  written to the Mac's disk or its logs.

## Non-goals

- Signing tools in after a Codespace is created (step 4). The owner ruled on 2026-11-01 (ruling 21,
  "Everything fresh") that nothing is copied: step 4 opens each tool's sign-in through this helper.
- The `task work` menu (#87, step 5).
- VS Code terminals and the VS Code browser tab: VS Code sets its own `BROWSER` and keeps its own flow.
- Upgrading any project. Innovate runs generator `9ad61ea`; mainstage and strata-sap are older and their
  roll-out is deferred by the owner. Real Codespaces get this only through a later roll-out.
- Ruling 16's Herdr toast for the keeper's notices; Codex sync (#53).

## How it works

**Codespace side.**
- `~/.local/bin/open-on-mac` (installed by `templates/scripts/install-tools.sh`). `$BROWSER` points at it.
  Contract, from REPORT Q3: one absolute path, no arguments; takes the URL as `$1`; exits within about
  100 ms (Python's `webbrowser` waits for it, `src/cpython/webbrowser.py:182-191`); writes nothing to its
  own stdout or stderr; accepts only `http`/`https` (anything else: exit 1 so the opener tries its next
  method). Exit 0 when a Mac is listening, 1 when not (call 1).
- **The requester is found by walking up the process tree,** not from the parent: npm `open` starts its
  bundled `xdg-open` detached with stdio ignored (`src/npm-open/x/package/index.js:227-231`), and that
  script runs the helper as its own child, so the parent is `sh` with `/dev/null` for fds 0-2. The
  requester is the first ancestor with a terminal on fd 0, 1 or 2; its program is named from its command
  line (`node …/@plaud-ai/cli/…` is `plaud`, `python3 … az` is `az`).
- It writes one request, mode 600, to `~/.local/state/devcontainer-links/requests/<id>.json`: the URL, the
  time, the requester (pid, start time, program) and its terminal. Requests past their lifetime (call 10)
  are discarded by whoever reads them.
- When `~/.local/state/devcontainer-links/mac-listening` is older than 15 seconds, no Mac is listening: the
  helper writes the link to the requester's terminal under call 1's rule and exits 1.
- `system-path.sh` writes `BROWSER=/home/vscode/.local/bin/open-on-mac` to `/etc/environment` (read by PAM
  for every SSH session, so the Herdr server, its panes and agent children inherit it) and to the zshenv
  block as `: ${BROWSER:=…}; export BROWSER`, set only when unset, so VS Code terminals keep their own.
- Live test 1 checks that agent tool shells see it (Claude's binary removes it in one background mode,
  REPORT spike 1). If one does not, call 11 applies.
- `validate-selected.sh` checks that `open-on-mac` is installed and executable and that a fresh
  non-interactive SSH session sees `BROWSER` pointing at it.

**Mac side (`devcontainer_setup/project_sync/`).**
- The scratch worker (`scratch.supervise`) starts a link watcher thread inside its
  `with CodespaceConnection(...)` block, after `ssh-config` is written, and stops it when the block exits.
  Not in the 5-second loop: that loop runs `synchronize()`, whose stages can take up to 240 s
  (`sync_cycle.py:17`).
- The thread keeps one `ssh -F <state>/ssh-config -T project-sync python3 -I -B -c <watcher>` open. The remote
  watcher claims new requests (rename, so each is handled once), streams them as JSON lines, and touches
  `mac-listening` every 5 seconds. When the stream ends while the connection lives, the thread restarts it
  with backoff (2 s doubling to 60 s).
- Each request is untrusted input from any Codespace process. Parse strictly: `http` or `https`, at most
  8 KB, a port (when present) in 1024–65535. Loopback hosts are `localhost`, `127.0.0.1` and `[::1]`. The
  port to forward is the port in a loopback `redirect_uri` query value, if there is one; else, for a link
  to a loopback host, the link's own port; else none. Reject anything else with a notice. Logs and notices
  carry the host, the port and the requesting program, never the URL.
- A message for the Codespace terminal (a busy port) goes back through the watcher to the terminal the
  request recorded, under call 1's rule; the macOS notice always carries it.
- No port needed: open with `/usr/bin/open <url>` (argument list, no shell) and notify.
- A port P: preflight first. The Mac port must equal the Codespace port (Codex's are on an identity
  provider's allow-list, `server.rs:79-80`), so the keep-alive's `+10000` offset and `keepalive.free_ports`
  do not apply. Test-bind `127.0.0.1:P` (and `::1:P` when the host is `localhost`), and name a holder with
  `lsof -nP -iTCP:P -sTCP:LISTEN`. Check that P is listening on the Codespace, read-only, from
  `/proc/net/tcp` and `/proc/net/tcp6`. Never send HTTP to a callback server: Codex and OpenCode's legacy
  sign-in treat `/cancel` as a cancel.
- Then start `ssh -F <state>/ssh-config -o ClearAllForwardings=no -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=15 -N -L 127.0.0.1:P:127.0.0.1:P project-sync` with `start_new_session`, prove it
  with `lsof -a -p <ssh pid> -nP -iTCP:P -sTCP:LISTEN`, then open the URL and notify.
- The forward ends when the Codespace stops listening on P (checked every 2 s), at its deadline (call 10;
  none for a plain loopback link, which only the worker serves), on INT, TERM or HUP, or when its owner
  stops. End it by process group. Record pid, process identity and the owner (worker or `work:signin`,
  with its own process identity) in the project state dir. A later run ends a leftover only when its owner
  is gone, proven by fact as `keepalive.end_left_behind` does, never by pid alone; a live owner's forwards
  are left alone.
- One forward per port. A second request for a port already forwarded opens the URL on the existing forward
  if it targets the same Codespace port; two different ports run side by side.

**Busy Mac port: what the notice and the Codespace terminal say.**

| Program | Port | Way around it |
|---|---|---|
| plaud | 8199 | Stop the Mac's own `plaud login` or `plaud-mcp http`, then sign in again. Plaud has no device code |
| codex | 1455, 1457 | `codex login --device-auth` |
| opencode | random, 1455, 19876 | Pick the device-code method, or sign in again (a new random port) |
| claude | random | Paste the code from the page into Claude's "Paste code here" field |
| az | random | `az login --use-device-code` |
| anything else | P | Name the holder; nothing else |

**`task work:signin`.** Opens its own `CodespaceConnection`, handles every unexpired sign-in request with
the code above, keeps their forwards until they end, and exits when none is left. It refuses while the
worker's link watcher is live (its heartbeat is fresh), not merely while the worker process is alive: the
worker can sit in backoff or the Deleted retry outside its connection, with no watcher running
(`scratch.py:744-779`). Requests are claimed by rename on the Codespace, so the two never handle the same one.

## Calls left to the builder

- The helper's language (POSIX sh, or Python started by an absolute interpreter path).
- How the remote watcher sees new files (inotify through `ctypes`, or polling at 250 ms or less).
- The probe for P: one-shot `connection.python()` calls, or messages on the watcher stream.
- File names under `~/.local/state/devcontainer-links/` and in the Mac project state dir.
- Notice wording, within: project name, program, host; for a busy port, the holder and the way around it.

## Live tests the plan runs first

On an innovate test branch and an unpaired test Codespace billed to kyndryl-agentic-ai, deleted when done
(ruling 15; recipe in memory `live-test-unpushed-generator`). Each settles a cell REPORT marks unverified:

1. Does `BROWSER` from `/etc/environment` reach a new Herdr pane after a Herdr server restart, `ssh
   project-sync 'printenv BROWSER'`, and the tool shells of Claude, Codex and OpenCode? Decides system file
   versus per-agent wiring.
2. With a recording helper: the link each of `plaud login`, `opencode providers login` (three OpenAI
   methods), `codex login`, Claude `/login`, `gh auth login` and `az login` (with and without `CODESPACES`)
   opens. Decides the catalog's open cells, and whether az's switch to a browser sign-in works.
3. Does OpenCode's bundled `open` reach `$BROWSER`, a PATH `xdg-open` stand-in, or neither? Decides call 9.
4. Does walking up from the helper find the requester's terminal and program for Plaud (started detached by
   npm `open`), az (Python `webbrowser`) and an agent's `gh pr view --web`? Decides call 1's path.
5. Busy port: a dummy listener on Mac 8199, then `plaud login`; on 1455, then `codex login`.
6. Disconnect mid-sign-in: no forward left (`pgrep -fl 'ssh -F .* -L'` empty), a clean rerun.
7. Plaud and OpenCode ChatGPT at the same time.
8. Claude's `localhost` redirect with only the IPv4 forward. Decides whether `-L [::1]:P:…` is added.

## Definition of done

On a test Codespace built from the plan's branch, with the Mac connected through `task work:cs`:
- `plaud login`, `codex login`, Claude `/login`, OpenCode's ChatGPT sign-in and `gh auth login` each open on
  the Mac, finish on the Codespace with no other step, and leave no forward behind.
- `gh pr view --web` run by an agent in a Herdr pane opens on the Mac with a notice.
- A loopback dev-server link opens and loads.
- Live tests 5–7 behave as written above; with the worker stopped, `plaud login` prints its link, and
  `task work:signin` then finishes it.
- The gates (`task check`, `task test`) pass at the branch's commit, re-run by the lead.

## Dead ends (settled; do not reopen)

- **Starting sign-ins from the Mac (REPORT shape A) alone.** Many sign-ins start inside a process already
  running (Claude `/login`, MCP OAuth, OpenCode's TUI, Codex's TUI); a Mac task cannot start those, and an
  agent on the Codespace cannot start a Mac task.
- **An `xdg-open` stand-in alone.** Plaud's npm `open` runs its own bundled `xdg-open`; Claude without
  `DISPLAY` never calls `xdg-open`; Python registers it only with a display (REPORT Q3).
- **A `RemoteForward` socket, Herdr, OSC 8/52, `.scratch` or a LaunchAgent as the channel** (REPORT, "The
  channel back to the Mac"): each fails on the alias producer's pinned settings, a missing feature, no port
  forward, writing to the Mac's disk, or ruling 5.
- **A different Mac port than the Codespace's.** Identity providers check the callback port.
