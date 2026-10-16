"""Interactive configuration, plus read-only scan/check/dry-run commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from .codespaces import Codespaces
from .discovery import scan
from .git_ops import GitPlan, ReviewChanged, git, proposed_branch
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
        return dict(json.loads(json.dumps(previous)), agents=True)
    managers = {family: entry['selected'] for family, entry in found['package_managers'].items()
                if entry.get('selected')}
    return {'runtimes': {name: entry['version'] or RUNTIME_DEFAULTS[name]
                         for name, entry in found['runtimes'].items() if entry['selected']},
            'tools': [name for name, entry in found['tools'].items() if entry['selected']],
            'agents': True, 'dotfiles': preferences['apply_dotfiles'],
            'ssh': False, 'ports': found['ports'], 'package_managers': managers,
            'package_manager_versions': {family: entry.get('version') or '' for family, entry in found['package_managers'].items()
                                         if entry.get('selected') and (entry.get('version') or
                                             entry.get('versions', {}).get(entry['selected'], {}).get('conflicts'))}, 'polyglot': False}


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
    target = '.devcontainer/Taskfile.yml'
    if target not in text:
        entry = '  devcontainer:\n    taskfile: .devcontainer/Taskfile.yml\n    optional: true\n'
        match = re.search(r'^includes:[ \t]*(?:#.*)?$', text, re.M)
        if match:
            if re.search(r'^  devcontainer:', text[match.end():], re.M):
                raise ValueError('The Taskfile already uses the devcontainer include namespace.')
            text = text[:match.end()] + '\n' + entry.rstrip('\n') + text[match.end():]
        elif re.search(r'^includes:', text, re.M):
            raise ValueError('Expand the inline Taskfile includes mapping before setup; existing tasks were preserved.')
        else:
            text = text.rstrip() + '\n\nincludes:\n' + entry
        files[name] = text.encode()
    ignore = project / '.gitignore'
    ignored = ignore.read_text() if ignore.exists() else ''
    line = '/.devcontainer/devcontainer.env'
    if line not in ignored.splitlines() and '.devcontainer/devcontainer.env' not in ignored.splitlines():
        files['.gitignore'] = (ignored.rstrip() + '\n\n# Personal devcontainer settings\n' + line + '\n').lstrip('\n').encode()


def check_candidate(files: dict[str, bytes], project: Path | None = None) -> None:
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
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
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


def _repository(project: Path) -> str:
    remote = git(project, 'remote', 'get-url', 'origin')
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?', remote)
    if not match:
        raise ValueError('Codespaces launch requires a GitHub origin remote.')
    return match[1]


def _preferences_after_commit(project: Path, preferences: dict) -> None:
    try:
        saved = save_preferences(preferences)
        print(f'Preferences saved: {saved}', flush=True)
        if preferences['apply_dotfiles']:
            path = project / '.devcontainer/devcontainer.env'
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


def _launch_options(ui: Gum, api: Codespaces, project: Path, branch: str, resume: str | None) -> dict | None:
    action = ui.choose('After setup', ['Commit only', 'Commit, push and open in browser',
                                     'Commit, push and open in VS Code'],
                       'Commit, push and open in browser' if resume else 'Commit only')
    if action == 'Commit only':
        return None
    repo = _repository(project)
    if not resume:
        spaces = api.existing(repo, branch)
        if spaces:
            choices = ['Create a new codespace', *[f'Reuse {space["name"]}' for space in spaces]]
            choice = ui.choose('Existing codespaces for this branch', choices)
            if choice.startswith('Reuse '):
                resume = choice.removeprefix('Reuse ')
    if resume:
        settings = api.resource_settings(api.get(resume))
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
    try:
        # Before approval the local branch/configuration may not exist on GitHub.
        # Offer repository machines now; launch rechecks the committed, pushed SHA.
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


def interactive(project: Path, found: dict, metadata: dict, ref: str, resume: str | None) -> int:
    ui, api = Gum(), Codespaces()
    preferences = load_preferences()
    chosen = defaults(found, preferences, metadata)
    while True:
        chosen = _select(ui, found, chosen, preferences)
        branch = proposed_branch(project)
        launch = _launch_options(ui, api, project, branch, resume)
        chosen['ssh'] = bool(launch)
        files = render(project, SOURCE, chosen, ref)
        integrate_taskfile(project, files)
        check_candidate(files, project)
        plan = GitPlan(project, files, branch=branch)
        if not plan.changed:
            plan.branch = plan.start_branch
        print(f'\nProject: {project}\nBranch: {plan.branch}\nRuntimes: {chosen["runtimes"]}'
              f'\nTools: {", ".join(chosen["tools"]) or "baseline only"}'
              f'\nAgents: Codex, Claude Code, OpenCode (baseline); dotfiles: {preferences["dotfiles_repo"] if chosen["dotfiles"] else "disabled"}'
              f'\nFiles: {", ".join(plan.changed) or "already configured"}', flush=True)
        generated_config = json.loads(files['.devcontainer/devcontainer.json'])
        marker = generated_config['customizations']['devcontainerSetup']
        for detail in marker.get('dependencies', {}).values():
            print(f'Dependency: {detail}', flush=True)
        for detail in marker.get('pinningLimitations', {}).values():
            print(f'Version note: {detail}', flush=True)
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
        prompt = (f'Commit setup files, push {plan.branch}, and create/open the selected codespace?'
                  if launch else 'Commit these setup files?' if plan.changed else 'Save these personal preferences?')
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
                errors = validate(project)
                if errors:
                    raise ValueError('Commit hooks changed generated invariants; fix the generator before retrying:\n' + '\n'.join(errors))
                print(plan.diff(), flush=True)
                if not ui.confirm('Commit the hook-updated setup files shown above?', False):
                    raise Cancelled()
        print(f'Setup commit: {sha}', flush=True)
        _preferences_after_commit(project, preferences)
        if launch:
            repo = launch.pop('repo')
            state_home = os.environ.get('XDG_STATE_HOME', '')
            state = Path(state_home) if state_home and Path(state_home).is_absolute() else Path.home() / '.local/state'
            receipt = state / 'devcontainer-setup' / (hashlib.sha256(f'{repo}:{plan.branch}'.encode()).hexdigest()[:16] + '.json')
            config = json.loads(files['.devcontainer/devcontainer.json'])
            api.launch(plan, repo, dotfiles_repo=preferences['dotfiles_repo'] if chosen['dotfiles'] else None,
                       receipt=receipt, workspace=config.get('workspaceFolder'), **launch)
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
        found = scan(project)
        if args.scan:
            print(json.dumps(found, indent=2))
            return 0
        metadata, ref = _metadata(project), source_ref()
        if args.dry_run:
            chosen = defaults(found, load_preferences(), metadata)
            conflicts = [str(conflict) for entry in [*found['runtimes'].values(), *found['package_managers'].values()]
                         for conflict in entry['conflicts']]
            if conflicts:
                raise ValueError('Resolve these recommendations interactively:\n' + '\n'.join(conflicts))
            files = render(project, SOURCE, chosen, ref)
            integrate_taskfile(project, files)
            check_candidate(files, project)
            print(GitPlan(project, files).diff())
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError('Interactive setup requires a terminal. Use --scan, --dry-run, or --check for read-only commands.')
        if args.upgrade and not args.upgrade_loaded:
            result = upgrade_handoff(project, ref, args.resume)
            if result is not None:
                return result
        if metadata and metadata.get('sourceRef') != ref and not (args.upgrade or args.upgrade_loaded):
            raise ValueError('This project uses a different generator revision. Run its installed task, or use --upgrade to review updating it.')
        return interactive(project, found, metadata, ref, args.resume)
    except Cancelled:
        print('Setup cancelled. No push or codespace creation performed.', file=sys.stderr)
        return 130
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'Setup: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
