"""GitHub Codespaces boundary; allocation happens only after the setup is on the default branch."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
from urllib.parse import urlencode

from .git_ops import SETUP_BRANCH

RERUN = 'Merge it, then run task devcontainer:setup again to create the Codespace.'
GH_TIMEOUT = 60  # seconds for each GitHub call while merging


def move_first(name: str, branch: str, ref: str | None) -> str:
    return f'Move {name} to {branch} first; it is on {ref}.'


def save_receipt(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.launch-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(values, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _with_github(command: str) -> str:
    """Run a remote command that reaches GitHub in a login shell.

    A codespace sets GITHUB_TOKEN for login shells only; a one-off SSH command
    without it cannot fetch or clone (emsilva/devcontainer#35).
    """
    return shlex.join(['bash', '-lc', command])


class Codespaces:
    def __init__(self, run=subprocess.run):
        self.run = run

    def _gh(self, *args: str, timeout: float | None = None, input: str | None = None):
        try:
            extra = {} if input is None else {'input': input}
            return self.run(['gh', *args], capture_output=True, text=True, timeout=timeout, **extra)
        except FileNotFoundError as error:
            raise ValueError('GitHub CLI is required: install gh and run gh auth login.') from error

    def command(self, *args: str, json_output: bool = False, timeout: float | None = None, input: str | None = None):
        result = self._gh(*args, timeout=timeout, input=input)
        if result.returncode:
            raise ValueError(result.stderr.strip() or result.stdout.strip() or 'GitHub operation failed.')
        if json_output:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise ValueError('GitHub returned an invalid response.') from error
        return result.stdout.strip()

    def _read_ssh(self, name: str, command: str) -> str:
        """Wait for SSH transport on read-only probes; never repeat mutations."""
        for attempt in range(4):
            try:
                return self.command('codespace', 'ssh', '--codespace', name, '--', command, timeout=20)
            except (ValueError, subprocess.TimeoutExpired) as error:
                transport = isinstance(error, subprocess.TimeoutExpired) or any(
                    marker in str(error).lower() for marker in (
                        'kex_exchange_identification:', 'ssh_exchange_identification:',
                        'tunnel closed:', 'ssh: connect to host ',
                    ))
                if not transport:
                    raise
                if attempt == 3:
                    raise ValueError(f'SSH connection to {name} failed after 4 attempts: {error}') from error
                delay = 2 ** (attempt + 1)
                print(f'SSH tunnel for {name} is not ready; retrying in {delay}s '
                      f'({attempt + 2}/4).', flush=True)
                time.sleep(delay)
        raise AssertionError('SSH retry loop exhausted')

    def _container_identity(self, name: str) -> str:
        command = r'''printf '%s:%s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$(awk '{sub(/^.*\) /, ""); print $20}' /proc/1/stat)"'''
        result = self._read_ssh(name, command).splitlines()
        identity = result[-1] if result else ''
        if not re.fullmatch(r'[0-9a-f-]{36}:\d+', identity):
            raise ValueError('Cannot identify the running codespace container; rebuild was not confirmed.')
        return identity

    def _wait_rebuilt(self, name: str, previous: str, timeout: float = 1200) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.wait(name, timeout=max(1, deadline - time.monotonic()))
            if self._container_identity(name) != previous:
                return
            print(f'Waiting for the rebuilt container in {name}...', flush=True)
            time.sleep(3)
        raise ValueError('The old container is still running; resume to finish the pending rebuild.')

    def machines(self, repo: str, ref: str | None = None) -> list[dict]:
        self._repository(repo)
        args = ['api', f'repos/{repo}/codespaces/machines', '--method', 'GET']
        if ref:
            args += ['-f', f'ref={ref}']
        data = self.command(*args, json_output=True)
        return data.get('machines', [])

    @staticmethod
    def _repository(repo: str) -> None:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError('Select a GitHub repository in owner/repo form.')

    def existing(self, repo: str, branch: str | None = None) -> list[dict]:
        """This account's codespaces for repo; only those on branch when one is given."""
        pages = self.command('api', 'user/codespaces?per_page=100', '--paginate', '--slurp', json_output=True)
        return [space for page in pages for space in page.get('codespaces', [])
                if space.get('repository', {}).get('full_name', '').lower() == repo.lower()
                and branch in (None, space.get('git_status', {}).get('ref'))]

    def user(self) -> str:
        """The active gh account; setup never switches it."""
        data = self.command('api', 'user', json_output=True)
        login = data.get('login') if isinstance(data, dict) else None
        if not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9-]+', login):
            raise ValueError('GitHub returned no account name; check gh auth status.')
        return login

    def setup_prs(self, repo: str, base: str) -> list[dict]:
        """Open PRs into base from this repository's setup/devcontainer[-N] branches."""
        self._repository(repo)
        query = urlencode({'state': 'open', 'base': base, 'per_page': 100})
        pages = self.command('api', f'repos/{repo}/pulls?{query}', '--paginate', '--slurp', json_output=True)
        return [pr for page in pages for pr in page
                if SETUP_BRANCH.fullmatch(pr.get('head', {}).get('ref', ''))
                and ((pr['head'].get('repo') or {}).get('full_name') or '').lower() == repo.lower()]

    def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> int:
        """Open a PR from head into base, or reuse the one already open for head."""
        self._repository(repo)
        query = urlencode({'state': 'open', 'head': f'{repo.split("/")[0]}:{head}', 'base': base})
        found = self.command('api', f'repos/{repo}/pulls?{query}', json_output=True)
        if found:
            return int(found[0]['number'])
        # REST, not gh pr create: the body goes as sent, and the repo never comes from the cwd.
        created = self.command('api', '--method', 'POST', f'repos/{repo}/pulls', '--input', '-', json_output=True,
                               input=json.dumps({'title': title, 'head': head, 'base': base, 'body': body}))
        return int(created['number'])

    def _checks_running(self, repo: str, sha: str) -> bool:
        status = self.command('api', f'repos/{repo}/commits/{sha}/status', json_output=True, timeout=GH_TIMEOUT)
        # With no statuses at all GitHub still reports "pending"; only a counted one is running.
        if status.get('state') == 'pending' and status.get('total_count', 0) > 0:
            return True
        runs = self.command('api', f'repos/{repo}/commits/{sha}/check-runs?per_page=100', json_output=True,
                            timeout=GH_TIMEOUT)
        return any(run.get('status') != 'completed' for run in runs.get('check_runs', []))

    def merge_pr(self, repo: str, number: int, title: str, message: str, expected_head: str, *,
                 wait: float = 900, sleep=time.sleep) -> str:
        """Merge the reviewed commit, expected_head, through REST with a verbatim title and message
        (gh pr merge adds a co-author stub); GitHub decides whether it may. Returns the merge commit."""
        self._repository(repo)
        try:
            return self._merge(repo, number, title, message, expected_head, time.monotonic() + wait, sleep)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f'PR #{number} is not confirmed merged: GitHub did not answer within '
                             f'{GH_TIMEOUT}s. {RERUN}') from error

    def _merge(self, repo: str, number: int, title: str, message: str, expected: str, deadline: float, sleep) -> str:
        start = time.monotonic()
        pr = self._pull(repo, number)
        while True:
            head = (pr.get('head') or {}).get('sha') or ''
            if pr.get('merged'):
                if head != expected:
                    raise ValueError(f'PR #{number} was merged at {head[:12]}, not the reviewed {expected[:12]}; no '
                                     'codespace was created. Run task devcontainer:setup again to create the Codespace.')
                return self._merge_commit(pr, number)
            if head != expected:
                raise ValueError(f'PR #{number} is at {head[:12]}, not the reviewed {expected[:12]}; nothing was '
                                 f'merged. {RERUN}')
            if pr.get('state') != 'open':
                raise ValueError(f'PR #{number} was closed without being merged; nothing was merged. '
                                 'Run task devcontainer:setup again to open a new one.')
            body = {'merge_method': 'merge', 'commit_title': title, 'commit_message': message, 'sha': expected}
            result = self._gh('api', '--method', 'PUT', f'repos/{repo}/pulls/{number}/merge', '--input', '-',
                              input=json.dumps(body), timeout=GH_TIMEOUT)
            if not result.returncode:
                try:
                    sha = json.loads(result.stdout).get('sha')
                except (ValueError, AttributeError):
                    sha = None
                return sha if isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}', sha) else (
                    self._merge_commit(self._pull(repo, number), number))
            reason = self._reason(result)
            pr = self._pull(repo, number)
            if pr.get('merged') or pr.get('state') != 'open' or (pr.get('head') or {}).get('sha') != expected:
                continue
            if time.monotonic() >= deadline or not self._checks_running(repo, expected):
                raise ValueError(f'PR #{number} is open: {reason}. {RERUN}')
            waited = int(time.monotonic() - start) + 30
            print(f'PR #{number}: required checks are still running; merging again in 30s '
                  f'({waited // 60}:{waited % 60:02d} of {int(deadline - start) // 60} min).', flush=True)
            sleep(30)

    def _pull(self, repo: str, number: int) -> dict:
        pr = self.command('api', f'repos/{repo}/pulls/{number}', json_output=True, timeout=GH_TIMEOUT)
        if not isinstance(pr, dict):
            raise ValueError(f'GitHub returned no PR #{number}; run task devcontainer:setup again.')
        return pr

    @staticmethod
    def _merge_commit(pr: dict, number: int) -> str:
        sha = pr.get('merge_commit_sha')
        if not pr.get('merged') or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError(f'GitHub accepted the merge of PR #{number} but does not show it merged yet; '
                             'run task devcontainer:setup again to create the Codespace.')
        return sha

    @staticmethod
    def _reason(result) -> str:
        """GitHub's own message for a refused call, from gh's error body or its stderr line."""
        try:
            message = json.loads(result.stdout).get('message')
        except (ValueError, AttributeError):
            message = None
        if not isinstance(message, str) or not message.strip():
            message = re.sub(r'^gh: |\s*\(HTTP \d+\)$', '', result.stderr.strip()) or 'GitHub refused the merge'
        return message.strip().rstrip('.')

    def get(self, name: str) -> dict:
        if not re.fullmatch(r'[A-Za-z0-9-]+', name):
            raise ValueError('Invalid codespace name.')
        return self.command('api', f'user/codespaces/{name}', json_output=True)

    def wait(self, name: str, timeout: float = 1200) -> None:
        deadline = time.monotonic() + timeout
        last_state = None
        while time.monotonic() < deadline:
            data = self.get(name)
            state = data.get('state')
            if state != last_state:
                print(f'Codespace {name}: {state}', flush=True)
                last_state = state
            if state == 'Available':
                return
            if state in {'Failed', 'Deleted', 'Archived', 'Unavailable'}:
                raise ValueError(f'Codespace {name} entered {state}; inspect gh codespace logs -c {name}.')
            time.sleep(3)
        raise ValueError(f'Codespace {name} is still provisioning; resume rather than create another.')

    def personalize(self, name: str, repo: str, profile: dict | None = None) -> None:
        if profile:
            # The dotfiles read identity and Claude settings from an unmanaged per-machine
            # profile, which a new codespace lacks. Seed it once; never replace one.
            self.command('codespace', 'ssh', '--codespace', name, '--',
                         'umask 077 && mkdir -p ~/.config/chezmoi && { test -e ~/.config/chezmoi/machine.json'
                         ' || cat > ~/.config/chezmoi/machine.json; }', input=json.dumps(profile, indent=2) + '\n')
        self.command('codespace', 'ssh', '--codespace', name, '--', _with_github(shlex.join(['load-dotfiles', repo])))

    def open(self, name: str, client: str) -> None:
        args = ['codespace', 'code', '--codespace', name]
        if client == 'browser':
            args += ['--web']
        try:
            self.command(*args)
        except ValueError as error:
            raise ValueError(f'{error}\nOpen https://{name}.github.dev/ in your browser.') from error

    _save_receipt = staticmethod(save_receipt)

    @staticmethod
    def resource_settings(data: dict) -> dict:
        machine = data.get('machine')
        if not isinstance(machine, dict) or not machine.get('name'):
            raise ValueError('Existing codespace machine metadata is unavailable; inspect it with '
                             'gh api user/codespaces/<name> before resuming.')
        return {'machine': machine['name'],
                'idle_timeout_minutes': data.get('idle_timeout_minutes'),
                'retention_period_minutes': data.get('retention_period_minutes'),
                'location': data.get('location')}

    @staticmethod
    def _minutes(value: str | None) -> int | None:
        if value is None:
            return None
        match = re.fullmatch(r'(\d+)([mh])', value)
        if not match:
            raise ValueError(f'Unsupported resource duration: {value}. Use minutes or hours (30m, 24h).')
        return int(match[1]) * (60 if match[2] == 'h' else 1)

    @staticmethod
    def _preflight_receipt(path: Path) -> None:
        try:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError(f'Invalid launch receipt path {path}; existing content was preserved.')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, probe = tempfile.mkstemp(prefix='.launch-probe-', dir=path.parent)
            os.close(fd)
            os.unlink(probe)
        except OSError as error:
            raise ValueError(f'Cannot store launch receipt at {path}: {error}') from error

    def launch(self, repo: str, branch: str, sha: str, machine: str | None, idle_timeout: str | None,
               client: str, dotfiles_repo: str | None, receipt: Path,
               *, resume: str | None = None, location: str | None = None,
               retention: str | None = None, workspace: str | None = None,
               machine_profile: dict | None = None, state_root: Path | None = None) -> str:
        """Create (or reuse) a codespace on branch, which must contain sha, the merged setup. A reused
        codespace confirmed on branch takes over its retired setup branch's receipt from state_root."""
        self._repository(repo)
        self._preflight_receipt(receipt)
        folder = workspace or f'/workspaces/{repo.split("/")[1]}'
        if not folder.startswith('/') or '${' in folder:
            raise ValueError('Resolve the custom workspace path before remote validation.')
        name = resume
        if name:
            # Refused before anything changes: there is nothing to resume.
            data = self.get(name)
            if data.get('repository', {}).get('full_name', '').lower() != repo.lower():
                raise ValueError('The selected codespace belongs to a different repository.')
            ref = data.get('git_status', {}).get('ref')
            if ref != branch:
                raise ValueError(move_first(name, branch, ref))
            settings = self.resource_settings(data)
            requested = {'machine': machine, 'idle_timeout_minutes': self._minutes(idle_timeout),
                         'location': location, 'retention_period_minutes': self._minutes(retention)}
            for key, value in requested.items():
                if value is not None and value != settings[key]:
                    raise ValueError(f'The existing codespace has {key}={settings[key]!r}; '
                                     f'requested {value!r} was not applied. Review its actual resource settings.')
            if state_root is not None:
                migrate_receipt(state_root, repo, branch, name)
        try:
            if not name:
                available = self.machines(repo, sha)
                if machine not in {entry['name'] for entry in available}:
                    raise ValueError(f'Selected machine {machine} is unavailable for the merged configuration; '
                                     f'setup is on {branch}. Rerun to choose an available machine.')
                args = ['codespace', 'create', '--repo', repo, '--branch', branch,
                        '--machine', machine, '--idle-timeout', idle_timeout,
                        '--devcontainer-path', '.devcontainer/devcontainer.json']
                if location:
                    args += ['--location', location]
                if retention:
                    args += ['--retention-period', retention]
                print('Creating codespace; provisioning may take a few minutes.', flush=True)
                created = self.command(*args).splitlines()[-1]
                if not re.fullmatch(r'[A-Za-z0-9-]+', created):
                    raise ValueError('Creation did not return a codespace name. Check gh codespace list before retrying.')
                name = created
                settings = {'machine': machine, 'idle_timeout_minutes': self._minutes(idle_timeout),
                            'location': location, 'retention_period_minutes': self._minutes(retention)}
            record = {'codespace': name, 'repository': repo, 'branch': branch,
                      'commit': sha, **settings, 'client': client,
                      'validated_commit': None, 'rebuild_pending': bool(resume)}
            if resume and receipt.exists():
                try:
                    previous = json.loads(receipt.read_text())
                except (ValueError, OSError) as error:
                    raise ValueError(f'Cannot read launch receipt {receipt}; preserve it before retrying.') from error
                if not isinstance(previous, dict):
                    raise ValueError(f'Invalid launch receipt {receipt}; preserve it before retrying.')
                if (previous.get('codespace') == name and previous.get('repository') == repo
                        and previous.get('branch') == branch):
                    record['validated_commit'] = previous.get('validated_commit')
                    record['rebuild_pending'] = not (
                        previous.get('validated_commit') == sha
                        and previous.get('rebuild_pending') is False)
            self._save_receipt(receipt, record)
            if resume and data.get('state') in {'Shutdown', 'ShutDown'}:
                self.command('api', f'user/codespaces/{name}/start', '--method', 'POST')
            self.wait(name)
            if resume:
                actual_branch = self._read_ssh(name, f'cd {shlex.quote(folder)} && git branch --show-current')
                if actual_branch != branch:
                    raise ValueError(move_first(name, branch, actual_branch))
                dirty = self._read_ssh(name, f'cd {shlex.quote(folder)} && git status --porcelain --untracked-files=all')
                if dirty:
                    raise ValueError(f'The existing codespace has user changes. Preserve them before reuse:\n{dirty}')
            # The branch may have moved past the merge (another PR merged since); it must contain it.
            contains_command = (f'cd {shlex.quote(folder)} && if git merge-base --is-ancestor {shlex.quote(sha)} HEAD; '
                                'then echo yes; else echo no; fi')
            contains = self._read_ssh(name, contains_command) == 'yes'
            if resume and not contains:
                # Arm before changing checkout HEAD so an interrupted rebuild
                # cannot be mistaken for an already up-to-date environment.
                record['rebuild_pending'] = True
                self._save_receipt(receipt, record)
                # A fast-forward is the only permitted update; divergence fails closed.
                command = (f'cd {shlex.quote(folder)} && '
                           f'test "$(git branch --show-current)" = {shlex.quote(branch)} && '
                           f'test -z "$(git status --porcelain)" && '
                           f'git fetch origin {shlex.quote(branch)} && '
                           f'git merge-base --is-ancestor HEAD {shlex.quote(sha)} && git merge --ff-only {shlex.quote(sha)}')
                self.command('codespace', 'ssh', '--codespace', name, '--', _with_github(command))
            if resume and record['rebuild_pending']:
                previous_container = self._container_identity(name)
                self.command('codespace', 'rebuild', '--codespace', name)
                self._wait_rebuilt(name, previous_container)
                contains = self._read_ssh(name, contains_command) == 'yes'
            if not contains:
                raise ValueError('The codespace does not contain the reviewed commit; inspect its Git state before resuming.')
            self._read_ssh(name, f'cd {shlex.quote(folder)} && bash .devcontainer/scripts/validate-selected.sh')
            record['validated_commit'] = sha
            record['rebuild_pending'] = False
            self._save_receipt(receipt, record)
            if dotfiles_repo:
                self.personalize(name, dotfiles_repo, machine_profile)
            self.open(name, client)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            if not name:
                raise ValueError(str(error)) from error
            raise ValueError(f'{error}\nSetup is on {branch}. Resume with: '
                             f'task devcontainer:setup -- --resume {name}') from error
        return name


def receipt_path(state_root: Path, repo: str, branch: str) -> Path:
    """Where the launch receipt for repo's codespace on branch lives under the state home."""
    return Path(state_root) / 'devcontainer-setup' / (hashlib.sha256(f'{repo}:{branch}'.encode()).hexdigest()[:16] + '.json')


def migrate_receipt(state_root: Path, repo: str, default: str, codespace: str) -> Path | None:
    """Copy the receipt a retired setup branch left for codespace, which the caller saw on default,
    to default's path with that branch; the old file stays beside it. Returns the new path, if any."""
    target = receipt_path(state_root, repo, default)
    if target.exists() or target.is_symlink():
        return None
    found = []
    for path in (Path(state_root) / 'devcontainer-setup').glob('*.json'):
        try:
            values = json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else None
        except (ValueError, OSError):
            continue
        branch = values.get('branch') if isinstance(values, dict) else None
        if (isinstance(branch, str) and branch != default and values.get('repository') == repo
                and values.get('codespace') == codespace and path == receipt_path(state_root, repo, branch)):
            found.append((path.stat().st_mtime, path, values))
    if not found:
        return None
    _, _, values = max(found, key=lambda entry: entry[0])
    save_receipt(target, dict(values, branch=default))
    return target
