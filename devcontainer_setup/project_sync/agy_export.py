"""Read-only Antigravity CLI (agy) conversation exporter into host-scoped .scratch/history/agy/."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import quote, unquote, urlsplit

from .state import _parent, _regular, atomic_write

HOSTS = ('mac', 'codespace')
MAX_CONVERSATIONS = 20
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_EXPORT_BYTES = 96 * 1024
MAX_TURN_CHARS = 24_000
MAX_FILES_TOUCHED = 25
_CONVERSATION_ID = re.compile(r'^[0-9A-Za-z_-]{8,128}$')
_USER_REQUEST_RE = re.compile(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', re.DOTALL)
_STRIP_TAGS_RE = re.compile(
    r'<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE|CONTEXT_SUMMARY|EPHEMERAL_MESSAGE)\b[^>]*>.*?</\1>',
    re.DOTALL,
)
_CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_FORMAT_VERSION = 1


def agy_home(env: Mapping | None = None) -> Path:
    """Resolve the Antigravity CLI app data directory without following traversal."""
    values = os.environ if env is None else env
    for key in ('ANTIGRAVITY_CLI_HOME', 'AGY_HOME'):
        override = values.get(key, '')
        if override:
            candidate = Path(override)
            if candidate.is_absolute() and '..' not in candidate.parts:
                return candidate
    home = Path(values.get('HOME') or Path.home())
    if not home.is_absolute() or '..' in home.parts:
        raise ValueError('Home directory must be absolute without traversal')
    return home / '.gemini/antigravity-cli'


def _clean_text(text: str) -> str:
    return _CONTROL_RE.sub('', text.replace('\r\n', '\n').replace('\r', '\n')).strip()


def _single_line(text: str, limit: int = 120) -> str:
    collapsed = re.sub(r'\s+', ' ', _clean_text(text)).replace('|', '\\|')
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)].rstrip() + '…'


def _format_timestamp(value) -> str:
    if value is None or value == '':
        return ''
    if isinstance(value, (int, float)):
        # Handle seconds, milliseconds, or microseconds epochs cleanly.
        seconds = float(value)
        if seconds > 1e14:
            seconds /= 1_000_000.0
        elif seconds > 1e11:
            seconds /= 1_000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        except (OSError, OverflowError, ValueError):
            return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ''
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        except ValueError:
            return _single_line(text, 40)
    return _single_line(str(value), 40)


def _uri_to_path(uri: str) -> Path | None:
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith('file://'):
        parsed = urlsplit(uri)
        if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
            return None
        raw_path = unquote(parsed.path)
    elif uri.startswith('/'):
        raw_path = uri
    else:
        return None
    if not raw_path or not raw_path.startswith('/'):
        return None
    path = Path(raw_path)
    if '..' in path.parts:
        return None
    return path


def _parse_workspace_uris(raw) -> list[Path]:
    if raw is None or raw == '':
        return []
    items = raw
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except ValueError:
            items = [raw]
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return []
    paths = []
    for item in items:
        converted = _uri_to_path(item)
        if converted is not None:
            paths.append(converted)
    return paths


def _matching_scope(uris: list[Path], scopes: list[Path]) -> Path | None:
    matched: list[Path] = []
    for uri_path in uris:
        candidates = [uri_path]
        try:
            if uri_path.exists():
                resolved = uri_path.resolve()
                if resolved != uri_path:
                    candidates.append(resolved)
        except OSError:
            pass
        for candidate in candidates:
            for scope in scopes:
                if candidate == scope or candidate.is_relative_to(scope):
                    matched.append(scope)
    return max(matched, key=lambda item: len(str(item))) if matched else None


def _open_readonly_db(db_path: Path) -> sqlite3.Connection | None:
    try:
        with _parent(db_path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return None
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return None
    encoded = quote(str(db_path), safe='/')
    for query in ('mode=ro', 'mode=ro&immutable=1'):
        con = None
        try:
            con = sqlite3.connect(f'file:{encoded}?{query}', uri=True, timeout=1.5)
            con.row_factory = sqlite3.Row
            # Force a lightweight schema probe so unreadable/corrupt DBs fall through cleanly.
            con.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
            return con
        except sqlite3.Error:
            if con is not None:
                con.close()
            continue
    return None


def _list_project_conversations(app_dir: Path, scopes: list[Path], limit: int = MAX_CONVERSATIONS) -> list[dict]:
    db_path = app_dir / 'conversation_summaries.db'
    con = _open_readonly_db(db_path)
    if con is None:
        return []
    try:
        cols = {row['name'] for row in con.execute('PRAGMA table_info(conversation_summaries)').fetchall()}
        if 'conversation_id' not in cols or 'workspace_uris' not in cols:
            return []
        wanted = [
            col for col in (
                'conversation_id', 'title', 'preview', 'step_count',
                'last_modified_time', 'workspace_uris', 'status',
                'parent_conversation_id', 'nesting_depth', 'last_user_input_time',
            )
            if col in cols
        ]
        clauses = []
        if 'parent_conversation_id' in cols:
            clauses.append("COALESCE(parent_conversation_id, '') = ''")
        if 'nesting_depth' in cols:
            clauses.append('COALESCE(nesting_depth, 0) = 0')
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        order = ' ORDER BY last_modified_time DESC' if 'last_modified_time' in cols else ''
        query = f'SELECT {", ".join(wanted)} FROM conversation_summaries{where}{order}'
        rows = con.execute(query).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    matched = []
    for row in rows:
        record = dict(row)
        cid = record.get('conversation_id')
        if not isinstance(cid, str) or not _CONVERSATION_ID.fullmatch(cid):
            continue
        scope = _matching_scope(_parse_workspace_uris(record.get('workspace_uris')), scopes)
        if scope is None:
            continue
        record['matched_root'] = scope
        matched.append(record)
        if len(matched) >= limit:
            break
    return matched


def _locate_transcript(app_dir: Path, cid: str) -> tuple[Path, os.stat_result] | None:
    logs_dir = app_dir / 'brain' / cid / '.system_generated' / 'logs'
    for filename in ('transcript_full.jsonl', 'transcript.jsonl'):
        path = logs_dir / filename
        try:
            with _parent(path) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    return path, info
        except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
            continue
    return None


def _read_bounded_regular(path: Path, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
    with _parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            _regular(fd, allow_unlinked=True)
            size = os.fstat(fd).st_size
            if size <= max_bytes:
                return os.pread(fd, size, 0)
            tail = os.pread(fd, max_bytes, size - max_bytes)
            newline = tail.find(b'\n')
            return tail[newline + 1:] if newline != -1 else b''
        finally:
            os.close(fd)


def _read_existing_first_line(path: Path) -> bytes | None:
    try:
        with _parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                _regular(fd, allow_unlinked=True)
                chunk = os.pread(fd, 512, 0)
            finally:
                os.close(fd)
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return None
    return chunk.split(b'\n', 1)[0]


def _read_existing_bytes(path: Path) -> bytes | None:
    try:
        with _parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                _regular(fd, allow_unlinked=True)
                size = os.fstat(fd).st_size
                if size > MAX_EXPORT_BYTES * 2:
                    return None
                return os.pread(fd, size, 0)
            finally:
                os.close(fd)
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return None


def _extract_user_content(raw: str) -> str:
    matches = _USER_REQUEST_RE.findall(raw)
    if matches:
        parts = [_clean_text(part) for part in matches]
        return '\n\n'.join(part for part in parts if part)
    cleaned = _STRIP_TAGS_RE.sub('', raw)
    return _clean_text(cleaned)


def _unwrap_arg_string(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str):
                return parsed
        except ValueError:
            pass
    return value


def _relativize_touched(path_str: str, root: Path) -> str:
    path = Path(path_str)
    try:
        if path.is_absolute() and path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    except ValueError:
        pass
    return path_str


def _parse_transcript(data: bytes, matched_root: Path) -> tuple[list[dict], list[str]]:
    turns: list[dict] = []
    touched: list[str] = []
    seen_touched: set[str] = set()
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            # Tolerate a partial trailing record while agy is actively appending.
            continue
        if not isinstance(row, dict):
            continue
        source = row.get('source')
        kind = row.get('type')
        status = row.get('status')
        if status and status != 'DONE':
            continue
        created_at = _format_timestamp(row.get('created_at'))
        if source == 'USER_EXPLICIT' and kind == 'USER_INPUT':
            raw = row.get('content')
            if isinstance(raw, str):
                text = _extract_user_content(raw)
                if text:
                    turns.append(dict(role='User', at=created_at, text=text))
        elif source == 'MODEL' and kind == 'PLANNER_RESPONSE':
            raw = row.get('content')
            if isinstance(raw, str):
                text = _clean_text(raw)
                if text:
                    turns.append(dict(role='Assistant', at=created_at, text=text))
            tool_calls = row.get('tool_calls')
            if isinstance(tool_calls, list) and len(touched) < MAX_FILES_TOUCHED:
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    if call.get('name') not in ('edit_file', 'write_file'):
                        continue
                    args = call.get('args')
                    if not isinstance(args, dict):
                        continue
                    raw_path = _unwrap_arg_string(args.get('Path') or args.get('path'))
                    if not raw_path:
                        continue
                    label = _relativize_touched(raw_path, matched_root)
                    if label not in seen_touched:
                        seen_touched.add(label)
                        touched.append(label)
                        if len(touched) >= MAX_FILES_TOUCHED:
                            break
    return turns, touched


def _clip_turn(text: str, limit: int = MAX_TURN_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head - 40
    return text[:head].rstrip() + '\n\n… (middle truncated) …\n\n' + text[-max(0, tail):].lstrip()


def _fingerprint_line(host: str, record: dict, source_name: str, source_size: int, source_mtime: int) -> bytes:
    cid = record['conversation_id']
    steps = record.get('step_count', '')
    updated = record.get('last_modified_time', '')
    line = (
        f'<!-- agy-export: v={_FORMAT_VERSION} host={host} id={cid} '
        f'steps={steps} updated={updated} source={source_name} '
        f'size={source_size} mtime={source_mtime} -->'
    )
    return line.encode('utf-8')


def _render_conversation(
    host: str,
    record: dict,
    turns: list[dict],
    touched: list[str],
    fingerprint: bytes,
) -> bytes:
    title = _single_line(str(record.get('title') or ''), 160) or f"Conversation {record['conversation_id'][:8]}"
    updated = _format_timestamp(record.get('last_modified_time'))
    steps = record.get('step_count')
    status = _single_line(str(record.get('status') or ''), 40)
    header_lines = [
        fingerprint.decode('utf-8'),
        f'# {title}',
        '',
        f'- **Conversation ID:** `{record["conversation_id"]}`',
        f'- **Host:** `{host}`',
    ]
    if updated:
        header_lines.append(f'- **Updated:** {updated}')
    if steps is not None and steps != '':
        header_lines.append(f'- **Steps:** {steps}')
    if status:
        header_lines.append(f'- **Status:** {status}')
    if touched:
        rendered_files = ', '.join(f'`{_single_line(item, 120)}`' for item in touched)
        header_lines.append(f'- **Files touched:** {rendered_files}')
    header_lines.append('')
    header_bytes = ('\n'.join(header_lines) + '\n').encode('utf-8')

    if not turns:
        preview = _clean_text(str(record.get('preview') or ''))
        fallback = (
            '## Summary\n\n' + (_clip_turn(preview, 4000) if preview else '_No completed turns exported yet._') + '\n'
        ).encode('utf-8')
        return header_bytes + fallback

    rendered_turns: list[bytes] = []
    for turn in turns:
        stamp = f' ({turn["at"]})' if turn['at'] else ''
        block = f'## {turn["role"]}{stamp}\n\n{_clip_turn(turn["text"])}\n\n'
        rendered_turns.append(block.encode('utf-8'))

    budget = max(4096, MAX_EXPORT_BYTES - len(header_bytes) - 128)
    selected: list[bytes] = []
    used = 0
    for block in reversed(rendered_turns):
        if selected and used + len(block) > budget:
            break
        if not selected and len(block) > budget:
            block = block[:budget].decode('utf-8', errors='ignore').rstrip().encode('utf-8') + b'\n\n'
        selected.append(block)
        used += len(block)
    selected.reverse()

    omitted = len(rendered_turns) - len(selected)
    notice = (
        f'> Earlier turns omitted to keep this export compact (showing last {len(selected)} of {len(rendered_turns)} turns).\n\n'.encode('utf-8')
        if omitted > 0
        else b''
    )
    return header_bytes + notice + b''.join(selected)


def _render_index(host: str, entries: list[dict]) -> bytes:
    lines = [
        f'# Antigravity (`agy`) project history ({host})',
        '',
        'Read-only Markdown exports generated automatically by project sync.',
        'Newest conversations appear first. Do not edit these files; write lasting',
        'project facts and handoff notes to `.scratch/memory/`.',
        '',
    ]
    if not entries:
        lines.append('_No project Antigravity (`agy`) conversations recorded on this host yet._')
        lines.append('')
        return ('\n'.join(lines)).encode('utf-8')
    lines.extend([
        '| Updated (UTC) | Conversation | Steps | Preview |',
        '| --- | --- | ---: | --- |',
    ])
    for entry in entries:
        updated = _format_timestamp(entry.get('last_modified_time')) or '—'
        title = _single_line(str(entry.get('title') or ''), 80) or entry['conversation_id'][:8]
        cid = entry['conversation_id']
        steps = entry.get('step_count')
        steps_label = str(steps) if steps is not None and steps != '' else '—'
        preview = _single_line(str(entry.get('preview') or ''), 90) or '—'
        lines.append(f'| {updated} | [{title}](./{cid}.md) (`{cid[:8]}`) | {steps_label} | {preview} |')
    lines.append('')
    return ('\n'.join(lines)).encode('utf-8')


def _is_real_dir(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _prune_stale(target_dir: Path, keep_names: set[str]) -> int:
    pruned = 0
    try:
        with _parent(target_dir / '.probe') as (dir_fd, _):
            for name in os.listdir(dir_fd):
                if not name.endswith('.md') or name in keep_names:
                    continue
                try:
                    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    try:
                        os.unlink(name, dir_fd=dir_fd)
                        pruned += 1
                    except OSError:
                        pass
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return pruned
    return pruned


def export_project_history(
    project,
    *,
    host: str,
    state: Path | None = None,
    env: Mapping | None = None,
    historical_roots: tuple[str, ...] | list[str] = (),
    max_conversations: int = MAX_CONVERSATIONS,
) -> dict:
    """Export project-scoped top-level agy conversations into .scratch/history/agy/<host>/."""
    if host not in HOSTS:
        raise ValueError(f'Unsupported agy export host: {host}')
    root = Path(project.root)
    scratch = root / '.scratch'
    if not _is_real_dir(scratch):
        return dict(exported=0, unchanged=0, pruned=0, total=0, skipped=True)

    app_dir = agy_home(env)
    from .conversations import _scopes
    scopes = [Path(item) for item in _scopes(project, state, historical_roots)]
    records = _list_project_conversations(app_dir, scopes, limit=max_conversations)

    target_dir = scratch / 'history' / 'agy' / host
    if not records and not _is_real_dir(target_dir):
        return dict(exported=0, unchanged=0, pruned=0, total=0, skipped=True)

    exported = 0
    unchanged = 0
    keep_names = {'INDEX.md'}
    for record in records:
        cid = record['conversation_id']
        filename = f'{cid}.md'
        keep_names.add(filename)
        destination = target_dir / filename
        located = _locate_transcript(app_dir, cid)
        if located is not None:
            source_path, info = located
            fingerprint = _fingerprint_line(host, record, source_path.name, info.st_size, info.st_mtime_ns)
        else:
            source_path = None
            fingerprint = _fingerprint_line(host, record, 'summary', 0, 0)

        if _read_existing_first_line(destination) == fingerprint:
            unchanged += 1
            continue

        turns: list[dict] = []
        touched: list[str] = []
        if source_path is not None:
            try:
                raw = _read_bounded_regular(source_path)
            except (OSError, ValueError):
                raw = b''
            if raw:
                turns, touched = _parse_transcript(raw, record['matched_root'])
        content = _render_conversation(host, record, turns, touched, fingerprint)
        atomic_write(destination, content, mode=0o600)
        exported += 1

    index_path = target_dir / 'INDEX.md'
    index_bytes = _render_index(host, records)
    if _read_existing_bytes(index_path) != index_bytes:
        atomic_write(index_path, index_bytes, mode=0o600)

    pruned = _prune_stale(target_dir, keep_names)
    return dict(exported=exported, unchanged=unchanged, pruned=pruned, total=len(records), skipped=False)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .state import discover_project, state_dir

    parser = argparse.ArgumentParser(description='Export project-scoped Antigravity (agy) conversations to .scratch/history/agy/.')
    parser.add_argument('--project', default='.', help='Path inside the project Git checkout')
    parser.add_argument('--host', choices=HOSTS, default='codespace' if os.environ.get('CODESPACES') == 'true' else 'mac')
    args = parser.parse_args(argv)

    project = discover_project(Path(args.project).resolve())
    try:
        state = state_dir(project)
    except (OSError, ValueError):
        state = None
    result = export_project_history(project, host=args.host, state=state)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
