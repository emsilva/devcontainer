"""Private replace journals and receipts shared by observational runtime readers.

Only the setup coordinator writes or archives journals. Runtime readers never
acquire replace.lock, including when checked deletion runs beneath its owner.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

from .state import atomic_write, read_json
from .transport import _login as _github_login, _name


JOURNAL = 'replace.json'
ARCHIVE = 'replacements'
LOCK = 'replace.lock'
RECEIPTS = 'replace-receipts'
PHASES = ('inspected', 'create-requested', 'created', 'ready', 'moved', 'signed-in', 'checked', 'offered')
ENDED = ('completed', 'abandoned', 'deleted')
_ERROR = 'The replace record cannot be read'


def _text(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError(_ERROR)
    return value


def _operation(value):
    if not re.fullmatch('[0-9a-f]{12}', _text(value)):
        raise ValueError(_ERROR)
    return value


def _epoch(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(_ERROR)


def _login(value):
    try:
        return _github_login(value)
    except ValueError:
        raise ValueError(_ERROR) from None


def _validate(value):
    try:
        keys = {'schema', 'operation', 'old', 'new', 'account', 'repository', 'branch', 'commit',
                'remote_root', 'resources', 'old_receipt', 'branch_receipt', 'request', 'phase', 'at',
                'signins', 'check', 'moving', 'ended', 'ended_at'}
        if (not isinstance(value, dict) or set(value) != keys
                or type(value['schema']) is not int or value['schema'] != 1 or value['phase'] not in PHASES):
            raise ValueError
        _operation(value['operation'])
        _name(value['old'])
        if value['new'] is not None:
            _name(value['new'])
            if value['new'] == value['old']:
                raise ValueError
        _login(value['account'])
        if not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', _text(value['repository'])):
            raise ValueError
        _text(value['branch'])
        if not re.fullmatch('[0-9a-f]{40}', _text(value['commit'])):
            raise ValueError
        root = Path(_text(value['remote_root']))
        if not root.is_absolute() or '..' in root.parts:
            raise ValueError
        resources = value['resources']
        if not isinstance(resources, dict) or set(resources) != {'machine', 'cpus', 'idle_timeout_minutes', 'billable_owner'}:
            raise ValueError
        _text(resources['machine'])
        _login(resources['billable_owner'])
        for key in ('cpus', 'idle_timeout_minutes'):
            if type(resources[key]) is not int or resources[key] <= 0:
                raise ValueError
        for key in ('old_receipt', 'branch_receipt', 'check'):
            if value[key] is not None and not isinstance(value[key], dict):
                raise ValueError
        if not isinstance(value['at'], dict) or value['phase'] not in value['at']:
            raise ValueError
        for phase, stamp in value['at'].items():
            if phase not in PHASES:
                raise ValueError
            _epoch(stamp)
        request = value['request']
        if request is not None:
            if not isinstance(request, dict) or set(request) != {'at', 'before', 'retries'}:
                raise ValueError
            _epoch(request['at'])
            if not isinstance(request['before'], list) or not isinstance(request['retries'], list):
                raise ValueError
            for name in request['before']:
                _name(name)
            for stamp in request['retries']:
                _epoch(stamp)
        signins = value['signins']
        if signins is not None:
            if not isinstance(signins, dict) or set(signins) != {'retry', 'at'} or not isinstance(signins['retry'], list):
                raise ValueError
            _epoch(signins['at'])
            for tool in signins['retry']:
                if tool not in ('plaud', 'codex', 'claude', 'opencode', 'grok', 'unknown'):
                    raise ValueError
        if value['moving'] is not None:
            _epoch(value['moving'])
        if value['ended'] is None:
            if value['ended_at'] is not None:
                raise ValueError
        else:
            if value['ended'] not in ENDED:
                raise ValueError
            _epoch(value['ended_at'])
        # Also reject non-JSON and non-finite data inside optional receipt objects.
        json.dumps(value, allow_nan=False)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError(_ERROR) from None
    return value


def _optional(path):
    try:
        return read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError(_ERROR) from None


def _archived(state, operation):
    value = _optional(Path(state) / ARCHIVE / (_operation(operation) + '.json'))
    if value is not None:
        _validate(value)
        if value['operation'] != operation or value['ended'] is None:
            raise ValueError(_ERROR)
    return value


def read(state) -> dict | None:
    value = _optional(Path(state) / JOURNAL)
    if value is None:
        return None
    _validate(value)
    return None if _archived(state, value['operation']) is not None else value


def _bytes(value):
    return json.dumps(_validate(value), sort_keys=True, allow_nan=False).encode() + b'\n'


def write(state, journal) -> None:
    atomic_write(Path(state) / JOURNAL, _bytes(journal))


def archive(state, journal, ended, now) -> Path:
    value = dict(_validate(journal), ended=ended, ended_at=now)
    _validate(value)
    state = Path(state)
    path = state / ARCHIVE / (value['operation'] + '.json')
    existing = _archived(state, value['operation'])
    if existing is None:
        atomic_write(path, _bytes(value))
    elif dict(existing, ended_at=now) != value:
        raise ValueError(_ERROR)
    active = _optional(state / JOURNAL)
    if active is not None and _validate(active)['operation'] == value['operation']:
        (state / JOURNAL).unlink()
    return path


def ended_by_fact(state, journal) -> str | None:
    for name in (journal['old'], journal['new']):
        if name is None:
            continue
        value = _optional(Path(state) / 'deletions' / (_name(name) + '.json'))
        if value and value.get('phase') in ('deleted', 'cleaned') and value.get('owner_checked', True) is True:
            return name
    return None


def move_started(state, journal) -> bool:
    if journal.get('moving') is not None:
        return True
    new = journal['new']
    if new is None:
        return False
    state = Path(state)
    record = _optional(state / 'retired' / (_name(journal['old']) + '.json'))
    if not record or record.get('operation') != journal['operation']:
        return False
    for filename, key in (('scratch.json', 'codespace'), ('scratch-retarget.json', 'candidate'),
                          ('recovery-move.json', 'new'), ('workspace-pending.json', 'codespace')):
        value = _optional(state / filename) or {}
        named = value.get(key)
        if key == 'candidate':
            named = named.get('codespace') if isinstance(named, dict) else None
        if named == new:
            return True
    return False


def naming_new(state, name) -> dict | None:
    name = _name(name)
    journals = []
    active = read(state)
    if active is not None:
        journals.append(active)
    for path in (Path(state) / ARCHIVE).glob('*.json'):
        value = _archived(state, path.stem)
        if value is not None:
            journals.append(value)
    # Check NEW directly: OLD may also have a deletion record and be returned
    # first by ended_by_fact, which must not hide NEW's own deletion fact.
    deletion = _optional(Path(state) / 'deletions' / (name + '.json'))
    if deletion and deletion.get('phase') in ('deleted', 'cleaned') and deletion.get('owner_checked', True) is True:
        return None
    candidates = [value for value in journals if value['new'] == name]
    return max(candidates, key=lambda value: (max(value['at'].values()), value['ended_at'] or 0,
                                             value['operation']), default=None)


def receipt_path(state, operation, step) -> Path:
    if step not in ('cutover', 'check'):
        raise ValueError(_ERROR)
    return Path(state) / RECEIPTS / (_operation(operation) + '-' + step + '.json')


def read_receipt(path, operation, step) -> dict | None:
    try:
        value = read_json(path)
    except (OSError, ValueError):
        return None
    if (type(value.get('schema')) is int and value['schema'] == 1
            and value.get('operation') == operation and value.get('step') == step):
        return value
    return None
