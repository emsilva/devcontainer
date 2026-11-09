"""Project-wide outbound conversation exclusions, without deleting retained copies."""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import re
import time

from . import scratch, standing
from .state import atomic_write, locked, read_json

FILE = 'conversation-forget.json'
_UNREADABLE = FILE + ' cannot be read; move it aside to send every conversation again'
_AGENT = re.compile(r'[a-z][a-z0-9_-]{0,31}')
_SESSION = re.compile(r'[A-Za-z0-9._-]{1,128}')
_CODESPACE = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}')


class Unreadable(ValueError):
    def __init__(self):
        super().__init__(_UNREADABLE)


def _ids(agent, session_id):
    return (isinstance(agent, str) and _AGENT.fullmatch(agent)
            and isinstance(session_id, str) and _SESSION.fullmatch(session_id))


def _records(state):
    try:
        value = read_json(Path(state) / FILE)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise Unreadable() from None
    try:
        if (set(value) != {'schema', 'conversations'} or type(value['schema']) is not int
                or value['schema'] != 1 or not isinstance(value['conversations'], list)):
            raise ValueError()
        seen = set()
        for row in value['conversations']:
            if (not isinstance(row, dict)
                    or set(row) != {'agent', 'session_id', 'codespace', 'reason', 'forgotten_at'}
                    or not _ids(row['agent'], row['session_id'])
                    or not isinstance(row['codespace'], str) or not _CODESPACE.fullmatch(row['codespace'])
                    or not isinstance(row['reason'], str)
                    or type(row['forgotten_at']) not in (int, float)
                    or not math.isfinite(row['forgotten_at'])):
                raise ValueError()
            datetime.fromtimestamp(row['forgotten_at'])
            pair = row['agent'], row['session_id']
            if pair in seen:
                raise ValueError()
            seen.add(pair)
        return value['conversations']
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        raise Unreadable() from None


def forgotten(state: Path) -> frozenset[tuple[str, str]]:
    return frozenset((row['agent'], row['session_id']) for row in _records(state))


def _already(records, agent, session_id):
    for row in records:
        if (row['agent'], row['session_id']) == (agent, session_id):
            local = datetime.fromtimestamp(row['forgotten_at'])
            date = local.strftime('%H:%M' if local.date() == datetime.now().date() else '%Y-%m-%d %H:%M')
            return f'{agent} conversation {session_id} was already forgotten on {date}.'
    return None


def forget(project, state: Path, *, agent: str, session_id: str, now=time.time) -> str:
    if not _ids(agent, session_id):
        raise ValueError('Invalid conversation agent or session id')
    state = Path(state)
    already = _already(_records(state), agent, session_id)
    if already:
        return already
    with locked(state / 'workspace-control.lock', blocking=True):
        # Another forget may have completed while this writer waited for a connect.
        records = _records(state)
        already = _already(records, agent, session_id)
        if already:
            return already
        try:
            binding = read_json(state / scratch.CONFIG)
        except FileNotFoundError:
            raise ValueError('Pair a Codespace first: run task work:cs') from None
        codespace = binding.get('codespace')
        if not isinstance(codespace, str) or not _CODESPACE.fullmatch(codespace):
            raise ValueError('Pair a Codespace first: run task work:cs')
        refusal = next((row for row in standing.refusals(state, project, binding)
                        if (row.agent, row.session_id) == (agent, session_id)), None)
        if refusal is None:
            raise ValueError(f'{codespace} refused no {agent} conversation {session_id} in the last sync; nothing was forgotten.')
        records.append(dict(agent=agent, session_id=session_id, codespace=codespace,
                            reason=refusal.reason, forgotten_at=now()))
        atomic_write(state / FILE, json.dumps(dict(schema=1, conversations=records),
                                            sort_keys=True, allow_nan=False).encode() + b'\n')
    return (f'Forgot {agent} conversation {session_id}: it is no longer sent to {codespace} on its own, '
            'and nothing is deleted. The next sync clears the warning.')
