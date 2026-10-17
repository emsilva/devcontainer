"""A project-owned OpenSSH alias for the supervisor's existing live forward."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat

from .state import _parent, _regular, atomic_write, locked, read_json


def _read(path: Path, *, private: bool = False) -> bytes:
    with _parent(path, private=private) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as source:
            _regular(source.fileno(), allow_unlinked=True)
            info = os.fstat(source.fileno())
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError('SSH configuration ownership or permissions changed')
            data = source.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError('SSH configuration is too large')
    return data


def _quote(path: Path) -> str:
    text = str(path)
    if any(char in text for char in '\r\n\0%'):
        raise ValueError('Unsupported SSH configuration path')
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _paths(state: Path):
    state = Path(state)
    home = Path(os.environ.get('HOME') or Path.home())
    if not state.is_absolute() or not home.is_absolute() or '..' in state.parts or '..' in home.parts:
        raise ValueError('SSH state and home must be absolute without traversal')
    return home / '.ssh/config', state / 'herdr-ssh-config', state / 'herdr-ssh.json'


def _block(alias: str, include: Path) -> bytes:
    return (f'# devcontainer-setup:{alias} begin\nHost *\n'
            f'  Include {_quote(include)}\n'
            f'# devcontainer-setup:{alias} end\n').encode()


def _alias_config(state: Path, alias: str) -> bytes:
    data = _read(state / 'ssh-config', private=True)
    try:
        rows = [shlex.split(line, comments=True) for line in data.decode().splitlines()]
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError('Invalid worker SSH forward configuration') from error
    rows = [row for row in rows if row]
    if not rows or rows[0] != ['Host', 'project-sync']:
        raise ValueError('Worker SSH forward must name project-sync')
    values = {}
    for row in rows[1:]:
        if len(row) != 2 or row[0].lower() in values:
            raise ValueError('Invalid worker SSH forward directive')
        values[row[0].lower()] = row[1]
    home = Path(os.environ.get('HOME') or Path.home())
    required = {'hostname': '127.0.0.1', 'user': 'vscode',
                'identityfile': str(home / '.ssh/codespaces.auto'), 'identitiesonly': 'yes',
                'batchmode': 'yes', 'stricthostkeychecking': 'yes',
                'userknownhostsfile': str(state / 'known_hosts'), 'globalknownhostsfile': '/dev/null',
                'connecttimeout': '15', 'connectionattempts': '1', 'serveraliveinterval': '0',
                'controlmaster': 'no', 'controlpath': 'none', 'clearallforwardings': 'yes',
                'loglevel': 'ERROR'}
    if set(values) != set(required) | {'port', 'hostkeyalias'} or any(values[key] != value for key, value in required.items()):
        raise ValueError('Unsafe worker SSH forward configuration')
    if not values['port'].isdigit() or not 1 <= int(values['port']) <= 65535:
        raise ValueError('Invalid worker SSH forward port')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}', values['hostkeyalias']):
        raise ValueError('Invalid worker SSH forward host identity')
    # Re-emit only the validated allowlist; never copy executable SSH options.
    lines = [f'Host {alias}']
    for key, value in values.items():
        lines.append(f'  {key} {_quote(Path(value))}')
    lines.extend(('  ProxyCommand none', '  ProxyJump none', '  PermitLocalCommand no', '  RemoteCommand none'))
    return ('\n'.join(lines) + '\nHost *\n').encode()


def _directory(config: Path) -> None:
    try:
        config.parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = config.parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError('SSH directory ownership or permissions are unsafe')


def _configure(state: Path, project_id: str | None) -> str | None:
    config, include, receipt_path = _paths(state)
    try:
        receipt = read_json(receipt_path)
    except FileNotFoundError:
        if project_id is None:
            return None  # Background reconnect never creates global SSH setup.
        receipt = None
    if project_id is None:
        project_id = receipt.get('project_id')
    if not isinstance(project_id, str) or not re.fullmatch('[0-9a-f]{24}', project_id):
        raise ValueError('Invalid SSH alias project identity')
    alias = 'devcontainer-' + project_id
    block = _block(alias, include)
    candidate = _alias_config(state, alias)
    _directory(config)
    with locked(config.parent / '.devcontainer-setup.lock', blocking=True):
        try:
            original = _read(config)
            mode = stat.S_IMODE(config.lstat().st_mode)
        except FileNotFoundError:
            original, mode = b'', 0o600
        if receipt is not None:
            expected = {'schema': 1, 'project_id': project_id, 'alias': alias,
                        'config': str(config), 'include': str(include)}
            if any(receipt.get(key) != value for key, value in expected.items()):
                raise ValueError('SSH alias ownership receipt conflicts with this project')
            if original.count(block) != 1 or original.count(f'devcontainer-setup:{alias}'.encode()) != 2:
                raise ValueError('Owned SSH include block is missing or modified')
            if hashlib.sha256(_read(include, private=True)).hexdigest() != receipt.get('sha256'):
                raise ValueError('Owned SSH include was modified; review required')
        else:
            if include.exists() or include.is_symlink() or alias.encode() in original:
                raise ValueError('SSH alias already exists without an ownership receipt')
        digest = hashlib.sha256(candidate).hexdigest()
        if receipt is None or receipt['sha256'] != digest:
            atomic_write(include, candidate)
            atomic_write(receipt_path, json.dumps({'schema': 1, 'project_id': project_id, 'alias': alias,
                         'config': str(config), 'include': str(include),
                         'sha256': digest}, sort_keys=True).encode() + b'\n')
        if receipt is None:
            atomic_write(config, block + original, mode=mode)
    return alias


def configure_ssh(project, state: Path) -> str:
    """Explicitly install or refresh this project's owned SSH alias."""
    return _configure(Path(state), project.project_id)


def refresh_ssh(state: Path) -> None:
    """Refresh an already installed alias after the worker reconnects."""
    _configure(Path(state), None)
