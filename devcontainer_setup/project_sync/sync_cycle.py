"""Current conversation cycle evidence, separate from retained byte custody."""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime
import json
import secrets
import signal
import threading
import time

SCHEMA = 3
DIRECTIONS = ('desktop_to_codespace', 'codespace_to_desktop')
# RPC calls have their own 180 second transport bound. A whole stage (which may
# contain several calls) also has a deadline; an idle PID cannot refresh evidence.
STAGE_TIMEOUT = 240
HEARTBEAT_MAX_AGE = 30
RESULT_MAX_AGE = 300


def recent(timestamp, limit, *, now=None):
    now = time.time() if now is None else now
    return type(timestamp) in (int, float) and 0 <= now - timestamp <= limit


def fresh_worker(runtime):
    cycle = runtime.get('conversations') or {}
    active = (cycle.get('schema') == SCHEMA and cycle.get('outcome') == 'pending'
              and recent(cycle.get('stage_started_at'), STAGE_TIMEOUT))
    return recent(runtime.get('heartbeat_at'), STAGE_TIMEOUT if active else HEARTBEAT_MAX_AGE)


def direction():
    return dict(stage='pending', outcome='pending', snapshot_id=None, receipt=None,
                publication=None, capture_issues=[], publication_issues=[], error=None,
                counts=dict(captured=0, received=0, received_bytes=0, available=0, pending=0, unsupported=0))


def begin(project, remote_root):
    now = time.time()
    return dict(schema=SCHEMA, cycle_id=secrets.token_hex(16), project_id=project.project_id,
                identity=project.identity, local_root=str(project.root), remote_root=remote_root,
                started_at=now, updated_at=now, completed_at=None, stage='verify',
                stage_started_at=now, stage_deadline_at=now + STAGE_TIMEOUT,
                outcome='pending', directions={name: direction() for name in DIRECTIONS}, issues=[])


def emit(cycle, progress):
    cycle['updated_at'] = time.time()
    if progress is not None:
        progress(copy.deepcopy(cycle))


class _StageExpired(BaseException):
    """Unwind per-session OSError/ValueError handlers at the stage boundary."""


@contextmanager
def stage(cycle, name, progress):
    now = time.time()
    cycle.update(stage=name, stage_started_at=now, stage_deadline_at=now + STAGE_TIMEOUT)
    emit(cycle, progress)
    # The detached supervisor and CLI run on the main thread. Never abandon a
    # background writer on timeout: interrupt the operation so its locks unwind.
    timed = threading.current_thread() is threading.main_thread()
    if timed:
        def expired(*_):
            raise _StageExpired()
        previous_handler = signal.signal(signal.SIGALRM, expired)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, STAGE_TIMEOUT)
        started = time.monotonic()
    try:
        yield
    except _StageExpired:
        raise TimeoutError('Conversation stage timed out') from None
    finally:
        if timed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0]:
                signal.setitimer(signal.ITIMER_REAL, max(.001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


def receipt_matches(receipt, snapshot):
    try:
        return (isinstance(snapshot, str) and len(snapshot) == 64
                and all(c in '0123456789abcdef' for c in snapshot)
                and set(receipt) == {'snapshot_id', 'received_at'} and receipt['snapshot_id'] == snapshot
                and datetime.fromisoformat(receipt['received_at']).tzinfo is not None)
    except (TypeError, ValueError, KeyError):
        return False


def unique_issues(issues):
    return list({json.dumps(issue, sort_keys=True): issue for issue in issues}.values())


def publication_result(row, manifest, publication):
    fields = ('imported', 'unchanged', 'issues')
    if (not isinstance(publication, dict)
            or any(not isinstance(publication.get(field), list) for field in fields)
            or any(not isinstance(item, dict) for field in fields for item in publication[field])):
        raise ValueError('Invalid native publication result')
    entries = {(entry.get('agent'), entry.get('session_id')) for entry in manifest['conversations']['entries']}
    available = {(entry.get('agent'), entry.get('source_session_id'))
                 for field in ('imported', 'unchanged') for entry in publication[field]}
    if not available <= entries:
        raise ValueError('Publication result differs from the received snapshot')
    row['publication'] = publication
    row['publication_issues'] = unique_issues([issue for issue in publication['issues']
                                              if issue not in row['capture_issues']])
    row['counts'].update(available=len(available), pending=len(entries - available))
    row['outcome'] = 'degraded' if row['capture_issues'] or row['publication_issues'] or entries - available else 'complete'


def summary(cycle, *, current, local_receipt):
    valid = cycle.get('schema') == SCHEMA
    completed = valid and cycle.get('completed_at') is not None
    fresh = current and valid and (recent(cycle.get('completed_at'), RESULT_MAX_AGE) if completed
                                  else recent(cycle.get('stage_started_at'), STAGE_TIMEOUT))
    rows = {}
    for name in DIRECTIONS:
        row = copy.deepcopy(cycle.get('directions', {}).get(name, direction())) if valid else direction()
        bound = receipt_matches(row.get('receipt'), row.get('snapshot_id'))
        if name == 'codespace_to_desktop':
            bound = bound and row['receipt'] == local_receipt
        row['stale'] = not fresh
        if not bound:
            row['counts'].update(received=0, received_bytes=0, pending=row['counts']['captured'])
        if not fresh or not bound:
            row['counts']['available'] = 0
        rows[name] = row
    healthy = bool(fresh and completed and cycle.get('outcome') == 'complete' and not cycle.get('issues')
                   and all(row.get('outcome') == 'complete' and receipt_matches(row['receipt'], row['snapshot_id'])
                           and not row['counts']['pending'] for row in rows.values())
                   and rows['codespace_to_desktop']['receipt'] == local_receipt)
    outcome = 'stale' if valid and not fresh else cycle.get('outcome', 'pending')
    return dict(healthy=healthy, outcome=outcome, cycle_id=cycle.get('cycle_id'),
                started_at=cycle.get('started_at'), completed_at=cycle.get('completed_at'),
                stage=cycle.get('stage', 'pending'), directions=rows, issues=cycle.get('issues', []))
