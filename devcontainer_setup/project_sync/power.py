"""Owner stops shared by the headless CLI and the Mac worker."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import stat
import time

from . import arming, lifecycle, scratch
from .state import atomic_write, locked, read_json

LINKS = '.local/state/devcontainer-links'
WINDOW = 60
LISTENING_AGE = 15
CAPABILITY = 'stop-listening-v1'


def request_from_codespace(*, home: Path, env: Mapping, now=time.time, sleep=time.sleep,
                           wait: float = WINDOW) -> int:
    """Queue a stop only for a capable worker; unlink arbitrates cancellation with its claim."""
    base = home / LINKS
    try:
        heartbeat = (base / CAPABILITY).lstat()
        listening = stat.S_ISREG(heartbeat.st_mode) and 0 <= now() - heartbeat.st_mtime <= LISTENING_AGE
    except FileNotFoundError:
        listening = False
    if not listening:
        print('No Mac worker is connected to this Codespace, so nothing stops. Stop it from your Mac with '
              'task work:cs:stop; otherwise it stops after its idle timeout.')
        return 1
    from .transport import _name
    codespace = _name(env.get('CODESPACE_NAME'))
    identity = f'{time.time_ns()}-{os.getpid()}-{secrets.token_hex(4)}'
    request = base / 'stops' / (identity + '.json')
    answer = request.with_suffix('.result')
    created = now()

    def remove_request():
        try:
            request.unlink()
            return True
        except OSError:
            # Missing means claimed; every other failure is equally uncertain.
            return False

    try:
        atomic_write(request, json.dumps(dict(schema=1, kind='stop', id=identity,
                                              created_at=created, codespace=codespace)).encode())
        until = created + wait
        while now() < until:
            try:
                value = read_json(answer)
            except FileNotFoundError:
                sleep(0.5)
                continue
            if (set(value) != {'ok', 'message'} or type(value['ok']) is not bool
                    or not isinstance(value['message'], str)):
                raise ValueError('Invalid Mac stop answer; check task work:status on your Mac.')
            answer.unlink()
            print(('Your Mac recorded this as your stop. ' if value['ok'] else '') + value['message'])
            return 0 if value['ok'] else 1
        if remove_request():
            print('Your Mac did not take the stop request within 60 seconds, so nothing stops. '
                  'Check task work:status on your Mac.')
        else:
            print('Your Mac took the stop request but has not answered; check task work:status on your Mac.')
        return 1
    except KeyboardInterrupt:
        print('Cancelled; nothing stops.' if remove_request()
              else 'Cancelled, but your Mac already took the stop request.')
        return 130


@dataclass(frozen=True)
class Stopped:
    codespace: str
    armed: bool
    api: object
    stopped_at: float
    message: str


def stop(state: Path, *, expected: str | None = None, until: float | None = None,
         halted=lambda: False, after_record: Callable[[Stopped], None] | None = None,
         api=None, now=time.time, sleep=time.sleep) -> Stopped:
    """Record the owner's stop, notify the requester, then POST outside the lock."""
    if os.environ.get('CODESPACES') == 'true':
        raise ValueError('Run task work:cs:stop on your Mac to stop the Codespace')
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if os.environ.get(name):
            raise ValueError(f'Unset {name} before stopping or restarting the Codespace; it must use a real gh account.')

    def check_request():
        at = now()
        if until is not None and at >= until:
            raise ValueError('The stop request expired before it could run; nothing stopped.')
        if halted():
            raise ValueError('The Mac worker is shutting down; nothing stopped.')
        return at

    with ExitStack() as stack:
        if until is None:
            stack.enter_context(locked(state / 'workspace-control.lock', blocking=True))
        else:
            while True:
                check_request()
                try:
                    stack.enter_context(locked(state / 'workspace-control.lock'))
                    break
                except BlockingIOError:
                    sleep(0.2)
        try:
            codespace = read_json(state / scratch.CONFIG)['codespace']
        except FileNotFoundError:
            raise ValueError('Pair a Codespace first: run task work:cs') from None
        if expected is not None and expected != codespace:
            raise ValueError(f'The stop request names {expected}, but this project is paired with {codespace}; '
                             'nothing stopped.')
        value = arming.read(state)
        armed = bool(value.get('armed'))
        if armed and value.get('codespace') != codespace:
            raise ValueError('The paired Codespace is not the armed one; run task work:cs:armed')
        stopped_at = check_request()
        arming.record_owner_stop(state, stopped_at, codespace)

    # Use the account and target captured in the same transaction as the owner stop.
    api = api or lifecycle.LifecycleAPI(codespace, value.get('gh_user') if armed else None)
    message = f'Stopping {codespace}; it stays stopped until task work:cs starts it again.'
    result = Stopped(codespace, armed, api, stopped_at, message)
    if after_record is not None:
        after_record(result)
    status = api.post_stop()
    # R33: keep the owner stop on every failure, including an ambiguous 404.
    if status == 404 and api.confirm_deleted() is True:
        raise ValueError(arming.deleted_message(codespace, armed=armed))
    if 400 <= status < 500:
        raise ValueError(f'GitHub refused the stop request (HTTP {status}); it is recorded as your stop, so the '
                         'keeper will not restart it. Check task work:status.')
    if not 200 <= status < 300:
        raise ValueError(f'The stop request did not complete (HTTP {status}); it may still take effect, and the '
                         'keeper treats it as your stop. Inspect task work:status, then retry.')
    return result
