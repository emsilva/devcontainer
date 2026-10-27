"""Detached scratch reconciliation and project conversation synchronization."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import shlex
import signal
import subprocess
import sys
import tarfile
import threading
import time

from .state import atomic_write, discover_project, locked, read_json
from . import state as project_state
from . import scratch_tree, sync_cycle
from .tools import install_mutagen, mutagen_env
from .transport import CodespaceConnection

CONFIG = 'scratch.json'
RUNTIME = 'scratch-runtime.json'
SESSION = 'scratch-session.json'
# A failure reads the Codespace state at most once a minute. verify() reads it on its own
# failures, once per attempt, and those attempts back off from 2 s to 60 s (R23).
STATE_READ_GAP = 60
DELETED_RETRY = 15 * 60
KEEPER_CHECK = 60  # the keeper watch reads local files once a minute while the keeper is healthy
# Over a re-arm's keeper start without a consent prompt (~208 s: _ready 120 s, account 50 s, Codespace row
# 30 s, keeper start 8 s); a kill is told within KEEPER_CHECK + 240 s + two ticks, 6 min up to 30 s ticks.
KEEPER_DOWN = 240
_runtime_id = project_state.runtime_id


def _private_location(root, state):
    if Path(state).resolve().is_relative_to(Path(root).resolve() / '.scratch'):
        raise ValueError('Private sync state must remain outside .scratch.')


def _write(path, obj):
    atomic_write(path, (json.dumps(obj, sort_keys=True) + '\n').encode())


def _scratch(root):
    path = Path(root) / '.scratch'
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Project .scratch must be an existing directory without a symlink.')
    _validate_tree(path)
    return path


def _validate_tree(path):
    for _ in scratch_tree.entries(path):
        pass


def _archive_path(state, label):
    path = Path(state) / 'backups' / (str(time.time_ns()) + '-' + secrets.token_hex(8) + '-' + label + '.tar.gz')
    return path


def backup(root: Path, state: Path) -> Path:
    """Retain payloads and markers, excluding transient caches from private backups."""
    _private_location(root, state)
    source = _scratch(discover_project(Path(root)).root)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz', dereference=False) as archive:
        scratch_tree.add_backup(archive, source)
    path = _archive_path(state, 'local')
    atomic_write(path, buffer.getvalue(), mode=0o400)
    atomic_write(path.with_suffix(path.suffix + '.sha256'),
                 (hashlib.sha256(buffer.getvalue()).hexdigest() + '\n').encode(), mode=0o400)
    return path


def _remote(connection, remote_root, *, snapshot=False, allow_missing_scratch=False, prepare_identity=None):
    # Execute the canonical identity implementation on native remote Python.
    # Do not duplicate identity normalization or rely on directory basenames.
    # Identity probes must also accept older checkouts before scratch is seeded.
    source = Path(project_state.__file__).read_bytes()
    tree_source = Path(scratch_tree.__file__).read_bytes()
    code = '''import base64,io,json,os,pathlib,sys,tarfile,types
m=types.ModuleType("_project_sync_state");sys.modules[m.__name__]=m
exec(compile(base64.b64decode(__SOURCE__),"project-state.py","exec"),m.__dict__)
tree=types.ModuleType("_scratch_tree");sys.modules[tree.__name__]=tree
exec(compile(base64.b64decode(__TREE_SOURCE__),"scratch-tree.py","exec"),tree.__dict__)
p=m.discover_project(pathlib.Path(__ROOT__))
s=p.root/".scratch"
if __PREPARE_IDENTITY__ is not None:
 if p.identity!=__PREPARE_IDENTITY__ or str(p.root)!=__ROOT__ or ".devcontainer-work" in p.root.parts:
  raise ValueError("Scratch preparation requires the verified ordinary checkout identity/root")
 with m._parent(s) as (parent,name):
  try:os.mkdir(name,mode=0o700,dir_fd=parent)
  except FileExistsError:pass
if s.is_symlink():raise ValueError("Remote scratch symlink")
if s.exists() and not s.is_dir():raise ValueError("Remote scratch is not a directory")
if not s.is_dir() and (__SNAPSHOT__ or not __ALLOW_MISSING__):raise ValueError("Remote .scratch missing; configure project continuity remotely first")
if s.is_dir():
 for _ in tree.entries(s):pass
if __SNAPSHOT__:
 b=io.BytesIO()
 with tarfile.open(fileobj=b,mode="w:gz",dereference=False) as t:tree.add_backup(t,s)
 sys.stdout.buffer.write(b.getvalue())
else:print(json.dumps({"identity":p.identity,"root":str(p.root)}))
'''.replace('__SOURCE__', repr(base64.b64encode(source).decode())).replace('__TREE_SOURCE__', repr(base64.b64encode(tree_source).decode())).replace('__ROOT__', repr(remote_root)).replace('__SNAPSHOT__', repr(snapshot)).replace('__ALLOW_MISSING__', repr(allow_missing_scratch)).replace('__PREPARE_IDENTITY__', repr(prepare_identity))
    return connection.python(code, timeout=90)


def _config(root, state):
    path = Path(state) / CONFIG
    if not path.exists():
        raise ValueError('Scratch sync is not configured; run scratch:configure first.')
    config = read_json(path)
    project = discover_project(Path(root))
    if (config.get('version') != 1 or config.get('identity') != project.identity
            or config.get('root') != str(project.root)
            or not isinstance(config.get('codespace'), str)
            or not isinstance(config.get('remote_root'), str)
            or not config['remote_root'].startswith('/')
            or '..' in Path(config['remote_root']).parts):
        raise ValueError('Scratch configuration does not match this repository/checkout.')
    return config


def configure(root: Path, state: Path, codespace: str, remote_root: str) -> dict:
    project = discover_project(Path(root))
    _private_location(project.root, state)
    _scratch(project.root)
    if (not remote_root.startswith('/') or '..' in Path(remote_root).parts
            or any(c in remote_root for c in '\r\n\0')):
        raise ValueError('Remote checkout path must be absolute without traversal.')
    candidate = {'version': 1, 'identity': project.identity, 'root': str(project.root),
                 'codespace': codespace, 'remote_root': remote_root}
    with locked(Path(state) / 'scratch-control.lock', blocking=True):
        config_path = Path(state) / CONFIG
        if config_path.exists() and read_json(config_path) != candidate:
            raise ValueError('Scratch pairing already exists; preserve it and explicitly review a rebind.')
        with CodespaceConnection(codespace, state=Path(state)) as connection:
            connection.accept_new = True  # Explicit configure is the sole TOFU boundary.
            remote = json.loads(_remote(connection, remote_root, allow_missing_scratch=True))
            if remote.get('identity') != project.identity or remote.get('root') != remote_root:
                raise ValueError('Remote repository identity/checkout does not match the selected local repository.')
        _write(config_path, candidate)
    return candidate


def _owned(runtime, state):
    return project_state.owned_process(runtime, state, flag='--supervise', module='.scratch')


def status(state: Path) -> dict:
    path = Path(state) / RUNTIME
    if not path.exists():
        return {'running': False, 'phase': 'stopped'}
    runtime = read_json(path)
    return {**runtime, 'running': _owned(runtime, state)}


def start(root: Path, state: Path) -> dict:
    state = Path(state).absolute()
    root = Path(root).resolve()
    if (state / 'scratch-retarget.json').exists():
        raise ValueError('An interrupted scratch retarget is pending; run task work:cs to finish its owned transition first')
    _config(root, state)
    _private_location(root, state)
    _scratch(root)
    runtime_id = _runtime_id()
    current = status(state)
    if current.get('running') and (current.get('checkpoint_version') != sync_cycle.SCHEMA or current.get('runtime_id') != runtime_id):
        stop(state)
    with locked(state / 'scratch-control.lock', blocking=True):
        current = status(state)
        if current['running']:
            if current.get('checkpoint_version') == sync_cycle.SCHEMA and current.get('runtime_id') == runtime_id:
                return current
            raise ValueError('Another worker started during the helper upgrade; retry task work:cs.')
        if current.get('phase') != 'stopped' and current.get('session'):
            _recover_session(state, current)
        # Installation failures happen synchronously and clearly to the caller.
        install_mutagen(state)
        return project_state.launch_detached(
            'scratch', '--supervise', root, state, env=dict(os.environ), status=status,
            exited='Scratch supervisor exited during startup; inspect scratch.log.',
            timed_out='Scratch supervisor startup timed out.')


def _saved_session(state, config=None):
    path = Path(state) / SESSION
    if not path.exists():
        return None
    receipt = read_json(path)
    nonce = receipt.get('nonce')
    if (not isinstance(nonce, str) or len(nonce) != 48
            or any(c not in '0123456789abcdef' for c in nonce)
            or receipt.get('name') != 'scratch-' + nonce
            or not isinstance(receipt.get('binding'), dict)
            or not isinstance(receipt.get('established'), bool)):
        raise ValueError('Invalid owned scratch session receipt; existing state was preserved.')
    if config is not None and receipt['binding'] != config:
        raise ValueError('Saved scratch session belongs to a different pairing; existing baseline was preserved.')
    return receipt


def _session_info(binary, state, receipt, *, persist=True):
    # List all sessions so an absent selector is distinguishable from a failed
    # daemon/API request. Never infer absence from arbitrary command failure.
    entries = json.loads(_mutagen(binary, state, 'sync', 'list', '--template={{json .}}'))
    if not isinstance(entries, list):
        raise ValueError('Invalid Mutagen session inventory; existing state was preserved.')
    matches = [entry for entry in entries if entry.get('name') == receipt['name']]
    if len(matches) > 1:
        raise ValueError('Duplicate owned Mutagen session names; existing state was preserved.')
    if not matches:
        if receipt['established']:
            raise ValueError('Saved Mutagen baseline is missing; restore private state before restarting sync.')
        return None
    info = matches[0]
    identifier = info.get('identifier')
    if not isinstance(identifier, str) or not identifier.startswith('sync_'):
        raise ValueError('Invalid Mutagen session identifier.')
    if receipt.get('identifier') is not None and receipt['identifier'] != identifier:
        raise ValueError('Owned Mutagen session identity changed; existing baseline was preserved.')
    receipt.update(identifier=identifier, established=True)
    if persist:
        _write(Path(state) / SESSION, receipt)
    return info


def _reconciliation(info):
    conflicts = info.get('conflicts', [])
    paths = []
    for conflict in conflicts[:100]:
        path = conflict.get('root', '') if isinstance(conflict, dict) else ''
        paths.append(''.join(c for c in str(path) if c.isprintable())[:300])
    if conflicts:
        return dict(phase='conflicts', conflicts=paths)
    connected = all(info.get(side, {}).get('connected') is True for side in ('alpha', 'beta'))
    clean = (connected and not info.get('paused') and info.get('status') == 'watching'
             and not info.get('lastError') and not info.get('excludedConflicts')
             and not any(info.get(side, {}).get('scanProblems') or info.get(side, {}).get('transitionProblems')
                         or info.get(side, {}).get('excludedScanProblems') or info.get(side, {}).get('excludedTransitionProblems')
                         for side in ('alpha', 'beta')))
    return dict(phase='synchronized' if clean else 'waiting', conflicts=[])


def inspect_sync(root, state):
    runtime = status(Path(state))
    if not runtime.get('running'):
        return dict(runtime, phase='cleanup_required' if runtime.get('phase') == 'cleanup_required' else 'stopped')
    if runtime.get('phase') in ('stopped', 'cleanup_required'):
        return runtime
    config = _config(root, state)
    receipt = _saved_session(state, config)
    if receipt is None:
        return dict(phase='connecting', conflicts=[])
    info = _session_info(install_mutagen(state, allow_download=False), state, dict(receipt), persist=False)
    return _reconciliation(info) if info else dict(phase='connecting', conflicts=[])


def _pause_saved_session(binary, state, receipt):
    # Pausing for safety must not promote a planned receipt before the remote
    # endpoint passes validation. Resume inspection owns that durable update.
    info = _session_info(binary, state, dict(receipt), persist=False)
    if info is not None:
        _mutagen(binary, state, 'sync', 'pause', info['identifier'])
    return info


def _recover_session(state, runtime):
    receipt = _saved_session(state)
    if receipt is None:
        # A runtime nonce alone is not an authority to mutate a foreign session.
        return
    if runtime.get('session') != receipt['name']:
        raise ValueError('Runtime and saved scratch session disagree; existing state was preserved.')
    binary = install_mutagen(state)
    _pause_saved_session(binary, state, receipt)
    _mutagen(binary, state, 'daemon', 'stop')
    _write(state / RUNTIME, {**runtime, 'phase': 'stopped', 'process_identity': None})


def stop(state: Path) -> dict:
    state = Path(state).absolute()
    with locked(state / 'scratch-control.lock', blocking=True):
        current = status(state)
        if not current['running']:
            if current.get('phase') != 'stopped' and current.get('session'):
                _recover_session(state, current)
            return status(state)
        # A nonce in argv plus a fresh process creation identity protects PID reuse.
        if _owned(current, state):
            os.kill(current['pid'], signal.SIGTERM)
        if project_state.wait_gone(current, 12):
            return status(state)
        raise ValueError('Scratch supervisor is still shutting down; inspect status before retrying.')


def _mutagen(binary, state, *args, timeout=30):
    try:
        result = subprocess.run([str(binary), *args], env=mutagen_env(state),
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError('Mutagen command timed out; inspect session status before retrying.') from None
    if result.returncode:
        raise ValueError('Mutagen command failed (' + args[0] + '); inspect session status.')
    return result.stdout[-32768:]


def _transport_wrapper(state):
    # MUTAGEN_SSH_PATH selects a directory containing ordinary OpenSSH wrappers.
    # shlex.quote preserves spaces without accepting shell fragments from paths.
    script = '#!/bin/sh\nexec /usr/bin/ssh -F ' + shlex.quote(str(state / 'ssh-config')) + ' "$@"\n'
    atomic_write(state / 'transport-bin/ssh', script.encode(), mode=0o700)
    scp = '#!/bin/sh\nexec /usr/bin/scp -F ' + shlex.quote(str(state / 'ssh-config')) + ' "$@"\n'
    atomic_write(state / 'transport-bin/scp', scp.encode(), mode=0o700)


class _Shutdown(BaseException):
    pass


class _Notices:
    """The worker's notices to the owner (spec 6.7, D7), each sent once.

    An unarmed project hears once per Codespace stop; an armed one, once while its keeper
    is dead or hung. The flags live in the runtime, so a restarted worker keeps them.
    An unreadable file skips a notice and a failed osascript is logged; neither stops the worker.
    """

    def __init__(self, state, config, runtime, logger, *, clock=time.monotonic):
        self.state, self.runtime, self.logger, self.clock = Path(state), runtime, logger, clock
        self.codespace, self.project = config['codespace'], Path(config['root']).name
        self.read_at = self.checked_at = self.unhealthy_at = self.known = None
        self.unreadable_logged = False
        self.deleted_login = None

    def save(self):
        _write(self.state / RUNTIME, self.runtime)

    def connected(self):
        # A working connection makes an old stop reading stale; the stop itself ends at verify() (R23).
        self.known = None

    def verified(self):
        # Only a cycle that passes verify() shows the Codespace is back (R23): that ends the
        # stop, so the next one notifies again. A connection alone can still fail verify().
        self.runtime['notified_stop'] = False
        self.runtime['deleted_logged'] = False
        self.deleted_login = None
        self.known = None

    def _read_state(self):
        """Pin the state read and account label to the same token, kept out of runtime."""
        from .replication import read_codespace_state
        from .transport import github_token, _upstream
        try:
            token = github_token(os.environ)
            learned = read_codespace_state(self.codespace, dict(os.environ, GH_TOKEN=token))
            if learned == 'Deleted' and self.deleted_login is None:
                self.deleted_login = "this Mac's GitHub account"
                try:
                    status, body = _upstream('https://api.github.com/user', token)
                    account = json.loads(body) if status == 200 else {}
                    login = account.get('login') if isinstance(account, dict) else None
                    if isinstance(login, str) and login and all(c.isalnum() or c == '-' for c in login):
                        self.deleted_login = login
                except Exception:
                    pass  # only the fixed fallback reaches a message
            return learned
        except (OSError, ValueError):
            return None

    def _deleted_message(self):
        return (f'{self.codespace} is not visible to ' + (self.deleted_login or "this Mac's GitHub account")
                + ': it was deleted, or it belongs to another account.')

    def explain(self, error, message):
        """The worker's error for a failure; a known stop names its state, not the symptom (#48)."""
        from .replication import ConnectionVerificationError, stopped_message
        now = self.clock()
        read_wall = None
        if isinstance(error, ConnectionVerificationError):
            # verify() does not carry its credential. Re-read a new Deleted episode with a
            # pinned token so its /user label describes the account that confirmed absence.
            learned = self._read_state() if error.state == 'Deleted' and self.deleted_login is None else error.state
            self.read_at, self.known = now, learned
            read_wall = time.time()
        elif self.read_at is None or now - self.read_at >= (DELETED_RETRY if self.known == 'Deleted' else STATE_READ_GAP):
            learned = self._read_state()
            self.read_at = now
            if learned is not None or self.known != 'Deleted':
                self.known = learned
            read_wall = time.time()
        # A cached known records nothing: a start in that minute must not inherit the old stop (#61).
        from . import evidence, keeper
        if read_wall is not None and self.known in keeper.DOWN:
            evidence.record_stop(self.state, self.codespace, read_wall)
        if self.known == 'Deleted':
            if not self.runtime.get('deleted_logged'):
                self.runtime['deleted_logged'] = True
                last = self.runtime.get('last_synchronized_at')
                when = (' (last synchronized ' + time.strftime('%H:%M', time.localtime(last)) + ')'
                        if isinstance(last, (int, float)) else '')
                self.logger.info('Scratch paused: %s Your .scratch/ is intact%s.', self._deleted_message(), when)
            return self._deleted_message()
        return message if self.known in (None, 'Available') else stopped_message(self.known)

    def notify_stop(self):
        """Notify once per stop of an unarmed project's Codespace, from the state explain() learned."""
        from . import keeper, lifecycle
        # Only a state that means down (R24); Starting or Queued may be the owner's own start.
        if (self.known not in keeper.DOWN + lifecycle.TERMINAL or self.runtime.get('notified_stop')
                or self._armed() is not False):
            return  # armed projects hear from their keeper
        self.runtime['notified_stop'] = True
        self.save()
        self._send(self._deleted_message() if self.known == 'Deleted' else
                   f"{self.project}'s Codespace stopped ({self.known}); run task work:cs to bring it back")

    def watch_keeper(self):
        """Notify once an armed project's keeper has read dead or hung for KEEPER_DOWN; local files only."""
        from . import keeper
        now = self.clock()
        # A reading a minute while healthy or already told; every tick while a notice is due (R27).
        if ((self.unhealthy_at is None or self.runtime.get('notified_keeper'))
                and self.checked_at is not None and now - self.checked_at < KEEPER_CHECK):
            return
        self.checked_at = now
        armed = self._armed()
        if armed is not True:
            self.unhealthy_at = None
            if armed is False and self.runtime.get('notified_keeper'):
                self.runtime['notified_keeper'] = False  # disarmed: a re-armed keeper is watched afresh
                self.save()
            return  # unarmed, disarmed or unknown: nothing to watch
        try:
            current = keeper.status(self.state)
        except (OSError, ValueError):
            current = {'running': False}  # as task work:status shows it
        except Exception as error:
            self.logger.info('Keeper check skipped: %s', type(error).__name__)
            return
        age = current.get('heartbeat_age')
        if current.get('running') and isinstance(age, (int, float)) and age <= keeper.HEARTBEAT_STALE:
            self.unhealthy_at = None
            if self.runtime.get('notified_keeper'):
                self.runtime['notified_keeper'] = False
                self.save()
        elif self.unhealthy_at is None:
            # An arm can start its keeper minutes after the worker, as on a re-arm (D1), so wait
            # out KEEPER_DOWN before notifying (R25, R26).
            self.unhealthy_at = now
        elif now - self.unhealthy_at >= KEEPER_DOWN and not self.runtime.get('notified_keeper'):
            self.runtime['notified_keeper'] = True
            self.save()
            self._send(f'Recovery for {self.project} is not running; run task work:cs:armed')

    def _armed(self):
        """True or False from arming.json; None when it cannot be read, which sends no notice."""
        from . import arming
        try:
            return bool(arming.read(self.state).get('armed'))
        except (OSError, ValueError):
            if not self.unreadable_logged:
                self.unreadable_logged = True
                self.logger.info('arming.json cannot be read, so no stop or recovery notice is sent; '
                                 'inspect task work:status')
            return None

    def _send(self, text):
        from . import keeper, notify
        self.logger.info('Notify: %s', text)
        if not notify.notify(keeper.TITLE, text):
            self.logger.info('The notification could not be shown')


def complete_retarget(state):
    """Finish a prior explicit owned transition before choosing another one."""
    state = Path(state)
    try:
        intent = read_json(state / 'scratch-retarget.json')
    except FileNotFoundError:
        return
    candidate = intent['candidate']
    return retarget(Path(candidate['root']), state, candidate['codespace'], candidate['remote_root'])


def _verify_destination(project, state, codespace, remote_root):
    if '.devcontainer-work' in Path(remote_root).parts:
        raise ValueError('Choose a verified ordinary Codespace checkout before retargeting')
    with CodespaceConnection(codespace, state=Path(state)) as connection:
        remote = json.loads(_remote(connection, remote_root, allow_missing_scratch=True))
    if remote.get('identity') != project.identity or remote.get('root') != remote_root:
        raise ValueError('Remote repository identity/checkout differs; old binding preserved')


def retarget(root, state, codespace, remote_root):
    """Retire a verified binding by pausing and retaining its exact baseline."""
    state = Path(state)
    project = discover_project(Path(root))
    _scratch(project.root)
    if (not Path(remote_root).is_absolute() or '..' in Path(remote_root).parts
            or any(c in remote_root for c in '\r\n\0')):
        raise ValueError('Invalid remote checkout path')
    candidate = dict(version=1, identity=project.identity, root=str(project.root),
                     codespace=codespace, remote_root=remote_root)
    intent_path = state / 'scratch-retarget.json'
    path = state / CONFIG
    with locked(state / 'scratch-control.lock', blocking=True):
        if not intent_path.exists() and path.exists() and read_json(path) == candidate:
            return candidate
    # Before stop, archives or config writes, authenticate the selected normal
    # checkout. Discovery and retarget never create/restore repository files.
    _verify_destination(project, state, codespace, remote_root)
    if not intent_path.exists() or status(state).get('running'):
        stop(state)
    with locked(state / 'scratch-control.lock', blocking=True):
        if not intent_path.exists() and not path.exists():
            _write(path, candidate)
            return candidate
        legacy_intent = False
        if intent_path.exists():
            intent = read_json(intent_path)
            previous = intent.get('previous')
            if (intent.get('schema') not in (1, 2) or not isinstance(previous, dict)
                    or previous.get('identity') != project.identity
                    or intent.get('candidate', {}).get('identity') != project.identity):
                raise ValueError('Invalid interrupted scratch retarget; existing state preserved')
            receipt = intent.get('receipt')
            archive = Path(intent['archive'])
            if archive.parent != state / 'retired-bindings':
                raise ValueError('Invalid scratch retarget archive')
            original_intent = archive / 'retarget-intent.json'
            allowed = [previous, intent['candidate']]
            observed = intent.get('observed_binding')
            if observed is not None:
                # The retained original intent anchors the extra crash state;
                # an arbitrary changed live config never becomes authorized.
                original = read_json(original_intent)
                if observed not in (previous, original.get('candidate')):
                    raise ValueError('Invalid observed scratch binding in retarget journal')
                allowed.append(observed)
            actual = read_json(path)
            if actual not in allowed:
                raise ValueError('Scratch binding changed during interrupted retarget')
            legacy_intent = intent['schema'] == 1
            # Preserve the old intent, including its generated candidate, as
            # evidence; never replay a generated endpoint into the live binding.
            if not original_intent.exists():
                _write(original_intent, intent)
            # Journal the authenticated normal destination before changing any
            # receipt/config so a crash after config publication can replay.
            if intent['candidate'] != candidate:
                intent = dict(intent, candidate=candidate, observed_binding=intent.get('observed_binding', actual))
                _write(intent_path, intent)
        else:
            previous = read_json(path)
            if previous == candidate:
                return candidate
            if previous.get('identity') != project.identity:
                raise ValueError('Cannot retarget another project identity')
            receipt = _saved_session(state, previous)
            archive = state / 'retired-bindings' / (str(time.time_ns()) + '-' + secrets.token_hex(4))
            intent = dict(schema=2, previous=previous, candidate=candidate, receipt=receipt, archive=str(archive))
            _write(intent_path, intent)
        saved = _saved_session(state)
        if saved is not None and saved != receipt:
            raise ValueError('Scratch receipt changed during interrupted retarget')
        _write(archive / CONFIG, previous)
        baseline = 'none'
        if receipt:
            if receipt.get('binding') != previous:
                raise ValueError('Retarget receipt does not own the old binding')
            binary = install_mutagen(state)
            checked = dict(receipt)
            if legacy_intent:
                # Old schema-1 producers may already have terminated the
                # baseline before crashing. Preserve that fact in the archive.
                checked['established'] = False
            info = _session_info(binary, state, checked, persist=False)
            if info is not None:
                _mutagen(binary, state, 'sync', 'pause', info['identifier'])
                baseline = 'paused'
            else:
                baseline = 'missing-before-migration' if legacy_intent else 'never-established'
            _write(archive / SESSION, receipt)
            (state / SESSION).unlink(missing_ok=True)
        _write(archive / 'migration.json', dict(baseline=baseline, destination=candidate))
        _write(path, candidate)
        intent_path.unlink(missing_ok=True)
    return candidate


def supervise(root: Path, state: Path, nonce=None) -> None:
    root, state = Path(root).resolve(), Path(state).absolute()
    nonce = nonce or secrets.token_hex(24)
    exiting = threading.Event()
    def shutdown(*_):
        exiting.set()
        raise _Shutdown()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)
    with locked(state / 'scratch-supervisor.lock'):
        config = _config(root, state)
        atomic_write(state / '.log-ready', b'1\n')
        log_path = state / 'scratch.log'
        if log_path.is_symlink():
            raise ValueError('Scratch log must not be a symlink.')
        handler = RotatingFileHandler(log_path, maxBytes=256 * 1024, backupCount=2)
        handler.setFormatter(project_state.log_formatter())
        os.chmod(log_path, 0o600)
        logger = logging.getLogger('project-sync-' + nonce)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        receipt = _saved_session(state, config)
        previous = read_json(state / RUNTIME) if (state / RUNTIME).exists() else {}
        same = previous.get('binding', previous.get('prepared_binding')) == config
        retained = previous.get('retained_receipts', {}) if same else {}
        runtime = {'pid': os.getpid(), 'nonce': nonce, 'process_identity': project_state.process_identity(os.getpid()),
                   'phase': 'starting', 'session': receipt['name'] if receipt else None,
                   'checkpoint_version': sync_cycle.SCHEMA, 'runtime_id': _runtime_id(),
                   'connection_prepared': False, 'binding': config, 'retained_receipts': retained,
                   'notified_stop': same and previous.get('notified_stop') is True,
                   'notified_keeper': same and previous.get('notified_keeper') is True,
                   'deleted_logged': same and previous.get('deleted_logged') is True,
                   'last_synchronized_at': previous.get('last_synchronized_at') if same else None}
        notices = _Notices(state, config, runtime, logger)
        def update(phase, **values):
            runtime.update(phase=phase, heartbeat_at=time.time(), **values)
            if phase != 'syncing':
                runtime['connection_prepared'] = False
            _write(state / RUNTIME, runtime)
        def verified():
            # The Codespace answered verify(): end the stop and reset the reconnect backoff,
            # so a reachable Codespace whose verify() keeps failing still backs off (R23).
            nonlocal backoff
            backoff = 2
            notices.verified()
        update('starting')
        binary = None
        session = receipt['name'] if receipt else None
        try:
            binary = install_mutagen(state)
            _transport_wrapper(state)
            if receipt:
                _pause_saved_session(binary, state, receipt)
            backoff = 2
            while not exiting.is_set():
                try:
                    _scratch(root)
                    update('connecting')
                    with CodespaceConnection(config['codespace'], state=state) as connection:
                        # Only first setup may seed missing scratch. A retained
                        # baseline requires its existing endpoint before resume.
                        remote = json.loads(_remote(connection, config['remote_root'],
                                                    prepare_identity=config['identity'] if receipt is None else None))
                        if remote.get('identity') != config['identity'] or remote.get('root') != config['remote_root']:
                            raise ValueError('Remote repository identity changed; refusing synchronization.')
                        connection.ssh_config(state / 'ssh-config')
                        from .ssh_hosts import refresh_ssh
                        refresh_ssh(state)
                        if receipt is None:
                            session = 'scratch-' + nonce
                            receipt = {'name': session, 'nonce': nonce, 'binding': config, 'established': False}
                            _write(state / SESSION, receipt)
                            update('preparing', session=session)
                        info = _session_info(binary, state, receipt)
                        if info is not None and _reconciliation(info)['phase'] == 'synchronized':
                            runtime['last_synchronized_at'] = time.time()
                        if info is None:
                            backup(root, state)
                            remote_snapshot = _remote(connection, config['remote_root'], snapshot=True)
                            remote_backup = _archive_path(state, 'remote')
                            atomic_write(remote_backup, remote_snapshot, mode=0o400)
                            args = ['sync', 'create', str(root / '.scratch'),
                                    'project-sync:' + config['remote_root'] + '/.scratch',
                                    '--name=' + session, '--mode=two-way-safe', '--hash=sha256',
                                    '--symlink-mode=ignore', '--no-global-configuration', '--ignore-vcs', '--paused']
                            args += ['--ignore=' + item for item in scratch_tree.MUTAGEN_IGNORES]
                            # Record the owned name before create; partial creation
                            # can then be cleaned without touching another session.
                            update('creating', session=session)
                            _mutagen(binary, state, *args, timeout=120)
                            info = _session_info(binary, state, receipt)
                            if info is None:
                                raise ValueError('Mutagen create returned without a session; retrying safely.')
                        _mutagen(binary, state, 'sync', 'resume', info['identifier'])
                        notices.connected()
                        update('syncing', connection_prepared=True, prepared_binding=config,
                               prepared_at=time.time(), error=None)
                        logger.info('Scratch connection established.')
                        def synchronize():
                            from .replication import sync_project_data, ConnectionVerificationError
                            project = discover_project(root)
                            def progress(result):
                                if result['stage'] != 'verify':
                                    verified()  # this cycle has passed verify()
                                for name, row in result['directions'].items():
                                    if sync_cycle.receipt_matches(row.get('receipt'), row.get('snapshot_id')):
                                        runtime['retained_receipts'][name] = row['receipt']
                                failed = result['outcome'] in ('failed', 'degraded')
                                phase = 'waiting' if result['outcome'] == 'failed' and result['stage'] == 'verify' else 'syncing'
                                update(phase, conversations=result,
                                       conversation_error='Conversation sync incomplete; inspect current cycle' if failed else None)
                            # Reset evidence before registry reads or verification.
                            update('syncing', conversations={}, conversation_error=None)
                            registry_path = state / 'workspace.json'
                            value = read_json(registry_path) if registry_path.exists() else {}
                            old_roots = value.get('verified_remote_historical_roots', [])
                            try:
                                # The exchange verifies every authorized remote
                                # scope; changed identities must fail, not vanish.
                                result = sync_project_data(connection, project, state,
                                                           config['remote_root'], historical_roots=old_roots,
                                                           progress=progress)
                                progress(result)
                            except ConnectionVerificationError:
                                raise
                            except (OSError, ValueError, KeyError, TypeError):
                                result = runtime['conversations'] or sync_cycle.begin(project, config['remote_root'])
                                result.update(outcome='failed', completed_at=time.time(),
                                              issues=[dict(reason='Conversation cycle incomplete; previous receipts preserved')])
                                progress(result)
                        synchronize()
                        while not exiting.wait(5):
                            notices.watch_keeper()
                            if connection.child.poll() is not None:
                                raise ValueError('Codespace forward disconnected; waiting to reconnect.')
                            _validate_tree(root / '.scratch')
                            remote = json.loads(_remote(connection, config['remote_root']))
                            if remote.get('identity') != config['identity'] or remote.get('root') != config['remote_root']:
                                raise ValueError('Remote repository identity changed; synchronization paused.')
                            summary = _mutagen(binary, state, 'sync', 'list', session)
                            info = _session_info(binary, state, receipt)
                            if info is not None and _reconciliation(info)['phase'] == 'synchronized':
                                runtime['last_synchronized_at'] = time.time()
                            update('syncing', detail=summary)
                            synchronize()
                except Exception as error:
                    if receipt:
                        try:
                            _pause_saved_session(binary, state, receipt)
                        except Exception:
                            update('cleanup_required', error='Owned session could not be paused; refusing reconnect.')
                            logger.info('Owned session could not be paused; stopping supervisor.')
                            break
                    # Only controlled ValueError messages are exposed. Never log
                    # request/connection bodies or arbitrary subprocess output.
                    message = str(error) if isinstance(error, ValueError) else type(error).__name__
                    # Waiting first: the state read can take 40 s, and status must not show syncing meanwhile.
                    update('waiting', error=message)
                    explained = notices.explain(error, message)
                    if explained != message:
                        message = explained
                        update('waiting', error=message)
                    if notices.known != 'Deleted':
                        logger.info('Scratch waiting: %s', message)
                    notices.notify_stop()
                    notices.watch_keeper()
                    if notices.known == 'Deleted':
                        # A paused connection still watches the keeper locally (R27).
                        # Only connection/API attempts wait the full fifteen minutes.
                        remaining = DELETED_RETRY
                        while remaining > 0 and not exiting.is_set():
                            interval = min(remaining, KEEPER_CHECK)
                            if exiting.wait(interval):
                                break
                            remaining -= interval
                            notices.watch_keeper()
                        if exiting.is_set():
                            break
                    elif exiting.wait(backoff):
                        break
                    backoff = min(backoff * 2, 60)
        except _Shutdown:
            pass
        finally:
            # Further stop requests should not interrupt owned-child cleanup.
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: None)
            cleanup_ok = True
            if binary is not None and receipt:
                try:
                    _pause_saved_session(binary, state, receipt)
                except Exception:
                    cleanup_ok = False
                    logger.info('Owned Mutagen session pause requires retry: %s', session)
            if binary is not None:
                try:
                    _mutagen(binary, state, 'daemon', 'stop')
                except Exception:
                    logger.info('Private Mutagen daemon cleanup requires retry.')
            update('stopped' if cleanup_ok else 'cleanup_required', process_identity=None)
            handler.close()
            logger.removeHandler(handler)


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--supervise':
        supervise(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    else:
        raise SystemExit('Expected --supervise ROOT STATE NONCE')
