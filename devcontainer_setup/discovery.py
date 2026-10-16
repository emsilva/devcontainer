"""Read repository signals without executing project code or following symlinks."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib

RUNTIMES = ('python', 'node', 'ruby', 'go', 'rust')
TOOLS = ('azure', 'aws', 'gcp', 'docker', 'devtunnel', 'precommit', 'kubernetes')
DISCOUNTED = {'vendor', 'vendors', 'generated', 'examples', 'example', 'fixtures', 'fixture', 'testdata', 'node_modules', '.venv', 'venv', '.bundle', 'target', 'dist', 'build', '__pycache__', '.devcontainer', '.git'}
MAX_FILE_BYTES = 512 * 1024


def _version(value: str) -> tuple[int, ...]:
    match = re.match(r'v?(\d+(?:\.\d+)*)', value.strip())
    return tuple(map(int, match[1].split('.'))) if match else ()


def _compatible(version: str, declaration: str) -> bool:
    """Compare common numeric pins and constraints; unknown syntax needs review."""
    candidate = _version(version)
    for alternative in declaration.split('||'):
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
            elif op == '^': okay = a >= b and candidate[:1] == expected[:1]
            else: okay = a >= b and candidate[:max(1, len(expected) - 1)] == expected[:max(1, len(expected) - 1)]
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
    if baseline and baseline not in candidates:
        candidates.append(baseline)
    for candidate in candidates:
        if all(_compatible(candidate, value) for _, value, _ in declarations):
            return candidate, []
    return None, [f'{path}: {value}' for _, value, path in ordered]


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
    ports = set()

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

    def manager(family, name, path):
        managers.setdefault(family, {}).setdefault(name, []).append(f'{path}: {name}')

    names = sorted(set(proc.stdout.decode('utf-8', errors='surrogateescape').split('\0')) - {''})
    for relative in names:
        path = Path(relative)
        if any(part.lower() in DISCOUNTED for part in path.parts):
            continue
        full = project / path
        if any(part.is_symlink() for part in (full, *full.parents)):
            continue
        base = path.name
        suffix_runtime = {'.py': 'python', '.rb': 'ruby', '.go': 'go', '.js': 'node', '.jsx': 'node', '.ts': 'node', '.tsx': 'node', '.rs': 'rust'}.get(path.suffix)
        if suffix_runtime:
            sources[suffix_runtime].append(relative)
        relevant = (base in {'package.json', 'pyproject.toml', 'Pipfile', 'Gemfile', 'Gemfile.lock', 'go.mod', 'go.work', 'go.sum', 'Cargo.toml', 'Cargo.lock', '.python-version', '.ruby-version', '.node-version', '.nvmrc', '.tool-versions', '.gitignore', '.pre-commit-config.yaml', '.pre-commit-config.yml', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock', 'bun.lockb', 'uv.lock', 'poetry.lock', 'Pipfile.lock'} or base.startswith(('requirements', 'Dockerfile', 'Taskfile', 'docker-compose', 'compose.')) or base.endswith('.gemspec') or path.suffix in {'.sh', '.bash'} or relative.startswith('.github/workflows/'))
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
                    manager('node', pm.split('@')[0], relative)
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
                runtime('go', relative, 'Go module/workspace', match[1] if match else None)
            elif base in {'Gemfile', 'Gemfile.lock'} or base.endswith('.gemspec'):
                match = re.search(r'\bruby\s+[\'"]([^\'"]+)', text)
                runtime('ruby', relative, 'Ruby manifest/lock', match[1] if match else None)
                manager('ruby', 'bundler', relative)
            elif base.startswith('requirements') or base in {'Pipfile', 'uv.lock', 'poetry.lock', 'Pipfile.lock'}:
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

        workflow = base.startswith(('Taskfile', 'Dockerfile', 'docker-compose', 'compose.')) or path.suffix in {'.sh', '.bash'} or relative.startswith('.github/workflows/') or base == 'package.json'
        if not workflow:
            continue
        active = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith(('#', 'desc:', 'description:')))
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
        if base.startswith(('Dockerfile', 'docker-compose', 'compose.')): tool('docker', relative, 'container workflow')
        for pattern in (r'^\s*EXPOSE\s+(\d+)', r'\blsof\b[^\n]*?\btcp:(\d+)', r'(?<![\w-])(?:[A-Z_]*PORT)\s*[:=]\s*[\'"]?(\d+)', r'--port(?:=|\s+)(\d+)'):
            for value in re.findall(pattern, active, re.M):
                port = int(value)
                if 0 < port < 65536: ports.add(port)
    for name in RUNTIMES:
        if len(sources[name]) >= 2:
            found['runtimes'][name]['selected'] = True
        found['runtimes'][name]['evidence'].extend(f'{path}: supporting {name} source' for path in sources[name][:5])
        version, conflicts = _choose(declarations[name], '3.13' if name == 'python' else None)
        found['runtimes'][name]['version'] = version
        found['runtimes'][name]['conflicts'] = conflicts
    for family, choices in managers.items():
        found['package_managers'][family] = {'selected': next(iter(choices)) if len(choices) == 1 else None, 'evidence': [item for values in choices.values() for item in values], 'conflicts': [f'{family}: conflicting package managers: {", ".join(sorted(choices))}'] if len(choices) > 1 else []}
    found['ports'] = sorted(ports)
    return found
