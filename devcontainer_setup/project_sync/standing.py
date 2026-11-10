"""Read local project facts without creating state, taking locks or contacting a service."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

from . import keeper, replacing, scratch, state as project_state, sync_cycle


@dataclass(frozen=True)
class Checkout:
    default: str = 'main'
    branch: str | None = None
    relation: str = 'unknown'
    behind: int = 0
    ahead: int = 0
    copied_at: float | None = None


@dataclass(frozen=True)
class Launch:
    codespace: str
    complete: bool


@dataclass(frozen=True)
class Refusal:
    agent: str
    session_id: str
    reason: str


@dataclass(frozen=True)
class Local:
    root: Path
    state: Path | None = None
    repository: str = ''
    generated: bool = False
    in_codespace: bool = False
    setup: str = 'source'
    source_ref: str | None = None
    taskfile: str | None = None
    managed: frozenset[str] | None = None
    pairing: str | None = None
    armed: bool | None = False
    gh_user: str | None = None
    owner_stop_at: float | None = None
    hold_until: float | None = None
    started_at: float | None = None
    last_up_at: float | None = None
    keeper: str = 'stopped'
    keeper_heartbeat_age: float | None = None
    reading: tuple[str, float] | None = None
    deleted_at: float | None = None
    down_kind: str | None = None
    down_at: float | None = None
    worker: str = 'stopped'
    cycle_at: float | None = None
    refusals: tuple[Refusal, ...] = ()
    retired: tuple[str, ...] = ()
    cleanup: tuple[str, ...] = ()
    replacing: dict | None = None
    launch: Launch | None = None
    checkout: Checkout = Checkout()
    problems: tuple[str, ...] = ()


_TASKFILES = ('Taskfile.yml', 'Taskfile.yaml', 'taskfile.yml', 'taskfile.yaml',
              'Taskfile.dist.yml', 'Taskfile.dist.yaml', 'taskfile.dist.yml', 'taskfile.dist.yaml')
_LABEL = 'devcontainer-setup:work-tasks'
_BLOCK = re.compile(r'^( +)# ' + _LABEL + r' begin sha256=([0-9a-f]{64})\n(.*?)^\1# '
                    + _LABEL + r' end\n', re.M | re.S)
_KEY = re.compile(r'''^( *)("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s'"{}\[\]#][^\n]*?):(?:[ \t]+(.*)|[ \t]*)$''')
_REPOSITORY = re.compile(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?')
_NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9-]{0,99}')


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _object(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value if isinstance(value, str) and value else None


class _Reader:
    def __init__(self):
        self.problems = []

    def problem(self, message):
        if message not in self.problems:
            self.problems.append(message)

    def json(self, path, *, private=False, missing=None):
        try:
            value = project_state.read_json(path) if private else json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError('Expected an object')
            return value
        except FileNotFoundError:
            return {} if missing is None else missing
        except (OSError, ValueError):
            self.problem(path.name + ' cannot be read')
            return None


class _Git:
    def __init__(self, root, env, timeout, reader):
        self.root, self.env, self.timeout, self.reader = root, env, timeout, reader

    def run(self, *args):
        try:
            return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True,
                                  timeout=self.timeout,
                                  env=dict(self.env, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0'))
        except (OSError, ValueError, subprocess.SubprocessError):
            self.reader.problem('Git ' + args[0] + ' cannot be read')
            return None

    def text(self, *args):
        result = self.run(*args)
        return result.stdout.strip() if result is not None and result.returncode == 0 else None


def _checkout(git):
    head = git.run('symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD')
    prefix = 'refs/remotes/origin/'
    default = head.stdout.strip().removeprefix(prefix) if (head is not None and head.returncode == 0
                  and head.stdout.strip().startswith(prefix)) else 'main'
    branch = git.run('symbolic-ref', '--quiet', '--short', 'HEAD')
    name = branch.stdout.strip() if branch is not None and branch.returncode == 0 else None
    ref = prefix + default
    # %gD with a raw date is the reflog entry's time, not the commit's %ct time.
    log = git.text('reflog', 'show', '-1', '--date=raw', '--format=%gD', ref)
    stamp = re.search(r'@\{(\d+) [+-]\d{4}\}', log or '')
    copied = float(stamp[1]) if stamp else None
    values = dict(default=default, branch=name, copied_at=copied)
    if head is None or branch is None or branch.returncode not in (0, 1):
        return Checkout(**values)
    if branch.returncode == 1:
        return Checkout(**values, relation='detached')
    if name != default:
        return Checkout(**values, relation='other-branch')
    exists = git.run('show-ref', '--verify', '--quiet', ref)
    if exists is None:
        return Checkout(**values)
    if exists.returncode == 1:
        return Checkout(**values, relation='no-remote-ref')
    if exists.returncode:
        return Checkout(**values)
    counts = git.text('rev-list', '--left-right', '--count', f'HEAD...{ref}')
    if counts is None or not re.fullmatch(r'\d+\s+\d+', counts):
        return Checkout(**values)
    ahead, behind = map(int, counts.split())
    relation = 'diverged' if ahead and behind else 'behind' if behind else 'ahead' if ahead else 'current'
    return Checkout(**values, relation=relation, ahead=ahead, behind=behind)


def _scalar(value):
    value = re.sub(r'\s+#.*$', '', value).strip()
    if value.startswith('"'):
        value = json.loads(value)
    elif value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError('Unsupported quoted scalar')
        value = value[1:-1].replace("''", "'")
    elif not value or any(c in value for c in '{}[]&*!|>'):
        raise ValueError('Unsupported scalar')
    if '{{' in value or value == '<<':
        raise ValueError('Dynamic or merged mapping')
    return value


def _key(line):
    match = _KEY.fullmatch(line.rstrip('\r\n'))
    if not match:
        raise ValueError('Unsupported mapping')
    return len(match[1]), _scalar(match[2]), (match[3] or '').strip()


def _mapping(text):
    """Mirror setup's conservative tasks mapping checks; no YAML dependency is vendored."""
    lines = text.splitlines(keepends=True)
    meaningful = lambda line: bool(line.strip()) and not line.lstrip().startswith('#')
    roots = [(i, *_key(line)[1:]) for i, line in enumerate(lines)
             if meaningful(line) and not line.startswith((' ', '\t'))]
    found = [(n, row) for n, row in enumerate(roots) if row[1] == 'tasks']
    if len(found) != 1:
        raise ValueError('Missing or duplicate tasks mapping')
    number, (start, _, value) = found[0]
    if value and not value.startswith('#'):
        raise ValueError('Inline tasks mapping')
    end = roots[number + 1][0] if number + 1 < len(roots) else len(lines)
    entries = [i for i in range(start + 1, end) if meaningful(lines[i])]
    indent = len(lines[entries[0]]) - len(lines[entries[0]].lstrip(' ')) if entries else 2
    names = set()
    for i in entries:
        line = lines[i]
        width = len(line) - len(line.lstrip(' '))
        if '\t' in line[:len(line) - len(line.lstrip())] or width < indent:
            raise ValueError('Inconsistent mapping indentation')
        if width == indent:
            name = _key(line)[1]
            if name in names:
                raise ValueError('Duplicate mapping key')
            names.add(name)
    return sum(map(len, lines[:start + 1])), sum(map(len, lines[:end])), indent, names


def _taskfile(root, reader):
    name = None
    try:
        present = {p.name for p in root.iterdir()}
        names = [name for name in _TASKFILES if name in present]
        if len(names) > 1:
            reader.problem('Multiple supported Taskfiles found; choose one active entrypoint')
            return None, None
        if not names:
            return None, None
        name = names[0]
        path = root / name
        if path.is_symlink():
            raise ValueError('Symlink Taskfile')
        text = path.read_text()
        if _LABEL not in text:
            return name, None
        matches = list(_BLOCK.finditer(text))
        if len(matches) != 1 or text.count(_LABEL) != 2:
            raise ValueError('Malformed block')
        match = matches[0]
        if hashlib.sha256(match[3].encode()).hexdigest() != match[2]:
            raise ValueError('Edited block')
        removed = text[:match.start()] + text[match.end():]
        start, end, indent, _ = _mapping(removed)
        if not start <= match.start() <= end or len(match[1]) != indent:
            raise ValueError('Moved block')
        # Validate the complete mapping too, including duplicate keys in the block.
        _mapping(text)
        return name, frozenset(_mapping('tasks:\n' + match[3])[3])
    except (OSError, ValueError):
        if name is not None:
            reader.problem(f'The managed work tasks in {name} were edited or moved')
            return name, None
        reader.problem('Root Taskfile cannot be read')
        return None, None


def _setup(root, generated, state, reader):
    if not generated:
        return 'source', None
    path = root / '.devcontainer/devcontainer.json'
    config = reader.json(path)
    if config is None:
        return 'unknown', None
    if not path.exists():
        return 'missing', None
    custom = config.get('customizations', {})
    if not isinstance(custom, dict):
        reader.problem('devcontainer.json cannot be read')
        return 'unknown', None
    if 'devcontainerSetup' not in custom:
        return 'unmanaged', None
    metadata = custom['devcontainerSetup']
    ref = metadata.get('sourceRef') if isinstance(metadata, dict) else None
    if not isinstance(ref, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', ref):
        reader.problem('devcontainer.json sourceRef cannot be read')
        return 'unknown', None
    if state is None:
        return 'unknown', ref
    return ('installed' if any((state / name).exists() for name in ('workspace.json', 'scratch.json'))
            else 'partial'), ref


def _launch(repo, default, env, reader):
    if not repo:
        return None
    configured = env.get('XDG_STATE_HOME', '')
    home = Path(configured) if configured and Path(configured).is_absolute() else Path(env.get('HOME') or Path.home()) / '.local/state'
    digest = hashlib.sha256(f'{repo}:{default}'.encode()).hexdigest()[:16]
    record = reader.json(home / 'devcontainer-setup' / (digest + '.json'))
    if not record or record.get('repository') != repo or record.get('branch') != default:
        return None
    name = record.get('codespace')
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return None
    commit = record.get('commit')
    complete = bool(isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit)
                    and record.get('validated_commit') == commit and record.get('rebuild_pending') is False)
    return Launch(name, complete)


def _cycle(runtime, project, binding):
    cycle = _object(runtime.get('conversations'))
    if (not binding or runtime.get('binding') != binding or cycle.get('schema') != 3
            or _number(cycle.get('completed_at')) is None
            or cycle.get('project_id') != project.project_id or cycle.get('identity') != project.identity
            or cycle.get('local_root') != str(project.root)
            or cycle.get('remote_root') != binding.get('remote_root')):
        return {}
    return cycle


def _refusals(runtime, project, binding, reader):
    cycle = _cycle(runtime, project, binding)
    row = _object(_object(cycle.get('directions')).get('desktop_to_codespace'))
    if not sync_cycle.receipt_matches(row.get('receipt'), row.get('snapshot_id')):
        return ()
    issues = row.get('publication_issues', [])
    if not isinstance(issues, list):
        reader.problem('scratch-runtime.json publication issues cannot be read')
        return ()
    result = []
    for issue in issues:
        item = _object(issue)
        agent, session, reason = (item.get(key) for key in ('agent', 'session_id', 'reason'))
        if (not isinstance(agent, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', agent)
                or not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', session)
                or not isinstance(reason, str)):
            reader.problem('scratch-runtime.json publication issue has invalid ids or reason')
            continue
        result.append(Refusal(agent, session, reason))
    return tuple(result)


def refusals(state: Path, project, binding: dict) -> tuple[Refusal, ...]:
    """Return only receipt-bound native-publication refusals for this pairing."""
    reader = _Reader()
    runtime = reader.json(Path(state) / scratch.RUNTIME, private=True)
    return _refusals(runtime or {}, project, binding, reader)


def _process(runtime, state, reader, timeout, *, flag, module):
    if runtime is None:
        return 'unknown'
    try:
        return 'running' if project_state.owned_process(runtime, state, flag=flag, module=module,
                                                       timeout=timeout) else 'stopped'
    except (OSError, ValueError, subprocess.SubprocessError):
        reader.problem(('keeper-runtime.json' if module == '.keeper' else scratch.RUNTIME) + ' cannot be read')
        return 'unknown'


def _retirement_facts(state, reader):
    """Read seals and delete journals without importing the deletion coordinator."""
    # seal validates through replication, which reaches standing via forget on import.
    from . import seal
    retired = ()
    try:
        retired = seal.names(state)
        # names() deliberately skips bad seals; expose each unreadable record here.
        for path in sorted((state / 'retired').glob('*.json')):
            if path.stem not in retired:
                reader.problem('retired/' + path.name + ' cannot be read')
    except (OSError, ValueError):
        reader.problem('retired cannot be read')
    journals, pending = {}, []
    phases = ('confirmed', 'stop-recorded', 'stop-requested', 'shutdown', 'delete-requested', 'deleted', 'cleaned')
    try:
        paths = sorted((state / 'deletions').glob('*.json'))
    except OSError:
        reader.problem('deletions cannot be read')
        paths = []
    for path in paths:
        try:
            record = project_state.read_json(path)
            name, phase = path.stem, record.get('phase')
            at = _number(_object(record.get('at')).get(phase)) if isinstance(phase, str) else None
            if (not _NAME.fullmatch(name) or type(record.get('schema')) is not int or record['schema'] != 1
                    or record.get('codespace') != name or phase not in phases or at is None
                    or not isinstance(record.get('cleanup'), dict)):
                raise ValueError('Invalid delete journal')
            journals[name] = phase
            if phase == 'deleted':
                pending.append((at, name))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, OverflowError):
            reader.problem('deletions/' + path.name + ' cannot be read')
    return dict(retired=tuple(name for name in retired if journals.get(name) != 'cleaned'),
                cleanup=tuple(name for _, name in sorted(pending)))


def _state_facts(state, project, reader, timeout):
    binding = reader.json(state / scratch.CONFIG, private=True)
    pairing = (binding or {}).get('codespace')
    if not isinstance(pairing, str) or not _NAME.fullmatch(pairing):
        pairing = None
    values = dict(pairing=pairing, **_retirement_facts(state, reader))
    try:
        journal = replacing.read(state)
        if journal is not None and journal['ended'] is None:
            if journal['repository'].casefold() != project.identity.removeprefix('github.com/').casefold():
                reader.problem('The replace record belongs to another project')
            elif replacing.ended_by_fact(state, journal) is None:
                values['replacing'] = {key: journal[key] for key in ('old', 'new', 'phase')}
    except (OSError, ValueError):
        reader.problem('The replace record cannot be read')
    arming = reader.json(state / 'arming.json', private=True, missing={'schema': 1, 'armed': False})
    if arming is None or arming.get('schema') != 1 or type(arming.get('armed')) is not bool:
        reader.problem('arming.json cannot be read')
        values['armed'] = None
    elif arming and pairing:
        matches = arming.get('codespace') == pairing
        values['armed'] = arming['armed'] if matches else False
        if matches:
            values.update(gh_user=_text(arming.get('gh_user')), hold_until=_number(arming.get('hold_until')))
        stop_target = arming.get('owner_stop_codespace') if 'owner_stop_codespace' in arming else (
            arming.get('codespace') if arming.get('armed') else None)
        if stop_target == pairing:
            values['owner_stop_at'] = _number(arming.get('owner_stop_at'))
    runtime = reader.json(state / keeper.RUNTIME, private=True)
    if runtime is None:
        values['keeper'] = 'unknown'
    elif pairing and runtime.get('codespace') == pairing:
        status = _process(runtime, state, reader, timeout, flag='--keep', module='.keeper')
        beat = _number(runtime.get('heartbeat_at'))
        age = time.time() - beat if beat is not None else None
        if status == 'running' and (age is None or age > keeper.HEARTBEAT_STALE):
            status = 'stale'
        values.update(keeper=status, keeper_heartbeat_age=age,
                      last_up_at=_number(runtime.get('last_up_at')))
        if not values.get('gh_user'):
            values['gh_user'] = _text(runtime.get('gh_user'))
        reading, last = _object(runtime.get('reading')), _object(runtime.get('last_ok'))
        for record in ([reading, last] if reading.get('ok') is True else [last]):
            at = _number(record.get('at'))
            if isinstance(record.get('state'), str) and at is not None:
                values['reading'] = (record['state'], at)
                break
        if values.get('reading', (None,))[0] == 'Deleted':
            values['deleted_at'] = values['reading'][1]
        down = _object(runtime.get('down'))
        at = _number(down.get('first_at'))
        if status == 'running' and at is not None and down.get('kind') in ('OWNER', 'LIFETIME', 'OTHER'):
            values.update(down_kind=down['kind'], down_at=at)
    worker = reader.json(state / scratch.RUNTIME, private=True)
    if worker is None:
        values['worker'] = 'unknown'
    elif pairing and binding and worker.get('binding') == binding:
        values['worker'] = _process(worker, state, reader, timeout, flag='--supervise', module='.scratch')
        cycle = _cycle(worker, project, binding)
        values['cycle_at'] = _number(cycle.get('completed_at'))
        values['refusals'] = _refusals(worker, project, binding, reader)
    evidence = reader.json(state / 'start-evidence.json', private=True)
    if evidence and pairing:
        values['started_at'] = _number(_object(evidence.get(pairing)).get('started_at'))
    return values


def read(root: Path, *, env: Mapping[str, str] = os.environ, timeout: float = 1.0) -> Local:
    """Read independent facts best-effort; failures never create or repair anything."""
    reader = _Reader()
    values = dict(root=Path(root), in_codespace=env.get('CODESPACES') == 'true')
    try:
        root = Path(root).resolve()
        values['root'] = root
        values['generated'] = Path(__file__).resolve().parent == root / '.devcontainer/scripts/project_sync'
        git = _Git(root, env, timeout, reader)
        values['checkout'] = _checkout(git)
        remote = git.text('remote', 'get-url', 'origin')
        match = _REPOSITORY.fullmatch(remote or '')
        values['repository'] = match[1] if match else ''
        state = None
        project = None
        try:
            identity = project_state._identity(remote or '')
            project = project_state.Project(root, remote, identity, hashlib.sha256(identity.encode()).hexdigest()[:24])
            state = project_state.state_dir(project, env)
        except (OSError, ValueError) as error:
            reader.problem(str(error))
        values['state'] = state
        values['setup'], values['source_ref'] = _setup(root, values['generated'], state, reader)
        values['taskfile'], values['managed'] = _taskfile(root, reader)
        values['launch'] = _launch(values['repository'], values['checkout'].default, env, reader)
        if state is not None:
            values.update(_state_facts(state, project, reader, timeout))
        else:
            values.update(armed=None, keeper='unknown', worker='unknown')
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        reader.problem('Local facts cannot be read: ' + type(error).__name__)
    return Local(**values, problems=tuple(reader.problems))
