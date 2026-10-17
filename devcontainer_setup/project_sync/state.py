"""Repository identity and private, symlink-safe host-local state."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
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
    # A chosen fallback is sticky across later permission repairs, so native
    # ownership UUIDs and project registries never move back implicitly.
    try:
        fallback.lstat()
    except FileNotFoundError:
        pass
    else:
        return fallback
    # Codespaces commonly supplies group-writable .local/state parents. Leave
    # those user directories alone and choose a private default instead. Explicit
    # XDG overrides above remain authoritative and fail normal mutation guards.
    for candidate in (home / '.local', home / '.local/state'):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            break
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            return fallback
    return home / '.local/state'


def state_dir(project: Project, env: Mapping | None = None) -> Path:
    """Compute state location without creating it."""
    if not re.fullmatch(r'[0-9a-f]{24}', project.project_id):
        raise ValueError('Invalid project ID')
    return _state_home(env) / 'devcontainer-setup/projects' / project.project_id


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


def _regular(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
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
            _regular(source.fileno())
            result = json.load(source)
    if not isinstance(result, dict):
        raise ValueError('State JSON must contain an object')
    return result


@contextmanager
def locked(path: Path, blocking: bool = False):
    """Hold an advisory process lock; never unlink the shared lock inode."""
    with _parent(path, create=True, private=True) as (parent, name):
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
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


def host_id(env: Mapping | None = None) -> str:
    """Persist a private installation UUID, independent of hostname changes."""
    directory = _state_home(env) / 'devcontainer-setup'
    path = directory / 'host.json'
    with locked(directory / 'host.lock', blocking=True):
        try:
            value = read_json(path)['host_id']
        except FileNotFoundError:
            value = str(uuid.uuid4())
            atomic_write(path, json.dumps({'host_id': value}).encode() + b'\n')
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError('Invalid persisted host identity')
        return value
