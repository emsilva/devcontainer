"""This package's own record of each Codespace start, per project (#61)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .lifecycle import LIFETIME, WINDOW
from .state import atomic_write, locked, read_json

FILE = 'start-evidence.json'
LOCK = 'start-evidence.lock'
_LOG = logging.getLogger(__name__)


def _log(error):
    # Fixed text only: an OSError can name a path, and nothing here is for a person to act on.
    _LOG.info('Start evidence was not written: %s', type(error).__name__)


def _load(state):
    try:
        data = read_json(Path(state) / FILE)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update(state, change):
    state = Path(state)
    with locked(state / LOCK, blocking=True):
        updated = change(_load(state))
        if updated is not None:
            atomic_write(state / FILE, (json.dumps(updated, sort_keys=True) + '\n').encode())


def record_start(state: Path, codespace: str, started_at: float, source: str) -> None:
    """Replace this Codespace's record with a start and no stop. Never raises."""
    def change(current):
        return {**current, codespace: {'started_at': started_at, 'source': source, 'stopped_at': None}}

    try:
        _update(state, change)
    except (OSError, ValueError) as error:
        _log(error)


def record_stop(state: Path, codespace: str, stopped_at: float) -> None:
    """Set stopped_at when a record exists and this stop is at or after its start. Never raises.

    A current stopped_at that is already at or after started_at stays: the first sighting is the one
    classification uses. A stopped_at from before this start is replaced.
    """
    def change(current):
        record = current.get(codespace)
        if not isinstance(record, dict):
            return None
        started = record.get('started_at')
        if isinstance(started, bool) or not isinstance(started, (int, float)):
            return None
        if stopped_at < started:
            return None
        existing = record.get('stopped_at')
        if existing is not None and not (isinstance(existing, (int, float)) and not isinstance(existing, bool)
                                          and existing < started):
            return None
        updated = dict(current)
        updated[codespace] = dict(record, stopped_at=stopped_at)
        return updated

    try:
        _update(state, change)
    except (OSError, ValueError) as error:
        _log(error)


def usable_start(state: Path, codespace: str, reading_at: float) -> float | None:
    """started_at when this Codespace's record can still be the run being read, else None.

    Usable when stopped_at is unset or earlier than started_at, and started_at falls in
    [reading_at - LIFETIME - WINDOW, reading_at]. A missing, corrupt or non-dict file is None.
    """
    try:
        data = read_json(Path(state) / FILE)
    except (OSError, ValueError):
        return None
    record = data.get(codespace) if isinstance(data, dict) else None
    if not isinstance(record, dict):
        return None
    started = record.get('started_at')
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return None
    stopped = record.get('stopped_at')
    if stopped is not None and (isinstance(stopped, bool) or not isinstance(stopped, (int, float))
                                or stopped >= started):
        return None
    if not reading_at - LIFETIME - WINDOW <= started <= reading_at:
        return None
    return float(started)
