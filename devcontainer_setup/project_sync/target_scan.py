"""One bounded, read-only inventory; an incomplete answer never proves absence."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math
from pathlib import Path
import re
import time


@dataclass(frozen=True)
class Limits:
    entries: int = 200_000
    ignored_entries: int = 200_000
    repositories: int = 64
    worktrees: int = 256
    files: int = 100_000
    hashed_bytes: int = 4 * 1024**3
    seconds: float = 120
    output: int = 16 * 1024 * 1024


LIMITS = Limits()


@dataclass(frozen=True)
class Worktree:
    path: str
    head: str | None
    branch: str | None
    changes: int
    changed: tuple[str, ...]
    ok: bool


@dataclass(frozen=True)
class Repository:
    path: str
    identity: str | None
    branches: tuple[tuple[str, str], ...]
    stashes: int
    worktrees: tuple[Worktree, ...]
    bare: bool
    ok: bool


@dataclass(frozen=True)
class Scan:
    codespace: str
    at: float
    repositories: tuple[Repository, ...]
    git_complete: bool
    scratch: tuple[tuple[str, str], ...]
    scratch_complete: bool
    sessions: tuple[tuple[str, str], ...]
    sessions_complete: bool
    cutoffs_sha256: str | None
    checked: tuple[tuple[str, str], ...]
    mismatched: tuple[tuple[str, str], ...]
    tails: tuple[tuple[str, str], ...]
    agents: tuple[tuple[str, int], ...]
    agents_complete: bool
    problems: tuple[str, ...]


@dataclass(frozen=True)
class Cutoff:
    size: int
    sha256: str
    own: bool


def cutoffs_digest(cutoffs: Mapping[tuple[str, str], Cutoff]) -> str:
    """Bind the entire comparison map, including missing and dependency files."""
    record = {agent + '/' + name: [value.size, value.sha256, value.own]
              for (agent, name), value in cutoffs.items()}
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


_AGENTS = ('claude', 'codex', 'grok', 'opencode', 'herdr')
_PROBLEMS = ('could not connect to the Codespace', 'could not read the Codespace inventory',
             'could not read every repository under /workspaces', 'could not read .scratch',
             'could not read conversations', 'could not read processes')


def _text(value):
    if (not isinstance(value, str) or not value or
            any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError('Invalid inventory text')
    return value


def _path(value, *, absolute=False):
    value = _text(value)
    path = Path(value)
    if '..' in path.parts or (absolute and not path.is_absolute()) or (not absolute and path.is_absolute()):
        raise ValueError('Invalid inventory path')
    return value


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('Invalid inventory count')
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError('Invalid inventory completeness')
    return value


def _hex(value, length):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is None:
        raise ValueError('Invalid inventory digest')
    return value


def _object(value, cls):
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
        raise ValueError('Invalid inventory fields')
    return value


def _list(value):
    if not isinstance(value, list):
        raise ValueError('Invalid inventory list')
    return value


def _pairs(value, first, second):
    result = []
    for pair in _list(value):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError('Invalid inventory pair')
        result.append((first(pair[0]), second(pair[1])))
    if result != sorted(result) or len({key for key, _ in result}) != len(result):
        raise ValueError('Unordered or duplicate inventory entries')
    return tuple(result)


def _decode(raw, codespace, at, cutoffs, cutoffs_sha256, limits):
    if not isinstance(raw, (bytes, str)) or len(raw if isinstance(raw, bytes) else raw.encode()) > limits.output:
        raise ValueError('Inventory output limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate inventory key')
            result[key] = value
        return result
    obj = _object(json.loads(raw, object_pairs_hook=unique), Scan)
    if obj['codespace'] != codespace or type(obj['at']) not in (float, int) or not math.isfinite(obj['at']):
        raise ValueError('Invalid inventory target or time')
    compared = None if obj['cutoffs_sha256'] is None else _hex(obj['cutoffs_sha256'], 64)
    if compared != cutoffs_sha256:
        raise ValueError('Invalid inventory cutoff digest')
    repositories = []
    worktree_count = 0
    from .state import _identity
    for row in _list(obj['repositories']):
        row = _object(row, Repository)
        path = _path(row['path'], absolute=True)
        identity = row['identity']
        if identity is not None:
            _text(identity)
            if identity not in (_identity('https://' + identity), _identity('ssh://git@' + identity)):
                raise ValueError('Invalid inventory identity')
        branches = _pairs(row['branches'], _text, lambda value: _hex(value, 40))
        trees = []
        for tree in _list(row['worktrees']):
            tree = _object(tree, Worktree)
            changed = tuple(_path(value) for value in _list(tree['changed']))
            count = _integer(tree['changes'])
            if len(changed) > 20 or len(changed) > count:
                raise ValueError('Invalid changed path sample')
            trees.append(Worktree(_path(tree['path'], absolute=True),
                                  None if tree['head'] is None else _hex(tree['head'], 40),
                                  None if tree['branch'] is None else _text(tree['branch']),
                                  count, changed, _boolean(tree['ok'])))
        tree_paths = [tree.path for tree in trees]
        if tree_paths != sorted(set(tree_paths)):
            raise ValueError('Unordered worktrees')
        worktree_count += len(trees)
        repositories.append(Repository(path, identity, branches, _integer(row['stashes']), tuple(trees),
                                       _boolean(row['bare']), _boolean(row['ok'])))
    paths = [row.path for row in repositories]
    if paths != sorted(set(paths)) or len(paths) > limits.repositories or worktree_count > limits.worktrees:
        raise ValueError('Invalid repository inventory size')
    scratch = _pairs(obj['scratch'], _path, lambda value: _hex(value, 64))
    def session_path(value):
        value = _path(value)
        if not value.startswith(('codex/sessions/', 'codex/archived_sessions/', 'claude/')):
            raise ValueError('Invalid conversation path')
        return value
    sessions = _pairs(obj['sessions'], session_path, lambda value: _hex(value, 64))
    if len(scratch) + len(sessions) > limits.files:
        raise ValueError('Invalid file inventory size')
    # Marker keys are (agent, basename), so multiple names per agent are valid.
    def marker_rows(key):
        result = []
        for pair in _list(obj[key]):
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError('Invalid cutoff pair')
            agent, name = _text(pair[0]), _path(pair[1])
            if agent not in ('codex', 'claude') or '/' in name or (agent, name) not in cutoffs:
                raise ValueError('Invalid conversation cutoff')
            result.append((agent, name))
        if result != sorted(set(result)):
            raise ValueError('Unordered cutoff entries')
        return tuple(result)
    checked, mismatched, tails = (marker_rows(key) for key in ('checked', 'mismatched', 'tails'))
    found = {(key.split('/', 1)[0], key.rsplit('/', 1)[-1]) for key, _ in sessions}
    if (not set(checked) <= found or not set(mismatched) <= set(checked) or not set(tails) <= set(checked)
            or any(not cutoffs[key].own for key in tails)):
        raise ValueError('Inconsistent conversation cutoffs')
    agents = _pairs(obj['agents'], _text, _integer)
    if {name for name, _ in agents} != set(_AGENTS):
        raise ValueError('Invalid agent names')
    problems = tuple(_text(value) for value in _list(obj['problems']))
    allowed = set(_PROBLEMS) | {row.path + ': its origin cannot be read' for row in repositories}
    if any(value not in allowed for value in problems) or len(set(problems)) != len(problems):
        raise ValueError('Invalid inventory problem')
    return Scan(codespace, at, tuple(repositories), _boolean(obj['git_complete']), scratch,
                _boolean(obj['scratch_complete']), sessions, _boolean(obj['sessions_complete']), compared, checked,
                mismatched, tails, agents, _boolean(obj['agents_complete']), problems)


def _failed(codespace, at, problem):
    return Scan(codespace, at, (), False, (), False, (), False, None, (), (), (),
                tuple(sorted((name, 0) for name in _AGENTS)), False, (problem,))


def scan(connection, codespace: str, remote_root: str, *,
         cutoffs: Mapping[tuple[str, str], Cutoff] | None = None, limits: Limits = LIMITS,
         workspaces: str = '/workspaces', proc: str = '/proc', now=time.time) -> Scan:
    """Transport exactly one program; only validated, allowlisted evidence returns."""
    at = now()
    try:
        _text(codespace)
        for path in (remote_root, workspaces, proc):
            _path(path, absolute=True)
        for name in ('entries', 'ignored_entries', 'repositories', 'worktrees', 'files', 'hashed_bytes', 'output'):
            _integer(getattr(limits, name))
        if type(limits.seconds) not in (float, int) or not math.isfinite(limits.seconds) or limits.seconds < 0:
            raise ValueError('Invalid time limit')
        supplied_cutoffs = cutoffs is not None
        cutoffs = {} if cutoffs is None else dict(cutoffs)
        for (agent, name), cutoff in cutoffs.items():
            if agent not in ('codex', 'claude') or '/' in _path(name):
                raise ValueError('Invalid cutoff key')
            _integer(cutoff.size); _hex(cutoff.sha256, 64); _boolean(cutoff.own)
        cutoffs_sha256 = cutoffs_digest(cutoffs) if supplied_cutoffs else None
        base = Path(__file__).parent
        payload = dict(codespace=codespace, remote_root=remote_root, workspaces=workspaces, proc=proc,
                       at=at, limits=asdict(limits),
                       cutoffs=([[agent, name, asdict(value)] for (agent, name), value in sorted(cutoffs.items())]
                                if supplied_cutoffs else None),
                       sources={name: (base / (name + '.py')).read_text() for name in ('state', 'scratch_tree', 'sessions')})
        raw = connection.python(_PROGRAM, payload=json.dumps(payload).encode(), timeout=limits.seconds + 30)
    except (ValueError, OSError, TypeError, AttributeError):
        return _failed(codespace, now(), _PROBLEMS[0])
    try:
        decoded = _decode(raw, codespace, at, cutoffs, cutoffs_sha256, limits)
        return replace(decoded, at=now())
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        return _failed(codespace, now(), _PROBLEMS[1])


def fingerprint(scan: Scan) -> str | None:
    if not all((scan.git_complete, scan.scratch_complete, scan.sessions_complete, scan.agents_complete)):
        return None
    record = asdict(scan)
    evidence = {name: record[name] for name in ('repositories', 'scratch', 'sessions', 'agents')}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


# Kept self-contained: the remote interpreter uses no installed project package,
# no temporary files, and -B at the transport boundary prevents bytecode writes.
_PROGRAM = r'''
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
import types

request = json.load(sys.stdin)
package = types.ModuleType('_psync')
package.__path__ = []
sys.modules['_psync'] = package
for name in ('state', 'scratch_tree', 'sessions'):
    module = types.ModuleType('_psync.' + name)
    module.__package__ = '_psync'
    sys.modules[module.__name__] = module
    exec(compile(request['sources'][name], '<' + module.__name__ + '>', 'exec'), module.__dict__)
    setattr(package, name, module)
state, scratch_tree, sessions = package.state, package.scratch_tree, package.sessions
limits = request['limits']
deadline = time.monotonic() + limits['seconds']
usage = dict(entries=0, ignored_entries=0, repositories=0, worktrees=0, files=0, hashed_bytes=0)
cutoffs = {(agent, name): value for agent, name, value in (request['cutoffs'] or [])}
cutoff_record = {agent + '/' + name: [value['size'], value['sha256'], value['own']]
                 for (agent, name), value in cutoffs.items()}
cutoffs_sha256 = (None if request['cutoffs'] is None else
                  hashlib.sha256(json.dumps(cutoff_record, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
result = dict(codespace=request['codespace'], at=request['at'], repositories=[], git_complete=True,
              scratch=[], scratch_complete=True, sessions=[], sessions_complete=True,
              cutoffs_sha256=cutoffs_sha256, checked=[], mismatched=[], tails=[], agents=[], agents_complete=True, problems=[])

class Incomplete(Exception):
    pass


class Budget(Incomplete):
    pass


def clock():
    if time.monotonic() >= deadline:
        raise Budget()


def spend(key, count=1):
    clock()
    if usage[key] + count > limits[key]:
        raise Budget()
    usage[key] += count


def text(value):
    if not value or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise Incomplete()
    return value


def path_text(value):
    value = text(str(value))
    if '..' in Path(value).parts:
        raise Incomplete()
    return value


def problem(part):
    result[part + '_complete'] = False
    message = {'git': 'could not read every repository under /workspaces',
               'scratch': 'could not read .scratch', 'sessions': 'could not read conversations',
               'agents': 'could not read processes'}[part]
    if message not in result['problems']:
        result['problems'].append(message)


def safe_directory(path):
    # Refuse links in ancestors too, including a linked native home.
    for parent in [*reversed(path.parents), path]:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise Incomplete()


def git(path, *args, absent=()):
    clock()
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1', LC_ALL='C')
    # Disable optional filesystem writers in Git, including configured fsmonitor
    # hooks and untracked-cache refreshes; none of these queries needs a hook.
    command = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', '-c', 'protocol.allow=never', '-C', str(path), *args]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    end = min(deadline, time.monotonic() + 15)
    chunks, size = [], 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise Incomplete()
                events = selector.select(remaining)
                if not events:
                    raise Incomplete()
                data = os.read(child.stdout.fileno(), 65536)
                if not data:
                    selector.unregister(child.stdout)
                    break
                size += len(data)
                if size > limits['output']:
                    raise Budget()
                chunks.append(data)
        child.wait(timeout=max(0.001, end - time.monotonic()))
        if child.returncode and child.returncode not in absent:
            raise Incomplete()
        return b''.join(chunks) if child.returncode == 0 else None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()


def scalar(data):
    return text(data.decode('utf-8').removesuffix('\n'))


def oid(value):
    if not re.fullmatch('[0-9a-f]{40}', value):
        raise Incomplete()
    return value


def worktree(path):
    row = dict(path=path_text(path), head=None, branch=None, changes=0, changed=[], ok=True)
    try:
        safe_directory(Path(path))
        # Status can invoke content-conversion helpers. Read names only and
        # disable both conversion commands, including mandatory drivers.
        keys = git(path, 'config', '-z', '--name-only', '--get-regexp',
                   r'^filter\..*\.(clean|process)$', absent=(1,))
        drivers = set()
        for key in (keys or b'').split(b'\0'):
            if key:
                driver = text(key.decode('utf-8')[len('filter.'):].rsplit('.', 1)[0])
                if '=' in driver:
                    raise Incomplete()
                drivers.add(driver)
        filters = []
        for driver in sorted(drivers):
            for setting in ('clean=', 'process=', 'required=false'):
                filters.extend(('-c', 'filter.' + driver + '.' + setting))
        # Each nested repository is checked separately; parent status must not
        # recurse into a child's configured filters. Gitlink HEAD changes remain.
        data = git(path, *filters, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                   '--ignore-submodules=dirty').split(b'\0')
        index = 0
        changed = []
        while index < len(data) and data[index]:
            record = data[index]
            if len(record) < 4 or record[2:3] != b' ':
                raise Incomplete()
            changed.append(path_text(record[3:].decode('utf-8')))
            index += 2 if b'R' in record[:2] or b'C' in record[:2] else 1
        changed = set(changed)
        for record in git(path, 'ls-files', '-v', '--stage', '-z').split(b'\0'):
            if not record or not (record[:1] == b'S' or record[:1].islower()):
                continue
            metadata, name = record.split(b'\t', 1)
            indexed = oid(metadata.decode().split()[2])
            name = path_text(name.decode('utf-8'))
            target = Path(path) / name
            try:
                info = target.lstat()
            except FileNotFoundError:
                continue  # Sparse checkouts intentionally leave these files absent.
            if stat.S_ISDIR(info.st_mode):
                continue  # Gitlinks are checked as repositories, not file content.
            if not stat.S_ISREG(info.st_mode):
                raise Incomplete()
            safe_directory(target.parent)
            actual = oid(scalar(git(path, *filters, 'hash-object', '--no-filters', '--', name)))
            if actual != indexed:
                changed.add(name)
        git_dir = Path(scalar(git(path, 'rev-parse', '--path-format=absolute', '--git-dir')))
        safe_directory(git_dir)
        operations = {'rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD'}
        with os.scandir(git_dir) as markers:
            in_progress = any(item.name in operations or item.name.endswith('AUTOSTASH') for item in markers)
        row['changes'], row['changed'] = len(changed) + int(in_progress), sorted(changed)[:20]
        branch = git(path, 'symbolic-ref', '-q', '--short', 'HEAD', absent=(1,))
        row['branch'] = None if branch is None else scalar(branch)
        head = git(path, 'rev-parse', '-q', '--verify', 'HEAD', absent=(1,))
        row['head'] = None if head is None else oid(scalar(head))
        if head is None and branch is None:
            raise Incomplete()
    except Budget:
        row['ok'] = False
        problem('git')
    except (OSError, ValueError, Incomplete, subprocess.SubprocessError):
        row['ok'] = False
        if time.monotonic() >= deadline:
            problem('git')
    return row


def repository(path, candidates):
    row = dict(path=path_text(path), identity=None, branches=[], stashes=0, worktrees=[], bare=False, ok=True)
    try:
        row['bare'] = scalar(git(path, 'rev-parse', '--is-bare-repository')) == 'true'
        branches = []
        for line in git(path, 'for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/heads').decode().splitlines():
            name, head = line.split(' ', 1)
            branches.append([text(name), oid(head)])
        row['branches'] = sorted(branches)
        stash = git(path, 'rev-parse', '-q', '--verify', 'refs/stash', absent=(1,))
        if stash is not None:
            count = scalar(git(path, 'rev-list', '--walk-reflogs', '--count', 'refs/stash'))
            if not count.isdecimal():
                raise Incomplete()
            row['stashes'] = int(count)
        origin = git(path, 'config', '--get', 'remote.origin.url', absent=(1,))
        if origin is not None:
            try:
                row['identity'] = state._identity(origin.decode().removesuffix('\n'))
            except (ValueError, UnicodeError):
                row['ok'] = False
                result['problems'].append(row['path'] + ': its origin cannot be read')
        listing = git(path, 'worktree', 'list', '--porcelain', '-z').split(b'\0')
        paths = set(candidates if not row['bare'] else ())
        current = listed_head = None
        bare = prunable = False
        for field in listing + [b'']:
            if field.startswith(b'worktree '):
                current, bare, prunable = path_text(field[9:].decode()), False, False
            elif field.startswith(b'HEAD '):
                listed_head = oid(field[5:].decode())
            elif field == b'bare':
                bare = True
            elif field == b'prunable' or field.startswith(b'prunable '):
                prunable = True
            elif not field:
                if current is not None and not bare:
                    missing = False
                    try:
                        Path(current).lstat()
                    except FileNotFoundError:
                        missing = True
                    if prunable and missing:
                        # The directory is gone, but a detached commit can still
                        # exist only in this worktree's HEAD in the common store.
                        if listed_head is not None and listed_head not in {head for _, head in branches}:
                            spend('worktrees')
                            row['worktrees'].append(dict(path=current, head=listed_head, branch=None,
                                                         changes=0, changed=[], ok=True))
                    else:
                        paths.add(current)
                current, listed_head, bare, prunable = None, None, False, False
        for target in sorted(paths):
            spend('worktrees')
            row['worktrees'].append(worktree(target))
        row['worktrees'].sort(key=lambda tree: tree['path'])
        row['ok'] = row['ok'] and all(tree['ok'] for tree in row['worktrees'])
    except Budget:
        row['ok'] = False
        problem('git')
    except (OSError, ValueError, Incomplete, subprocess.SubprocessError):
        row['ok'] = False
        if time.monotonic() >= deadline:
            problem('git')
    return row


def git_inventory():
    groups = {}
    ignored = set()
    stack = [(Path(request['workspaces']), False)]
    try:
        safe_directory(stack[0][0])
        while stack:
            clock()
            directory, ignored_only = stack.pop()
            safe_directory(directory)
            children = []
            with os.scandir(directory) as entries:
                for child in entries:
                    spend('ignored_entries' if ignored_only else 'entries')
                    children.append(child)
            by_name = {child.name: child for child in children}
            marker = by_name.get('.git')
            bare = ('HEAD' in by_name and by_name['HEAD'].is_file(follow_symlinks=False)
                    and all(name in by_name and by_name[name].is_dir(follow_symlinks=False) for name in ('objects', 'refs')))
            if marker is not None and not (marker.is_dir(follow_symlinks=False) or marker.is_file(follow_symlinks=False)):
                problem('git')
                continue
            candidate = marker is not None or bare
            if candidate:
                ignored_only = False  # A discovered repository owns its own ignore rules.
                try:
                    common = scalar(git(directory, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
                    if not Path(common).is_absolute():
                        raise Incomplete()
                    if common not in groups:
                        spend('repositories'); groups[common] = []
                    groups[common].append(path_text(directory))
                    if not bare:
                        data = git(directory, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', '--directory')
                        ignored.update(str(directory / value.decode().rstrip('/')) for value in data.split(b'\0') if value.endswith(b'/'))
                except (OSError, ValueError, Incomplete, subprocess.SubprocessError):
                    problem('git')
                if bare:
                    continue
            for child in sorted(children, key=lambda child: child.name, reverse=True):
                hidden = ignored_only or str(Path(child.path)) in ignored
                if child.name == '.git' or (hidden and child.name in ('node_modules', '.venv', '__pycache__')):
                    continue
                if child.is_dir(follow_symlinks=False):
                    stack.append((Path(child.path), hidden))
    except (OSError, ValueError, Incomplete):
        problem('git')
    for common, candidates in groups.items():
        # Prefer the main checkout when present, independent of traversal order.
        main = str(Path(common).parent) if Path(common).name == '.git' else None
        path = main if main in candidates else min(candidates)
        result['repositories'].append(repository(path, candidates))
    result['repositories'].sort(key=lambda row: row['path'])


def claude_tail(data):
    if not data:
        return False
    if not data.endswith(b'\n'):
        return True
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            raise Incomplete()
        if not isinstance(row, dict):
            raise Incomplete()
        if row.get('type') == 'assistant':
            return True
        if row.get('type') == 'user' and row.get('isMeta') is not True:
            message = row.get('message', {})
            content = message.get('content', '') if isinstance(message, dict) else ''
            if isinstance(content, list):
                if not all(isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str)
                           for item in content):
                    return True
                content = ''.join(item['text'] for item in content)
            if not isinstance(content, str) or not content.startswith(('<command-message>', '<command-name>', '<local-command-')):
                return True
    return False


def hashed(path, cutoff=None, agent=None):
    spend('files')
    digest, prefix = hashlib.sha256(), hashlib.sha256()
    remaining = cutoff['size'] if cutoff else 0
    tail = bytearray()
    tail_present = tail_overflow = False
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise Incomplete()
        while True:
            clock()
            data = stream.read(min(1024 * 1024, max(1, limits['hashed_bytes'] - usage['hashed_bytes'] + 1)))
            if not data:
                break
            spend('hashed_bytes', len(data))
            digest.update(data)
            take = min(remaining, len(data))
            if take:
                prefix.update(data[:take]); remaining -= take
            if cutoff and cutoff['own'] and len(data) > take:
                tail_present = True
                if agent == 'claude':
                    room = limits['output'] - len(tail)
                    tail_overflow = tail_overflow or len(data) - take > room
                    tail.extend(data[take:take + room])
        after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise Incomplete()
    mismatch = cutoff is not None and (remaining != 0 or prefix.hexdigest() != cutoff['sha256'])
    unfinished = tail_present and (agent == 'codex' or tail_overflow or claude_tail(tail))
    return digest.hexdigest(), mismatch, unfinished


def scratch_inventory():
    root = Path(request['remote_root']) / '.scratch'
    try:
        try:
            root.lstat()
        except FileNotFoundError:
            return
        safe_directory(root)
        for item, relative in scratch_tree.entries(root):
            clock()
            if item.is_file():
                digest, _, _ = hashed(item)
                result['scratch'].append([path_text(relative), digest])
    except (OSError, ValueError, RecursionError, Incomplete):
        problem('scratch')
    result['scratch'].sort()


def session_inventory():
    seen = set()
    for agent, suffix in (('codex', 'sessions'), ('codex', 'archived_sessions'), ('claude', 'projects')):
        try:
            root = sessions.native_home(agent) / suffix
            try:
                root.lstat()
            except FileNotFoundError:
                continue
            safe_directory(root)
            stack = [root]
            while stack:
                clock()
                directory = stack.pop()
                safe_directory(directory)
                with os.scandir(directory) as children:
                    for child in children:
                        clock()
                        mode = child.stat(follow_symlinks=False).st_mode
                        if stat.S_ISDIR(mode):
                            stack.append(Path(child.path))
                        elif stat.S_ISREG(mode):
                            key = (agent, child.name)
                            if key in seen and key in cutoffs:
                                problem('sessions')
                            seen.add(key)
                            cutoff = cutoffs.get(key)
                            digest, mismatch, unfinished = hashed(child.path, cutoff, agent)
                            relative = Path(child.path).relative_to(root).as_posix()
                            name = agent + '/' + (suffix + '/' if agent == 'codex' else '') + relative
                            result['sessions'].append([path_text(name), digest])
                            if cutoff is not None:
                                result['checked'].append(list(key))
                                if mismatch:
                                    result['mismatched'].append(list(key))
                                if unfinished:
                                    result['tails'].append(list(key))
                        else:
                            problem('sessions')
        except (OSError, ValueError, Incomplete):
            problem('sessions')
    for key in ('sessions', 'checked', 'mismatched', 'tails'):
        result[key] = sorted({tuple(row) for row in result[key]})


def process_inventory():
    names = ('claude', 'codex', 'grok', 'opencode', 'herdr')
    counts = {name: 0 for name in names}
    root = Path(request['proc'])
    excluded = {os.getpid()}
    parent = os.getppid()
    try:
        safe_directory(root)
        while parent and parent not in excluded:
            clock(); excluded.add(parent)
            try:
                safe_directory(root / str(parent))
                fd = os.open(root / str(parent) / 'stat', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as source:
                    data = source.read(limits['output'] + 1)
                if len(data) > limits['output']:
                    raise Incomplete()
                data = data.decode('utf-8')
            except (FileNotFoundError, ProcessLookupError):
                break
            # Linux comm may contain spaces and closing parentheses.
            parent = int(data.rsplit(')', 1)[1].split()[1])
        with os.scandir(root) as processes:
            for process in processes:
                clock()
                if not process.name.isdecimal() or int(process.name) in excluded:
                    continue
                try:
                    if process.is_symlink():
                        raise Incomplete()
                    fd = os.open(root / process.name / 'cmdline', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'rb') as source:
                        command = source.read(limits['output'] + 1)
                    if len(command) > limits['output']:
                        raise Incomplete()
                    found = {value.rsplit(b'/', 1)[-1] for value in command.split(b'\0')}
                    for name in names:
                        counts[name] += name.encode() in found
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except (OSError, ValueError, Incomplete):
                    problem('agents')
    except (OSError, ValueError, IndexError, Incomplete):
        problem('agents')
    result['agents'] = sorted(counts.items())

for inventory, part in ((git_inventory, 'git'), (scratch_inventory, 'scratch'),
                        (session_inventory, 'sessions'), (process_inventory, 'agents')):
    try:
        clock()
        inventory()
    except (OSError, ValueError, Incomplete, subprocess.SubprocessError):
        problem(part)
# Preserve the fixed shape even if the global time allowance expired first.
if not result['agents']:
    result['agents'] = sorted((name, 0) for name in ('claude', 'codex', 'grok', 'opencode', 'herdr'))
output = json.dumps(result, sort_keys=True, separators=(',', ':')).encode()
if len(output) > limits['output']:
    # The host rejects this fixed malformed response as incomplete in all parts.
    output = b'{}'
sys.stdout.buffer.write(output)
'''
