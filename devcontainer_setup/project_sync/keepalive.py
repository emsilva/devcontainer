"""The K4 keep-alive child: one read-only `gh codespace ssh` that holds an armed Codespace up.

It carries the project's port forwards (local N+10000 to remote N). Its gh talks
only to a ReadOnlyAPI capability, so it can never start a stopped Codespace.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time

from .state import process_identity
from .transport import ReadOnlyAPI, TokenTimeout, _name, clean_env, github_token

OFFSET = 10000
BACKOFF = 2
BACKOFF_MAX = 60
TERMINATE_WAIT = 5
KEY = '.ssh/codespaces.auto'
COMMAND = 'exec sleep infinity'
_LOG = logging.getLogger('project-sync-keeper')


def argv(name: str, key: Path, ports: list[tuple[int, int]]) -> list[str]:
    # The positional command sets gh's keep-alive override (spec 6.3).
    forwards = [arg for local, remote in ports for arg in ('-L', f'127.0.0.1:{local}:localhost:{remote}')]
    return ['gh', 'codespace', 'ssh', '-c', _name(name), '--', '-i', str(key), '-o', 'ServerAliveInterval=30',
            '-o', 'ExitOnForwardFailure=yes', *forwards, COMMAND]


def _holder(port):
    try:
        result = subprocess.run(['lsof', '-nP', '-t', f'-iTCP:{port}', '-sTCP:LISTEN'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    pids = result.stdout.split()
    return int(pids[0]) if result.returncode == 0 and pids and pids[0].isdigit() else None


def free_ports(ports: list[int]) -> tuple[list[tuple[int, int]], list[tuple[int, int | None]]]:
    """([(local, remote) forwarded], [(local, holder pid or None) in use]); local = remote + 10000.

    A port any TCP listener holds, on any address, is in use (R19): beside a 0.0.0.0 or ::
    listener ssh's own 127.0.0.1 bind succeeds and shadows that service. lsof sees only this
    user's listeners, so a 127.0.0.1 test bind must pass too.
    """
    up, skipped = [], []
    for remote in ports:
        local = remote + OFFSET
        holder = _holder(local)
        if holder is not None:
            skipped.append((local, holder))
            continue
        try:
            with socket.socket() as sock:
                # As ssh binds its forwards, so a TIME_WAIT leftover is not "in use".
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', local))
        except OSError:
            skipped.append((local, None))
        else:
            up.append((local, remote))
    return up, skipped


def _marked(identity, name):
    return isinstance(identity, str) and (' codespace ssh -c ' + name + ' -- ') in identity \
        and identity.endswith(' ' + COMMAND)


def _ours(pid, identity, name):
    # By fact: the same start time (ps lstart, the first five fields) and an argv naming this Codespace.
    current = process_identity(pid)
    return bool(isinstance(name, str) and _marked(identity, name) and _marked(current, name)
                and current.split()[:5] == identity.split()[:5])


def _signal(pid, number):
    # The child leads its own session (start_new_session), so its ssh shares its group.
    try:
        os.killpg(pid, number)
    except (ProcessLookupError, PermissionError):
        pass


def _recorded(record):
    if not isinstance(record, dict):
        return None, None
    pid = record.get('pid')
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 1):
        return None, None
    return pid, record.get('process_identity')


def recorded_child(record, name) -> int | None:
    """The pid of the live keep-alive child a runtime record names, proven by fact; else None."""
    pid, identity = _recorded(record)
    try:
        return pid if pid is not None and _ours(pid, identity, name) else None
    except subprocess.TimeoutExpired:
        return None


def end_left_behind(record, name) -> str | None:
    """End a keep-alive child that a killed keeper left running; never by pid alone.

    'ended' once it is gone, 'survived' when it outlives SIGKILL, None when the record
    names no such process.
    """
    pid, identity = _recorded(record)
    if pid is None:
        return None
    try:
        if not _ours(pid, identity, name):
            return None
        for number in (signal.SIGTERM, signal.SIGKILL):
            _signal(pid, number)
            deadline = time.monotonic() + TERMINATE_WAIT
            while time.monotonic() < deadline:
                if not _ours(pid, identity, name):
                    return 'ended'
                time.sleep(.1)
            if not _ours(pid, identity, name):
                return 'ended'
    except subprocess.TimeoutExpired:
        return None
    return 'survived'


@contextmanager
def _signals_held():
    """Hold SIGTERM and SIGINT through the block, then deliver the first that came (R14).

    Held by handler, not by signal mask: a launched child inherits a blocked mask, and
    an ssh started with SIGTERM blocked ignores it (measured).
    """
    if threading.current_thread() is not threading.main_thread():
        yield  # Python runs signal handlers on the main thread only
        return
    caught = []
    held = {number: signal.signal(number, lambda number, _: caught.append(number))
            for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for number, handler in held.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)
        if caught:
            signal.raise_signal(caught[0])


class KeepAlive:
    """Holds at most one child; ensure() follows each reading and reports a failure in its record.

    ensure(True) starts, keeps or restarts it; ensure(False) stops it with no restart;
    ensure(None), a reading that failed, leaves a running child and starts nothing.
    """

    def __init__(self, name: str, state: Path, gh_user: str | None, ports: list[int], *,
                 popen=subprocess.Popen, now=time.monotonic):
        self.name, self.state, self.gh_user = _name(name), Path(state), gh_user
        self.ports = sorted({p for p in ports if isinstance(p, int) and not isinstance(p, bool)
                             and 1 <= p <= 65535 - OFFSET})
        self.popen, self.now = popen, now
        self.child = self.api = self.identity = self.error = self.due = None
        self.ports_up, self.ports_skipped = [], []
        self.started_at, self.restarts, self.backoff = None, 0, BACKOFF
        self.wanted = self.exited = False
        self._token = None

    def record(self) -> dict:
        return {'pid': self.child.pid if self.child is not None else None, 'process_identity': self.identity,
                'ports_up': [list(pair) for pair in self.ports_up],
                'ports_skipped': [list(pair) for pair in self.ports_skipped],
                'restarts': self.restarts, 'error': self.error}

    def ensure(self, available: bool | None) -> dict:
        if available is True:
            self.wanted = True
            self._hold()
        elif available is False:
            self.wanted = self.exited = False
            self.stop()
            self.backoff, self.due, self.error = BACKOFF, None, None
        else:
            self.wanted = False
            self._reap()
        return self.record()

    def tend(self) -> dict | None:
        """Between readings: restart a child that exited, once its backoff allows; the record if it changed."""
        if not self.wanted:
            return None
        before = self.record()
        self._hold()
        after = self.record()
        return after if after != before else None

    def stop(self) -> None:
        child = self.child
        # Unreaped, so its pid (and group) cannot have been reused.
        if child is not None and child.poll() is None:
            _signal(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=TERMINATE_WAIT)
            except subprocess.TimeoutExpired:
                _signal(child.pid, signal.SIGKILL)
                child.wait(timeout=TERMINATE_WAIT)
            _LOG.info('Keep-alive stopped (pid %s)', child.pid)
        # Only once it is reaped: a stop that a signal cuts short still names it for the next (R14).
        self.child = None
        self._close()

    def _close(self):
        api, self.api = self.api, None
        if api is not None:
            api.__exit__(None, None, None)
        self.identity, self.ports_up, self.ports_skipped = None, [], []

    def _hold(self):
        self._reap()
        if self.child is None and (self.due is None or self.now() >= self.due):
            self._start()

    def _reap(self):
        if self.child is None or self.child.poll() is None:
            return
        code, now = self.child.returncode, self.now()
        self.child = None
        self._close()
        self.error = f'the connection exited with status {code}'
        # 2 s doubling to 60 s while children die young; one that held a minute starts afresh.
        # Whatever the reading was, so the next start after any exit waits and counts.
        if now - self.started_at >= BACKOFF_MAX:
            self.backoff = BACKOFF
        self.exited = True
        _LOG.info('Keep-alive exited with status %s; a restart waits %s s', code, self._defer(now))

    def _defer(self, now):
        wait, self.backoff = self.backoff, min(self.backoff * 2, BACKOFF_MAX)
        self.due = now + wait
        return wait

    def _read_token(self):
        # Read per start; the last good token covers a read that fails later (spec D10), memory only.
        try:
            self._token = github_token(dict(clean_env(), DEVCONTAINER_GH_USER=self.gh_user)
                                       if self.gh_user else clean_env()) or self._token
        except TokenTimeout:
            if not self._token:
                raise ValueError(f'reading the GitHub token for {self.gh_user} timed out') from None
        except (OSError, ValueError):
            if not self._token:
                raise ValueError(f'the GitHub token for {self.gh_user} could not be read') from None
        return self._token

    def _start(self):
        key = Path(os.environ.get('HOME') or Path.home()) / KEY
        try:
            if not key.is_file():
                raise ValueError('the paired SSH key ~/' + KEY + ' is missing')
            token = self._read_token()
            up, skipped = free_ports(self.ports)
            # A stop signal waits until the child is recorded, so the keeper's finally finds it (R14).
            with _signals_held():
                try:
                    api = ReadOnlyAPI(self.name, token).__enter__()
                except OSError:
                    raise ValueError('the read-only GitHub adapter could not be opened') from None
                try:
                    child = self.popen(argv(self.name, key, up), env=api.child_env(), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                except OSError:
                    api.__exit__(None, None, None)
                    raise ValueError('gh could not be started') from None
                self.child, self.api, self.ports_up, self.ports_skipped = child, api, up, skipped
        except ValueError as error:
            self.error = str(error)
            _LOG.info('Keep-alive not started: %s; retrying in %s s', self.error, self._defer(self.now()))
            return
        self.started_at, self.due, self.error = self.now(), None, None
        if self.exited:
            self.restarts += 1
        self.exited = False
        try:
            self.identity = process_identity(child.pid)
        except (OSError, subprocess.TimeoutExpired):
            self.identity = None  # then a killed keeper's child cannot be proven ours, and is left alone
        _LOG.info('Keep-alive started (pid %s); ports %s%s', child.pid,
                  ' '.join(str(local) for local, _ in up) or 'none',
                  ''.join(f'; {local} in use' + (f' by pid {pid}' if pid else '') for local, pid in skipped))
