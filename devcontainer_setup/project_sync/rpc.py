"""Small authenticated-SSH RPC surface for immutable project recovery data."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re

from .checkpoint_store import Store
from .state import discover_project, state_dir

LIMIT = 384 * 1024 * 1024
BATCH_BYTES = 32 * 1024 * 1024
BATCH_ITEMS = 1024
SINGLE_BYTES = 256 * 1024 * 1024
OPERATIONS = {'capture', 'manifest', 'blob', 'put_blob', 'has_blobs', 'get_blobs',
              'put_blobs', 'save', 'acknowledge', 'restore'}


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid checkpoint digest')
    return value


def _digests(values):
    if not isinstance(values, list) or len(values) > BATCH_ITEMS:
        raise ValueError('Checkpoint batch exceeds reference bound')
    result = [_digest(value) for value in values]
    if len(set(result)) != len(result):
        raise ValueError('Checkpoint batch contains duplicate digests')
    return result


def _decode(data, limit):
    if not isinstance(data, str) or len(data) > 4 * ((limit + 2) // 3):
        raise ValueError('Checkpoint blob exceeds bounded size')
    decoded = base64.b64decode(data, validate=True)
    if len(decoded) > limit:
        raise ValueError('Checkpoint blob exceeds bounded size')
    return decoded


class LocalEndpoint:
    def __init__(self, project, state):
        self.project, self.store = project, Store(Path(state))

    def _manifest(self, snapshot_id):
        value = self.store.load(_digest(snapshot_id))
        from .replication import validate_manifest
        validate_manifest(value, self.project.identity, self.project.project_id)
        return value

    def call(self, operation, **values):
        if operation not in OPERATIONS:
            raise ValueError('Unsupported checkpoint operation')
        if operation == 'capture':
            from .replication import capture
            return {'snapshot_id': capture(self.project, self.store)}
        if operation == 'has_blobs':
            missing, sizes = [], {}
            for digest in _digests(values['digests']):
                try:
                    sizes[digest] = len(self.store.read(digest))
                except FileNotFoundError:
                    missing.append(digest)
            return {'missing': missing, 'sizes': sizes}
        if operation == 'get_blobs':
            blobs, size = {}, 0
            for digest in _digests(values['digests']):
                data = self.store.read(digest)
                size += len(data)
                if size > BATCH_BYTES:
                    raise ValueError('Checkpoint batch exceeds bounded size')
                blobs[digest] = base64.b64encode(data).decode()
            return {'blobs': blobs}
        if operation == 'put_blobs':
            blobs = values['blobs']
            if not isinstance(blobs, dict):
                raise ValueError('Checkpoint batch must contain a blob map')
            digests = _digests(list(blobs))
            decoded, size = {}, 0
            for digest in digests:
                data = _decode(blobs[digest], BATCH_BYTES - size)
                size += len(data)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('Checkpoint blob digest differs')
                decoded[digest] = data
            # Validate the complete request before publishing any immutable data.
            for digest, data in decoded.items():
                if self.store.put(data) != digest:
                    raise ValueError('Checkpoint blob digest differs')
            return {'digests': digests}
        if operation == 'blob':
            try:
                data = self.store.read(_digest(values['digest']))
            except FileNotFoundError:
                return {'missing': True}
            if len(data) > SINGLE_BYTES:
                raise ValueError('Checkpoint blob exceeds bounded size')
            return {'data': base64.b64encode(data).decode()}
        if operation == 'put_blob':
            return {'digest': self.store.put(_decode(values['data'], SINGLE_BYTES))}
        if operation == 'save':
            manifest = values['manifest']
            from .replication import validate_manifest
            validate_manifest(manifest, self.project.identity, self.project.project_id)
            return {'snapshot_id': self.store.save(manifest)}
        snapshot_id = _digest(values['snapshot_id'])
        if operation == 'restore':
            expected = Path('/workspaces/.devcontainer-work') / self.project.project_id / snapshot_id
            if str(expected) != values['destination']:
                raise ValueError('Recovery destination differs from owned project checkout')
        manifest = self._manifest(snapshot_id)
        if operation == 'manifest':
            return {'manifest': manifest}
        if operation == 'acknowledge':
            return self.store.acknowledge(snapshot_id, source=values['source'])
        from .project_files import restore_files
        # Remote callers may create only this project's immutable-id checkout.
        destination = Path('/workspaces/.devcontainer-work') / self.project.project_id / snapshot_id
        if str(destination) != values['destination']:
            raise ValueError('Recovery destination differs from owned project checkout')
        if destination.exists():
            from .state import read_json
            receipt = read_json(self.store.state / 'remote-recovery' / (snapshot_id + '.json'))
            found = discover_project(destination)
            if receipt.get('root') != str(destination) or found.identity != self.project.identity:
                raise ValueError('Existing recovery checkout lacks matching ownership')
        else:
            from .state import _parent
            with _parent(destination, create=True):
                pass
            restore_files(self.store, manifest['files'], destination)
            from .state import atomic_write
            atomic_write(self.store.state / 'remote-recovery' / (snapshot_id + '.json'),
                         json.dumps({'root': str(destination)}).encode())
        return {'root': str(destination)}


def _dispatch(request):
    try:
        project = discover_project(Path(request['root']))
        if str(project.root) != request['root'] or project.identity != request['identity']:
            raise ValueError('Remote project binding differs')
        result = LocalEndpoint(project, state_dir(project)).call(request['operation'], **request.get('values', {}))
        return {'ok': True, 'result': result}
    except Exception:
        return {'ok': False, 'error': 'Checkpoint operation refused; source data and previous checkpoints preserved'}


_BOOTSTRAP = '''import base64,json,pathlib,sys,tempfile
limit=384*1024*1024
raw=sys.stdin.buffer.read(limit+1)
if len(raw)>limit:raise ValueError("RPC payload exceeds limit")
payload=json.loads(raw)
with tempfile.TemporaryDirectory(prefix="project-checkpoint-") as directory:
 root=pathlib.Path(directory);package=root/"project_sync";package.mkdir(mode=0o700)
 for name,source in payload["modules"].items():
  if not name.isidentifier():raise ValueError("Invalid source module")
  path=package/(name+".py");path.write_bytes(base64.b64decode(source,validate=True));path.chmod(0o600)
 sys.path.insert(0,str(root))
 from project_sync.rpc import _dispatch
 response=json.dumps(_dispatch(payload["request"]),sort_keys=True).encode()
 if len(response)>limit:raise ValueError("RPC response exceeds limit")
 sys.stdout.buffer.write(response)
'''


class RemoteEndpoint:
    def __init__(self, connection, root, identity):
        self.connection, self.root, self.identity = connection, str(root), identity

    def call(self, operation, **values):
        if operation not in OPERATIONS:
            raise ValueError('Unsupported checkpoint operation')
        modules = {p.stem: base64.b64encode(p.read_bytes()).decode()
                   for p in Path(__file__).parent.glob('*.py') if p.stem != '__main__'}
        payload = json.dumps(dict(modules=modules, request=dict(root=self.root,
            identity=self.identity, operation=operation, values=values))).encode()
        if len(payload) > LIMIT:
            raise ValueError('Checkpoint transfer exceeds bounded size')
        code = 'import os,sys;os.execv(sys.executable,[sys.executable,"-I","-B","-c",' + repr(_BOOTSTRAP) + '])'
        raw = self.connection.python(code, payload=payload, timeout=180)
        if len(raw) > LIMIT:
            raise ValueError('Checkpoint response exceeds bounded size')
        response = json.loads(raw)
        if response.get('ok') is not True or not isinstance(response.get('result'), dict):
            raise ValueError('Remote checkpoint operation refused; previous recovery data preserved')
        return response['result']
