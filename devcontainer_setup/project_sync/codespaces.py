"""Explicit Codespaces discovery and startup; background transport stays read-only."""
import json
from pathlib import Path
import subprocess
import sys
import time
from . import scratch
from .state import read_json
from .transport import CodespaceConnection, clean_env, github_token, _name

class Codespaces:
    def __init__(self, project, state):
        self.project, self.state = project, Path(state)

    def binding(self):
        try:
            read_json(self.state / scratch.CONFIG)
        except FileNotFoundError:
            return None
        return scratch._config(self.project.root, self.state)

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
        return [
            row for page in pages for row in page.get('codespaces', [])
            if self._matches_repository(row)
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

    def roots(self, name, *, accept_new=True):
        _name(name)
        # Discovery uses the owned read-only transport and never starts a space.
        code = """import pathlib
import subprocess
import json

roots = []
for path in list(pathlib.Path('/workspaces').iterdir())[:100]:
    if path.name == '.devcontainer-work' or not path.is_dir():
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
            connection.accept_new = accept_new
            raw = connection.python(code)
        if len(raw) > 65536:
            raise ValueError('Checkout inventory exceeds limit')
        result = []
        for root, remote in json.loads(raw):
            try:
                if (
                    _identity(remote) == self.project.identity
                    and '.devcontainer-work' not in Path(root).parts
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
