"""Owner stops shared by the headless CLI and the Mac worker."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import time

from . import arming, lifecycle, scratch
from .state import locked, read_json


@dataclass(frozen=True)
class Stopped:
    codespace: str
    armed: bool
    api: object
    stopped_at: float
    message: str


def stop(state: Path, *, expected: str | None = None, until: float | None = None,
         halted=lambda: False, api=None, now=time.time, sleep=time.sleep) -> Stopped:
    """Record the owner's stop against the locked pairing, then POST outside the lock."""
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
        arming.record_owner_stop(state, stopped_at)

    # Use the account and target captured in the same transaction as the owner stop.
    api = api or lifecycle.LifecycleAPI(codespace, value.get('gh_user') if armed else None)
    status = api.post_stop()
    # R33: keep the owner stop on every failure, including an ambiguous 404.
    if status == 404 and api.confirm_deleted() is True:
        raise ValueError(f'{codespace} no longer exists on GitHub; there is nothing to stop.')
    if 400 <= status < 500:
        raise ValueError(f'GitHub refused the stop request (HTTP {status}); it is recorded as your stop, so the '
                         'keeper will not restart it. Check task work:status.')
    if not 200 <= status < 300:
        raise ValueError(f'The stop request did not complete (HTTP {status}); it may still take effect, and the '
                         'keeper treats it as your stop. Inspect task work:status, then retry.')
    message = (f'Stopping {codespace}; it stays stopped until task work:cs' + (':armed' if armed else '')
               + ' starts it again.')
    return Stopped(codespace, armed, api, stopped_at, message)
