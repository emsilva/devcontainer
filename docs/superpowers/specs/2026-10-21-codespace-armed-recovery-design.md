# Codespace recovery after GitHub's 12-hour stop: design

Written 2026-10-21 (-03:00). Inputs:
- the brief, `tmp/codespace-lifecycle/BRIEF.md`;
- the stack review, `tmp/codespace-lifecycle/STACK-UNIFY-SYNTHESIS.md`;
- the overnight tests, `tmp/codespace-lifecycle/tests/TEST-STATE.md` and `MORNING-REPORT.md`;
- issues #35, #36, #38, #42, #45, #46, #47 and #48.

The `tmp/` paths are machine-local and gitignored. Every fact this design rests on is restated in section 3 with the record that measured it, so the design stands without them.

Three kinds of statement appear below, and each is labeled:
- **Owner ruling**: decided by the owner, with the date.
- **Measured**: observed, with its source.
- **Design call**: decided here, for the owner to check while reading. Section 11 lists them all.

The stack review's other proposals are not owner decisions: the Mac-wide LaunchAgent, the installed runtime and its fifteen open questions. This design does not depend on them.

## 1. Why

GitHub stops every Codespace 12 hours after it starts. When that happens at 3 a.m. or during a block of meetings, the agents sit idle until the owner notices. When the owner brings the Codespace back, Herdr resumes the agents with `--resume`. They find their services down and assume something went wrong, so the owner has to tell them, in their words:

> Codespace restarted, we are back. You need to bring the services that you require up and continue with the work

On 2026-10-21 two 12-hour stops needed a restart: innovate at 10:09Z and mainstage at 14:22Z. The mainstage stop cut off a long multi-agent workflow mid-run. Mainstage also had two planned restarts, at 02:10Z and 01:23Z. Each restart needed:
- a correctly timed start request;
- in one case, a stale SSH connection on the Mac cleared by hand;
- the port forward reopened by hand;
- the resume sentence typed or sent.

The goal is that none of this needs the owner.

## 2. Owner rulings

1. **Recovery comes first** (2026-10-20/21). Parking the agents before the stop (the owner's original "hour 10" idea) is later work.
2. **Only armed projects are restarted** (2026-10-20/21):
   - at most 2 restarts in any 24 hours;
   - at least 30 minutes apart;
   - the owner can put restarts on hold.
3. **Unarmed projects only get a notification** (2026-10-20/21). Nothing starts them.
4. **`work:cs` must not hold a pane** (2026-10-20/21). It returns once the work is handed off.
5. **Keep-alive: an armed project gets the K4 child** (2026-10-21, "lets follow your recommendation"). That is `gh codespace ssh … 'exec sleep infinity'` under the read-only API. It also carries the port forwards and replaces `cs:ports`. Recovery after the 12-hour stop is still needed, because keep-alive does not prevent that stop.
6. **A project's Codespace must be on main** (2026-10-21). Setup reaches main through a PR merged with the admin bypass (owner: "can we do 1 with --admin?"). The setup branch is cut from the current main. Then the Codespace is created on main.
7. **Existing Codespaces: innovate now, mainstage later** (2026-10-21). innovate moved to main that day (consult-sm-innovate PR #3). Mainstage moves at a moment the owner picks, after its lead's `docs/model-resilience-design` work merges.
8. **Standing rules** (brief):
   - Never change mainstage's Codespace without the owner's go-ahead.
   - Never switch the active gh account (mannu_kyndryl).
   - Never print a secret.
   - Gaps found in a Codespace are fixed here, in the generator.

## 3. What was measured

| Fact | Source |
|---|---|
| The stop comes 12 h + 10 to 30 s after the latest accepted start request, meaning one that brought the Codespace up. Creation counts as a start. This held on 3 of 3 Codespaces: +10 s, +21 s and +29 s. | Probe: start 01:37:32Z, ShuttingDown 13:37:44Z (`results/long/K4.tsv`). Mainstage: POST 02:21:51Z, stop 14:22:12Z. innovate: created 22:08:24Z, stop ~10:08:53Z. TEST-STATE "The 12-hour clock, settled". |
| VM boot time is not the clock. Pooled VMs boot minutes to hours before the start. | btime vs start: innovate 4.5 min before, mainstage 83 min before (TEST-STATE). |
| A start request that changes nothing does not reset the clock, but it does move `last_used_at`. So `last_used_at` is the last start *request*. | innovate's 22:53:55Z `last_used_at` bump; it still stopped at creation + 12 h. |
| A start request during ShuttingDown returns 409 or is ignored. The Codespace still goes to Shutdown and stays there. | Probe test (synthesis, lead verification). |
| Shutdown with `pending_operation` false, then one start request: accepted every time, Available 24 to 62 s later, plus about 60 s in Queued. | `ka.py start` 10:16Z; `mainstage-restart2.log`; `mainstage-restart3.log`. |
| Stopping takes about 4.5 min from the stop request to Shutdown with nothing pending. A live client stream holds ShuttingDown open: 11 min while `mirror.sh` was copying. | `mainstage-restart3.log`: 01:23:49Z to 01:28:18Z. TEST-STATE: 02:10:55Z to 02:21:51Z. |
| The API gives no stop reason. `last_known_stop_notice` and `idle_timeout_notice` are null even on lifetime stops. `updated_at` on a stopped row tracks each read, so it is not the stop time. | TEST-STATE window 1. |
| Keep-alive. These hold an idle Codespace: K4 (a positional remote command), K3 (cs:ports' `-N -L`) and K2 (the Herdr `--stdio` link). These do not: K0 (nothing), K1 (the sync worker's forward) and K5 (`-N` with no traffic). | Keep-alive matrix, probe with a 5-min idle timeout (MORNING-REPORT). |
| K4 held the probe for 10 h 55 min, until the 12-hour stop, and was never denied a request. Exact argv: `gh codespace ssh -c <name> -- -i <key> -o ServerAliveInterval=0 -o TCPKeepAlive=no 'exec sleep infinity'`, under `ReadOnlyAPI`, **with no `-L` forwards**. | `results/long/K4.json`; `ka.py:81-83`. |
| The sync worker reconnects by itself after a restart, in under 250 s, with no `work:cs`. | innovate recovery (TEST-STATE). |
| Herdr becomes reachable 21 to 64 s after Available. The Herdr TUI's bridge starts the remote server, and agents come back with the same `agent_session`. | TEST-STATE: innovate 10:50Z, mainstage 14:36Z and 01:30Z. |
| Restore relaunches an agent as `<agent> --resume <id>`, dropping its launch flags. Upstream treats this as intended and closed it twice (herdrdev/herdr#965, #3388). The maintainer's advice is to put persistent behavior in the agent's own settings. | innovate test agent lost `--model haiku --permission-mode plan`. Mainstage's lead came back with bypass on, from its settings. |
| A stale Herdr SSH master can outlive the stop, and then Herdr never reconnects. This happened on 1 of 3 stops, and ending the master fixed it in 21 s. | #46. |
| The Codespace's GITHUB_TOKEN in `.env-secrets` changes on every restart. | Hash prefix before and after innovate's restart (TEST-STATE). |
| Resume delivery: the lead echoed the rid but shortened the rest. Proof of delivery must match the rid alone. | `results/innovate-recovery/resume-manual-screen.txt`. |
| Under launchd, the keychain token read, the GET and an osascript notification all work, and own-session children survive job events. Still open: the token read with the screen locked. | MORNING-REPORT, launchd section. |

## 4. Goals, non-goals, anti-goals

**Goals**
- An armed project's Codespace stays up while the Mac is up, and its ports are reachable on the Mac.
- After GitHub's 12-hour stop, an armed project is started again. The Mac side reconnects, and the lead is told, in the owner's words, to bring its services up and continue. No keystroke is needed.
- Every stop of a connected project that is not restarted produces exactly one Mac notification saying what happened and what to run.
- New Codespaces are created on main, and setup reaches main through a merged PR.
- The Herdr connection can no longer get stuck on a dead SSH proxy (#46).

**Non-goals for this design**
- Parking agents before the stop (ruling 1), and the "commit and checkpoint" nudge before it. Both are next.
- Surviving a Mac reboot or logout. After one, the owner runs `task work:cs:armed` again, as `work:cs` today (design call D1).
- The Mac-wide LaunchAgent and the installed runtime from the stack review.
- Sync cadence, checkpoint garbage collection and transcript coverage (#39, #40, #45).
- GITHUB_TOKEN inside Herdr panes (#35, #44) and Claude/Codex settings in the Codespace (#38). They are related, but separate.

**Anti-goals.** Any of these makes the design a failure, even if a restart works:
- A Codespace the owner stopped on purpose comes back.
- Any process other than the keeper can start a Codespace in the background.
- A restart loop spends credits: more than 2 restarts in 24 hours, or a start request repeated outside the measured sequence.
- Text is typed into a pane that is not an idle, recognized agent.
- Mainstage's Codespace or its sync worker is touched before the owner's go-ahead.
- `work:cs` or `work:cs:armed` keeps a pane busy.

## 5. Overview

`task work:cs:armed` connects the project exactly as `task work:cs` does today, then arms it. Arming does three things:
1. records the owner's consent;
2. starts one **keeper** process for the project, detached like today's sync worker, and returns;
3. hands keep-alive to that keeper: it holds a K4 child that also carries the port forwards.

The keeper reads the Codespace's state once a minute. When the 12-hour stop arrives, it waits for Shutdown, sends one correctly timed start request, waits for Available, clears any stale Herdr proxy, waits for Herdr, and sends the lead the owner's sentence. It is the only background process that can start a Codespace, and only within the budget.

Unarmed projects keep today's behavior. Their sync worker gains a one-time notification per stop (with #48's correct reason).

Separately, two fixes land in code that every project already runs:
- the Herdr SSH proxy exits when its Codespace is no longer Available (#46, origin fix);
- setup creates Codespaces on main (#47).

What runs on the Mac for an armed project: the Herdr TUI and its per-machine processes, unchanged; the sync worker, its forward and its Mutagen daemon, unchanged; plus **the keeper**, its **K4 child** and one `caffeinate`.

## 6. Design

### 6.1 Arming: `task work:cs:armed`

1. Run today's `workspace.remote` path: pair, connect, start the worker and open Herdr. It still returns without holding a pane (ruling 4). It still needs a terminal the first time, for pairing.
2. **Refuse to arm while a foreign process holds this Codespace's link**, and name its pid. A foreign process is any `gh codespace ssh`/`ports forward` for this Codespace that this package did not start, such as mainstage's `cs:ports` or `mirror.sh`. Plain gh can start a stopped Codespace, and it would also hold the ports the K4 child needs.
3. **Consent, asked once and recorded.** The text is: "Arm \<project\>: keep \<codespace\> running while this Mac is up, and start it again after GitHub's 12-hour stop (at most 2 times in 24 hours, 30 minutes apart). Usage is billed to \<billable_owner.login\>. Type arm to confirm." The billing owner comes from the Codespace's API row.
4. **Record the account.** Store the gh account whose token the keeper will use (`gh_user`): the account `github_token()` resolves at arm time. It must have the `codespace` scope. The keeper reads that token per call with `gh auth token --user <gh_user>`, so a later `gh auth switch` cannot move it.
5. **Ports.**
   - The default list is the project's `.devcontainer/devcontainer.json` `forwardPorts` minus 2222. Each remote port N maps to local 127.0.0.1:N+10000.
   - `task work:cs:armed -- --port 3571` adds one, and `--no-port 8787` removes one.
   - The list is stored. A refused remote port fails only its own connection, so forwarding an idle port is harmless (#36, K3).
6. Write `arming.json`, start or adopt the keeper (6.2), and wait up to 60 s for its first heartbeat with the K4 child up. Print a status block.
7. **A repeat run is a no-op, checked by observation:** same keeper pid and nonce, same K4 child pid, no new ledger rows. It prints "Already armed" and the status block. It never asks for consent twice.

`task work:cs` (unarmed) is unchanged, except for the notification in 6.7.

### 6.2 The keeper process

- **One per armed project**, started by the CLI and never by another background process. It reuses the worker's launch pattern (`scratch.start`, `scratch.py:182-226`):
  - `subprocess.Popen(..., start_new_session=True)` from the same vendored package;
  - a random nonce in argv;
  - adoption by pid, `ps lstart` and nonce (the `_owned` pattern, `scratch.py:157-171`);
  - a single-instance lock in the state dir.
- **Environment.** It runs with an allow-listed environment: HOME, PATH, LANG and TMPDIR, plus the recorded `gh_user`. It never runs with the pane's environment.
- **Stop by fact.** `task work:disarm` and `task work:stop` find the keeper by `_owned` and SIGTERM it. On SIGTERM it ends its K4 child and `caffeinate`, then exits. `task work:stop` also stops the worker, as today.
- **Mac sleep.** While it runs, the keeper holds `caffeinate -i -w <keeper pid>`. A closed lid still sleeps the Mac; that is a stated precondition.
- **No silent death.**
  - Every subprocess the keeper runs has a timeout.
  - An exception in one loop pass is logged, and the loop continues with backoff.
  - The keeper writes a heartbeat every pass.
  - The sync worker, which already runs, checks the keeper's heartbeat. If the project is armed and the heartbeat is over 5 minutes old or the keeper is not `_owned`, the worker sends one notification: "Recovery for \<project\> is not running; run task work:cs:armed" (design call D7).
- **Files**, all in the project's private state dir `~/.local/state/devcontainer-setup/projects/<id>/`, which `state.py` already allows:

| File | Single writer | Content |
|---|---|---|
| `arming.json` | the CLI, under `workspace-control.lock` | schema, armed, armed_at, gh_user, consent text sha256 and time, billable_owner, ports, hold_until, owner_stop_at |
| `keeper-runtime.json` | the keeper | pid, nonce, process_identity, heartbeat_at, phase, last GET (time, state, pending), anchor {at, source}, K4 child {pid, ports up, ports skipped}, last restart summary |
| `keeper-ledger.jsonl` | the keeper, append-only | one `intent` line before and one `result` line after each start request: rid, UTC time, HTTP status, never a body |
| `keeper.log` | the keeper | rotating text log, one line per phase change, UTC time and pid on every line |

### 6.3 Keep-alive child with ports (#36)

- **Command**, run under `ReadOnlyAPI` (the env from `api.child_env()`):

  ```
  gh codespace ssh -c <name> -- -i ~/.ssh/codespaces.auto
     -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes
     -L 127.0.0.1:<N+10000>:localhost:<N> …  'exec sleep infinity'
  ```

  The positional command sets gh's keep-alive override, and `ReadOnlyAPI` means it cannot start a stopped Codespace.
- **Not yet measured with the forwards.** The measured K4 had no `-L` and used `ServerAliveInterval=0` (section 3). The slice that builds this child must measure the exact argv above before anything relies on it (section 9, slice E).
- **Lifecycle.**
  - Started when the keeper sees the Codespace Available.
  - Restarted with backoff (2 s doubling to 60 s) if it exits while the Codespace is still Available.
  - Not restarted while the Codespace is not Available.
- **Bind check.** Before launch, the keeper test-binds each local port. A port already in use is skipped and shown in status as "13570 in use by pid X", and the child starts without it. Keep-alive matters more than one port.
- **Replaces** mainstage's project-local `cs:ports:*` once mainstage is armed. The owner stops `cs:ports` at that moment (section 7).

### 6.4 Recovery after the 12-hour stop

The keeper loop runs once a minute. Its GET goes over direct HTTPS with redirects disabled, the `transport._upstream` pattern, and reads only `GET /user/codespaces/<name>`.

1. **Anchor.** The anchor is when the Codespace's current run started (design call D2):
   - after a start the keeper made, the time of the accepted POST that brought it up (source `keeper`). The stop comes 12 h after that POST (section 3);
   - when the keeper sees the Codespace go from not-Available to Available without having started it, the time of that GET (source `observed`, ±60 s);
   - at arm time, for a Codespace that is already Available, `last_used_at` (source `api`). That is correct unless a start request was ignored after the real start.
2. **Detect.** Three consecutive successful GETs show ShuttingDown or Shutdown. Each of these resets the count: a failed GET, a 5xx, a 401/403/404, a non-JSON body, or a name mismatch.
3. **Classify** when the count reaches three:
   - `OWNER`: `owner_stop_at` (6.8) is later than the anchor. The keeper logs it and does nothing else.
   - `LIFETIME`: the first non-Available reading falls between anchor + 11 h 45 min and anchor + 12 h 15 min.
   - `OTHER`: anything else. That covers an idle stop (keep-alive failed), a stop from the GitHub UI, a resize, or an anchor that was wrong. **Never restarted.** One notification: "\<project\> stopped at HH:MM (-03:00) for a reason other than the 12-hour limit; run task work:cs to start it." (design call D3)
4. **Gate**, for LIFETIME only:
   - armed;
   - no hold active (`hold_until` in the future);
   - fewer than 2 restart events (rids) in the last 24 h, at least 30 min since the last;
   - the token read works, and the ledger is writable.

   If any check fails, the keeper does not start the Codespace. It sends one notification naming the check.
5. **Start**, using one shared start routine that `work:cs` uses too (6.9):
   1. Wait for Shutdown with `pending_operation` false, for up to 20 min.
   2. Allocate a rid (`r-HHMMSSZ`) and write the ledger `intent` line, then POST `/user/codespaces/<name>/start` with the token for `gh_user`, then write the `result` line.
   3. 409 means "not started yet": wait for Shutdown with nothing pending again, then POST once more. Any other 4xx stops the restart and notifies. A 5xx is retried within the same rid, at most 3 POSTs per rid.
   4. If the state is still Shutdown 2 minutes after an accepted POST, POST once more.
   5. Wait for Available, for up to 45 min. The budget counts rids, not POSTs.
6. **Restore.**
   1. Record the anchor (source `keeper`): the time of the POST after which the Codespace left Shutdown.
   2. Start the K4 child.
   3. End stale Herdr proxies (6.5).
   4. Wait for the Herdr machine (`herdr machine status <machine_id> --json`) to read `reachable`, for up to 10 min. The machine id comes from the project's `herdr-machine.json`. If Herdr is not reachable, notify that the Codespace is back but Herdr is not, and that the Herdr TUI must be open. Then stop, with no resume.
   5. The sync worker reconnects by itself. The keeper does not wait on it, and status shows its phase.
7. **Resume** (6.6).

A keeper that starts and finds the Codespace already stopped classifies with whatever anchor it has. With only an `api` anchor or none, the stop is `OTHER`, which fails safe.

### 6.5 The stale Herdr proxy (#46)

- **Origin fix in `herdr_stdio`** (`transport.py:194`), for every project, armed or not:
  - While gh `--stdio` runs, the proxy reads the Codespace's state every 30 s through its existing single GET route.
  - After 3 consecutive successful reads that show the Codespace is not Available, it terminates gh. A failed or odd read counts for nothing.
  - When gh exits, ssh sees EOF and the master closes. Herdr's own retry then reconnects once the Codespace is back.
  - Cost: about 120 GETs an hour per open Herdr connection.
- **Defensive layer in the keeper**, labeled as such in the code with a link to #46. At restore, the keeper terminates (SIGTERM) any `herdr_stdio` process whose argv names this Codespace and this state dir and whose start time is earlier than the stop. The proxy's `finally` then reaps its gh. This uses only processes this package owns and never signals Herdr's own processes.
- **Not yet measured:** whether ending the proxy closes the master as quickly as `ssh -O exit` did. Slice F measures it on innovate.

### 6.6 Resume message

- **Who.** The live agents on the project's Herdr machine (`herdr --machine <id> agent list`) whose name starts with `lead-`.
  - Exactly one: send to it.
  - None or several: send nothing, and notify "\<project\> is back; tell its lead to resume" (design call D4).
  - Always pass `--machine`, because the Mac has agents with the same names.
- **When.** Only after a restart the keeper made, once per rid. The lead must be `idle` on two reads 10 s apart. If it is not idle after 10 min, notify instead.
- **Text.** The owner's sentence, then one line of facts, as one `agent prompt`:

  > Codespace restarted, we are back. You need to bring the services that you require up and continue with the work. (Automatic restart \<rid\>: GitHub's 12-hour stop at HH:MMZ, back at HH:MMZ. Restored agents came back with `--resume` and without their launch flags, so check each worker's model, effort and sandbox before dispatching to it.)

  The second part comes from the upstream ruling on #965 (section 3). A Codex worker launched read-only comes back with the Codespace's `~/.codex/config.toml` defaults, so it cannot be assumed to still be read-only.
- **Proof of delivery.** Within 2 min, the rid appears in `agent read --source recent-unwrapped`, or the agent's status turns `working`. If neither happens and the agent is idle, send once more. Otherwise notify.
- **Logging.** The keeper logs the delivery, never the screen content.

### 6.7 Notifications

- **Channel:** `osascript -e 'display notification …'`. It was measured under launchd. Slice C verifies it from a detached process started in a Herdr pane.
- **One notification per event**, never repeated, keyed by the stop's first non-Available reading.
- **Armed projects** (from the keeper):
  - restart done and lead told;
  - restart refused (a gate check, or `OTHER`);
  - start failed;
  - Herdr not reachable;
  - lead not found or not idle.
- **Unarmed projects** (from the sync worker, which already watches the Codespace through its read-only GET):
  - one notification when the Codespace stops: "\<project\>'s Codespace stopped (\<reason if known\>); run task work:cs to bring it back".
  - With #48 fixed in the same slice, the worker reports the real state instead of "identity/authentication must be valid".
  - A project with no worker running gets nothing, because nothing is watching it.

### 6.8 Commands and status

| Command | What it does |
|---|---|
| `task work:cs:armed` | 6.1 |
| `task work:cs` | unchanged |
| `task work:hold -- 09:30` / `-- off` | No restarts or resume prompts until that local time (-03:00). Keep-alive continues. A stop during a hold is not restarted later; it notifies at the stop (design call D5). |
| `task work:disarm` | Stops the keeper (and so the K4 child) by fact and sets armed false. Sync continues. |
| `task work:cs:stop` | Writes `owner_stop_at`, then POSTs stop with the owner present. The keeper classifies it as `OWNER` and never restarts it. |
| `task work:cs:restart` | A planned restart with the owner present, like the one asked for at 01:23Z: write `owner_stop_at`, stop, wait for Shutdown, start through the shared routine (6.9). If the project is armed, the keeper then runs restore and resume as for its own restart. It does not use the budget (design call D6). |
| `task work:status` | Gains a "Recovery" block (below). `-- --json` includes `keeper-runtime.json`. |
| `task work:logs` | The last 80 lines of `keeper.log` and `scratch.log`, interleaved by time. |

Status example (illustrative):

```
Recovery     armed since 21:12 (-03:00) as mannu_kyndryl, no hold; restarts 1 of 2 in 24 h
Keeper       running (pid 4121), heartbeat 12 s, caffeinate held
Codespace    Available since 22:29 (01:29Z), anchor keeper; 12-hour stop due ~10:29 (13:29Z)
Keep-alive   holding (pid 5402); ports 13570 13571 13572 18787
Last restart r-012820Z 22:28 (-03:00): back in 63 s, Herdr in 64 s, lead told 22:31
```

Each line that is not fine says what to run.

### 6.9 One start routine for the foreground and the keeper

Today `Codespaces.start_codespace` (`project_sync/codespaces.py:127-157`) has three faults:
- it POSTs without checking the state;
- it reports a 409 as "check GitHub authentication";
- it times out at 300 s.

A start during ShuttingDown is ignored (section 3), so `work:cs` run during a stop fails. Steps 5.1 to 5.5 of 6.4 become one function used by `work:cs`, `work:cs:restart` and the keeper. Only the keeper adds the ledger and the budget. The owner-present start stays outside the budget, as today.

### 6.10 Codespaces on main (#47)

1. **Branch.**
   - `proposed_branch` (`git_ops.py:25`) always returns a new `setup/devcontainer[-N]`, cut from a freshly fetched `origin/<default>`. This overrides the setup-wizard design's "on a default branch, propose a setup branch".
   - The setup commit is made in a temporary worktree under the state dir. The owner's checkout, its branch and its uncommitted work are not touched (design call D8).
2. **PR.** Push the setup branch, then open a PR into the default branch with the reviewed summary as its body. The body has no AI attribution.
3. **Merge**, after one confirmation: "Merge PR #\<n\> into \<default\> as \<user\>, bypassing the required review?"
   - The setup merges only when the ruleset says the account may bypass (`current_user_can_bypass` is `always`), or when no review is required.
   - It merges through `PUT /repos/{owner}/{repo}/pulls/{n}/merge` with `merge_method=merge` and a verbatim commit title and message. That avoids gh's co-author stub.
   - Otherwise it stops: "PR #\<n\> is open. Merge it, then run task devcontainer:setup again to create the Codespace." A rerun resumes from the open or merged PR.
4. **Create** the Codespace on the default branch only after the merge, and verify the remote head contains the setup commit.
5. **Receipts and reuse** are keyed on the repository and the default branch (`codespaces.py:95-99, 199-202, 230, 251-253`; `__main__.py:665`).
   - An existing receipt for a retired setup branch whose Codespace now reports ref `main` is rewritten. The old receipt is kept beside it.
   - A Codespace on any other branch is refused with what to do: "Move \<codespace\> to main first." The setup never silently creates a second Codespace.
6. **The owner's checkout.** After the merge, the setup prints that the checkout still lacks the setup files until the owner merges or pulls the default branch. It never switches the owner's branch.

## 7. Adopting the existing projects

- **innovate first, as the test bed.** It is already on main.
  1. `task devcontainer:upgrade:self`.
  2. `task work:cs:armed`.
  3. Run every slice's DoD on it (section 9).
  4. Its innovate receipt names `setup/devcontainer`; that is the case in 6.10 step 5.
- **mainstage, only on the owner's go-ahead, at a moment the owner picks** (rulings 7 and 8). The order is fixed:
  1. The lead's `docs/model-resilience-design` work merges.
  2. Move the Codespace and the Mac checkout to main, as was done for innovate.
  3. `task devcontainer:upgrade:self`.
  4. The owner stops `cs:ports` and `mirror.sh` (the brief's standing rule leaves them running until then). Run `task work:cs:armed` right away, because arming refuses while `cs:ports` holds the link (6.1 step 2).
  5. Verify 13570-13572 and 18787 listen.

  Mainstage's sync worker stays stopped until #45 is fixed. The keeper does not need it.

## 8. Constraints

- **Python standard library only,** as the rest of `project_sync`. Tests are offline: the `upstream=` seam of `ReadOnlyAPI`, fake gh and herdr executables on PATH, and no network. The existing real-gh boundary test (`tests/test_sync_transport.py`) is extended to prove the K4 child cannot POST.
- **gh stays pinned at 2.102.0** (`transport.py`, `SUPPORTED_GH`).
- **Exactly one code path can POST start in the background:** the keeper's start routine.
  - The worker, the K4 child and the Herdr proxy stay GET-only.
  - The keeper's POST goes over direct HTTPS, never through gh.
  - The README and `docs/project-continuity.md` statement "the detached worker … cannot restart a Codespace" is rewritten in the same change to say which process can, and when.
- **Secrets.**
  - Tokens are read per call and never written to disk or logs.
  - Logs and ledger lines carry status codes and times, never bodies.
  - The consent is stored as text sha256 and time.
- **Taskfile rules** (owner's global standards):
  - long-running processes start detached and log to files;
  - `stop` works by fact;
  - `up` and arm are idempotent, checked by observation.
- **Times** print in both -03:00 and UTC.
- **Repo conventions:** `AGENTS.md` style, commits in imperative sentence case, no AI attribution.

## 9. Build order and Definition of Done

Each slice:
- has its gates re-run by the lead in the default environment, logged to `verify/<name>-<sha>.log`;
- shows each new test failing with its feature removed;
- leaves mainstage's Codespace, worker state and Herdr machine untouched (`gh api user/codespaces/<mainstage>` state and `last_used_at` unchanged by the slice).

A and B are independent of the rest and can go first and in parallel. D needs C. E and F need D.

- **A. Proxy liveness (#46).**
  - Offline: a fake gh that never exits plus a fake upstream returning Shutdown three times → the proxy exits within about 90 s and reaps gh. One failed read → it keeps running.
  - On innovate: after the next stop, no innovate `herdr_stdio` process is older than the stop when the Codespace is back.
- **B. Codespaces on main (#47).**
  - Offline: fixture repos on a feature branch and on main → the setup branch is cut from `origin/main`, the owner's checkout and branch are untouched, the merge goes through REST with a verbatim message (no `Co-authored-by`), the Codespace is created with `--branch main`, and the receipt is migrated for a retired setup branch.
  - Live: one new project set up end to end, on the owner's choice of repo.
- **C. Notifications, #48, and the shared start routine (6.9).**
  - Offline: table tests for the start routine from the measured sequences: 409 during ShuttingDown, ignored POST, Queued, Available.
  - The worker reports Shutdown, not an auth failure. One notification per stop.
  - Measured: osascript shows from a detached process started in a Herdr pane.
- **D. Keeper core.**
  - Contents: arming, consent, `gh_user`, the keeper launch, adoption and stop by fact, the GET loop, the anchor, classification, the gate, the ledger, `caffeinate`, status, logs, hold, disarm and `work:cs:stop`. The POST is switched off: a LIFETIME stop notifies "would restart".
  - DoD on innovate:
    - two arms leave the same keeper pid, nonce and K4 pid, and no new ledger rows;
    - `kill -9` of the keeper produces the worker's notification within 6 min;
    - `work:disarm` leaves no keeper and no K4 child;
    - the classification table tests cover the three measured stops as LIFETIME, and a stop at anchor + 6 h as OTHER.
- **E. K4 child with ports (#36).**
  - Measure first, with the owner's OK (it costs credits): a throwaway Codespace with a 5-min idle timeout, held only by the exact 6.3 argv with 2 forwards, stays Available for at least 30 min. If it does not, stop and bring it back to the owner.
  - DoD: ports listen at N+10000 and come back after a reconnect; a port in use is skipped and shown; the real-gh boundary test shows the child cannot POST.
- **F. Recovery on.**
  - Contents: the POST, restore, the defensive proxy step, resume, and `work:cs:restart`.
  - DoD on innovate:
    1. a `work:cs:restart` ends with the Codespace back, Herdr reachable and the rid in a test lead's screen, with no keystroke after the command;
    2. a `work:cs:stop` is never restarted;
    3. a crash injected between the ledger `intent` line and the POST causes no second POST after the keeper restarts;
    4. **one real 12-hour stop on innovate is recovered with no one at the keyboard**: the ledger shows one intent and one result, status shows the rid, and the lead's transcript has the rid.

    Only after item 4 is mainstage offered for arming.

**The work is done when** step 4 of slice F has run green on innovate, and mainstage has been armed on the owner's go-ahead and has come back from one 12-hour stop by itself.

## 10. Still unknown, and how each is settled

| Unknown | Settled by |
|---|---|
| Whether the K4 child with `-L` forwards and `ServerAliveInterval=30` holds an idle Codespace like the measured K4 | Slice E measurement |
| Whether ending the proxy closes the Herdr master as quickly as `ssh -O exit` | Slice F, on innovate |
| Why the stale master ignored `ServerAliveInterval 15` / `CountMax 4` | Not needed once A lands. Recorded in #46. |
| osascript from a detached process outside launchd | Slice C |
| Whether a Codex worker comes back with its sandbox, model and effort | Likely not (upstream #965). The resume text warns the lead. Checked on the first restart with a Codex worker. |
| The keychain token read with the screen locked | Not needed: the keeper is not a LaunchAgent (D1). It becomes relevant if D1 changes. |
| Whether a live client stretches ShuttingDown | Covered: step 5.1 waits up to 20 min for Shutdown |

## 11. Design calls for the owner to check

- **D1 Process host.**
  - The keeper is a per-project process, detached like today's worker. It survives the pane and the session closing, but not a Mac reboot or logout. After one, run `task work:cs:armed` again.
  - A LaunchAgent would survive a reboot, but it brings the installed runtime, launchd's own failure modes and the screen-locked keychain question.
  - With FileVault and no auto-login, nothing runs before you log in anyway.
- **D2 Anchor.** The time of the keeper's own accepted start request when it made the start, else the observed start, else `last_used_at` at arm time. A wrong anchor makes a stop `OTHER`, which is not restarted.
- **D3 `OTHER` stops are never restarted.** That covers idle stops, GitHub UI stops, resizes and anything off the 12-hour window. The keeper cannot tell your own stop from an idle one, so it notifies instead.
- **D4 The lead is found by name.** Exactly one agent named `lead-*` on the project's machine. Anything else means notify and send nothing.
- **D5 Hold.** A stop that happens during a hold is not restarted when the hold ends. You get a notification at the stop.
- **D6 `work:cs:restart`.** Added for planned restarts like tonight's, outside the budget, with resume. It can be cut without affecting the rest.
- **D7 The worker watches the keeper.** If an armed project's keeper dies, the worker notifies once.
- **D8 The setup commit in a temporary worktree.** Your checkout and branch are never switched. After the merge, your checkout needs a pull or a merge of main to get the setup files.
- **D9 Recovery ships with the POST on after slice F's DoD.** The clock is settled on 3 of 3 Codespaces, so there is no notify-only night. Slice D's "would restart" notifications are the dry run.

## 12. Left to the builder

- Module layout (for example `project_sync/keeper.py` and `project_sync/lifecycle.py`), function names and the JSON schema details.
- Wording of every notification and status line, within the content above.
- Log rotation sizes and backoff constants, within the bounds above.
- How the CLI waits for the keeper's first heartbeat.
