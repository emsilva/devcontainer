"""Verified conversation-only exchange; repositories remain entirely user managed."""
from __future__ import annotations

import base64
import hashlib
import time
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
            or not isinstance(conversations.get('issues'), list)
            or not all(isinstance(row, dict) for row in conversations['entries'] + conversations['issues'])):
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


class ConnectionVerificationError(ValueError):
    """Root or authenticated transport verification failed before exchange.

    `state` is the Codespace state read after the failure, or None when it is unknown.
    """

    def __init__(self, message, state=None):
        super().__init__(message)
        self.state = state


def read_codespace_state(codespace, env):
    """One read-only GET of the Codespace state, made only after a failure; None when unknown.

    Neither the token nor the response body reaches a message or a log.
    """
    from .transport import codespace_state, github_token
    try:
        return codespace_state(codespace, github_token(env))
    except (OSError, ValueError):
        return None


def stopped_message(state):
    return 'Codespace is ' + state + '; no start request was allowed.'


def sync_project_data(connection, project, state, remote_root, *, env=None, historical_roots=(), progress=None):
    """Exchange independent directions; acknowledge custody before native publication."""
    from .conversations import publish_conversations
    from .rpc import RemoteEndpoint
    from . import sync_cycle
    from .workspace import _verify_roots
    state = Path(state)
    store = Store(state)
    endpoint = RemoteEndpoint(connection, remote_root, project.identity)
    remote_store = RemoteStore(endpoint)
    cycle = sync_cycle.begin(project, remote_root)
    def verify():
        try:
            # Identity/authentication is a connection failure, never an optional
            # publication issue. Verify all authorized roots before any transfer.
            with sync_cycle.stage(cycle, 'verify', progress):
                _verify_roots(connection, project, [remote_root, *historical_roots])
        except (OSError, ValueError) as error:
            cycle.update(outcome='failed', completed_at=time.time(),
                         issues=[dict(stage='verify', reason='Project connection verification failed')])
            sync_cycle.emit(cycle, progress)
            # A stopped Codespace fails here too; name its state, not authentication (#48).
            state = read_codespace_state(connection.codespace, connection.env)
            if state is None:
                message = 'Project connection verification failed; Codespace state unknown.'
            elif state != 'Available':
                message = stopped_message(state)
            else:
                message = 'Project connection verification failed; identity/authentication must be valid'
            raise ConnectionVerificationError(message, state) from error
    verify()
    try:
        with sync_cycle.stage(cycle, 'memory', progress):
            cycle['issues'].extend(_memory(project, env=env))
    except OSError:
        cycle['issues'].append(dict(component='memory', reason='Memory preparation incomplete'))
    for name in sync_cycle.DIRECTIONS:
        row = cycle['directions'][name]
        outbound = name == 'desktop_to_codespace'
        source, destination = (store, remote_store) if outbound else (remote_store, store)
        try:
            row['stage'] = 'capture'
            with sync_cycle.stage(cycle, name + ':capture', progress):
                snapshot = (capture(project, store, env=env) if outbound else
                            endpoint.call('capture', historical_roots=list(historical_roots))['snapshot_id'])
                row['snapshot_id'] = snapshot
                manifest = source.load(snapshot)
                validate_manifest(manifest, project.identity, project.project_id)
                row['capture_issues'] = sync_cycle.unique_issues(manifest['conversations']['issues'])
                count = len({(entry.get('agent'), entry.get('session_id')) for entry in manifest['conversations']['entries']})
                unsupported = {(issue.get('agent'), issue['session_id']) for issue in row['capture_issues']
                               if issue.get('session_id') is not None}
                row['counts'].update(captured=count, pending=count, unsupported=len(unsupported))
            row['stage'] = 'transfer'
            with sync_cycle.stage(cycle, name + ':transfer', progress):
                receipt = transfer(source, destination, snapshot, source='desktop' if outbound else 'codespace',
                                   identity=project.identity)
                if not sync_cycle.receipt_matches(receipt, snapshot):
                    raise ValueError('Receiver receipt differs from the source snapshot')
                row['receipt'] = receipt
                row['counts'].update(received=count, received_bytes=sum(len(store.read(d)) for d in set(references(manifest))))
            row['stage'] = 'publication'
            with sync_cycle.stage(cycle, name + ':publication', progress):
                publication = (endpoint.call('publish', snapshot_id=snapshot) if outbound else
                               publish_conversations(project, store, snapshot, state=state, env=env))
                sync_cycle.publication_result(row, manifest, publication)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Keep any completed receipt and continue the opposite direction.
            # Never expose arbitrary remote process output or request bodies.
            row.update(outcome='failed', error=('Conversation stage timed out' if isinstance(error, TimeoutError)
                                               else 'Conversation ' + row['stage'] + ' failed; previous receipts preserved'))
        sync_cycle.emit(cycle, progress)
    if any(row['error'] for row in cycle['directions'].values()):
        # A directional refusal may be a transcript issue or a lost authenticated
        # connection. Recheck the binding before reporting usable connectivity.
        verify()
    for name, row in cycle['directions'].items():
        cycle['issues'].extend(dict(issue, direction=name) for issue in row['capture_issues'] + row['publication_issues'])
        if row['error']:
            cycle['issues'].append(dict(direction=name, stage=row['stage'], reason=row['error']))
    cycle.update(outcome='degraded' if cycle['issues'] or any(row['outcome'] != 'complete' for row in cycle['directions'].values()) else 'complete',
                 stage='complete', completed_at=time.time(), stage_deadline_at=None)
    # Preserve the callable's existing receipt/publication accessors.
    cycle.update(receipt=cycle['directions']['codespace_to_desktop']['receipt'],
                 local_publication=cycle['directions']['codespace_to_desktop']['publication'],
                 remote_publication=cycle['directions']['desktop_to_codespace']['publication'])
    sync_cycle.emit(cycle, progress)
    return cycle
