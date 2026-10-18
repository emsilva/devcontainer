"""Verified conversation-only exchange; repositories remain entirely user managed."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from .checkpoint_store import Store
from .rpc import BATCH_BYTES, BATCH_ITEMS, SINGLE_BYTES, _decode, _digests


def capture_conversations(*args, **kwargs):
    from .conversations import capture_conversations as collect
    return collect(*args, **kwargs)


def references(value):
    if isinstance(value, dict):
        if 'blob' in value:
            digest = value['blob']
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Invalid checkpoint blob digest')
            yield digest
        for item in value.values():
            yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def capture(project, store, *, env=None, historical_roots=()):
    conversations = capture_conversations(project, store, state=store.state, env=env, historical_roots=historical_roots)
    return store.save(dict(schema=2,
        project=dict(identity=project.identity, project_id=project.project_id, root=str(project.root)),
        conversations=conversations,
        producer=dict(format='devcontainer-project-conversations-v2')))


def validate_manifest(manifest, identity, project_id=None):
    if (not isinstance(manifest, dict) or manifest.get('schema') != 2
            or set(manifest) != {'schema', 'project', 'conversations', 'producer'}):
        raise ValueError('New transfers require conversation-only schema-2 manifests')
    project = manifest.get('project')
    if (not isinstance(project, dict) or project.get('identity') != identity
            or project.get('project_id') != hashlib.sha256(identity.encode()).hexdigest()[:24]
            or (project_id is not None and project.get('project_id') != project_id)):
        raise ValueError('Checkpoint project identity differs')
    conversations = manifest.get('conversations')
    if (not isinstance(conversations, dict) or not isinstance(conversations.get('entries'), list)
            or not isinstance(conversations.get('issues'), list)):
        raise ValueError('Invalid conversation-only manifest')
    list(references(manifest))


def transfer(source_store, destination, snapshot_id, *, source, identity=None):
    manifest = source_store.load(snapshot_id)
    validate_manifest(manifest, identity or manifest.get('project', {}).get('identity', ''))
    digests = sorted(set(references(manifest)))
    if isinstance(destination, RemoteStore):
        missing = destination.missing(digests)
    else:
        missing = []
        for digest in digests:
            try:
                destination.read(digest)
            except FileNotFoundError:
                missing.append(digest)
    bodies = (source_store.read_many(missing) if isinstance(source_store, RemoteStore)
              else ((digest, source_store.read(digest)) for digest in missing))
    if isinstance(destination, RemoteStore):
        destination.put_many(bodies)
    else:
        for digest, data in bodies:
            if destination.put(data) != digest:
                raise ValueError('Checkpoint blob digest differs')
    if destination.save(manifest) != snapshot_id:
        raise ValueError('Checkpoint manifest digest differs')
    return destination.acknowledge(snapshot_id, source=source)


class RemoteStore:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def load(self, snapshot_id):
        return self.endpoint.call('manifest', snapshot_id=snapshot_id)['manifest']

    def read(self, digest):
        _digests([digest])
        response = self.endpoint.call('blob', digest=digest)
        if response.get('missing') is True:
            raise FileNotFoundError(digest)
        data = _decode(response['data'], SINGLE_BYTES)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Remote blob digest differs')
        return data

    def put(self, data):
        if len(data) > SINGLE_BYTES:
            raise ValueError('Checkpoint blob exceeds bounded size')
        digest = hashlib.sha256(data).hexdigest()
        if self.endpoint.call('put_blob', data=base64.b64encode(data).decode())['digest'] != digest:
            raise ValueError('Remote blob digest differs')
        return digest

    def _inventory(self, digests):
        missing, sizes = [], {}
        for start in range(0, len(digests), BATCH_ITEMS):
            group = digests[start:start + BATCH_ITEMS]
            response = self.endpoint.call('has_blobs', digests=group)
            absent, found = response['missing'], response['sizes']
            if (not isinstance(found, dict) or not isinstance(absent, list)
                    or len(set(absent)) != len(absent) or set(absent) & set(found)
                    or set(absent) | set(found) != set(group)
                    or any(type(size) is not int or size < 0 for size in found.values())):
                raise ValueError('Remote checkpoint inventory differs')
            missing.extend(absent)
            sizes.update(found)
        return missing, sizes

    def missing(self, digests):
        return self._inventory(digests)[0]

    def read_many(self, digests):
        absent, sizes = self._inventory(digests)
        if absent:
            raise FileNotFoundError(absent[0])
        group, size = [], 0
        for digest in digests:
            length = sizes[digest]
            if group and (size + length > BATCH_BYTES or len(group) == BATCH_ITEMS):
                yield from self._read_batch(group)
                group, size = [], 0
            if length > BATCH_BYTES:
                yield digest, self.read(digest)
            else:
                group.append(digest)
                size += length
        if group:
            yield from self._read_batch(group)

    def _read_batch(self, digests):
        response = self.endpoint.call('get_blobs', digests=digests)['blobs']
        if not isinstance(response, dict) or set(response) != set(digests):
            raise ValueError('Remote checkpoint batch differs')
        bodies, size = [], 0
        for digest in digests:
            data = _decode(response[digest], BATCH_BYTES - size)
            size += len(data)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('Remote blob digest differs')
            bodies.append((digest, data))
        yield from bodies

    def put_many(self, bodies):
        group, size = {}, 0
        for digest, data in bodies:
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('Checkpoint blob digest differs')
            if group and (size + len(data) > BATCH_BYTES or len(group) == BATCH_ITEMS):
                self._put_batch(group)
                group, size = {}, 0
            if len(data) > BATCH_BYTES:
                if self.put(data) != digest:
                    raise ValueError('Remote blob digest differs')
            else:
                group[digest] = base64.b64encode(data).decode()
                size += len(data)
        if group:
            self._put_batch(group)

    def _put_batch(self, blobs):
        digests = self.endpoint.call('put_blobs', blobs=blobs)['digests']
        if (not isinstance(digests, list) or len(digests) != len(blobs)
                or set(digests) != set(blobs)):
            raise ValueError('Remote blob batch digests differ')

    def save(self, manifest):
        return self.endpoint.call('save', manifest=manifest)['snapshot_id']

    def acknowledge(self, snapshot_id, *, source):
        return self.endpoint.call('acknowledge', snapshot_id=snapshot_id, source=source)


def _memory(project, *, env=None):
    from .notes import configure_memory
    try:
        configure_memory(project, env=env)
    except (OSError, ValueError) as error:
        from .sessions import display_text
        return [dict(component='memory', reason=display_text(str(error), 400))]
    return []


def sync_project_data(connection, project, state, remote_root, *, env=None, historical_roots=()):
    """Publish each direction independently of scratch and retain verified receipts."""
    from .conversations import publish_conversations
    from .rpc import RemoteEndpoint
    state = Path(state)
    store = Store(state)
    endpoint = RemoteEndpoint(connection, remote_root, project.identity)
    issues = _memory(project, env=env)
    snapshot_id = capture(project, store, env=env)
    remote_store = RemoteStore(endpoint)
    transfer(store, remote_store, snapshot_id, source='desktop', identity=project.identity)
    remote_publication = endpoint.call('publish', snapshot_id=snapshot_id)
    # One capture validates every historical root and authorizes their combined
    # structural scope/ancestor closure. Its single receipt covers the complete
    # remote inventory rather than only the final historical checkout.
    captured = endpoint.call('capture', historical_roots=list(historical_roots))['snapshot_id']
    receipt = transfer(remote_store, store, captured, source='codespace', identity=project.identity)
    local_publication = publish_conversations(project, store, captured, state=state, env=env)
    issues.extend(remote_publication.get('issues', []))
    issues.extend(local_publication['issues'])
    return dict(schema=2, receipt=receipt, local_publication=local_publication,
                remote_publication=remote_publication, issues=issues)
