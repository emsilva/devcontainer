"""Interactive configuration, plus read-only scan/check/dry-run commands."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from .codespaces import RERUN, Codespaces, move_first, receipt_path
from .discovery import scan
from .git_ops import (GitPlan, ReviewChanged, default_branch, default_contains, fast_forward_checkout, git,
                      is_ancestor, merged_setup_branches, proposed_branch, remove_setup_worktree, remove_worktree,
                      setup_worktree)
from .preferences import load_preferences, save_preferences
from .releases import fetch_release
from .render import render, validate
from .ui import Cancelled, Gum, cache_home

SOURCE = Path(__file__).resolve().parents[1]
RUNTIME_DEFAULTS = {'python': '3.13', 'node': '24', 'ruby': '3.4', 'go': '1.26', 'rust': '1.99.1'}
TOOL_LABELS = {'azure': 'Azure CLI', 'aws': 'AWS CLI', 'gcp': 'Google Cloud CLI',
               'docker': 'Docker', 'devtunnel': 'Dev Tunnels', 'precommit': 'pre-commit',
               'kubernetes': 'Kubernetes / Helm', 'db-clients': 'Database clients'}


def source_ref() -> str:
    pinned = os.environ.get('DEVCONTAINER_SETUP_REF', '')
    if re.fullmatch(r'[0-9a-f]{40}', pinned):
        return pinned
    if re.fullmatch(r'[0-9a-f]{40}', SOURCE.name):
        return SOURCE.name
    return git(SOURCE, 'rev-parse', 'HEAD')


def _metadata(project: Path) -> dict:
    path = project / '.devcontainer/devcontainer.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get('customizations', {}).get('devcontainerSetup', {})
    except (ValueError, AttributeError) as error:
        raise ValueError(f'Cannot read existing devcontainer configuration: {error}') from error


def defaults(found: dict, preferences: dict, metadata: dict) -> dict:
    previous = metadata.get('selections')
    if previous:
        return dict(json.loads(json.dumps(previous)), agents=True,
                    continuity=previous.get('continuity', True))
    managers = {family: entry['selected'] for family, entry in found['package_managers'].items()
                if entry.get('selected')}
    return {'runtimes': {name: entry['version'] or RUNTIME_DEFAULTS[name]
                         for name, entry in found['runtimes'].items() if entry['selected']},
            'tools': [name for name, entry in found['tools'].items() if entry['selected']],
            'agents': True, 'dotfiles': preferences['apply_dotfiles'],
            'ssh': False, 'ports': found['ports'], 'package_managers': managers,
            'package_manager_versions': {family: entry.get('version') or '' for family, entry in found['package_managers'].items()
                                         if entry.get('selected') and (entry.get('version') or
                                             entry.get('versions', {}).get(entry['selected'], {}).get('conflicts'))},
            'polyglot': False, 'continuity': True}


def integrate_taskfile(project: Path, files: dict[str, bytes]) -> None:
    names = ('Taskfile.yml', 'Taskfile.yaml', 'taskfile.yml', 'taskfile.yaml',
             'Taskfile.dist.yml', 'Taskfile.dist.yaml', 'taskfile.dist.yml', 'taskfile.dist.yaml')
    present = {path.name for path in project.iterdir()}
    existing = [name for name in names if name in present]
    if len(existing) > 1:
        raise ValueError('Multiple supported Taskfiles found; choose one active entrypoint before setup: ' + ', '.join(existing))
    name = existing[0] if existing else 'Taskfile.yml'
    path = project / name
    if path.is_symlink():
        raise ValueError(f'Existing Taskfile is a symlink: {name}; review integration manually')
    text = path.read_text() if existing else "version: '3'\ntasks:\n" + ''.join(
        f"  {operation}:\n    cmds: [{{task: 'devcontainer:{operation}'}}]\n"
        for operation in ('up', 'down', 'status', 'build'))
    from .taskfile_mapping import integrate_include, mapping
    mapping(text, 'tasks', required=True)
    changed = integrate_include(text)
    if changed != text or not existing:
        files[name] = changed.encode()
    ignore = project / '.gitignore'
    ignored = ignore.read_text() if ignore.exists() else ''
    line = '/.devcontainer/devcontainer.env'
    if line not in ignored.splitlines() and '.devcontainer/devcontainer.env' not in ignored.splitlines():
        files['.gitignore'] = (ignored.rstrip() + '\n\n# Personal devcontainer settings\n' + line + '\n').lstrip('\n').encode()


def integrate_continuity(project: Path, files: dict[str, bytes], enabled: bool) -> None:
    """Review project guidance and optional root aliases in the same GitPlan."""
    from .project_sync.notes import integrate_notes
    candidates = dict(files)
    if enabled:
        integrate_notes(project, candidates, True)
    names = ('Taskfile.yml', 'Taskfile.yaml', 'taskfile.yml', 'taskfile.yaml',
             'Taskfile.dist.yml', 'Taskfile.dist.yaml', 'taskfile.dist.yml', 'taskfile.dist.yaml')
    present = {path.name for path in project.iterdir()}
    existing = [name for name in names if name in candidates or name in present]
    if len(existing) != 1:
        raise ValueError('Project continuity needs one active root Taskfile')
    name = existing[0]
    if (project / name).is_symlink():
        raise ValueError('Project Taskfile is a symlink; review continuity integration manually')
    text = candidates[name].decode() if name in candidates else (project / name).read_text()
    from .root_tasks import integrate
    candidates[name] = integrate(text, enabled=enabled).encode()
    files.update(candidates)


def _task_inventory(root: Path, name: str) -> set[str]:
    # Listing parses Task's command graph; never dry-run/evaluate owner commands,
    # preconditions or status checks (which may launch services or need live files).
    if shutil.which('task') is None:
        raise ValueError('go-task is required to validate the candidate Taskfile')
    try:
        result = subprocess.run(['task', '--taskfile', name, '--list-all', '--json'],
                                cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError('Cannot inspect Taskfile inventory: ' + str(error)) from error
    if result.returncode:
        raise ValueError('Taskfile inventory failed: ' + result.stderr.strip())
    try:
        return {entry['name'] for entry in json.loads(result.stdout)['tasks']}
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError('Invalid Taskfile inventory response') from error


def _stage_task_context(root: Path, project: Path, name: str, seen=None) -> None:
    """Copy only literal local Task includes, retaining their relative layout."""
    from .taskfile_mapping import include_paths, NAMES
    seen = set() if seen is None else seen
    if name in seen:
        return
    seen.add(name)
    target = root / name
    source = project / name
    if not source.resolve().is_relative_to(project.resolve()):
        raise ValueError('Taskfile include context escapes the reviewed project: ' + name)
    if source.is_symlink():
        raise ValueError('Taskfile include context is a symlink; review required: ' + name)
    if not target.exists():
        if not source.is_file():
            return  # Task enforces required/optional includes itself.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    for location in include_paths(target.read_text()).values():
        path = Path(name).parent / location
        # Include context never reads outside the reviewed project or fetches URLs.
        if Path(location).is_absolute() or '..' in path.parts or '://' in location:
            raise ValueError('Taskfile validation needs a local project include: ' + location)
        if (project / path).is_dir():
            choices = [path / item for item in NAMES if (project / path / item).is_file()]
            if not choices:
                continue
            path = choices[0]
        _stage_task_context(root, project, path.as_posix(), seen)


def _literal_task_graph(root: Path, name: str):
    """Static names supplement Task's public inventory (which omits internals)."""
    from .taskfile_mapping import NAMES, include_namespaces, task_aliases, task_stanzas
    definitions, contexts, seen = set(), [], set()

    def visit(path, namespace='', ancestors=()):
        if path in ancestors:
            raise ValueError('Cannot prove Task reference retirement through cyclic includes: ' + path)
        if (path, namespace) in seen or not (root / path).is_file():
            return
        seen.add((path, namespace))
        text = (root / path).read_text()
        try:
            stanzas = list(task_stanzas(text))
            for task, fields in stanzas:
                definitions.update(namespace + alias for alias in [task] + task_aliases(fields))
                if task == 'default' and namespace:
                    definitions.add(namespace[:-1])  # Task's implicit include alias.
        except ValueError as error:
            raise ValueError(f'Cannot prove Task reference definitions in {path}: {error}') from error
        contexts.append((path, namespace, stanzas))
        for location, namespaces in include_namespaces(text):
            included = Path(path).parent / location
            if (root / included).is_dir():
                choices = [included / item for item in NAMES if (root / included / item).is_file()]
                if not choices:
                    continue
                included = choices[0]
            for prefix in namespaces:
                visit(included.as_posix(), namespace + (prefix + ':' if prefix else ''), ancestors + (path,))

    visit(name)
    return definitions, contexts


def _check_owner_task_references(before_root, after_root, name, owned_aliases, owned_include):
    from .taskfile_mapping import task_references
    before, contexts = _literal_task_graph(before_root, name)
    after, _ = _literal_task_graph(after_root, name)
    lost = before - after
    if not lost:
        return
    wildcards = sorted(item for item in lost if any(char in item for char in '*?['))
    for path, namespace, stanzas in contexts:
        if path == owned_include:
            continue
        for task, fields in stanzas:
            owner = namespace + task
            if owner not in after or (path == name and task in owned_aliases):
                continue
            try:
                references = list(task_references(fields))
            except ValueError as error:
                raise ValueError(f'Owner Task {owner} reference in {path} cannot prove safe retirement: {error}') from error
            for reference in references:
                target = reference[1:] if namespace and reference.startswith(':') else namespace + reference
                if target not in after and wildcards:
                    raise ValueError(f'Owner Task {owner} reference {target} in {path} cannot prove safe '
                                     'retirement of wildcard targets: ' + ', '.join(wildcards))
                if target in lost:
                    raise ValueError(f'Owner Task {owner} reference in {path} needs retiring target {target}; '
                                     'retain the delegate or review the owner reference before setup')


def _check_task_candidate(root: Path, project: Path | None) -> None:
    from .taskfile_mapping import NAMES, mapping
    present = {path.name for path in root.iterdir()}
    if project:
        present.update(path.name for path in project.iterdir())
    names = [name for name in NAMES if name in present]
    if not names:
        return
    if len(names) != 1:
        raise ValueError('Taskfile candidate needs one active root entrypoint')
    name = names[0]
    if project:
        _stage_task_context(root, project, name)
    after = _task_inventory(root, name)
    from .root_tasks import integrate
    candidate = (root / name).read_text()
    without_owned = integrate(candidate, enabled=False)
    generated = (set(mapping(candidate, 'tasks', required=True).entries)
                 - set(mapping(without_owned, 'tasks', required=True).entries))
    dangling = {'devcontainer:' + alias for alias in generated} - after
    if dangling:
        raise ValueError('Candidate has missing managed Task delegates: ' + ', '.join(sorted(dangling)))
    if project is None or not (project / name).is_file():
        return
    with tempfile.TemporaryDirectory(prefix='devcontainer-task-before-') as directory:
        before_root = Path(directory)
        _stage_task_context(before_root, project, name)
        before = _task_inventory(before_root, name)
        original = (project / name).read_text()
        remaining = integrate(original, enabled=False)
        retired = (set(mapping(original, 'tasks', required=True).entries)
                   - set(mapping(remaining, 'tasks', required=True).entries))
        owned_aliases, owned_include = set(retired), None
        # Derive ownership from Task's actual graph, never a namespace prefix:
        # owner root tasks can legitimately use the generated include's prefix.
        config = project / '.devcontainer/devcontainer.json'
        if config.is_file():
            metadata = json.loads(config.read_text()).get('customizations', {}).get('devcontainerSetup', {})
            old = project / '.devcontainer/Taskfile.yml'
            digest = metadata.get('managedFiles', {}).get('.devcontainer/Taskfile.yml')
            if old.is_file() and hashlib.sha256(old.read_bytes()).hexdigest() == digest:
                owned_include = '.devcontainer/Taskfile.yml'
                staged = before_root / '.devcontainer/Taskfile.yml'
                if staged.is_file():
                    content = staged.read_bytes()
                    try:
                        staged.write_text('version: "3"\ntasks:\n')
                        retired.update(before - _task_inventory(before_root, name))
                    finally:
                        staged.write_bytes(content)
        missing = before - after - retired
        if missing:
            raise ValueError('Candidate lost owner Task commands: ' + ', '.join(sorted(missing)))
        if retired or owned_include:
            _check_owner_task_references(before_root, root, name, owned_aliases, owned_include)


def check_candidate(files: dict[str, bytes], project: Path | None = None) -> None:
    from .taskfile_mapping import NAMES
    if any(name in files and files[name] is None for name in NAMES):
        raise ValueError('Candidate cannot delete the owner Taskfile entrypoint')
    with tempfile.TemporaryDirectory(prefix='devcontainer-review-') as directory:
        root = Path(directory)
        if project:
            marker = json.loads(files['.devcontainer/devcontainer.json']).get('customizations', {}).get('devcontainerSetup', {})
            for name in marker.get('managedFiles', {}):
                source = project / name
                if name not in files and source.is_file() and not source.is_symlink():
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
        for name, data in files.items():
            if data is None:
                continue
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        _check_task_candidate(root, project)
        errors = validate(root)
        if errors:
            raise ValueError('Generated configuration did not validate:\n' + '\n'.join(errors))


def dotfiles_repository(value: str) -> str:
    value = value.strip()
    if (not value or value.startswith('-') or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError('Enter a dotfiles repository such as emsilva/dotfiles or a Git URL.')
    if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        return value
    if re.fullmatch(r'git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+', value):
        return value
    parsed = urlsplit(value)
    if (parsed.scheme in {'https', 'ssh'} and parsed.hostname and not parsed.password
            and not parsed.query and (parsed.scheme == 'ssh' or not parsed.username)):
        return value
    raise ValueError('Use owner/repo, HTTPS, or SSH without embedded passwords/tokens.')


def repository_access(repo: str) -> bool:
    target = repo if '://' in repo or repo.startswith('git@') else f'https://github.com/{repo.removesuffix(".git")}.git'
    try:
        result = subprocess.run(['git', 'ls-remote', '--exit-code', '--', target, 'HEAD'],
                                env=dict(os.environ, GIT_TERMINAL_PROMPT='0'),
                                capture_output=True, text=True, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ensure_runner(ref: str) -> None:
    # Even an existing SHA-named directory needs an independent source authority.
    repository = (str(SOURCE) if (SOURCE / '.git').exists() else
                  os.environ.get('SETUP_DEVCONTAINER_REPO', 'https://github.com/emsilva/devcontainer.git'))
    fetch_release(repository, ref, cache_home() / 'devcontainer-setup/releases')


NO_GITHUB_ORIGIN = ('Codespaces launch requires a GitHub origin remote. Add one with '
                    '`git remote add origin https://github.com/OWNER/REPO.git`, push, and run setup again.')


def _github_repository(project: Path) -> str | None:
    remote = git(project, 'remote', 'get-url', 'origin', check=False)
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?', remote)
    return match[1] if match else None


def _git_identity(project: Path) -> tuple[str, str] | None:
    name = git(project, 'config', 'user.name', check=False)
    email = git(project, 'config', 'user.email', check=False)
    return (name, email) if name and email else None


def _machine_profile(project: Path) -> dict | None:
    """This Mac's chezmoi machine profile for a codespace, minus what only works here."""
    path = Path.home() / '.config/chezmoi/machine.json'
    try:
        profile = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        identity = _git_identity(project)
        return {'git': {'name': identity[0], 'email': identity[1]}} if identity else None
    if not isinstance(profile, dict):
        raise ValueError(f'{path} must contain a JSON object.')
    profile.pop('codespace', None)  # Names a codespace from the Mac side.
    home = str(Path.home())
    hooks = profile.get('claudeSettings', {}).get('hooks', {})
    for event in list(hooks):
        # A hook command under this Mac's home directory cannot run in a codespace.
        groups = [dict(group, hooks=[hook for hook in group.get('hooks', []) if home not in str(hook.get('command', ''))])
                  for group in hooks[event]]
        hooks[event] = [group for group in groups if group['hooks']]
        if not hooks[event]:
            del hooks[event]
    return profile


def _repository(project: Path) -> str:
    repo = _github_repository(project)
    if not repo:
        raise ValueError(NO_GITHUB_ORIGIN)
    return repo


def _check_revision(metadata: dict, ref: str, upgrade: bool, default: str | None = None) -> None:
    if metadata and metadata.get('sourceRef') != ref and not upgrade:
        if default:
            raise ValueError(f'origin/{default} uses a different generator revision. Run its installed task from an '
                             f'up-to-date {default} checkout, or use --upgrade to review updating it.')
        raise ValueError('This project uses a different generator revision. Run its installed task, or use --upgrade to review updating it.')


@contextmanager
def setup_checkout(project: Path):
    """Yield (checkout, default) to render and commit in: with a GitHub origin, a temporary
    worktree on a new setup branch cut from origin/<default>; else the owner's checkout and None."""
    if not _github_repository(project):
        yield project, None
        return
    from .project_sync.state import discover_project, setup_worktree_root
    default = default_branch(project)
    root = setup_worktree_root(discover_project(project))
    worktree, branch = setup_worktree(project, root)
    start = git(project, 'rev-parse', f'refs/heads/{branch}')
    try:
        yield worktree, default
    except BaseException:
        _close_setup_worktree(project, root, worktree, branch, start, failing=True)
        raise
    _close_setup_worktree(project, root, worktree, branch, start)


def _close_setup_worktree(project: Path, root: Path, worktree: Path, branch: str, start: str,
                          failing: bool = False) -> None:
    """Keep a branch that holds a setup commit; drop one that holds nothing."""
    committed = git(project, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False) != start
    try:
        if not committed:
            remove_setup_worktree(project, worktree, branch, remote=False, root=root, cut=start)
            return
        pushed = ', also pushed to origin' if git(project, 'config', f'branch.{branch}.remote', check=False) else ''
        print(f'Setup commit is on branch {branch}{pushed}; your checkout and its branch are unchanged.', flush=True)
        remove_worktree(project, worktree, branch, root=root)
    except ValueError as error:
        # A committed setup stands; never mask the error or cancellation that ended setup.
        if not (failing or committed):
            raise
        print(f'Setup: {error}', file=sys.stderr)


def _preferences_after_commit(project: Path, preferences: dict) -> None:
    try:
        saved = save_preferences(preferences)
        print(f'Preferences saved: {saved}', flush=True)
        if preferences['apply_dotfiles']:
            path = project / '.devcontainer/devcontainer.env'
            if not git(project, 'check-ignore', '.devcontainer/devcontainer.env', check=False):
                # Personal settings may hold tokens: only where Git already ignores them.
                print(f'Add DOTFILES_REPO={preferences["dotfiles_repo"]} to .devcontainer/devcontainer.env '
                      'once this checkout has the setup files.', flush=True)
                return
            if path.is_symlink():
                raise ValueError('Local dotfiles settings path is a symlink; not overwritten.')
            if path.exists():
                # Existing personal settings belong to their owner.
                lines = path.read_text().splitlines()
                lines = [line for line in lines if not line.startswith('DOTFILES_REPO=')]
            else:
                lines = []
            lines.append('DOTFILES_REPO=' + preferences['dotfiles_repo'])
            fd, temporary = tempfile.mkstemp(prefix='.env-', dir=path.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    stream.write('\n'.join(lines) + '\n')
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    except (OSError, ValueError) as error:
        print(f'Setup is committed, but personal settings could not be saved: {error}', file=sys.stderr)


def upgrade_handoff(project: Path, current_ref: str, resume: str | None) -> int | None:
    release, revision = fetch_release(os.environ.get('SETUP_DEVCONTAINER_REPO', 'https://github.com/emsilva/devcontainer.git'),
                                      os.environ.get('SETUP_DEVCONTAINER_REF', 'main'))
    if revision == current_ref:
        return None
    args = ['bash', str(release / 'scripts/setup.sh'), '--project', str(project), '--upgrade-loaded']
    if resume:
        args += ['--resume', resume]
    result = subprocess.run(args, env=dict(os.environ, DEVCONTAINER_SETUP_REF=revision))
    return result.returncode


def _select(ui: Gum, found: dict, chosen: dict, preferences: dict) -> dict:
    print('\nRecommendations (Python + uv are always included):', flush=True)
    for name, entry in found['runtimes'].items():
        if entry['selected']:
            print(f'  {name}: {entry["version"] or "choose version"}')
            for evidence in entry['evidence'][:3]:
                print(f'    {evidence}')
    for name, entry in found['tools'].items():
        if entry['selected']:
            print(f'  {TOOL_LABELS.get(name, name)}: ' + '; '.join(entry['evidence'][:2]))
    for warning in found['warnings']:
        print(f'  Note: {warning}')
    runtime_names = ui.multi('Project runtimes (Python + uv included)', ['node', 'ruby', 'go', 'rust'],
                             [name for name in chosen['runtimes'] if name != 'python'])
    runtimes = {'python': chosen['runtimes'].get('python', '3.13')}
    for name in ['python', *runtime_names]:
        entry = found['runtimes'][name]
        value = chosen['runtimes'].get(name, entry['version'] or RUNTIME_DEFAULTS[name])
        if name in runtime_names and name not in chosen['runtimes'] or entry['conflicts']:
            if entry['conflicts']:
                print('\n'.join(entry['conflicts']), flush=True)
            value = ui.input(f'{name} version (resolve declarations above)', value)
        runtimes[name] = value
    chosen['runtimes'] = runtimes
    labels = list(TOOL_LABELS.values())
    reverse = {label: name for name, label in TOOL_LABELS.items()}
    chosen['tools'] = [reverse[label] for label in ui.multi('Project tools', labels,
                      [TOOL_LABELS[name] for name in chosen['tools'] if name in TOOL_LABELS])]
    chosen['continuity'] = ui.confirm('Sync scratch files and project notes with your Mac?',
                                      chosen.get('continuity', True))
    chosen['package_managers'] = {family: manager for family, manager in chosen.get('package_managers', {}).items()
                                  if family in runtimes}
    chosen['package_manager_versions'] = {family: version for family, version in chosen.get('package_manager_versions', {}).items()
                                           if family in chosen['package_managers']}
    for family, entry in found['package_managers'].items():
        if family not in runtimes:
            continue
        if entry['conflicts']:
            print('\n'.join(entry['conflicts']), flush=True)
            options = {'node': ['npm', 'pnpm', 'yarn', 'bun'], 'python': ['uv', 'pip', 'poetry', 'pipenv']}.get(family)
            if options:
                chosen['package_managers'][family] = ui.choose(f'{family} package manager', options)
        manager = chosen['package_managers'].get(family)
        version_entry = entry.get('versions', {}).get(manager, {})
        if version_entry.get('conflicts'):
            print('\n'.join(version_entry['conflicts']), flush=True)
            chosen['package_manager_versions'][family] = ui.input(
                f'{manager} version (resolve declarations above; exact x.y.z)',
                chosen['package_manager_versions'].get(family, ''))
        elif version_entry.get('version'):
            chosen['package_manager_versions'][family] = version_entry['version']
        elif entry['conflicts']:
            chosen['package_manager_versions'].pop(family, None)
    chosen['dotfiles'] = ui.confirm('Apply dotfiles inside the container?', preferences['apply_dotfiles'])
    if chosen['dotfiles']:
        while True:
            try:
                repo = dotfiles_repository(ui.input('Dotfiles repository', preferences['dotfiles_repo']))
                if repo != preferences['dotfiles_repo']:
                    print('Checking dotfiles repository access...', flush=True)
                    if not repository_access(repo) and not ui.confirm('Could not verify access. Continue with this repository?', False):
                        continue
                preferences['dotfiles_repo'] = repo
                break
            except ValueError as error:
                print(error, file=sys.stderr)
    chosen['agents'] = True
    preferences['apply_dotfiles'], preferences['agentic_tools'] = chosen['dotfiles'], chosen['agents']
    if ui.confirm('Advanced environment options?', False):
        for name, version in chosen['runtimes'].items():
            chosen['runtimes'][name] = ui.input(f'{name} version', version)
        ports = ui.input('Forwarded ports (comma-separated)', ','.join(map(str, chosen['ports'])))
        try:
            chosen['ports'] = sorted({int(value.strip()) for value in ports.split(',') if value.strip()})
        except ValueError as error:
            raise ValueError('Forwarded ports must be comma-separated numbers.') from error
        chosen['polyglot'] = ui.confirm('Use the full polyglot preset?', chosen.get('polyglot', False))
    return chosen


LAUNCH_BROWSER = 'Push and open a Codespace in the browser'
LAUNCH_VSCODE = 'Push and open a Codespace in VS Code'
COMMIT_ONLY = 'Commit only (no Codespace)'


def _launch_options(ui: Gum, api: Codespaces, project: Path, branch: str, resume: str | None) -> dict | None:
    """Codespace choices for branch (the default branch, with a GitHub origin)."""
    action = ui.choose('After setup', [LAUNCH_BROWSER, LAUNCH_VSCODE, COMMIT_ONLY],
                       LAUNCH_BROWSER if resume or _github_repository(project) else COMMIT_ONLY)
    if action == COMMIT_ONLY:
        return None
    try:
        repo = _repository(project)
        if not resume:
            # Every codespace of the repository is shown, so a second one is never created silently.
            spaces = api.existing(repo)
            if spaces:
                ref = {space['name']: space.get('git_status', {}).get('ref') for space in spaces}
                choices = ['Create a new codespace', *[f'Reuse {name}' if ref[name] == branch
                                                       else f'Reuse {name} (on {ref[name]})' for name in ref]]
                choice = ui.choose('Existing codespaces for this repository', choices)
                if choice.startswith('Reuse '):
                    resume = choice.removeprefix('Reuse ').split(' ')[0]
        if resume:
            data = api.get(resume)
            settings = api.resource_settings(data)
            if data.get('git_status', {}).get('ref') != branch:
                raise ValueError(move_first(resume, branch, data.get('git_status', {}).get('ref')))
            print(f'Existing codespace {resume}: {json.dumps(settings, sort_keys=True)}', flush=True)
            if not ui.confirm('Reuse this codespace with these existing resource settings?'):
                raise Cancelled()
            return {'repo': repo, 'machine': settings['machine'],
                    'idle_timeout': (f"{settings['idle_timeout_minutes']}m"
                                     if settings['idle_timeout_minutes'] is not None else None),
                    'location': settings['location'],
                    'retention': (f"{settings['retention_period_minutes']}m"
                                  if settings['retention_period_minutes'] is not None else None),
                    'client': 'browser' if action.endswith('browser') else 'vscode', 'resume': resume}
        # Before approval the configuration is not on GitHub yet.
        # Offer repository machines now; launch rechecks them at the merged SHA.
        machines = api.machines(repo)
        if not machines:
            raise ValueError('No available Codespaces machines for this repository/account.')
    except ValueError as error:
        print(f'Launch unavailable: {error}', file=sys.stderr)
        if ui.confirm('Continue with commit only?'):
            return None
        raise Cancelled()
    labels = {f'{m["cpus"]} cores / {m["memory_in_bytes"] // (1024**3)} GB RAM / '
              f'{m["storage_in_bytes"] // (1024**3)} GB disk ({m["name"]})': m for m in machines}
    selected = next((label for label, machine in labels.items() if machine['cpus'] == 4), next(iter(labels)))
    machine = labels[ui.choose('Codespaces machine', list(labels), selected)]
    timeout = ui.choose('Stop when idle for', ['15m', '30m', '1h', '2h'], '30m')
    options = {'repo': repo, 'machine': machine['name'], 'idle_timeout': timeout,
               'client': 'browser' if action.endswith('browser') else 'vscode', 'resume': resume}
    if ui.confirm('Show advanced launch options?', False):
        region = ui.choose('Region', ['Automatic', 'EastUs', 'WestUs2', 'WestEurope', 'SouthEastAsia'])
        if region != 'Automatic':
            options['location'] = region
        options['retention'] = ui.choose('Delete stopped codespace after', ['24h', '72h', '168h', '720h'], '168h')
    return options


def setup_continuity(project: Path, ui, codespace: str | None) -> None:
    """Finish the approved setup using its selected Codespace; Git is durable."""
    if not codespace:
        if os.environ.get('CODESPACES') == 'true':
            print('Run task work:cs on your Mac to connect scratch and conversation sync for this Codespace.', flush=True)
        else:
            print('No Codespace was opened. To create one, run task devcontainer:setup again and choose a Codespace; '
                  'if this project already has one, run task work:cs to connect it.', flush=True)
        return
    from .project_sync.state import discover_project, state_dir
    from .project_sync.codespaces import Codespaces as ProjectCodespaces
    try:
        discovered = discover_project(project)
        service = ProjectCodespaces(discovered, state_dir(discovered))
        if os.environ.get('CODESPACES') == 'true':
            print('Run task work:cs on your Mac to connect scratch and conversation sync for this Codespace.', flush=True)
            return
        binding = service.binding()
        if binding and binding['codespace'] == codespace:
            root = binding['remote_root']
        else:
            roots = service.roots(codespace)
            if not roots:
                raise ValueError('No matching project checkout was found in the selected Codespace.')
            root = roots[0] if len(roots) == 1 else ui.choose('Which remote project checkout?', roots)
        service.pair(codespace, root)
        print('Codespace paired. Run task work:cs to open Herdr and start project sync.', flush=True)
    except Cancelled:
        print('Setup is preserved. Run task work:cs on your Mac to finish connecting.', file=sys.stderr)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f'Setup is preserved. Scratch sync needs attention: {error}\n'
              'Run task work:cs on your Mac to finish connecting.', file=sys.stderr)


SETUP_TITLE = 'Configure project devcontainer'
MERGE_MESSAGE = 'Reviewed and merged with task devcontainer:setup.'


def _open_setup_prs(prs: list[dict]) -> str:
    if len(prs) == 1:
        return f'Setup PR #{prs[0]["number"]} ({prs[0]["head"]["ref"]}) is open. {RERUN}'
    listed = ', '.join(f'#{pr["number"]} ({pr["head"]["ref"]})' for pr in prs)
    return (f'Setup PRs {listed} are open. Merge one and close the others, '
            'then run task devcontainer:setup again to create the Codespace.')


def _launch_on_default(api: Codespaces, project: Path, repo: str, default: str, sha: str, launch: dict | None,
                       preferences: dict, merged: str | None = None, worktree: Path | None = None):
    """Once sha is on <default>: verify it, prune merged setup branches, fast-forward the checkout
    when safe, then create or reuse the codespace on <default>. Returns (codespace, config)."""
    from .project_sync.state import discover_project, setup_worktree_root
    if not default_contains(project, default, sha):
        raise ValueError(f'Cannot confirm that origin/{default} contains the setup merge {sha[:12]}; no codespace was '
                         'created. Check origin, then run task devcontainer:setup again to create the Codespace.')
    # Pruning never blocks the codespace; each failure names what to run by hand.
    paths, root, listed, here = {merged: worktree} if merged else {}, None, [], shlex.quote(str(project))
    try:
        root = setup_worktree_root(discover_project(project))
        listed = merged_setup_branches(project, default)
    except (ValueError, subprocess.CalledProcessError, OSError) as error:
        print(f'Setup: could not list merged setup branches: {error}. Check them with '
              f'git -C {here} branch --list "setup/*"', file=sys.stderr, flush=True)
    for branch in dict.fromkeys([*paths, *listed]) if root else ():
        try:
            remove_setup_worktree(project, paths.get(branch) or root / branch.replace('/', '-'), branch,
                                  remote=True, root=root)
        except ValueError as error:
            print(f'Setup: {error}', file=sys.stderr, flush=True)
        except (subprocess.CalledProcessError, OSError) as error:
            print(f'Setup: could not remove {branch}: {error}. Check it, then run by hand: git -C {here} branch -D '
                  f'{branch} && git -C {here} push origin --delete {branch}', file=sys.stderr, flush=True)
    if not fast_forward_checkout(project, default) and not is_ancestor(project, sha, 'HEAD'):
        # Never a branch switch, a merge into a feature branch or a rebase.
        print(f'Your checkout lacks the new setup files until you merge or pull {default}.', flush=True)
    config = json.loads(git(project, 'show', f'{sha}:.devcontainer/devcontainer.json'))
    if not launch:
        return None, config
    state_home = os.environ.get('XDG_STATE_HOME', '')
    state = Path(state_home) if state_home and Path(state_home).is_absolute() else Path.home() / '.local/state'
    dotfiles = preferences['dotfiles_repo'] if preferences['apply_dotfiles'] else None
    name = api.launch(repo, default, sha, dotfiles_repo=dotfiles, receipt=receipt_path(state, repo, default),
                      workspace=config.get('workspaceFolder'), state_root=state,
                      machine_profile=_machine_profile(project) if dotfiles else None, **launch)
    return name, config


def _merge_and_launch(ui, api: Codespaces, project: Path, repo: str, default: str, number: int, branch: str,
                      head: str, worktree: Path | None, launch: dict | None, preferences: dict):
    """One confirmation, GitHub's merge of the reviewed head, then the codespace on <default>."""
    user = api.user()
    try:
        # Only an explicit yes merges: Enter and Ctrl-C leave the PR open.
        merge = ui.confirm(f'Merge PR #{number} into {default} as {user}? '
                           'Required reviews are bypassed if your account is allowed to.', False)
    except Cancelled:
        merge = False
    if not merge:
        raise ValueError(f'PR #{number} is open. {RERUN}')
    sha = api.merge_pr(repo, number, f'{SETUP_TITLE} (#{number})', MERGE_MESSAGE, head)
    return _launch_on_default(api, project, repo, default, sha, launch, preferences, merged=branch, worktree=worktree)


def _resume_setup_pr(ui, api: Codespaces, project: Path, repo: str, default: str, prs: list[dict],
                     resume: str | None, preferences: dict) -> int:
    """A rerun resumes the one open setup PR instead of opening another."""
    if len(prs) != 1:
        raise ValueError(_open_setup_prs(prs))
    (pr,) = prs
    print(f'Setup PR #{pr["number"]} is open: {pr["title"]} ({pr["head"]["ref"]} into {default}, '
          f'at {pr["head"]["sha"][:12]}).', flush=True)
    try:
        launch = _launch_options(ui, api, project, default, resume)
    except Cancelled:
        launch = None
    if not launch:
        # Commit only means no push and no PR: the open one stays as it is.
        raise ValueError(f'PR #{pr["number"]} is open. {RERUN}')
    launch.pop('repo')
    name, config = _merge_and_launch(ui, api, project, repo, default, pr['number'], pr['head']['ref'],
                                     pr['head']['sha'], None, launch, preferences)
    if config.get('customizations', {}).get('devcontainerSetup', {}).get('selections', {}).get('continuity'):
        setup_continuity(project, ui, name)
    return 0


def interactive(project: Path, found: dict | None, metadata: dict | None, ref: str, resume: str | None,
                upgrade: bool = False) -> int:
    ui, api = Gum(), Codespaces()
    preferences = load_preferences()
    github = _github_repository(project)
    if not github:
        print(f'Note: {NO_GITHUB_ORIGIN} Commit-only setup works without it.', file=sys.stderr, flush=True)
    else:
        base = default_branch(project)
        try:
            pending = api.setup_prs(github, base)
        except ValueError as error:
            # Commit only needs no GitHub; a launch checks again before it pushes.
            pending = []
            print(f'Note: cannot check GitHub for an open setup PR: {error}', file=sys.stderr, flush=True)
        if pending:
            return _resume_setup_pr(ui, api, project, github, base, pending, resume, preferences)
    pull = None
    with setup_checkout(project) as (source, default):
        if default:
            # What reaches the default branch is computed from it, never from this checkout.
            found, metadata = scan(source), _metadata(source)
            _check_revision(metadata, ref, upgrade, default)
        chosen = defaults(found, preferences, metadata)
        while True:
            chosen = _select(ui, found, chosen, preferences)
            branch = git(source, 'branch', '--show-current') if default else proposed_branch(project)
            launch = _launch_options(ui, api, project, default or branch, resume)
            chosen['ssh'] = bool(launch)
            files = render(source, SOURCE, chosen, ref)
            integrate_taskfile(source, files)
            integrate_continuity(source, files, chosen.get('continuity', False))
            check_candidate(files, source)
            # The worktree is on its setup branch already.
            plan = GitPlan(source, files, branch=None if default else branch)
            if not plan.changed:
                plan.branch = plan.start_branch
            origin = f' (from origin/{default})' if default else ''
            marker = json.loads(files['.devcontainer/devcontainer.json'])['customizations']['devcontainerSetup']
            # The PR body: the review without the local path, the dotfiles repository or the launch choices.
            body = [f'Branch: {plan.branch}{origin}', f'Runtimes: {chosen["runtimes"]}',
                    f'Tools: {", ".join(chosen["tools"]) or "baseline only"}',
                    'Agents: Codex, Claude Code, OpenCode (baseline)',
                    f'Files: {", ".join(plan.changed) or "already configured"}',
                    *[f'Dependency: {detail}' for detail in marker.get('dependencies', {}).values()],
                    *[f'Version note: {detail}' for detail in marker.get('pinningLimitations', {}).values()]]
            summary = list(body)
            summary[3] += f'; dotfiles: {preferences["dotfiles_repo"] if chosen["dotfiles"] else "disabled"}'
            print(f'\nProject: {project}\n' + '\n'.join(summary), flush=True)
            if launch:
                print(f'Launch: {launch["machine"]}, idle {launch["idle_timeout"]}, {launch["client"]}', flush=True)
            while True:
                action = ui.choose('Review setup', ['View diff', 'Change answers', 'Continue', 'Cancel'], 'Continue')
                if action == 'View diff':
                    print(plan.diff() or 'No project changes.', flush=True)
                else:
                    break
            if action == 'Change answers':
                continue
            if action == 'Cancel':
                raise Cancelled()
            prompt = (f'Commit setup files, push {plan.branch} and open a PR into {default}?' if launch and plan.changed
                      else f'Create or open the selected codespace on {default}?' if launch
                      else 'Commit these setup files?' if plan.changed else 'Save these personal preferences?')
            if not ui.confirm(prompt):
                raise Cancelled()
            _ensure_runner(ref)
            plan.apply()
            while True:
                try:
                    sha = plan.commit()
                    break
                except ReviewChanged as error:
                    print(error, file=sys.stderr)
                    plan.accept_hook_changes()
                    errors = validate(source)
                    if errors:
                        raise ValueError('Commit hooks changed generated invariants; fix the generator before retrying:\n' + '\n'.join(errors))
                    print(plan.diff(), flush=True)
                    if not ui.confirm('Commit the hook-updated setup files shown above?', False):
                        raise Cancelled()
            print(f'Setup commit: {sha}', flush=True)
            _preferences_after_commit(project, preferences)
            if launch and plan.changed:
                # Never a second setup branch or PR while one is open.
                if open_prs := api.setup_prs(launch['repo'], default):
                    raise ValueError(_open_setup_prs(open_prs))
                plan.push()
                pull = (api.open_pr(launch['repo'], plan.branch, default, SETUP_TITLE, '\n'.join(body)),
                        plan.branch, sha, source)
            break
    name = None
    if launch:
        repo = launch.pop('repo')
        if pull:
            name, _ = _merge_and_launch(ui, api, project, repo, default, *pull, launch, preferences)
        else:
            # Already configured: origin/<default> carries the setup files; nothing is pushed.
            name, _ = _launch_on_default(api, project, repo, default, sha, launch, preferences)
    if chosen.get('continuity'):
        setup_continuity(project, ui, name)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Configure a project devcontainer with a short interactive wizard.')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--scan', action='store_true', help='Print repository recommendations as JSON; no changes')
    mode.add_argument('--dry-run', action='store_true', help='Print the recommended configuration diff; no changes')
    mode.add_argument('--check', action='store_true', help='Validate the installed project configuration')
    parser.add_argument('--upgrade', action='store_true', help='Review an explicit generator-version upgrade')
    parser.add_argument('--upgrade-loaded', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--resume', metavar='CODESPACE', help='Reuse the named codespace after confirmation')
    args = parser.parse_args(argv)
    try:
        project = Path(git(args.project, 'rev-parse', '--show-toplevel')).resolve()
        if args.check:
            errors = validate(project)
            if errors:
                raise ValueError('\n'.join(errors))
            print('Selected devcontainer configuration is valid.')
            return 0
        if args.scan:
            print(json.dumps(scan(project), indent=2))
            return 0
        # With a GitHub origin what reaches main is computed from main: only its worktree is read.
        github = _github_repository(project)
        found, metadata = (None, None) if github else (scan(project), _metadata(project))
        ref, upgrade = source_ref(), args.upgrade or args.upgrade_loaded
        if args.dry_run:
            with setup_checkout(project) as (source, default):
                if default:
                    found, metadata = scan(source), _metadata(source)
                chosen = defaults(found, load_preferences(), metadata)
                conflicts = [str(conflict) for entry in [*found['runtimes'].values(), *found['package_managers'].values()]
                             for conflict in entry['conflicts']]
                if conflicts:
                    raise ValueError('Resolve these recommendations interactively:\n' + '\n'.join(conflicts))
                files = render(source, SOURCE, chosen, ref)
                integrate_taskfile(source, files)
                integrate_continuity(source, files, chosen.get('continuity', False))
                check_candidate(files, source)
                diff = GitPlan(source, files).diff()
            print(diff)
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError('Interactive setup requires a terminal. Use --scan, --dry-run, or --check for read-only commands.')
        if args.upgrade and not args.upgrade_loaded:
            result = upgrade_handoff(project, ref, args.resume)
            if result is not None:
                return result
        if not github:
            _check_revision(metadata, ref, upgrade)
        return interactive(project, found, metadata, ref, args.resume, upgrade=upgrade)
    except Cancelled:
        print('Setup cancelled. No push or codespace creation performed.', file=sys.stderr)
        return 130
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'Setup: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
