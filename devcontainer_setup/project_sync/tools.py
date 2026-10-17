"""Lazy checksum-pinned Mutagen installation in a private project cache."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

VERSION = '0.18.1'
MANIFEST = Path(__file__).with_name('sync-tools.json')
if not MANIFEST.is_file():
    MANIFEST = Path(__file__).resolve().parents[2] / 'templates/sync-tools.json'


def _download(url):
    for attempt in range(3):
        try:
            with urlopen(url, timeout=60) as response:
                data = response.read(100 * 1024 * 1024 + 1)
            break
        except OSError:
            if attempt == 2:
                data = _curl_download(url)
                break
    if len(data) > 100 * 1024 * 1024:
        raise ValueError('Mutagen archive exceeds download limit.')
    return data


def _curl_download(url):
    # curl is already a host prerequisite and can handle large release assets
    # when Python's HTTP connection repeatedly resets. The caller still checks
    # the complete archive size and the installer verifies its pinned SHA-256.
    with tempfile.TemporaryDirectory(prefix='mutagen-download-') as directory:
        target = Path(directory) / 'archive.tar.gz'
        try:
            result = subprocess.run(
                ['curl', '--fail', '--location', '--silent', '--show-error',
                 '--retry', '1', '--retry-all-errors', '--retry-max-time', '60',
                 '--connect-timeout', '15', '--max-time', '60',
                 '--max-filesize', str(100 * 1024 * 1024), '--output', str(target), '--', url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=125)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Pinned Mutagen download failed; retry when the network is available.') from None
        if result.returncode or not target.is_file():
            raise ValueError('Pinned Mutagen download failed; retry when the network is available.')
        with target.open('rb') as source:
            return source.read(100 * 1024 * 1024 + 1)


def install_mutagen(state: Path, *, asset=None) -> Path:
    from .state import atomic_write, locked, read_json
    system = platform.system().lower()
    arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'amd64', 'amd64': 'amd64'}.get(platform.machine().lower())
    if not arch or system not in {'darwin', 'linux'}:
        raise ValueError('Mutagen supports macOS/Linux arm64/amd64 in this release.')
    if asset is None:
        asset = json.loads(MANIFEST.read_text())['mutagen']['platforms'][system + '_' + arch]
    directory = Path(state) / 'tools' / ('mutagen-' + VERSION + '-' + system + '-' + arch)
    binary = directory / 'mutagen'
    with locked(Path(state) / 'tools.lock', blocking=True):
        if binary.exists() or binary.is_symlink():
            receipt = read_json(directory / 'receipt.json')
            if receipt.get('archive_sha256') != asset['sha256']:
                raise ValueError('Mutagen archive pin changed; existing tool was preserved.')
            for name, digest in receipt.get('files', {}).items():
                path = directory / name
                if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError('Cached Mutagen tool was modified; existing content was preserved.')
            if 'mutagen' not in receipt.get('files', {}):
                raise ValueError('Invalid Mutagen installation receipt.')
            return binary
        data = _download(asset['url'])
        if hashlib.sha256(data).hexdigest() != asset['sha256']:
            raise ValueError('Mutagen archive checksum mismatch; nothing was installed.')
        files = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            for member in archive.getmembers():
                # Do not extract arbitrary archive paths, links or executables.
                if member.name in {'mutagen', 'mutagen-agents.tar.gz'}:
                    if not member.isfile() or member.size > 100 * 1024 * 1024:
                        raise ValueError('Unsafe Mutagen archive member.')
                    if member.name in files:
                        raise ValueError('Duplicate Mutagen archive member.')
                    files[member.name] = archive.extractfile(member).read()
        if 'mutagen' not in files:
            raise ValueError('Mutagen archive lacks the binary.')
        receipt = {'version': VERSION, 'archive_sha256': asset['sha256'],
                   'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
        # Publish receipt last. A partial install is refused on the next attempt.
        for name, content in files.items():
            atomic_write(directory / name, content, mode=0o700 if name == 'mutagen' else 0o600)
        atomic_write(directory / 'receipt.json', (json.dumps(receipt, sort_keys=True) + '\n').encode())
        return binary


def mutagen_env(state: Path):
    from .transport import clean_env
    env = clean_env()
    # Compact versioned basename leaves room for Darwin's 104-byte sun_path.
    directory = Path(state) / 'm181'
    limit = 103 if platform.system() == 'Darwin' else 107
    if len(os.fsencode(str(directory / 'daemon/daemon.sock'))) > limit:
        raise ValueError('Mutagen socket path is too long; select a shorter absolute XDG_STATE_HOME before configuring.')
    env.update(MUTAGEN_DATA_DIRECTORY=str(directory),
               MUTAGEN_SSH_PATH=str(Path(state) / 'transport-bin'),
               MUTAGEN_SSH_CONNECT_TIMEOUT='15')
    return env
