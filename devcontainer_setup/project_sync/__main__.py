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
    work.add_argument('--rebind', metavar='CODESPACE',
                      help='Move scratch and conversation sync to another Codespace of this repository')
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
    # Records consent; _armed then starts the keeper. Network calls and the
    # prompt run unlocked; the lock covers only the read and write.
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


def _armed(project, state, args):
    """Spec 6.1 steps 2-7, run by workspace.remote once the worker is ready and before Herdr."""
    from . import arming, keeper, workspace
    from .state import read_json
    name = read_json(state / scratch.CONFIG)['codespace']
    pids = keeper.foreign_processes(state, name)
    if pids:
        raise ValueError('Another gh process holds ' + name + ' (pid ' + ', '.join(map(str, pids))
                         + '); stop it before arming, because gh can start the Codespace and it holds the ports.')
    before = arming.read(state)
    _arm(project, state, args)
    if before.get('armed') and arming.read(state).get('ports') != before.get('ports'):
        # R20: the running keep-alive child carries the old ports; a new keeper starts one with the new list.
        keeper.stop(state)
    started = keeper.start(project.root, state)
    if keeper.wait_ready(state, started['nonce']).get('phase') == 'starting':
        print('The keeper has not reported within 60 seconds; inspect task work:logs.')
    print('\n'.join(workspace.recovery_lines(workspace.recovery(state))))


def _arming_edit(args, state):
    from . import arming, keeper
    from .state import locked
    with locked(state / 'workspace-control.lock', blocking=True):
        value = arming.read(state)
        if args.hold is not None:
            if not value.get('armed'):
                raise ValueError('This project is not armed; run task work:cs:armed first')
            until = arming.hold_until(args.hold, time.time())
            arming.write(state, dict(value, hold_until=until))
            print('Hold off' if until is None else 'Holding restarts until ' + arming.format_time(until))
            return 0
        if value.get('armed'):
            arming.write(state, dict(value, armed=False))
    # By fact, whatever arming.json said: a keeper left running is stopped too.
    running = keeper.status(state)['running']
    keeper.stop(state)
    print(('Disarmed' if value.get('armed') else 'Not armed') + ('; keeper stopped' if running else ''))
    return 0


def _wait_stopping(api, now, sleep):
    # A stop takes effect asynchronously; until the Codespace leaves Available the
    # start routine would take the old run for the new one.
    from . import lifecycle
    deadline = now() + lifecycle.SHUTDOWN_WAIT
    while True:
        reading = api.read()
        if reading.ok and reading.state != 'Available':
            return
        if now() >= deadline:
            raise ValueError('The Codespace has not begun to stop within 20 minutes; inspect it in GitHub, '
                             'then retry task work:cs:restart')
        sleep(lifecycle.POLL)


def _owner_power(project, state, *, restart, api=None, now=time.time, sleep=time.sleep):
    """Spec 6.8 work:cs:stop and work:cs:restart, with the owner present.

    The owner stop is recorded before the stop request, so the keeper reads the stop as
    OWNER and never restarts it. A restart then starts the Codespace outside the budget
    (D6) and, for an armed project, hands restore and resume to the keeper.
    """
    from . import arming, evidence, keeper, lifecycle
    from .codespaces import start_error
    from .state import locked, read_json
    if os.environ.get('CODESPACES') == 'true':
        verb = 'restart' if restart else 'stop'
        raise ValueError(f'Run task work:cs:{verb} on your Mac to {verb} the Codespace')
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if os.environ.get(name):
            raise ValueError(f'Unset {name} before stopping or restarting the Codespace; it must use a real gh account.')
    try:
        codespace = read_json(state / scratch.CONFIG)['codespace']
    except FileNotFoundError:
        raise ValueError('Pair a Codespace first: run task work:cs') from None
    value = arming.read(state)
    armed = bool(value.get('armed'))
    if armed and value.get('codespace') != codespace:
        raise ValueError('The paired Codespace is not the armed one; run task work:cs:armed')
    # The armed account, as the keeper uses; otherwise the active gh account, as work:cs.
    api = api or lifecycle.LifecycleAPI(codespace, value.get('gh_user') if armed else None)
    with locked(state / 'workspace-control.lock', blocking=True):
        stopped_at = now()
        arming.record_owner_stop(state, stopped_at)
    status = api.post_stop()
    # The record stays on every failure (R33): a refused stop during GitHub's own stop would
    # otherwise let the keeper restart a Codespace you asked to keep stopped.
    if status == 404 and api.confirm_deleted() is True:
        raise ValueError(f'{codespace} no longer exists on GitHub; there is nothing to stop.')
    if 400 <= status < 500:
        raise ValueError(f'GitHub refused the stop request (HTTP {status}); it is recorded as your stop, so the '
                         'keeper will not restart it. Check task work:status.')
    if not 200 <= status < 300:
        raise ValueError(f'The stop request did not complete (HTTP {status}); it may still take effect, and the '
                         'keeper treats it as your stop. Inspect task work:status, then retry.')
    if not restart:
        print(f'Stopping {codespace}; it stays stopped until task work:cs' + (':armed' if armed else '')
              + ' starts it again.')
        return 0
    _wait_stopping(api, now, sleep)
    rid = lifecycle.owner_rid(now())
    # No ledger and no gate: the owner's restart is outside the budget (D6).
    result = lifecycle.start(api, rid=rid, now=now, sleep=sleep, say=lambda text: print(text + '…', file=sys.stderr))
    if result['outcome'] != 'available':
        raise start_error(result)
    if result['posted_at'] is not None:
        evidence.record_start(state, codespace, result['posted_at'], 'started')
    back_at = now()
    if not armed:
        print(f'{codespace} restarted ({rid}): stopped {arming.format_time(stopped_at)}, back '
              f'{arming.format_time(back_at)}.')
        return 0
    with locked(state / 'workspace-control.lock', blocking=True):
        latest = arming.read(state)
        if latest.get('armed'):
            arming.write(state, dict(latest, pending_restore={'rid': rid, 'stopped_at': stopped_at,
                                                              'back_at': back_at}))
    print(f'{codespace} is back ({rid}); its keeper reconnects Herdr and tells the lead to resume. Follow it '
          'with task work:logs.')
    if not keeper.status(state)['running']:
        print('The keeper is not running, so nothing will tell the lead; run task work:cs:armed.')
    return 0


def _log_lines(path, count=80):
    # The last lines of one log, each keyed by its leading UTC time; an older line
    # without one keeps the key of the line before it.
    try:
        if path.is_symlink():
            return []
        with path.open('rb') as source:
            source.seek(0, os.SEEK_END)
            source.seek(max(0, source.tell() - 256 * 1024))
            text = source.read().decode(errors='replace')
    except (FileNotFoundError, NotADirectoryError):
        return []
    lines, key = [], ''
    for line in text.splitlines()[-count:]:
        stamp = line[:24]
        if len(stamp) == 24 and stamp.endswith('Z') and stamp[:4].isdigit() and stamp[4] == '-':
            key = stamp
        lines.append((key, line))
    return lines


def _logs(state, follow):
    from . import keeper
    sources = (('keeper', state / keeper.LOG), ('worker', state / 'scratch.log'))
    merged = sorted(((key, label + ' ' + line) for label, path in sources for key, line in _log_lines(path)),
                    key=lambda row: row[0])
    if not merged and not follow:
        print('No keeper or worker log yet. Run task work:cs on your Mac to connect.')
        return 0
    for _, line in merged:
        print(line)
    if follow:
        sys.stdout.flush()
        return subprocess.call(['tail', '-n', '0', '-F'] + [str(path) for _, path in sources])
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
            from . import keeper
            try:
                stopped = keeper.stop(state)
                summary, failure = {'running': stopped['running'], 'phase': stopped.get('phase')}, None
            except Exception as error:
                # Whatever the keeper step raises (a hung ps included), the worker still stops.
                failure = str(error) if isinstance(error, ValueError) else type(error).__name__
                summary = {'running': None, 'error': failure}
            _print(dict(scratch.stop(state), keeper=summary))
            if failure is not None:
                raise ValueError('The worker is stopped, but the keeper was not (' + failure
                                 + '); inspect task work:status, then retry task work:stop.')
            return 0
        if args.disconnect:
            from .herdr import disconnect_remote
            return disconnect_remote(project)
        if args.disarm or args.hold is not None:
            return _arming_edit(args, state)
        if args.logs:
            return _logs(state, args.follow)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError('Work entrypoints require a terminal; use task work:status for scripts')
        if args.armed:
            return workspace.remote(project, state, armed=lambda primary: _armed(primary, state, args),
                                    rebind=args.rebind)
        if args.codespace_stop or args.codespace_restart:
            return _owner_power(project, state, restart=args.codespace_restart)
        if args.codespace:
            from . import arming
            if args.rebind is not None and arming.read(state).get('armed'):
                # Moving only the sync would leave automatic restart on the old Codespace.
                raise ValueError('This project is armed; move it with task work:cs:armed -- --rebind '
                                 + args.rebind + ' so restart moves too')
            return workspace.remote(project, state, rebind=args.rebind)
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
        commands = parser()
        args = commands.parse_args(argv)
        if getattr(args, 'rebind', None) is not None and not (args.codespace or args.armed):
            commands.error('--rebind works only with task work:cs or task work:cs:armed')
        return run(args)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
