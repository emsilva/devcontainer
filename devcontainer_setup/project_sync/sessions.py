"""Strict native transcript snapshots. Historical bytes are never rewritten.

Only the selected transcript dependency closure is eligible. Native databases,
credentials, settings and global memory are deliberately outside this format.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import uuid
import unicodedata

from .state import atomic_write, locked, read_json

SCHEMA = 1
MAX_BYTES = 256 * 1024 * 1024
CODEX_RECORDS = {'session_meta', 'response_item', 'event_msg', 'turn_context', 'compacted', 'world_state', 'token_usage_record'}
CLAUDE_RECORDS = {'user', 'assistant', 'system', 'summary', 'progress', 'queue-operation', 'file-history-snapshot', 'custom-title', 'agent-name', 'agent-color', 'pr-link', 'last-prompt', 'saved_hook_context', 'bridge-pointer', 'attachment', 'atis-latch', 'api-request-shape', 'api-request-blob', 'api-request', 'cost-state', 'mode'}
MEMORY_MARKERS = (b'<memory_summary>', b'<memory_context>', b'<memory_citation>', b'<oai-mem-citation>', b'========= MEMORY_SUMMARY BEGINS', b'/.codex/memories/', b'\\.codex\\memories\\')


def session_uuid(value):
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('An exact canonical session UUID is required')
    return value


def claude_namespace(root):
    return re.sub(r'[^a-zA-Z0-9]', '-', str(root))


def native_home(agent, env=None):
    values = os.environ if env is None else env
    home = Path(values.get('HOME', str(Path.home())))
    if agent == 'codex':
        result = Path(values.get('CODEX_HOME', str(home / '.codex')))
    elif agent == 'claude':
        result = Path(values.get('CLAUDE_CONFIG_DIR', str(home / '.claude')))
    else:
        raise ValueError('Unsupported agent')
    if not result.is_absolute() or '..' in result.parts:
        raise ValueError('Native home must be absolute')
    return result


def _safe(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe native path')
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise ValueError('Symlink native paths are unsupported')
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError('Native file must be regular without hard links')
    return path


def _read(path):
    path = _safe(path)
    with path.open('rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise ValueError('Unsupported native file size/type')
        data = source.read(MAX_BYTES + 1)
        after = os.fstat(source.fileno())
    if any(getattr(before, key) != getattr(after, key) for key in ("st_ino", "st_dev", "st_size", "st_mtime_ns", "st_ctime_ns")) or len(data) != after.st_size:
        raise ValueError('Native file changed during snapshot')
    return data


def assert_quiescent(paths):
    """Refuse any process holding a selected native file, including this process.

    A loaded native app-server can append later even while presently idle. Missing
    lsof is uncertainty, not permission to proceed.
    """
    existing = [str(_safe(path)) for path in paths if Path(path).exists()]
    if not existing:
        return
    command = shutil.which('lsof')
    if not command:
        raise ValueError('Cannot establish writer quiescence: lsof is required')
    result = subprocess.run([command, '-t', '--', *existing], capture_output=True, text=True, timeout=30)
    if result.stdout.strip():
        raise ValueError('Native session has an open file / possible active writer: ' + result.stdout.strip().replace('\n', ', '))
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise ValueError('Cannot establish native writer quiescence: ' + result.stderr.strip())


def _records(data, agent):
    if not data or not data.endswith(b'\n'):
        raise ValueError('Native transcript must end at a complete JSONL record')
    if any(marker in data for marker in MEMORY_MARKERS):
        raise ValueError('Native global memory context prevents strict project-only export')
    rows = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Malformed native JSONL record') from exc
        allowed = CODEX_RECORDS if agent == 'codex' else CLAUDE_RECORDS
        if not isinstance(row, dict) or row.get('type') not in allowed:
            raise ValueError('Unsupported native record type')
        if agent == 'codex' and not isinstance(row.get('payload'), dict):
            raise ValueError('Unsupported native record payload')
        if agent == 'codex' and row['type'] == 'response_item':
            supported = {'message', 'reasoning', 'function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output', 'local_shell_call', 'web_search_call', 'image_generation_call', 'compaction', 'computer_call', 'computer_call_output', 'tool_search_call', 'tool_search_output'}
            if row['payload'].get('type') not in supported:
                raise ValueError('Unsupported native response_item variant')
        rows.append(row)
    return rows


def _validate_codex_scope(rows, roots):
    """Check structural runtime scopes, never paths mentioned in historical text."""
    def structural(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'cwd':
                    _scoped_roots(child, roots, child=True)
                elif key in ('runtime_workspace_roots', 'runtimeWorkspaceRoots', 'workspace_roots'):
                    if not isinstance(child, list):
                        raise ValueError('Unsupported native workspace scope')
                    for root in child:
                        _scoped_roots(root, roots, child=True)
                elif key == 'filesystem' and isinstance(child, str) and '<workspace_roots>' in child:
                    import xml.etree.ElementTree as ET
                    try:
                        document = ET.fromstring(child)
                    except ET.ParseError as exc:
                        raise ValueError('Unsupported native filesystem scope') from exc
                    for root in document.findall('.//workspace_roots/root'):
                        _scoped_roots(root.text, roots, child=True)
                elif isinstance(child, (dict, list)):
                    structural(child)
        elif isinstance(value, list):
            for child in value:
                structural(child)
    for row in rows:
        kind, payload = row['type'], row['payload']
        if kind in ('session_meta', 'turn_context'):
            structural({key: payload[key] for key in ('cwd', 'runtime_workspace_roots', 'runtimeWorkspaceRoots') if key in payload})
        elif kind == 'world_state':
            structural(payload.get('state', {}))
        elif kind == 'event_msg' and payload.get('type') == 'thread_settings_applied':
            structural(payload.get('thread_settings', {}))


def _validate_claude(rows, sid, roots, *, child=False):
    """Validate inline native request dependencies and structural checkout scope."""
    shapes, blobs, previous = {}, {}, []
    scoped = False
    for row in rows:
        if 'sessionId' in row and row['sessionId'] != sid:
            raise ValueError('Wrong Claude session ID')
        if 'cwd' in row:
            _scoped_roots(row['cwd'], roots, child=child)
            scoped = True
        kind = row['type']
        if kind in ('user', 'assistant'):
            message = row.get('message')
            if isinstance(message, dict) and message.get('role') == kind:
                content = message.get('content')
                if isinstance(content, str):
                    content = [dict(type='text', text=content)]
                if isinstance(content, list):
                    normalized = dict(role=kind, content=content)
                    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
                    blobs[digest] = normalized
        elif kind == 'attachment':
            attachment = row.get('attachment')
            supported = {'environment', 'model', 'total_tokens_reminder', 'session_context', 'date', 'credential_org', 'advisor_tool'}
            if not isinstance(attachment, dict) or attachment.get('type') not in supported:
                raise ValueError('Unsupported Claude attachment dependency')
            if attachment['type'] == 'environment':
                snapshot = attachment.get('snapshot')
                if not isinstance(snapshot, dict) or not isinstance(snapshot.get('additionalWorkingDirectories', []), list):
                    raise ValueError('Unsupported Claude environment project scope')
                _scoped_roots(snapshot.get('workingDirectory'), roots, child=child)
                for directory in snapshot.get('additionalWorkingDirectories', []):
                    _scoped_roots(directory, roots, child=True)
        elif kind in ('api-request-shape', 'api-request-blob'):
            key, value, known = ('shapeHash', row.get('shape'), shapes) if kind == 'api-request-shape' else ('hash', row.get('message'), blobs)
            if not isinstance(value, dict):
                raise ValueError('Invalid Claude request dependency')
            digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            if row.get(key) != digest:
                raise ValueError('Invalid Claude request dependency digest')
            known[digest] = value
        elif kind == 'api-request':
            keep, tail = row.get('keep'), row.get('tail')
            if (not isinstance(row.get('shapeHash'), str) or row['shapeHash'] not in shapes or type(keep) is not int or not 0 <= keep <= len(previous)
                    or not isinstance(tail, list) or any(not isinstance(digest, str) or digest not in blobs for digest in tail)):
                raise ValueError('Missing or invalid Claude request dependency')
            previous = previous[:keep] + tail
            params, controls = row.get('params'), row.get('cacheControl', [])
            if not isinstance(params, dict) or not isinstance(controls, list):
                raise ValueError('Invalid Claude request dependency parameters')
            request = copy.deepcopy(dict(params, **shapes[row['shapeHash']], messages=[blobs[digest] for digest in previous]))
            try:
                for control in controls:
                    path, value = control['path'], control['value']
                    if not isinstance(path, list) or not path or not isinstance(value, dict):
                        raise ValueError('Invalid Claude request dependency cache control')
                    target = request
                    for part in path:
                        if not isinstance(part, str) and (type(part) is not int or part < 0):
                            raise ValueError('Invalid Claude request dependency cache path')
                        target = target[part]
                    if not isinstance(target, dict):
                        raise ValueError('Invalid Claude request dependency cache target')
                    target['cache_control'] = value
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError('Unresolved Claude request dependency cache path') from exc
            digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            if row.get('requestDigest') != digest:
                raise ValueError('Invalid Claude request digest')
    if not scoped:
        raise ValueError('Cannot establish Claude project scope')


def _validate_retained(data, agent, sid, roots, *, child=False):
    rows = _records(data, agent)
    if agent == 'codex':
        if rows[0]['type'] != 'session_meta' or sum(row['type'] == 'session_meta' for row in rows) != 1:
            raise ValueError('Unsupported retained native metadata')
        meta = rows[0]['payload']
        session_uuid(meta.get('id'))
        _version(agent, meta.get('cli_version'))
        _validate_codex_dependencies(rows, meta, sid)
        _validate_codex_scope(rows, roots)
    else:
        _validate_claude(rows, sid, roots, child=child)
    return rows


def _version(agent, version):
    if not isinstance(version, str) or not re.fullmatch(r'(?:0\.(163|164)\.\d+|0\.164\.0-alpha\.1)' if agent == 'codex' else r'2\.1\.\d+', version):
        raise ValueError(f'Unsupported {agent} native version: {version!r}')


def _codex_index(home):
    found = {}
    for directory in ('sessions', 'archived_sessions'):
        root = home / directory
        if root.is_symlink():
            raise ValueError('Symlink native session store')
        if not root.exists():
            continue
        for path in root.rglob('*'):
            if path.is_symlink():
                raise ValueError('Symlink native session store')
            match = re.search(r'([0-9a-f-]{36})\.jsonl(?:\.(zst|gz))?$', path.name)
            if not match:
                continue
            sid = session_uuid(match[1])
            if sid in found:
                raise ValueError('Duplicate physical session ID')
            found[sid] = path
    return found


def _metadata(path):
    if path.suffix != '.jsonl':
        raise ValueError('Unsupported compressed native transcript; decompress with the native tool first')
    data = _read(path)
    rows = _records(data, 'codex')
    if rows[0]['type'] != 'session_meta' or sum(r['type'] == 'session_meta' for r in rows) != 1:
        raise ValueError('Expected one leading native session metadata record')
    meta = rows[0].get('payload')
    if not isinstance(meta, dict):
        raise ValueError('Invalid native session metadata')
    session_uuid(meta.get('id'))
    _version('codex', meta.get('cli_version'))
    return data, meta


def _validate_codex_dependencies(rows, meta, physical_id):
    if meta.get('id') != physical_id and not meta.get('history_base'):
        raise ValueError('Native root session ID does not match physical identity')
    if isinstance(meta.get('source'), dict):
        raise ValueError('Unsupported Codex subagent source dependency')
    for row in rows:
        payload = row['payload']
        name = payload.get('name', '')
        event = payload.get('type', '')
        if (isinstance(name, str) and name.rsplit('.', 1)[-1] in ('spawn_agent', 'spawn_agents_on_csv')) or (isinstance(event, str) and event.startswith('collab_agent_spawn')):
            raise ValueError('Unsupported Codex subagent dependency; transfer tree is not proven')


def _parent(meta):
    base = meta.get('history_base')
    if base is not None:
        if not isinstance(base, dict) or not isinstance(base.get('end_byte_offset'), int) or base['end_byte_offset'] < 0 or not isinstance(base.get('end_ordinal_exclusive'), int) or base['end_ordinal_exclusive'] < 0:
            raise ValueError('Unsupported history ancestor reference')
        return session_uuid(base.get('thread_id'))
    return None


def _scope(cwd, root, *, child=False):
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError('Cannot establish native project scope')
    if Path(cwd) != Path(root) and not (child and Path(cwd).is_relative_to(root)):
        raise ValueError('Native transcript belongs to another project checkout')


def display_text(value, limit=120):
    """Bound and strip terminal escape sequences and Unicode controls."""
    if not isinstance(value, str):
        return ''
    value = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', value[:8192])
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    value = ''.join(' ' if c.isspace() else c for c in value if c.isspace() or not unicodedata.category(c).startswith('C'))
    return ' '.join(value.split())[:limit]


def _catalog_metadata(data, agent, path):
    # Validation has already consumed a bounded native snapshot. Metadata never
    # reads global indexes or unrelated histories.
    rows = _records(data, agent)
    title, prompt = '', ''
    for row in rows:
        payload = row.get('payload', {}) if agent == 'codex' else row
        candidate = payload.get('title') or payload.get('thread_name') or payload.get('customTitle')
        if candidate:
            title = display_text(candidate)
        message = payload if agent == 'codex' else row.get('message', {})
        if not isinstance(message, dict):
            continue
        if not prompt and message.get('role') == 'user':
            content = message.get('content', '')
            if isinstance(content, list):
                content = ' '.join(item.get('text', '') for item in content if isinstance(item, dict) and isinstance(item.get('text', ''), str))
            prompt = display_text(content)
        if agent == 'codex' and not prompt and payload.get('type') == 'user_message':
            prompt = display_text(payload.get('message'))
    return dict(title=title or prompt or 'Untitled conversation', updated_at=path.stat().st_mtime)


def list_sessions(project, agent, home, *, state=None):
    home = Path(home)
    result = []
    if agent == 'codex':
        for sid, path in _codex_index(home).items():
            try:
                data, meta = _metadata(path)
                _scope(meta.get('cwd'), _imported_origin(project, state, path, home, data))
            except ValueError:
                continue
            result.append(dict(session_id=sid, agent=agent, cwd=meta['cwd'], version=meta['cli_version'], path=str(path), **_catalog_metadata(data, agent, path)))
    elif agent == 'claude':
        directory = home / 'projects' / claude_namespace(project.root)
        for path in directory.glob('*.jsonl'):
            try:
                sid = session_uuid(path.stem)
                data = _read(path)
                rows = _records(data, agent)
                scopes = _imported_scopes(project, state, path, home, data)
                _validate_claude(rows, sid, scopes)
            except ValueError:
                continue
            result.append(dict(session_id=sid, agent=agent, cwd=str(project.root), path=str(path), **_catalog_metadata(data, agent, path)))
    else:
        raise ValueError('Unsupported agent')
    return sorted(result, key=lambda item: (-item['updated_at'], item['session_id']))


def _git(project):
    def run(*args):
        result = subprocess.run(['git', '-C', str(project.root), *args], capture_output=True, text=True)
        return result.stdout.rstrip('\n') if result.returncode == 0 else None
    return dict(head=run('rev-parse', 'HEAD'), branch=run('symbolic-ref', '--short', 'HEAD'), status=run('status', '--porcelain'))


def _imported_origin(project, state, path, home, data):
    if state is not None:
        for receipt_path in (Path(state) / 'imports').glob('*/receipt.json'):
            receipt = read_json(receipt_path)
            if receipt.get('status') != 'complete' or receipt.get('project_id') != project.project_id:
                continue
            manifest = read_json(receipt_path.parent / 'manifest.json')
            for entry in manifest['files']:
                relative = PurePosixPath(entry['path'])
                if manifest['agent'] == 'claude' and relative.parts[0] == 'projects':
                    relative = PurePosixPath('projects', claude_namespace(project.root), *relative.parts[2:])
                if home / relative == path and data.startswith(base64.b64decode(entry['data'])):
                    return entry['origin_root']
    return str(project.root)


def _imported_scopes(project, state, path, home, data):
    roots = {str(project.root)}
    if state is not None:
        for receipt_path in (Path(state) / 'imports').glob('*/receipt.json'):
            receipt = read_json(receipt_path)
            if receipt.get('status') != 'complete' or receipt.get('project_id') != project.project_id:
                continue
            manifest = read_json(receipt_path.parent / 'manifest.json')
            for entry in manifest['files']:
                relative = PurePosixPath(entry['path'])
                if manifest['agent'] == 'claude' and relative.parts[0] == 'projects':
                    relative = PurePosixPath('projects', claude_namespace(project.root), *relative.parts[2:])
                if home / relative == path and data.startswith(base64.b64decode(entry['data'])):
                    roots.update(entry['scope_roots'])
    return sorted(roots)


def _scoped_roots(cwd, roots, child=False):
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or '..' in Path(cwd).parts:
        raise ValueError('Invalid native project scope path')
    if not isinstance(roots, list) or not roots or any(not isinstance(root, str) or not Path(root).is_absolute() or '..' in Path(root).parts for root in roots):
        raise ValueError('Invalid historical project scopes')
    for root in roots:
        if isinstance(cwd, str) and (cwd == root or (child and Path(cwd).is_relative_to(root))):
            return
    raise ValueError('Native transcript belongs to another project checkout')


def export_snapshot(project, agent, session_id, home, *, source_host, generation, agent_version=None, state=None):
    session_uuid(session_id)
    home = Path(home)
    files, ancestry, paths = [], {}, []
    version = agent_version
    if agent == 'codex':
        index = _codex_index(home)
        visiting, visited = set(), set()
        def visit(sid):
            nonlocal version
            if sid in visiting:
                raise ValueError('Cyclic history ancestor dependency')
            if sid in visited:
                return
            if sid not in index:
                raise ValueError('Missing selected session or required history ancestor: ' + sid)
            visiting.add(sid)
            path = index[sid]
            data, meta = _metadata(path)
            origin_root = _imported_origin(project, state, path, home, data)
            scopes = _imported_scopes(project, state, path, home, data)
            _scope(meta.get('cwd'), origin_root)
            # Native subagent closure cannot be reconstructed safely from opaque
            # tool responses. Fail explicitly until a native tree fixture proves it.
            _validate_codex_dependencies(_records(data, agent), meta, sid)
            _validate_codex_scope(_records(data, agent), scopes)
            parent = _parent(meta)
            if parent:
                visit(parent)
                parent_data = _read(index[parent])
                offset = meta['history_base']['end_byte_offset']
                if offset > len(parent_data) or (offset and parent_data[offset - 1:offset] != b'\n'):
                    raise ValueError('Invalid ancestor complete-record byte offset')
                ancestry[sid] = parent
            if sid == session_id:
                version = meta['cli_version']
            files.append(dict(path=path.relative_to(home).as_posix(), kind='transcript', session_id=sid, origin_root=origin_root, scope_roots=scopes, data=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest(), size=len(data)))
            paths.append(path)
            visiting.remove(sid)
            visited.add(sid)
        visit(session_id)
    elif agent == 'claude':
        _version(agent, version)
        parent = home / 'projects' / claude_namespace(project.root) / f'{session_id}.jsonl'
        paths = [parent]
        descendants = parent.with_suffix('')
        if descendants.exists():
            for path in descendants.rglob('*'):
                if path.is_file() or path.is_symlink():
                    relative = path.relative_to(descendants).as_posix()
                    if not re.fullmatch(r'subagents/agent-[A-Za-z0-9_-]+\.jsonl', relative):
                        raise ValueError('Unsupported Claude subagent state: ' + relative)
                    paths.append(path)
        tasks = home / 'tasks' / session_id
        if tasks.exists():
            for path in tasks.iterdir():
                if not re.fullmatch(r'[0-9]+\.json', path.name):
                    raise ValueError('Unsupported Claude task or lock state')
                paths.append(path)
        for path in paths:
            data = _read(path)
            origin_root = _imported_origin(project, state, path, home, data)
            scopes = _imported_scopes(project, state, path, home, data)
            kind = 'task' if path.parent == tasks else 'transcript'
            if kind == 'transcript':
                rows = _records(data, agent)
                _validate_claude(rows, session_id, scopes, child=path != parent)
            else:
                task = json.loads(data)
                if not isinstance(task, dict) or str(task.get('id')) != path.stem:
                    raise ValueError('Invalid Claude task binding')
            files.append(dict(path=path.relative_to(home).as_posix(), kind=kind, session_id=session_id, origin_root=origin_root, scope_roots=scopes, data=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest(), size=len(data)))
    else:
        raise ValueError('Unsupported agent')
    assert_quiescent(paths)
    for item, path in zip(files, paths):
        if hashlib.sha256(_read(path)).hexdigest() != item['sha256']:
            raise ValueError('Session changed during export')
    if sum(item['size'] for item in files) > MAX_BYTES:
        raise ValueError('Snapshot exceeds size limit')
    return dict(schema=SCHEMA, agent=agent, agent_version=version, session_id=session_id, project=dict(identity=project.identity, project_id=project.project_id, root=str(project.root), git=_git(project)), source_host=source_host, generation=generation, ancestry=ancestry, files=files)


def _preflight(project, agent, session_id, home, bundle):
    if not isinstance(bundle, dict) or set(bundle) != {'schema', 'agent', 'agent_version', 'session_id', 'project', 'source_host', 'generation', 'ancestry', 'files'}:
        raise ValueError('Invalid snapshot manifest schema')
    if bundle['schema'] != SCHEMA or bundle['agent'] != agent or bundle['session_id'] != session_uuid(session_id):
        raise ValueError('Wrong snapshot version, agent or session')
    _version(agent, bundle['agent_version'])
    source = bundle['project']
    if source.get('identity') != project.identity or source.get('project_id') != project.project_id:
        raise ValueError('Wrong project identity')
    if not isinstance(bundle['generation'], int) or bundle['generation'] < 0 or not bundle['source_host']:
        raise ValueError('Invalid ownership generation/source')
    if not isinstance(bundle['files'], list) or not bundle['files']:
        raise ValueError('Empty snapshot')
    seen, ids, ancestry, result = set(), set(), {}, []
    for entry in bundle['files']:
        if not isinstance(entry, dict) or set(entry) != {'path', 'kind', 'session_id', 'origin_root', 'scope_roots', 'data', 'sha256', 'size'}:
            raise ValueError('Invalid native inventory entry')
        rel = PurePosixPath(entry['path'])
        if rel.is_absolute() or '..' in rel.parts or str(rel) != entry['path'] or entry['path'] in seen:
            raise ValueError('Unsafe or duplicate native inventory path')
        seen.add(entry['path'])
        try:
            data = base64.b64decode(entry['data'], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid snapshot file encoding') from exc
        if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('Snapshot digest/size mismatch')
        sid = session_uuid(entry['session_id'])
        _scoped_roots(entry['origin_root'], entry['scope_roots'])
        if agent == 'codex':
            if entry['kind'] != 'transcript' or len(rel.parts) < 2 or rel.parts[0] not in ('sessions', 'archived_sessions') or not rel.name.endswith(sid + '.jsonl') or sid in ids:
                raise ValueError('Invalid or duplicate Codex native path/ID')
            rows = _records(data, agent)
            if rows[0]['type'] != 'session_meta' or sum(r['type'] == 'session_meta' for r in rows) != 1:
                raise ValueError('Unsupported native metadata')
            meta = rows[0].get('payload', {})
            _validate_codex_dependencies(rows, meta, sid)
            session_uuid(meta.get('id'))
            _version(agent, meta.get('cli_version'))
            _scope(meta.get('cwd'), entry['origin_root'])
            _validate_codex_scope(rows, entry['scope_roots'])
            parent = _parent(meta)
            if parent:
                ancestry[sid] = parent
            ids.add(sid)
            target_rel = rel
        else:
            prefix = ('projects', claude_namespace(source['root']))
            if rel.parts[:2] == prefix and (rel.parts[2:] == (sid + '.jsonl',) or (len(rel.parts) == 5 and rel.parts[2] == sid and rel.parts[3] == 'subagents' and re.fullmatch(r'agent-[A-Za-z0-9_-]+\.jsonl', rel.name))):
                if entry['kind'] != 'transcript' or sid != session_id:
                    raise ValueError('Wrong Claude transcript binding')
                rows = _records(data, agent)
                _validate_claude(rows, sid, entry['scope_roots'], child=len(rel.parts) == 5)
                target_rel = PurePosixPath('projects', claude_namespace(project.root), *rel.parts[2:])
                if len(rel.parts) == 3:
                    ids.add(sid)
            elif rel.parts[:2] == ('tasks', sid) and len(rel.parts) == 3 and re.fullmatch(r'[0-9]+\.json', rel.name) and entry['kind'] == 'task' and sid == session_id:
                task = json.loads(data)
                if not isinstance(task, dict) or str(task.get('id')) != rel.stem:
                    raise ValueError('Wrong Claude task binding')
                target_rel = rel
            else:
                raise ValueError('Unsupported Claude native path')
        destination = _safe(home / target_rel)
        old = _read(destination) if destination.exists() else None
        if old is not None and entry['kind'] == 'transcript':
            _validate_retained(old, agent, sid, sorted(set(entry['scope_roots']) | {str(project.root)}), child=agent == 'claude' and len(target_rel.parts) == 5)
        if old is None:
            action = 'create'
        elif old == data:
            action = 'identical'
        elif entry['kind'] == 'transcript' and data.startswith(old) and old.endswith(b'\n'):
            action = 'append'
        elif entry['kind'] == 'transcript' and old.startswith(data):
            action = 'local-ahead'
        else:
            raise ValueError('Native history/task diverges: ' + str(target_rel))
        result.append(dict(path=str(target_rel), destination=destination, data=data, old=old, action=action))
    if session_id not in ids or ancestry != bundle['ancestry'] or any(parent not in ids for parent in ancestry.values()):
        raise ValueError('Missing or inconsistent session/ancestor dependency')
    if agent == 'codex':
        closure, sid = set(), session_id
        while sid is not None:
            if sid in closure:
                raise ValueError('Cyclic ancestor closure')
            closure.add(sid)
            sid = ancestry.get(sid)
        if closure != ids:
            raise ValueError('Snapshot includes unrelated sessions outside selected dependency closure')
        by_id = {entry['session_id']: entry for entry in bundle['files']}
        for sid, parent in ancestry.items():
            meta = _records(base64.b64decode(by_id[sid]['data']), agent)[0]['payload']
            parent_data = base64.b64decode(by_id[parent]['data'])
            offset = meta['history_base']['end_byte_offset']
            if offset > len(parent_data) or (offset and parent_data[offset - 1:offset] != b'\n'):
                raise ValueError('Invalid ancestor complete-record byte offset')
    if sum(len(item['data']) for item in result) > MAX_BYTES:
        raise ValueError('Snapshot exceeds size limit')
    return result


def import_snapshot(project, agent, session_id, home, bundle, *, state):
    """All-bundle preflight, private rollback copies, atomic per-file commit.

    A receipt marked prepared is an interrupted operation, never an import ACK.
    Retrying the same bundle safely completes an interrupted compatible import.
    """
    home, state = Path(home), Path(state)
    with locked(state / 'native-import.lock'):
        plan = _preflight(project, agent, session_id, home, bundle)
        assert_quiescent(item['destination'] for item in plan)
        bundle_id = hashlib.sha256(json.dumps(bundle, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        directory = state / 'imports' / bundle_id
        receipt = dict(schema=1, bundle_id=bundle_id, agent=agent, session_id=session_id, project_id=project.project_id, source_host=bundle['source_host'], generation=bundle['generation'], status='prepared', files=[dict(path=item['path'], action=item['action']) for item in plan])
        for index, item in enumerate(plan):
            if item['action'] in ('create', 'append'):
                backup = directory / 'backup' / str(index)
                if item['old'] is not None and not backup.exists():
                    atomic_write(backup, item['old'])
                atomic_write(directory / 'staged' / str(index), item['data'])
        atomic_write(directory / 'manifest.json', json.dumps(bundle, sort_keys=True).encode() + b'\n')
        atomic_write(directory / 'receipt.json', json.dumps(receipt, sort_keys=True).encode() + b'\n')
        for item in plan:
            path = item['destination']
            current = _read(path) if path.exists() else None
            if current != item['old']:
                raise ValueError('Destination changed after preflight; recovery receipt retained')
            if item['action'] in ('create', 'append'):
                assert_quiescent([path])
                atomic_write(path, item['data'])
        receipt['status'] = 'complete'
        atomic_write(directory / 'receipt.json', json.dumps(receipt, sort_keys=True).encode() + b'\n')
        return receipt
