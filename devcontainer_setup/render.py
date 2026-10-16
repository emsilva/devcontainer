"""Pure, selective generation with recorded ownership and safe updates."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

FEATURE = 'ghcr.io/devcontainers/features/'
BASE_FEATURES = [FEATURE + 'common-utils:2',
                 'ghcr.io/rocker-org/devcontainer-features/apt-packages:1',
                 FEATURE + 'python:1', 'ghcr.io/va-h/devcontainers-features/uv:1',
                 'ghcr.io/devcontainers-extra/features/go-task:1', FEATURE + 'github-cli:1']
TOOLS = {'azure': FEATURE + 'azure-cli:1', 'aws': FEATURE + 'aws-cli:1',
         'gcp': 'ghcr.io/dhoeric/features/google-cloud-cli:1',
         'docker': FEATURE + 'docker-in-docker:2',
         'kubernetes': FEATURE + 'kubectl-helm-minikube:1'}
RUNTIMES = {'python', 'node', 'ruby', 'go', 'rust'}
AGENTS = ['ghcr.io/anthropics/devcontainer-features/claude-code:1.0',
          'ghcr.io/jsburckhardt/devcontainer-features/codex:1']
CONFIG = '.devcontainer/devcontainer.json'
LOCK = '.devcontainer/devcontainer-lock.json'
NAMESPACE = 'devcontainerSetup'
PINNED_OPTIONS = {
    FEATURE + 'aws-cli:1': {'version': '2.38.1'},
    FEATURE + 'azure-cli:1': {'version': '2.91.0'},
    FEATURE + 'github-cli:1': {'version': '2.102.0'},
    FEATURE + 'rust:1': {'version': '1.99.1'},
    FEATURE + 'docker-in-docker:2': {'version': '29.9.1'},
    FEATURE + 'kubectl-helm-minikube:1': {'version': '1.37.1', 'helm': '4.3.1'},
    'ghcr.io/dhoeric/features/google-cloud-cli:1': {'version': '589.0.0'},
    'ghcr.io/va-h/devcontainers-features/uv:1': {'version': '0.13.1'},
    'ghcr.io/devcontainers-extra/features/go-task:1': {'version': '3.54.0'},
}


def _json(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + '\n').encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot read JSON {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return value


def _safe_path(project, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != '.devcontainer':
        raise ValueError(f'Unsafe managed path: {relative}')
    target = project
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError(f'Unsafe symlink at {relative}')
    if target.exists() and not target.is_file():
        raise ValueError(f'Expected managed file but found directory: {relative}')
    return target


def _selections(values):
    selected = {'runtimes': dict(values.get('runtimes', {})),
                'tools': sorted(set(values.get('tools', []))),
                'agents': bool(values.get('agents', True)),
                'dotfiles': bool(values.get('dotfiles', True)),
                'ssh': bool(values.get('ssh', False)),
                'ports': sorted(set(values.get('ports', []))),
                'package_managers': dict(values.get('package_managers', {})),
                'polyglot': bool(values.get('polyglot', False))}
    for runtime, version in selected['runtimes'].items():
        if runtime not in RUNTIMES or not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9_.+-]+', version):
            raise ValueError(f'Choose a concrete supported runtime version: {runtime}={version}')
    unknown = set(selected['tools']) - set(TOOLS) - {'db-clients', 'precommit', 'devtunnel'}
    if unknown:
        raise ValueError(f'Unknown tool bundles: {sorted(unknown)}')
    if any(type(port) is not int or not 1 <= port <= 65535 for port in selected['ports']):
        raise ValueError('Ports must be integers between 1 and 65535')
    for family, manager in selected['package_managers'].items():
        if family not in RUNTIMES or not isinstance(manager, str):
            raise ValueError('Package managers must map runtime families to names')
        supported = {'node': {'npm', 'pnpm', 'yarn', 'bun'}, 'python': {'uv', 'pip', 'poetry', 'pipenv'},
                     'ruby': {'bundler'}, 'go': {'go'}, 'rust': {'cargo'}}
        if manager not in supported[family]:
            raise ValueError(f'Unsupported package manager: {family}/{manager}')
    return selected


def _merge_config(current, previous, desired, path=()):
    """Three-way merge; ports, editor configuration, and project hooks are user-owned."""
    if path == ('forwardPorts',):
        additions = [port for port in current if port not in previous]
        return list(dict.fromkeys(desired + additions))
    if path[:2] == ('customizations', 'vscode'):
        result = copy.deepcopy(desired)
        if isinstance(result, dict) and isinstance(current, dict):
            result.update(copy.deepcopy(current))
            return result
        return copy.deepcopy(current)
    if len(path) == 1 and path[0].endswith('Command'):
        if current == previous:
            return copy.deepcopy(desired)
        result = copy.deepcopy(desired)
        if not isinstance(result, dict):
            result = {NAMESPACE: result}
        if isinstance(current, dict):
            # Only the reserved generator command remains protected.
            if current.get(NAMESPACE) != previous.get(NAMESPACE):
                raise ValueError(f'Modified managed command: {path[0]}.{NAMESPACE}')
            result.update({key: value for key, value in current.items() if key != NAMESPACE})
        else:
            result['project'] = copy.deepcopy(current)
        return result
    if isinstance(previous, dict) and isinstance(current, dict) and isinstance(desired, dict):
        result = {}
        for key in previous.keys() | current.keys() | desired.keys():
            if key not in previous:
                if key in current:
                    if key in desired and current[key] != desired[key]:
                        raise ValueError(f'Conflicting unmanaged configuration: {".".join(path + (key,))}')
                    result[key] = copy.deepcopy(current[key])
                else:
                    result[key] = copy.deepcopy(desired[key])
            elif key not in current:
                if key in desired:
                    raise ValueError(f'Deleted managed configuration: {".".join(path + (key,))}')
            elif key not in desired:
                if current[key] != previous[key]:
                    raise ValueError(f'Modified managed configuration: {".".join(path + (key,))}')
            else:
                result[key] = _merge_config(current[key], previous[key], desired[key], path + (key,))
        return result
    if current != previous and current != desired:
        raise ValueError(f'Modified managed configuration: {".".join(path)}')
    return copy.deepcopy(desired)


def _install_artifact(name, artifact):
    cases = ' '.join(f'{arch}) url="{artifact[arch]["url"]}"; checksum="{artifact[arch]["sha256"]}"; member="{artifact[arch].get("member", "")}" ;;'
                     for arch in ('amd64', 'arm64'))
    extraction = (f'tar -xzf /tmp/{name}.download -C /usr/local/bin {name}' if name == 'chezmoi'
                  else f'install -m 0755 /tmp/{name}.download /usr/local/bin/{name}')
    if name == 'bun':
        extraction = 'unzip -p /tmp/bun.download "$member" > /usr/local/bin/bun; chmod 0755 /usr/local/bin/bun; ln -s /usr/local/bin/bun /usr/local/bin/bunx'
    return (f'RUN set -eu; case "$(dpkg --print-architecture)" in {cases} '
            f'*) echo "Unsupported architecture for {name}" >&2; exit 1 ;; esac; \\\n'
            f'    curl --fail --location --silent --show-error "$url" -o /tmp/{name}.download; \\\n'
            f'    printf "%s  %s\\n" "$checksum" /tmp/{name}.download | sha256sum -c -; \\\n'
            f'    {extraction}; rm /tmp/{name}.download')


def render(project: Path, source: Path, selections: dict, source_ref: str) -> dict[str, bytes]:
    """Return the exact candidate files; never write to the consumer checkout."""
    project, source = Path(project), Path(source)
    if not re.fullmatch(r'[0-9a-f]{40}', source_ref):
        raise ValueError('source revision must be an immutable full 40-hex Git commit')
    selected = _selections(selections)
    target = _safe_path(project, CONFIG)
    current = _read(target) if target.exists() else {}
    marker = current.get('customizations', {}).get(NAMESPACE)
    if current and not marker and not selected['polyglot']:
        raise ValueError('Existing unmanaged devcontainer requires explicit reviewed migration or the polyglot preset')
    if marker:
        if marker.get('version') != 1 or not isinstance(marker.get('managedConfig'), dict):
            raise ValueError('Unsupported or malformed managed devcontainer metadata')
        for relative, digest in marker.get('managedFiles', {}).items():
            managed = _safe_path(project, relative)
            if not managed.is_file() or _digest(managed.read_bytes()) != digest:
                raise ValueError(f'Modified or missing managed file: {relative}')
    canonical = _read(source / CONFIG)
    catalog = canonical['features']
    catalog_lock = _read(source / LOCK)['features']
    names = set(catalog if selected['polyglot'] else BASE_FEATURES)
    runtimes = dict(selected['runtimes'])
    if selected['agents']:
        names.update(AGENTS)
        runtimes.setdefault('node', catalog[FEATURE + 'node:1']['version'])
    for runtime in selected['package_managers']:
        runtimes.setdefault(runtime, catalog[FEATURE + runtime + ':1']['version'])
    if selected['agents']:
        node_major = re.match(r'^(\d+)', runtimes['node'])
        if not node_major or int(node_major.group(1)) < 18:
            raise ValueError('Agent tools require Node 18 or newer; choose a compatible Node version')
    names.update(FEATURE + runtime + ':1' for runtime in runtimes)
    names.update(TOOLS[tool] for tool in selected['tools'] if tool in TOOLS)
    if selected['ssh']:
        names.add(FEATURE + 'sshd:1')
    # Resolve real dependency IDs, keeping lock keys in their original feature-ID form.
    pending = list(names)
    while pending:
        name = pending.pop()
        if name not in catalog_lock:
            raise ValueError(f'Missing canonical feature lock: {name}')
        for dependency in catalog_lock[name].get('dependsOn', []):
            if dependency not in names:
                names.add(dependency)
                pending.append(dependency)
    features = {name: copy.deepcopy(catalog.get(name, {})) for name in sorted(names)}
    for name in features:
        features[name].update(PINNED_OPTIONS.get(name, {}))
    apt = 'git curl jq ripgrep procps lsof iproute2 iputils-ping ca-certificates'
    if selected['polyglot']:
        apt = catalog['ghcr.io/rocker-org/devcontainer-features/apt-packages:1']['packages']
    elif 'db-clients' in selected['tools']:
        apt += ' postgresql-client default-mysql-client sqlite3 redis-tools'
    features['ghcr.io/rocker-org/devcontainer-features/apt-packages:1']['packages'] = apt
    features[FEATURE + 'python:1']['installTools'] = False
    for runtime, version in runtimes.items():
        features[FEATURE + runtime + ':1']['version'] = version
    if FEATURE + 'node:1' in features:
        manager = selected['package_managers'].get('node', 'npm')
        features[FEATURE + 'node:1'].update(installYarn=manager == 'yarn',
                                         pnpmVersion='12.11.2' if manager == 'pnpm' else 'none')
    desired = {'name': 'Project development environment', 'image': canonical['image'],
               'features': features, 'remoteUser': 'vscode',
               'containerEnv': {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'},
               'customizations': {'vscode': {'settings': {'editor.formatOnSave': True}}},
               'forwardPorts': sorted(set(selected['ports'] + ([2222] if selected['ssh'] else []))),
               'postCreateCommand': {NAMESPACE: ['bash', '.devcontainer/scripts/post-create-selected.sh']},
               'waitFor': 'postCreateCommand'}
    templates = source / 'templates'
    files = {}
    commands = ['python3', 'uv', 'uvx', 'git', 'task', 'curl', 'jq', 'rg', 'gh']
    runtime_commands = {'node': ['node', 'npm'], 'ruby': ['ruby', 'bundle'], 'go': ['go'], 'rust': ['rustc', 'cargo']}
    for runtime, bins in runtime_commands.items():
        if FEATURE + runtime + ':1' in names:
            commands.extend(bins)
    tool_commands = {'azure': ['az'], 'aws': ['aws'], 'gcp': ['gcloud'],
                     'docker': ['docker'], 'devtunnel': ['devtunnel'], 'precommit': ['pre-commit'],
                     'kubernetes': ['kubectl', 'helm'], 'db-clients': ['psql', 'mysql', 'sqlite3', 'redis-cli']}
    effective_tools = set(selected['tools'])
    effective_tools.update(tool for tool, feature in TOOLS.items() if feature in names)
    if selected['polyglot']:
        effective_tools.add('db-clients')
    for tool in sorted(effective_tools):
        commands.extend(tool_commands[tool])
    if selected['agents'] or selected['polyglot']:
        commands.extend(['claude', 'codex'])
    commands.extend(selected['package_managers'].values())
    if selected['dotfiles']:
        commands.extend(['chezmoi', 'load-dotfiles'])
    replacements = {'__SOURCE_REF__': source_ref, '__TOOLS__': ' '.join(dict.fromkeys(commands)),
                    '__DOTFILES__': 'if [ -n "${DOTFILES_REPO:-}" ]; then\n  bash .devcontainer/scripts/apply-dotfiles.sh "$DOTFILES_REPO"\nfi' if selected['dotfiles'] else ''}
    helper_names = ['setup-wizard.sh', 'validate-selected.sh', 'post-create-selected.sh']
    if selected['dotfiles']:
        helper_names.append('apply-dotfiles.sh')
    for name in helper_names:
        content = (templates / 'scripts' / name).read_text()
        for old, new in replacements.items():
            content = content.replace(old, new)
        files['.devcontainer/scripts/' + name] = content.encode()
    files['.devcontainer/Taskfile.yml'] = (templates / 'Taskfile.yml').read_bytes()
    artifacts = _read(templates / 'artifacts.json')
    installs = []
    if selected['dotfiles']:
        installs.append(_install_artifact('chezmoi', artifacts['chezmoi']))
        installs.append('COPY scripts/apply-dotfiles.sh /usr/local/bin/load-dotfiles\nRUN chmod 0755 /usr/local/bin/load-dotfiles')
    if 'devtunnel' in selected['tools']:
        installs.append(_install_artifact('devtunnel', artifacts['devtunnel']))
    python_tools = {}
    if 'precommit' in selected['tools']:
        python_tools['pre-commit'] = '4.7.0'
    manager = selected['package_managers'].get('python')
    if manager in ('poetry', 'pipenv'):
        python_tools[manager] = {'poetry': '2.5.1', 'pipenv': '2026.9.1'}[manager]
    if python_tools:
        installs.append('RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv && rm -rf /var/lib/apt/lists/*')
        for package, version in python_tools.items():
            installs.append(f'RUN python3 -m venv /opt/{package} && /opt/{package}/bin/pip install --no-cache-dir {package}=={version} && ln -s /opt/{package}/bin/{package} /usr/local/bin/{package}')
    if selected['package_managers'].get('node') == 'bun':
        installs.append('RUN apt-get update && apt-get install -y --no-install-recommends unzip && rm -rf /var/lib/apt/lists/*')
        installs.append(_install_artifact('bun', artifacts['bun']))
    if installs:
        dockerfile = (templates / 'Dockerfile').read_text().replace('__BASE_IMAGE__', canonical['image']).replace('__INSTALLS__', '\n'.join(installs))
        files['.devcontainer/Dockerfile'] = dockerfile.encode()
        desired.pop('image')
        desired['build'] = {'dockerfile': 'Dockerfile'}
    current_without_marker = copy.deepcopy(current)
    current_without_marker.get('customizations', {}).pop(NAMESPACE, None)
    if marker:
        config = _merge_config(current_without_marker, marker['managedConfig'], desired)
    elif current:
        config = copy.deepcopy(desired)
        # Explicit polyglot adoption preserves the existing environment and hooks.
        config.update(copy.deepcopy(current_without_marker))
        config['features'] = dict(features, **current.get('features', {}))
        for key in ('postCreateCommand',):
            old_command = current.get(key)
            if old_command:
                user_commands = old_command if isinstance(old_command, dict) else {'project': old_command}
                config[key] = dict(user_commands, **desired[key])
        if installs and ('build' in current or current.get('image') != canonical['image']):
            raise ValueError('Build-time extras require reviewed migration of the existing custom image/build')
    else:
        config = desired
    # Reuse existing resolved feature records for ordinary reruns, including custom polyglot features.
    existing_lock = _read(_safe_path(project, LOCK)).get('features', {}) if (project / LOCK).exists() else {}
    locked = {}
    pending = list(config['features'])
    while pending:
        name = pending.pop()
        if name in locked:
            continue
        entry = existing_lock.get(name, catalog_lock.get(name))
        if not entry or not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', entry.get('resolved', '')):
            raise ValueError(f'Missing resolved lock for selected feature: {name}')
        locked[name] = copy.deepcopy(entry)
        pending.extend(entry.get('dependsOn', []))
    files[LOCK] = _json({'features': {name: locked[name] for name in sorted(locked)}})
    for relative, data in files.items():
        destination = _safe_path(project, relative)
        if destination.exists() and not (marker and relative in marker.get('managedFiles', {})):
            if relative == LOCK:
                continue
            if destination.read_bytes() != data:
                raise ValueError(f'Existing unmanaged file requires reviewed migration: {relative}')
    # Obsolete files stay untouched and remain protected; cleanup is a separate explicit review.
    managed_files = dict(marker.get('managedFiles', {})) if marker else {}
    managed_files.update({relative: _digest(data) for relative, data in files.items()})
    snapshot = copy.deepcopy(desired)
    if current and not marker:
        snapshot = copy.deepcopy(config)
    config.setdefault('customizations', {})[NAMESPACE] = {
        'version': 1, 'sourceRef': source_ref, 'selections': selected,
        'dependencies': {'node': 'Coding-agent tools use Node' } if selected['agents'] else {},
        'managedConfig': snapshot, 'managedFiles': dict(sorted(managed_files.items()))}
    files[CONFIG] = _json(config)
    return files


def validate(project: Path) -> list[str]:
    """Validate selected feature resolution, ownership, and local helper references."""
    project = Path(project)
    errors = []
    try:
        config = _read(_safe_path(project, CONFIG))
        lock = _read(_safe_path(project, LOCK)).get('features', {})
        marker = config.get('customizations', {}).get(NAMESPACE)
        if not isinstance(marker, dict) or marker.get('version') != 1:
            return ['Missing supported customizations.devcontainerSetup metadata']
        if not re.fullmatch(r'[0-9a-f]{40}', marker.get('sourceRef', '')):
            errors.append('Generator sourceRef must be an immutable commit')
        selected = _selections(marker.get('selections', {}))
        features = config.get('features', {})
        required = set(BASE_FEATURES)
        required.update(FEATURE + runtime + ':1' for runtime in selected['runtimes'])
        required.update(TOOLS[tool] for tool in selected['tools'] if tool in TOOLS)
        if selected['agents']:
            required.update(AGENTS + [FEATURE + 'node:1'])
        if selected['ssh']:
            required.add(FEATURE + 'sshd:1')
        for name in sorted(required - set(features)):
            errors.append(f'Missing selected feature: {name}')
        for name in features.keys() | lock.keys():
            if name not in lock:
                errors.append(f'Missing feature lock: {name}')
                continue
            entry = lock[name]
            if not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', entry.get('resolved', '')):
                errors.append(f'Invalid resolved feature digest: {name}')
            for dependency in entry.get('dependsOn', []):
                if dependency not in lock:
                    errors.append(f'Missing transitive feature lock: {dependency}')
        for relative, digest in marker.get('managedFiles', {}).items():
            target = _safe_path(project, relative)
            if not target.is_file():
                errors.append(f'Missing managed helper/file: {relative}')
            elif _digest(target.read_bytes()) != digest:
                errors.append(f'Modified managed helper/file: {relative}')
        for required_file in ['.devcontainer/scripts/validate-selected.sh', '.devcontainer/scripts/post-create-selected.sh', '.devcontainer/scripts/setup-wizard.sh']:
            if required_file not in marker.get('managedFiles', {}):
                errors.append(f'Missing managed script registration: {required_file}')
        if selected['dotfiles'] and not (project / '.devcontainer/scripts/apply-dotfiles.sh').is_file():
            errors.append('Missing dotfiles helper')
        if (selected['dotfiles'] or set(selected['tools']) & {'devtunnel', 'precommit'}
                or set(selected['package_managers'].values()) & {'poetry', 'pipenv', 'bun'}):
            if not (project / '.devcontainer/Dockerfile').is_file() or config.get('build', {}).get('dockerfile') != 'Dockerfile':
                errors.append('Missing selected build-time installation Dockerfile')
        actual = copy.deepcopy(config)
        actual.get('customizations', {}).pop(NAMESPACE, None)
        _merge_config(actual, marker['managedConfig'], marker['managedConfig'])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(str(exc))
    return errors
