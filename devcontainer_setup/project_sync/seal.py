"""Private final-flush records, bound to a verified Mac snapshot and scan."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

from . import state as project_state, target_scan
from .replication import validate_manifest
from .transport import _name


@dataclass(frozen=True)
class Seal:
    codespace: str
    identity: str
    repository: str
    gh_user: str
    remote_root: str
    operation: str
    sealed_at: float
    cycle_id: str
    snapshot_id: str
    scratch: tuple[tuple[str, str], ...]
    sessions: tuple[tuple[str, str], ...]


def _text(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError('Invalid final-flush text')
    return value


def _path(value, *, absolute=False):
    path = Path(_text(value))
    if path.is_absolute() != absolute or '..' in path.parts:
        raise ValueError('Invalid final-flush path')
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid final-flush digest')
    return value


def _pairs(value, *, sessions=False):
    if not isinstance(value, (tuple, list)):
        raise ValueError('Invalid final-flush files')
    result = []
    for pair in value:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError('Invalid final-flush file')
        path, digest = pair
        _path(path)
        _digest(digest)
        if sessions and not path.startswith(('codex/sessions/', 'codex/archived_sessions/', 'claude/')):
            raise ValueError('Invalid final-flush conversation path')
        result.append((path, digest))
    if result != sorted(result) or len({path for path, _ in result}) != len(result):
        raise ValueError('Invalid final-flush file order')
    return tuple(result)


def _decode(state, codespace, value):
    if (not isinstance(value, dict) or set(value) != {'schema', *(field.name for field in fields(Seal))}
            or type(value['schema']) is not int or value['schema'] != 1 or value['codespace'] != codespace):
        raise ValueError('Invalid final-flush record')
    _name(value['codespace'])
    identity = _text(value['identity'])
    if (project_state._identity('https://' + identity) != identity
            or identity != 'github.com/' + _text(value['repository'])):
        raise ValueError('Final-flush repository differs')
    project_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
    state = Path(state)
    if re.fullmatch('[0-9a-f]{24}', state.name) and state.name != project_id:
        raise ValueError('Final-flush project differs')
    for filename in ('workspace.json', 'scratch.json'):
        try:
            current = project_state.read_json(state / filename)
        except FileNotFoundError:
            continue
        if current.get('identity') != identity:
            raise ValueError('Final-flush project differs')
    if not re.fullmatch('[A-Za-z0-9-]+', _text(value['gh_user'])):
        raise ValueError('Invalid final-flush account')
    _path(value['remote_root'], absolute=True)
    _text(value['operation'])
    _text(value['cycle_id'])
    _digest(value['snapshot_id'])
    if type(value['sealed_at']) not in (int, float) or not math.isfinite(value['sealed_at']):
        raise ValueError('Invalid final-flush time')
    datetime.fromtimestamp(value['sealed_at'])
    record = {field.name: value[field.name] for field in fields(Seal)}
    record.update(scratch=_pairs(value['scratch']), sessions=_pairs(value['sessions'], sessions=True))
    return Seal(**record)


def write(state, store, scan, *, evidence, identity, repository, gh_user, remote_root, operation,
          cycle_id, snapshot_id, now) -> Path:
    """Refuse a seal unless this scan compared exactly the loaded snapshot map."""
    from .verdict import Evidence, SAFE, Target, cutoffs, judge
    if isinstance(evidence, Evidence):
        if not evidence.steady:
            raise ValueError('Final-flush evidence is not steady')
        target = Target(scan.codespace, 'paired', remote_root, None, None)
        if next(row for row in judge(target, evidence, now=now) if row.name == 'History').verdict != SAFE:
            raise ValueError('Final-flush capture is not complete')
    try:
        if (not isinstance(evidence, Evidence) or evidence.scan is not scan or evidence.sync_running is not True
                or evidence.conflicts or evidence.mac_scratch is None
                or not set(scan.scratch) <= set(evidence.mac_scratch)):
            raise ValueError('Final-flush scratch has not reached this Mac')
        if Path(store.state) != Path(state):
            raise ValueError('Final-flush store differs')
        manifest = store.load(snapshot_id)
        validate_manifest(manifest, identity)
        if manifest['project'].get('root') != remote_root or manifest['conversations']['issues']:
            raise ValueError('Final-flush capture is incomplete or out of scope')
        for entry in manifest['conversations']['entries']:
            for item in entry['files']:
                if item['blob'] != item['sha256'] or len(store.read(item['blob'])) != item['size']:
                    raise ValueError('Final-flush prefix differs from the retained blob')
        compared = cutoffs(manifest)
        found = {(path.split('/', 1)[0], Path(path).name) for path, _ in scan.sessions}
        if (target_scan.fingerprint(scan) is None or scan.cutoffs_sha256 != target_scan.cutoffs_digest(compared)
                or set(scan.checked) != set(compared) & found or scan.mismatched or scan.tails):
            raise ValueError('Final-flush scan does not prove the captured prefixes')
        record = Seal(scan.codespace, identity, repository, gh_user, remote_root, operation, now,
                      cycle_id, snapshot_id, scan.scratch, scan.sessions)
        value = dict(schema=1, **asdict(record))
        _decode(state, scan.codespace, value)
        path = Path(state) / 'retired' / (scan.codespace + '.json')
        project_state.atomic_write(path, json.dumps(value, sort_keys=True, allow_nan=False).encode() + b'\n')
        return path
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('The final flush for ' + _name(scan.codespace) + ' cannot be recorded') from None


def read(state, codespace) -> Seal | None:
    codespace = _name(codespace)
    try:
        value = project_state.read_json(Path(state) / 'retired' / (codespace + '.json'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError('The final-flush record for ' + codespace + ' cannot be read') from None
    try:
        return _decode(state, codespace, value)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('The final-flush record for ' + codespace + ' cannot be read') from None


def names(state) -> tuple[str, ...]:
    records = []
    for path in (Path(state) / 'retired').glob('*.json'):
        try:
            record = read(state, path.stem)
        except ValueError:
            continue
        if record is not None:
            records.append(record)
    return tuple(record.codespace for record in sorted(records, key=lambda item: (item.sealed_at, item.codespace)))
