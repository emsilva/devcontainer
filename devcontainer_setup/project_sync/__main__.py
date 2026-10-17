"""Project continuity commands, also shipped as a self-contained consumer package."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import subprocess
import sys
import uuid

from . import agents, ownership, scratch, sessions
from .state import discover_project, host_id, state_dir


def parser():
    result = argparse.ArgumentParser(description='Synchronize project scratch files and hand off native agent sessions.')
    result.add_argument('--project', type=Path, default=Path.cwd())
    groups = result.add_subparsers(dest='group', required=True)
    groups.add_parser('work', help='Continue a project conversation here or in its Codespace')
    sync = groups.add_parser('scratch').add_subparsers(dest='action', required=True)
    configure = sync.add_parser('configure', help='Pair this checkout with an already running Codespace')
    configure.add_argument('--codespace')
    configure.add_argument('--remote-root')
    for action in ('start', 'stop', 'status', 'backup'):
        sync.add_parser(action)
    logs = sync.add_parser('logs')
    logs.add_argument('--follow', action='store_true')
    agent = groups.add_parser('agent').add_subparsers(dest='action', required=True)
    for tool in ('codex', 'claude'):
        launch = agent.add_parser(tool, help='Launch under the project session ownership lease')
        choose = launch.add_mutually_exclusive_group()
        choose.add_argument('--session', type=sessions.session_uuid)
        choose.add_argument('--new', action='store_true')
    listing = agent.add_parser('sessions')
    listing.add_argument('--agent', choices=('codex', 'claude'), required=True)
    transfer = agent.add_parser('handoff')
    transfer.add_argument('--agent', choices=('codex', 'claude'), required=True)
    transfer.add_argument('--session', type=sessions.session_uuid, required=True)
    transfer.add_argument('--to', choices=('mac', 'codespace'), required=True)
    transfer.add_argument('--dry-run', action='store_true')
    recover = agent.add_parser('recover')
    recover.add_argument('--session', type=sessions.session_uuid, required=True)
    recover.add_argument('--agent', choices=('codex', 'claude'), required=True)
    choice = recover.add_mutually_exclusive_group(required=True)
    choice.add_argument('--adopt', action='store_true', help='Explicitly adopt an existing, verified local conversation')
    choice.add_argument('--crashed-local', action='store_true', help='Recover a crashed local writer after checking its process and files')
    return result


def _print(value):
    print(json.dumps(value, indent=2, sort_keys=True))


def _claude_version():
    result = subprocess.run(['claude', '--version'], capture_output=True, text=True, timeout=15)
    match = re.fullmatch(r'(2\.1\.\d+) \(Claude Code\)\n?', result.stdout)
    if result.returncode or not match:
        raise ValueError('Cannot establish the installed Claude Code version')
    return match.group(1)


def _select_session(project, agent, state):
    if not sys.stdin.isatty():
        return None
    candidates = sessions.list_sessions(project, agent, sessions.native_home(agent), state=state)
    for index, row in enumerate(candidates, 1):
        owner = ownership.owner_status(state, agent, row['session_id'])
        print(f"{index}. {row['session_id']} ({owner['status']})", file=sys.stderr)
    answer = input('Conversation number, exact UUID, or Enter for a new conversation: ').strip()
    if not answer:
        return None
    if answer.isdecimal() and 1 <= int(answer) <= len(candidates):
        return candidates[int(answer) - 1]['session_id']
    return sessions.session_uuid(answer)


def run(args, extra=()):
    if args.group == 'work':
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError('task work requires a terminal. For scripts, use scratch status or agent sessions.')
        from .work import run as work
        project = discover_project(args.project.resolve())
        return work(project, state_dir(project))
    project = discover_project(args.project.resolve())
    state = state_dir(project)
    if args.group == 'scratch':
        if args.action == 'configure':
            codespace, remote_root = args.codespace, args.remote_root
            if sys.stdin.isatty():
                codespace = codespace or input('Existing Codespace name: ').strip()
                remote_root = remote_root or input(f'Remote checkout [/workspaces/{project.root.name}]: ').strip() or '/workspaces/' + project.root.name
            if not codespace or not remote_root:
                raise ValueError('Use scratch:configure -- --codespace NAME --remote-root /workspaces/PROJECT')
            _print(scratch.configure(project.root, state, codespace, remote_root))
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
                print('No scratch log yet. Run task scratch:start after pairing.')
            else:
                command = ['tail', '-n', '80'] + (['-F'] if args.follow else []) + [str(path)]
                return subprocess.call(command)
        return 0

    if args.action == 'sessions':
        rows = sessions.list_sessions(project, args.agent, sessions.native_home(args.agent), state=state)
        for row in rows:
            row['ownership'] = ownership.owner_status(state, args.agent, row['session_id'])
        _print(rows)
        return 0
    if args.action == 'handoff':
        from .handoff import check, handoff
        operation = check if args.dry_run else handoff
        _print(operation(project, args.agent, args.session, args.to, state))
        return 0
    host = host_id()
    if args.action == 'recover':
        home = sessions.native_home(args.agent)
        # Validate complete project scope and dependencies, not only filename ID.
        sessions.export_snapshot(project, args.agent, args.session, home,
                                 source_host=host, generation=0, state=state,
                                 agent_version=_claude_version() if args.agent == 'claude' else None)
        paths = agents.native_paths(project, args.agent, args.session, home, state=state)
        operation = ownership.adopt_local if args.adopt else ownership.recover_local
        _print(operation(state, args.agent, args.session, host, native_paths=paths))
        return 0

    agent = args.action
    session_id = args.session or (None if args.new else _select_session(project, agent, state))
    new = session_id is None
    if new:
        if agent == 'codex':
            session_id = agents.create_codex_session(project, state=state, host=host)
            new = False  # Native bootstrap already created the ownership receipt.
        else:
            session_id = str(uuid.uuid4())
    print(f'{agent} session: {session_id}', file=sys.stderr)
    return agents.launch(project, agent, session_id, state=state, host=host,
                         new=new, extra_args=extra)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if '--' in argv:
        index = argv.index('--')
        argv, extra = argv[:index], argv[index + 1:]
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if extra and (args.group != 'agent' or args.action not in ('codex', 'claude')):
        command_parser.error('Native arguments after -- apply only to agent codex/claude')
    try:
        return run(args, extra)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        # Native/provider stderr is not relayed by the transport. Controlled
        # validation errors remain actionable without dumping private payloads.
        print('Project sync: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
