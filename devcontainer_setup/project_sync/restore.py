"""Restore after a restart: stale Herdr proxies, the project's Herdr machine, its lead and the resume prompt.

Every Herdr call is bounded and reads a failure as "not yet": False, [] or ''.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

SENTENCE = ('Codespace restarted, we are back. You need to bring the services that you require up and '
            'continue with the work')
READY = ('idle', 'done')  # Herdr's states for an agent ready for input
SETTLE = 10
IDLE_WAIT = 10 * 60
PROOF_WAIT = 2 * 60
REACH_WAIT = 10 * 60
POLL = 10
_AGENT = re.compile(r'[a-z][a-z0-9_-]{0,31}')


def is_proxy(command, codespace: str, state) -> bool:
    """True for this project's Herdr SSH proxy: herdr_stdio whose argv ends with this Codespace and state dir."""
    return (isinstance(command, str) and 'herdr_stdio' in command
            and command.rstrip().endswith(f' {codespace} {state}'))


def _elapsed(text):
    # ps etime: [[dd-]hh:]mm:ss, in seconds; None for anything else.
    days, dash, clock = text.rpartition('-')
    parts = clock.split(':')
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts) or (dash and not days.isdigit()):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return int(days or 0) * 86400 + seconds


def stale_proxies(codespace: str, state: Path, before: float, *, run=subprocess.run, now=time.time) -> list[int]:
    """Pids of this project's Herdr proxies started before `before`; [] when ps cannot be read."""
    try:
        # Elapsed time, not lstart: no local-time parse, so a DST fall-back hour cannot misdate one (R35).
        listing = run(['ps', '-axo', 'pid=,etime=,command='], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if listing.returncode:
        return []
    found, at = [], now()
    for line in listing.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        elapsed = _elapsed(fields[1])
        if elapsed is not None and at - elapsed < before and is_proxy(fields[2], codespace, state):
            found.append(int(fields[0]))
    return found


def proxies_elsewhere(codespace: str, state: Path, *, run=subprocess.run) -> list[int] | None:
    """Pids of this project's Herdr proxies serving a Codespace other than `codespace`; None when ps cannot be read."""
    try:
        listing = run(['ps', '-axo', 'pid=,etime=,command='], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listing.returncode:
        return None
    suffix, found = f' {state}', []
    for line in listing.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        command = fields[2].rstrip()
        if 'herdr_stdio' in command and command.endswith(suffix):
            served = command[:-len(suffix)].rsplit(' ', 1)[-1]
            if served != codespace:
                found.append(int(fields[0]))
    return found


def end_proxies_elsewhere(codespace: str, state: Path, *, run=subprocess.run) -> list[int] | None:
    """SIGTERM this project's Herdr proxies serving another Codespace; the pids signalled, None when ps cannot be read.

    Transitional (#51): a proxy from 66719dc on leaves by itself once the binding names another
    Codespace. One an older runner started does not, and keeps Herdr on the Codespace the project left.
    Remove once no project runs a runner older than 66719dc.
    """
    pids = proxies_elsewhere(codespace, state, run=run)
    if pids is None:
        return None
    ended = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        ended.append(pid)
    return ended


def end_stale_proxies(codespace: str, state: Path, before: float, *, run=subprocess.run) -> list[int]:
    """SIGTERM this project's Herdr proxies that outlived the stop; the pids signalled.

    Defensive (#46): herdr_stdio already ends its gh once the Codespace reads down, which is the
    origin fix. This clears one that survived anyway, so Herdr reconnects. Only a process whose argv
    proves it is this package's proxy for this Codespace and state is signalled, never Herdr's own.
    """
    ended = []
    for pid in stale_proxies(codespace, state, before, run=run):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        ended.append(pid)
    return ended


def _named(name):
    return isinstance(name, str) and bool(_AGENT.fullmatch(name))


class Herdr:
    """The project's saved Herdr machine, through the herdr CLI."""

    def __init__(self, machine_id: str, *, run=subprocess.run, timeout: float = 60):
        if (not isinstance(machine_id, str) or not machine_id or machine_id.startswith('-')
                or any(ord(c) < 33 for c in machine_id)):
            raise ValueError('herdr-machine.json names no usable Herdr machine; run task work:cs to reconnect Herdr')
        self.machine, self.run, self.timeout = machine_id, run, timeout

    def _call(self, *args):
        # Never a pane's HERDR_* variables: they would aim the command at that pane's session.
        env = {key: value for key, value in os.environ.items() if not key.startswith('HERDR_')}
        try:
            result = self.run(['herdr', *args], capture_output=True, text=True, timeout=self.timeout, env=env,
                              stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    def _json(self, *args):
        out = self._call(*args)
        try:
            return json.loads(out) if out is not None else None
        except ValueError:
            return None

    def reachable(self) -> bool:
        rows = self._json('machine', 'status', self.machine, '--json')
        # Anything else is "not yet": right after Available it read 'error' (remote server stopped
        # or incompatible) and turned reachable 28 s later by itself. Its suggested fix attaches a TUI.
        return (isinstance(rows, list) and bool(rows) and isinstance(rows[0], dict)
                and rows[0].get('status') == 'reachable')

    def agents(self) -> list[dict]:
        reply = self._json('--machine', self.machine, 'agent', 'list')
        try:
            agents = reply['result']['agents']
        except (KeyError, TypeError):
            return []
        return [agent for agent in agents if isinstance(agent, dict)] if isinstance(agents, list) else []

    def prompt(self, name: str, text: str) -> bool:
        # Never --wait: a restored lead reads idle before its UI is up, and the wait then
        # times out although the prompt arrived (measured). resume() proves delivery itself.
        if not _named(name) or '\0' in text:
            return False
        return self._call('--machine', self.machine, 'agent', 'prompt', name, text) is not None

    def read(self, name: str, lines: int = 80) -> str:
        if not _named(name):
            return ''
        return self._call('--machine', self.machine, 'agent', 'read', name, '--source', 'recent-unwrapped',
                          '--lines', str(lines)) or ''


class _Ticked:
    """A Herdr whose every call is followed by tick(): each one may take its whole timeout."""

    def __init__(self, herdr, tick):
        self.herdr, self.tick = herdr, tick

    def _after(self, value):
        self.tick()
        return value

    def reachable(self):
        return self._after(self.herdr.reachable())

    def agents(self):
        return self._after(self.herdr.agents())

    def prompt(self, name, text):
        return self._after(self.herdr.prompt(name, text))

    def read(self, name, lines=80):
        return self._after(self.herdr.read(name, lines))


def _ticked(herdr, tick):
    return _Ticked(herdr, tick) if tick else herdr


def wait_reachable(herdr: Herdr, *, now=time.time, sleep=time.sleep, tick=None) -> float | None:
    """When the machine first reads reachable, polling every POLL s for REACH_WAIT; else None.

    `tick`, when given, runs after each Herdr call, as the keeper's heartbeat.
    """
    herdr, deadline = _ticked(herdr, tick), now() + REACH_WAIT
    while True:
        if herdr.reachable():
            return now()
        if now() >= deadline:
            return None
        sleep(min(POLL, max(0, deadline - now())))


def lead(agents: list[dict]) -> dict | None:
    """The one live agent named lead-*; None for none or several (D4). Unnamed agents never count."""
    found = [agent for agent in agents if isinstance(agent.get('name'), str) and agent['name'].startswith('lead-')]
    return found[0] if len(found) == 1 else None


def _hhmmz(when):
    return time.strftime('%H:%MZ', time.gmtime(when))


def resume_text(rid: str, stopped_at: float, back_at: float) -> str:
    """The owner's sentence verbatim, then one line of facts (spec 6.6); an o- rid is the owner's restart."""
    if rid.startswith('o-'):
        what = f'Planned restart {rid}: stopped at {_hhmmz(stopped_at)}'
    else:
        what = f"Automatic restart {rid}: GitHub's 12-hour stop at {_hhmmz(stopped_at)}"
    return (f'{SENTENCE}. ({what}, back at {_hhmmz(back_at)}. Restored agents came back with --resume and '
            "without their launch flags, so check each worker's model, effort and sandbox before dispatching "
            'to it.)')


def _status(herdr, name):
    for agent in herdr.agents():
        if agent.get('name') == name:
            return agent.get('agent_status')
    return None


def _settled(herdr, name, now, sleep):
    # Ready on two reads SETTLE s apart, within IDLE_WAIT.
    deadline, ready = now() + IDLE_WAIT, False
    while True:
        if _status(herdr, name) in READY:
            if ready:
                return True
            ready = True
        else:
            ready = False
        if now() >= deadline:
            return False
        sleep(SETTLE)


def _proven(herdr, name, rid, now, sleep):
    # The rid alone (the lead may shorten the rest), or the lead at work, within PROOF_WAIT.
    deadline = now() + PROOF_WAIT
    while True:
        sleep(min(POLL, max(0, deadline - now())))
        if rid in herdr.read(name) or _status(herdr, name) == 'working':
            return True
        if now() >= deadline:
            return False


def resume(herdr: Herdr, rid: str, text: str, *, now=time.time, sleep=time.sleep,
           tick=None) -> tuple[str, str | None]:
    """Spec 6.6: ('delivered' | 'resent' | 'no-lead' | 'busy' | 'undelivered', the lead's name or None).

    Never logs the screen. `tick`, when given, runs after each Herdr call, as the keeper's heartbeat.
    """
    herdr = _ticked(herdr, tick)
    found = lead(herdr.agents())
    if found is None:
        return 'no-lead', None
    name = found['name']
    if not _settled(herdr, name, now, sleep):
        return 'busy', name
    herdr.prompt(name, text)
    if _proven(herdr, name, rid, now, sleep):
        return 'delivered', name
    if _status(herdr, name) not in READY:
        return 'undelivered', name
    herdr.prompt(name, text)
    return ('resent' if _proven(herdr, name, rid, now, sleep) else 'undelivered'), name
