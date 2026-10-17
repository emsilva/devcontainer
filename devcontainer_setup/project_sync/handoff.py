"""Mac-driven, receipt-based handoff over the configured project pairing.

Historical payloads stay in private snapshots. RPCs run canonical Python helpers
in a transient isolated package; no checkout, auth or native SQLite is copied.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

from . import agents, ownership, sessions
from .scratch import _config, backup
from .state import atomic_write, discover_project, host_id, locked, read_json, state_dir
from .transport import CodespaceConnection

MAX_RPC_BYTES = 384 * 1024 * 1024
_OPERATIONS = {'describe', 'status', 'backup', 'inspect', 'source_prepare', 'target_prepare', 'import', 'verify', 'accept', 'complete', 'activate'}
_MODULES = ('__init__', 'state', 'sessions', 'ownership', 'agents', 'tools', 'transport', 'scratch', 'handoff')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'


def _save(path, value, *, immutable=False):
    data = _json(value)
    if len(data) > MAX_RPC_BYTES:
        raise ValueError('Handoff snapshot exceeds the bounded transfer size')
    if immutable and Path(path).exists():
        if _json(read_json(path)) != data:
            raise ValueError('Immutable handoff snapshot differs; inspect private receipts')
        return
    atomic_write(Path(path), data, mode=0o400 if immutable else 0o600)


def _optional(path):
    try:
        return read_json(path)
    except FileNotFoundError:
        return None


def _agent_version(agent):
    if agent == 'codex':
        return None  # Exact native version comes from session metadata.
    result = subprocess.run(['claude', '--version'], capture_output=True, text=True, timeout=15)
    match = re.match(r'(2\.1\.\d+)\b', result.stdout)
    if result.returncode or not match:
        raise ValueError('A supported native Claude 2.1 version is required')
    return match[1]


class LocalEndpoint:
    def __init__(self, project, state, *, env=None):
        self.project, self.state, self.env = project, Path(state), env

    def _export(self, agent, session_id, host, generation):
        return sessions.export_snapshot(self.project, agent, session_id, sessions.native_home(agent, self.env), source_host=host, generation=generation, agent_version=_agent_version(agent), state=self.state)

    def _source(self, agent, session_id, request_id, destination_host):
        sessions.session_uuid(request_id)
        host = host_id(self.env)
        pointer = self.state / 'handoffs/source' / f'{agent}-{session_id}.json'
        with locked(pointer.with_suffix('.lock')):
            current = ownership.owner_status(self.state, agent, session_id)
            record = _optional(pointer)
            if record and record.get('request_id') == request_id:
                if record.get('result'):
                    result = record['result']
                    transfer = result['transfer']
                    if (current.get('status') not in ('transferring', 'transferred')
                            or any(current.get(key) != transfer.get(key) for key in ('nonce', 'generation', 'host', 'destination_host'))):
                        raise ValueError('Source ownership changed since snapshot; inspect handoff receipts')
                    return result
            else:
                if current.get('host') != host or current.get('status') != 'idle':
                    raise ValueError('Source owner is unknown, active or uncertain; inspect ownership receipt')
                record = dict(request_id=request_id, host=host, destination_host=destination_host, generation=current['generation'])
                _save(pointer, record)
            if record['host'] != host or record['destination_host'] != destination_host:
                raise ValueError('Pending handoff peer differs; inspect private handoff receipts')
            if current.get('status') == 'idle' and current.get('host') == host and current.get('generation') == record['generation']:
                # Validate strict project scope before any native API/lease writes.
                self._export(agent, session_id, host, current['generation'])
                with ownership.managed_lease(self.state, agent, session_id, host):
                    self._export(agent, session_id, host, current['generation'])
                    history = agents.native_history(self.project, session_id, env=self.env) if agent == 'codex' else None
                record['history'] = history
                _save(pointer, record)
                transfer = ownership.begin_transfer(self.state, agent, session_id, host, destination_host)
            elif (current.get('status') == 'transferring' and current.get('host') == host
                    and current.get('destination_host') == destination_host
                    and current.get('generation') == record['generation'] + 1 and 'history' in record):
                transfer = current
            else:
                raise ValueError('Source custody is uncertain; inspect the pending ownership and handoff receipts')
            bundle = self._export(agent, session_id, host, transfer['generation'])
            result = dict(transfer=transfer, bundle=bundle, history=record['history'])
            _save(self.state / 'handoffs/requests' / request_id / 'source.json', result, immutable=True)
            _save(pointer, dict(record, result=result))
            return result

    def call(self, operation, **values):
        if operation not in _OPERATIONS:
            raise ValueError('Unsupported handoff operation')
        if operation == 'describe':
            return dict(host=host_id(self.env), root=str(self.project.root), identity=self.project.identity, project_id=self.project.project_id)
        if operation == 'backup':
            return dict(path=str(backup(self.project.root, self.state)))
        agent, session_id = values['agent'], sessions.session_uuid(values['session_id'])
        if agent not in ('codex', 'claude'):
            raise ValueError('Unsupported agent')
        if operation == 'status':
            return ownership.owner_status(self.state, agent, session_id)
        if operation == 'source_prepare':
            return self._source(agent, session_id, values['request_id'], values['destination_host'])
        if operation == 'inspect':
            current = ownership.owner_status(self.state, agent, session_id)
            host = host_id(self.env)
            if current.get('host') != host or current.get('status') != 'idle':
                raise ValueError('Source inspection needs known idle ownership; inspect its receipt')
            bundle = self._export(agent, session_id, host, current['generation'])
            return dict(files=len(bundle['files']), generation=current['generation'], agent_version=bundle['agent_version'])
        if operation == 'target_prepare':
            return ownership.prepare_receive(self.state, agent, session_id, host_id(self.env), values['transfer'])
        if operation in ('import', 'verify'):
            current = ownership.owner_status(self.state, agent, session_id)
            bundle = values['bundle']
            if (current.get('status') not in ('receiving', 'accepted')
                    or current.get('destination_host') != host_id(self.env)
                    or current.get('host') != bundle.get('source_host')
                    or current.get('generation') != bundle.get('generation')):
                raise ValueError('Matching reserved destination custody is required before import or native reconciliation')
        if operation == 'import':
            return sessions.import_snapshot(self.project, agent, session_id, sessions.native_home(agent, self.env), values['bundle'], state=self.state)
        if operation == 'verify':
            plan = sessions._preflight(self.project, agent, session_id, sessions.native_home(agent, self.env), values['bundle'])
            sessions.assert_quiescent(item['destination'] for item in plan)
            if agent == 'codex':
                actual = agents.native_history(self.project, session_id, env=self.env)
                expected = values['history']
                if not isinstance(expected, dict) or any(actual.get(key) != expected.get(key) for key in ('turns', 'items')):
                    raise ValueError('Native histories differ; destination bytes and private handoff receipts retained')
            else:
                if any(hashlib.sha256(sessions._read(item['destination'])).digest() != hashlib.sha256(item['data']).digest() for item in plan):
                    raise ValueError('Claude history bytes differ; destination preserved and private receipts retained')
            return dict(verified=True)
        if operation == 'accept':
            return ownership.accept_transfer(self.state, agent, session_id, host_id(self.env), values['transfer'], values['import_receipt'])
        if operation == 'complete':
            return ownership.complete_transfer(self.state, agent, session_id, host_id(self.env), values['acknowledgment'])
        return ownership.activate_transfer(self.state, agent, session_id, host_id(self.env), values['committed'])


# The first interpreter immediately execs isolated Python before reading payloads.
# Temporary package sources are supplied only by our authenticated distribution.
_BOOTSTRAP = '''import base64,json,pathlib,sys,tempfile
limit=384*1024*1024
raw=sys.stdin.buffer.read(limit+1)
if len(raw)>limit:raise ValueError("RPC payload exceeds limit")
payload=json.loads(raw)
with tempfile.TemporaryDirectory(prefix="project-handoff-") as directory:
 root=pathlib.Path(directory);package=root/"project_sync";package.mkdir(mode=0o700)
 for name,source in payload["modules"].items():
  if name not in ("__init__","state","sessions","ownership","agents","tools","transport","scratch","handoff"):raise ValueError("Invalid source module")
  path=package/(name+".py");path.write_bytes(base64.b64decode(source,validate=True));path.chmod(0o600)
 sys.path.insert(0,str(root))
 from project_sync.handoff import _rpc_dispatch
 response=json.dumps(_rpc_dispatch(payload["request"]),sort_keys=True).encode()
 if len(response)>limit:raise ValueError("RPC response exceeds limit")
 sys.stdout.buffer.write(response)
'''


class RemoteEndpoint:
    def __init__(self, connection, root, identity):
        self.connection, self.root, self.identity = connection, str(root), identity

    def call(self, operation, **values):
        if operation not in _OPERATIONS:
            raise ValueError('Unsupported remote handoff operation')
        sources = {name: base64.b64encode((Path(__file__).parent / (name + '.py')).read_bytes()).decode() for name in _MODULES}
        payload = _json(dict(modules=sources, request=dict(root=self.root, identity=self.identity, operation=operation, values=values)))
        if len(payload) > MAX_RPC_BYTES:
            raise ValueError('Remote handoff payload exceeds bounded size; inspect private snapshot')
        code = 'import os,sys;os.execv(sys.executable,[sys.executable,"-I","-B","-c",' + repr(_BOOTSTRAP) + '])'
        try:
            raw = self.connection.python(code, payload=payload, timeout=180)
            if len(raw) > MAX_RPC_BYTES:
                raise ValueError('Oversized remote response')
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get('ok'), bool) or (result['ok'] and not isinstance(result.get('result'), dict)):
                raise ValueError('Malformed remote response')
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ValueError(f'Remote {operation} failed; check Codespace availability and retry using the saved handoff receipt') from None
        if not result.get('ok'):
            raise ValueError(f'Remote {operation} refused; inspect private handoff receipts and resolve project scope, writer or history conflicts before retrying')
        return result['result']


def _rpc_dispatch(request):
    operation = request.get('operation')
    try:
        if operation not in _OPERATIONS:
            raise ValueError('Unknown operation')
        project = discover_project(Path(request['root']))
        if project.identity != request['identity'] or str(project.root) != request['root']:
            raise ValueError('Remote binding changed')
        endpoint = LocalEndpoint(project, state_dir(project))
        return dict(ok=True, result=endpoint.call(operation, **request.get('values', {})))
    except Exception:
        # Never serialize native stderr, transcript fragments or credentials.
        return dict(ok=False, error='Remote operation refused; inspect private handoff receipts')


def _validate(project, agent, session_id, direction):
    sessions.session_uuid(session_id)
    if agent not in ('codex', 'claude') or direction not in ('mac', 'codespace'):
        raise ValueError('Choose codex/claude and a handoff direction of mac or codespace')
    found = discover_project(project.root)
    if found != project:
        raise ValueError('Local project identity or checkout changed')


def _peers(project, direction, config, local, remote):
    left, right = local.call('describe'), remote.call('describe')
    if (left.get('identity') != project.identity or right.get('identity') != project.identity
            or left.get('project_id') != project.project_id or right.get('project_id') != project.project_id
            or left.get('root') != str(project.root) or right.get('root') != config['remote_root']
            or not left.get('host') or not right.get('host') or left['host'] == right['host']):
        raise ValueError('Handoff project binding or distinct host identity does not match configured pair')
    if direction == 'codespace':
        return local, remote, left, right
    return remote, local, right, left


def check(project, agent, session_id, direction, state, *, local=None, remote=None):
    """Validate binding and strict export without ownership writes/native resume."""
    _validate(project, agent, session_id, direction)
    config = _config(project.root, state)
    with ExitStack() as stack:
        local = local or LocalEndpoint(project, state)
        remote = remote or RemoteEndpoint(stack.enter_context(CodespaceConnection(config['codespace'], state=Path(state))), config['remote_root'], project.identity)
        source, target, source_info, target_info = _peers(project, direction, config, local, remote)
        inspection = source.call('inspect', agent=agent, session_id=session_id)
        return dict(status='checked', session_id=session_id, agent=agent, direction=direction, source_host=source_info['host'], destination_host=target_info['host'], inspection=inspection)


def handoff(project, agent, session_id, direction, state, *, local=None, remote=None):
    """Transfer exact-ID custody, retaining recoverable receipts at every boundary.

    Repeating an uncertain request uses its saved nonce and immutable snapshot.
    Different ownership/generation refuses; this API never adopts or force-takes.
    """
    _validate(project, agent, session_id, direction)
    state = Path(state)
    config = _config(project.root, state)
    key = f'{agent}-{session_id}-{direction}'
    with locked(state / 'handoffs' / (key + '.lock')), ExitStack() as stack:
        local = local or LocalEndpoint(project, state)
        remote = remote or RemoteEndpoint(stack.enter_context(CodespaceConnection(config['codespace'], state=state)), config['remote_root'], project.identity)
        source, target, source_info, target_info = _peers(project, direction, config, local, remote)
        binding = dict(source=source_info, destination=target_info, codespace=config['codespace'], direction=direction)
        pointer = state / 'handoffs' / (key + '.json')
        journal = _optional(pointer)
        common = dict(agent=agent, session_id=session_id)
        if journal and journal['binding'] != binding:
            raise ValueError('Pending handoff pairing changed; inspect private receipts')
        if journal and journal.get('status') == 'complete':
            current = source.call('status', **common)
            if (current.get('status') == 'transferred' and current.get('nonce') == journal['result']['nonce']
                    and current.get('generation') == journal['result']['generation']):
                target_current = target.call('status', **common)
                if (target_current.get('host') != target_info['host']
                        or target_current.get('status') not in ('idle', 'running', 'uncertain')
                        or any(target_current.get(key) != journal['result'].get(key) for key in ('nonce', 'generation', 'bundle_id'))):
                    raise ValueError('Completed handoff target ownership receipt changed; inspect both hosts')
                return journal['result']
            if not (current.get('status') == 'idle' and current.get('host') == source_info['host'] and current.get('generation', -1) > journal['result']['generation']):
                raise ValueError('Completed handoff ownership changed; inspect private receipts')
            journal = None
        if journal is None:
            request_id = str(uuid.uuid4())
            journal = dict(schema=1, request_id=request_id, binding=binding, agent=agent, session_id=session_id, status='preparing')
            _save(pointer, journal)
        directory = state / 'handoffs/requests' / journal['request_id']

        def step(name, endpoint, operation, **arguments):
            journal.update(status='pending', operation=name)
            _save(pointer, journal)
            _save(directory / 'receipt.json', journal)
            try:
                result = endpoint.call(operation, **common, **arguments)
            except Exception:
                journal.update(status='uncertain', operation=name)
                _save(pointer, journal)
                _save(directory / 'receipt.json', journal)
                raise
            journal[name] = result
            _save(pointer, journal)
            _save(directory / 'receipt.json', journal)
            return result

        if 'local_backup' not in journal:
            step('local_backup', local, 'backup')
        if 'remote_backup' not in journal:
            step('remote_backup', remote, 'backup')
        prepared = step('source', source, 'source_prepare', request_id=journal['request_id'], destination_host=target_info['host'])
        transfer, bundle = prepared['transfer'], prepared['bundle']
        if (transfer.get('host') != source_info['host'] or transfer.get('destination_host') != target_info['host']
                or transfer.get('generation') != bundle.get('generation') or transfer.get('host') != bundle.get('source_host')):
            raise ValueError('Source snapshot does not match exact ownership transfer generation')
        _save(directory / 'bundle.json', bundle, immutable=True)
        target_status = target.call('status', **common)
        if target_status.get('status') == 'idle' and target_status.get('nonce') == transfer['nonce']:
            if 'committed' not in journal or 'acknowledgment' not in journal:
                raise ValueError('Activated target lacks matching local release receipt; inspect both hosts')
            acknowledgment = journal['acknowledgment']
        else:
            step('reserved', target, 'target_prepare', transfer=transfer)
            imported = step('imported', target, 'import', bundle=bundle)
            step('verified', target, 'verify', bundle=bundle, history=prepared['history'])
            acknowledgment = step('acknowledgment', target, 'accept', transfer=transfer, import_receipt=imported)
        committed = step('committed', source, 'complete', acknowledgment=acknowledgment)
        step('activated', target, 'activate', committed=committed)
        result = dict(status='complete', request_id=journal['request_id'], agent=agent, session_id=session_id, direction=direction, source_host=source_info['host'], destination_host=target_info['host'], nonce=transfer['nonce'], generation=transfer['generation'], bundle_id=acknowledgment['bundle_id'], receipt=str(directory / 'receipt.json'))
        journal.update(status='complete', result=result)
        _save(pointer, journal)
        _save(directory / 'receipt.json', journal)
        return result
