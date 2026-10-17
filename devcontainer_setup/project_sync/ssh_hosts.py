"""A project-owned OpenSSH alias for the supervisor's existing live forward."""
from __future__ import annotations

import base64
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


def _alias_config(state: Path, alias: str, data: bytes | None = None) -> bytes:
    owned = data is not None
    if data is None:
        data = _read(state / 'ssh-config', private=True)
    try:
        rows = [shlex.split(line, comments=True) for line in data.decode().splitlines()]
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError('Invalid worker SSH forward configuration') from error
    rows = [row for row in rows if row]
    if owned:
        if not rows or rows[-1] != ['Host', '*']:
            raise ValueError('Invalid pending SSH alias configuration')
        rows.pop()
    if not rows or rows[0] != ['Host', alias if owned else 'project-sync']:
        raise ValueError('Worker SSH forward must name project-sync')
    values = {}
    for row in rows[1:]:
        if len(row) != 2 or row[0].lower() in values:
            raise ValueError('Invalid worker SSH forward directive')
        values[row[0].lower()] = row[1]
    extra = {'proxycommand': 'none', 'proxyjump': 'none',
             'permitlocalcommand': 'no', 'remotecommand': 'none'}
    if owned:
        for key, value in extra.items():
            if values.pop(key, None) != value:
                raise ValueError('Unsafe pending SSH alias configuration')
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


def _optional_json(path: Path):
    try:
        return read_json(path)
    except FileNotFoundError:
        return None


def _version(path: Path, *, private: bool = False):
    try:
        return _read(path, private=private), stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError:
        return None, None


def _digest(data: bytes | None):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _encode(value):
    return json.dumps(value, sort_keys=True).encode() + b'\n'


def _context(project_id, config, include):
    return {'schema': 1, 'project_id': project_id, 'alias': 'devcontainer-' + project_id,
            'config': str(config), 'include': str(include)}


def _finish(state: Path, project_id: str, pending: dict) -> None:
    """Replay only exact owned before/after versions; never guess at user edits."""
    config, include, receipt = _paths(state)
    context = _context(project_id, config, include)
    paths = {'include': include, 'config': config, 'receipt': receipt}
    if pending.get('context') != context or set(pending.get('artifacts', {})) != set(paths):
        raise ValueError('Pending SSH transaction ownership conflicts with this project')
    prepared = {}
    for name, path in paths.items():
        item = pending['artifacts'][name]
        try:
            data = base64.b64decode(item['data'], validate=True)
            mode, before_mode = item['mode'], item['before_mode']
            before, after = item['before'], item['after']
            if (not isinstance(mode, int) or not 0 <= mode <= 0o777 or mode & 0o022
                    or (before_mode is not None and (not isinstance(before_mode, int)
                        or not 0 <= before_mode <= 0o777 or before_mode & 0o022))
                    or (before is not None and (not isinstance(before, str) or not re.fullmatch('[0-9a-f]{64}', before)))
                    or after != _digest(data) or (before is None) != (before_mode is None)):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError('Invalid pending SSH transaction artifact') from None
        current, current_mode = _version(path, private=name != 'config')
        current_digest = _digest(current)
        allowed_modes = set()
        if current_digest == before:
            allowed_modes.add(before_mode)
        if current_digest == after:
            allowed_modes.add(mode)
        if current_mode not in allowed_modes:
            raise ValueError(f'Owned SSH {name} was modified during a pending transaction; review required')
        prepared[name] = (path, data, mode, current, current_mode)
    # The journal must describe this producer's exact safe alias and receipt.
    target_include = prepared['include'][1]
    if _alias_config(state, context['alias'], target_include) != target_include:
        raise ValueError('Invalid pending SSH alias bytes')
    expected_receipt = _encode(dict(context, sha256=_digest(target_include)))
    if prepared['receipt'][1] != expected_receipt:
        raise ValueError('Invalid pending SSH receipt bytes')
    block = _block(context['alias'], include)
    target_config = prepared['config'][1]
    if (target_config.count(block) != 1
            or target_config.count(f'devcontainer-setup:{context["alias"]}'.encode()) != 2):
        raise ValueError('Invalid pending SSH include ownership block')
    config_item = pending['artifacts']['config']
    if config_item['before'] != config_item['after']:
        # Initial setup can only prepend the owned include to the exact bytes
        # reviewed before the transaction. Refresh never rewrites global SSH.
        original = target_config[len(block):]
        if (not target_config.startswith(block)
                or (_digest(original) if config_item['before'] is not None else None) != config_item['before']
                or (config_item['before'] is None and original)):
            raise ValueError('Invalid pending SSH global configuration transition')
    for path, data, mode, current, current_mode in prepared.values():
        if current != data or current_mode != mode:
            atomic_write(path, data, mode=mode)
    with _parent(state / 'herdr-ssh.pending.json', private=True) as (parent, name):
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)


def _configure(state: Path, project_id: str | None) -> str | None:
    config, include, receipt_path = _paths(state)
    pending_path = state / 'herdr-ssh.pending.json'
    pending = _optional_json(pending_path)
    if project_id is None and pending is None and _optional_json(receipt_path) is None:
        return None  # Background reconnect never initiates global SSH setup.
    _directory(config)
    with locked(config.parent / '.devcontainer-setup.lock', blocking=True):
        pending = _optional_json(pending_path)
        if project_id is None:
            owner = pending.get('context', {}) if pending else (_optional_json(receipt_path) or {})
            project_id = owner.get('project_id')
        if not isinstance(project_id, str) or not re.fullmatch('[0-9a-f]{24}', project_id):
            raise ValueError('Invalid SSH alias project identity')
        if pending is not None:
            _finish(state, project_id, pending)
        # Finish a previous acknowledged intent before adapting a newer worker
        # forward. In particular, its stale port never becomes new authority.
        receipt = _optional_json(receipt_path)
        context = _context(project_id, config, include)
        alias = context['alias']
        block = _block(alias, include)
        candidate = _alias_config(state, alias)
        original, mode = _version(config)
        if receipt is not None:
            if any(receipt.get(key) != value for key, value in context.items()):
                raise ValueError('SSH alias ownership receipt conflicts with this project')
            if original is None or original.count(block) != 1 or original.count(f'devcontainer-setup:{alias}'.encode()) != 2:
                raise ValueError('Owned SSH include block is missing or modified')
            if hashlib.sha256(_read(include, private=True)).hexdigest() != receipt.get('sha256'):
                raise ValueError('Owned SSH include was modified; review required')
        else:
            if include.exists() or include.is_symlink() or alias.encode() in (original or b''):
                raise ValueError('SSH alias already exists without an ownership receipt')
        digest = hashlib.sha256(candidate).hexdigest()
        if receipt is None or receipt['sha256'] != digest:
            targets = {'include': (include, candidate, 0o600),
                       'config': (config, original if receipt else block + (original or b''), mode or 0o600),
                       'receipt': (receipt_path, _encode(dict(context, sha256=digest)), 0o600)}
            artifacts = {}
            for name, (path, data, target_mode) in targets.items():
                before, before_mode = _version(path, private=name != 'config')
                artifacts[name] = {'before': _digest(before), 'before_mode': before_mode,
                                   'after': _digest(data), 'mode': target_mode,
                                   'data': base64.b64encode(data).decode()}
            pending = {'context': context, 'artifacts': artifacts}
            atomic_write(pending_path, _encode(pending))
            _finish(state, project_id, pending)
    return alias


def configure_ssh(project, state: Path) -> str:
    """Explicitly install or refresh this project's owned SSH alias."""
    return _configure(Path(state), project.project_id)


def refresh_ssh(state: Path) -> None:
    """Refresh an already installed alias after the worker reconnects."""
    _configure(Path(state), None)
