"""Small Gum adapter; downloaded binaries are pinned and checksum checked."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile

GUM_VERSION = '2.0.2'
GUM_CHECKSUMS = {
    ('Darwin', 'arm64'): '4777a69b1170b8db23c95d5889fb32186cfda1a3ac950d339aa17e3513633890',
    ('Darwin', 'x86_64'): '5374966c7c7199ea879fcaa525ddc6d447a098d3d35496e430a9a1ef38d30485',
    ('Linux', 'arm64'): '8ebf8b54ec1e8c81f2bb58b59ff9b70998186a4d11375f0cf357b80e0ccfa1d5',
    ('Linux', 'x86_64'): 'd842e06d93dbed90af48cb8dd10698db6f22e331fc40346bb37bbc753109edc2',
}


def cache_home() -> Path:
    configured = os.environ.get('XDG_CACHE_HOME', '')
    return Path(configured) if configured and Path(configured).is_absolute() else Path.home() / '.cache'


def gum_binary() -> str:
    installed = shutil.which('gum')
    if installed:
        return installed
    machine = {'aarch64': 'arm64', 'AMD64': 'x86_64'}.get(platform.machine(), platform.machine())
    key = (platform.system(), machine)
    if key not in GUM_CHECKSUMS:
        raise ValueError('Install Gum for this platform before running interactive setup.')
    destination = cache_home() / 'devcontainer-setup' / 'tools' / 'gum' / GUM_VERSION / 'gum'
    if destination.is_file() and os.access(destination, os.X_OK):
        return str(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    asset = f'gum_{GUM_VERSION}_{key[0]}_{key[1]}.tar.gz'
    print(f'Downloading Gum {GUM_VERSION} to the user cache...', flush=True)
    with tempfile.TemporaryDirectory(prefix='.download-', dir=destination.parent) as temporary:
        archive = Path(temporary) / asset
        result = subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                                 '--connect-timeout', '15', '--max-time', '120',
                                 f'https://github.com/charmbracelet/gum/releases/download/v{GUM_VERSION}/{asset}',
                                 '-o', str(archive)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f'Could not download Gum: {result.stderr.strip()}')
        if hashlib.sha256(archive.read_bytes()).hexdigest() != GUM_CHECKSUMS[key]:
            raise ValueError('Gum download checksum mismatch; no binary was installed.')
        with tarfile.open(archive) as package:
            member = next((m for m in package if m.isfile() and Path(m.name).name == 'gum'), None)
            if member is None or member.size > 64 * 1024 * 1024:
                raise ValueError('Gum archive contains no supported binary.')
            data = package.extractfile(member).read()
        staged = Path(temporary) / 'gum'
        staged.write_bytes(data)
        staged.chmod(0o755)
        os.replace(staged, destination)
    return str(destination)


class Cancelled(Exception):
    pass


class Gum:
    def __init__(self):
        self.binary = gum_binary()

    def _run(self, args: list[str]) -> str:
        result = subprocess.run([self.binary, *args], stdout=subprocess.PIPE, text=True)
        if result.returncode:
            raise Cancelled()
        return result.stdout.strip()

    def choose(self, title: str, options: list[str], selected: str | None = None) -> str:
        args = ['choose', '--header', title, '--height', '12']
        if selected in options:
            args += ['--selected', selected]
        return self._run([*args, '--', *options])

    def multi(self, title: str, options: list[str], selected: list[str]) -> list[str]:
        args = ['choose', '--header', title, '--height', '12', '--no-limit']
        if selected:
            args += ['--selected', ','.join(selected)]
        output = self._run([*args, '--', *options])
        return output.splitlines() if output else []

    def input(self, title: str, value: str = '') -> str:
        return self._run(['input', '--header', title, '--value', value])

    def confirm(self, title: str, default: bool = True) -> bool:
        result = subprocess.run([self.binary, 'confirm', title, f'--default={str(default).lower()}'])
        if result.returncode not in (0, 1):
            raise Cancelled()
        return result.returncode == 0
