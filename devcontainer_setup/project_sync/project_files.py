"""Read-only Git/worktree capture and offline recovery into new checkouts."""
from __future__ import annotations

import hashlib
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import unicodedata

from .checkpoint_store import Store
from .state import Project, _identity, _parent, atomic_write, discover_project, locked, read_json


# These are transient/private scratch producer directories, never project notes.
_SCRATCH_EXCLUDES = {'.git', '__pycache__', '.venv', 'node_modules', '.cache',
                     '.project-sync', '.handoff', '.mutagen', '.DS_Store'}
_MODES = {'100644', '100755', '120000'}
_OID = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
_NATIVE_PRIVATE = ('.codex/auth.json', '.codex/history.jsonl', '.codex/state_*.sqlite*',
                   '.codex/threads_*.sqlite*', '.codex/sessions', '.codex/sessions/**',
                   '.codex/archived_sessions', '.codex/archived_sessions/**',
                   '.codex/memories', '.codex/memories/**', '.codex/log', '.codex/log/**',
                   '.claude/.credentials.json', '.claude/projects', '.claude/projects/**',
                   '.claude/tasks', '.claude/tasks/**', '.claude/todos', '.claude/todos/**')


def _native_homes(root):
    from .sessions import native_home
    homes = []
    for agent in ('codex', 'claude'):
        home = native_home(agent).resolve()
        if home.is_relative_to(root):
            homes.append(home)
    return homes


def _private_paths(root, names):
    """Fail before reading native credentials/profile bytes into any object."""
    homes = _native_homes(root)
    for name in names:
        if (any(fnmatch.fnmatchcase(name, pattern) for pattern in _NATIVE_PRIVATE)
                or any((root / name).is_relative_to(home) for home in homes)):
            raise ValueError('Native private profile/auth/database in project; move it outside the checkout before checkpointing')


def _git(root: Path, *args, data=None) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
    command = ['git', '--no-optional-locks', '-c', 'protocol.allow=never',
               '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
               '-c', 'gc.auto=0', '-C', str(root), *args]
    result = subprocess.run(command, input=data, capture_output=True, env=env)
    if result.returncode:
        raise ValueError('Git recovery operation failed: ' + ' '.join(args[:2]) + ': '
                         + result.stderr.decode(errors='replace').strip())
    return result.stdout


def _path(value: str) -> str:
    if (not isinstance(value, str) or not value or '\0' in value or '\\' in value
            or value.startswith('/') or any(p in ('', '.', '..') or p.casefold() == '.git'
                                           for p in value.split('/'))):
        raise ValueError('Unsafe project file path or path escape')
    return value


def _symlink(path: str, target: str) -> None:
    if not isinstance(target, str) or not target or target.startswith('/') or '\0' in target or '\\' in target:
        raise ValueError('Unsafe symlink target')
    pieces = list(PurePosixPath(path).parent.parts)
    for piece in target.split('/'):
        if piece == '..':
            if not pieces:
                raise ValueError('Symlink would escape project')
            pieces.pop()
        elif piece not in ('', '.'):
            if piece.casefold() == '.git':
                raise ValueError('Symlink points into Git metadata')
            pieces.append(piece)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _regular_bytes(path: Path) -> bytes:
    with _parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Unsupported special file in project')
            data = source.read()
            after = os.fstat(source.fileno())
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (_signature(before) != _signature(after) or _signature(after) != _signature(named)
            or len(data) != after.st_size):
        raise ValueError('Unstable project file read; retry checkpoint')
    return data


def _record(root: Path, name: str, store: Store) -> dict | None:
    name = _path(name)
    with _parent(root / name) as (parent, leaf):
        try:
            before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(before.st_mode):
            target = os.readlink(leaf, dir_fd=parent)
            _symlink(name, target)
            after = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if _signature(before) != _signature(after):
                raise ValueError('Unstable symlink read; retry checkpoint')
            return {'path': name, 'kind': 'symlink', 'mode': '120000', 'target': target}
        if stat.S_ISDIR(before.st_mode):
            return None  # A tracked file was replaced by an untracked directory.
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Unsupported special file or submodule in project')
    data = _regular_bytes(root / name)
    if _signature(before) != _signature((root / name).lstat()):
        raise ValueError('Unstable project file read; retry checkpoint')
    return {'path': name, 'kind': 'file',
            'mode': '100755' if before.st_mode & 0o111 else '100644', 'blob': store.put(data)}


def _portable_paths(names) -> None:
    seen = {}
    for name in names:
        components = _path(name).split('/')
        for length in range(1, len(components) + 1):
            path = '/'.join(components[:length])
            folded = unicodedata.normalize('NFC', path).casefold()
            if folded in seen and seen[folded] != path:
                raise ValueError('Recovery path case or Unicode collision')
            seen[folded] = path


def _absent(root: Path, name: str) -> bool:
    try:
        with _parent(root / name) as (parent, leaf):
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            return stat.S_ISDIR(info.st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return True


def _names(data: bytes) -> set[str]:
    return {_path(os.fsdecode(name)) for name in data.split(b'\0') if name}


def _inventory(root: Path, index: list[dict], head_paths: set[str]) -> set[str]:
    paths = {entry['path'] for entry in index} | head_paths
    paths |= _names(_git(root, 'ls-files', '--others', '--exclude-standard', '-z'))
    scratch = root / '.scratch'
    if scratch.is_symlink():
        paths.add('.scratch')
    elif scratch.exists():
        # No links are followed; every selected payload is opened with O_NOFOLLOW.
        for directory, dirs, files in os.walk(scratch, followlinks=False):
            dirs[:] = [name for name in dirs if name not in _SCRATCH_EXCLUDES]
            for name in dirs[:]:
                if (Path(directory) / name).is_symlink():
                    paths.add((Path(directory) / name).relative_to(root).as_posix())
                    dirs.remove(name)
            for name in files:
                if name not in _SCRATCH_EXCLUDES:
                    paths.add((Path(directory) / name).relative_to(root).as_posix())
    # Git omits special files (for example FIFOs) from ls-files altogether.
    # Inspect nonignored directories explicitly so that omission is a failure.
    ignored = {os.fsdecode(name).rstrip('/') for name in _git(
        root, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z').split(b'\0') if name}
    for directory, dirs, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        def keep(name):
            child = (relative / name).as_posix()
            in_scratch = child == '.scratch' or child.startswith('.scratch/')
            return (name.casefold() != '.git' and
                    (name not in _SCRATCH_EXCLUDES if in_scratch else child not in ignored))
        dirs[:] = [name for name in dirs if keep(name)]
        for name in files:
            if keep(name):
                info = (Path(directory) / name).lstat()
                if not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    raise ValueError('Unsupported special file in project')
    return paths


def _index(root: Path, store: Store) -> list[dict]:
    entries = []
    for record in _git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if not record:
            continue
        header, name = record.split(b'\t', 1)
        mode, oid, stage = header.decode('ascii').split(' ')
        _private_paths(root, [os.fsdecode(name)])
        if mode == '160000':
            raise ValueError('Unsupported submodules prevent complete recovery')
        if mode not in _MODES:
            raise ValueError('Unsupported Git index mode')
        data = _git(root, 'cat-file', 'blob', oid)
        entries.append({'path': _path(os.fsdecode(name)), 'mode': mode,
                        'oid': oid, 'stage': int(stage), 'blob': store.put(data)})
    return entries


def _bundle(project: Project, store: Store, head: str) -> dict:
    path = store.root / 'bundles' / (head + '.json')
    with locked(store.root / 'bundle.lock', blocking=True):
        try:
            reference = read_json(path)
        except FileNotFoundError:
            reference = {'blob': store.put(_git(project.root, 'bundle', 'create', '-', 'HEAD'))}
            atomic_write(path, json.dumps(reference, sort_keys=True).encode(), mode=0o400)
        store.read(reference['blob'])
        return reference


def capture_files(project: Project, store: Store) -> dict:
    """Capture one stable checkout; do not refresh or otherwise write its index."""
    root = project.root
    if discover_project(root) != project:
        raise ValueError('Project identity or root changed')
    if store.state.resolve().is_relative_to(root.resolve()):
        raise ValueError('Private recovery state must remain outside checkout')
    head = _git(root, 'rev-parse', '--verify', 'HEAD').decode().strip()
    branch_result = _git(root, 'rev-parse', '--abbrev-ref', 'HEAD').decode().strip()
    branch = None if branch_result == 'HEAD' else branch_result
    index_path = Path(os.fsdecode(_git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').rstrip(b'\n')))
    if _git(root, 'rev-parse', '--shared-index-path').strip():
        raise ValueError('Split index is unsupported for complete recovery')
    original_index = _regular_bytes(index_path)
    entries = _index(root, store)
    head_paths = _names(_git(root, 'ls-tree', '-rz', '--name-only', 'HEAD'))
    _private_paths(root, head_paths)
    # Refuse submodules even after a staged removal; the HEAD history is retained.
    if any(record.startswith(b'160000 ') for record in _git(root, 'ls-tree', '-rz', 'HEAD').split(b'\0')):
        raise ValueError('Unsupported submodules prevent complete recovery')
    names = _inventory(root, entries, head_paths)
    _private_paths(root, names)
    # A bundle includes reachable historical blobs too. Never hide a private
    # runtime from the tree while exporting its earlier committed bytes.
    pathspecs = [':(glob)' + pattern for pattern in _NATIVE_PRIVATE]
    pathspecs += [':(literal)' + home.relative_to(root).as_posix() for home in _native_homes(root)]
    if _git(root, 'rev-list', '--full-history', '--max-count=1', 'HEAD', '--', *pathspecs).strip():
        raise ValueError('Git history contains a native private profile; checkpoint refused')
    files, deleted = [], []
    for name in sorted(names):
        try:
            record = _record(root, name, store)
        except (FileNotFoundError, NotADirectoryError):
            record = None
        if record is None:
            deleted.append(name)
        else:
            files.append(record)
    _portable_paths([entry['path'] for entry in files] + [entry['path'] for entry in entries])
    bundle = _bundle(project, store, head)
    # Re-read content and inventories so active editors produce an explicit retry.
    if names != _inventory(root, entries, head_paths):
        raise ValueError('Unstable project file inventory; retry checkpoint')
    for recorded in files:
        if _record(root, recorded['path'], store) != recorded:
            raise ValueError('Unstable project content; retry checkpoint')
    for name in deleted:
        if not _absent(root, name):
            raise ValueError('Unstable deleted project file; retry checkpoint')
    if (_regular_bytes(index_path) != original_index
            or _git(root, 'rev-parse', '--verify', 'HEAD').decode().strip() != head
            or _git(root, 'rev-parse', '--abbrev-ref', 'HEAD').decode().strip() != branch_result
            or discover_project(root) != project):
        raise ValueError('Unstable Git state; retry checkpoint')
    return {'schema': 1, 'project': {'root': str(root), 'remote': project.remote,
                                   'identity': project.identity, 'project_id': project.project_id},
            'git': {'head': head, 'branch': branch, 'bundle': bundle,
                    'object_format': _git(root, 'rev-parse', '--show-object-format').decode().strip(),
                    'index': entries, 'index_blob': {'blob': store.put(original_index)}},
            'files': files, 'deleted': deleted}


def _validate(store: Store, part: dict):
    try:
        project, git, files, deleted = part['project'], part['git'], part['files'], part['deleted']
        if part['schema'] != 1 or _identity(project['remote']) != project['identity']:
            raise ValueError('Invalid recovery project identity')
        if hashlib.sha256(project['identity'].encode()).hexdigest()[:24] != project['project_id']:
            raise ValueError('Invalid recovery project ID')
        if not _OID.fullmatch(git['head']) or git['object_format'] not in ('sha1', 'sha256'):
            raise ValueError('Invalid recovery Git HEAD')
        if git['branch'] is not None:
            branch = git['branch']
            if (not isinstance(branch, str) or branch.startswith('-')
                    or _git(Path.cwd(), 'check-ref-format', 'refs/heads/' + branch) != b''):
                raise ValueError('Invalid recovery branch')
        contents = {}
        names = set()
        for entry in files:
            name = _path(entry['path'])
            if name in names:
                raise ValueError('Duplicate recovery file path')
            names.add(name)
            if entry['kind'] == 'symlink' and entry['mode'] == '120000':
                _symlink(name, entry['target'])
            elif entry['kind'] == 'file' and entry['mode'] in ('100644', '100755'):
                contents[name] = store.read(entry['blob'])
            else:
                raise ValueError('Unsupported recovery file mode/kind')
        _portable_paths(names)
        for name in names:
            if any(parent.as_posix() in names for parent in PurePosixPath(name).parents if parent.as_posix() != '.'):
                raise ValueError('Recovery file path traverses another file or symlink')
        for name in deleted:
            if _path(name) in names:
                raise ValueError('Deleted path also has file content')
        index_names = set()
        for entry in git['index']:
            name = _path(entry['path'])
            if (entry['mode'] not in _MODES or not _OID.fullmatch(entry['oid'])
                    or type(entry['stage']) is not int or entry['stage'] not in (0, 1, 2, 3)
                    or (name, entry['stage']) in index_names):
                raise ValueError('Invalid recovery index entry')
            index_names.add((name, entry['stage']))
            contents[('index', name, entry['stage'])] = store.read(entry['blob'])
        _portable_paths([entry['path'] for entry in git['index']] + list(names))
        bundle = store.read(git['bundle']['blob'])
        index_data = store.read(git['index_blob']['blob'])
        return project, git, files, contents, bundle, index_data
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('Invalid project recovery manifest') from error


def _sync_tree(root: Path) -> None:
    for directory, dirs, files in os.walk(root, followlinks=False, topdown=False):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def restore_files(store: Store, manifest: dict, destination: Path) -> Project:
    """Restore offline, claiming an absent directory atomically before writing."""
    destination = Path(destination)
    if not destination.is_absolute() or '..' in destination.parts:
        raise ValueError('Recovery destination must be absolute without traversal')
    project, git, files, contents, bundle, index_data = _validate(store, manifest)
    with _parent(destination) as (parent, name):
        # mkdir refuses existing paths, including empty directories and symlinks.
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    _git(destination, 'init', '--template=', '--object-format=' + git['object_format'], '-b', 'recovery')
    bundle_path = destination / '.git/recovery.bundle'
    atomic_write(bundle_path, bundle, mode=0o400)
    _git(destination, 'bundle', 'unbundle', str(bundle_path))
    bundle_path.unlink()
    if _git(destination, 'cat-file', '-t', git['head']).strip() != b'commit':
        raise ValueError('Recovery HEAD is not a commit')
    if git['branch'] is None:
        _git(destination, 'update-ref', '--no-deref', 'HEAD', git['head'])
    else:
        _git(destination, 'update-ref', 'refs/heads/' + git['branch'], git['head'])
        _git(destination, 'symbolic-ref', 'HEAD', 'refs/heads/' + git['branch'])
    _git(destination, 'remote', 'add', 'origin', project['remote'])
    for entry in git['index']:
        data = contents[('index', entry['path'], entry['stage'])]
        oid = _git(destination, 'hash-object', '-w', '--stdin', data=data).decode().strip()
        if oid != entry['oid']:
            raise ValueError('Staged blob Git object hash mismatch')
    atomic_write(destination / '.git/index', index_data)
    expected = b''.join((entry['mode'] + ' ' + entry['oid'] + ' ' + str(entry['stage']) + '\t').encode()
                        + os.fsencode(entry['path']) + b'\0' for entry in git['index'])
    if _git(destination, 'ls-files', '--stage', '-z') != expected:
        raise ValueError('Retained index entries differ from recovery manifest')
    for entry in files:
        path = destination / entry['path']
        if entry['kind'] == 'symlink':
            with _parent(path, create=True) as (parent, name):
                os.symlink(entry['target'], name, dir_fd=parent)
                os.fsync(parent)
        else:
            atomic_write(path, contents[entry['path']], mode=0o755 if entry['mode'] == '100755' else 0o644)
    _sync_tree(destination)
    return discover_project(destination)
