"""Project Herdr workspaces, using public CLI IDs and native SSH setup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time as clock

from .ssh_hosts import configure_ssh
from .state import atomic_write, read_json


class _NotRunning(ValueError):
    """The native API explicitly reported an absent named-session server."""


class _RemoteNotReady(ValueError):
    """A read-only probe found the saved remote session's API still starting."""


def _json(args: list[str], *, timeout=90):
    remote_probe = args[:1] == ['--machine'] and args[2:] == ['workspace', 'list']
    try:
        result = subprocess.run(['herdr', *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ValueError('Herdr is required; install it from https://herdr.dev.') from None
    except subprocess.TimeoutExpired:
        if remote_probe:
            raise _RemoteNotReady('Herdr remote API has not answered yet') from None
        raise ValueError('Herdr did not respond; inspect the project connection and retry.') from None
    if result.returncode:
        try:
            absent = json.loads(result.stderr).get('error', {}).get('code') == 'server_not_running'
        except (ValueError, TypeError, AttributeError):
            absent = False
        if absent:
            raise _NotRunning('Herdr session is stopped')
        if (remote_probe and 'failed to connect to remote Herdr API socket ' in result.stderr
                and any(reason in result.stderr for reason in (
                    'Connection refused (os error 111)', 'No such file or directory (os error 2)'))):
            raise _RemoteNotReady('Herdr remote API socket is still starting')
        operation = args[:4] if args[:1] in (['--machine'], ['--session']) else args[:2]
        raise ValueError('Herdr command failed: ' + ' '.join(operation))
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError('Herdr returned an invalid command response') from None


def _session(project) -> str:
    if not re.fullmatch('[0-9a-f]{24}', project.project_id):
        raise ValueError('Invalid Herdr project identity')
    return 'project-' + project.project_id


def _workspace_list(prefix):
    if prefix[:1] != ['--machine']:
        return _json([*prefix, 'workspace', 'list'])
    # Enabling a saved machine starts its GUI connection asynchronously. After
    # a Codespace restart the persisted Unix socket may outlive its API server.
    # Finish this read-only readiness handshake before any workspace mutation.
    deadline, waiting = clock.monotonic() + 45, False
    while True:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise ValueError('Herdr remote API was not ready within 45 seconds; inspect the saved machine connection and retry task work:cs.')
        try:
            return _json([*prefix, 'workspace', 'list'], timeout=min(15, remaining))
        except (_RemoteNotReady, _NotRunning):
            if not waiting:
                print('Waiting for Herdr’s remote session to become ready…', flush=True)
                waiting = True
            remaining = deadline - clock.monotonic()
            if remaining > 0:
                clock.sleep(min(.5, remaining))


def _workspace(project, root: str, prefix: list[str]):
    if not isinstance(root, str) or not root.startswith('/') or any(c in root for c in '\0\r\n') or '..' in Path(root).parts:
        raise ValueError('Herdr checkout must be an absolute path without traversal')
    response = _workspace_list(prefix)
    try:
        rows = response['result']['workspaces']
        matches = [row for row in rows if (row.get('worktree') or {}).get('checkout_path') == root]
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Herdr did not return workspace checkout metadata') from None
    if not matches:
        # Ordinary `workspace create --cwd` entries have no worktree metadata.
        # Pane cwd is the launch directory; foreground_cwd changes as a shell
        # navigates and must never turn an unrelated workspace into a match.
        for row in sorted(rows, key=lambda row: not row.get('focused', False)):
            if row.get('worktree'):
                continue
            candidate = row.get('workspace_id')
            if not isinstance(candidate, str) or not candidate or candidate.startswith('-'):
                raise ValueError('Herdr returned an invalid workspace ID')
            panes = _json([*prefix, 'pane', 'list', '--workspace', candidate])
            try:
                found = any(pane.get('workspace_id') == candidate and pane.get('cwd') == root
                            for pane in panes['result']['panes'])
            except (KeyError, TypeError, AttributeError):
                raise ValueError('Herdr did not return pane launch directories') from None
            if found:
                matches.append(row)
                break
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
    if prefix[:1] == ['--machine']:
        _close_idle_home_workspaces(rows, workspace_id, prefix)
    _json([*prefix, 'workspace', 'focus', workspace_id])


def _close_idle_home_workspaces(rows, keep: str, prefix: list[str]) -> None:
    # A Codespace's Herdr server starts with a home workspace beside the project
    # one. Close it only while it is a single idle shell; never touch the Mac's.
    for row in rows:
        candidate = row.get('workspace_id')
        if (candidate == keep or row.get('worktree') or row.get('label') != '~'
                or row.get('tab_count') != 1 or row.get('pane_count') != 1
                or not isinstance(candidate, str) or not candidate or candidate.startswith('-')):
            continue
        try:
            panes = _json([*prefix, 'pane', 'list', '--workspace', candidate])['result']['panes']
        except (KeyError, TypeError):
            continue
        if all(pane.get('agent') is None and pane.get('foreground_process') is None for pane in panes):
            _json([*prefix, 'workspace', 'close', candidate])


def _matching_machines(alias, session):
    rows = _json(['machine', 'list', '--json'])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('Herdr returned an invalid machine catalog')
    matches = [row for row in rows if row.get('target') == alias and row.get('session') == session]
    for row in matches:
        machine_id = row.get('id')
        if not isinstance(machine_id, str) or not machine_id or machine_id.startswith('-'):
            raise ValueError('Herdr returned an invalid machine ID')
    return matches


def disconnect_remote(project) -> int:
    """Disable only this project's saved machine, preserving its remote session."""
    if os.environ.get('CODESPACES') == 'true':
        raise ValueError('Run task work:disconnect on your Mac to disconnect its Herdr connection')
    matches = _matching_machines('devcontainer-' + project.project_id, _session(project))
    for machine in matches:
        if machine.get('enabled'):
            result = subprocess.call(['herdr', 'machine', 'disable', machine['id']])
            if result:
                return result
    print('Codespace disconnected from Herdr. Reconnect with task work:cs.')
    return 0


def version_line(text):
    """`herdr --version` output when it is one 'herdr …' line, otherwise None."""
    text = text.strip()
    return text if text.startswith('herdr ') and '\n' not in text else None


def _local_version():
    """This Mac's `herdr --version`, or None when it cannot be read."""
    try:
        result = subprocess.run(['herdr', '--version'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return version_line(result.stdout) if result.returncode == 0 else None


def open_remote(project, state: Path, remote_root: str, remote_herdr: str | None = None) -> int:
    """Connect the project's saved machine and open local Herdr when needed."""
    state = Path(state)
    alias, session = configure_ssh(project, state), _session(project)
    receipt_path = state / 'herdr-machine.json'
    try:
        receipt = read_json(receipt_path)
    except FileNotFoundError:
        receipt = {}
    binding = state / 'scratch.json'
    codespace = read_json(binding).get('codespace') if binding.exists() else None
    matches = _matching_machines(alias, session)
    # Only setup installs the Herdr this Mac runs on a Codespace (it asks first),
    # saving a fresh profile. A Codespace with none, or another version, gets
    # one. When that was not read, a profile saved for another Codespace counts
    # as lacking it.
    previous, versions = receipt.get('codespace'), receipt.get('herdr_versions')
    pair = None
    if remote_herdr is None:
        lacking = None not in (previous, codespace) and previous != codespace
    else:
        local = _local_version() if remote_herdr else None
        pair = [local, remote_herdr]
        lacking = not remote_herdr or (local is not None and remote_herdr != local)
        if lacking and remote_herdr and pair == versions:
            # Setup already ran for this pair and kept the remote, which Herdr
            # accepts as compatible; asking again on every connection would loop.
            print(f'Herdr on this Codespace is {remote_herdr}; this Mac runs {local}. Keeping the profile setup saved.')
            lacking = False
    replaced = {row['id'] for row in matches} if lacking else set()
    if not matches or replaced:
        if pair is not None:
            versions = pair  # An unread probe settles nothing, so it erases nothing.
        result = subprocess.call(['herdr', 'machine', 'add', alias, '--label', project.root.name + ' (Codespace)',
                                  '--remote-session', session])
        if result:
            return result
        found = _matching_machines(alias, session)
        matches = [row for row in found if row['id'] not in replaced]
        if not matches:
            raise ValueError('Herdr setup did not save the expected project machine'
                             + ('; existing profile kept' if replaced else ''))
        for row in found:
            if row['id'] in replaced:
                result = subprocess.call(['herdr', 'machine', 'remove', row['id']])
                if result:
                    return result
    machine = next((row for row in matches if row.get('id') == receipt.get('machine_id')), matches[0])
    machine_id = machine.get('id')
    if not machine.get('enabled'):
        result = subprocess.call(['herdr', 'machine', 'enable', machine_id])
        if result:
            return result
    saved = {'machine_id': machine_id, 'alias': alias, 'session': session, 'codespace': codespace}
    if versions:
        saved['herdr_versions'] = versions
    atomic_write(receipt_path, json.dumps(saved).encode() + b'\n')
    _workspace(project, remote_root, ['--machine', machine_id])
    print('Codespace connected. Select its Codespace group in Herdr.')
    if os.environ.get('HERDR_ENV') == '1':
        return 0
    return subprocess.call(['herdr'], cwd=project.root)
