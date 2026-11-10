"""Record OLD's final normal flush, then finish its move to NEW without a TUI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from . import arming, deletion, lifecycle, move, overview, replacing, scratch, seal, standing, target_scan, transport, verdict, workspace
from .checkpoint_store import Store
from .state import atomic_write, read_json


def _binding(state):
    try:
        return read_json(state / scratch.CONFIG)
    except FileNotFoundError:
        return {}


def run(project, state, *, operation, old, new, receipt, recovery, ask=input, tty=None,
        api_factory=lifecycle.LifecycleAPI, connect=transport.CodespaceConnection,
        now=time.time, sleep=time.sleep) -> int:
    """Only the coordinator writes its journal; every accepted step leaves a receipt."""
    state = Path(state)
    terminal = tty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())
    if os.environ.get('CODESPACES') == 'true' or not terminal():
        print('Replacing a Codespace needs a terminal on your Mac.')
        return 1
    for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if key in os.environ:
            print(f'Unset {key} before replacing a Codespace; it must use a real gh account.')
            return 1
    try:
        journal = replacing.read(state)
        matches = (journal is not None and journal['ended'] is None and journal['operation'] == operation
                   and journal['old'] == old and journal['new'] == new
                   and replacing.PHASES.index(journal['phase']) >= replacing.PHASES.index('ready')
                   and journal['repository'].casefold() == project.identity.removeprefix('github.com/').casefold()
                   and replacing.ended_by_fact(state, journal) is None)
    except (OSError, ValueError):
        matches = False
    if not matches:
        print('This replace record does not match; run the replace again.')
        return 1

    record, herdr = None, None

    def moved():
        try:
            binding = _binding(state)
        except (OSError, ValueError):
            return False
        return binding.get('codespace') == new and binding.get('identity') == project.identity

    def remote(**options):
        nonlocal herdr
        herdr = None
        try:
            result = workspace.remote(project, state, enter=False, **options)
            if moved():
                herdr = result
        except (OSError, ValueError):
            if moved():
                herdr = 1
            raise

    def moved_line():
        if herdr:
            print(f'Moved sync to {new}; Herdr could not connect it. The replace checks it again.')
        elif arming.read(state).get('armed'):
            print(f'Moved sync, recovery and Herdr to {new}.')
        else:
            print(f'Moved sync and Herdr to {new}.')

    def flush_lines(target, categories):
        print(f"{old}'s final flush is not complete; nothing moved:")
        observed = verdict.Verdict(target, now(), journal['account'], journal['repository'],
                                   categories, (), None, None)
        local = standing.Local(project.root, repository=journal['repository'])
        for line in verdict.lines(observed, local):
            if line.startswith(tuple(f'  {name:<8} ' for name in ('Scratch', 'History', 'Agents'))) or line.startswith('           '):
                print(line)
        print('Fix the lines above, then run the replace again.')

    try:
        record = seal.read(state, old)
        if record is not None and record.operation != operation:
            record = None
        armed = recovery if arming.read(state).get('armed') else None
        if moved() or replacing.move_started(state, journal):
            move.finish_pending(project, state)
            armed = recovery if arming.read(state).get('armed') else None
            if moved():
                remote(armed=armed)
            elif _binding(state).get('codespace') == old:
                decision = move.decide(project, state, new, ask=ask)
                remote(rebind=new, move=decision, armed=recovery if decision is not None else armed, expected_old=old)
            else:
                raise ValueError(f'{old} is no longer the paired Codespace; nothing moved.')
            if moved():
                moved_line()
                return 0
            return 1

        binding = _binding(state)
        if binding.get('codespace') != old or binding.get('identity') != project.identity:
            raise ValueError(f'{old} is no longer the paired Codespace; nothing moved.')
        account = arming.account(os.environ)
        if account != journal['account']:
            raise ValueError(f'This replace was started as {journal["account"]}, but gh is signed in as {account}. '
                             'Switch with gh auth switch, then run the replace again.')
        reading = api_factory(old, gh_user=account).read()
        if not reading.ok or reading.state != 'Available':
            print(f'{old} is {reading.state or "unknown"}. Start it with task work:cs:restart, then run the replace again.')
            return 1
        if not workspace._current_worker(scratch.status(state)):
            move.finish_pending(project, state)
            remote(armed=armed, expected_old=old, start=False)
            deadline = now() + 120
            while not workspace._current_worker(scratch.status(state)):
                if now() >= deadline:
                    print(f'Replace stopped at flush and move: sync with {old} did not start.')
                    return 1
                sleep(min(2, deadline - now()))
        decision = move.decide(project, state, new, ask=ask)
        target = verdict.resolve(project, state, old)
        if target.kind != 'paired':
            raise ValueError(f'{old} is no longer the paired Codespace; nothing moved.')
        with connect(old, state=state) as connection:
            while True:
                scan = target_scan.scan(connection, old, target.remote_root, now=now)
                if not scan.agents_complete:
                    print(f'  Agents   Unknown: could not read processes on {old}')
                    return 1
                counts = dict(scan.agents)
                agents = [f'{agent} ({counts[agent]})' for agent in ('claude', 'codex', 'grok', 'opencode')
                          if counts.get(agent, 0) > 0]
                if not agents:
                    break
                print(f'On {old}: {", ".join(agents)} running. End them, then press Enter to check again, or type stop.')
                if ask('> ').strip() == 'stop':
                    print('Nothing moved.')
                    return 1
            evidence = deletion.gather(project, state, target, connection, now=now, sleep=sleep)
            categories = verdict.judge(target, evidence, now=now())
            required = tuple(row for row in categories if row.name in ('Scratch', 'History', 'Agents'))
            if not evidence.steady or len(required) != 3 or any(row.verdict != verdict.SAFE for row in required):
                flush_lines(target, required)
                return 1
            sealed_at = now()
            seal.write(state, Store(state), evidence.scan, evidence=evidence, identity=project.identity,
                       repository=journal['repository'], gh_user=journal['account'], remote_root=target.remote_root,
                       operation=operation, cycle_id=evidence.cycle['cycle_id'],
                       snapshot_id=evidence.receipt['snapshot_id'], now=sealed_at)
            print(f'Final flush of {old} recorded at {overview._when(sealed_at, sealed_at)} '
                  f'({len(evidence.scan.scratch)} files, '
                  f'{len({(agent, session) for agent, _, session in evidence.sessions_by_file})} conversations).')
            remote(rebind=new, move=decision, armed=recovery if decision is not None else None, expected_old=old)
        if moved():
            moved_line()
            return 0
        return 1
    except (EOFError, KeyboardInterrupt):
        if moved():
            raise
        print('Nothing moved.')
        return 1
    except (OSError, ValueError) as error:
        print(str(error))
        if moved():
            moved_line()
            return 0
        print('Nothing moved.')
        return 1
    finally:
        # Re-read the seal even when remote raised after publishing its binding.
        try:
            record = seal.read(state, old)
        except (OSError, ValueError):
            record = None
        if record is not None and record.operation != operation:
            record = None
        value = dict(schema=1, operation=operation, step='cutover', old=old, new=new, moved=moved(),
                     sealed_at=record.sealed_at if record else None, cycle_id=record.cycle_id if record else None,
                     snapshot_id=record.snapshot_id if record else None, herdr=herdr, at=now())
        atomic_write(Path(receipt), json.dumps(value, sort_keys=True).encode() + b'\n')
