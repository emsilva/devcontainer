"""Read remote facts under one deadline, owning and reaping every child group."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
from urllib.parse import urlencode

from .lifecycle import parse_time
from .transport import clean_env, _login, _name

LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True)
class Request:
    repository: str
    default: str
    want_prs: bool
    generator: tuple[str, str] | None


@dataclass(frozen=True)
class Observation:
    status: str
    value: object
    at: float | None
    reason: str | None


@dataclass(frozen=True)
class Space:
    name: str
    state: str
    last_used_at: float | None


@dataclass(frozen=True)
class Remote:
    account: Observation
    names: Observation
    spaces: Observation
    setup_prs: Observation
    upstream: Observation


def generator_request(env: Mapping[str, str]) -> tuple[str, str]:
    return (env.get('SETUP_DEVCONTAINER_REPO', 'https://github.com/emsilva/devcontainer.git'),
            env.get('SETUP_DEVCONTAINER_REF', 'main'))


def _unknown(reason='no answer within 2 s'):
    return Observation('unknown', None, None, reason)


def _failed(reason):
    return Observation('failed', None, time.time(), reason)


def _inventory(pages, repository):
    if not isinstance(pages, list) or not pages:
        raise ValueError('incomplete list')
    rows, counts = [], []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get('codespaces'), list):
            raise ValueError('incomplete list')
        rows.extend(page['codespaces'])
        counts.append(page.get('total_count'))
    names = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('incomplete list')
        try:
            names.add(_name(row.get('name')))
        except ValueError:
            raise ValueError('incomplete list') from None
    if any('total_count' in page for page in pages):
        if any(type(count) is not int or count != len(names) for count in counts):
            raise ValueError('incomplete list')
    elif len(pages[-1]['codespaces']) >= 100:
        raise ValueError('incomplete list')
    spaces, seen = [], set()
    for row in rows:
        repo = row.get('repository')
        full_name = repo.get('full_name') if isinstance(repo, dict) else None
        if isinstance(full_name, str) and full_name.lower() == repository.lower() and row['name'] not in seen:
            state = row.get('state')
            if not isinstance(state, str):
                raise ValueError('request failed')
            spaces.append(Space(row['name'], state, parse_time(row.get('last_used_at'))))
            seen.add(row['name'])
    return frozenset(names), tuple(spaces)


def _setup_prs(pages, repository):
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError('request failed')
    numbers = []
    for page in pages:
        for pr in page:
            head = pr.get('head') if isinstance(pr, dict) else None
            if not isinstance(head, dict):
                raise ValueError('request failed')
            ref, repo = head.get('ref'), head.get('repo')
            full_name = repo.get('full_name') if isinstance(repo, dict) else None
            if (isinstance(ref, str) and re.fullmatch(r'setup/devcontainer(?:-[1-9][0-9]*)?', ref)
                    and isinstance(full_name, str) and full_name.lower() == repository.lower()):
                number = pr.get('number')
                if type(number) is not int or number < 1:
                    raise ValueError('request failed')
                numbers.append(number)
    return tuple(numbers)


@dataclass
class _Child:
    key: str
    process: subprocess.Popen
    output: bytearray = field(default_factory=bytearray)
    pipes: int = 2
    done: bool = False


class Probe:
    """No worker thread: start launches readers; join multiplexes their pipes.

    Groups remain owned after their leaders exit. A descendant that creates its
    own session escapes that ownership; gh and git do not normally do so.
    """

    def __init__(self, request, *, env, deadline, clock, popen):
        self._request, self._clock, self._popen = request, clock, popen
        self._end = clock() + max(0, deadline)
        self._selector = selectors.DefaultSelector()
        self._children = []
        self._observations = {key: _unknown() for key in ('account', 'names', 'spaces', 'setup_prs', 'upstream')}
        self._remote = None
        self._env = clean_env(env)
        self._env.update(GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1')
        self._token = ''
        self._patterns = ()
        try:
            if request.generator is not None:
                self._start_git(request.generator, env)
            if env.get('GH_TOKEN'):
                self._token = env['GH_TOKEN']
                self._start_api()
            else:
                args = ['gh', 'auth', 'token', '--hostname', 'github.com']
                if env.get('DEVCONTAINER_GH_USER'):
                    args += ['--user', env['DEVCONTAINER_GH_USER']]
                self._spawn('token', args, self._env)
        except BaseException:
            self._cleanup()
            raise

    def _set(self, key, observation, *, only_unknown=False):
        keys = ('account', 'names', 'spaces', 'setup_prs') if key == 'token' else (
            ('names', 'spaces') if key == 'names' else (key,))
        for name in keys:
            if not only_unknown or self._observations[name].status == 'unknown':
                self._observations[name] = observation

    def _spawn(self, key, args, env, **kwargs):
        if self._clock() >= self._end:
            return
        try:
            process = self._popen(args, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  start_new_session=True, **kwargs)
        except FileNotFoundError:
            self._set(key, _failed(args[0] + ' is not installed'))
            return
        except OSError:
            self._set(key, _failed('request failed'))
            return
        child = _Child(key, process)
        self._children.append(child)
        for stream, stdout in ((process.stdout, True), (process.stderr, False)):
            os.set_blocking(stream.fileno(), False)
            self._selector.register(stream, selectors.EVENT_READ, (child, stdout))

    def _start_git(self, generator, env):
        url, ref = generator
        if re.fullmatch(r'[0-9a-fA-F]{40}', ref):
            self._observations['upstream'] = _unknown('pinned')
            return
        if not url or not ref or ref.startswith('-'):
            self._observations['upstream'] = _failed('request failed')
            return
        local = Path(url).expanduser()
        if local.is_dir():
            url = str(local.resolve())
        self._patterns = ((ref + '^{}', ref) if ref.startswith('refs/') else
                          ('refs/heads/' + ref, 'refs/tags/' + ref + '^{}', 'refs/tags/' + ref))
        git_env = {key: value for key, value in env.items()
                   if not key.startswith('GIT_') and key != 'SSH_ASKPASS'}
        git_env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_SSH_COMMAND='ssh -o BatchMode=yes')
        # Include both a tag and its peeled object, preferring the peeled answer.
        patterns = ((ref, ref + '^{}') if ref.startswith('refs/') else
                    ('refs/heads/' + ref, 'refs/tags/' + ref, 'refs/tags/' + ref + '^{}'))
        self._spawn('upstream', ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=',
                                'ls-remote', '--', url, *patterns], git_env, cwd='/')

    def _start_api(self):
        env = dict(self._env, GH_TOKEN=self._token)
        base = ['gh', 'api', '--hostname', 'github.com']
        self._spawn('account', [*base, 'user'], env)
        self._spawn('names', [*base, 'user/codespaces?per_page=100', '--paginate', '--slurp'], env)
        if self._request.want_prs:
            query = urlencode({'state': 'open', 'base': self._request.default, 'per_page': 100})
            self._spawn('setup_prs', [*base, 'repos/' + self._request.repository + '/pulls?' + query,
                                     '--paginate', '--slurp'], env)

    def _answered(self, child):
        if child.process.returncode:
            self._set(child.key, _failed('not signed in to GitHub' if child.key == 'token' else 'request failed'))
            return
        try:
            text = child.output.decode('utf-8')
            if child.key == 'token':
                self._token = text.strip()
                if self._token:
                    self._start_api()
                else:
                    self._set('token', _failed('not signed in to GitHub'))
                return
            if child.key == 'upstream':
                refs = {}
                for line in text.splitlines():
                    parts = line.split()
                    if len(parts) != 2 or not re.fullmatch(r'[0-9a-fA-F]{40}', parts[0]):
                        raise ValueError('request failed')
                    refs[parts[1]] = parts[0].lower()
                value = next((refs[ref] for ref in self._patterns if ref in refs), None)
                if value is None:
                    raise ValueError('request failed')
            else:
                obj = json.loads(text)
                if child.key == 'account':
                    value = obj.get('login') if isinstance(obj, dict) else None
                    value = _login(value)
                elif child.key == 'names':
                    names, spaces = _inventory(obj, self._request.repository)
                    at = time.time()
                    self._observations['names'] = Observation('ok', names, at, None)
                    self._observations['spaces'] = Observation('ok', spaces, at, None)
                    return
                else:
                    value = _setup_prs(obj, self._request.repository)
            self._set(child.key, Observation('ok', value, time.time(), None))
        except (ValueError, TypeError, OverflowError) as error:
            reason = 'incomplete list' if str(error) == 'incomplete list' else 'request failed'
            self._set(child.key, _failed(reason))
        finally:
            child.output.clear()

    def _read(self, key):
        child, stdout = key.data
        try:
            chunk = os.read(key.fileobj.fileno(), 65536)
        except BlockingIOError:
            return
        except OSError:
            self._set(child.key, _failed('request failed'))
            child.done = True
            self._stop(child)
            chunk = b''
        if not chunk:
            self._selector.unregister(key.fileobj)
            key.fileobj.close()
            child.pipes -= 1
        elif stdout and not child.done:
            if len(child.output) + len(chunk) > LIMIT:
                self._set(child.key, _failed('answer too large'))
                child.done = True
                child.output.clear()
                self._stop(child)
            else:
                child.output.extend(chunk)
        # Stderr is drained and discarded, never decoded or retained.

    @staticmethod
    def _kill(child):
        # Darwin can return EPERM while a killed leader is still exiting.
        # Briefly reap and retry, still signalling the group for descendants.
        child.process.poll()
        retry_until = time.monotonic() + 0.1
        while True:
            try:
                os.killpg(child.process.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
            except PermissionError:
                if child.process.returncode is not None:
                    return  # A reaped leader's EPERM group contains only zombies.
                remaining = retry_until - time.monotonic()
                if remaining <= 0:
                    raise
                try:
                    child.process.wait(timeout=min(0.01, remaining))
                except subprocess.TimeoutExpired:
                    pass

    def _stop(self, child):
        try:
            self._kill(child)
        except OSError:
            self._set(child.key, _failed(f'request failed; child {child.process.pid} could not be signalled'),
                      only_unknown=True)

    def _cleanup(self):
        for child in self._children:
            self._stop(child)
        for child in self._children:
            try:
                child.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._set(child.key, _failed(f'request failed; child {child.process.pid} was not reaped'),
                          only_unknown=True)
            for stream in (child.process.stdout, child.process.stderr):
                if stream is not None:
                    stream.close()
            child.output.clear()
        self._selector.close()
        self._token = ''

    def _result(self):
        if self._remote is None:
            self._remote = Remote(**self._observations)
        return self._remote

    def join(self) -> Remote:
        if self._remote is not None:
            return self._remote
        try:
            while self._clock() < self._end:
                for child in list(self._children):
                    if not child.done and child.pipes == 0 and child.process.poll() is not None:
                        child.done = True
                        self._answered(child)
                if all(child.done for child in self._children):
                    break
                remaining = max(0, self._end - self._clock())
                for key, _ in self._selector.select(min(remaining, 0.05)):
                    self._read(key)
        finally:
            self._cleanup()
            self._result()
        return self._result()

    def cancel(self) -> None:
        if self._remote is None:
            self._cleanup()
            self._result()


def start(request: Request, *, env: Mapping[str, str] = os.environ, deadline: float = 2.0,
          clock=time.monotonic, popen=subprocess.Popen) -> Probe:
    return Probe(request, env=env, deadline=deadline, clock=clock, popen=popen)
