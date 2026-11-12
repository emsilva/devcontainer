"""Read-only readiness and full-closure conversation custody checks for NEW."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time

from . import arming, forget, keeper, replacing, restore, scratch, seal, sync_cycle, verdict, workspace
from .checkpoint_store import Store
from .replication import validate_manifest
from .state import atomic_write, read_json, runtime_id


_CATEGORIES = ('on_new', 'kept', 'forgotten', 'missing', 'pending', 'diverged', 'kept_mac_only')
_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError)


def _epoch(value):
    return type(value) in (int, float) and math.isfinite(value)


def _key(entry, field='session_id'):
    agent, session = entry.get('agent'), entry.get(field)
    if not forget._ids(agent, session):
        raise ValueError('Invalid conversation identity')
    return agent, session


def _binding(project, state, expected):
    journal = replacing.read(state)
    binding = read_json(state / scratch.CONFIG)
    if (journal is None or journal['ended'] is not None
            or replacing.ended_by_fact(state, journal) is not None
            or replacing.PHASES.index(journal['phase']) < replacing.PHASES.index('moved')
            or any(journal[key] != expected[key] for key in ('operation', 'old', 'new'))
            or not journal['new'] or binding.get('codespace') != journal['new']
            or binding.get('identity') != project.identity
            or journal['repository'].casefold() != project.identity.removeprefix('github.com/').casefold()):
        raise ValueError('Unbound replace check')
    workspace.registry(project, state)
    return binding


def _observe(project, state, journal, started, now, sleep, wait):
    deadline = started + wait
    while True:
        try:
            binding = _binding(project, state, journal)
            value = workspace.registry(project, state)
            runtime = scratch.status(state)
            sync = scratch.inspect_sync(Path(binding.get('root', project.root)), state) if runtime.get('running') else runtime
            cycle, current = workspace.cycle_scope(project, value, runtime, sync)
            if (current and runtime.get('binding') == binding and cycle.get('schema') == sync_cycle.SCHEMA
                    and _epoch(cycle.get('started_at')) and cycle['started_at'] > started
                    and _epoch(cycle.get('completed_at'))):
                _binding(project, state, journal)
                return cycle
        except _ERRORS:
            pass
        remaining = deadline - now()
        if remaining <= 0:
            return None
        sleep(min(2, remaining))


def _sync_ready(project, state, cycle):
    if cycle is None:
        return False
    try:
        inbound, outbound = (cycle['directions'][name] for name in
                             ('codespace_to_desktop', 'desktop_to_codespace'))
        latest, _ = workspace._conversation_receipt(project, state)
        if (inbound['outcome'] != 'complete' or outbound['outcome'] not in ('complete', 'degraded')
                or not sync_cycle.receipt_matches(inbound['receipt'], inbound['snapshot_id'])
                or inbound['receipt'] != latest
                or not sync_cycle.receipt_matches(outbound['receipt'], outbound['snapshot_id'])):
            return False
        issues = outbound['publication_issues']
        for issue in cycle['issues']:
            if (issue.get('direction') != 'desktop_to_codespace'
                    or {key: value for key, value in issue.items() if key != 'direction'} not in issues):
                return False
            _key(issue)
        # A remote memory failure can be in publication_issues without an identity.
        for issue in issues:
            _key(issue)
        if inbound.get('capture_issues') or inbound.get('publication_issues') or outbound.get('capture_issues'):
            return False
        if inbound.get('error') or outbound.get('error'):
            return False
        manifest = Store(state).load(outbound['snapshot_id'])
        validate_manifest(manifest, project.identity, project.project_id)
        return True
    except _ERRORS:
        return False


def _closure(store, entry):
    """Verify metadata and every retained file, including Codex ancestors."""
    try:
        if not isinstance(entry['files'], list) or not entry['files']:
            return None
        result = {}
        for item in entry['files']:
            key, size = (item['session_id'], item['path']), item['size']
            if (not all(isinstance(part, str) and part for part in key) or key in result
                    or type(size) is not int or size < 0):
                return None
            data = store.read(item['blob'])
            if (len(data) != size or item['blob'] != item['sha256']
                    or hashlib.sha256(data).hexdigest() != item['sha256']):
                return None
            result[key] = (size, item['sha256'], data)
        if sum(sid == entry['session_id'] for sid, _ in result) != 1:
            return None
        return result
    except _ERRORS:
        return None


def _contains(later, earlier):
    return (later is not None and earlier is not None
            and all(key in later and later[key][0] >= size
                    and hashlib.sha256(later[key][2][:size]).hexdigest() == digest
                    for key, (size, digest, _) in earlier.items()))


def _conversations(project, state, journal, cycle):
    result = {name: [] for name in _CATEGORIES}
    store = Store(state)
    try:
        record = seal.read(state, journal['old'])
        if (record is None or record.operation != journal['operation'] or record.identity != project.identity):
            raise ValueError('Unbound final flush')
        manifest = store.load(record.snapshot_id)
        validate_manifest(manifest, project.identity, project.project_id)
        sealed = {}
        for entry in manifest['conversations']['entries']:
            key = _key(entry)
            closure = _closure(store, entry)
            if closure is None or key in sealed:
                raise ValueError('Unreadable final closure')
            sealed[key] = closure
        forgotten = forget.forgotten(state)
    except _ERRORS:
        return result, f"the final flush of {journal['old']} cannot be read"
    outgoing, published, issues = {}, set(), set()
    if cycle is not None:
        try:
            row = cycle['directions']['desktop_to_codespace']
            outbound = store.load(row['snapshot_id'])
            validate_manifest(outbound, project.identity, project.project_id)
            for entry in outbound['conversations']['entries']:
                outgoing.setdefault(_key(entry), []).append(_closure(store, entry))
            publication = row.get('publication') or {}
            published = {_key(entry, 'source_session_id') for field in ('imported', 'unchanged')
                         for entry in publication.get(field, [])}
            issues = {_key(issue) for issue in row['publication_issues']}
        except _ERRORS:
            outgoing, published, issues = {}, set(), set()
    for key, original in sorted(sealed.items()):
        candidates = outgoing.get(key, [])
        carrying = [closure for closure in candidates if _contains(closure, original)]
        if key in forgotten:
            category = 'forgotten'
        elif not carrying:
            category = 'missing'
        elif any(not _contains(closure, original)
                 and not any(_contains(carrier, closure) for carrier in carrying) for closure in candidates):
            category = 'diverged'
        elif key in issues:
            category = 'kept'
        elif key in published:
            category = 'on_new'
        else:
            category = 'pending'
        result[category].append(list(key))
    result['kept_mac_only'] = [list(key) for key in sorted(issues - sealed.keys())]
    return result, None


def _recovery(state, old, new):
    try:
        value = arming.read(state)
        if not value.get('armed'):
            return 'not armed'
        if value.get('codespace') != new:
            return 'recovery still points at ' + old
        current = keeper.status(state)
        if (current.get('running') and current.get('codespace') == new
                and current.get('runtime_id') == runtime_id()):
            return 'ok'
    except _ERRORS:
        pass
    return 'its keeper is not running'


def _herdr(state, new):
    try:
        value = read_json(state / 'herdr-machine.json')
        if value.get('codespace') == new and restore.Herdr(value['machine_id']).reachable():
            return 'ok'
    except _ERRORS:
        pass
    return 'Herdr cannot reach ' + new


def _listed(keys):
    return ', '.join(' '.join(key) for key in keys[:3]) + (f' and {len(keys) - 3} more' if len(keys) > 3 else '')


def _history_text(conversations, old, new, error):
    if error:
        return 'Not ready: ' + error
    failures = []
    for category, singular, plural in [('missing', f'is not on {new}', f'are not on {new}'),
                                       ('pending', 'is still being published', 'are still being published'),
                                       ('diverged', 'has a different version on this Mac', 'have a different version on this Mac')]:
        keys = conversations[category]
        if keys:
            details = '' if category == 'pending' else ' (' + _listed(keys) + ')'
            text = singular if len(keys) == 1 else plural
            failures.append(f'{verdict._quantity(len(keys), "conversation")} from {old} {text}{details}')
    count = len(conversations['on_new'])
    return 'Not ready: ' + '; '.join(failures) if failures else (
        f'{verdict._quantity(count, "conversation")} from {old} ' + ('is' if count == 1 else 'are') + f' on {new}')


def run(project, state, *, operation, receipt, now=time.time, sleep=time.sleep, wait=360) -> int:
    """Observe a later normal cycle and write only the caller's private check receipt."""
    state = Path(state)
    started = now()
    try:
        journal = replacing.read(state)
        if journal is None or journal['operation'] != operation:
            raise ValueError('Unbound replace check')
        _binding(project, state, journal)
    except _ERRORS:
        print('This replace record does not match; run the replace again.')
        return 1
    new, old = journal['new'], journal['old']
    cycle = _observe(project, state, journal, started, now, sleep, wait)
    sync = ('no sync cycle completed in 6 minutes' if cycle is None else
            'ok' if _sync_ready(project, state, cycle) else f'the last cycle on {new} did not complete')
    recovery, herdr = _recovery(state, old, new), _herdr(state, new)
    conversations, history_error = _conversations(project, state, journal, cycle if sync == 'ok' else None)
    checked = now()
    try:
        _binding(project, state, journal)
    except _ERRORS:
        sync = f'the last cycle on {new} did not complete'
    result = dict(schema=1, operation=operation, step='check', codespace=new, checked_at=checked,
                  sync=sync, cycle_completed_at=cycle['completed_at'] if cycle else None,
                  recovery=recovery, herdr=herdr,
                  conversations={key: value[:100] for key, value in conversations.items()},
                  ok=(sync == 'ok' and recovery in ('ok', 'not armed') and herdr == 'ok'
                      and history_error is None and not any(conversations[key] for key in ('missing', 'pending', 'diverged'))))
    atomic_write(Path(receipt), json.dumps(result, sort_keys=True, allow_nan=False).encode() + b'\n')
    print(f'Checked {new} at {datetime.fromtimestamp(checked):%H:%M}:')
    rows = [('Sync', 'a cycle completed at ' + format(datetime.fromtimestamp(cycle['completed_at']), '%H:%M')
             if sync == 'ok' else 'Not ready: ' + sync),
            ('Recovery', 'armed; its keeper is running' if recovery == 'ok' else
             recovery if recovery == 'not armed' else 'Not ready: ' + recovery),
            ('Herdr', 'reachable' if herdr == 'ok' else 'Not ready: ' + herdr),
            ('History', _history_text(conversations, old, new, history_error))]
    for label, text in rows:
        print(f'  {label:10}{text}')
    kept = conversations['kept'] + conversations['kept_mac_only']
    if kept:
        print('Kept on this Mac only: ' + _listed(kept))
    return 0 if result['ok'] else 1
