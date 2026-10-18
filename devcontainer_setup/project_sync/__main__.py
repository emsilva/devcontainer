"""Codespace connection and project-data sync commands shipped to consumers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from . import scratch
from .state import discover_project, state_dir


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


def run(args):
    project = discover_project(args.project.resolve())
    state = state_dir(project)
    if args.group == 'work':
        from . import workspace
        if args.status:
            _print(workspace.status(project, state))
            return 0
        if args.stop:
            _print(scratch.stop(state))
            return 0
        if args.disconnect:
            from .herdr import disconnect_remote
            return disconnect_remote(project)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError('Work entrypoints require a terminal; use task work:status for scripts')
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
