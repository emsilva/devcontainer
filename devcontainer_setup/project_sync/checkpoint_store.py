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

    def load(self, snapshot_id: str) -> dict:
        data = self._read(self._path('manifests', snapshot_id), snapshot_id)
        try:
            manifest = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise ValueError('Corrupt checkpoint manifest') from error
        if _canonical(manifest) != data:
            raise ValueError('Checkpoint manifest is not canonical')
        self._verify(manifest)
        return manifest

    def _receipt(self, source: str) -> Path:
        if source not in _SOURCES:
            raise ValueError('Unknown checkpoint source')
        return self.root / 'receipts' / (source + '.json')

    def acknowledge(self, snapshot_id: str, *, source: str) -> dict:
        path = self._receipt(source)
        with locked(self.root / 'store.lock', blocking=True):
            self.load(snapshot_id)
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
