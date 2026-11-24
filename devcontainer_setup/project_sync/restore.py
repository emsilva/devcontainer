"""Restore after a restart: stale Herdr proxies, the project's Herdr machine, its lead and the resume prompt.

Every Herdr call is bounded. Prompts preserve the error kind; other calls read a failure as
"not yet": False, [] or ''. The process listing for a move returns None when ps cannot be read,
so work:cs can say so.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from . import ssh_hosts, state as project_state

SENTENCE = ('Codespace restarted, we are back. You need to bring the services that you require up and '
            'continue with the work')
READY = ('idle', 'done')  # Herdr's states for an agent ready for input
SETTLE = 10
IDLE_WAIT = 10 * 60
REACH_WAIT = 10 * 60
POLL = 10
_AGENT = re.compile(r'[a-z][a-z0-9_-]{0,31}')
_PANE = re.compile(r'w[0-9]{1,8}:p[0-9]{1,8}')


def _served(command, state):
    """The Codespace a herdr_stdio command line serves for this state dir, or None for any other process."""
    if not isinstance(command, str):
        return None
    # ps drops shell quoting. The bootstrap contains a Python string literal
    # naming its package copy; another checkout must recognize that copy too.
    literal = r'''(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")'''
    prefix = (re.escape(' -I -B -c import sys;sys.path.insert(0,') + literal
              + re.escape(');from ' + ssh_hosts.__package__ + '.transport import herdr_stdio;'
                          'raise SystemExit(herdr_stdio(sys.argv[1],sys.argv[2])) '))
    match = re.search(prefix + r'([A-Za-z0-9][A-Za-z0-9-]{0,99}) '
                      + re.escape(str(state)) + r'\Z', command)
    return match[1] if match else None


def is_proxy(command, codespace: str, state) -> bool:
    """True for this project's Herdr SSH proxy: herdr_stdio whose argv ends with this Codespace and state dir."""
    return _served(command, state) == codespace


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


def _proxies(state: Path, run):
    """(pid, seconds running or None, Codespace served) for this project's Herdr proxies; None when ps cannot be read."""
    try:
        # Elapsed time, not lstart: no local-time parse, so a DST fall-back hour cannot misdate one (R35).
        listing = run(['ps', '-axo', 'pid=,etime=,command='], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listing.returncode:
        return None
    found = []
    for line in listing.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit():
            served = _served(fields[2], state)
            if served is not None:
                found.append((int(fields[0]), _elapsed(fields[1]), served))
    return found


def _terminate(pids):
    ended = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        ended.append(pid)
    return ended


def end_target_proxies(codespace: str, state: Path, *, wait: float = 120, run=subprocess.run,
                       now=time.time, sleep=time.sleep) -> str:
    """Signal this target's owned proxies and wait for their disappearance."""
    found = _proxies(state, run)
    if found is None:
        raise ValueError('the process list cannot be read')
    pids = [pid for pid, _, served in found if served == codespace]
    if not pids:
        return 'absent'
    identities = {pid: project_state.process_identity(pid) for pid in pids}
    for pid, identity in identities.items():
        if (_served(identity, state) == codespace
                and project_state.process_identity(pid) == identity):
            _terminate([pid])
    deadline = now() + wait
    while True:
        found = _proxies(state, run)
        if found is None:
            raise ValueError('the process list cannot be read')
        if not any(served == codespace for _, _, served in found):
            return 'ended'
        if now() >= deadline:
            raise ValueError('a Herdr connection to ' + codespace + ' is still open')
        sleep(min(2, max(0, deadline - now())))


def stale_proxies(codespace: str, state: Path, before: float, *, run=subprocess.run, now=time.time) -> list[int]:
    """Pids of this project's Herdr proxies started before `before`; [] when ps cannot be read."""
    found = _proxies(state, run) or []
    at = now()  # After ps, which reports each age as it runs.
    return [pid for pid, elapsed, served in found
            if served == codespace and elapsed is not None and at - elapsed < before]


def proxies_elsewhere(codespace: str, state: Path, *, run=subprocess.run) -> list[int] | None:
    """Pids of this project's Herdr proxies serving a Codespace other than `codespace`; None when ps cannot be read."""
    found = _proxies(state, run)
    return None if found is None else [pid for pid, _, served in found if served != codespace]


def end_proxies_elsewhere(codespace: str, state: Path, *, run=subprocess.run) -> list[int] | None:
    """SIGTERM this project's Herdr proxies serving another Codespace; the pids signalled, None when ps cannot be read.

    Transitional (#51): a proxy from 66719dc on leaves by itself once the binding names another
    Codespace. One an older runner started does not, and keeps Herdr on the Codespace the project left.
    Remove once no project runs a runner older than 66719dc.
    """
    pids = proxies_elsewhere(codespace, state, run=run)
    return None if pids is None else _terminate(pids)


def end_stale_proxies(codespace: str, state: Path, before: float, *, run=subprocess.run) -> list[int]:
    """SIGTERM this project's Herdr proxies that outlived the stop; the pids signalled.

    Defensive (#46): herdr_stdio already ends its gh once the Codespace reads down, which is the
    origin fix. This clears one that survived anyway, so Herdr reconnects. Only a process whose argv
    proves it is this package's proxy for this Codespace and state is signalled, never Herdr's own.
    """
    return _terminate(stale_proxies(codespace, state, before, run=run))


def _named(name):
    return isinstance(name, str) and bool(_AGENT.fullmatch(name) or _PANE.fullmatch(name))


class Herdr:
    """The project's saved Herdr machine, through the herdr CLI."""

    def __init__(self, machine_id: str, *, run=subprocess.run, timeout: float = 60):
        if (not isinstance(machine_id, str) or not machine_id or machine_id.startswith('-')
                or any(ord(c) < 33 for c in machine_id)):
            raise ValueError('herdr-machine.json names no usable Herdr machine; run task work:cs to reconnect Herdr')
        self.machine, self.run, self.timeout = machine_id, run, timeout

    def _call_result(self, *args):
        """(stdout, None) on success, (None, error code) on failure."""
        # Never a pane's HERDR_* variables: they would aim the command at that pane's session.
        env = {key: value for key, value in os.environ.items() if not key.startswith('HERDR_')}
        try:
            result = self.run(['herdr', *args], capture_output=True, text=True, timeout=self.timeout, env=env,
                              stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None, 'unavailable'
        if result.returncode == 0:
            return result.stdout, None
        try:
            code = json.loads(result.stderr)['error']['code']
        except (ValueError, KeyError, TypeError):
            code = None
        return None, code if isinstance(code, str) and code else 'unknown'

    def _call(self, *args):
        out, _ = self._call_result(*args)
        return out

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

    def prompt(self, name: str, text: str) -> str:
        # Text can arrive in the input box without submission; wait for working or blocked.
        if not _named(name) or '\0' in text:
            return 'refused'
        _, error = self._call_result('--machine', self.machine, 'agent', 'prompt', name, text, '--wait',
                                     '--until', 'working', '--until', 'blocked', '--timeout', '30000')
        return error or 'started'

    def enter(self, name: str) -> bool:
        if not _named(name):
            return False
        return self._call('--machine', self.machine, 'agent', 'send-keys', name, 'enter') is not None

    def wait_active(self, name: str) -> bool:
        if not _named(name):
            return False
        return self._call('--machine', self.machine, 'agent', 'wait', name, '--until', 'working',
                          '--until', 'blocked', '--timeout', '30000') is not None

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

    def enter(self, name):
        return self._after(self.herdr.enter(name))

    def wait_active(self, name):
        return self._after(self.herdr.wait_active(name))

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


def leads(agents: list[dict]) -> list[dict]:
    """All live agents named lead-*, sorted by name. Unnamed agents never count."""
    seen: dict[str, dict] = {}
    for agent in agents:
        name = agent.get('name')
        if isinstance(name, str) and name.startswith('lead-') and _AGENT.fullmatch(name) and name not in seen:
            seen[name] = agent
    return [seen[name] for name in sorted(seen)]


def lead(agents: list[dict]) -> dict | None:
    """The one live agent named lead-*; None for none or several. Unnamed agents never count."""
    found = leads(agents)
    return found[0] if len(found) == 1 else None


def targets(agents: list[dict]) -> list[str]:
    """Target IDs to resume after a restart.

    If one or more lead-* agents exist, resume only those leads (they manage their
    own workers). Otherwise fall back to existing live non-worker agent panes
    (by valid agent name or Herdr pane_id such as w1:p1).
    """
    lead_agents = leads(agents)
    if lead_agents:
        return [agent['name'] for agent in lead_agents]
    fallback: list[str] = []
    seen: set[str] = set()
    for agent in agents:
        name = agent.get('name')
        if isinstance(name, str) and name.startswith('worker-'):
            continue
        ident = None
        if isinstance(name, str) and _AGENT.fullmatch(name):
            ident = name
        else:
            pane_id = agent.get('pane_id')
            if isinstance(pane_id, str) and _PANE.fullmatch(pane_id):
                ident = pane_id
        if ident and ident not in seen:
            seen.add(ident)
            fallback.append(ident)
    return sorted(fallback)


def _hhmmz(when):
    return time.strftime('%H:%MZ', time.gmtime(when))


def _hint_parts(hint: dict) -> list[str]:
    parts = []
    conv = hint.get('root_conversation_id')
    if isinstance(conv, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', conv):
        parts.append(f'top-level conversation {conv}')
    services = hint.get('services')
    if isinstance(services, list):
        clean = [str(s) for s in services[:12] if isinstance(s, (str, int)) and '\n' not in str(s) and '\0' not in str(s)]
        if clean:
            parts.append('check services ' + ', '.join(clean))
    return parts


def resume_text(rid: str, stopped_at: float, back_at: float, *, root: Path | None = None,
                lead_name: str | None = None) -> str:
    """The owner's sentence verbatim, then one line of facts (spec 6.6); an o- rid is the owner's restart."""
    if rid.startswith('o-'):
        what = f'Planned restart {rid}: stopped at {_hhmmz(stopped_at)}'
    else:
        what = f"Automatic restart {rid}: GitHub's 12-hour stop at {_hhmmz(stopped_at)}"
    extra = ''
    if root is not None:
        hint_path = Path(root) / '.scratch' / 'restart-resume.json'
        try:
            if hint_path.is_file() and not hint_path.is_symlink():
                hint = json.loads(hint_path.read_text(encoding='utf-8'))
                if isinstance(hint, dict):
                    selected = hint
                    per_lead = hint.get('leads')
                    if lead_name and isinstance(per_lead, dict) and isinstance(per_lead.get(lead_name), dict):
                        merged = dict(hint)
                        merged.update(per_lead[lead_name])
                        selected = merged
                    parts = _hint_parts(selected)
                    if parts:
                        extra = ' Recovery hints from .scratch/restart-resume.json: ' + '; '.join(parts) + '.'
        except (OSError, ValueError, TypeError):
            pass
    return (f'{SENTENCE}. ({what}, back at {_hhmmz(back_at)}. Restored agents came back with --resume / '
            "--conversation and without their launch flags, so check each worker's model, effort and sandbox "
            f'before dispatching to it.{extra})')


def _status(herdr, target):
    for agent in herdr.agents():
        if agent.get('name') == target or agent.get('pane_id') == target:
            return agent.get('agent_status')
    return None


def _deliver_one(herdr, name: str, rid: str, text: str) -> str:
    if herdr.prompt(name, text) == 'started':
        return 'blocked' if _status(herdr, name) == 'blocked' else 'delivered'
    status = _status(herdr, name)
    if status == 'working':
        return 'delivered'
    if status == 'blocked':
        return 'blocked'
    if status not in READY:
        return 'undelivered'
    # The screen chooses a remedy only: the rid can still be in the unsent input box.
    if rid in herdr.read(name):
        if herdr.enter(name) and herdr.wait_active(name):
            return 'blocked' if _status(herdr, name) == 'blocked' else 'entered'
        return 'undelivered'
    if herdr.prompt(name, text) == 'started':
        return 'blocked' if _status(herdr, name) == 'blocked' else 'resent'
    return 'undelivered'


def resume_all(herdr: Herdr, rid: str, text_for, *, now=time.time, sleep=time.sleep,
               tick=None) -> list[tuple[str, str]]:
    """Wait for every target agent pane to settle and deliver its resume prompt.

    Targets all live lead-* agents when any exist, or falls back to existing live
    non-worker agent panes (named or by pane_id). `text_for` may be a string or a
    callable `(target_id) -> str`. Returns `(outcome, target_id)` pairs in sorted
    order (empty if no target exists). All targets share one IDLE_WAIT window so
    one busy pane never blocks ready sibling panes.
    """
    herdr = _ticked(herdr, tick)
    selected = targets(herdr.agents())
    if not selected:
        return []
    prompt_for = text_for if callable(text_for) else (lambda _name: text_for)
    deadline = now() + IDLE_WAIT
    ready_once = {name: False for name in selected}
    outcomes: dict[str, str] = {}
    while len(outcomes) < len(selected):
        snapshot: dict[str, dict] = {}
        for agent in herdr.agents():
            for key in ('name', 'pane_id'):
                val = agent.get(key)
                if isinstance(val, str) and val not in snapshot:
                    snapshot[val] = agent
        for name in selected:
            if name in outcomes:
                continue
            agent = snapshot.get(name, {})
            status = agent.get('agent_status')
            if status == 'blocked':
                outcomes[name] = 'blocked'
            elif status in READY and agent.get('interactive_ready') is not False:
                if ready_once[name]:
                    outcomes[name] = _deliver_one(herdr, name, rid, prompt_for(name))
                else:
                    ready_once[name] = True
            else:
                ready_once[name] = False
        if len(outcomes) >= len(selected):
            break
        if now() >= deadline:
            for name in selected:
                outcomes.setdefault(name, 'busy')
            break
        sleep(SETTLE)
    return [(outcomes[name], name) for name in selected]


_SUCCESS = ('delivered', 'entered', 'resent')


def resume(herdr: Herdr, rid: str, text, *, now=time.time, sleep=time.sleep,
           tick=None) -> tuple[str, str | None]:
    """Spec 6.6 (extended for parallel leads): resume all live lead-* agents.

    Returns (outcome, label). For 0 leads: ('no-lead', None). For 1 lead:
    (outcome, lead_name). For multiple leads: ('delivered'|'entered'|'resent'|
    'blocked'|'busy'|'undelivered'|'partial', ', '.join(names) or summary).
    """
    results = resume_all(herdr, rid, text, now=now, sleep=sleep, tick=tick)
    if not results:
        return 'no-lead', None
    if len(results) == 1:
        return results[0]
    names = ', '.join(name for _, name in results)
    kinds = [outcome for outcome, _ in results]
    if all(kind in _SUCCESS for kind in kinds):
        return (kinds[0] if len(set(kinds)) == 1 else 'delivered'), names
    if len(set(kinds)) == 1:
        return kinds[0], names
    detail = '; '.join(f'{name}: {outcome}' for outcome, name in results)
    return 'partial', detail
