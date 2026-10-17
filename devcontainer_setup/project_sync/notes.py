"""Pure candidate integration for mixed-ownership project instructions and policy."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tomllib

from .state import _parent, _regular

_NOTES = b'''## Project memory

Read `.scratch/memory/` at the start of a project conversation when it exists.
Maintain useful project facts, decisions, and handoff notes there as Markdown;
create the directory when needed. These notes are shared by Codex and Claude.
Keep notes scoped to this project. Do not copy global memory, credentials,
tokens, native agent databases, or unrelated project information into scratch.
The scratch contents are ignored by Git and may synchronize between paired
checkouts. Preserve existing notes and resolve conflicting facts explicitly.
'''
_IGNORE = b'*\n!.gitignore\n!README.md\n'
_README = b'''# Project scratch

Working files and shared project notes in `memory/` are ignored by Git.
Only this README and `.gitignore` are tracked markers; scratch sync excludes
both markers. Do not put credentials or unrelated project memory here.
Use the project scratch tasks to configure synchronization and backups.
'''


def _block(kind: str, body: bytes, toml: bool = False) -> bytes:
    prefix, suffix = (b'# ', b'') if toml else (b'<!-- ', b' -->')
    label = f'devcontainer-setup:{kind}'.encode()
    return (prefix + label + b' begin sha256=' + hashlib.sha256(body).hexdigest().encode()
            + suffix + b'\n' + body + prefix + label + b' end' + suffix + b'\n')


def _owned(data: bytes, kind: str, toml: bool = False) -> bool:
    """Validate delimiters and the exact body digest before trusting ownership."""
    label = f'devcontainer-setup:{kind}'.encode()
    if label not in data:
        return False
    prefix, suffix = (b'# ', b'') if toml else (b'<!-- ', b' -->')
    pattern = (rb'^' + re.escape(prefix + label) + rb' begin sha256=([0-9a-f]{64})'
               + re.escape(suffix) + rb'\n(.*?)^' + re.escape(prefix + label + b' end' + suffix) + rb'\n')
    matches = list(re.finditer(pattern, data, re.MULTILINE | re.DOTALL))
    if len(matches) != 1 or data.count(label) != 2:
        raise ValueError(f'Malformed or duplicate managed {kind} block; review required')
    if hashlib.sha256(matches[0][2]).hexdigest().encode() != matches[0][1]:
        raise ValueError(f'Edited managed {kind} block; review required')
    return True


def _append(data: bytes, block: bytes) -> bytes:
    return data + (b'\n\n' if data and not data.endswith(b'\n') else b'\n' if data else b'') + block


def _read(project: Path, name: str, files: dict[str, bytes]) -> bytes:
    """Validate even existing candidate paths without following links or writing."""
    existing = b''
    try:
        with _parent(project / name) as (parent, filename):
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as source:
                _regular(source.fileno())
                existing = source.read()
    except FileNotFoundError:
        pass
    return files.get(name, existing)


def _parse(data: bytes) -> dict:
    try:
        return tomllib.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError('Project Codex config is not valid UTF-8 TOML; review required') from exc


def _table_policy(data: bytes, table: str, keys: tuple[str, ...], kind: str) -> bytes:
    owned = _owned(data, kind, toml=True)
    parsed = _parse(data)
    options = parsed.get(table, {})
    if not isinstance(options, dict):
        raise ValueError(f'Existing Codex {table} policy is not a table; review required')
    if any(key in options and options[key] is not False for key in keys):
        raise ValueError(f'Existing Codex [{table}] memory policy conflicts with project-only memory; review required')
    if owned:
        if not all(options.get(key) is False for key in keys):
            raise ValueError(f'Managed Codex policy no longer applies to [{table}]; review required')
        return data
    missing = [key for key in keys if key not in options]
    if not missing:
        return data
    expected = {**parsed, table: {**options, **{key: False for key in keys}}}
    body = b''.join(f'{key} = false\n'.encode() for key in missing)
    if table not in parsed:
        candidate = _append(data, _block(kind, f'[{table}]\n'.encode() + body, toml=True))
        if _parse(candidate) != expected:
            raise ValueError(f'Cannot safely select Codex [{table}] policy')
        return candidate
    # Retain the existing table header and every user option byte-for-byte. Test
    # the full parsed candidate to exclude header-like lines in multiline strings.
    label = re.escape(table.encode())
    header = rb'\s*\[\s*(?:' + label + rb'|"' + label + rb'"|\'' + label + rb'\')\s*\]\s*(?:#[^\n]*)?\s*'
    offset = 0
    for line in data.splitlines(keepends=True):
        offset += len(line)
        if re.fullmatch(header, line):
            block = _block(kind, body, toml=True)
            candidate = data[:offset] + (b'' if line.endswith(b'\n') else b'\n') + block + data[offset:]
            try:
                if _parse(candidate) == expected:
                    return candidate
            except ValueError:
                continue
    # Root dotted-key syntax can also own the table. Add only missing dotted
    # keys at root scope; parser validation prevents scope changes.
    dotted = b''.join(f'{table}.{key} = false\n'.encode() for key in missing)
    candidate = _block(kind, dotted, toml=True) + data
    try:
        if _parse(candidate) == expected:
            return candidate
    except ValueError:
        pass
    raise ValueError(f'Cannot safely extend existing Codex [{table}] policy; review required')


def _policy(data: bytes) -> bytes:
    data = _table_policy(data, 'memories', ('generate_memories', 'use_memories'), 'project-memory')
    return _table_policy(data, 'features', ('memories',), 'memory-feature')


def integrate_notes(project: Path, files: dict[str, bytes], enabled: bool) -> None:
    """Add reviewable candidates only; never write project files or note payloads."""
    if not enabled:
        return
    project = Path(project)
    additions = {}
    for name in ('AGENTS.md', 'CLAUDE.md'):
        data = _read(project, name, files)
        additions[name] = data if _owned(data, 'project-notes') else _append(data, _block('project-notes', _NOTES))
    additions['.codex/config.toml'] = _policy(_read(project, '.codex/config.toml', files))
    for name, desired in (('.scratch/.gitignore', _IGNORE), ('.scratch/README.md', _README)):
        data = _read(project, name, files)
        if data and data != desired:
            raise ValueError(f'Existing scratch marker {name} differs; review required')
        additions[name] = desired
    # Apply together so any refusal leaves the caller's candidate map untouched.
    files.update(additions)
