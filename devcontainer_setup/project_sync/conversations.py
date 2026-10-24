"""Read-only native checkpoints and new-ID project-scoped publication.

Native writers may append while capture retains a validated completed prefix.
Publication journals exclusively owned transcripts before metadata-only native
projection; existing source identities are never resumed or overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

from . import agents, sessions
from .state import _parent, atomic_write, locked, read_json

SCHEMA = 1


def _stable_prefix(path, length=None):
    """Return an unchanged complete-record prefix while allowing append growth."""
    path = sessions._safe(path)
    with _parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError('Native checkpoint requires a regular file without hard links')
            size = before.st_size if length is None else length
            if type(size) is not int or size < 0 or size > sessions.MAX_BYTES or size > before.st_size:
                raise ValueError('Unsupported or truncated native checkpoint prefix')
            data = os.pread(fd, size, 0)
            again = os.pread(fd, size, 0)
            after = os.fstat(fd)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (len(data) != size or len(again) != size or data != again
                    or after.st_size < before.st_size or after.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)):
                raise ValueError('Native prefix changed or was truncated during checkpoint')
            if length is not None and data and not data.endswith(b'\n'):
                raise ValueError('Ancestor byte offset is not a complete-record boundary')
            return data if length is not None else data[:data.rfind(b'\n') + 1]
        finally:
            os.close(fd)


def _rows(data):
    try:
        result = [json.loads(line) for line in data.splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Malformed native JSONL record') from exc
    if not result or any(not isinstance(row, dict) for row in result):
        raise ValueError('Empty or invalid native transcript')
    return result


def _scopes(project, state, historical_roots=()):
    scopes = {str(project.root)}
    registry = {}
    if state is not None:
        try:
            registry = read_json(Path(state) / 'workspace.json')
        except FileNotFoundError:
            pass
        if registry and (registry.get('project_id') != project.project_id or registry.get('identity') != project.identity):
            raise ValueError('Workspace registry belongs to another project')
    # Legacy active-root strings are not scope attestations. Migration verifies
    # actual Git identity before retaining these host-specific historical roots;
    # RPC independently verifies every explicit remote capture root.
    for roots in (registry.get('verified_local_historical_roots', []),
                  registry.get('verified_remote_historical_roots', []), historical_roots):
        if not isinstance(roots, (list, tuple)) or len(roots) > 100:
            raise ValueError('Historical project scopes exceed verified root bound')
        for root in roots:
            sessions._scoped_roots(root, [root])
            scopes.add(root)
    return sorted(scopes)


def _claude_child_scopes(project, receipt, path, data, store, destination_roots):
    """Authorize historical roots only through an owned native fork's history."""
    if receipt.get('schema') != SCHEMA or receipt.get('status') != 'complete' or receipt.get('agent') != 'claude':
        return set()
    if receipt.get('publication'):
        return _published_claude_scopes(receipt, path, data, store, destination_roots)
    result = receipt.get('result', {})
    child_id = result.get('session_id')
    destination = receipt.get('destination_root')
    if (not isinstance(destination, str) or destination not in destination_roots
            or path.name != str(child_id) + '.jsonl'
            or path.parent.name != sessions.claude_namespace(destination)):
        return set()
    sessions.session_uuid(child_id)
    source_id = sessions.session_uuid(receipt.get('source_session_id'))
    files = receipt.get('files', [])
    if len(files) != 1 or files[0].get('source_session_id') != source_id or not files[0].get('created'):
        raise ValueError('Claude fork provenance has no exact owned source alias')
    item = files[0]
    alias_id = sessions.session_uuid(item.get('physical_alias'))
    alias = Path(item.get('destination', ''))
    if alias != path.with_name(alias_id + '.jsonl'):
        raise ValueError('Claude fork source alias belongs to another destination')
    alias_data = _stable_prefix(alias)
    size = item.get('size')
    digest = item.get('sha256')
    if (type(size) is not int or size != len(alias_data) or alias.stat().st_size != size
            or hashlib.sha256(alias_data).hexdigest() != digest):
        raise ValueError('Claude fork source alias changed from its immutable checkpoint')
    try:
        retained = store.read(digest)
    except (KeyError, OSError, ValueError) as exc:
        raise ValueError('Claude fork source checkpoint blob is missing') from exc
    if retained != alias_data:
        raise ValueError('Claude fork source alias does not match its retained blob')
    if item.get('inode') is not None:
        info = alias.stat(follow_symlinks=False)
        if [info.st_dev, info.st_ino] != item['inode']:
            raise ValueError('Claude fork source alias inode ownership changed')
    original = sessions._records(alias_data, 'claude')
    sessions._validate_claude(original, source_id, item.get('scope_roots'))
    provenance = result.get('provenance', {})
    final = original[-1]
    if (provenance.get('agent') != 'claude' or provenance.get('source_session_id') != source_id
            or provenance.get('cutoff') != dict(turn_id=final.get('uuid'), bytes=size)
            or final.get('type') != 'assistant'
            or final.get('message', {}).get('stop_reason') not in ('end_turn', 'stop_sequence')):
        raise ValueError('Claude fork receipt does not bind its completed source history')
    # Native Claude retains message/attachment UUIDs and payloads. Forking changes
    # only the session/cwd envelopes and the user's promptId; attachments may move.
    inherited = [row for row in original if 'uuid' in row]
    child = sessions._records(data, 'claude')
    by_uuid = {}
    for position, row in enumerate(child):
        if 'uuid' in row:
            by_uuid.setdefault(row['uuid'], []).append((position, row))
    inherited_ids = [row['uuid'] for row in inherited]
    if len(inherited_ids) != len(set(inherited_ids)) or not inherited_ids:
        raise ValueError('Ambiguous Claude inherited source identities')
    positions = []
    for source in inherited:
        matches = by_uuid.get(source['uuid'], [])
        if len(matches) != 1:
            raise ValueError('Claude fork is missing its exact inherited history prefix')
        position, candidate = matches[0]
        positions.append(position)
        expected, actual = dict(source), dict(candidate)
        if 'sessionId' in expected:
            if actual.pop('sessionId', None) != child_id:
                raise ValueError('Claude fork inherited session identity changed')
            expected.pop('sessionId')
        if 'cwd' in expected:
            if actual.pop('cwd', None) != destination:
                raise ValueError('Claude fork inherited runtime checkout changed')
            expected.pop('cwd')
        if source['type'] == 'user' and 'promptId' in expected:
            sessions.session_uuid(actual.pop('promptId', None))
            expected.pop('promptId')
        if actual != expected:
            raise ValueError('Claude fork inherited history content changed')
    source_messages = [row['uuid'] for row in inherited if row['type'] in ('user', 'assistant')]
    child_messages = [row.get('uuid') for row in child if row['type'] in ('user', 'assistant')]
    if child_messages[:len(source_messages)] != source_messages:
        raise ValueError('Claude fork inherited conversation is not its history prefix')
    if any(row.get('uuid') not in inherited_ids for row in child[:max(positions) + 1] if 'uuid' in row):
        raise ValueError('Claude fork mixed unrelated rows into inherited history')
    # Unused receipt roots grant no authority. Only structural roots actually in
    # the immutable inherited history become approved historical project scopes.
    roots = set()
    for row in original:
        if 'cwd' in row:
            roots.add(row['cwd'])
        attachment = row.get('attachment', {})
        if attachment.get('type') == 'environment':
            snapshot = attachment.get('snapshot', {})
            roots.add(snapshot.get('workingDirectory'))
            roots.update(snapshot.get('additionalWorkingDirectories', []))
    if provenance.get('source_root') not in roots:
        raise ValueError('Claude fork source root disagrees with immutable history')
    return roots


def _receipt_scopes(project, state, path, data, store):
    destination_roots = _scopes(project, state)
    scopes = set(destination_roots)
    if state is not None:
        for receipt_path in (Path(state) / 'conversation-imports').glob('*/receipt.json'):
            receipt = read_json(receipt_path)
            if receipt.get('project_id') != project.project_id or receipt.get('identity') != project.identity:
                continue
            scopes.update(_claude_child_scopes(project, receipt, path, data, store, destination_roots))
            if receipt.get('publication') and receipt.get('agent') == 'claude':
                continue
            for item in receipt.get('files', []):
                destination = Path(item.get('destination', ''))
                archived_fork = (receipt.get('publication') and item.get('native_fork')
                                 and len(destination.parents) >= 5
                                 and path.is_relative_to(destination.parents[4] / 'archived_sessions')
                                 and path.name == destination.name)
                if str(path) != item.get('destination') and not archived_fork:
                    continue
                size = item.get('size')
                if type(size) is int and 0 <= size <= sessions.MAX_BYTES:
                    prefix = data[:size] if len(data) >= size else _stable_prefix(path, size)
                    if hashlib.sha256(prefix).hexdigest() != item.get('sha256'):
                        continue
                    for scope in item.get('scope_roots', []):
                        sessions._scoped_roots(scope, [scope])
                        scopes.add(scope)
    return sorted(scopes)


def _codex_turn(rows, inherited=None):
    active = None
    cutoff = inherited
    end = 0
    for number, row in enumerate(rows):
        payload = row.get('payload', {})
        if not isinstance(payload, dict):
            raise ValueError('Unsupported native record payload')
        if row.get('type') == 'event_msg' and payload.get('type') == 'task_started':
            active = payload.get('turn_id')
            if not isinstance(active, str) or not active:
                raise ValueError('Unsupported native turn identity')
        elif row.get('type') == 'event_msg' and payload.get('type') == 'task_complete':
            turn = payload.get('turn_id')
            if not isinstance(turn, str) or not turn or turn != active:
                raise ValueError('Completed native turn lacks matching start')
            cutoff, end, active = turn, number + 1, None
        elif active is None and cutoff is not None:
            # Native fork metadata/settings after inherited history are safe;
            # drop them if an incomplete turn starts after this point.
            end = number + 1
    return cutoff, end


def _ordinal(data):
    rows = _rows(data)
    ordinals = [row.get('ordinal') for row in rows]
    if any(type(value) is not int or value < 0 for value in ordinals):
        raise ValueError('Unsupported native ancestor ordinal')
    if any(right != left + 1 for left, right in zip(ordinals, ordinals[1:])):
        raise ValueError('Noncontiguous native ancestor ordinal')
    return ordinals[-1] + 1


def _codex_entry(project, store, sid, index, *, state, scopes):
    visiting, retained, cutoff_by_id, files, observed = set(), {}, {}, [], []
    def visit(current, length=None):
        if current in visiting:
            raise ValueError('Cyclic native history ancestor dependency')
        if current in retained:
            if length is not None and len(retained[current]) != length:
                raise ValueError('Inconsistent ancestor byte offset')
            return cutoff_by_id[current]
        if current not in index:
            raise ValueError('Missing native history ancestor: ' + current)
        path = index[current]
        if path.suffix != '.jsonl':
            raise ValueError('Unsupported compressed native transcript')
        visiting.add(current)
        data = _stable_prefix(path, length)
        rows = _rows(data)
        if rows[0].get('type') != 'session_meta' or sum(row.get('type') == 'session_meta' for row in rows) != 1:
            raise ValueError('Expected one leading native session metadata record')
        meta = rows[0].get('payload', {})
        if not isinstance(meta, dict):
            raise ValueError('Invalid native session metadata')
        sessions.session_uuid(meta.get('id'))
        sessions._version('codex', meta.get('cli_version'))
        approved = sorted(set(scopes) | set(_receipt_scopes(project, state, path, data, store)))
        sessions._scoped_roots(meta.get('cwd'), approved)
        parent = sessions._parent(meta)
        inherited = None
        if parent:
            base = meta['history_base']
            inherited = visit(parent, base['end_byte_offset'])
            if _ordinal(retained[parent]) != base['end_ordinal_exclusive']:
                raise ValueError('Invalid ancestor end ordinal')
            if rows[0].get('ordinal') != base['end_ordinal_exclusive']:
                raise ValueError('Invalid fork start ordinal')
        cutoff, end = _codex_turn(rows, inherited)
        if cutoff is None:
            raise ValueError('No validated completed native turn yet')
        if length is not None and end != len(rows):
            raise ValueError('History ancestor ends in an incomplete turn')
        kept = b''.join(data.splitlines(keepends=True)[:end])
        valid = sessions._records(kept, 'codex')
        sessions._validate_codex_dependencies(valid, meta, current)
        sessions._validate_codex_scope(valid, approved)
        retained[current], cutoff_by_id[current] = kept, cutoff
        digest = hashlib.sha256(kept).hexdigest()
        files.append(dict(session_id=current, path=path.name, size=len(kept), sha256=digest, blob=digest, scope_roots=approved, origin_root=meta['cwd']))
        observed.append((path, kept))
        visiting.remove(current)
        return cutoff
    cutoff = visit(sid)
    # Validate all dependency prefixes again after constructing the closure.
    for path, data in observed:
        if _stable_prefix(path, len(data)) != data:
            raise ValueError('Native dependency prefix changed during checkpoint')
    if sum(len(data) for data in retained.values()) > sessions.MAX_BYTES:
        raise ValueError('Native dependency closure exceeds size limit')
    meta = _rows(retained[sid])[0]['payload']
    return retained, dict(schema=SCHEMA, agent='codex', session_id=sid, project_id=project.project_id, identity=project.identity, source_root=meta['cwd'], scope_roots=sorted({root for item in files for root in item['scope_roots']}), cutoff=dict(turn_id=cutoff, bytes=len(retained[sid])), files=files, title=sessions._catalog_metadata(retained[sid], 'codex', index[sid])['title'], updated_at=index[sid].stat().st_mtime)


def _claude_entry(project, store, sid, path, *, state, scopes):
    # The transcript resumes on its own. Its side folders (subagents, large tool
    # results) and task list stay on this host for now (#40).
    data = _stable_prefix(path)
    rows = _rows(data)
    end, cutoff = 0, None
    for number, row in enumerate(rows):
        if row.get('type') == 'assistant' and isinstance(row.get('message'), dict) and row['message'].get('stop_reason') in ('end_turn', 'stop_sequence'):
            cutoff, end = row.get('uuid'), number + 1
            if not isinstance(cutoff, str) or not cutoff:
                raise ValueError('Claude completed turn lacks native UUID')
    if cutoff is None:
        raise ValueError('No validated completed Claude turn yet')
    kept = b''.join(data.splitlines(keepends=True)[:end])
    valid = sessions._records(kept, 'claude')
    approved = sorted(set(scopes) | set(_receipt_scopes(project, state, path, kept, store)))
    sessions._validate_claude(valid, sid, approved)
    versions = [row['version'] for row in valid if 'version' in row]
    if not versions:
        raise ValueError('Cannot establish Claude native version')
    for version in versions:
        sessions._version('claude', version)
    if _stable_prefix(path, len(kept)) != kept:
        raise ValueError('Claude prefix changed during checkpoint')
    origin = next(row['cwd'] for row in reversed(valid) if 'cwd' in row)
    digest = hashlib.sha256(kept).hexdigest()
    return {sid: kept}, dict(schema=SCHEMA, agent='claude', session_id=sid, project_id=project.project_id, identity=project.identity, source_root=origin, scope_roots=approved, cutoff=dict(turn_id=cutoff, bytes=len(kept)), files=[dict(session_id=sid, path=path.name, size=len(kept), sha256=digest, blob=digest, scope_roots=approved, origin_root=origin)], title=sessions._catalog_metadata(kept, 'claude', path)['title'], updated_at=path.stat().st_mtime)


def _claude_entrypoint(path):
    """The first entrypoint a Claude transcript records, read from a bounded prefix."""
    with _parent(sessions._safe(path)) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Native metadata requires a regular file without hard links')
            budget = 16 * 1024 * 1024  # Early rows can carry large prompt snapshots.
            for _ in range(64):
                line = source.readline(budget + 1)
                budget -= len(line)
                if budget < 0 or not line.endswith(b'\n'):
                    return None
                try:
                    row = json.loads(line)
                except ValueError:
                    return None
                if isinstance(row, dict) and isinstance(row.get('entrypoint'), str):
                    return row['entrypoint']
    return None


def _finished_workers(home, scopes):
    """Sessions a herd record shows as over: its worker closed, its pane reused or its context reset.

    The record is the herd tooling's herd.jsonl beside the lead's transcript. Only
    the current session of a launch still open is live.
    """
    over = set()
    for root in scopes:
        path = home / 'projects' / sessions.claude_namespace(root) / 'herd.jsonl'
        if not path.is_file() or path.is_symlink():
            continue
        launches, known = {}, set()
        for row in _rows(_stable_prefix(path)):
            lid, kind = row.get('launch_id'), row.get('type')
            if kind not in ('launch', 'dispatch', 'close'):
                continue  # Rows that change no session (reported, or newer tooling's).
            if not isinstance(lid, str):
                raise ValueError('Invalid herd record row')
            if kind == 'launch':
                for other in launches.values():
                    if other['pane'] == row.get('pane_id'):
                        other['open'] = False
                launches[lid] = dict(pane=row.get('pane_id'), session=row.get('session_id'), open=True)
            elif lid not in launches:
                raise ValueError('Herd record row names an unknown launch')
            elif kind == 'dispatch' and (row.get('reset') or row.get('session_id')):
                launches[lid]['session'] = row.get('session_id')
            elif kind == 'close':
                launches[lid]['open'] = False
            known.update(value for value in (row.get('session_id'),) if isinstance(value, str))
        over |= known - {launch['session'] for launch in launches.values() if launch['open']}
    return over


def _import_aliases(project, state):
    aliases = set()
    if state is not None:
        for receipt_path in (Path(state) / 'conversation-imports').glob('*/receipt.json'):
            receipt = read_json(receipt_path)
            if not receipt.get('publication') and receipt.get('project_id') == project.project_id and receipt.get('identity') == project.identity:
                aliases.update(item['destination'] for item in receipt.get('files', []) if item.get('physical_alias'))
    return aliases


def capture_conversations(project, store, *, state=None, env=None, historical_roots=()):
    """Observe ordinary project sessions without a native process or writer lock."""
    scopes = _scopes(project, state, historical_roots)
    result = dict(schema=SCHEMA, entries=[], issues=[])
    def issue(agent, sid, error):
        result['issues'].append(dict(agent=agent, session_id=sid, reason=sessions.display_text(str(error), 400)))
    def keep(retained, entry):
        # Decide before any put: no manifest would reference an unchanged import's
        # blobs, and the store never deletes. Its retained bytes are still in hand.
        if _unchanged_import(project, retained, state, entry):
            return
        try:
            for item in entry['files']:
                store.put(retained[item['session_id']])
        except (ValueError, OSError) as exc:
            issue(entry['agent'], entry['session_id'], exc)
            return
        result['entries'].append(entry)
    home = sessions.native_home('claude', env)
    try:
        finished = _finished_workers(home, scopes)
    except (ValueError, OSError) as exc:
        # Capturing too much is recoverable; dropping a live session is not.
        finished = set()
        issue('herd', None, 'Unreadable herd record; capturing every session: ' + str(exc))
    try:
        index = sessions._codex_index(sessions.native_home('codex', env))
    except (ValueError, OSError) as exc:
        index = {}
        issue('codex', None, exc)
    for sid, path in index.items():
        if sid in finished:
            continue
        try:
            # Identify scope from the first line before validating content so
            # unrelated personal histories neither export nor flood warnings.
            with _parent(sessions._safe(path)) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, 'rb') as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError('Native metadata requires a regular file without hard links')
                    first = source.readline(min(sessions.MAX_BYTES, 1024 * 1024) + 1)
            row = json.loads(first)
            if not isinstance(row, dict):
                raise ValueError('Invalid native session metadata')
            meta = row.get('payload', {})
            if not isinstance(meta, dict):
                raise ValueError('Invalid native session metadata')
            if meta.get('source') == 'exec':
                continue  # Headless runs are not conversations; the picker hides them (#49).
            approved = sorted(set(scopes) | set(_receipt_scopes(project, state, path, first, store)))
            if meta.get('cwd') not in approved:
                continue
            retained, entry = _codex_entry(project, store, sid, index, state=state, scopes=scopes)
        except (ValueError, OSError) as exc:
            issue('codex', sid, exc)
            continue
        keep(retained, entry)
        del retained  # Hold one session's retained bytes at a time.
    aliases = _import_aliases(project, state)
    for root in scopes:
        directory = home / 'projects' / sessions.claude_namespace(root)
        if directory.is_symlink():
            issue('claude', None, ValueError('Symlink native project store'))
            continue
        for path in directory.glob('*.jsonl'):
            sid = path.stem
            if str(path) in aliases or sid in finished or not re.fullmatch(r'[0-9a-f-]{36}', sid):
                continue  # Not a transcript (herd.jsonl and the like), or a finished worker's.
            try:
                sessions.session_uuid(sid)
                if (_claude_entrypoint(path) or '').startswith('sdk-'):
                    continue  # Agent SDK and claude -p runs are not conversations.
                retained, entry = _claude_entry(project, store, sid, path, state=state, scopes=scopes)
            except (ValueError, OSError) as exc:
                issue('claude', sid, exc)
                continue
            keep(retained, entry)
            del retained
    result['entries'].sort(key=lambda item: (-item['updated_at'], item['agent'], item['session_id']))
    return result


def _preflight(project, store, entry):
    if (not isinstance(entry, dict) or entry.get('schema') != SCHEMA
            or entry.get('project_id') != project.project_id or entry.get('identity') != project.identity):
        raise ValueError('Conversation checkpoint belongs to another project or schema')
    agent = entry.get('agent')
    if agent not in ('codex', 'claude'):
        raise ValueError('Unsupported checkpoint agent')
    sid = sessions.session_uuid(entry.get('session_id'))
    scopes = entry.get('scope_roots')
    sessions._scoped_roots(entry.get('source_root'), scopes)
    if not isinstance(entry.get('files'), list) or not entry['files']:
        raise ValueError('Empty native dependency closure')
    by_id, metadata = {}, {}
    for item in entry['files']:
        if not isinstance(item, dict):
            raise ValueError('Invalid native dependency entry')
        current = sessions.session_uuid(item.get('session_id'))
        if current in by_id:
            raise ValueError('Duplicate native dependency ID')
        name = item.get('path')
        if not isinstance(name, str) or PurePosixPath(name).name != name or not name.endswith(current + '.jsonl'):
            raise ValueError('Unsafe native checkpoint path')
        digest = item.get('blob')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or digest != item.get('sha256'):
            raise ValueError('Invalid native checkpoint blob digest')
        try:
            data = store.read(digest)
        except (KeyError, OSError, ValueError) as exc:
            raise ValueError('Missing native checkpoint blob') from exc
        if type(item.get('size')) is not int or len(data) != item['size'] or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Native checkpoint blob size/digest mismatch')
        if len(data) > sessions.MAX_BYTES:
            raise ValueError('Native checkpoint exceeds size limit')
        approved = item.get('scope_roots')
        sessions._scoped_roots(item.get('origin_root'), approved)
        if any(root not in scopes for root in approved):
            raise ValueError('Dependency scopes exceed conversation checkpoint scopes')
        rows = sessions._records(data, agent)
        if agent == 'codex':
            if rows[0]['type'] != 'session_meta' or sum(row['type'] == 'session_meta' for row in rows) != 1:
                raise ValueError('Invalid native metadata closure')
            meta = rows[0]['payload']
            sessions._version(agent, meta.get('cli_version'))
            sessions._validate_codex_dependencies(rows, meta, current)
            sessions._validate_codex_scope(rows, approved)
            if meta.get('cwd') != item.get('origin_root'):
                raise ValueError('Native metadata root disagrees with checkpoint')
            metadata[current] = meta
        else:
            sessions._validate_claude(rows, sid, approved)
            for row in rows:
                if 'version' in row:
                    sessions._version(agent, row['version'])
        by_id[current] = (item, data, rows)
    if sid not in by_id:
        raise ValueError('Missing selected native checkpoint')
    cutoff = entry.get('cutoff')
    if not isinstance(cutoff, dict) or type(cutoff.get('bytes')) is not int or cutoff['bytes'] != len(by_id[sid][1]):
        raise ValueError('Invalid native completed-turn cutoff')
    order, seen, visiting = [], set(), set()
    def visit(current):
        if current in visiting or current not in by_id:
            raise ValueError('Missing or cyclic native history ancestor dependency')
        if current in seen:
            raise ValueError('Duplicate native history ancestor reference')
        visiting.add(current)
        item, data, rows = by_id[current]
        inherited = None
        parent = sessions._parent(metadata[current])
        if parent:
            inherited = visit(parent)
            parentdata = by_id[parent][1]
            base = metadata[current]['history_base']
            if base['end_byte_offset'] != len(parentdata) or _ordinal(parentdata) != base['end_ordinal_exclusive'] or rows[0].get('ordinal') != base['end_ordinal_exclusive']:
                raise ValueError('Invalid native ancestor byte/ordinal cutoff')
        turn, end = _codex_turn(rows, inherited)
        if turn is None or end != len(rows):
            raise ValueError('Checkpoint does not end at a validated completed turn')
        visiting.remove(current)
        seen.add(current)
        order.append(current)
        return turn
    if agent == 'codex':
        turn = visit(sid)
        if len(seen) != len(by_id) or turn != cutoff.get('turn_id') or metadata[sid].get('cwd') != entry['source_root']:
            raise ValueError('Native dependency closure or completed-turn cutoff mismatch')
    else:
        if len(by_id) != 1:
            raise ValueError('Unproven Claude live dependency closure')
        final = by_id[sid][2][-1]
        if final.get('type') != 'assistant' or final.get('uuid') != cutoff.get('turn_id') or final.get('message', {}).get('stop_reason') not in ('end_turn', 'stop_sequence'):
            raise ValueError('Invalid Claude completed-turn cutoff')
        order = [sid]
    if sum(len(item[1]) for item in by_id.values()) > sessions.MAX_BYTES:
        raise ValueError('Native dependency closure exceeds size limit')
    return agent, sid, by_id, order


def _owned_content(parent, name, intent, *, growing=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink not in (1, 2)
                or [before.st_dev, before.st_ino] != intent['inode']):
            raise ValueError('Native publication inode ownership changed')
        size = intent['size']
        if before.st_size < size or (not growing and before.st_size != size):
            raise ValueError('Incomplete staged native publication')
        data = os.pread(fd, size, 0)
        if len(data) != size or hashlib.sha256(data).hexdigest() != intent['sha256'] or os.pread(fd, size, 0) != data:
            raise ValueError('Native publication content changed')
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if ((before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                or after.st_size < before.st_size or after.st_nlink not in (1, 2)):
            raise ValueError('Native publication changed during validation')
        return after
    finally:
        os.close(fd)


def _reconcile_links(source, duplicate, intent, *, check_only=False):
    """Remove only the exact second link recorded by an owned publication."""
    if intent.get('created') is not True:
        raise ValueError('Borrowed native files cannot authorize link recovery')
    with _parent(source) as (parent, name), _parent(duplicate) as (other_parent, other_name):
        first = _owned_content(parent, name, intent, growing=True)
        second = _owned_content(other_parent, other_name, intent, growing=True)
        if (first.st_nlink != 2 or second.st_nlink != 2
                or (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino)
                or source == duplicate):
            raise ValueError('Native publication has an unowned hard link')
        if not check_only:
            os.unlink(other_name, dir_fd=other_parent)
            os.fsync(other_parent)


def _publish(intent, *, check_only=False):
    """Finish only a durably journaled inode; link never replaces a final path."""
    target, stage = Path(intent['destination']), Path(intent['staged_path'])
    if (intent.get('created') is not True or stage.parent != target.parent
            or not re.fullmatch(r'\.devcontainer-import-[0-9a-f]{32}\.tmp', stage.name)):
        raise ValueError('Invalid native publication staging path')
    with _parent(target, create=not check_only) as (parent, name):
        def present(filename):
            try:
                return os.stat(filename, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
        final, staged = present(name), present(stage.name)
        if final is not None:
            final = _owned_content(parent, name, intent, growing=True)
            if staged is not None:
                _reconcile_links(target, stage, intent, check_only=check_only)
            elif final.st_nlink != 1:
                raise ValueError('Native publication has an unowned hard link')
            return True
        if staged is None:
            return False  # A failed prepublication journal may have no file.
        staged = _owned_content(parent, stage.name, intent)
        if staged.st_nlink != 1:
            raise ValueError('Native publication has an unowned hard link')
        if not check_only:
            os.link(stage.name, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
            _reconcile_links(target, stage, intent)
        return True


def _reconcile_owned_publication(intent, target, data):
    """Bind a retry to its checkpoint, repair owned links, then apply _safe."""
    if (intent.get('created') is not True or intent.get('destination') != str(target)
            or intent.get('size') != len(data) or intent.get('sha256') != hashlib.sha256(data).hexdigest()):
        raise ValueError('Native publication intent differs from retained history')
    if not _publish(intent):
        raise ValueError('Journaled native publication stage disappeared')
    return sessions._safe(target)


def _codex_metadata_only(rows):
    return all(row.get('type') in ('world_state', 'turn_context')
               or (row.get('type') == 'event_msg' and isinstance(row.get('payload'), dict)
                   and row['payload'].get('type') == 'thread_settings_applied') for row in rows)


def _codex_projection_ready(target, intent, roots):
    """Defer loaded or user-continued leaves before starting another writer."""
    sessions.assert_quiescent([target])
    with _parent(target) as (parent, name):
        _owned_content(parent, name, intent, growing=True)
    data = _stable_prefix(target)
    if target.stat().st_size != len(data):
        raise ValueError('Native import has an unfinished local suffix; projection deferred')
    suffix = data[intent['size']:]
    if suffix:
        rows = sessions._records(suffix, 'codex')
        sessions._validate_codex_scope(rows, roots)
        if not _codex_metadata_only(rows):
            raise ValueError('Native import has local user activity; projection deferred')


def _create(path, data, mode=0o600, *, journal):
    """Stage, fsync, journal ownership, then publish without replacement."""
    with _parent(path, create=True) as (parent, name):
        temporary = '.devcontainer-import-' + uuid.uuid4().hex + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        recorded = False
        try:
            try:
                remaining = memoryview(data)
                while remaining:
                    count = os.write(fd, remaining)
                    if count <= 0:
                        raise OSError('Native staged write made no progress')
                    remaining = remaining[count:]
                os.fchmod(fd, mode)
                os.fsync(fd)
                info = os.fstat(fd)
            finally:
                os.close(fd)
            os.fsync(parent)
            intent = dict(destination=str(path), staged_path=str(path.parent / temporary),
                          inode=[info.st_dev, info.st_ino], size=len(data), sha256=hashlib.sha256(data).hexdigest())
            journal(intent)
            recorded = True
            if not _publish(dict(intent, created=True)):
                raise ValueError('Journaled native stage disappeared before publication')
        finally:
            # Once journaled, retain the stage on interruption so retry can prove
            # a publication between link and unlink. Before that it is private.
            if not recorded:
                try:
                    os.unlink(temporary, dir_fd=parent)
                    os.fsync(parent)
                except FileNotFoundError:
                    pass


def _expected(by_id, order):
    turns, items = [], []
    for sid in order:
        for row in by_id[sid][2]:
            payload = row['payload']
            if row['type'] == 'event_msg' and payload.get('type') == 'task_complete':
                turns.append(payload['turn_id'])
            if row['type'] == 'event_msg' and payload.get('type') == 'item_completed':
                if not isinstance(payload.get('item'), dict):
                    raise ValueError('Invalid completed native history item')
                items.append(payload['item'])
    return turns, items


def _verify_history(history, expected):
    turns, items = expected
    actual_turns = history.get('turns', [])
    if [item.get('id') for item in actual_turns] != turns or any(item.get('status') != 'completed' for item in actual_turns):
        raise ValueError('Native completed history turns do not match retained checkpoint')
    actual = [item.get('item', {}) for item in history.get('items', [])]
    # Persisted events use internal Rust enum names/fields; the paginated API
    # exposes camelCase public items. Compare their stable identities and text
    # rather than pretending those distinct representations are byte equal.
    def identity(item, persisted=False):
        kind = item.get('type', '')
        if persisted and kind == 'Extension' and item.get('kind') == 'web.search':
            kind = 'webSearch'  # Persisted as an extension kind, listed as its own item type.
        return item.get('id'), kind[:1].lower() + kind[1:]
    if [identity(item) for item in actual] != [identity(item, persisted=True) for item in items]:
        raise ValueError('Native history items do not match retained checkpoint')
    for retained, native in zip(items, actual):
        kind = retained.get('type')
        if kind in ('UserMessage', 'userMessage') and retained.get('content') != native.get('content'):
            raise ValueError('Native user history content does not match checkpoint')
        if kind in ('AgentMessage', 'agentMessage'):
            content = retained.get('content')
            text = ''.join(part['text'] for part in content) if isinstance(content, list) and all(isinstance(part, dict) and part.get('type') == 'Text' and isinstance(part.get('text'), str) for part in content) else retained.get('text')
            if text != native.get('text'):
                raise ValueError('Native assistant history content does not match checkpoint')
        if kind == 'Reasoning' and (retained.get('summary_text', []) != native.get('summary', []) or retained.get('raw_content', []) != native.get('content', [])):
            raise ValueError('Native reasoning history content does not match checkpoint')


def _publish_conversation(project, store, entry, *, state, env=None):
    agent, sid, by_id, order = _preflight(project, store, entry)
    key = _revision(entry, by_id, order)
    receipt_path = state / 'conversation-imports' / key / 'receipt.json'
    home = sessions.native_home(agent, env)
    with locked(state / 'conversation-recovery.lock', blocking=True):
        try:
            prior = read_json(receipt_path)
        except FileNotFoundError:
            prior = {}
        if prior:
            if (prior.get('project_id') != project.project_id or prior.get('identity') != project.identity
                    or prior.get('destination_root') != str(project.root) or prior.get('agent') != agent):
                raise ValueError('Native publication journal belongs to another destination')
            if prior.get('status') == 'complete':
                _check_published(prior, home, agent, project, receipt_path=receipt_path)
                return prior['result']
            _, old_sid, old_by_id, old_order = _preflight(project, store, prior['source_checkpoint'])
            if (old_sid != sid or old_order != order
                    or any(old_by_id[current][1] != by_id[current][1] for current in order)):
                raise ValueError('Native publication journal differs from retained physical checkpoint')
        receipt = dict(publication=True, source_checkpoint=entry, schema=SCHEMA, status='importing',
                       project_id=project.project_id, identity=project.identity, agent=agent,
                       source_session_id=sid, destination_root=str(project.root), files=[])
        provenance = dict(agent=agent, source_session_id=sid, source_root=entry['source_root'],
                          cutoff=entry['cutoff'], checkpoint=key)
        if agent == 'claude':
            return _publish_claude(project, store, entry, by_id, home, prior, receipt, receipt_path, provenance)
        return _publish_codex(project, store, entry, by_id, order, home, prior, receipt, receipt_path, provenance, env=env)


def _publish_codex(project, store, entry, by_id, order, home, prior, receipt, receipt_path, provenance, *, env):
    """Clone only the selected metadata envelope; ancestors remain byte exact."""
    index = sessions._codex_index(home)
    previous = {item.get('source_session_id'): item for item in prior.get('files', []) if not item.get('native_fork')}
    prior_fork = next((item for item in prior.get('files', []) if item.get('native_fork')), None)
    new = sessions.session_uuid(prior_fork['session_id']) if prior_fork else str(uuid.uuid4())
    item, original, _ = by_id[entry['session_id']]
    fork_target = _codex_target(home, item['path'].replace(entry['session_id'], new))
    parts = original.splitlines(keepends=True)
    head = json.loads(parts[0])
    head['payload'].update(id=new, session_id=new, cwd=str(project.root), runtime_workspace_roots=[str(project.root)])
    # Preserve every inherited record byte and all history_base offsets.
    fork_data = json.dumps(head, ensure_ascii=False, separators=(',', ':')).encode() + b'\n' + b''.join(parts[1:])
    # Bind every prior file/fork intent before any native repair or write.
    for saved in prior.get('files', []):
        if saved.get('native_fork'):
            expected_target, expected_data = fork_target, fork_data
        else:
            current = saved.get('source_session_id')
            if current not in order[:-1]:
                raise ValueError('Native ancestor journal differs from retained history')
            item, expected_data, _ = by_id[current]
            expected_target = index.get(current, _codex_target(home, item['path']))
        if (saved.get('destination') != str(expected_target) or saved.get('size') != len(expected_data)
                or saved.get('sha256') != hashlib.sha256(expected_data).hexdigest()):
            raise ValueError('Native publication intent differs from retained history')
        if saved.get('staged_path'):
            if not _publish(saved, check_only=True):
                raise ValueError('Journaled native stage disappeared')
    if prior_fork:
        fork_target = _reconcile_owned_publication(prior_fork, fork_target, fork_data)
        _codex_projection_ready(fork_target, prior_fork, sorted(set(entry['scope_roots']) | {str(project.root)}))
    targets = []
    inherited = None
    baselines = []
    # Preflight the complete ancestor closure before publishing any new file.
    for current in order[:-1]:
        item, data, rows = by_id[current]
        inherited, _ = _codex_turn(rows, inherited)
        target = index.get(current, _codex_target(home, item['path']))
        saved = previous.get(current)
        owned = saved if saved and saved.get('created') is True else _owned_staging(project, store, receipt_path.parents[2], target, current, data)
        if owned and owned.get('staged_path'):
            _publish(owned)
        target = sessions._safe(target)
        if target.exists():
            appending = owned and owned.get('append_from') is not None
            existing = _finish_append(owned, data, check_only=True) if appending else _stable_prefix(target)
            if len(existing) < len(data):
                if not owned or not data.startswith(existing):
                    raise ValueError('Existing native ancestor is not an owned extendable prefix')
                if not appending:
                    sessions.assert_quiescent([target])
                    with _parent(target) as (parent, name):
                        _owned_content(parent, name, owned)
            elif existing[:len(data)] != data:
                raise ValueError('Existing native ancestor identity diverges from checkpoint')
        targets.append((current, target, owned, inherited))
    def persist():
        atomic_write(receipt_path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
    for current, target, owned, cutoff in targets:
        item, data, _ = by_id[current]
        base = dict(destination=str(target), size=len(data), sha256=item['sha256'], scope_roots=item['scope_roots'], source_session_id=current, created=False)
        if owned and owned.get('append_from') is not None and target.stat().st_size <= len(data):
            _finish_append(owned, data)
        if not target.exists():
            def journal(intent):
                receipt['files'].append(dict(base, **intent, created=True))
                persist()
            _create(target, data, journal=journal)
        elif target.stat().st_size < len(data):
            intent = dict(base, created=True, inode=owned['inode'], append_from=owned['size'], previous_sha256=owned['sha256'])
            receipt['files'].append(intent)
            persist()
            _finish_append(intent, data)
            # Finalize this intent only after the complete suffix is fsynced.
            # Interrupted journals retain append_from for exact-prefix retry.
            receipt['files'][-1] = dict(base, created=True, inode=intent['inode'])
            persist()
        else:
            info = target.stat(follow_symlinks=False)
            receipt['files'].append(dict(base, created=bool(owned), inode=[info.st_dev, info.st_ino]))
            persist()
        if receipt['files'][-1]['created']:
            baselines.append(dict(agent='codex', session_id=current, turn_id=cutoff))
    target, data = fork_target, fork_data
    if prior_fork:
        receipt['files'].append(prior_fork)
    else:
        def journal(intent):
            receipt['files'].append(dict(intent, created=True, native_fork=True, session_id=new,
                                         scope_roots=sorted(set(entry['scope_roots']) | {str(project.root)})))
            persist()
        _create(target, data, journal=journal)
    receipt['fork_path'] = str(target)
    receipt['result'] = dict(session_id=new, provenance=provenance)
    receipt['baselines'] = baselines + [dict(agent='codex', session_id=new, turn_id=entry['cutoff']['turn_id'])]
    persist()
    if prior_fork:
        _codex_projection_ready(target, prior_fork, sorted(set(entry['scope_roots']) | {str(project.root)}))
    with agents.CodexClient(project, env=env) as client:
        # Only this new, journal-owned identity is loaded once for native history
        # projection. Completed publications never resume on later cycles.
        response = client.call('thread/resume', dict(threadId=new, **agents._runtime(project)))
        if response.get('thread', {}).get('id') != new:
            raise ValueError('Native import reconciliation selected the wrong identity')
        _verify_history(agents._history(client, new), _expected(by_id, order))
        _verify_discovery(client, new, project)
    receipt['status'] = 'complete'
    atomic_write(receipt_path, json.dumps(receipt, sort_keys=True).encode() + b'\n', mode=0o400)
    return receipt['result']


def _revision(entry, by_id, order):
    """Content identity survives picker titles, mtimes and native fork envelopes."""
    content = []
    for sid in order:
        for row in by_id[sid][2]:
            if entry['agent'] == 'codex':
                if row['type'] == 'session_meta':
                    continue
                if row['type'] == 'event_msg' and row['payload'].get('type') == 'thread_settings_applied':
                    continue
                value = dict(type=row['type'], payload=row['payload'])
            else:
                if row['type'] in ('summary', 'custom-title', 'tag', 'agent-name', 'agent-color'):
                    continue
                value = {key: child for key, child in row.items() if key not in ('sessionId', 'cwd', 'timestamp', 'version', 'gitBranch', 'promptId')}
            content.append(value)
    return hashlib.sha256(json.dumps(dict(agent=entry['agent'], project_id=entry['project_id'], turn_id=entry['cutoff']['turn_id'], content=content), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _publication_receipts(project, state):
    if state is None:
        return
    for path in (Path(state) / 'conversation-imports').glob('*/receipt.json'):
        receipt = read_json(path)
        if (receipt.get('publication') and receipt.get('project_id') == project.project_id
                and receipt.get('identity') == project.identity):
            yield receipt


def _unchanged_import(project, retained, state, entry):
    for receipt in _publication_receipts(project, state):
        for baseline in receipt.get('baselines', []):
            if baseline.get('agent') == entry['agent'] and baseline.get('session_id') == entry['session_id']:
                # A completed-turn advance is the only reason to export an import.
                # Still collect and fully validate its dependency closure first.
                if baseline.get('turn_id') == entry['cutoff']['turn_id']:
                    data = retained[entry['session_id']]
                    for owned in receipt.get('files', []):
                        if (Path(owned['destination']).name.endswith(entry['session_id'] + '.jsonl')
                                and len(data) >= owned['size']
                                and hashlib.sha256(data[:owned['size']]).hexdigest() == owned['sha256']):
                            return True
    return False


def _claude_materialized(data, sid, destination):
    rows = sessions._records(data, 'claude')
    for row in rows:
        if 'sessionId' in row:
            row['sessionId'] = sid
        if 'cwd' in row:
            row['cwd'] = str(destination)
    # Inline request/shape/cache payloads are deliberately untouched.
    return b''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode() + b'\n' for row in rows)


def _published_claude_scopes(receipt, path, data, store, destinations):
    result = receipt.get('result', {})
    destination = receipt.get('destination_root')
    if (destination not in destinations or path.name != str(result.get('session_id')) + '.jsonl'
            or path.parent.name != sessions.claude_namespace(destination)):
        return set()
    source = receipt.get('source_checkpoint', {})
    _, sid, by_id, _ = _preflight_proxy(receipt, store, source)
    original = by_id[sid][1]
    expected = _claude_materialized(original, result['session_id'], destination)
    if data[:len(expected)] != expected:
        raise ValueError('Published Claude inherited history changed')
    roots = {destination}
    for row in by_id[sid][2]:
        if 'cwd' in row:
            roots.add(row['cwd'])
        attachment = row.get('attachment', {})
        if attachment.get('type') == 'environment':
            roots.add(attachment['snapshot']['workingDirectory'])
            roots.update(attachment['snapshot'].get('additionalWorkingDirectories', []))
    return roots


def _preflight_proxy(receipt, store, entry):
    from types import SimpleNamespace
    return _preflight(SimpleNamespace(project_id=receipt['project_id'], identity=receipt['identity']), store, entry)


def _publish_claude(project, store, entry, by_id, home, prior, receipt, receipt_path, provenance):
    previous = prior.get('files', [])
    sid = previous[0]['physical_alias'] if previous else str(uuid.uuid4())
    target = home / 'projects' / sessions.claude_namespace(project.root) / (sid + '.jsonl')
    data = _claude_materialized(by_id[entry['session_id']][1], sid, project.root)
    sessions._validate_claude(sessions._records(data, 'claude'), sid, sorted(set(entry['scope_roots']) | {str(project.root)}))
    receipt['source_checkpoint'] = entry
    receipt['baselines'] = [dict(agent='claude', session_id=sid, turn_id=entry['cutoff']['turn_id'])]
    if previous:
        intent = previous[0]
        target = _reconcile_owned_publication(intent, target, data)
        receipt['files'] = previous
    else:
        target = sessions._safe(target)
        def journal(intent):
            receipt['files'] = [dict(intent, created=True, physical_alias=sid, source_session_id=entry['session_id'])]
            atomic_write(receipt_path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
        _create(target, data, journal=journal)
    result = dict(session_id=sid, provenance=provenance)
    receipt.update(status='complete', result=result, fork_path=str(target))
    atomic_write(receipt_path, json.dumps(receipt, sort_keys=True).encode() + b'\n', mode=0o400)
    return result


def _check_published(receipt, home, agent, project, *, receipt_path):
    sid = sessions.session_uuid(receipt.get('result', {}).get('session_id'))
    path = sessions._safe(Path(receipt.get('fork_path', '')))
    if agent == 'claude':
        valid = path == home / 'projects' / sessions.claude_namespace(project.root) / (sid + '.jsonl')
    else:
        valid = path.is_relative_to(home / 'sessions') and path.name.endswith(sid + '.jsonl')
    if not valid:
        raise ValueError('Published native conversation is outside its profile')
    original_path = path
    if not path.exists():
        archived = receipt_path.with_name('archived.jsonl') if agent == 'claude' else sessions._codex_index(home).get(sid)
        if archived is None or not Path(archived).exists():
            raise ValueError('Published native conversation is missing')
        path = sessions._safe(Path(archived))
    item = next((item for item in receipt.get('files', []) if item.get('destination') == str(original_path)), None)
    if item:
        with _parent(path) as (parent, name):
            _owned_content(parent, name, item, growing=True)


def publish_conversations(project, store, snapshot_id, *, state=None, env=None):
    """Publish checked history to ordinary native resume lists, without turns."""
    from .state import state_dir
    state = Path(state) if state is not None else state_dir(project, env)
    manifest = store.load(snapshot_id)
    if (not isinstance(manifest, dict) or manifest.get('schema') != 2 or 'files' in manifest
            or manifest.get('project', {}).get('project_id') != project.project_id
            or manifest.get('project', {}).get('identity') != project.identity):
        raise ValueError('Publication requires a same-project conversation-only schema-2 manifest')
    conversations = manifest.get('conversations')
    if not isinstance(conversations, dict) or not isinstance(conversations.get('entries'), list):
        raise ValueError('Invalid conversation publication manifest')
    result = dict(imported=[], unchanged=[], issues=list(conversations.get('issues', [])))
    for entry in conversations['entries']:
        try:
            agent, sid, by_id, order = _preflight(project, store, entry)
            key = _revision(entry, by_id, order)
            receipt_path = state / 'conversation-imports' / key / 'receipt.json'
            try:
                prior = read_json(receipt_path)
            except FileNotFoundError:
                prior = {}
            published = _publish_conversation(project, store, entry, state=state, env=env)
            _supersede_imports(project, store, state, entry, key, env=env)
            result['unchanged' if prior.get('status') == 'complete' else 'imported'].append(dict(agent=agent, source_session_id=sid, session_id=published['session_id']))
        except (ValueError, OSError, KeyError) as exc:
            result['issues'].append(dict(agent=entry.get('agent'), session_id=entry.get('session_id'), reason=sessions.display_text(str(exc), 400)))
    return result


def _owned_staging(project, store, state, target, sid, desired):
    """Return verified historical ownership, never a newer append's intent."""
    candidates = []
    for receipt in _publication_receipts(project, state):
        for item in receipt.get('files', []):
            if (item.get('created') is True and not item.get('native_fork')
                    and item.get('source_session_id') == sid and item.get('destination') == str(target)):
                candidates.append((item, receipt))
    if not candidates:
        return None
    item, receipt = max(candidates, key=lambda candidate: candidate[0]['size'])
    if item.get('append_from') is None:
        return item
    # Older receipts can retain a completed append intent, or an interrupted
    # append can be overtaken by another source checkpoint. Its own retained
    # final bytes must be proven before it can authorize a separate new append.
    _, _, by_id, _ = _preflight(project, store, receipt['source_checkpoint'])
    retained = by_id[sid][1]
    if len(retained) != item['size'] or hashlib.sha256(retained).hexdigest() != item['sha256']:
        raise ValueError('Historical native append differs from its retained checkpoint')
    target = sessions._safe(target)
    if target.stat().st_size < item['size']:
        if receipt.get('status') == 'complete':
            raise ValueError('Completed historical native append prefix is incomplete')
        if not desired.startswith(retained):
            raise ValueError('Historical native append is not a prefix of this checkpoint')
        _finish_append(item, retained)
    with _parent(target) as (parent, name):
        _owned_content(parent, name, item, growing=True)
    # Normalize only the returned baseline; immutable historical receipts stay
    # intact, and incomplete provenance is retained until recovery succeeds.
    return {key: value for key, value in item.items() if key not in ('append_from', 'previous_sha256')}


def _finish_append(intent, data, *, check_only=False):
    """Append a journaled completed suffix only to an exclusively owned inode."""
    target = sessions._safe(Path(intent['destination']))
    if intent.get('created') is not True:
        raise ValueError('Borrowed native files cannot authorize append')
    if len(data) != intent['size'] or hashlib.sha256(data).hexdigest() != intent['sha256']:
        raise ValueError('Native append intent disagrees with immutable checkpoint')
    sessions.assert_quiescent([target])
    with _parent(target) as (parent, name):
        fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            previous = intent['append_from']
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or [info.st_dev, info.st_ino] != intent['inode']
                    or not previous <= info.st_size <= len(data)):
                raise ValueError('Native append inode ownership or size changed')
            actual = os.pread(fd, info.st_size, 0)
            if (actual != data[:info.st_size] or hashlib.sha256(actual[:previous]).hexdigest() != intent['previous_sha256']
                    or os.pread(fd, info.st_size, 0) != actual):
                raise ValueError('Native append source prefix changed')
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('Native append path changed')
            if check_only:
                return actual
            remaining = memoryview(data)[info.st_size:]
            while remaining:
                count = os.write(fd, remaining)
                if count <= 0:
                    raise OSError('Native append made no progress')
                remaining = remaining[count:]
            os.fsync(fd)
            if os.fstat(fd).st_size != len(data) or os.pread(fd, len(data), 0) != data:
                raise ValueError('Native append changed concurrently')
        finally:
            os.close(fd)


def _codex_target(home, name):
    date = re.search(r'rollout-(\d{4})-(\d{2})-(\d{2})T', name)
    return home / 'sessions' / Path(*(date.groups() if date else ('2000', '01', '01'))) / name


def _supersede_imports(project, store, state, entry, current_key, *, env):
    """Retain continued/active imports; archive only provably untouched copies."""
    agent = entry['agent']
    home = sessions.native_home(agent, env)
    with locked(state / 'conversation-recovery.lock', blocking=True):
        for path in (state / 'conversation-imports').glob('*/receipt.json'):
            if path.parent.name == current_key:
                continue
            old = read_json(path)
            if (not old.get('publication') or old.get('status') != 'complete'
                    or old.get('project_id') != project.project_id or old.get('identity') != project.identity
                    or old.get('agent') != agent or old.get('source_session_id') != entry['session_id']
                    or old.get('destination_root') != str(project.root)
                    or old.get('source_checkpoint', {}).get('cutoff', {}).get('turn_id') == entry['cutoff']['turn_id']):
                continue
            old_source = next(item for item in old['source_checkpoint']['files'] if item['session_id'] == old['source_session_id'])
            new_source = next(item for item in entry['files'] if item['session_id'] == entry['session_id'])
            if not store.read(new_source['blob']).startswith(store.read(old_source['blob'])):
                continue  # Replaying an older checkpoint never supersedes a newer import.
            marker = path.with_name('superseded.json')
            if marker.exists():
                continue
            sid = sessions.session_uuid(old['result']['session_id'])
            target = Path(old['fork_path'])
            item = next((item for item in old['files'] if item.get('destination') == str(target)), None)
            if not item:
                continue
            expected = (home / 'projects' / sessions.claude_namespace(project.root) / (sid + '.jsonl')
                        if agent == 'claude' else _codex_target(home, old_source['path'].replace(old['source_session_id'], sid)))
            if target != expected:
                raise ValueError('Owned predecessor path no longer matches its native profile')
            archive = path.with_name('archived.jsonl')
            if agent == 'claude' and target.exists() and archive.exists():
                _reconcile_links(target, archive, item)
            target = sessions._safe(target)
            if not target.exists():
                continue
            try:
                sessions.assert_quiescent([target])
            except ValueError:
                continue
            with _parent(target) as (parent, name):
                _owned_content(parent, name, item, growing=True)
            data = _stable_prefix(target)
            # Any unfinished user activity also retains the import. Native
            # Codex resume appends only settings/context, which is safe to archive.
            suffix = data[item['size']:]
            if target.stat().st_size != len(data):
                continue
            if agent == 'claude' and suffix:
                continue
            if agent == 'codex' and suffix and not _codex_metadata_only(_rows(suffix)):
                continue
            if agent == 'claude':
                archive = path.with_name('archived.jsonl')
                with _parent(target) as (source_parent, source_name), _parent(archive, create=True) as (destination_parent, destination_name):
                    try:
                        os.link(source_name, destination_name, src_dir_fd=source_parent, dst_dir_fd=destination_parent, follow_symlinks=False)
                    except FileExistsError:
                        _owned_content(destination_parent, destination_name, item, growing=True)
                    os.fsync(destination_parent)
                    _reconcile_links(archive, target, item)
            else:
                with agents.CodexClient(project, env=env) as client:
                    client.call('thread/archive', dict(threadId=sid))
                archive = sessions._codex_index(home).get(sid)
                if archive is None or not archive.is_relative_to(home / 'archived_sessions'):
                    # An older native tool may defer archive visibility. Keep
                    # its history and retry the harmless archive next cycle.
                    continue
            atomic_write(marker, json.dumps(dict(session_id=sid, superseded_by=current_key, archive=str(archive)), sort_keys=True).encode() + b'\n', mode=0o400)


def _verify_discovery(client, sid, project):
    cursor, seen = None, set()
    while True:
        params = dict(limit=100, cwd=str(project.root))
        if cursor is not None:
            params['cursor'] = cursor
        page = client.call('thread/list', params)
        if not isinstance(page.get('data'), list):
            raise ValueError('Native picker returned an invalid conversation list')
        if any(row.get('id') == sid for row in page['data']):
            return
        cursor = page.get('nextCursor')
        if not cursor:
            raise ValueError('Native resume picker does not expose this imported history yet')
        if not isinstance(cursor, str) or cursor in seen or len(seen) >= 10000:
            raise ValueError('Native picker pagination stalled or exceeded limit')
        seen.add(cursor)
