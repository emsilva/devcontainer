"""Project-scoped orchestration for the interactive daily launcher."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid
from . import agents, handoff, ownership, scratch, sessions
from .state import atomic_write, host_id, read_json
from .transport import CodespaceConnection, clean_env, github_token, _name


class Workflows:
    def __init__(self, project, state):
        self.project, self.state = project, Path(state)
        self.is_remote = os.environ.get('CODESPACES') == 'true'

    def binding(self):
        try:
            read_json(self.state / scratch.CONFIG)
        except FileNotFoundError:
            return None
        return scratch._config(self.project.root, self.state)

    def preferences(self):
        try:
            value = read_json(self.state / 'work.json')
            keys = ('agent', 'session_id', 'location')
            if not isinstance(value, dict) or any(key not in value for key in keys):
                raise ValueError('Missing selection fields')
            self._selection(value['agent'], value['session_id'], value['location'])
            return {key: value[key] for key in keys}
        except FileNotFoundError:
            return {}
        except (ValueError, TypeError) as error:
            raise ValueError(
                'Invalid saved conversation selection; choose a conversation again. '
                'The saved file has been preserved.'
            ) from error

    @staticmethod
    def _selection(agent, session_id, location):
        sessions.session_uuid(session_id)
        if agent not in ('codex', 'claude') or location not in ('mac', 'codespace'):
            raise ValueError('Choose a supported agent and machine')

    def remember(self, agent, session_id, location):
        self._selection(agent, session_id, location)
        selection = dict(agent=agent, session_id=session_id, location=location)
        atomic_write(
            self.state / 'work.json', json.dumps(selection).encode() + b'\n'
        )

    @staticmethod
    def _api_env():
        env = clean_env()
        env['GH_TOKEN'] = github_token()
        return env

    @staticmethod
    def _api(method, route, env, *, paginate=False, timeout=30):
        args = ['gh', 'api', '--method', method, '--hostname', 'github.com', route]
        if paginate:
            args += ['--paginate', '--slurp']
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, env=env, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                'Codespace API request timed out; inspect its status in GitHub before retrying'
            ) from None
        if result.returncode:
            raise ValueError('Codespace API request failed; check GitHub authentication and retry')
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError('Codespace inventory exceeds limit')
        return json.loads(result.stdout or '{}')

    def _matches_repository(self, row):
        repository = row.get('repository', {}).get('full_name', '').lower()
        return repository == self.project.identity.removeprefix('github.com/').lower()

    def spaces(self):
        pages = self._api('GET', 'user/codespaces?per_page=100', self._api_env(), paginate=True)
        branch = sessions._git(self.project)['branch']
        return [
            row for page in pages for row in page.get('codespaces', [])
            if self._matches_repository(row)
            and row.get('git_status', {}).get('ref') == branch
            and _name(row['name'])
        ]

    def space(self):
        binding = self.binding()
        if not binding:
            return None
        name = _name(binding['codespace'])
        row = self._api('GET', 'user/codespaces/' + name, self._api_env())
        if row.get('name') != name or not self._matches_repository(row):
            raise ValueError('Paired Codespace repository differs; pair the correct project checkout')
        return row

    def roots(self, name):
        _name(name)
        # Discovery uses the owned read-only transport and never starts a space.
        code = """import pathlib
import subprocess
import json

roots = []
for path in list(pathlib.Path('/workspaces').iterdir())[:100]:
    if not path.is_dir():
        continue
    result = subprocess.run(
        ['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
        capture_output=True, text=True,
    )
    if result.returncode:
        continue
    root = result.stdout.strip()
    remote = subprocess.run(
        ['git', '-C', root, 'remote', 'get-url', 'origin'],
        capture_output=True, text=True,
    )
    roots.append([root, remote.stdout.strip()])
print(json.dumps(roots))
"""
        from .state import _identity
        with CodespaceConnection(name, state=self.state) as connection:
            # Explicit pairing discovery is the first host-key acceptance boundary.
            connection.accept_new = True
            raw = connection.python(code)
        if len(raw) > 65536:
            raise ValueError('Checkout inventory exceeds limit')
        result = []
        for root, remote in json.loads(raw):
            try:
                if (
                    _identity(remote) == self.project.identity
                    and root.startswith('/')
                    and '..' not in Path(root).parts
                    and not any(ord(c) < 32 for c in root)
                ):
                    result.append(root)
            except ValueError:
                pass
        return sorted(set(result))

    def pair(self, name, remote_root=None):
        _name(name)
        roots = self.roots(name)
        if remote_root is None:
            if len(roots) != 1:
                raise ValueError('Choose one verified remote checkout')
            remote_root = roots[0]
        if remote_root not in roots:
            raise ValueError('Remote checkout does not match project identity')
        return scratch.configure(self.project.root, self.state, name, remote_root)

    def start_codespace(self, name=None):
        config = self.binding()
        if name is None and not config:
            raise ValueError('Choose or pair a Codespace first')
        name = _name(name or config['codespace'])
        env = self._api_env()
        self._api('POST', 'user/codespaces/' + name + '/start', env, timeout=120)
        deadline = time.monotonic() + 300
        last_state = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    f'Codespace start timed out (last state: {last_state or "unknown"}); inspect its status in GitHub '
                    'and retry when Available'
                )
            row = self._api('GET', 'user/codespaces/' + name, env, timeout=min(30, remaining))
            state = row.get('state')
            if state == 'Available':
                return
            # A successful start request can precede its visible state change.
            # Shutdown remains a waiting state until the bounded deadline.
            if state in ('Failed', 'Unavailable', 'Deleted', 'Archived'):
                raise ValueError(
                    f'Codespace start ended in {state}; '
                    'inspect its logs in GitHub before retrying'
                )
            if state != last_state:
                print('Waiting for Codespace to become Available…', file=sys.stderr)
                last_state = state
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def inventory(self, remote=False):
        if not remote:
            return handoff.LocalEndpoint(self.project, self.state).call('inventory')
        config = self.binding()
        if not config:
            raise ValueError('Pair a Codespace first')
        with CodespaceConnection(config['codespace'], state=self.state) as connection:
            endpoint = handoff.RemoteEndpoint(
                connection, config['remote_root'], self.project.identity
            )
            return endpoint.call('inventory')

    def ensure_sync(self):
        return scratch.start(self.project.root, self.state)

    def sync_status(self):
        return scratch.inspect_sync(self.project.root, self.state)

    def stop_sync(self):
        return scratch.stop(self.state)

    def sync_ready(self):
        # A newly started supervisor returns before SSH and Mutagen reconcile.
        # Wait here so first use has the same transfer barrier as a warm worker.
        deadline = time.monotonic() + 90
        announced = False
        while True:
            status = self.sync_status()
            phase = status.get('phase')
            if phase == 'synchronized':
                return scratch.flush(self.project.root, self.state)
            if phase == 'conflicts':
                raise ValueError('Scratch conflicts: ' + ', '.join(status.get('conflicts', [])))
            if phase in ('stopped', 'cleanup_required'):
                raise ValueError('Scratch sync is not running; start it from Settings and retry.')
            if time.monotonic() >= deadline:
                raise ValueError('Scratch synchronization timed out. Check Settings and troubleshooting, then retry.')
            if not announced:
                print('Waiting for scratch synchronization...', flush=True)
                announced = True
            time.sleep(1)

    @staticmethod
    def _check_git(left, right):
        if (
            not left.get('head')
            or not left.get('branch')
            or any(left.get(key) != right.get(key) for key in ('branch', 'head'))
            or left.get('status') != ''
            or right.get('status') != ''
        ):
            raise ValueError(
                'Git branch, HEAD or dirty state differs. Commit or preserve changes '
                'and reconcile both checkouts manually before switching; Git was not changed.'
            )

    def continue_session(self, agent, session_id, destination):
        self._selection(agent, session_id, destination)
        if self.is_remote and destination == 'mac':
            raise ValueError('Use task work on your Mac to return custody')
        local = self.inventory()
        local_location = 'codespace' if self.is_remote else 'mac'
        target = local if destination == local_location else self.inventory(remote=True)
        row = next(
            (row for row in target['sessions']
             if row['agent'] == agent and row['session_id'] == session_id),
            None,
        )
        owner = row.get('ownership', {}) if row else {}
        if owner.get('status') == 'idle' and owner.get('host') == target['host']:
            if destination != local_location:
                self._check_git(local['git'], target['git'])
                self.sync_ready()
        else:
            if self.is_remote:
                raise ValueError('Local conversation needs explicit adoption or writer recovery')
            remote = target if destination == 'codespace' else self.inventory(remote=True)
            source = local if destination == 'codespace' else remote
            candidate = next(
                (row for row in source['sessions']
                 if row['agent'] == agent and row['session_id'] == session_id),
                None,
            )
            status = candidate.get('ownership', {}) if candidate else {}
            if status.get('status') not in ('idle', 'transferring', 'transferred'):
                raise ValueError(
                    'Conversation has an active or uncertain writer; '
                    'stop it or explicitly adopt locally after inspection'
                )
            self._check_git(local['git'], remote['git'])
            self.sync_ready()
            handoff.handoff(self.project, agent, session_id, destination, self.state)
        self.remember(agent, session_id, destination)
        if destination == local_location:
            return agents.launch(self.project, agent, session_id, state=self.state, host=local['host'])
        return self._launch_remote(agent, session_id)

    def _launch_remote(self, agent, session_id, *, request_id=None):
        config = self.binding()
        arguments = (' --session ' + shlex.quote(session_id)) if session_id else ' --new'
        if request_id:
            arguments += ' --launch-request ' + shlex.quote(sessions.session_uuid(request_id))
        # The generated helper is self-contained inside the selected checkout.
        command = (
            'cd ' + shlex.quote(config['remote_root'])
            + " && { test -f .devcontainer/scripts/project-sync.sh || { printf "
            + "'Update project devcontainer helpers before continuing; custody is retained.\\n'; "
            + "exit 1; }; } && exec bash .devcontainer/scripts/project-sync.sh agent "
            + shlex.quote(agent) + arguments
        )
        with CodespaceConnection(config['codespace'], state=self.state) as connection:
            path = self.state / 'ssh-interactive-config'
            connection.ssh_config(path)
            return subprocess.call(['ssh', '-F', str(path), '-tt', 'project-sync', command], env=clean_env())

    def _launch_result(self, request_id):
        config = self.binding()
        with CodespaceConnection(config['codespace'], state=self.state) as connection:
            return handoff.RemoteEndpoint(connection, config['remote_root'], self.project.identity).call(
                'launch_result', request_id=request_id)

    def new_session(self, agent, *, destination=None):
        if agent not in ('codex', 'claude'):
            raise ValueError('Unsupported agent')
        here = 'codespace' if self.is_remote else 'mac'
        destination = destination or here
        if destination not in ('mac', 'codespace') or (self.is_remote and destination == 'mac'):
            raise ValueError('Use task work on your Mac to launch on that host')
        if destination != here:
            local, remote = self.inventory(), self.inventory(remote=True)
            self._check_git(local['git'], remote['git'])
            self.sync_ready()
            request_id = str(uuid.uuid4())
            code = self._launch_remote(agent, None, request_id=request_id)
            try:
                result = self._launch_result(request_id)
                if not isinstance(result, dict) or result.get('agent') != agent:
                    raise ValueError('Unexpected managed launch receipt')
                sid = sessions.session_uuid(result.get('session_id'))
                self.remember(agent, sid, destination)
            except (ValueError, OSError, RuntimeError):
                print('Could not refresh the saved selection. Any created conversation remains in the Codespace; '
                      f'use task work:{agent}:cs -- --resume to find it. The launch was not repeated.', file=sys.stderr)
                sid = None
            return code, sid
        host = host_id()
        new = agent == 'claude'
        sid = (
            str(uuid.uuid4()) if new
            else agents.create_codex_session(self.project, state=self.state, host=host)
        )
        self.remember(agent, sid, 'codespace' if self.is_remote else 'mac')
        return (agents.launch(self.project, agent, sid, state=self.state, host=host, new=new), sid)

    def adopt(self, agent, session_id):
        self._selection(agent, session_id, 'mac')
        home = sessions.native_home(agent)
        host = host_id()
        sessions.export_snapshot(
            self.project, agent, session_id, home, source_host=host,
            generation=0, state=self.state, agent_version=handoff._agent_version(agent),
        )
        ownership.adopt_local(
            self.state, agent, session_id, host,
            native_paths=agents.native_paths(
                self.project, agent, session_id, home, state=self.state
            ),
        )
