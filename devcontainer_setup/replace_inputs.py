"""Read replacement resources and prove the pinned project tree carries its setup."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tempfile

from .git_ops import _fetch_default, git
from .project_sync.state import discover_project, ensure_setup_worktree_root
from .render import validate


@dataclass(frozen=True)
class Resources:
    machine: str
    cpus: int
    idle_timeout_minutes: int
    billable_owner: str


def read_resources(data: dict, repo: str) -> Resources:
    """Use OLD's live API fields only; missing resources never acquire defaults."""
    name = data.get('name', 'OLD')
    repository = data.get('repository')
    full_name = repository.get('full_name') if isinstance(repository, dict) else None
    if not isinstance(full_name, str) or full_name.lower() != repo.lower():
        raise ValueError(f'{name} belongs to another repository; nothing was created.')

    def unreadable(field):
        raise ValueError(f"{name}'s {field} cannot be read from GitHub; nothing was created.")

    machine = data.get('machine')
    machine_name = machine.get('name') if isinstance(machine, dict) else None
    if not isinstance(machine_name, str) or not machine_name.strip():
        unreadable('machine')
    cpus = machine.get('cpus')
    if type(cpus) is not int or cpus <= 0:
        unreadable('core count')
    idle = data.get('idle_timeout_minutes')
    if type(idle) is not int or idle <= 0:
        unreadable('idle timeout')
    owner = data.get('billable_owner')
    login = owner.get('login') if isinstance(owner, dict) else None
    if not isinstance(login, str) or not login.strip():
        unreadable('billing owner')
    return Resources(machine_name, cpus, idle, login)


def offered(machines: list[dict], resources: Resources) -> bool:
    """Require the same named machine and numeric core count at the pin."""
    return any(entry.get('name') == resources.machine and type(entry.get('cpus')) is int
               and entry['cpus'] == resources.cpus for entry in machines)


def pin(project: Path, branch: str) -> str:
    """Fetch only this branch into its tracking ref without changing FETCH_HEAD."""
    if not _fetch_default(project, branch):
        raise ValueError(f'could not read {branch} from GitHub')
    sha = git(project, 'rev-parse', '--verify', f'refs/remotes/origin/{branch}^{{commit}}', check=False)
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError(f'could not read {branch} from GitHub')
    return sha


def setup_differs(project: Path, sha: str) -> str | None:
    """Validate both installations using renderer ownership and lock-normalization rules."""
    errors = validate(project)
    if errors:
        return errors[0]
    root = ensure_setup_worktree_root(discover_project(project))
    temporary = Path(tempfile.mkdtemp(prefix='replace-inspect-', dir=root))
    try:
        git(project, 'worktree', 'add', '--quiet', '--detach', str(temporary), sha)
        errors = validate(temporary)
        if errors:
            return errors[0]
        def marker(tree):
            config = json.loads((tree / '.devcontainer/devcontainer.json').read_text())
            return config['customizations']['devcontainerSetup']

        installed, pinned = marker(project), marker(temporary)
        if installed.get('sourceRef') != pinned.get('sourceRef'):
            return 'a different generator version'
        if any(installed.get(key) != pinned.get(key) for key in ('selections', 'managedConfig', 'managedFiles')):
            return 'different selections'
        return None
    finally:
        # A failing post-checkout hook can leave a fully registered worktree even though add failed.
        registered = git(project, 'worktree', 'list', '--porcelain', '-z').split('\0')
        if f'worktree {temporary}' in registered:
            git(project, 'worktree', 'remove', '--force', str(temporary))
        else:
            temporary.rmdir()
