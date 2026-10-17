"""Project Herdr workspaces, using public CLI IDs and native SSH setup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time as clock

from .ssh_hosts import configure_ssh
from .state import _parent, _regular, atomic_write, read_json


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
    _json([*prefix, 'workspace', 'focus', workspace_id])


def open_local(project, state: Path) -> int:
    """Focus this checkout in the current Herdr, or attach a named local session."""
    if os.environ.get('HERDR_ENV') == '1':
        _workspace(project, str(project.root), [])
        return 0
    prefix = ['--session', _session(project)]
    try:
        _workspace(project, str(project.root), prefix)
    except _NotRunning:
        # A restored named session can contain an older checkout. Start its
        # native persistent server, then select the actual recovery root before
        # attaching a client. Never reset or close its existing workspaces.
        log = Path(state) / 'herdr-local-server.log'
        with _parent(log, create=True, private=True) as (parent, name):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, 'ab') as output:
                _regular(output.fileno())
                os.fchmod(output.fileno(), 0o600)
                child = subprocess.Popen(['herdr', *prefix, 'server'], cwd=project.root,
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                        start_new_session=True)
        deadline = clock.monotonic() + 15
        while True:
            try:
                _workspace(project, str(project.root), prefix)
                break
            except _NotRunning:
                if child.poll() is not None or clock.monotonic() >= deadline:
                    raise ValueError(f'Project Herdr server did not start; inspect {log}') from None
                clock.sleep(.1)
    return subprocess.call(['herdr', *prefix], cwd=project.root)


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


def open_remote(project, state: Path, remote_root: str) -> int:
    """Connect the project's saved machine and open local Herdr when needed."""
    state = Path(state)
    alias, session = configure_ssh(project, state), _session(project)
    receipt_path = state / 'herdr-machine.json'
    try:
        receipt = read_json(receipt_path)
    except FileNotFoundError:
        receipt = {}
    matches = _matching_machines(alias, session)
    if not matches:
        result = subprocess.call(['herdr', 'machine', 'add', alias, '--label', project.root.name + ' (Codespace)',
                                  '--remote-session', session])
        if result:
            return result
        matches = _matching_machines(alias, session)
        if not matches:
            raise ValueError('Herdr setup did not save the expected project machine')
    machine = next((row for row in matches if row.get('id') == receipt.get('machine_id')), matches[0])
    machine_id = machine.get('id')
    if not machine.get('enabled'):
        result = subprocess.call(['herdr', 'machine', 'enable', machine_id])
        if result:
            return result
    atomic_write(receipt_path, json.dumps({'machine_id': machine_id, 'alias': alias, 'session': session}).encode() + b'\n')
    _workspace(project, remote_root, ['--machine', machine_id])
    print('Codespace connected. Select its Codespace group in Herdr.')
    if os.environ.get('HERDR_ENV') == '1':
        return 0
    return subprocess.call(['herdr'], cwd=project.root)
