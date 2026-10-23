"""Repository identity and private, symlink-safe host-local state."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid


@dataclass(frozen=True)
class Project:
    root: Path
    remote: str
    identity: str
    project_id: str


def _identity(remote: str) -> str:
    if not remote or any(ord(c) < 33 or ord(c) == 127 for c in remote):
        raise ValueError('Origin must not contain whitespace or control characters')
    if '://' not in remote:
        match = re.fullmatch(r'git@([A-Za-z0-9.-]+):(.+)', remote)
        if not match:
            raise ValueError('Origin must be an SSH or HTTPS repository URL')
        remote = f'ssh://git@{match[1]}/{match[2]}'
    try:
        parsed = urlsplit(remote)
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Malformed repository origin') from exc
    if (parsed.scheme not in ('ssh', 'https') or not parsed.hostname
            or parsed.password is not None or parsed.query or parsed.fragment
            or (parsed.username is not None and not (parsed.scheme == 'ssh' and parsed.username == 'git'))):
        raise ValueError('Origin must not contain credentials, query, or fragment')
    host = parsed.hostname.lower()
    if not re.fullmatch(r'[a-z0-9.-]+', host):
        raise ValueError('Invalid repository host')
    path = parsed.path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    pieces = path.split('/')
    if len(pieces) < 2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in pieces):
        raise ValueError('Origin must name an owner and repository without traversal')
    if host == 'github.com':
        if len(pieces) != 2:
            raise ValueError('GitHub origin must name exactly owner/repository')
        path = path.lower()
    if port is not None and port != {'https': 443, 'ssh': 22}[parsed.scheme]:
        host += f':{port}'
    return f'{host}/{path}'


def discover_project(path: Path) -> Project:
    """Resolve the current checkout, retaining linked worktree boundaries."""
    def git(*args):
        result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True)
        if result.returncode:
            raise ValueError('Project must be a Git checkout with an origin')
        return result.stdout.removesuffix('\n')

    root = Path(git('rev-parse', '--show-toplevel')).resolve()
    remote = git('remote', 'get-url', 'origin')
    identity = _identity(remote)
    return Project(root, remote, identity, hashlib.sha256(identity.encode()).hexdigest()[:24])


def _state_home(env: Mapping | None) -> Path:
    values = os.environ if env is None else env
    override = values.get('XDG_STATE_HOME', '')
    if override and Path(override).is_absolute() and '..' not in Path(override).parts:
        return Path(override)
    home = Path(values.get('HOME') or Path.home())
    if not home.is_absolute() or '..' in home.parts:
        raise ValueError('Home directory must be absolute without traversal')
    fallback = home / '.devcontainer-state'
    default = home / '.local/state'
    # Inspect without following .local symlinks: a hidden initialized namespace
    # cannot safely be distinguished from a fresh default through such a link.
    unsafe = False
    can_have_state = True
    for candidate in (home / '.local', default):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            can_have_state = False
            break
        if stat.S_ISLNK(info.st_mode):
            raise PermissionError('Cannot establish default state selection through a symlink; explicit repair or migration required')
        if not stat.S_ISDIR(info.st_mode):
            unsafe = True
            can_have_state = False
            break
        unsafe = unsafe or info.st_uid != os.geteuid() or bool(stat.S_IMODE(info.st_mode) & 0o022)
    def continuity_registry(namespace):
        # apply-dotfiles shares devcontainer-setup, so the directory itself is
        # not evidence of continuity ownership. Inspect only our entry names,
        # through no-follow directory descriptors and without changing anything.
        try:
            with _parent(namespace / 'host.json') as (parent, _):
                found = False
                for name in ('host.json', 'host.lock', 'projects'):
                    try:
                        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        raise PermissionError('Continuity registry contains a symlink; explicit repair or migration required')
                    found = True
                return found
        except FileNotFoundError:
            return False
        except OSError as error:
            raise PermissionError('Cannot safely inspect continuity registry; explicit repair or migration required') from error
    existing_default = can_have_state and continuity_registry(default / 'devcontainer-setup')
    existing_fallback = continuity_registry(fallback / 'devcontainer-setup')
    try:
        fallback.lstat()
    except FileNotFoundError:
        fallback_selected = False
    else:
        fallback_selected = True
    if existing_default and existing_fallback:
        raise ValueError('Both default and fallback continuity state exist; ambiguous identity requires explicit migration')
    if existing_default:
        if unsafe:
            raise PermissionError('Existing default continuity state has unsafe ancestors; explicit permission repair or migration required')
        return default
    # Once selected, a fallback remains canonical after a default permission
    # repair. Never silently fork an initialized default host/registry identity.
    if existing_fallback or fallback_selected or unsafe:
        return fallback
    return default


def state_dir(project: Project, env: Mapping | None = None) -> Path:
    """Compute state location without creating it."""
    if not re.fullmatch(r'[0-9a-f]{24}', project.project_id):
        raise ValueError('Invalid project ID')
    return _state_home(env) / 'devcontainer-setup/projects' / project.project_id


def setup_worktree_root(project: Project, env: Mapping | None = None) -> Path:
    """Where setup's temporary worktrees for this project go, beside its state; not created here."""
    if not re.fullmatch(r'[0-9a-f]{24}', project.project_id):
        raise ValueError('Invalid project ID')
    return _state_home(env) / 'devcontainer-setup/setup-worktrees' / project.project_id


@contextmanager
def _parent(path: Path, create: bool = False, private: bool = False):
    """Walk using directory descriptors so symlink swaps cannot redirect writes."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.name in ('', '.', '..'):
        raise ValueError('State path must be absolute without traversal')
    # Match the state layout, not an arbitrary project directory with the same
    # basename. Native transcript destinations and notes readers remain mixed
    # ownership; they must never chmod unrelated user directories.
    boundary = None
    if private:
        for index, piece in enumerate(path.parts[:-1]):
            if piece != 'devcontainer-setup':
                continue
            rest = path.parts[index + 1:]
            if ((len(rest) >= 3 and rest[0] == 'projects' and re.fullmatch(r'[0-9a-f]{24}', rest[1]))
                    or rest in (('host.json',), ('host.lock',))):
                boundary = index
                break
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for index, piece in enumerate(path.parts[1:-1], 1):
            if create:
                try:
                    os.mkdir(piece, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(piece, flags, dir_fd=fd)
            try:
                if boundary is not None:
                    info = os.fstat(next_fd)
                    mode = stat.S_IMODE(info.st_mode)
                    if index < boundary:
                        # An unsafe ancestor could rename the private namespace.
                        # Shared root/current-user sticky directories (/tmp) are
                        # safe parents; never change unmanaged ancestors.
                        if (info.st_uid not in (0, os.geteuid())
                                or (mode & 0o022 and not mode & stat.S_ISVTX)):
                            raise PermissionError('Unsafe state namespace ancestor')
                    else:
                        if info.st_uid != os.geteuid():
                            raise PermissionError('State namespace is owned by another user')
                        if mode != 0o700:
                            if not create:
                                raise PermissionError('State namespace is not private')
                            os.fchmod(next_fd, 0o700)
                    current = os.stat(piece, dir_fd=fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        raise PermissionError('State directory changed during validation')
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        yield fd, path.name
    finally:
        os.close(fd)


def _regular(fd: int, allow_unlinked: bool = False) -> None:
    """Only opened read snapshots may outlive their atomically replaced name."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (info.st_nlink != 1 and not (allow_unlinked and info.st_nlink == 0)):
        raise ValueError('State must be a regular file without hard links')


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace a regular file atomically; create private parent directories."""
    with _parent(path, create=True, private=True) as (parent, name):
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Refusing nonregular or linked state destination')
        temporary = f'.{name}.{uuid.uuid4().hex}.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                os.fchmod(output.fileno(), mode)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def read_json(path: Path) -> dict:
    with _parent(path, private=True) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as source:
            _regular(source.fileno(), allow_unlinked=True)
            result = json.load(source)
    if not isinstance(result, dict):
        raise ValueError('State JSON must contain an object')
    return result


@contextmanager
def locked(path: Path, blocking: bool = False):
    """Hold an advisory process lock; never unlink the shared lock inode."""
    with _parent(path, create=True, private=True) as (parent, name):
        for attempt in range(2):
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
                break
            except FileNotFoundError:
                # macOS (measured on 26.7) fails one of two simultaneous creates of a
                # new name with ENOENT; the other creator won, so open its file.
                if attempt:
                    raise
        try:
            _regular(fd)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield fd
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


_CHILDREN = {}
STARTUP_WAIT = 8
TERMINATE_WAIT = 5


def runtime_id() -> str:
    """The running producer: this package's code and tool pins, its location and interpreter."""
    from .tools import MANIFEST
    digest = hashlib.sha256()
    # The alias bootstrap embeds these inputs, so identical source bytes from
    # another checkout/interpreter are not the same running producer.
    for value in (str(Path(__file__).parent.resolve()), __package__, sys.executable):
        digest.update(value.encode() + b'\0')
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode() + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(hashlib.sha256(MANIFEST.read_bytes()).digest())
    return digest.hexdigest()


def launch_detached(name: str, flag: str, root: Path, state: Path, *, env: dict, status,
                    exited: str, timed_out: str) -> dict:
    """Start this package's `name` module as `flag ROOT STATE NONCE` in its own session.

    Returns status(state) once the runtime it writes names the new nonce and is owned.
    """
    nonce = secrets.token_hex(24)
    package = __package__
    package_parent = Path(__file__).resolve().parents[len(package.split('.'))]
    bootstrap = ('import sys,runpy; sys.path.insert(0,' + repr(str(package_parent))
                 + '); runpy.run_module(' + repr(package + '.' + name) + ',run_name="__main__")')
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', bootstrap, flag,
                              str(root), str(state), nonce], cwd=root, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    _CHILDREN[child.pid] = child
    deadline = time.monotonic() + STARTUP_WAIT
    while time.monotonic() < deadline:
        current = status(state)
        if current.get('nonce') == nonce and current['running']:
            return current
        if child.poll() is not None:
            raise ValueError(exited)
        time.sleep(.1)
    # We own this Popen handle, so startup timeout can safely terminate it,
    # and kill it if it ignores that, so it is always reaped.
    child.terminate()
    try:
        child.wait(timeout=TERMINATE_WAIT)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=TERMINATE_WAIT)
    raise ValueError(timed_out)


def wait_gone(runtime: dict, timeout: float) -> bool:
    """True once the process a runtime record names (same start time and argv) has exited.

    A child this process launched is reaped, so it leaves no zombie behind.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_identity(runtime.get('pid')) != runtime.get('process_identity'):
            child = _CHILDREN.pop(runtime.get('pid'), None)
            if child is not None:
                child.wait(timeout=5)
            return True
        time.sleep(.1)
    return False


def process_identity(pid):
    """Start time and argv of a live process, or None; it pins a pid against reuse."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'args='],
                            capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else None


def owned_process(runtime: dict, state: Path, *, flag: str, module: str) -> bool:
    """True only for the live process a runtime record launched: pid, start time, argv and nonce."""
    identity = process_identity(runtime.get('pid'))
    nonce = runtime.get('nonce')
    return bool(identity and isinstance(nonce, str) and len(nonce) == 48
                and flag in identity and module in identity
                and str(Path(state)) in identity and nonce in identity
                and identity == runtime.get('process_identity'))


def log_formatter() -> logging.Formatter:
    """UTC time and pid on every line, so logs from several processes merge by time."""
    formatter = logging.Formatter('%(asctime)sZ %(process)d %(message)s')
    formatter.converter = time.gmtime
    return formatter
