"""Connect Herdr to existing checkouts with scratch and native conversation sync."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from . import scratch, sync_cycle
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
            raise ValueError('No Codespace for this repository. Run task devcontainer:setup and choose a Codespace to create one')
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
            raise ValueError('Invalid historical checkout path')
        remote = json.loads(scratch._remote(connection, root, allow_missing_scratch=True))
        if remote.get('identity') != project.identity or remote.get('root') != root:
            raise ValueError('Remote repository identity/checkout differs')
        verified.append(root)
    return verified


def _prerequisites():
    """Local checks precede any billable stopped-Codespace start."""
    import shutil
    import sys
    from .transport import _check_gh, github_token
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or newer is required')
    _check_gh(os.environ)
    for command in ('ssh', 'herdr'):
        if shutil.which(command) is None:
            raise ValueError(command + ' is required before connecting a Codespace')
    key = Path(os.environ.get('HOME') or Path.home()) / '.ssh/codespaces.auto'
    if not key.is_file():
        raise ValueError('Codespace SSH key missing (~/.ssh/codespaces.auto); configure SSH explicitly first.')
    github_token(os.environ)  # Read the selected credential; never switch accounts.


def _current_worker(runtime):
    return bool(runtime.get('running') and runtime.get('phase') == 'syncing'
                and runtime.get('checkpoint_version') == sync_cycle.SCHEMA
                and runtime.get('runtime_id') == scratch._runtime_id() and sync_cycle.fresh_worker(runtime))


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
        if not runtime.get('running'):
            raise ValueError('Scratch/conversation worker stopped; inspect task scratch:logs')
        if _current_worker(runtime) and runtime.get('connection_prepared'):
            binding = scratch._config(project.root, state)
            if (runtime.get('prepared_binding') == binding
                    and scratch.inspect_sync(project.root, state).get('phase') == 'synchronized'):
                return runtime
        time.sleep(.25)
    raise ValueError('Waiting for current scratch connection timed out; inspect task work:status and scratch:logs')


def remote(project, state, armed=None):
    """Connect the project; `armed(primary)` runs once the worker is ready, before Herdr opens."""
    if os.environ.get('CODESPACES') == 'true':
        raise ValueError('Run task work:cs on your Mac to connect the project')
    from .herdr import open_remote
    from .ssh_hosts import configure_ssh
    from .transport import CodespaceConnection
    state = Path(state)
    with locked(state / 'workspace-control.lock', blocking=True):
        value = registry(project, state)
        primary = _project(value['local_root'], project.identity)
        if not _ordinary(str(primary.root)) or primary.root.is_relative_to(state):
            raise ValueError('Saved local checkout is generated; select the original ordinary checkout')
        _prerequisites()
        print('Connecting Codespace…', flush=True)
        # Selection/verification precedes any controller/binding changes.
        name, remote_root = _choose_pairing(primary, state, value)
        if not _ordinary(remote_root):
            raise ValueError('Choose a verified ordinary Codespace checkout')
        with CodespaceConnection(name, state=state) as connection:
            _verify_roots(connection, primary, [remote_root])
            old_roots = [value.get(k) for k in ('remote_root', 'remote_active_root')]
            old_roots += value.get('verified_remote_historical_roots', [])
            local_candidates = list(value.get('verified_local_historical_roots', []))
            # Migrate the first mixed-root producer without dropping local history.
            local_candidates += [root for root in value.get('verified_historical_roots', [])
                                 if root not in old_roots and Path(root).is_dir()]
            old_roots += [root for root in value.get('verified_historical_roots', [])
                          if root not in local_candidates]
            historical = _verify_roots(connection, primary, [r for r in old_roots if r and r != remote_root])
            local_old = value.get('local_active_root')
            local_history = []
            for local_root in dict.fromkeys([local_old, *local_candidates]):
                if local_root and local_root != str(primary.root):
                    local_history.append(str(_project(local_root, primary.identity).root))
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
        scratch.retarget(primary.root, state, name, remote_root)
        print('Starting scratch and conversation synchronization…', flush=True)
        scratch.start(primary.root, state)
        runtime = _ready(primary, state)
        configure_ssh(primary, state)
    history = (runtime or {}).get('conversations', {}).get('outcome', 'pending')
    if history != 'complete':
        print('Conversation history is ' + history + '; inspect task work:status.')
    if armed is not None:
        # Outside workspace-control.lock, which arming takes for its own write,
        # and before Herdr, whose TUI would otherwise hold the step back.
        armed(primary)
    return open_remote(primary, state, remote_root)


def status(project, state):
    value = registry(project, state)
    latest, _ = _conversation_receipt(project, state)
    runtime = scratch.status(state)
    try:
        binding = read_json(Path(state) / scratch.CONFIG) if (Path(state) / scratch.CONFIG).exists() else {}
        sync = scratch.inspect_sync(Path(binding.get('root', value['local_root'])), state) if runtime.get('running') else runtime
    except (OSError, ValueError) as error:
        sync = dict(running=runtime.get('running', False), issue=str(error))
    cycle = runtime.get('conversations', {})
    current = (_current_worker(runtime) and sync.get('phase') == 'synchronized'
               and cycle.get('project_id') == project.project_id and cycle.get('identity') == project.identity
               and cycle.get('local_root') == value['local_root']
               and (not value.get('remote_root') or cycle.get('remote_root') == value['remote_root']))
    history = sync_cycle.summary(cycle, current=current, local_receipt=latest)
    history['retained_receipts'] = dict(runtime.get('retained_receipts', {}), codespace_to_desktop=latest)
    return dict(workspace=value, worker=runtime, scratch=sync, last_mac_checkpoint=latest,
                conversations=history, recovery=recovery(state),
                synchronization='Scratch and project conversations only; Git is managed manually')


def recovery(state):
    """Offline view of arming, the keeper and the restart budget; never creates state."""
    from . import arming, keeper, lifecycle
    now = time.time()
    try:
        config = arming.read(state)
    except (OSError, ValueError) as error:
        # Only the reason; the status line carries the fix.
        config = {'armed': None, 'issue': str(error).split(';')[0], 'path': str(Path(state) / arming.ARMING)}
    try:
        runtime = keeper.status(state)
    except (OSError, ValueError) as error:
        runtime = {'running': False, 'phase': 'unknown', 'heartbeat_age': None, 'error': str(error)}
    try:
        rows = lifecycle.Ledger(Path(state) / keeper.LEDGER).rows()
    except OSError:
        rows = []
    return dict(arming=config, keeper=runtime, restarts=len(lifecycle.restart_rids(rows, now)), now=now)


def _ports(ports):
    return ' '.join(str(port + 10000) for port in ports) or 'none'


def _token_hint(runtime):
    # Only a token error has a known fix; other waits say nothing more.
    from . import lifecycle
    error = str(runtime.get('error') or '')
    if lifecycle.TOKEN_TIMEOUT in error:
        return '; unlock the Mac and its login keychain'
    if lifecycle.NO_TOKEN in error or lifecycle.BAD_REQUEST in error:
        return '; run gh auth status --hostname github.com'
    return ''


def recovery_lines(value):
    """Spec 6.8's Recovery block; each line that is not fine names the command to run."""
    from .arming import format_time
    from . import keeper, lifecycle
    config, runtime, now = value['arming'], value['keeper'], value['now']
    if config.get('issue'):
        # Unknown, not unarmed: a keeper may still be running, so its lines follow.
        lines = ['Recovery     arming.json cannot be read (' + config['issue'] + '); move '
                 + config['path'] + ' aside, then run task work:cs:armed to arm again']
    elif not config.get('armed'):
        return ['Recovery     not armed; task work:cs:armed arms it']
    else:
        hold = config.get('hold_until')
        held = ('on hold until ' + format_time(hold) + ' (task work:hold -- off ends it)'
                if isinstance(hold, (int, float)) and hold > now else 'no hold')
        lines = ['Recovery     armed since ' + format_time(config.get('armed_at') or now) + ' as '
                 + str(config.get('gh_user')) + ', ' + held + '; restarts ' + str(value['restarts'])
                 + ' of ' + str(lifecycle.MAX_RESTARTS) + ' in 24 h']
    running, age = runtime.get('running'), runtime.get('heartbeat_age')
    if not running:
        lines.append('Keeper       not running; run task work:cs:armed')
    elif runtime.get('phase') == 'refused':
        lines.append('Keeper       refused: ' + str(runtime.get('error')))
    elif age is None or age > keeper.HEARTBEAT_STALE:
        lines.append(f'Keeper       running (pid {runtime.get("pid")}) but its heartbeat is '
                     + ('missing' if age is None else f'{int(age)} s old') + '; run task work:cs:armed')
    else:
        text = (f'Keeper       running (pid {runtime.get("pid")}), heartbeat {int(age)} s, caffeinate '
                + ('held' if runtime.get('caffeinate') else 'not held'))
        if runtime.get('phase') == 'waiting':
            text += '; waiting: ' + str(runtime.get('error'))
        lines.append(text)
    reading, anchor, watch = runtime.get('reading') or {}, runtime.get('anchor'), runtime.get('down') or {}
    if not running or not reading:
        lines.append('Codespace    not read yet' + ('; run task work:cs:armed' if not running else _token_hint(runtime)))
    elif not reading.get('ok'):
        lines.append('Codespace    unreadable at ' + format_time(reading['at'])
                     + '; check gh auth status --hostname github.com and the network')
    elif reading.get('state') == 'Available':
        text = 'Codespace    Available'
        if anchor:
            text += (' since ' + format_time(anchor['at']) + ', anchor ' + str(anchor.get('source'))
                     + '; 12-hour stop due ~' + format_time(anchor['at'] + lifecycle.LIFETIME))
        else:
            text += ', no anchor yet'
        lines.append(text)
    else:
        text = 'Codespace    ' + str(reading.get('state')) + ' since ' + format_time(watch.get('first_at') or reading['at'])
        reason = {'OWNER': 'stopped by you', 'OTHER': 'not the 12-hour stop',
                  'LIFETIME': "GitHub's 12-hour stop"}.get(watch.get('kind'))
        lines.append(text + (' (' + reason + ')' if reason else '') + '; run task work:cs to start it')
    keepalive = runtime.get('keepalive') or {}
    if running and keepalive.get('pid'):
        lines.append(f'Keep-alive   holding (pid {keepalive["pid"]}); ports '
                     + _ports([remote for _, remote in keepalive.get('ports_up', [])]))
    else:
        lines.append('Keep-alive   not holding; ports ' + _ports(config.get('ports', []))
                     + ('' if running else '; run task work:cs:armed'))
    last = runtime.get('last_restart')
    if isinstance(last, dict) and isinstance(last.get('at'), (int, float)):
        lines.append('Last restart ' + str(last.get('rid')) + ' ' + format_time(last['at']) + ': '
                     + str(last.get('outcome', 'unknown')))
    else:
        lines.append('Last restart none')
    return lines


def status_text(value):
    worker, scratch_status, history = value['worker'], value['scratch'], value['conversations']
    lines = ['Sync worker: ' + ('running' if worker.get('running') else 'stopped'),
             'Scratch: ' + scratch_status.get('phase', 'unknown'),
             'History: ' + history['outcome'] + ' (' + history['stage'] + ')']
    for name, label in zip(sync_cycle.DIRECTIONS, ('Desktop → Codespace', 'Codespace → Desktop')):
        row = history['directions'][name]
        counts = row['counts']
        lines.append(label + ': ' + str(counts['received']) + ' received, ' + str(counts['available'])
                     + ' available, ' + str(counts['pending']) + ' pending, ' + str(counts['unsupported']) + ' unsupported')
    if any(history['retained_receipts'].values()):
        lines.append('Previous verified receipts retained; availability reflects the current cycle.')
    if history['issues'] or worker.get('error') or scratch_status.get('issue'):
        lines.append('Details: task work:status -- --json; logs: task scratch:logs')
    if value.get('recovery'):
        lines += recovery_lines(value['recovery'])
    return '\n'.join(lines)
