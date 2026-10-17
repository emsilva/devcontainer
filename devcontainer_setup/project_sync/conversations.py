"""Read-only native checkpoints and new-identity, project-scoped recovery.

Native writers may keep appending while we retain a checked immutable prefix.
Recovery never resumes an existing source identity: only imported, absent copies
are reconciled before the native tool forks a new conversation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
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


def _scopes(project, state):
    scopes = {str(project.root)}
    if state is not None:
        try:
            registry = read_json(Path(state) / 'workspace.json')
        except FileNotFoundError:
            registry = {}
        if registry:
            if registry.get('project_id') != project.project_id or registry.get('identity') != project.identity:
                raise ValueError('Workspace registry belongs to another project')
            for key in ('local_root', 'local_active_root', 'remote_root', 'remote_active_root'):
                value = registry.get(key)
                if value is not None:
                    sessions._scoped_roots(value, [value])
                    scopes.add(value)
    return sorted(scopes)


def _claude_child_scopes(project, receipt, path, data, store, destination_roots):
    """Authorize historical roots only through an owned native fork's history."""
    if receipt.get('schema') != SCHEMA or receipt.get('status') != 'complete' or receipt.get('agent') != 'claude':
        return set()
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
            for item in receipt.get('files', []):
                if str(path) != item.get('destination'):
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
        files.append(dict(session_id=current, path=path.name, size=len(kept), sha256=hashlib.sha256(kept).hexdigest(), scope_roots=approved, origin_root=meta['cwd']))
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
    for item in files:
        item['blob'] = store.put(retained[item['session_id']])
    meta = _rows(retained[sid])[0]['payload']
    return dict(schema=SCHEMA, agent='codex', session_id=sid, project_id=project.project_id, identity=project.identity, source_root=meta['cwd'], scope_roots=sorted({root for item in files for root in item['scope_roots']}), cutoff=dict(turn_id=cutoff, bytes=len(retained[sid])), files=files, title=sessions._catalog_metadata(retained[sid], 'codex', index[sid])['title'], updated_at=index[sid].stat().st_mtime)


def _claude_entry(project, store, sid, path, *, state, scopes):
    if any(path.with_suffix('').rglob('*')):
        raise ValueError('Claude subagent dependency closure is not proven for live checkpoints')
    home = path.parents[2]
    if any((home / 'tasks' / sid).glob('*')):
        raise ValueError('Claude task dependency closure is not proven for live checkpoints')
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
    if any(path.with_suffix('').rglob('*')) or any((home / 'tasks' / sid).glob('*')):
        raise ValueError('Claude live dependency closure changed during checkpoint')
    origin = next(row['cwd'] for row in reversed(valid) if 'cwd' in row)
    return dict(schema=SCHEMA, agent='claude', session_id=sid, project_id=project.project_id, identity=project.identity, source_root=origin, scope_roots=approved, cutoff=dict(turn_id=cutoff, bytes=len(kept)), files=[dict(session_id=sid, path=path.name, size=len(kept), sha256=hashlib.sha256(kept).hexdigest(), blob=store.put(kept), scope_roots=approved, origin_root=origin)], title=sessions._catalog_metadata(kept, 'claude', path)['title'], updated_at=path.stat().st_mtime)


def _import_aliases(project, state):
    aliases = set()
    if state is not None:
        for receipt_path in (Path(state) / 'conversation-imports').glob('*/receipt.json'):
            receipt = read_json(receipt_path)
            if receipt.get('project_id') == project.project_id and receipt.get('identity') == project.identity:
                aliases.update(item['destination'] for item in receipt.get('files', []) if item.get('physical_alias'))
    return aliases


def capture_conversations(project, store, *, state=None, env=None):
    """Observe ordinary project sessions without a native process or writer lock."""
    scopes = _scopes(project, state)
    result = dict(schema=SCHEMA, entries=[], issues=[])
    def issue(agent, sid, error):
        result['issues'].append(dict(agent=agent, session_id=sid, reason=sessions.display_text(str(error), 400)))
    try:
        index = sessions._codex_index(sessions.native_home('codex', env))
    except (ValueError, OSError) as exc:
        index = {}
        issue('codex', None, exc)
    for sid, path in index.items():
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
            approved = sorted(set(scopes) | set(_receipt_scopes(project, state, path, first, store)))
            if meta.get('cwd') not in approved:
                continue
            result['entries'].append(_codex_entry(project, store, sid, index, state=state, scopes=scopes))
        except (ValueError, OSError) as exc:
            issue('codex', sid, exc)
    home = sessions.native_home('claude', env)
    aliases = _import_aliases(project, state)
    for root in scopes:
        directory = home / 'projects' / sessions.claude_namespace(root)
        if directory.is_symlink():
            issue('claude', None, ValueError('Symlink native project store'))
            continue
        for path in directory.glob('*.jsonl'):
            if str(path) in aliases:
                continue
            sid = path.stem
            try:
                sessions.session_uuid(sid)
                result['entries'].append(_claude_entry(project, store, sid, path, state=state, scopes=scopes))
            except (ValueError, OSError) as exc:
                issue('claude', sid, exc)
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


def _publish(intent):
    """Finish only a durably journaled inode; link never replaces a final path."""
    target, stage = Path(intent['destination']), Path(intent['staged_path'])
    if (stage.parent != target.parent or not re.fullmatch(r'\.devcontainer-import-[0-9a-f]{32}\.tmp', stage.name)):
        raise ValueError('Invalid native publication staging path')
    with _parent(target, create=True) as (parent, name):
        def present(filename):
            try:
                return os.stat(filename, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
        final, staged = present(name), present(stage.name)
        if final is not None:
            final = _owned_content(parent, name, intent, growing=True)
            if staged is not None:
                _owned_content(parent, stage.name, intent)
                os.unlink(stage.name, dir_fd=parent)
                os.fsync(parent)
            elif final.st_nlink != 1:
                raise ValueError('Native publication has an unowned hard link')
            return True
        if staged is None:
            return False  # A failed prepublication journal may have no file.
        _owned_content(parent, stage.name, intent)
        os.link(stage.name, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
        os.unlink(stage.name, dir_fd=parent)
        os.fsync(parent)
        return True


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
            if not _publish(intent):
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


def _repair_publications(prior, home, agent, project, by_id):
    for item in prior.get('files', []):
        if not item.get('created') or not item.get('staged_path'):
            continue
        sid = item.get('source_session_id')
        if sid not in by_id or item.get('sha256') != by_id[sid][0]['sha256'] or item.get('size') != len(by_id[sid][1]):
            raise ValueError('Publication intent does not match the retained checkpoint')
        target = Path(item['destination'])
        if agent == 'codex':
            valid = target.is_relative_to(home / 'sessions') and target.name == by_id[sid][0]['path']
        else:
            alias = sessions.session_uuid(item.get('physical_alias'))
            valid = target == home / 'projects' / sessions.claude_namespace(project.root) / (alias + '.jsonl')
        if not valid:
            raise ValueError('Native publication target is outside its owned profile scope')
        _publish(item)


def _codex_argv(project, sid):
    return ['codex', 'resume', '--cd', str(project.root), '-c', 'features.memories=false', '-c', 'memories.use_memories=false', '-c', 'memories.generate_memories=false', sid]


def _claude_argv(project, *, sid=None, parent=None):
    argv = ['claude', '--system-prompt-snapshot', 'off', '--append-system-prompt', agents.project_context(project)]
    if parent:
        argv += ['--resume', parent, '--fork-session', '--session-id', sid]
    elif sid:
        argv += ['--resume', sid]
    return argv


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
    def identity(item):
        kind = item.get('type', '')
        return item.get('id'), kind[:1].lower() + kind[1:]
    if [identity(item) for item in actual] != [identity(item) for item in items]:
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


def restore_conversation(project, store, entry, *, state=None, env=None):
    """Fork checked historical bytes using the destination's ordinary profile."""
    from .state import state_dir
    state = Path(state) if state is not None else state_dir(project, env)
    agent, sid, by_id, order = _preflight(project, store, entry)
    key = hashlib.sha256(json.dumps(dict(entry=entry, destination_root=str(project.root)), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    directory = state / 'conversation-imports' / key
    receipt_path = directory / 'receipt.json'
    home = sessions.native_home(agent, env)
    with locked(state / 'conversation-recovery.lock', blocking=True):
        try:
            prior = read_json(receipt_path)
        except FileNotFoundError:
            prior = {}
        if prior.get('status') == 'complete' and prior.get('project_id') == project.project_id and prior.get('identity') == project.identity and prior.get('destination_root') == str(project.root):
            result = dict(prior['result'])
            if agent == 'claude':
                parent_item = next(item for item in prior['files'] if item.get('physical_alias'))
                child = sessions._safe(home / 'projects' / sessions.claude_namespace(project.root) / (result['session_id'] + '.jsonl'))
                if child.exists():
                    sessions.assert_quiescent([child])
                    result['argv'] = _claude_argv(project, sid=result['session_id'])
                else:
                    parent_path = Path(parent_item['destination'])
                    data = _stable_prefix(parent_path)
                    if len(data) != parent_item['size'] or hashlib.sha256(data).hexdigest() != parent_item['sha256'] or parent_path.stat().st_size != len(data):
                        raise ValueError('Imported Claude fork source changed; original checkpoint preserved')
                    result['argv'] = _claude_argv(project, sid=result['session_id'], parent=parent_item['physical_alias'])
            else:
                path = prior.get('fork_path')
                if not path or not Path(path).exists():
                    raise ValueError('Previously recovered native conversation is missing; preserved checkpoint remains available')
                sessions.assert_quiescent([Path(path)])
                with agents.CodexClient(project, env=env) as client:
                    client.call('thread/resume', dict(threadId=result['session_id'], **agents._runtime(project)))
                    client.call('thread/inject_items', dict(threadId=result['session_id'], items=[dict(type='message', role='developer', content=[dict(type='input_text', text=agents.project_context(project))])]))
            return result
        prior_import = (prior.get('status') == 'importing' and prior.get('project_id') == project.project_id
                        and prior.get('identity') == project.identity and prior.get('destination_root') == str(project.root))
        if prior_import:
            _repair_publications(prior, home, agent, project, by_id)
        receipt = dict(schema=SCHEMA, status='importing', project_id=project.project_id, identity=project.identity, agent=agent, source_session_id=sid, destination_root=str(project.root), files=[])
        provenance = dict(agent=agent, source_session_id=sid, source_root=entry['source_root'], cutoff=entry['cutoff'], checkpoint=key)
        if agent == 'claude':
            # A physical alias gives the native fork its bounded source without
            # replacing or reading beyond any active same-ID original.
            previous_alias = next((item for item in prior.get('files', []) if item.get('physical_alias') and item.get('source_session_id') == sid), None) if prior_import else None
            alias = previous_alias['physical_alias'] if previous_alias else str(uuid.uuid4())
            target = sessions._safe(home / 'projects' / sessions.claude_namespace(project.root) / (alias + '.jsonl'))
            item, data, _ = by_id[sid]
            base = dict(destination=str(target), physical_alias=alias, source_session_id=sid, size=len(data), sha256=item['sha256'], scope_roots=item['scope_roots'], created=True)
            if target.exists():
                if not previous_alias or _stable_prefix(target) != data or target.stat().st_size != len(data):
                    raise ValueError('Existing Claude import alias is not the owned checkpoint')
                receipt['files'].append(previous_alias)
            else:
                def journal(intent):
                    receipt['files'].append(dict(base, **intent))
                    atomic_write(receipt_path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
                _create(target, data, mode=0o400, journal=journal)
            new = str(uuid.uuid4())
            result = dict(session_id=new, argv=_claude_argv(project, sid=new, parent=alias), env={'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'}, provenance=provenance)
            receipt.update(status='complete', result=result)
            atomic_write(receipt_path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
            return result
        index = sessions._codex_index(home)
        targets, newly_created, reconcile = {}, [], []
        prior_owned = {}
        if (prior.get('status') == 'importing' and prior.get('project_id') == project.project_id
                and prior.get('identity') == project.identity and prior.get('destination_root') == str(project.root)):
            prior_owned = {item['destination']: item for item in prior.get('files', []) if item.get('created')}
        # Preflight every existing identity before creating even one import.
        for current in order:
            item, data, _ = by_id[current]
            if current in index:
                target = sessions._safe(index[current])
                if _stable_prefix(target, len(data)) != data:
                    raise ValueError('Existing native source identity diverges from checkpoint')
                owned = prior_owned.get(str(target))
                info = target.stat(follow_symlinks=False)
                if owned and [info.st_dev, info.st_ino] == owned.get('inode') and owned.get('sha256') == item['sha256']:
                    sessions.assert_quiescent([target])
                    reconcile.append(current)
            else:
                name = item['path']
                date = re.search(r'rollout-(\d{4})-(\d{2})-(\d{2})T', name)
                parts = date.groups() if date else ('2000', '01', '01')
                target = sessions._safe(home / 'sessions' / Path(*parts) / name)
                if target.exists():
                    raise ValueError('Native import destination exists outside the verified index')
                newly_created.append(current)
                reconcile.append(current)
            targets[current] = target
        # Journal each durable import before invoking the native writer. A retry
        # may reconcile only the exact inode we created, never a foreign source.
        for current in order:
            item, data, _ = by_id[current]
            base = dict(destination=str(targets[current]), size=len(data), sha256=item['sha256'], scope_roots=item['scope_roots'], source_session_id=current, created=current in reconcile)
            if current in newly_created:
                def journal(intent):
                    receipt['files'].append(dict(base, **intent))
                    atomic_write(receipt_path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
                _create(targets[current], data, journal=journal)
            else:
                info = targets[current].stat(follow_symlinks=False)
                receipt['files'].append(dict(base, inode=[info.st_dev, info.st_ino]))
                atomic_write(receipt_path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
        expected = _expected(by_id, order)
        with agents.CodexClient(project, env=env) as client:
            for current in reconcile:
                # Only our absent imports may acquire native runtime settings.
                resumed = client.call('thread/resume', dict(threadId=current, **agents._runtime(project)))
                if resumed.get('thread', {}).get('id') != current:
                    raise ValueError('Native import reconciliation selected the wrong identity')
            history = agents._history(client, sid)
            retained_turns = set(expected[0])
            prefix = dict(turns=history.get('turns', [])[:len(expected[0])], items=[item for item in history.get('items', []) if item.get('turnId') in retained_turns])
            _verify_history(prefix, expected)
            response = client.call('thread/fork', dict(threadId=sid, path=str(targets[sid]), lastTurnId=entry['cutoff']['turn_id'], deferGoalContinuation=True, **agents._runtime(project)))
            thread = response.get('thread', {})
            new = sessions.session_uuid(thread.get('id'))
            if new in by_id or thread.get('cwd') != str(project.root) or response.get('runtimeWorkspaceRoots') != [str(project.root)]:
                raise ValueError('Native fork selected wrong identity or runtime roots')
            _verify_history(agents._history(client, new), expected)
            client.call('thread/inject_items', dict(threadId=new, items=[dict(type='message', role='developer', content=[dict(type='input_text', text=agents.project_context(project))])]))
        result = dict(session_id=new, argv=_codex_argv(project, new), env={}, provenance=provenance)
        receipt.update(status='complete', result=result, fork_path=thread.get('path'))
        atomic_write(receipt_path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
        return result


def _new_codex(project, *, env=None):
    with agents.CodexClient(project, env=env) as client:
        response = client.call('thread/start', dict(historyMode='paginated', **agents._runtime(project)))
        sid = sessions.session_uuid(response.get('thread', {}).get('id'))
        client.call('thread/inject_items', dict(threadId=sid, items=[dict(type='message', role='developer', content=[dict(type='input_text', text=agents.project_context(project))])]))
    return sid


def _resume_local(project, entry, *, env=None):
    agent, sid = entry['agent'], entry['session_id']
    home = sessions.native_home(agent, env)
    if agent == 'codex':
        index = sessions._codex_index(home)
        sessions.assert_quiescent([index[sid]])
        with agents.CodexClient(project, env=env) as client:
            response = client.call('thread/resume', dict(threadId=sid, **agents._runtime(project)))
            if response.get('thread', {}).get('id') != sid:
                raise ValueError('Native local resume selected the wrong identity')
            client.call('thread/inject_items', dict(threadId=sid, items=[dict(type='message', role='developer', content=[dict(type='input_text', text=agents.project_context(project))])]))
        return dict(session_id=sid, argv=_codex_argv(project, sid), env={})
    directory = home / 'projects'
    candidates = [path for path in directory.glob('*/' + sid + '.jsonl') if path.parent.name in {sessions.claude_namespace(root) for root in entry['scope_roots']}]
    if len(candidates) != 1:
        raise ValueError('Cannot establish exact local Claude session identity')
    sessions.assert_quiescent(candidates)
    # Native Claude only resumes within its current project namespace. A prior
    # checkout is recovered through a bounded new-identity fork instead.
    if candidates[0].parent.name != sessions.claude_namespace(project.root):
        return None
    return dict(session_id=sid, argv=_claude_argv(project, sid=sid), env={'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'})


def launch(project, state, agent, *, resume=False, env=None):
    """Start fresh by default; resume selects only this project's checked history."""
    from .checkpoint_store import Store
    from .terminal import Gum
    if agent not in ('codex', 'claude'):
        raise ValueError('Unsupported project agent')
    values = dict(os.environ if env is None else env)
    store = Store(Path(state))
    result = None
    if resume:
        native = capture_conversations(project, store, state=state, env=values)
        choices = []
        seen = set()
        for item in native['entries']:
            if item['agent'] == agent:
                choices.append((item, 'This machine', True))
                seen.add(item['session_id'])
        for source in ('codespace', 'desktop'):
            latest = store.latest(source)
            if latest:
                manifest = store.load(latest['snapshot_id'])
                if manifest.get('project', {}).get('project_id') != project.project_id or manifest.get('project', {}).get('identity') != project.identity:
                    raise ValueError('Acknowledged conversation checkpoint belongs to another project')
                for item in manifest.get('conversations', {}).get('entries', []):
                    if item.get('agent') == agent and item.get('session_id') not in seen:
                        choices.append((item, 'Recovery checkpoint', False))
                        seen.add(item['session_id'])
        if not choices:
            for issue in native['issues']:
                if issue['agent'] == agent:
                    print('Conversation unavailable:', issue['reason'])
            raise ValueError('No recoverable completed project conversations yet; start a new conversation first')
        labels = [f"{index + 1}. {sessions.display_text(item.get('title'), 80) or 'Untitled conversation'} · {origin} · {item['session_id'][-8:]}" for index, (item, origin, _) in enumerate(choices)]
        selected = Gum().choose('Resume a project conversation', labels)
        item, _, local = choices[labels.index(selected)]
        if local:
            result = _resume_local(project, item, env=values)
        if result is None:
            result = restore_conversation(project, store, item, state=state, env=values)
    elif agent == 'codex':
        sid = _new_codex(project, env=values)
        result = dict(session_id=sid, argv=_codex_argv(project, sid), env={})
    else:
        sid = str(uuid.uuid4())
        result = dict(session_id=sid, argv=[*_claude_argv(project), '--session-id', sid], env={'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'})
    values.update(result.get('env', {}))
    return subprocess.run(result['argv'], cwd=project.root, env=values).returncode
