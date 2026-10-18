"""Connect Herdr to existing checkouts with scratch and native conversation sync."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from . import scratch
from .checkpoint_store import Store
from .state import atomic_write, discover_project, locked, read_json


def _write(state, value):
    atomic_write(Path(state) / 'workspace.json', json.dumps(value, sort_keys=True).encode() + b'\n')


def registry(project, state):
    try:
        value = read_json(Path(state) / 'workspace.json')
    except FileNotFoundError:
        return dict(schema=1, project_id=project.project_id, identity=project.identity,
                    local_root=str(project.root), local_active_root=str(project.root), active_host='local')
    if (value.get('schema') != 1 or value.get('project_id') != project.project_id
            or value.get('identity') != project.identity):
        raise ValueError('Workspace registry differs from this project; existing state preserved')
    allowed = [value.get(k) for k in ('local_root', 'local_active_root', 'remote_root', 'remote_active_root')]
    if str(project.root) not in allowed:
        raise ValueError('Another checkout already owns this project pairing; use its task work:cs entrypoint')
    return value


def _project(root, identity):
    project = discover_project(Path(root))
    if project.identity != identity:
        raise ValueError('Workspace repository identity changed')
    return project


def _ordinary(root):
    return (isinstance(root, str) and Path(root).is_absolute()
            and '..' not in Path(root).parts and '.devcontainer-work' not in Path(root).parts
            and not any(ord(c) < 32 for c in root))


def _choose_pairing(project, state, value):
    from .codespaces import Codespaces
    from .terminal import Gum
    primary = _project(value['local_root'], project.identity)
    service = Codespaces(primary, state)
    binding_path = Path(state) / scratch.CONFIG
    binding = read_json(binding_path) if binding_path.exists() else None
    if binding:
        if binding.get('identity') != project.identity:
            raise ValueError('Codespace pairing belongs to another project')
        name = binding['codespace']
        row = service._api('GET', 'user/codespaces/' + name, service._api_env())
        if row.get('name') != name or not service._matches_repository(row):
            raise ValueError('Paired Codespace repository differs')
        if row.get('state') != 'Available':
            service.start_codespace(name)
        # Verify even a saved original: old registries may point at generated
        # worktrees, or the checkout may have been removed or repurposed.
        roots = service.roots(name, accept_new=False)
    else:
        spaces = service.spaces()
        if not spaces:
            raise ValueError('No Codespace for this repository; create one in setup first')
        labels = [r['name'] + ' (' + r.get('state', 'unknown') + ')' for r in spaces]
        row = spaces[0] if len(spaces) == 1 else spaces[labels.index(Gum().choose('Codespace', labels))]
        name = row['name']
        if row.get('state') != 'Available':
            service.start_codespace(name)
        roots = service.roots(name)
    roots = [root for root in roots if _ordinary(root)]
    for saved in (value.get('remote_root'), binding.get('remote_root') if binding else None):
        if saved in roots:
            return name, saved
    if len(roots) == 1:
        return name, roots[0]
    if not roots:
        raise ValueError('No verified ordinary project checkout found; existing bindings preserved')
    raise ValueError('Choose a verified ordinary project checkout: ' + ', '.join(roots))


def _verify_roots(connection, project, roots):
    verified = []
    for root in dict.fromkeys(roots):
        if not isinstance(root, str) or not Path(root).is_absolute() or '..' in Path(root).parts:
            continue
        try:
            remote = json.loads(scratch._remote(connection, root, allow_missing_scratch=True))
            if remote.get('identity') == project.identity and remote.get('root') == root:
                verified.append(root)
        except (OSError, ValueError):
            continue
    return verified


def _conversation_receipt(project, state):
    try:
        store = Store(Path(state))
        latest = store.latest('codespace')
        if latest is None:
            return None, {}
        manifest = store.load(latest['snapshot_id'])
        from .replication import validate_manifest
        validate_manifest(manifest, project.identity, project.project_id)
        return latest, manifest['conversations']
    except (OSError, ValueError, KeyError):
        # Legacy objects/receipts remain retained, without claiming current health.
        return None, {}


def _ready(project, state, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runtime = scratch.status(state)
        latest, _ = _conversation_receipt(project, state)
        if (runtime.get('running') and runtime.get('phase') == 'syncing'
                and runtime.get('conversations', {}).get('schema') == 2 and latest
                and not runtime.get('conversation_error')):
            return
        if not runtime.get('running'):
            raise ValueError('Scratch/conversation worker stopped; inspect task scratch:logs')
        time.sleep(.25)
    raise ValueError('Waiting for conversation synchronization timed out; inspect task work:status and scratch:logs')


def remote(project, state):
    if os.environ.get('CODESPACES') == 'true':
        raise ValueError('Run task work:cs on your Mac to connect the project')
    from .herdr import open_remote
    from .ssh_hosts import configure_ssh
    from .replication import sync_project_data
    from .transport import CodespaceConnection
    state = Path(state)
    with locked(state / 'workspace-control.lock', blocking=True):
        value = registry(project, state)
        primary = _project(value['local_root'], project.identity)
        if not _ordinary(str(primary.root)) or primary.root.is_relative_to(state):
            raise ValueError('Saved local checkout is generated; select the original ordinary checkout')
        print('Connecting Codespace…', flush=True)
        # Selection/verification precedes any controller/binding changes.
        name, remote_root = _choose_pairing(primary, state, value)
        if not _ordinary(remote_root):
            raise ValueError('Choose a verified ordinary Codespace checkout')
        with CodespaceConnection(name, state=state) as connection:
            old_roots = [value.get(k) for k in ('remote_root', 'remote_active_root')]
            old_roots += value.get('verified_remote_historical_roots', value.get('verified_historical_roots', []))
            local_candidates = list(value.get('verified_local_historical_roots', []))
            # Migrate the first mixed-root producer without dropping local history.
            local_candidates += value.get('verified_historical_roots', [])
            historical = _verify_roots(connection, primary, [r for r in old_roots if r and r != remote_root])
            local_old = value.get('local_active_root')
            local_history = []
            for local_root in dict.fromkeys([local_old, *local_candidates]):
                if local_root and local_root != str(primary.root):
                    try:
                        local_history.append(str(_project(local_root, primary.identity).root))
                    except (OSError, ValueError):
                        pass
            retained = sorted(set(historical + local_history))
            # Persist only verified historical scopes. Registry fields authorize
            # capture, so unverified prior active roots cannot remain in them.
            updated = dict(value, local_active_root=str(primary.root), remote_root=remote_root,
                         remote_active_root=remote_root, verified_historical_roots=retained,
                         verified_local_historical_roots=sorted(set(local_history)),
                         verified_remote_historical_roots=sorted(set(historical)),
                         codespace=name, active_host='codespace')
            if updated != value:
                atomic_write(state / 'workspace-history' / (str(time.time_ns()) + '.json'),
                             json.dumps(value, sort_keys=True).encode() + b'\n')
            _write(state, updated)
            synced = sync_project_data(connection, primary, state, remote_root, historical_roots=historical)
        scratch.retarget(primary.root, state, name, remote_root)
        print('Starting scratch and conversation synchronization…', flush=True)
        scratch.start(primary.root, state)
        _ready(primary, state)
        configure_ssh(primary, state)
    if synced['issues']:
        print('Conversation/memory synchronization has issues; inspect task work:status.')
    return open_remote(primary, state, remote_root)


def status(project, state):
    value = registry(project, state)
    latest, conversations = _conversation_receipt(project, state)
    runtime = scratch.status(state)
    try:
        binding = read_json(Path(state) / scratch.CONFIG) if (Path(state) / scratch.CONFIG).exists() else {}
        sync = scratch.inspect_sync(Path(binding.get('root', value['local_root'])), state) if runtime.get('running') else runtime
    except (OSError, ValueError) as error:
        sync = dict(running=runtime.get('running', False), issue=str(error))
    publication = runtime.get('conversations', {})
    issues = publication.get('issues', conversations.get('issues', []))
    return dict(workspace=value, worker=runtime, scratch=sync, last_mac_checkpoint=latest,
                conversations=dict(protected=len(conversations.get('entries', [])), issues=issues,
                                   healthy=bool(runtime.get('running') and runtime.get('phase') == 'syncing'
                                                and latest and publication.get('schema') == 2 and not issues
                                                and not runtime.get('conversation_error'))),
                synchronization='Scratch and project conversations only; Git is managed manually')
