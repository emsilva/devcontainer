"""Local-first Herdr workspaces backed by acknowledged project checkpoints."""
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
        raise ValueError('Another checkout already owns this project pairing; use its task work entrypoint')
    return value


def _project(root, identity):
    project = discover_project(Path(root))
    if project.identity != identity:
        raise ValueError('Workspace repository identity changed')
    return project


def _recover(project, state, value):
    from .project_files import restore_files
    store = Store(Path(state))
    latest = store.latest('codespace')
    if latest is None:
        raise ValueError('No acknowledged Codespace checkpoint is available on this Mac yet')
    snapshot_id = latest['snapshot_id']
    manifest = store.load(snapshot_id)
    from .replication import validate_manifest
    validate_manifest(manifest, project.identity, project.project_id)
    destination = Path(state) / 'recovered' / snapshot_id
    # Existing directories are never reconstructed over user work. Only registry
    # ownership permits reuse, and every fresh recovery gets an absent directory.
    if destination.exists():
        destination = Path(state) / 'recovered' / (snapshot_id + '-' + str(time.time_ns()))
    from .state import _parent
    with _parent(destination, create=True, private=True):
        pass
    recovered = restore_files(store, manifest['files'], destination)
    value.update(local_active_root=str(recovered.root), active_host='local', recovered_from=snapshot_id)
    _write(state, value)
    print('Recovered the last checkpoint received on this Mac:', latest['received_at'])
    print('Working copy:', recovered.root)
    return recovered


def local_project(project, state):
    state = Path(state)
    with locked(state / 'workspace-control.lock', blocking=True):
        value = registry(project, state)
        if value.get('active_host') == 'codespace':
            # Deliberately no Codespaces API, SSH, source-release or network gate.
            scratch.stop(state)
            active = _recover(project, state, value)
        else:
            active = _project(value['local_active_root'], project.identity)
            _write(state, value)
        binding_path = state / scratch.CONFIG
        if binding_path.exists():
            binding = read_json(binding_path)
            if binding.get('identity') != project.identity:
                raise ValueError('Scratch pairing project identity differs')
            try:
                scratch.retarget(active.root, state, binding['codespace'], binding['remote_root'])
                scratch.start(active.root, state)
            except (OSError, ValueError):
                print('Local work is available. Background protection is stopped; inspect task work:status.')
    return active


def local(project, state):
    from .herdr import open_local
    return open_local(local_project(project, state), state)


def _choose_pairing(project, state, value):
    from .codespaces import Codespaces
    from .terminal import Gum
    primary = _project(value['local_root'], project.identity)
    service = Codespaces(primary, state)
    binding_path = Path(state) / scratch.CONFIG
    if binding_path.exists():
        binding = read_json(binding_path)
        if binding.get('identity') != project.identity:
            raise ValueError('Codespace pairing belongs to another project')
        name = binding['codespace']
        row = service._api('GET', 'user/codespaces/' + name, service._api_env())
        if row.get('name') != name or not service._matches_repository(row):
            raise ValueError('Paired Codespace repository differs')
        if row.get('state') != 'Available':
            service.start_codespace(name)
        return name, value.get('remote_root') or binding['remote_root']
    spaces = service.spaces()
    if not spaces:
        raise ValueError('No Codespace for this repository/branch; create one in setup first')
    labels = [r['name'] + ' (' + r.get('state', 'unknown') + ')' for r in spaces]
    index = 0 if len(spaces) == 1 else labels.index(Gum().choose('Codespace', labels))
    row = spaces[index]
    if row.get('state') != 'Available':
        service.start_codespace(row['name'])
    roots = service.roots(row['name'])
    if not roots:
        raise ValueError('No verified project checkout found in Codespace')
    root = roots[0] if len(roots) == 1 else Gum().choose('Project checkout', roots)
    service.pair(row['name'], root)
    return row['name'], root


def _ready(project, state, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runtime = scratch.status(state)
        if runtime.get('phase') == 'syncing' and runtime.get('checkpoint') and not runtime.get('checkpoint_error'):
            return
        if not runtime.get('running'):
            raise ValueError('Project backup worker stopped; inspect task scratch:logs')
        time.sleep(.25)
    raise ValueError('Waiting for the first Mac checkpoint timed out; inspect task work:status and scratch:logs')


def remote(project, state):
    if os.environ.get('CODESPACES') == 'true':
        raise ValueError('Run task work:cs on your Mac so it can receive recovery checkpoints')
    from .herdr import open_remote
    from .ssh_hosts import configure_ssh
    from .replication import capture, checkpoint_remote, transfer, RemoteStore
    from .rpc import RemoteEndpoint
    from .transport import CodespaceConnection
    state = Path(state)
    with locked(state / 'workspace-control.lock', blocking=True):
        value = registry(project, state)
        name, original_remote = _choose_pairing(project, state, value)
        active_local = _project(value['local_active_root'], project.identity)
        with CodespaceConnection(name, state=state) as connection:
            # Preserve remote work before making a new desktop-seeded checkout.
            prior_root = value.get('remote_active_root') or original_remote
            checkpoint_remote(connection, project, state, prior_root)
            if value.get('active_host') != 'codespace' or not value.get('remote_active_root'):
                store = Store(state)
                snapshot_id = capture(active_local, store)
                store.acknowledge(snapshot_id, source='local')
                endpoint = RemoteEndpoint(connection, original_remote, project.identity)
                transfer(store, RemoteStore(endpoint), snapshot_id, source='desktop', identity=project.identity)
                destination = '/workspaces/.devcontainer-work/' + project.project_id + '/' + snapshot_id
                active_remote = endpoint.call('restore', snapshot_id=snapshot_id, destination=destination)['root']
            else:
                active_remote = value['remote_active_root']
        scratch.retarget(active_local.root, state, name, active_remote)
        scratch.start(active_local.root, state)
        _ready(active_local, state)
        configure_ssh(project, state)
        value.update(codespace=name, remote_root=original_remote, remote_active_root=active_remote, active_host='codespace')
        _write(state, value)
    print('Mac checkpoint protection is running. Keep this Mac awake and connected.')
    return open_remote(project, state, active_remote)


def status(project, state):
    value = registry(project, state)
    store = Store(Path(state))
    latest = store.latest('codespace')
    conversations = store.load(latest['snapshot_id']).get('conversations', {}) if latest else {}
    runtime = scratch.status(state)
    sync = scratch.inspect_sync(Path(value['local_active_root']), state) if runtime.get('running') else runtime
    return dict(workspace=value, worker=runtime, scratch=sync, last_mac_checkpoint=latest,
                conversations=dict(protected=len(conversations.get('entries', [])), issues=conversations.get('issues', [])),
                protection='Only checkpoints acknowledged on this awake, connected Mac are recoverable')
