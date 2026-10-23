"""The per-project keeper: one detached process that watches an armed Codespace.

It reads the Codespace once a minute over direct HTTPS and classifies each stop,
and holds the read-only keep-alive child while the Codespace is Available. After
GitHub's 12-hour stop, within the budget, it is the one background process that
starts the Codespace again; then it restores Herdr and tells the project's lead.
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

from . import arming, lifecycle, notify, restore
from .keepalive import KeepAlive, end_left_behind, recorded_child
from .scratch import CONFIG
from .state import (atomic_write, launch_detached, locked, log_formatter, owned_process, process_identity,
                    read_json, runtime_id, wait_gone)
from .transport import _terminate

RUNTIME = 'keeper-runtime.json'
LOG = 'keeper.log'
LEDGER = 'keeper-ledger.jsonl'
POLL = 60
TEND = 1
HEARTBEAT_STALE = 300
ENV = ('HOME', 'PATH', 'LANG', 'TMPDIR', 'USER', 'LOGNAME')
DOWN = ('ShuttingDown', 'Shutdown')
TITLE = 'Codespace recovery'
NOTICE_DAYS = 7
PENDING_FRESH = 30 * 60  # a planned restart is restored only this soon after it came back
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


def _end_left_behind(state):
    # A keeper killed outright (SIGKILL, a crash) skips its finally, so its keep-alive child
    # would hold the Codespace up and billed; end it by fact (R14).
    try:
        runtime = read_json(Path(state) / RUNTIME)
    except (OSError, ValueError):
        return None, None
    record = runtime.get('keepalive')
    outcome = end_left_behind(record, runtime.get('codespace'))
    pid = record.get('pid') if isinstance(record, dict) else None
    if outcome == 'ended':
        _LOG.info('Ended the keep-alive child %s a previous keeper left', pid)
    elif outcome == 'survived':
        _LOG.info('The keep-alive child %s a previous keeper left survived SIGKILL; it may still hold the '
                  'Codespace', pid)
    return outcome, pid


def stop(state: Path) -> dict:
    """Stop by fact: only the process the runtime record proves is ours; safe when none runs."""
    state = Path(state).absolute()
    with locked(state / 'keeper-control.lock', blocking=True):
        result = _stop(state, status(state))
        outcome, pid = _end_left_behind(state)
        if outcome == 'survived':
            raise ValueError(f'The keep-alive child {pid} a previous keeper left survived SIGKILL; '
                             f'inspect it with ps -p {pid}')
        return result


def _child_pending(runtime):
    # Available, and the keep-alive child has neither started nor said why not.
    reading, held = runtime.get('reading') or {}, runtime.get('keepalive') or {}
    return bool(reading.get('ok') and reading.get('state') == 'Available'
                and not held.get('pid') and not held.get('error'))


def wait_ready(state: Path, nonce: str, timeout: float = 60) -> dict:
    """Wait for the keeper's first full pass, its first heartbeat after startup.

    While the Codespace reads Available, also wait for the keep-alive child (R4).
    """
    deadline = time.monotonic() + timeout
    while True:
        current = status(state)
        if not current['running'] or (current.get('nonce') == nonce and current.get('phase') != 'starting'
                                       and not _child_pending(current)):
            return current
        if time.monotonic() >= deadline:
            return current
        time.sleep(.25)


def foreign_processes(state: Path, codespace: str, *, run=subprocess.run) -> list[int]:
    """Pids of gh codespace processes naming this Codespace that this package did not start.

    Ours are the worker's forward, the gh --stdio this project's Herdr proxy started
    and the keeper's keep-alive child. Anything else, such as a project's cs:ports or
    another gh --stdio, can start the Codespace through gh and holds the ports the
    keep-alive child needs.
    """
    try:
        listing = run(['ps', '-axo', 'pid=,ppid=,command='], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        listing = None
    if listing is None or listing.returncode:
        raise ValueError('Cannot list processes to check for other gh connections; retry task work:cs:armed')
    try:
        # Exempt by fact (pid, start time, argv), never a recorded pid alone (R14).
        keepalive = recorded_child(read_json(Path(state) / RUNTIME).get('keepalive'), codespace)
    except FileNotFoundError:
        keepalive = None
    processes = {}
    for line in listing.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            processes[int(fields[0])] = int(fields[1]), fields[2]
    found = []
    for pid, (parent, command) in processes.items():
        args = command.split()
        if len(args) < 2 or os.path.basename(args[0]) != 'gh' or args[1] not in ('codespace', 'cs'):
            continue
        if codespace not in args and not any(arg.endswith('=' + codespace) for arg in args):
            continue
        forward = args[2:4] == ['ports', 'forward'] and any(arg.startswith('2222:') for arg in args)
        # By its parent: the same proxy signature the keeper's restore matches (#46).
        proxy = (args[2:3] == ['ssh'] and '--stdio' in args
                 and restore.is_proxy(processes.get(parent, (0, ''))[1], codespace, Path(state)))
        if not (forward or proxy or pid == keepalive):
            found.append(pid)
    return found


def _utc(when):
    return time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(when))


def _start_detail(result):
    # What a start routine that did not end Available reports, as fixed text.
    if result['outcome'] == 'failed' and result['last_state'] == 'Shutdown':
        if result['status'] is None:
            return f'no start request left; {lifecycle.MAX_POSTS} were made'
        return f'HTTP {result["status"]}'
    return f'{result["outcome"]} in {result["last_state"] or "an unknown state"}'


class Keeper:
    """One pass reads the Codespace once; every pass writes the heartbeat and never raises.

    Each pass hands its reading to the keep-alive child: Available holds it, another
    state stops it, and no good reading leaves it as it is (R13).
    """

    def __init__(self, state, *, api, notifier, keepalive=None, herdr=None, now=time.time, sleep=time.sleep,
                 clock=time.monotonic, end_proxies=restore.end_stale_proxies):
        self.state, self.api, self.notifier = Path(state), api, notifier
        self.keepalive, self.herdr, self.now, self.sleep, self.clock = keepalive, herdr, now, sleep, clock
        self.end_proxies = end_proxies
        self.ledger = lifecycle.Ledger(self.state / LEDGER, now=now)
        self.project = api.name
        self.previous = None, None  # (state, time) of the last successful reading
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
            runtime['last_restart'] = dict(old['last_restart'])
            if runtime['last_restart'].get('outcome') in ('restarting', 'restoring'):
                runtime['last_restart']['outcome'] = 'interrupted'  # its keeper died mid-flow
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
            if isinstance(watch.get('hold_at_stop'), (int, float)):
                runtime['down']['hold_at_stop'] = watch['hold_at_stop']
            if isinstance(old.get('last_up_at'), (int, float)):
                runtime['last_up_at'] = old['last_up_at']
        last = old.get('last_ok')
        if isinstance(last, dict) and isinstance(last.get('state'), str) and isinstance(last.get('at'), (int, float)):
            # So an upgrade keeps observing transitions and measuring reading gaps.
            runtime['last_ok'] = {'at': last['at'], 'state': last['state']}
            self.previous = last['state'], last['at']
        return runtime

    def step(self) -> None:
        available = None
        try:
            available = self._pass()
            self.delay, self._backoff = POLL, 2
        except Exception as error:
            # Only fixed ValueError texts are kept; anything else is named by its type.
            text = str(error) if isinstance(error, ValueError) else 'unexpected error'
            _LOG.info('Keeper pass failed: %s: %s', type(error).__name__, text)
            self.runtime['down']['count'] = 0
            self.runtime.update(phase='waiting', error=type(error).__name__ + ': ' + text)
            self.delay, self._backoff = self._backoff, min(self._backoff * 2, POLL)
        self._keep(available)
        try:
            self.beat()
        except Exception as error:
            _LOG.info('Keeper heartbeat not written: %s', type(error).__name__)

    def _keep(self, available):
        if self.keepalive is None:
            return
        try:
            self.runtime['keepalive'] = self.keepalive.ensure(available)
        except Exception as error:
            _LOG.info('Keep-alive check failed: %s', type(error).__name__)

    def rest(self) -> None:
        """Wait out the delay; meanwhile restart an exited keep-alive child once its backoff allows.

        Nothing is read: the restart rests on the last reading, and the child cannot start a Codespace.
        """
        end = self.clock() + self.delay  # monotonic: a wall-clock step cannot stretch it
        while (left := end - self.clock()) > 0:
            if self.keepalive is None:
                self.sleep(left)
                return
            self.sleep(min(left, TEND))
            try:
                record = self.keepalive.tend()
                if record is not None:
                    self.runtime['keepalive'] = record
                    self.beat()
            except Exception as error:
                _LOG.info('Keep-alive check failed: %s', type(error).__name__)

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
            return False
        refusal = self._refusal(value)
        if refusal:
            if self.runtime.get('phase') != 'refused':
                _LOG.info('Keeper refused: %s', refusal)
            self.runtime.update(phase='refused', error=refusal)
            self._notify('refused-' + repr(value.get('consented_at')),
                         f'Recovery for {self.project} is refused: {refusal}.')
            return False  # never hold a Codespace the pairing has moved away from
        reading = self.api.read()
        self.runtime['reading'] = {'at': reading.at, 'ok': reading.ok, 'state': reading.state,
                                   'pending': reading.pending}
        watch = self.runtime['down']
        if not reading.ok:
            watch['count'] = 0
            self.runtime.update(phase='waiting', error='Codespace state unreadable; check gh auth status and the network')
            return None
        if reading.state != self.previous[0]:
            _LOG.info('Codespace %s at %s', reading.state, _utc(reading.at))
        self.runtime.update(phase='watching', error=None)
        unanswered, due = self._unanswered(reading), None
        if reading.state == 'Available':
            self._up(reading)
        else:
            if watch['first_at'] is None:
                watch['first_at'] = reading.at  # the stop's first non-Available reading
                hold = value.get('hold_until')
                if isinstance(hold, (int, float)) and hold > reading.at:
                    # D5: a stop that began in a hold is never restarted, whatever the hold does later.
                    watch['hold_at_stop'] = hold
            if reading.state in DOWN:
                watch['count'] += 1
                if unanswered is not None:
                    watch['kind'] = 'LIFETIME'  # its restart is under way; never a second rid for it
                elif watch['count'] >= lifecycle.DOWN_READINGS and not watch.get('kind'):
                    due = self._classify(value, watch)
            else:
                watch['count'] = 0
        self.previous = reading.state, reading.at
        self.runtime['last_ok'] = {'at': reading.at, 'state': reading.state}
        if unanswered is not None:
            rid, posts, last_at = unanswered
            # Within REPOST_AFTER of its intent the POST may still be landing (spec 6.4 step 5.4).
            if self.now() - last_at >= lifecycle.REPOST_AFTER:
                return self._restart(watch, rid, posts)
        elif due is not None:
            return self._restart(watch, due, 0)
        elif reading.state == 'Available':
            self._planned(value)
        return reading.state == 'Available'

    def _unanswered(self, reading):
        """The rid, POST count and last intent time of a restart interrupted between intent and result.

        Only a Shutdown reading within the start routine's own span continues it; any other
        state closes it with an interrupted result and no POST, since a Queued, Starting or
        Available Codespace needs none, and so does an intent older than that span.
        """
        try:
            rows = [row for row in self.ledger.rows() if not row['rid'].startswith(lifecycle.OWNER_RID)]
        except OSError:
            return None  # an unreadable ledger also refuses any new restart at the gate
        if not rows or rows[-1]['kind'] != 'intent':
            return None
        rid = rows[-1]['rid']
        stale = self.now() - rows[-1]['at'] > lifecycle.SHUTDOWN_WAIT + lifecycle.AVAILABLE_WAIT
        if reading.state != 'Shutdown' or stale:
            self.ledger.append('result', rid, interrupted=True, state=reading.state)
            _LOG.info('Restart %s was interrupted; the Codespace is %s, so nothing is posted', rid, reading.state)
            last = self.runtime.get('last_restart')
            if isinstance(last, dict) and last.get('rid') == rid:
                last['outcome'] = 'interrupted; the Codespace was ' + str(reading.state)
            return None
        return rid, sum(row['rid'] == rid and row['kind'] == 'intent' for row in rows), rows[-1]['at']

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
        state, seen_at = self.previous
        gap = seen_at is None or reading.at - seen_at > HEARTBEAT_STALE
        if state is not None and state != 'Available':
            if gap and reading.last_used_at is not None and seen_at < reading.last_used_at <= reading.at:
                # A start this keeper did not see, inside a gap: its request is the run's start.
                anchor = {'at': reading.last_used_at, 'source': 'api'}
            else:
                anchor = {'at': reading.at, 'source': 'observed'}
        elif reading.last_used_at is not None and (
                anchor is None or reading.at > anchor['at'] + lifecycle.LIFETIME + lifecycle.WINDOW
                or (anchor.get('source') != 'keeper' and reading.last_used_at > anchor['at'] and gap)):
            # No anchor yet; one from a run GitHub has certainly stopped since; or a newer start
            # request after a gap long enough to hide a stop and a start (R16). Without such a
            # gap a newer last_used_at is a no-op request, which leaves GitHub's clock alone.
            # The keeper's own accepted POST stays the anchor of the run it started.
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
        key, due = 'stop-' + repr(watch['first_at']), None
        if kind == 'OTHER':
            self._notify(key, f'{self.project} stopped at {arming.format_time(watch["first_at"])} for a reason '
                              'other than the 12-hour limit; run task work:cs to start it.')
        elif kind == 'LIFETIME':
            try:
                reason = self._start_refusal(None, watch)
            except OSError:
                reason = 'the restart ledger is unreadable'
            if reason is None:
                due = lifecycle.new_rid(self.now())
            else:
                self._refused(key, watch['first_at'], reason)
        # Only now: a raise above leaves the stop unclassified, so the next readings retry it.
        watch['kind'] = kind
        return due

    def _start_refusal(self, rid, watch):
        """The gate, read afresh: arming, hold and budget, no hold when the stop began (D5),
        and no owner stop since the stop.

        Asked at classification and again at each POST. The rid's own ledger rows are not
        another restart, and an owner stop after the stop began means keep it stopped.
        """
        value, rows = arming.read(self.state), self.ledger.rows()
        reason = lifecycle.gate(arming=value, rows=[row for row in rows if row['rid'] != rid], now=self.now())
        # The pairing, as each pass checks it: a re-pair during the wait never starts the old one.
        reason = reason or self._refusal(value)
        if reason is None and getattr(self.api, 'token_read_ok', True) is False:
            # The reading just made used the last good token (D10); a start request needs a read that works.
            reason = (f'the GitHub token for {value.get("gh_user")} could not be read; run gh auth status '
                      '--hostname github.com')
        held = watch.get('hold_at_stop')
        if reason is None and isinstance(held, (int, float)):
            reason = 'it stopped during your hold until ' + arming.format_time(held)
        since = min([watch['first_at']] + [row['at'] for row in rows if row['rid'] == rid])
        owner = value.get('owner_stop_at')
        if reason is None and isinstance(owner, (int, float)) and owner >= since:
            reason = 'you stopped it at ' + arming.format_time(owner)
        if reason is None and value.get('codespace') != self.api.name:
            reason = 'the armed Codespace changed; run task work:cs:armed'
        return reason

    def _refused(self, key, stop_at, reason):
        self._notify(key, f"{self.project} stopped at {arming.format_time(stop_at)} for GitHub's 12-hour limit "
                          f'and was not restarted: {reason}. Run task work:cs to start it.')

    def _tick(self, phase=None):
        try:
            self.beat(phase)
        except Exception as error:
            _LOG.info('Keeper heartbeat not written: %s', type(error).__name__)

    def _pause(self, seconds):
        # A restart's waits keep the heartbeat, and a held keep-alive child, going.
        self.sleep(seconds)
        if self.keepalive is not None:
            try:
                record = self.keepalive.tend()
                if record is not None:
                    self.runtime['keepalive'] = record
            except Exception as error:
                _LOG.info('Keep-alive check failed: %s', type(error).__name__)
        self._tick()

    def _restart(self, watch, rid, posts):
        """Spec 6.4 steps 5-7 for one rid: True once the Codespace is back. One notification per outcome."""
        stop_at = watch['first_at']
        key = 'stop-' + repr(stop_at)
        last = {'rid': rid, 'at': self.now(), 'stopped_at': stop_at, 'outcome': 'restarting'}
        self.runtime['last_restart'] = last
        _LOG.info('Restart %s after the stop first seen %s', rid, _utc(stop_at))
        self._tick('restarting')
        try:
            result = lifecycle.start(self.api, ledger=self.ledger, rid=rid, posts=posts, now=self.now,
                                     sleep=self._pause, tick=self._tick,
                                     gate=lambda: self._start_refusal(rid, watch))
        except Exception as error:
            # Only fixed ValueError texts are kept; anything else is named by its type.
            detail = str(error).split(';')[0] if isinstance(error, ValueError) else type(error).__name__
            self._close(rid)
            return self._failed(key, last, detail)
        _LOG.info('Restart %s: %s after %s POSTs, last state %s, HTTP %s', rid, result['outcome'], result['posts'],
                  result['last_state'], result['status'])
        if result['outcome'] != 'available':
            self._close(rid)
        if result['outcome'] == 'refused':
            last['outcome'] = 'not restarted: ' + result['reason']
            self._refused(key, stop_at, result['reason'])
            return False
        if result['outcome'] != 'available':
            return self._failed(key, last, _start_detail(result))
        back_at = self.now()
        if result['posted_at'] is None:
            # Someone else started it during the wait: not this keeper's restart, so no resume.
            last['outcome'] = 'started elsewhere'
            _LOG.info('Restart %s: the Codespace came back without an accepted start request from the keeper', rid)
            self._notify(key, f"{self.project} was started by something else after GitHub's 12-hour stop; the "
                              f'keeper sent no start request that brought it up ({rid}), so its lead was not told '
                              'to resume. See task work:status.')
            return True
        anchor = {'at': result['posted_at'], 'source': 'keeper'}
        _LOG.info('Anchor %s from keeper', _utc(anchor['at']))
        # Recorded as a reading, so the next pass sees no transition and keeps the keeper's anchor.
        self.runtime.update(anchor=anchor, last_up_at=back_at, down={'count': 0, 'first_at': None},
                            reading={'at': back_at, 'ok': True, 'state': 'Available', 'pending': False},
                            last_ok={'at': back_at, 'state': 'Available'})
        self.previous = 'Available', back_at
        last.update(posted_at=result['posted_at'], back_at=back_at)
        self._restore(key, last, rid, stop_at, back_at,
                      f"{self.project} restarted after GitHub's 12-hour stop ({rid})",
                      f'back in {int(back_at - result["posted_at"])} s')
        return True

    def _close(self, rid):
        # A rid given up on is answered, so it never continues later (and never POSTs then).
        try:
            rows = [row for row in self.ledger.rows() if row['rid'] == rid]
            if rows and rows[-1]['kind'] == 'intent':
                self.ledger.append('result', rid, interrupted=True)
        except OSError as error:
            _LOG.info('Restart %s not closed in the ledger: %s', rid, type(error).__name__)

    def _failed(self, key, last, detail):
        last['outcome'] = 'start failed: ' + detail
        self._notify(key, f"{self.project} could not be restarted after GitHub's 12-hour stop ({detail}); "
                          'run task work:cs to start it.')
        return False

    def _planned(self, value):
        """work:cs:restart hands its restore and resume to the keeper (D6): once per rid, while fresh."""
        pending = value.get('pending_restore')
        if not isinstance(pending, dict):
            return
        rid, stopped_at, back_at = pending.get('rid'), pending.get('stopped_at'), pending.get('back_at')
        if not (isinstance(rid, str) and rid.startswith(lifecycle.OWNER_RID)
                and all(isinstance(at, (int, float)) and not isinstance(at, bool) for at in (stopped_at, back_at))):
            return
        key = 'restore-' + rid
        if ((self.runtime.get('last_restart') or {}).get('rid') == rid or key in self.runtime['notified']
                or self.now() - back_at > PENDING_FRESH):
            self._clear_pending(rid)  # consumed or expired
            return
        last = {'rid': rid, 'at': self.now(), 'stopped_at': stopped_at, 'back_at': back_at, 'outcome': 'restoring'}
        self.runtime['last_restart'] = last
        self._clear_pending(rid)
        _LOG.info('Restore %s after your restart', rid)
        self._restore(key, last, rid, stopped_at, back_at, f'{self.project} is back from your restart ({rid})', 'back')

    def _clear_pending(self, rid):
        # The keeper's one arming.json write: under the CLI's lock, never waiting for it. A skipped
        # clear is retried on a later pass; the rid check holds the restart to one restore meanwhile.
        try:
            with locked(self.state / 'workspace-control.lock'):
                arming.clear_pending_restore(self.state, rid)
        except BlockingIOError:
            pass
        except (OSError, ValueError) as error:
            _LOG.info('Planned restart %s not cleared from arming.json: %s', rid, type(error).__name__)

    def _machine(self):
        try:
            return restore.Herdr(read_json(self.state / 'herdr-machine.json').get('machine_id'))
        except (OSError, ValueError):
            return None

    def _restore(self, key, last, rid, stopped_at, back_at, what, back):
        """Spec 6.4 step 6 and 6.6: keep-alive, stale proxies, Herdr, then the lead. Never raises."""
        def outcome(text, note):
            last['outcome'] = back + '; ' + text
            self._notify(key, f'{what}{note}')
        self._tick('restoring')
        try:
            if self.keepalive is not None:
                self.runtime['keepalive'] = self.keepalive.ensure(True)
                self._tick()
            ended = self.end_proxies(self.api.name, self.state, stopped_at)
            if ended:
                _LOG.info('Ended Herdr proxies older than the stop (#46): %s', ' '.join(map(str, ended)))
            self._tick()
            herdr = self.herdr or self._machine()
            # Each Herdr call may take its 60 s timeout, so the heartbeat follows every one.
            herdr_at = (restore.wait_reachable(herdr, now=self.now, sleep=self._pause, tick=self._tick)
                        if herdr else None)
            if herdr_at is None:
                _LOG.info('Restore %s: Herdr not reachable', rid)
                return outcome('Herdr not reachable', ', but its Herdr machine is not reachable; open the Herdr TUI '
                                                      '(run herdr), then tell its lead to resume.')
            last['herdr_at'] = herdr_at
            hold = arming.read(self.state).get('hold_until')
            if isinstance(hold, (int, float)) and hold > self.now():
                # No resume prompts during a hold either (spec 6.8).
                return outcome('on hold, lead not told', f'; restarts are on hold until {arming.format_time(hold)}, '
                                                         'so its lead was not told to resume; tell it yourself. '
                                                         'task work:hold -- off ends the hold, and task work:status '
                                                         'shows the restart.')
            self._tick('resuming')
            told = restore.resume(herdr, rid, restore.resume_text(rid, stopped_at, back_at), now=self.now,
                                  sleep=self._pause, tick=self._tick)
            last['resume'] = told
            name = (restore.lead(herdr.agents()) or {}).get('name') if told != 'no-lead' else None
            who = f'its lead {name}' if name else 'its lead'
            _LOG.info('Resume %s: %s (%s)', rid, told, name or 'no lead')
            if told in ('delivered', 'resent'):
                outcome(f'Herdr in {int(herdr_at - back_at)} s; {name or "lead"} told',
                        f' and {who} was told to resume; task work:status shows the restart.')
            elif told == 'no-lead':
                outcome('no single lead', ', but no single lead-* agent was found on its Herdr machine; tell its '
                                          'lead to resume.')
            elif told == 'busy':
                outcome(f'{name or "lead"} busy', f', but {who} stayed busy for 10 minutes; tell it to resume.')
            else:
                outcome(f'resume not delivered to {name or "the lead"}',
                        f', but the resume message did not reach {who}; tell it to resume, and see task work:logs.')
        except Exception as error:
            detail = str(error).split(';')[0] if isinstance(error, ValueError) else type(error).__name__
            _LOG.info('Restore %s failed: %s', rid, detail)
            outcome('restore failed: ' + detail, f', but restoring it failed ({detail}); tell its lead to resume, '
                                                 'and see task work:logs.')
        finally:
            self.runtime['phase'] = 'watching'

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
            _end_left_behind(state)  # before this keeper starts its own
            current = Keeper(state, api=api, notifier=notify.notify,
                             keepalive=KeepAlive(api.name, state, value.get('gh_user'), value.get('ports') or []))
            caffeinate = _caffeinate()
            current.runtime.update(pid=os.getpid(), nonce=nonce, process_identity=process_identity(os.getpid()),
                                   runtime_id=runtime_id(), gh_user=value.get('gh_user'), error=None,
                                   caffeinate=caffeinate.pid if caffeinate else None)
            current.beat('starting')
            _LOG.info('Keeper started for %s as %s', api.name, value.get('gh_user'))
            while not current.done:
                current.step()
                if not current.done:
                    current.rest()
        except _Shutdown:
            pass
        except Exception as error:
            text = str(error) if isinstance(error, ValueError) else 'unexpected error'
            _LOG.info('Keeper cannot run: %s: %s', type(error).__name__, text)
        finally:
            # A second stop request must not interrupt cleanup.
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: None)
            if current is not None and current.keepalive is not None:
                # The child first: it is what holds the Codespace up and billed.
                try:
                    current.keepalive.stop()
                    current.runtime['keepalive'] = current.keepalive.record()
                except Exception as error:
                    _LOG.info('Keep-alive not stopped: %s', type(error).__name__)
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
