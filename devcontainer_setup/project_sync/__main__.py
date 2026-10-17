"""Project workspace and recovery commands, shipped to each consumer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from . import scratch
from .state import discover_project, state_dir


def parser():
    result = argparse.ArgumentParser(description='Open project work in Herdr with Mac recovery checkpoints.')
    result.add_argument('--project', type=Path, default=Path.cwd())
    groups = result.add_subparsers(dest='group', required=True)
    work = groups.add_parser('work', help='Open local Herdr work, or --codespace with Mac protection')
    work.add_argument('--agent', choices=('codex', 'claude'), help='Launch a new native conversation on this host')
    work.add_argument('--resume', action='store_true', help='Choose a project conversation to continue')
    action = work.add_mutually_exclusive_group()
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
        if args.resume and not args.agent:
            raise ValueError('Use task work:codex -- --resume or task work:claude -- --resume')
        if args.agent and (args.codespace or args.disconnect or args.status or args.stop):
            raise ValueError('Open task work:cs first, then launch the agent in its Herdr workspace')
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
        if args.agent:
            from .conversations import launch
            return launch(workspace.local_project(project, state), state, args.agent, resume=args.resume)
        return workspace.local(project, state)
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
