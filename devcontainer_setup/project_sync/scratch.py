"""Detached scratch-only synchronization and immutable recovery snapshots."""
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
from .tools import install_mutagen, mutagen_env
from .transport import CodespaceConnection

CONFIG = 'scratch.json'
RUNTIME = 'scratch-runtime.json'
SESSION = 'scratch-session.json'
_CHILDREN = {}


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
    for directory, dirs, files in os.walk(path, followlinks=False):
        folded = set()
        for name in dirs + files:
            item = Path(directory) / name
            if item.is_symlink():
                raise ValueError('Scratch symlinks are unsupported; preserve and resolve them explicitly.')
            if name.casefold() in folded:
                raise ValueError('Scratch case collisions are unsupported; preserve and resolve them explicitly.')
            folded.add(name.casefold())
            if not item.is_file() and not item.is_dir():
                raise ValueError('Scratch contains an unsupported special file.')


def _archive_path(state, label):
    path = Path(state) / 'backups' / (str(time.time_ns()) + '-' + secrets.token_hex(8) + '-' + label + '.tar.gz')
    return path


def backup(root: Path, state: Path) -> Path:
    """Include ignored payloads; snapshots never enter Git or the sync root."""
    _private_location(root, state)
    source = _scratch(discover_project(Path(root)).root)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz', dereference=False) as archive:
        archive.add(source, arcname='.scratch', recursive=True)
    path = _archive_path(state, 'local')
    atomic_write(path, buffer.getvalue(), mode=0o400)
    atomic_write(path.with_suffix(path.suffix + '.sha256'),
                 (hashlib.sha256(buffer.getvalue()).hexdigest() + '\n').encode(), mode=0o400)
    return path


def _remote(connection, remote_root, *, snapshot=False):
    # Execute the canonical identity implementation on native remote Python.
    # Do not duplicate identity normalization or rely on directory basenames.
    source = Path(project_state.__file__).read_bytes()
    code = '''import base64,io,json,os,pathlib,sys,tarfile,types
m=types.ModuleType("_project_sync_state");sys.modules[m.__name__]=m
exec(compile(base64.b64decode(__SOURCE__),"project-state.py","exec"),m.__dict__)
p=m.discover_project(pathlib.Path(__ROOT__))
s=p.root/".scratch"
if s.is_symlink():raise ValueError("Remote scratch symlink")
if not s.is_dir():raise ValueError("Remote .scratch missing; configure project continuity remotely first")
for parent,dirs,files in os.walk(s,followlinks=False):
 names=set()
 for name in dirs+files:
  item=pathlib.Path(parent)/name
  if item.is_symlink():raise ValueError("Remote scratch symlink")
  if name.casefold() in names:raise ValueError("Remote scratch case collision")
  names.add(name.casefold())
  if not item.is_file() and not item.is_dir():raise ValueError("Remote scratch special file")
if __SNAPSHOT__:
 b=io.BytesIO()
 with tarfile.open(fileobj=b,mode="w:gz",dereference=False) as t:t.add(s,arcname=".scratch")
 sys.stdout.buffer.write(b.getvalue())
else:print(json.dumps({"identity":p.identity,"root":str(p.root)}))
'''.replace('__SOURCE__', repr(base64.b64encode(source).decode())).replace('__ROOT__', repr(remote_root)).replace('__SNAPSHOT__', repr(snapshot))
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
            remote = json.loads(_remote(connection, remote_root))
            if remote.get('identity') != project.identity or remote.get('root') != remote_root:
                raise ValueError('Remote repository identity/checkout does not match the selected local repository.')
        _write(config_path, candidate)
    return candidate


def _process_identity(pid):
    if not isinstance(pid, int) or pid <= 1:
        return None
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'args='],
                            capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else None


def _owned(runtime, state):
    identity = _process_identity(runtime.get('pid'))
    nonce = runtime.get('nonce')
    return bool(identity and isinstance(nonce, str) and len(nonce) == 48
                and '--supervise' in identity and '.scratch' in identity
                and str(Path(state)) in identity and nonce in identity
                and identity == runtime.get('process_identity'))


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
        raise ValueError('An interrupted scratch retarget is pending; run task work to finish its owned transition first')
    _config(root, state)
    _private_location(root, state)
    _scratch(root)
    current = status(state)
    if current.get('running') and current.get('checkpoint_version') != 1:
        stop(state)
    with locked(state / 'scratch-control.lock', blocking=True):
        current = status(state)
        if current['running'] and current.get('checkpoint_version') == 1:
            return current
        if current.get('phase') != 'stopped' and current.get('session'):
            _recover_session(state, current)
        # Installation failures happen synchronously and clearly to the caller.
        install_mutagen(state)
        nonce = secrets.token_hex(24)
        package = __package__
        package_parent = Path(__file__).resolve().parents[len(package.split('.'))]
        env = dict(os.environ)
        bootstrap = ('import sys,runpy; sys.path.insert(0,' + repr(str(package_parent))
                     + '); runpy.run_module(' + repr(package + '.scratch') + ',run_name="__main__")')
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', bootstrap, '--supervise',
                                  str(root), str(state), nonce], cwd=root, env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        _CHILDREN[child.pid] = child
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = status(state)
            if current.get('nonce') == nonce and current['running']:
                return current
            if child.poll() is not None:
                raise ValueError('Scratch supervisor exited during startup; inspect scratch.log.')
            time.sleep(.1)
        # We own this Popen handle, so startup timeout can safely terminate it.
        child.terminate()
        child.wait(timeout=5)
        raise ValueError('Scratch supervisor startup timed out.')


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


def _session_info(binary, state, receipt):
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
    info = _session_info(install_mutagen(state), state, receipt)
    return _reconciliation(info) if info else dict(phase='connecting', conflicts=[])


def _pause_saved_session(binary, state, receipt):
    info = _session_info(binary, state, receipt)
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
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if not status(state)['running']:
                child = _CHILDREN.pop(current['pid'], None)
                if child is not None:
                    child.wait(timeout=5)
                return status(state)
            time.sleep(.1)
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


def complete_retarget(state):
    """Finish a prior explicit owned transition before choosing another one."""
    state = Path(state)
    try:
        intent = read_json(state / 'scratch-retarget.json')
    except FileNotFoundError:
        return
    candidate = intent['candidate']
    return retarget(Path(candidate['root']), state, candidate['codespace'], candidate['remote_root'])


def retarget(root, state, codespace, remote_root):
    """Explicitly replace a verified owned scratch binding, retaining its receipts."""
    state = Path(state)
    project = discover_project(Path(root))
    _scratch(project.root)
    if (not Path(remote_root).is_absolute() or '..' in Path(remote_root).parts
            or any(c in remote_root for c in '\r\n\0')):
        raise ValueError('Invalid remote checkout path')
    candidate = dict(version=1, identity=project.identity, root=str(project.root),
                     codespace=codespace, remote_root=remote_root)
    intent_path = state / 'scratch-retarget.json'
    if not intent_path.exists() or status(state).get('running'):
        stop(state)
    with locked(state / 'scratch-control.lock', blocking=True):
        path = state / CONFIG
        if intent_path.exists():
            intent = read_json(intent_path)
            if intent.get('schema') != 1 or intent.get('candidate') != candidate:
                raise ValueError('An interrupted scratch retarget needs its original destination; previous data preserved')
            previous = intent['previous']
            actual = read_json(path)
            if actual not in (previous, candidate):
                raise ValueError('Scratch binding changed during interrupted retarget')
            receipt = intent.get('receipt')
            archive = Path(intent['archive'])
            if archive.parent != state / 'retired-bindings':
                raise ValueError('Invalid scratch retarget archive')
            saved = _saved_session(state)
            if saved is not None and saved != receipt:
                raise ValueError('Scratch receipt changed during interrupted retarget')
            if receipt:
                binary = install_mutagen(state)
                # The durable intent was written only after verifying the owned
                # baseline. Its exact receipt permits absence after termination.
                info = _session_info(binary, state, {**receipt, 'established': False})
                if info is not None:
                    _mutagen(binary, state, 'sync', 'terminate', info['identifier'])
                _write(archive / SESSION, receipt)
                (state / SESSION).unlink(missing_ok=True)
            _write(archive / CONFIG, previous)
            _write(path, candidate)
            intent_path.unlink()
            return candidate
        if not path.exists():
            _write(path, candidate)
            return candidate
        previous = read_json(path)
        if previous == candidate:
            return candidate
        if previous.get('identity') != project.identity:
            raise ValueError('Cannot retarget another project identity')
        receipt = _saved_session(state, previous)
        archive = state / 'retired-bindings' / (str(time.time_ns()) + '-' + secrets.token_hex(4))
        _write(archive / CONFIG, previous)
        if receipt:
            binary = install_mutagen(state)
            info = _session_info(binary, state, receipt)
            _write(intent_path, dict(schema=1, previous=previous, candidate=candidate,
                                    receipt=receipt, archive=str(archive)))
            if info is not None:
                _mutagen(binary, state, 'sync', 'terminate', info['identifier'])
            _write(archive / SESSION, receipt)
            (state / SESSION).unlink()
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
        os.chmod(log_path, 0o600)
        logger = logging.getLogger('project-sync-' + nonce)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        receipt = _saved_session(state, config)
        runtime = {'pid': os.getpid(), 'nonce': nonce, 'process_identity': _process_identity(os.getpid()),
                   'phase': 'starting', 'session': receipt['name'] if receipt else None,
                   'checkpoint_version': 1}
        def update(phase, **values):
            runtime.update(phase=phase, **values)
            _write(state / RUNTIME, runtime)
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
                        remote = json.loads(_remote(connection, config['remote_root']))
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
                        if info is None:
                            backup(root, state)
                            remote_snapshot = _remote(connection, config['remote_root'], snapshot=True)
                            remote_backup = _archive_path(state, 'remote')
                            atomic_write(remote_backup, remote_snapshot, mode=0o400)
                            ignores = ['/.gitignore', '/README.md', '.DS_Store', '__pycache__',
                                       '.venv', 'node_modules', '.cache', '.project-sync', '.handoff', '.mutagen']
                            args = ['sync', 'create', str(root / '.scratch'),
                                    'project-sync:' + config['remote_root'] + '/.scratch',
                                    '--name=' + session, '--mode=two-way-safe', '--hash=sha256',
                                    '--symlink-mode=ignore', '--no-global-configuration', '--ignore-vcs', '--paused']
                            args += ['--ignore=' + item for item in ignores]
                            # Record the owned name before create; partial creation
                            # can then be cleaned without touching another session.
                            update('creating', session=session)
                            _mutagen(binary, state, *args, timeout=120)
                            info = _session_info(binary, state, receipt)
                            if info is None:
                                raise ValueError('Mutagen create returned without a session; retrying safely.')
                        _mutagen(binary, state, 'sync', 'resume', info['identifier'])
                        update('syncing')
                        backoff = 2
                        logger.info('Scratch connection established.')
                        def checkpoint():
                            from .replication import checkpoint_remote
                            try:
                                saved = checkpoint_remote(connection, discover_project(root), state, config['remote_root'])
                                update('syncing', checkpoint=saved, checkpoint_error=None)
                            except Exception:
                                # Leave the last acknowledged checkpoint intact;
                                # scratch reconciliation does not imply code protection.
                                update('syncing', checkpoint_error='Checkpoint incomplete; previous recovery data preserved')
                        checkpoint()
                        while not exiting.wait(5):
                            if connection.child.poll() is not None:
                                raise ValueError('Codespace forward disconnected; waiting to reconnect.')
                            _validate_tree(root / '.scratch')
                            remote = json.loads(_remote(connection, config['remote_root']))
                            if remote.get('identity') != config['identity'] or remote.get('root') != config['remote_root']:
                                raise ValueError('Remote repository identity changed; synchronization paused.')
                            summary = _mutagen(binary, state, 'sync', 'list', session)
                            update('syncing', detail=summary)
                            checkpoint()
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
                    update('waiting', error=message)
                    logger.info('Scratch waiting: %s', message)
                    if exiting.wait(backoff):
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
