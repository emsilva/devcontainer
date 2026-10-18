"""Pure candidate integration for mixed-ownership project instructions and policy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

from .state import _parent, _regular, atomic_write, locked, read_json, state_dir

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
    ignore = _read(project, '.gitignore', files)
    line = b'/.claude/settings.local.json'
    if line not in ignore.splitlines():
        additions['.gitignore'] = ignore.rstrip(b'\n') + (b'\n' if ignore else b'') + line + b'\n'
    # Apply together so any refusal leaves the caller's candidate map untouched.
    files.update(additions)


def _memory_source(project, env):
    from .sessions import claude_namespace, native_home
    values = os.environ if env is None else env
    if values.get('CLAUDE_CODE_PROJECT_DIR_NAME'):
        raise ValueError('Shared Claude project directory override requires explicit memory migration; settings preserved')
    root = Path(project.root)
    common = subprocess.run(['git', '-C', str(root), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True, timeout=15)
    if common.returncode == 0:
        directory = Path(common.stdout.strip())
        if directory.name != '.git':
            raise ValueError('Cannot establish Claude canonical repository memory directory; settings preserved')
        root = directory.parent
    return native_home('claude', env) / 'projects' / claude_namespace(root) / 'memory'


def _memory_bytes(path):
    from .sessions import _read as native_read
    try:
        return native_read(path)
    except FileNotFoundError:
        return None


def _memory_tree(source):
    if any(path.is_symlink() for path in [source, *source.parents]):
        raise ValueError('Linked Claude memory directories cannot be migrated; settings preserved')
    if not source.exists():
        return {}
    if not source.is_dir():
        raise ValueError('Claude memory source must be a directory; settings preserved')
    files, total = {}, 0
    def visit(directory):
        nonlocal total
        # pathlib's recursive glob suppresses unreadable-directory errors. A
        # migration must fail rather than certify an incomplete memory tree.
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    raise ValueError('Linked Claude memory notes cannot be migrated; settings preserved')
                if entry.is_dir(follow_symlinks=False):
                    visit(path)
                    continue
                data = _memory_bytes(path)
                if data is None:
                    raise ValueError('Claude memory changed during migration; settings preserved')
                total += len(data)
                files[path.relative_to(source).as_posix()] = data
                if len(files) > 4096 or total > 64 * 1024 * 1024:
                    raise ValueError('Claude project memory exceeds migration limits; settings preserved')
    visit(source)
    return files


def _copy_memory(path, data):
    """Create missing notes exclusively; a failed copy never changes settings."""
    with _parent(path, create=True) as (parent, name):
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        except FileExistsError:
            if _memory_bytes(path) != data:
                raise ValueError('Claude memory destination conflict; settings and existing notes preserved') from None
            return
        # Once visible, a note can receive owner edits. Even cancellation must
        # retain that path; a partial copy is an explicit conflict on retry.
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(parent)


def _seed_memory(project, source, destination, previous, env):
    """A local receipt prevents future sync cycles from replaying legacy notes."""
    if any(path.is_symlink() for path in [destination, *destination.parents]):
        raise ValueError('Linked Claude memory destinations cannot be migrated; settings preserved')
    state = state_dir(project, env)
    key = hashlib.sha256(json.dumps([str(source), str(destination)]).encode()).hexdigest()
    receipt = state / 'claude-memory-migrations' / (key + '.json')
    binding = dict(schema=1, project_id=project.project_id, root=str(project.root),
                   source=str(source), destination=str(destination))
    with locked(state / 'claude-memory-migration.lock', blocking=True):
        try:
            saved = read_json(receipt)
        except FileNotFoundError:
            saved = None
        if saved is not None:
            if saved.get('binding') != binding or not isinstance(saved.get('files'), dict):
                raise ValueError('Claude memory migration receipt differs; settings preserved')
            if previous == str(destination):
                return
        files = _memory_tree(source)
        # Validate every existing note before publishing any missing note.
        for name, data in files.items():
            current = _memory_bytes(destination / name)
            if current is not None and current != data:
                raise ValueError('Claude memory destination conflict; settings and existing notes preserved')
        # The startup index becomes visible only after its referenced notes.
        for name in sorted(files, key=lambda value: (value == 'MEMORY.md', value)):
            _copy_memory(destination / name, files[name])
        if _memory_tree(source) != files or any(_memory_bytes(destination / name) != data for name, data in files.items()):
            raise ValueError('Claude memory changed during migration; settings preserved')
        atomic_write(receipt, (json.dumps(dict(binding=binding, files={name: hashlib.sha256(data).hexdigest()
                                                                     for name, data in files.items()}), sort_keys=True) + '\n').encode())


def configure_memory(project, *, env=None) -> None:
    """Copy native project memory before routing Claude to the scratch tree."""
    root = Path(project.root)
    name = '.claude/settings.local.json'
    raw = _read(root, name, {})
    try:
        settings = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Project Claude local settings are not valid JSON; existing settings preserved') from None
    if not isinstance(settings, dict):
        raise ValueError('Project Claude local settings must be an object; existing settings preserved')
    directory = str(root / '.scratch/memory/claude')
    previous = settings.get('autoMemoryDirectory')
    source = _memory_source(project, env)
    if previous is not None and previous not in (directory, str(source)):
        raise ValueError('Existing project Claude memory directory differs; existing settings preserved')
    if previous is None:
        from .sessions import native_home
        for base, filename in ((root, '.claude/settings.json'), (native_home('claude', env), 'settings.json')):
            layer = _read(base, filename, {})
            options = json.loads(layer) if layer else {}
            if not isinstance(options, dict):
                raise ValueError('Claude settings must be an object; existing settings preserved')
            selected = options.get('autoMemoryDirectory')
            if selected is not None:
                if selected not in (directory, str(source)):
                    raise ValueError('Existing Claude memory directory differs; existing settings preserved')
                break
    if (root / '.git').exists():
        tracked = subprocess.run(['git', '-C', str(root), 'ls-files', '--error-unmatch', '--', name],
                                 capture_output=True, timeout=15)
        if tracked.returncode == 0:
            raise ValueError('Project Claude local settings are tracked; existing file preserved')
        if tracked.returncode != 1:
            raise ValueError('Cannot verify project Claude local settings ownership; existing file preserved')
    # Backfill the previous empty-target bug as well as first-time installations.
    if source.exists() or source.is_symlink():
        _seed_memory(project, source, Path(directory), previous, env)
    if previous == directory:
        return
    if _read(root, name, {}) != raw:
        raise ValueError('Project Claude settings changed during memory migration; existing settings preserved')
    settings['autoMemoryDirectory'] = directory
    atomic_write(root / name, (json.dumps(settings, indent=2, sort_keys=True) + '\n').encode(), mode=0o600)
