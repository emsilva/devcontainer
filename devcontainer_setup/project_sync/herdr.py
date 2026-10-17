"""Project Herdr workspaces, using public CLI IDs and native SSH setup."""
from __future__ import annotations

from datetime import date, datetime, time
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tomllib

from .ssh_hosts import configure_ssh
from .state import atomic_write, read_json


def _json(args: list[str]):
    try:
        result = subprocess.run(['herdr', *args], capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        raise ValueError('Herdr is required; install it from https://herdr.dev.') from None
    except subprocess.TimeoutExpired:
        raise ValueError('Herdr did not respond; inspect the project connection and retry.') from None
    if result.returncode:
        raise ValueError('Herdr command failed: ' + ' '.join(args[:3]))
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError('Herdr returned an invalid command response') from None


def _session(project) -> str:
    if not re.fullmatch('[0-9a-f]{24}', project.project_id):
        raise ValueError('Invalid Herdr project identity')
    return 'project-' + project.project_id


def _workspace(project, root: str, prefix: list[str]):
    if not isinstance(root, str) or not root.startswith('/') or any(c in root for c in '\0\r\n') or '..' in Path(root).parts:
        raise ValueError('Herdr checkout must be an absolute path without traversal')
    response = _json([*prefix, 'workspace', 'list'])
    try:
        rows = response['result']['workspaces']
        matches = [row for row in rows if (row.get('worktree') or {}).get('checkout_path') == root]
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Herdr did not return workspace checkout metadata') from None
    if matches:
        workspace_id = matches[0].get('workspace_id')
    else:
        created = _json([*prefix, 'workspace', 'create', '--cwd', root,
                         '--label', project.root.name, '--no-focus'])
        try:
            workspace_id = created['result']['workspace']['workspace_id']
        except (KeyError, TypeError):
            raise ValueError('Herdr did not return its new workspace ID') from None
    if not isinstance(workspace_id, str) or not workspace_id or workspace_id.startswith('-'):
        raise ValueError('Herdr returned an invalid workspace ID')
    _json([*prefix, 'workspace', 'focus', workspace_id])


def _toml(value) -> str:
    # Serialize a private semantic copy, preserving every parsed user setting.
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return str(value).lower() if not isinstance(value, float) or math.isfinite(value) else ('nan' if math.isnan(value) else 'inf' if value > 0 else '-inf')
    if isinstance(value, list):
        return '[' + ', '.join(_toml(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{ ' + ', '.join(json.dumps(key) + ' = ' + _toml(item) for key, item in value.items()) + ' }'
    raise ValueError('Unsupported setting in Herdr configuration')


def _attach_env(state: Path) -> dict:
    env = dict(os.environ)
    if env.get('HERDR_ENV') != '1':
        return env
    home = Path(env.get('HOME') or Path.home())
    source = Path(env.get('HERDR_CONFIG_PATH') or Path(env.get('XDG_CONFIG_HOME') or home / '.config') / 'herdr/config.toml').absolute()
    try:
        settings = tomllib.loads(source.read_text())
    except FileNotFoundError:
        settings = {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ValueError('Cannot read the effective Herdr configuration') from None
    settings.setdefault('experimental', {})['allow_nested'] = True
    sound = settings.get('ui', {}).get('sound', {})
    for name in ('path', 'done_path', 'request_path'):
        value = sound.get(name)
        if isinstance(value, str) and value and not Path(value).is_absolute():
            sound[name] = str(source.parent / value)
    target = state / 'herdr-nested.toml'
    encoded = ('\n'.join(json.dumps(key) + ' = ' + _toml(value) for key, value in settings.items()) + '\n').encode()
    # Refuse serialization drift before giving the private config to Herdr.
    if tomllib.loads(encoded.decode()) != settings:
        raise ValueError('Cannot preserve the effective Herdr configuration')
    atomic_write(target, encoded)
    env['HERDR_CONFIG_PATH'] = str(target)
    return env


def open_local(project, state: Path) -> int:
    """Focus this checkout in the current Herdr, or attach a named local session."""
    if os.environ.get('HERDR_ENV') == '1':
        _workspace(project, str(project.root), [])
        return 0
    return subprocess.call(['herdr', '--session', _session(project)], cwd=project.root)


def open_remote(project, state: Path, remote_root: str) -> int:
    """Register/reuse the project's remote machine and attach its persistent UI."""
    state = Path(state)
    alias, session = configure_ssh(project, state), _session(project)
    receipt_path = state / 'herdr-machine.json'
    try:
        receipt = read_json(receipt_path)
    except FileNotFoundError:
        receipt = {}
    rows = _json(['machine', 'list', '--json'])
    def matching(rows):
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError('Herdr returned an invalid machine catalog')
        return [row for row in rows if row.get('target') == alias and row.get('session') == session]
    matches = matching(rows)
    if not matches:
        result = subprocess.call(['herdr', 'machine', 'add', alias, '--label', project.root.name + ' (Codespace)',
                                  '--remote-session', session])
        if result:
            return result
        matches = matching(_json(['machine', 'list', '--json']))
        if not matches:
            raise ValueError('Herdr setup did not save the expected project machine')
    machine = next((row for row in matches if row.get('id') == receipt.get('machine_id')), matches[0])
    machine_id = machine.get('id')
    if not isinstance(machine_id, str) or not machine_id or machine_id.startswith('-'):
        raise ValueError('Herdr returned an invalid machine ID')
    if not machine.get('enabled'):
        result = subprocess.call(['herdr', 'machine', 'enable', machine_id])
        if result:
            return result
    atomic_write(receipt_path, json.dumps({'machine_id': machine_id, 'alias': alias, 'session': session}).encode() + b'\n')
    _workspace(project, remote_root, ['--machine', machine_id])
    return subprocess.call(['herdr', '--remote', alias, '--session', session], env=_attach_env(state), cwd=project.root)
