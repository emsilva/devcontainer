"""GitHub Codespaces boundary; allocation happens only after confirmed push."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time


class Codespaces:
    def __init__(self, run=subprocess.run):
        self.run = run

    def command(self, *args: str, json_output: bool = False, timeout: float | None = None):
        try:
            result = self.run(['gh', *args], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as error:
            raise ValueError('GitHub CLI is required: install gh and run gh auth login.') from error
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

    def existing(self, repo: str, branch: str) -> list[dict]:
        pages = self.command('api', 'user/codespaces?per_page=100', '--paginate', '--slurp', json_output=True)
        return [space for page in pages for space in page.get('codespaces', [])
                if space.get('repository', {}).get('full_name', '').lower() == repo.lower()
                and space.get('git_status', {}).get('ref') == branch]

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

    def personalize(self, name: str, repo: str) -> None:
        self.command('codespace', 'ssh', '--codespace', name, '--', shlex.join(['load-dotfiles', repo]))

    def open(self, name: str, client: str) -> None:
        args = ['codespace', 'code', '--codespace', name]
        if client == 'browser':
            args += ['--web']
        try:
            self.command(*args)
        except ValueError as error:
            raise ValueError(f'{error}\nOpen https://{name}.github.dev/ in your browser.') from error

    @staticmethod
    def _save_receipt(path: Path, values: dict) -> None:
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

    def launch(self, plan, repo: str, machine: str, idle_timeout: str,
               client: str, dotfiles_repo: str | None, receipt: Path,
               *, resume: str | None = None, location: str | None = None,
               retention: str | None = None, workspace: str | None = None) -> str:
        self._repository(repo)
        sha = plan.push()
        folder = workspace or f'/workspaces/{repo.split("/")[1]}'
        if not folder.startswith('/') or '${' in folder:
            raise ValueError('Resolve the custom workspace path before remote validation.')
        name = resume
        if name:
            data = self.get(name)
            if (data.get('repository', {}).get('full_name', '').lower() != repo.lower()
                    or data.get('git_status', {}).get('ref') != plan.branch):
                raise ValueError('The selected codespace belongs to a different repository or branch.')
        else:
            available = self.machines(repo, sha)
            if machine not in {entry['name'] for entry in available}:
                raise ValueError(f'Selected machine {machine} is unavailable for the pushed configuration; '
                                 'setup is committed and pushed. Rerun to choose an available machine.')
            args = ['codespace', 'create', '--repo', repo, '--branch', plan.branch,
                    '--machine', machine, '--idle-timeout', idle_timeout,
                    '--devcontainer-path', '.devcontainer/devcontainer.json']
            if location:
                args += ['--location', location]
            if retention:
                args += ['--retention-period', retention]
            print('Creating codespace; provisioning may take a few minutes.', flush=True)
            name = self.command(*args).splitlines()[-1]
            if not re.fullmatch(r'[A-Za-z0-9-]+', name):
                raise ValueError('Creation did not return a codespace name. Check gh codespace list before retrying.')
        self._save_receipt(receipt, {'codespace': name, 'repository': repo, 'branch': plan.branch,
                                    'commit': sha, 'machine': machine, 'client': client})
        try:
            if resume and data.get('state') in {'Shutdown', 'ShutDown'}:
                self.command('api', f'user/codespaces/{name}/start', '--method', 'POST')
            self.wait(name)
            if resume:
                actual_branch = self._read_ssh(name, f'cd {shlex.quote(folder)} && git branch --show-current')
                if actual_branch != plan.branch:
                    raise ValueError('The existing codespace is on a different branch. Preserve it before reuse.')
                dirty = self._read_ssh(name, f'cd {shlex.quote(folder)} && git status --porcelain --untracked-files=all')
                if dirty:
                    raise ValueError(f'The existing codespace has user changes. Preserve them before reuse:\n{dirty}')
            revision_command = f'cd {shlex.quote(folder)} && git rev-parse HEAD'
            current = self._read_ssh(name, revision_command)
            if resume and current != sha:
                # A fast-forward is the only permitted update; divergence fails closed.
                command = (f'cd {shlex.quote(folder)} && test -z "$(git status --porcelain)" && '
                           f'git fetch origin {shlex.quote(plan.branch)} && '
                           f'git merge-base --is-ancestor HEAD {shlex.quote(sha)} && git merge --ff-only {shlex.quote(sha)}')
                self.command('codespace', 'ssh', '--codespace', name, '--', command)
                self.command('codespace', 'rebuild', '--codespace', name)
                self.wait(name)
                current = self._read_ssh(name, revision_command)
            if current != sha:
                raise ValueError('The codespace is not at the reviewed commit; inspect its Git state before resuming.')
            self._read_ssh(name, f'cd {shlex.quote(folder)} && bash .devcontainer/scripts/validate-selected.sh')
            if dotfiles_repo:
                self.personalize(name, dotfiles_repo)
            self.open(name, client)
        except ValueError as error:
            raise ValueError(f'{error}\nCommit and push are preserved. Resume with: '
                             f'task devcontainer:setup -- --resume {name}') from error
        return name
