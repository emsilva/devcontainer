"""Codespace connection and project-data sync commands shipped to consumers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import scratch
from .state import discover_project, state_dir


def _port(value):
    port = int(value)
    if not 1 <= port <= 55535:
        raise argparse.ArgumentTypeError('port must be 1-55535')
    return port


def parser():
    result = argparse.ArgumentParser(description='Connect Codespace Herdr and sync project scratch and conversations.')
    result.add_argument('--project', type=Path, default=Path.cwd())
    groups = result.add_subparsers(dest='group', required=True)
    work = groups.add_parser('work', help='Manage the Codespace connection; run codex or claude directly')
    action = work.add_mutually_exclusive_group(required=True)
    action.add_argument('--codespace', action='store_true')
    action.add_argument('--disconnect', action='store_true', help='Disconnect this project’s saved Codespace machine from Herdr')
    action.add_argument('--status', action='store_true')
    action.add_argument('--stop', action='store_true')
    action.add_argument('--armed', action='store_true', help='Arm automatic Codespace restart after the 12-hour stop')
    action.add_argument('--disarm', action='store_true', help='Disarm automatic Codespace restart')
    action.add_argument('--hold', metavar='VALUE', help='No restarts until a local time like 09:30, or off')
    action.add_argument('--codespace-stop', action='store_true', help='Stop the Codespace and keep it stopped')
    action.add_argument('--codespace-restart', action='store_true', help='Restart the Codespace now')
    action.add_argument('--logs', action='store_true', help='Show recent keeper and worker logs')
    work.add_argument('--follow', action='store_true')
    work.add_argument('--port', type=_port, action='append', default=[], help=argparse.SUPPRESS)
    work.add_argument('--no-port', type=_port, action='append', default=[], help=argparse.SUPPRESS)
    work.add_argument('--json', action='store_true', help='Full offline status detail as JSON')
    sync = groups.add_parser('scratch', help='Advanced scratch synchronization controls').add_subparsers(dest='action', required=True)
    configure = sync.add_parser('configure')
    configure.add_argument('--codespace', required=True)
    configure.add_argument('--remote-root', required=True)
    for action in ('start', 'stop', 'status', 'backup'):
        sync.add_parser(action)
    logs = sync.add_parser('logs')
    logs.add_argument('--follow', action='store_true')
    return result


def _print(value):
    print(json.dumps(value, indent=2, sort_keys=True))


def _arm(project, state, args):
    # Arming only records state here; the keeper that acts on it starts elsewhere.
    # Network calls and the prompt run unlocked; the lock covers only the read and write.
    from . import arming
    from .codespaces import Codespaces
    from .state import locked
    user = arming.account(os.environ)
    row = Codespaces(project, state).space()
    if not row:
        raise ValueError('Pair a Codespace first: run task work:cs')
    owner = (row.get('billable_owner') or {}).get('login')
    if not owner:
        raise ValueError('The Codespace does not name a billing owner; check it in GitHub')
    text = arming.consent_text(project.root.name, row['name'], owner)
    digest = hashlib.sha256(text.encode()).hexdigest()
    current = arming.read(state)
    if (current.get('armed') and current.get('consent_sha256') == digest
            and current.get('codespace') == row['name'] and current.get('gh_user') == user):
        if args.port or args.no_port:
            with locked(state / 'workspace-control.lock', blocking=True):
                latest = arming.read(state)
                ports = arming.resolve_ports(latest.get('ports', []), args.port, args.no_port)
                arming.write(state, dict(latest, ports=ports))
            print('Already armed as ' + user + '; ports ' + (' '.join(map(str, ports)) or 'none'))
        else:
            print('Already armed as ' + user)
        return 0
    print(text)
    try:
        answer = input('> ')
    except (EOFError, KeyboardInterrupt):
        raise ValueError('Arming cancelled') from None
    if answer.strip() != 'arm':
        raise ValueError('Not armed: the confirmation was not "arm"')
    ports = arming.resolve_ports(arming.default_ports(project.root), args.port, args.no_port)
    with locked(state / 'workspace-control.lock', blocking=True):
        arming.arm(state, gh_user=user, codespace=row['name'], consent=text, billable_owner=owner,
                   ports=ports, now=time.time())
    print('Armed as ' + user + '; ports ' + (' '.join(map(str, ports)) or 'none'))
    return 0


def _arming_edit(args, state):
    from . import arming
    from .state import locked
    with locked(state / 'workspace-control.lock', blocking=True):
        value = arming.read(state)
        if args.hold is not None:
            if not value.get('armed'):
                raise ValueError('This project is not armed; run task work:cs:armed first')
            until = arming.hold_until(args.hold, time.time())
            arming.write(state, dict(value, hold_until=until))
            print('Hold off' if until is None else 'Holding restarts until ' + arming.format_time(until))
        elif value.get('armed'):
            arming.write(state, dict(value, armed=False))
            print('Disarmed')
        else:
            print('Not armed')
    return 0


def run(args):
    project = discover_project(args.project.resolve())
    state = state_dir(project)
    if args.group == 'work':
        from . import workspace
        if args.status:
            value = workspace.status(project, state)
            if args.json:
                _print(value)
            else:
                print(workspace.status_text(value))
            return 0
        if args.stop:
            _print(scratch.stop(state))
            return 0
        if args.disconnect:
            from .herdr import disconnect_remote
            return disconnect_remote(project)
        if args.disarm or args.hold is not None:
            return _arming_edit(args, state)
        if args.logs:
            raise ValueError('task work:logs is not available yet')
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError('Work entrypoints require a terminal; use task work:status for scripts')
        if args.armed:
            return _arm(project, state, args)
        if args.codespace_stop or args.codespace_restart:
            raise ValueError('Stopping and restarting the Codespace from a task is not available yet')
        if args.codespace:
            return workspace.remote(project, state)
    if args.action == 'configure':
        _print(scratch.configure(project.root, state, args.codespace, args.remote_root))
    elif args.action == 'start':
        _print(scratch.start(project.root, state))
    elif args.action == 'stop':
        _print(scratch.stop(state))
    elif args.action == 'status':
        _print(scratch.status(state))
    elif args.action == 'backup':
        _print({'backup': str(scratch.backup(project.root, state))})
    else:
        path = state / 'scratch.log'
        if not path.exists():
            print('No worker log yet. Run task work:cs on your Mac to connect.')
        else:
            return subprocess.call(['tail', '-n', '80'] + (['-F'] if args.follow else []) + [str(path)])
    return 0


def main(argv=None):
    try:
        return run(parser().parse_args(argv))
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
