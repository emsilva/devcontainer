"""Validated native transcript formats; no custody or same-ID import protocol."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid
import unicodedata

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
    def personal_memory(value):
        if isinstance(value, str):
            return any(marker.decode() in value for marker in MEMORY_MARKERS)
        if isinstance(value, dict):
            return any(personal_memory(key) or personal_memory(child) for key, child in value.items())
        if isinstance(value, list):
            return any(personal_memory(child) for child in value)
        return False
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Malformed native JSONL record') from exc
        allowed = CODEX_RECORDS if agent == 'codex' else CLAUDE_RECORDS
        if not isinstance(row, dict) or row.get('type') not in allowed:
            raise ValueError('Unsupported native record type')
        if personal_memory(row):
            raise ValueError('Native global memory context prevents strict project-only export')
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
                texts = [item.get('text', '') for item in content
                         if isinstance(item, dict) and isinstance(item.get('text', ''), str)]
                if agent == 'codex':
                    # Native Codex serializes its instruction/environment envelope
                    # as a user message before the developer's actual question.
                    texts = [text for text in texts if not text.lstrip().startswith(
                        ('# AGENTS.md instructions for ', '<environment_context>'))]
                content = ' '.join(texts)
            prompt = display_text(content)
        if agent == 'codex' and not prompt and payload.get('type') == 'user_message':
            prompt = display_text(payload.get('message'))
    return dict(title=title or prompt or 'Untitled conversation', updated_at=path.stat().st_mtime)


def _scoped_roots(cwd, roots, child=False):
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or '..' in Path(cwd).parts:
        raise ValueError('Invalid native project scope path')
    if not isinstance(roots, list) or not roots or any(not isinstance(root, str) or not Path(root).is_absolute() or '..' in Path(root).parts for root in roots):
        raise ValueError('Invalid historical project scopes')
    for root in roots:
        if isinstance(cwd, str) and (cwd == root or (child and Path(cwd).is_relative_to(root))):
            return
    raise ValueError('Native transcript belongs to another project checkout')
