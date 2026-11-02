"""Private immutable recovery objects and durable receiver acknowledgements."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .state import _parent, _regular, atomic_write, locked, read_json


_DIGEST = re.compile(r'[0-9a-f]{64}')
KEEP_DAYS = 7
# A capture or transfer puts its objects before it saves their manifest.
GRACE_SECONDS = 86400
_SOURCES = {'local', 'codespace', 'desktop'}


def _digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError('Invalid SHA256 digest')
    return value


def _canonical(value: dict) -> bytes:
    if not isinstance(value, dict):
        raise ValueError('Manifest must be an object')
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode()


def _references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError('Manifest keys must be strings')
            if key == 'blob':
                yield _digest(child)
            else:
                yield from _references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _references(child)


def _strings(value):
    """Every digest-shaped string in a JSON value; extra matches only keep more."""
    if isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, str) and _DIGEST.fullmatch(value):
        yield value


class Store:
    """Content-addressed blobs/manifests; saving never implies receipt."""

    def __init__(self, state: Path):
        self.state = Path(state)
        if not self.state.is_absolute() or '..' in self.state.parts:
            raise ValueError('Store state path must be absolute without traversal')
        self.root = self.state / 'checkpoints'

    def _path(self, family: str, digest: str) -> Path:
        return self.root / family / _digest(digest)

    def _read(self, path: Path, digest: str) -> bytes:
        with _parent(path, private=True) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as source:
                _regular(source.fileno(), allow_unlinked=True)
                data = source.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Corrupt checkpoint content: SHA256 hash mismatch')
        return data

    def _immutable(self, family: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(family, digest)
        with locked(self.root / 'store.lock', blocking=True):
            try:
                existing = self._read(path, digest)
            except FileNotFoundError:
                atomic_write(path, data, mode=0o400)
            else:
                if existing != data:
                    raise ValueError('Immutable checkpoint content differs')
                # Reused content is in use again: restart its collection grace.
                with _parent(path, private=True) as (parent, name):
                    os.utime(name, dir_fd=parent, follow_symlinks=False)
        return digest

    def put(self, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise TypeError('Checkpoint content must be bytes')
        return self._immutable('objects', data)

    def read(self, digest: str) -> bytes:
        return self._read(self._path('objects', digest), digest)

    def present(self, digest: str) -> int | None:
        """The size of a stored object, which is about to be referenced again; None when absent."""
        path = self._path('objects', digest)
        with locked(self.root / 'store.lock', blocking=True):
            try:
                with _parent(path, private=True) as (parent, name):
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    # Restart its collection grace, as a put of the same content would.
                    os.utime(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
        return info.st_size

    def _verify(self, manifest: dict) -> None:
        for digest in set(_references(manifest)):
            self.read(digest)

    def save(self, manifest: dict) -> str:
        data = _canonical(manifest)
        self._verify(manifest)
        return self._immutable('manifests', data)

    def _manifest(self, snapshot_id: str) -> dict:
        data = self._read(self._path('manifests', snapshot_id), snapshot_id)
        try:
            manifest = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise ValueError('Corrupt checkpoint manifest') from error
        if _canonical(manifest) != data:
            raise ValueError('Checkpoint manifest is not canonical')
        return manifest

    def load(self, snapshot_id: str) -> dict:
        manifest = self._manifest(snapshot_id)
        self._verify(manifest)
        return manifest

    def _receipt(self, source: str) -> Path:
        if source not in _SOURCES:
            raise ValueError('Unknown checkpoint source')
        return self.root / 'receipts' / (source + '.json')

    def acknowledge(self, snapshot_id: str, *, source: str) -> dict:
        path = self._receipt(source)
        with locked(self.root / 'store.lock', blocking=True):
            manifest = self.load(snapshot_id)
            if source == 'codespace':
                # Preserve the pre-upgrade receipt before this one replaces it.
                self._seed_relay()
                self._record_received(manifest)
            receipt = {'snapshot_id': snapshot_id,
                       'received_at': datetime.now(timezone.utc).isoformat()}
            atomic_write(path, _canonical(receipt), mode=0o600)
        return receipt

    def latest(self, source: str) -> dict | None:
        path = self._receipt(source)
        try:
            receipt = read_json(path)
        except FileNotFoundError:
            return None
        if (set(receipt) != {'snapshot_id', 'received_at'}
                or not isinstance(receipt['received_at'], str)):
            raise ValueError('Invalid checkpoint acknowledgement')
        parsed = datetime.fromisoformat(receipt['received_at'])
        if parsed.tzinfo is None:
            raise ValueError('Checkpoint acknowledgement has no timezone')
        self.load(receipt['snapshot_id'])
        return receipt

    def _relay_records(self, *, issues=None):
        records = []
        for path in sorted((self.root / 'relay').glob('*.json')):
            try:
                record = read_json(path)
                if (record['schema'] != 1 or not isinstance(record['entry'], dict)
                        or not isinstance(record['received_at'], str)):
                    raise ValueError('Invalid relay record')
                if hashlib.sha256(_canonical(record['entry'])).hexdigest() != path.stem:
                    raise ValueError('Relay record entry digest differs')
            except (OSError, ValueError, KeyError, TypeError):
                if issues is None:
                    raise  # Collection must not delete objects with an unreadable root.
                issues.append(dict(agent=None, session_id=None,
                                   reason='Relayed conversation checkpoint is unreadable'))
            else:
                records.append((path, record))
        return records

    def _head(self, entry: dict) -> bytes | None:
        try:
            head = next(item for item in entry['files'] if item['session_id'] == entry['session_id'])
            return self.read(head['blob'])
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return None  # An unreadable head proves no append relationship.

    def _record_received(self, manifest: dict) -> None:
        """Register received entries while the caller holds store.lock."""
        try:
            entries = manifest['conversations']['entries']
        except (OSError, ValueError, KeyError, TypeError):
            return
        if not isinstance(entries, list):
            return
        for entry in entries:
            try:
                digest = hashlib.sha256(_canonical(entry)).hexdigest()
            except (OSError, ValueError, KeyError, TypeError):
                continue
            path = self.root / 'relay' / (digest + '.json')
            records = self._relay_records(issues=[])
            if any(old_path == path for old_path, _ in records):
                continue  # An identical retry needs no head-blob reads.
            head = self._head(entry)
            retired = []
            for old_path, record in records:
                old = record['entry']
                if (old.get('agent'), old.get('session_id')) != (entry.get('agent'), entry.get('session_id')):
                    continue
                old_head = self._head(old)
                if head is None or old_head is None:
                    continue
                if old_head.startswith(head):
                    break  # A live revision already contains the new entry.
                if head.startswith(old_head):
                    retired.append(old_path)
            else:
                record = dict(schema=1, entry=entry, received_at=datetime.now(timezone.utc).isoformat())
                # A crash must leave an extra old record, never lose the new one.
                atomic_write(path, _canonical(record), mode=0o600)
                for old_path in retired:
                    with _parent(old_path, private=True) as (parent, name):
                        os.unlink(name, dir_fd=parent)

    def _seed_relay(self) -> None:
        """Migrate retained receipt/journal pointers once, under store.lock."""
        marker = self.root / 'relay' / 'seeded'
        try:
            read_json(marker)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        else:
            return
        try:
            receipt = read_json(self._receipt('codespace'))
            # Read verified manifest text without requiring all its blobs: a lost
            # blob becomes a per-entry relay issue, not a failed migration.
            manifest = self._manifest(receipt['snapshot_id'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        else:
            self._record_received(manifest)
        for path in sorted((self.state / 'conversation-imports').glob('*/receipt.json')):
            try:
                receipt = read_json(path)
                if receipt.get('status') != 'complete' or path.with_name('superseded.json').exists():
                    continue
                entry = receipt.get('source_checkpoint')
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if isinstance(entry, dict):
                self._record_received(dict(conversations=dict(entries=[entry])))
        atomic_write(marker, _canonical(dict(schema=1)), mode=0o600)

    def relay_entries(self) -> tuple[list[dict], list[dict]]:
        """Verified received conversations, oldest first, and per-entry issues."""
        entries, issues = [], []
        with locked(self.root / 'store.lock', blocking=True):
            self._seed_relay()
            for _, record in sorted(self._relay_records(issues=issues), key=lambda item: item[1]['received_at']):
                entry = record['entry']
                try:
                    self._verify(entry)
                except (OSError, ValueError):
                    issues.append(dict(agent=entry['agent'], session_id=entry['session_id'],
                                       reason='Relayed conversation checkpoint is unreadable'))
                else:
                    entries.append(dict(entry, relayed=True))
        return entries, issues

    def collect(self, *, apply: bool = False, keep_days: float = KEEP_DAYS,
                grace_seconds: float = GRACE_SECONDS) -> dict:
        """Remove manifests no receipt names that are older than keep_days, then the
        objects nothing kept refers to. A dry run (the default) only counts them."""
        now = time.time()
        with locked(self.root / 'store.lock', blocking=True):
            named = set()
            for source in sorted(_SOURCES):
                try:
                    named.add(_digest(read_json(self._receipt(source))['snapshot_id']))
                except FileNotFoundError:
                    pass
            manifests = self._entries('manifests')
            kept = {digest for digest, info in manifests.items()
                    if digest in named or info.st_mtime >= now - keep_days * 86400}
            # Manifest text only, never blob bodies. Raises before anything is
            # deleted when a kept or named manifest cannot be read.
            live = set()
            for digest in kept | named:
                data = self._read(self._path('manifests', digest), digest)
                live.update(_references(json.loads(data)))
            # Published conversations keep proving their source against these blobs.
            for receipt in (self.state / 'conversation-imports').glob('*/receipt.json'):
                live.update(_strings(read_json(receipt)))
            for _, record in self._relay_records():
                live.update(_strings(record))
            doomed = [('manifests', digest, info) for digest, info in manifests.items() if digest not in kept]
            doomed += [('objects', digest, info) for digest, info in self._entries('objects').items()
                       if digest not in live and info.st_mtime < now - grace_seconds]
            if apply:
                for family, digest, _ in doomed:
                    with _parent(self._path(family, digest), private=True) as (parent, name):
                        os.unlink(name, dir_fd=parent)
        return {'manifests': sum(family == 'manifests' for family, _, _ in doomed),
                'objects': sum(family == 'objects' for family, _, _ in doomed),
                'bytes': sum(info.st_size for family, _, info in doomed if family == 'objects'),
                'applied': apply}

    def _entries(self, family: str) -> dict:
        directory = self.root / family
        if not directory.is_dir():
            return {}
        return {entry.name: entry.stat(follow_symlinks=False) for entry in os.scandir(directory)
                if _DIGEST.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)}
