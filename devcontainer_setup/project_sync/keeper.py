"""The per-project keeper: one detached process that watches an armed Codespace.

It reads the Codespace once a minute over direct HTTPS and classifies each stop.
In this version a 12-hour stop only notifies "would restart"; nothing is posted.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from . import arming, lifecycle, notify
from .scratch import CONFIG
from .state import (atomic_write, launch_detached, locked, log_formatter, owned_process, process_identity,
                    read_json, runtime_id, wait_gone)
from .transport import _terminate

RUNTIME = 'keeper-runtime.json'
LOG = 'keeper.log'
LEDGER = 'keeper-ledger.jsonl'
POLL = 60
HEARTBEAT_STALE = 300
ENV = ('HOME', 'PATH', 'LANG', 'TMPDIR', 'USER', 'LOGNAME')
DOWN = ('ShuttingDown', 'Shutdown')
TITLE = 'Codespace recovery'
NOTICE_DAYS = 7
_LOG = logging.getLogger('project-sync-keeper')


def _owned(runtime, state):
    return owned_process(runtime, state, flag='--keep', module='.keeper')


def status(state: Path) -> dict:
    try:
        runtime = read_json(Path(state) / RUNTIME)
    except FileNotFoundError:
        return {'running': False, 'phase': 'stopped', 'heartbeat_age': None}
    beat = runtime.get('heartbeat_at')
    age = time.time() - beat if isinstance(beat, (int, float)) else None
    return {**runtime, 'running': _owned(runtime, state), 'heartbeat_age': age}


def _env(value):
    # Never the pane's environment: no tokens, Herdr pane ids or Python paths.
    env = {key: os.environ[key] for key in ENV if key in os.environ}
    env['DEVCONTAINER_GH_USER'] = value['gh_user']
    return env


def start(root: Path, state: Path) -> dict:
    """Adopt the keeper running this code for this arming; replace any other; else launch one."""
    state, root = Path(state).absolute(), Path(root).resolve()
    value = arming.read(state)
    if not (value.get('armed') and value.get('gh_user') and value.get('codespace')):
        raise ValueError('This project is not armed; run task work:cs:armed')
    wanted = {'runtime_id': runtime_id(), 'codespace': value['codespace'], 'gh_user': value['gh_user']}
    with locked(state / 'keeper-control.lock', blocking=True):
        current = status(state)
        if current['running']:
            age = current.get('heartbeat_age')
            # A hung keeper (no heartbeat for 5 min) is replaced, as status advises.
            if (all(current.get(key) == item for key, item in wanted.items())
                    and isinstance(age, (int, float)) and age <= HEARTBEAT_STALE):
                return current
            _stop(state, current)
        return launch_detached('keeper', '--keep', root, state, env=_env(value), status=status,
                               exited='The keeper exited during startup; inspect task work:logs',
                               timed_out='The keeper did not start within 8 seconds; inspect task work:logs')


def _stop(state, current):
    if not current['running']:
        return current
    try:
        os.kill(current['pid'], signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Done when that exact process (start time and argv) is gone, so its locks are free.
    if wait_gone(current, 15):
        return status(state)
    raise ValueError('The keeper is still shutting down; inspect task work:status before retrying')


def stop(state: Path) -> dict:
    """Stop by fact: only the process the runtime record proves is ours; safe when none runs."""
    state = Path(state).absolute()
    with locked(state / 'keeper-control.lock', blocking=True):
        return _stop(state, status(state))


def wait_ready(state: Path, nonce: str, timeout: float = 60) -> dict:
    """Wait for the keeper's first full pass, its first heartbeat after startup."""
    deadline = time.monotonic() + timeout
    while True:
        current = status(state)
        if (current.get('nonce') == nonce and current.get('phase') != 'starting') or not current['running']:
            return current
        if time.monotonic() >= deadline:
            return current
        time.sleep(.25)


def foreign_processes(state: Path, codespace: str, *, run=subprocess.run) -> list[int]:
    """Pids of gh codespace processes naming this Codespace that this package did not start.

    Ours are the worker's forward, a Herdr proxy's gh --stdio and the keeper's
    keep-alive child. Anything else, such as a project's cs:ports, can start
    the Codespace through gh and holds the ports the keep-alive child needs.
    """
    try:
        listing = run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        listing = None
    if listing is None or listing.returncode:
        raise ValueError('Cannot list processes to check for other gh connections; retry task work:cs:armed')
    try:
        keepalive = (read_json(Path(state) / RUNTIME).get('keepalive') or {}).get('pid')
    except FileNotFoundError:
        keepalive = None
    found = []
    for line in listing.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        pid, args = int(fields[0]), fields[1:]
        if os.path.basename(args[0]) != 'gh' or args[1] not in ('codespace', 'cs'):
            continue
        if codespace not in args and not any(arg.endswith('=' + codespace) for arg in args):
            continue
        forward = args[2:4] == ['ports', 'forward'] and any(arg.startswith('2222:') for arg in args)
        proxy = args[2:3] == ['ssh'] and '--stdio' in args
        if not (forward or proxy or pid == keepalive):
            found.append(pid)
    return found


def _utc(when):
    return time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(when))


class Keeper:
    """One pass reads the Codespace once; every pass writes the heartbeat and never raises."""

    def __init__(self, state, *, api, notifier, keepalive=None, herdr=None, now=time.time, sleep=time.sleep):
        self.state, self.api, self.notifier = Path(state), api, notifier
        self.keepalive, self.herdr, self.now, self.sleep = keepalive, herdr, now, sleep
        self.ledger = lifecycle.Ledger(self.state / LEDGER, now=now)
        self.project = api.name
        self.previous = None  # state of the last good reading this process saw; None before its first
        self.delay, self._backoff, self.done = 0, 2, False
        self.runtime = self._load()

    def _load(self):
        try:
            old = read_json(self.state / RUNTIME)
        except (FileNotFoundError, ValueError):
            old = {}
        runtime = {'phase': 'starting', 'codespace': self.api.name, 'down': {'count': 0, 'first_at': None},
                   'notified': {}}
        if old.get('codespace') != self.api.name:
            return runtime  # a new pairing starts a clean watch
        notified = old.get('notified')
        if isinstance(notified, dict):
            runtime['notified'] = notified
        if isinstance(old.get('last_restart'), dict):
            runtime['last_restart'] = old['last_restart']
        beat = old.get('heartbeat_at')
        if not (isinstance(beat, (int, float)) and self.now() - beat <= HEARTBEAT_STALE):
            # After a longer gap the old anchor and readings say nothing about this run.
            return runtime
        # A replacement within minutes (an upgrade) continues the same watch.
        anchor, watch = old.get('anchor'), old.get('down')
        if isinstance(anchor, dict) and isinstance(anchor.get('at'), (int, float)):
            runtime['anchor'] = anchor
        if (isinstance(watch, dict) and isinstance(watch.get('count'), int) and 'first_at' in watch
                and isinstance(watch['first_at'], (int, float, type(None)))):
            runtime['down'] = {'count': watch['count'], 'first_at': watch['first_at']}
            if watch.get('kind') in ('OWNER', 'LIFETIME', 'OTHER'):
                runtime['down']['kind'] = watch['kind']
            if isinstance(old.get('last_up_at'), (int, float)):
                runtime['last_up_at'] = old['last_up_at']
        return runtime

    def step(self) -> None:
        try:
            self._pass()
            self.delay, self._backoff = POLL, 2
        except Exception as error:
            # Only fixed ValueError texts are kept; anything else is named by its type.
            text = str(error) if isinstance(error, ValueError) else 'unexpected error'
            _LOG.info('Keeper pass failed: %s: %s', type(error).__name__, text)
            self.runtime['down']['count'] = 0
            self.runtime.update(phase='waiting', error=type(error).__name__ + ': ' + text)
            self.delay, self._backoff = self._backoff, min(self._backoff * 2, POLL)
        try:
            self.beat()
        except Exception as error:
            _LOG.info('Keeper heartbeat not written: %s', type(error).__name__)

    def beat(self, phase=None):
        if phase:
            self.runtime['phase'] = phase
        self.runtime['heartbeat_at'] = self.now()
        if self.state.is_dir():  # never recreate a removed state directory
            atomic_write(self.state / RUNTIME, (json.dumps(self.runtime, sort_keys=True) + '\n').encode())

    def _pass(self):
        value = arming.read(self.state)
        if not value.get('armed'):
            _LOG.info('Project disarmed; the keeper stops')
            self.done = True
            self.runtime.update(phase='disarmed', error=None)
            return
        refusal = self._refusal(value)
        if refusal:
            if self.runtime.get('phase') != 'refused':
                _LOG.info('Keeper refused: %s', refusal)
            self.runtime.update(phase='refused', error=refusal)
            self._notify('refused-' + repr(value.get('consented_at')),
                         f'Recovery for {self.project} is refused: {refusal}.')
            return
        reading = self.api.read()
        self.runtime['reading'] = {'at': reading.at, 'ok': reading.ok, 'state': reading.state,
                                   'pending': reading.pending}
        watch = self.runtime['down']
        if not reading.ok:
            watch['count'] = 0
            self.runtime.update(phase='waiting', error='Codespace state unreadable; check gh auth status and the network')
            return
        if reading.state != self.previous:
            _LOG.info('Codespace %s at %s', reading.state, _utc(reading.at))
        self.runtime.update(phase='watching', error=None)
        if reading.state == 'Available':
            self._up(reading)
        else:
            if watch['first_at'] is None:
                watch['first_at'] = reading.at  # the stop's first non-Available reading
            if reading.state in DOWN:
                watch['count'] += 1
                if watch['count'] >= lifecycle.DOWN_READINGS and not watch.get('kind'):
                    self._classify(value, watch)
            else:
                watch['count'] = 0
        self.previous = reading.state

    def _refusal(self, value):
        try:
            binding = read_json(self.state / CONFIG)
        except FileNotFoundError:
            return 'this project is not paired with a Codespace; run task work:cs'
        self.project = Path(binding.get('root') or self.api.name).name
        # Consent names one Codespace; never watch or start another one.
        if value.get('codespace') != self.api.name or binding.get('codespace') != self.api.name:
            return 'the paired Codespace is not the armed one; run task work:cs:armed'
        return None

    def _up(self, reading):
        anchor = self.runtime.get('anchor')
        if self.previous is not None and self.previous != 'Available':
            anchor = {'at': reading.at, 'source': 'observed'}
        elif reading.last_used_at is not None and (
                anchor is None or reading.at > anchor['at'] + lifecycle.LIFETIME + lifecycle.WINDOW
                or (self.previous is None and reading.last_used_at > anchor['at'])):
            # No anchor yet; one from a run GitHub has certainly stopped since; or, on this
            # process's first reading, a later start request that it could not have seen.
            anchor = {'at': reading.last_used_at, 'source': 'api'}
        if anchor != self.runtime.get('anchor'):
            _LOG.info('Anchor %s from %s', _utc(anchor['at']), anchor['source'])
        self.runtime.update(anchor=anchor, last_up_at=reading.at, down={'count': 0, 'first_at': None})

    def _classify(self, value, watch):
        anchor = self.runtime.get('anchor') or {}
        kind = lifecycle.classify(last_up_at=self.runtime.get('last_up_at'), first_down_at=watch['first_at'],
                                  anchor_at=anchor.get('at'), owner_stop_at=value.get('owner_stop_at'))
        _LOG.info('Stop first seen %s classified %s (anchor %s)', _utc(watch['first_at']), kind,
                  anchor.get('source', 'none'))
        key = 'stop-' + repr(watch['first_at'])
        if kind == 'OTHER':
            self._notify(key, f'{self.project} stopped at {arming.format_time(watch["first_at"])} for a reason '
                              'other than the 12-hour limit; run task work:cs to start it.')
        elif kind == 'LIFETIME':
            # Notify mode: name what a restart would do; the start routine is not called.
            try:
                reason = lifecycle.gate(arming=value, rows=self.ledger.rows(), now=self.now())
            except OSError:
                reason = 'the restart ledger is unreadable'
            if reason is None:
                self._notify(key, f'Would restart {self.project} ({lifecycle.new_rid(self.now())}) after '
                                  "GitHub's 12-hour stop; automatic restart is not switched on yet. "
                                  'Run task work:cs to start it.')
            else:
                self._notify(key, f'Would not restart {self.project}: {reason}. Run task work:cs to start it.')
        # Only now: a raise above leaves the stop unclassified, so the next readings retry it.
        watch['kind'] = kind

    def _notify(self, key, text):
        notified, now = self.runtime['notified'], self.now()
        if key in notified:
            return
        for old in [k for k, at in notified.items() if not isinstance(at, (int, float)) or now - at > NOTICE_DAYS * 86400]:
            del notified[old]
        notified[key] = now
        _LOG.info('Notify: %s', text)
        if not self.notifier(TITLE, text):
            _LOG.info('The notification could not be shown')


class _Shutdown(BaseException):
    pass


def _log_to(state):
    path = state / LOG
    if path.is_symlink():
        raise ValueError('Keeper log must not be a symlink.')
    handler = RotatingFileHandler(path, maxBytes=256 * 1024, backupCount=2)
    handler.setFormatter(log_formatter())
    os.chmod(path, 0o600)
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False
    _LOG.addHandler(handler)
    return handler


def _caffeinate():
    # Hold idle sleep off while the keeper runs; -w ends it with the keeper.
    try:
        return subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        _LOG.info('caffeinate unavailable; the Mac may sleep while armed')
        return None


def supervise(root: Path, state: Path, nonce: str) -> None:
    """Entry for `--keep ROOT STATE NONCE`, launched only by start()."""
    root, state = Path(root).resolve(), Path(state).absolute()

    def shutdown(*_):
        raise _Shutdown()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)
    with ExitStack() as stack:
        try:
            stack.enter_context(locked(state / 'keeper.lock'))
        except BlockingIOError:
            handler = _log_to(state)
            _LOG.info('Another keeper holds keeper.lock; this one exits')
            handler.close()
            _LOG.removeHandler(handler)
            return
        handler = _log_to(state)
        caffeinate = current = None
        try:
            value = arming.read(state)
            api = lifecycle.LifecycleAPI(value.get('codespace'), value.get('gh_user'))
            current = Keeper(state, api=api, notifier=notify.notify)
            caffeinate = _caffeinate()
            current.runtime.update(pid=os.getpid(), nonce=nonce, process_identity=process_identity(os.getpid()),
                                   runtime_id=runtime_id(), gh_user=value.get('gh_user'), error=None,
                                   caffeinate=caffeinate.pid if caffeinate else None)
            current.beat('starting')
            _LOG.info('Keeper started for %s as %s', api.name, value.get('gh_user'))
            while not current.done:
                current.step()
                if not current.done:
                    time.sleep(current.delay)
        except _Shutdown:
            pass
        except Exception as error:
            text = str(error) if isinstance(error, ValueError) else 'unexpected error'
            _LOG.info('Keeper cannot run: %s: %s', type(error).__name__, text)
        finally:
            # A second stop request must not interrupt cleanup.
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: None)
            _terminate(caffeinate)
            if current is not None:
                current.runtime.update(process_identity=None, caffeinate=None)
                try:
                    current.beat('disarmed' if current.done else 'stopped')
                except Exception:
                    pass
            _LOG.info('Keeper stopped')
            handler.close()
            _LOG.removeHandler(handler)


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--keep':
        supervise(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    else:
        raise SystemExit('Expected --keep ROOT STATE NONCE')
