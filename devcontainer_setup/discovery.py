"""Read repository signals without executing project code or following symlinks."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import subprocess
import tomllib

RUNTIMES = ('python', 'node', 'ruby', 'go', 'rust')
TOOLS = ('azure', 'aws', 'gcp', 'docker', 'devtunnel', 'precommit', 'kubernetes')
DISCOUNTED = {'vendor', 'vendors', 'generated', 'examples', 'example', 'fixtures', 'fixture', 'testdata', 'docs', 'node_modules', '.venv', 'venv', '.bundle', 'target', 'dist', 'build', '__pycache__', '.devcontainer', '.git'}
MAX_FILE_BYTES = 512 * 1024


def _version(value: str) -> tuple[int, ...]:
    match = re.match(r'v?(\d+(?:\.\d+)*)', value.strip())
    return tuple(map(int, match[1].split('.'))) if match else ()


def _compatible(version: str, declaration: str) -> bool:
    """Compare common numeric pins and constraints; unknown syntax needs review."""
    candidate = _version(version)
    for alternative in declaration.split('||'):
        if re.sub(r'(>=|<=|==|!=|~=|\^|~|>|<|=)?\s*v?\d+(?:\.(?:\d+|[xX*]))*|[,\s]', '', alternative):
            continue
        terms = re.findall(r'(>=|<=|==|!=|~=|\^|~|>|<|=)?\s*v?(\d+(?:\.(?:\d+|[xX*]))*)', alternative)
        if not terms:
            continue
        valid = True
        for op, raw in terms:
            expected = tuple(int(x) for x in raw.split('.') if x.isdigit())
            a = candidate + (0,) * max(0, len(expected) - len(candidate))
            b = expected + (0,) * max(0, len(candidate) - len(expected))
            if op in ('', '=', '=='):
                okay = candidate[:len(expected)] == expected
            elif op == '>=': okay = a >= b
            elif op == '<=': okay = a <= b
            elif op == '>': okay = a > b
            elif op == '<': okay = a < b
            elif op == '!=': okay = candidate[:len(expected)] != expected
            else:
                if op == '^':
                    index = next((i for i, part in enumerate(expected) if part), len(expected) - 1)
                elif op == '~':
                    index = min(1, len(expected) - 1)
                else:  # Python ~= keeps every specified component except the last.
                    index = max(0, len(expected) - 2)
                upper = expected[:index] + (expected[index] + 1,)
                padded_upper = upper + (0,) * max(0, len(candidate) - len(upper))
                okay = a >= b and a < padded_upper
            valid = valid and okay
        if valid:
            return True
    return False


def _choose(declarations: list[tuple[int, str, str]], baseline: str | None) -> tuple[str | None, list[str]]:
    if not declarations:
        return baseline, []
    ordered = sorted(declarations, key=lambda x: (-x[0], x[2]))
    candidates = []
    for _, declaration, _ in ordered:
        for match in re.finditer(r'(?<![\d.])v?(\d+(?:\.\d+)*)', declaration):
            if match.group(1) not in candidates:
                candidates.append(match.group(1))
    # Strict lower bounds and excluded numeric pins need interior candidates.
    # Try numeric successors after literal candidates, preserving explicit pins.
    for literal in candidates.copy():
        parts = _version(literal)
        for index in range(len(parts) - 1, -1, -1):
            successor = '.'.join(map(str, parts[:index] + (parts[index] + 1,)))
            if successor not in candidates:
                candidates.append(successor)
        if len(parts) < 3:
            patch_release = parts + (0,) * (3 - len(parts))
            patch_release = patch_release[:-1] + (patch_release[-1] + 1,)
            successor = '.'.join(map(str, patch_release))
            if successor not in candidates:
                candidates.append(successor)
    if baseline and baseline not in candidates:
        candidates.append(baseline)
    for candidate in candidates:
        if all(_compatible(candidate, value) for _, value, _ in declarations):
            return candidate, []
    return None, [f'{path}: {value}' for _, value, path in ordered]


def _without_printed_help(text: str) -> str:
    """Exclude echo/printf payloads while retaining following shell commands."""
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(('#', 'desc:', 'description:')):
            continue
        scalar = re.fullmatch(r'''\s*(?:-\s+|(?:run|cmd):\s*)(['"])(.*)\1\s*''', line)
        if scalar:
            # YAML scalar quotes surround shell syntax; remove that outer layer
            # before looking for echo/printf command tokens.
            line = scalar[2]
            if scalar[1] == "'":
                line = line.replace("''", "'")
        try:
            lexer = shlex.shlex(line, posix=False, punctuation_chars=';&|')
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            # A shell/YAML multiline quote cannot be parsed on this line alone.
            # Discard a printing tail rather than interpreting its prose.
            lines.append(re.split(r'\b(?:echo|printf)\s+', line, maxsplit=1)[0])
            continue
        kept = []
        printing = False
        for token in tokens:
            if token in {';', '&&', '||', '|', '&'}:
                printing = False
            if token in {'echo', 'printf'}:
                printing = True
            if not printing:
                kept.append(token)
        lines.append(' '.join(kept))
    return '\n'.join(lines)


def scan(project: Path) -> dict:
    project = Path(project).resolve()
    proc = subprocess.run(['git', '-C', str(project), 'ls-files', '-cz', '--others', '--exclude-standard'], capture_output=True)
    if proc.returncode:
        raise ValueError(f'Cannot inventory Git project {project}: {proc.stderr.decode(errors="replace").strip()}')
    found = {
        'runtimes': {name: {'selected': name == 'python', 'version': '3.13' if name == 'python' else None, 'evidence': [], 'conflicts': []} for name in RUNTIMES},
        'tools': {name: {'selected': False, 'evidence': []} for name in TOOLS},
        'ports': [], 'package_managers': {}, 'warnings': [],
    }
    found['runtimes']['python']['evidence'].append('Baseline Python 3.13 and uv for agent scratch environments')
    declarations = {name: [] for name in RUNTIMES}
    sources = {name: [] for name in RUNTIMES}
    managers: dict[str, dict[str, list[str]]] = {}
    manager_declarations = {}
    ports = set()
    command_managers: dict[str, list[str]] = {}

    def runtime(name, path, reason, version=None, priority=2):
        row = found['runtimes'][name]
        row['selected'] = True
        row['evidence'].append(f'{path}: {reason}')
        if isinstance(version, (str, int, float)) and str(version).strip():
            declarations[name].append((priority, str(version).strip(), path))

    def tool(name, path, reason):
        found['tools'][name]['selected'] = True
        evidence = f'{path}: {reason}'
        if evidence not in found['tools'][name]['evidence']:
            found['tools'][name]['evidence'].append(evidence)

    def manager(family, name, path, version=None):
        evidence = f'{path}: {name}' + (f'@{version}' if version else '')
        managers.setdefault(family, {}).setdefault(name, []).append(evidence)
        if version:
            manager_declarations.setdefault((family, name), []).append((3, str(version), path))

    names = sorted(set(proc.stdout.decode('utf-8', errors='surrogateescape').split('\0')) - {''})
    for relative in names:
        path = Path(relative)
        if any(part.lower() in DISCOUNTED for part in path.parts):
            continue
        full = project / path
        if any(part.is_symlink() for part in (full, *full.parents)):
            continue
        base = path.name
        compose_file = bool(re.fullmatch(r'(?:docker-compose|compose)(?:[.-][\w-]+)?\.ya?ml', base))
        container_file = base.startswith('Dockerfile') or compose_file
        suffix_runtime = {'.py': 'python', '.rb': 'ruby', '.go': 'go', '.js': 'node', '.jsx': 'node', '.ts': 'node', '.tsx': 'node', '.rs': 'rust'}.get(path.suffix)
        if suffix_runtime:
            sources[suffix_runtime].append(relative)
        lockfiles = {
            'package-lock.json': ('node', 'npm'), 'pnpm-lock.yaml': ('node', 'pnpm'),
            'yarn.lock': ('node', 'yarn'), 'bun.lock': ('node', 'bun'), 'bun.lockb': ('node', 'bun'),
            'uv.lock': ('python', 'uv'), 'poetry.lock': ('python', 'poetry'), 'Pipfile.lock': ('python', 'pipenv'),
            'Gemfile.lock': ('ruby', 'bundler'), 'Cargo.lock': ('rust', 'cargo'),
        }
        if base in lockfiles:
            family, package_manager = lockfiles[base]
            runtime(family, relative, f'{family} lockfile')
            manager(family, package_manager, relative)
            continue
        relevant = (base in {'package.json', 'pyproject.toml', 'Pipfile', 'Gemfile', 'Gemfile.lock', 'go.mod', 'go.work', 'go.sum', 'Cargo.toml', 'Cargo.lock', '.python-version', '.ruby-version', '.node-version', '.nvmrc', '.tool-versions', '.gitignore', '.pre-commit-config.yaml', '.pre-commit-config.yml', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock', 'bun.lockb', 'uv.lock', 'poetry.lock', 'Pipfile.lock'} or base.startswith(('requirements', 'Dockerfile', 'Taskfile')) or container_file or base.endswith('.gemspec') or path.suffix in {'.sh', '.bash'} or relative.startswith('.github/workflows/'))
        if not relevant:
            continue
        try:
            with full.open('rb') as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                found['warnings'].append(f'{relative}: skipped file larger than {MAX_FILE_BYTES} bytes')
                continue
            text = raw.decode('utf-8')
        except (OSError, UnicodeError) as error:
            found['warnings'].append(f'{relative}: cannot read text ({error})')
            continue
        try:
            if base == 'package.json':
                data = json.loads(text)
                if not isinstance(data, dict): raise ValueError('manifest must be an object')
                engines = data.get('engines', {})
                runtime('node', relative, 'Node manifest' + (f' engines.node={engines["node"]}' if 'node' in engines else ''), engines.get('node'))
                pm = data.get('packageManager')
                if isinstance(pm, str) and pm.split('@')[0] in {'npm', 'pnpm', 'yarn', 'bun'}:
                    name, _, version = pm.partition('@')
                    manager('node', name, relative + ' packageManager', version.split('+sha', 1)[0])
                for name in ('npm', 'pnpm', 'yarn', 'bun'):
                    if engines.get(name):
                        manager('node', name, relative + ' engines.' + name, str(engines[name]))
                text = '\n'.join(str(x) for x in data.get('scripts', {}).values())
            elif base == 'pyproject.toml':
                data = tomllib.loads(text)
                declared = data.get('project', {}).get('requires-python')
                if not declared:
                    declared = data.get('tool', {}).get('poetry', {}).get('dependencies', {}).get('python')
                runtime('python', relative, f'Python manifest {declared or ""}', declared)
                if 'poetry' in data.get('tool', {}): manager('python', 'poetry', relative)
            elif base == 'Cargo.toml':
                data = tomllib.loads(text)
                runtime('rust', relative, 'Rust manifest', data.get('package', {}).get('rust-version'))
                manager('rust', 'cargo', relative)
            elif base in {'.python-version', '.ruby-version', '.node-version', '.nvmrc'}:
                name = {'p': 'python', 'r': 'ruby', 'n': 'node'}[base.lstrip('.')[0]]
                runtime(name, relative, f'explicit pin {text.strip()}', text.strip(), 3)
            elif base == '.tool-versions':
                for name, version in re.findall(r'^(python|nodejs|ruby|golang|rust)\s+(\S+)', text, re.M):
                    runtime({'nodejs': 'node', 'golang': 'go'}.get(name, name), relative, f'explicit pin {version}', version, 3)
            elif base in {'go.mod', 'go.work', 'go.sum'}:
                match = re.search(r'^go\s+([\d.]+)', text, re.M)
                runtime('go', relative, 'Go module/workspace minimum', '>=' + match[1] if match else None)
            elif base in {'Gemfile', 'Gemfile.lock'} or base.endswith('.gemspec'):
                match = re.search(r'\bruby\s+[\'"]([^\'"]+)', text)
                runtime('ruby', relative, 'Ruby manifest/lock', match[1] if match else None)
                manager('ruby', 'bundler', relative)
            elif base == 'Pipfile':
                data = tomllib.loads(text)
                requires = data.get('requires', {})
                runtime('python', relative, 'Pipfile Python dependencies')
                for key in ('python_version', 'python_full_version'):
                    if key in requires:
                        runtime('python', relative, f'Pipfile {key}={requires[key]}', requires[key], 3)
                manager('python', 'pipenv', relative)
            elif base.startswith('requirements') or base in {'uv.lock', 'poetry.lock', 'Pipfile.lock'}:
                runtime('python', relative, 'Python dependencies')
                manager('python', {'uv.lock': 'uv', 'poetry.lock': 'poetry', 'Pipfile': 'pipenv', 'Pipfile.lock': 'pipenv'}.get(base, 'pip'), relative)
            elif base in {'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock', 'bun.lockb'}:
                runtime('node', relative, 'Node lockfile')
                manager('node', {'package-lock.json': 'npm', 'pnpm-lock.yaml': 'pnpm', 'yarn.lock': 'yarn', 'bun.lock': 'bun', 'bun.lockb': 'bun'}[base], relative)
            elif base == 'Cargo.lock':
                runtime('rust', relative, 'Rust lockfile')
                manager('rust', 'cargo', relative)
            elif base == '.gitignore':
                for hint in ('.venv', 'node_modules', '.bundle'):
                    if hint in text:
                        found['warnings'].append(f'{relative}: weak ignore hint {hint}; does not select a runtime')
            if base.startswith('.pre-commit-config.'):
                tool('precommit', relative, 'pre-commit configuration')
        except (ValueError, TypeError, AttributeError) as error:
            found['warnings'].append(f'{relative}: malformed manifest ({error})')
            continue

        workflow = base.startswith('Taskfile') or container_file or path.suffix in {'.sh', '.bash'} or relative.startswith('.github/workflows/') or base == 'package.json'
        if not workflow:
            continue
        active = _without_printed_help(text)
        commands = {'python': r'python(?:3(?:\.\d+)?)?\s+(?:-m|[\w./-]+\.py)', 'node': r'(?:node\s+[\w./-]+|(?:npm|pnpm|yarn|bun)\s+(?:run|install|ci|build|test|dev|start))', 'ruby': r'(?:ruby\s+[\w./-]+\.rb|bundle\s+(?:exec|install)|rails\s+\w+)', 'go': r'go\s+(?:build|test|run|mod|generate)\b', 'rust': r'cargo\s+(?:build|test|run|check|clippy)\b'}
        for name, pattern in commands.items():
            if re.search(r'(?:^|[\s;&|(\"\'])' + pattern, active, re.M):
                runtime(name, relative, f'{name} build/run command')
        for match in re.finditer(r'(?:^|[\s;&|(\"\'])(npm|pnpm|yarn|bun)\s+(?:run|install|ci|build|test|dev|start)\b', active, re.M):
            command_managers.setdefault(match[1], []).append(f'{relative}: {match[1]} workflow command')
        # CI setup actions and Docker image tags are declarative runtime evidence.
        for name, key in [('node', 'node'), ('python', 'python'), ('ruby', 'ruby'), ('go', 'go'), ('rust', 'rust')]:
            for match in re.finditer(rf'\b{key}-version:\s*[\'"]?([^\s\'"#]+)', active):
                runtime(name, relative, f'CI {key}-version {match[1]}', match[1])
            if re.search(rf'uses:\s*(?:actions/setup-{key}|ruby/setup-ruby|dtolnay/rust-toolchain)', active) and name == key:
                # Only the matching setup action selects this runtime.
                expected = {'ruby': r'ruby/setup-ruby', 'rust': r'dtolnay/rust-toolchain'}.get(name, rf'actions/setup-{key}')
                if re.search(rf'uses:\s*{expected}', active): runtime(name, relative, f'CI setup-{name}')
        for match in re.finditer(r'^\s*FROM\s+(?:--platform=\S+\s+)?(?:[\w.-]+/)*(node|python|ruby|golang|rust):([^\s]+)(?:\s+AS\s+(\S+))?', active, re.M | re.I):
            image, tag, stage = match.groups()
            version = re.match(r'v?\d+(?:\.\d+)*', tag)
            runtime({'golang': 'go'}.get(image.lower(), image.lower()), relative, f'Docker {stage or "runtime"} stage {image}:{tag}', version[0] if version else None)
        patterns = {'azure': r'(?:^|[\s;&|("\'])az\s+[a-z]|uses:\s*azure/', 'aws': r'(?:^|[\s;&|("\'])aws\s+[a-z]|uses:\s*aws-actions/', 'gcp': r'(?:^|[\s;&|("\'])gcloud\s+[a-z]|uses:\s*google-github-actions/', 'docker': r'(?:^|[\s;&|("\'])docker(?:-compose)?\s+[a-z]|uses:\s*docker/', 'devtunnel': r'(?:^|[\s;&|("\'])devtunnel\s+[a-z]', 'precommit': r'(?:^|[\s;&|("\'])pre-commit\s+[a-z]', 'kubernetes': r'(?:^|[\s;&|("\'])kubectl\s+[a-z]'}
        for name, pattern in patterns.items():
            if re.search(pattern, active, re.M): tool(name, relative, 'declared workflow command/action')
        if container_file: tool('docker', relative, 'container workflow')
        port_patterns = [(pattern, active) for pattern in (r'^\s*EXPOSE\s+(\d+)', r'\blsof\b[^\n]*?(?i:tcp):(\d+)', r'(?<![\w-])(?:[A-Z_]*PORT)\s*[:=]\s*[\'"]?(\d+)', r'--port(?:=|\s+)(\d+)')]
        if compose_file:
            # Published host ports, read before the printed-help filter unquotes list scalars:
            # short syntax [ip:]host:container, or long syntax published:.
            port_patterns += [(r'^\s*-\s*[\'"]?(?:[\d.]+:)?(\d+):\d+', text), (r'^\s*published:\s*[\'"]?(\d+)', text)]
        for pattern, source in port_patterns:
            for value in re.findall(pattern, source, re.M):
                port = int(value)
                if 0 < port < 65536: ports.add(port)
    for name in RUNTIMES:
        if len(sources[name]) >= 2:
            found['runtimes'][name]['selected'] = True
        found['runtimes'][name]['evidence'].extend(f'{path}: supporting {name} source' for path in sources[name][:5])
        version, conflicts = _choose(declarations[name], '3.13' if name == 'python' else None)
        found['runtimes'][name]['version'] = version
        found['runtimes'][name]['conflicts'] = conflicts
    if 'node' not in managers and command_managers:
        managers['node'] = command_managers
    for family, choices in managers.items():
        versions = {}
        for name in choices:
            declarations_for_manager = manager_declarations.get((family, name), [])
            version, conflicts = _choose(declarations_for_manager, None)
            if declarations_for_manager and (not version or not re.fullmatch(r'\d+\.\d+\.\d+', version)):
                version = None
                conflicts = [f'{path}: {value}' for _, value, path in declarations_for_manager]
            versions[name] = {'version': version, 'conflicts': conflicts}
        selected = next(iter(choices)) if len(choices) == 1 else None
        found['package_managers'][family] = {
            'selected': selected, 'version': versions[selected]['version'] if selected else None,
            'versions': versions, 'evidence': [item for values in choices.values() for item in values],
            'conflicts': [f'{family}: conflicting package managers: {", ".join(sorted(choices))}'] if len(choices) > 1 else []}
    found['ports'] = sorted(ports)
    return found
