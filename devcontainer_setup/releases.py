"""Fetch versioned wizard sources without changing installed releases."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .ui import cache_home


def _git(root: Path, *args: str) -> str:
    # A caller may itself run inside a Git hook or an alternate worktree.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull)
    try:
        result = subprocess.run(
            ['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null',
             '-c', 'core.autocrlf=false', *args],
            capture_output=True, text=True, env=env,
        )
    except OSError as error:
        raise ValueError(f'Cannot fetch wizard release: {error}') from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or 'Git failed'
        raise ValueError(f'Cannot fetch wizard release: {detail}')
    return result.stdout.strip()


def _validate(root: Path, source: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f'Invalid release cache at {root}; existing content was preserved.')
    for name in ('scripts/setup.sh', 'devcontainer_setup/__main__.py'):
        path = root / name
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError(f'Release cache {root} has no supported wizard ({name}). '
                             'Existing content was preserved.')
    # Installed runners also come from git archive/codeload and need not have
    # Git metadata. The freshly fetched checkout is the revision authority.
    for entry in _git(source, 'ls-files', '--stage', '-z').split('\0'):
        if not entry:
            continue
        metadata, name = entry.split('\t', 1)
        mode = metadata.split()[0]
        path = root / name
        expected = source / name
        ancestors = Path(name).parents
        safe_parents = not any((root / parent).is_symlink() for parent in ancestors)
        if mode == '120000':
            matches = (safe_parents and path.is_symlink()
                       and os.readlink(path) == os.readlink(expected))
        elif mode in {'100644', '100755'}:
            matches = (safe_parents and not path.is_symlink() and path.is_file()
                       and bool(path.stat().st_mode & 0o100) == (mode == '100755')
                       and path.read_bytes() == expected.read_bytes())
        else:
            raise ValueError(f'Unsupported release source mode {mode} for {name}.')
        if not matches:
            raise ValueError(f'Release cache {root} has modified tracked source ({name}); '
                             'existing content was preserved.')


def fetch_release(
    repository: str = 'https://github.com/emsilva/devcontainer.git',
    ref: str = 'main',
    cache: Path | None = None,
) -> tuple[Path, str]:
    """Resolve a branch/tag/commit and return its immutable cached source tree.

    ``cache`` is the releases directory, containing one checkout per full commit
    SHA. Fetching and validation never execute the downloaded wizard.
    """
    if not repository or not ref or ref.startswith('-'):
        raise ValueError('A repository and a valid branch, tag, or full commit are required.')
    local = Path(repository).expanduser()
    if local.is_dir():
        repository = str(local.resolve())
    if cache is None:
        cache = cache_home() / 'devcontainer-setup/releases'
    cache = Path(cache).expanduser().absolute()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.fetch-', dir=cache) as temporary:
            tree = Path(temporary) / 'source'
            tree.mkdir()
            _git(tree, 'init', '--quiet')
            _git(tree, 'check-ref-format', '--allow-onelevel', ref)
            _git(tree, 'fetch', '--quiet', '--depth=1', '--no-tags', '--', repository, ref)
            sha = _git(tree, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}')
            if not re.fullmatch(r'[0-9a-f]{40}', sha):
                raise ValueError('Fetched wizard revision is not a full SHA-1 commit.')
            _git(tree, 'checkout', '--quiet', '--detach', sha)
            _validate(tree, tree)
            release = cache / sha
            if os.path.lexists(release):
                _validate(release, tree)
                return release, sha
            # copytree creates its destination exclusively, so even a concurrent
            # empty directory cannot be replaced. A failed publication is kept
            # for inspection; cleanup is confined to our temporary fetch tree.
            try:
                shutil.copytree(tree, release, symlinks=True)
            except FileExistsError:
                _validate(release, tree)
            return release, sha
    except OSError as error:
        raise ValueError(f'Cannot fetch wizard release into cache {cache}: {error}') from error
