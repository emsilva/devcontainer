"""Review and commit a bounded set of generated files without losing user work."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile


class ReviewChanged(ValueError):
    """A normal Git hook changed the reviewed candidate before committing."""


def git(project: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(['git', '-C', str(project), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or 'Git command failed')
    return result.stdout.strip()


def proposed_branch(project: Path) -> str:
    current = git(project, 'branch', '--show-current')
    if not current:
        raise ValueError('Detached HEAD: choose a working branch before setup.')
    default = git(project, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD', check=False)
    default = default.removeprefix('origin/') or 'main'
    if current not in {default, 'main', 'master'}:
        return current
    candidate = 'setup/devcontainer'
    number = 2
    while git(project, 'rev-parse', '--verify', f'refs/heads/{candidate}', check=False):
        candidate = f'setup/devcontainer-{number}'
        number += 1
    return candidate


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_GUARD = r'''#!/usr/bin/env python3
import hashlib, json, os, pathlib, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
original = pathlib.Path(os.environ['DEVCONTAINER_ORIGINAL_HOOKS']) / name
if original.is_file() and os.access(original, os.X_OK):
    result = subprocess.run([str(original), *sys.argv[1:]])
    if result.returncode:
        sys.exit(result.returncode)
tree = subprocess.check_output(['git', 'write-tree'], text=True).strip()
head = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], capture_output=True, text=True).stdout.strip()
branch = subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip()
changed = (tree != os.environ['DEVCONTAINER_REVIEWED_TREE']
           or head != os.environ['DEVCONTAINER_REVIEWED_HEAD']
           or branch != os.environ['DEVCONTAINER_REVIEWED_BRANCH'])
for name, expected in json.loads(pathlib.Path(os.environ['DEVCONTAINER_REVIEW_MANIFEST']).read_text()).items():
    path = pathlib.Path(name)
    if ((expected is None and (path.exists() or path.is_symlink()))
            or (expected is not None and (not path.is_file() or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected))):
        changed = True
if changed:
    print('Setup review changed during Git hooks; review the new diff before committing.', file=sys.stderr)
    sys.exit(1)
'''


class GitPlan:
    def __init__(self, project: Path, files: dict[str, bytes | None], branch: str | None = None):
        self.project = Path(git(Path(project), 'rev-parse', '--show-toplevel')).resolve()
        self.files = dict(files)
        self.head = git(self.project, 'rev-parse', '--verify', 'HEAD', check=False)
        self.start_branch = git(self.project, 'branch', '--show-current')
        if not self.start_branch:
            raise ValueError('Detached HEAD: choose a working branch before setup.')
        self.branch = branch or self.start_branch
        git(self.project, 'check-ref-format', '--branch', self.branch)
        if git(self.project, 'diff', '--cached', '--name-only'):
            raise ValueError('Existing staged changes: commit or unstage them before setup.')
        self.original: dict[str, bytes | None] = {}
        for name in self.files:
            self._safe_path(name)
            if git(self.project, 'status', '--porcelain', '--untracked-files=all', '--', name):
                raise ValueError(f'{name} has existing changes; preserve or commit them before setup.')
            path = self.project / name
            self.original[name] = path.read_bytes() if path.exists() else None
        self.changed = [name for name, data in self.files.items() if self.original[name] != data]
        self.applied = False
        self.reviewed_tree: str | None = None

    def _safe_path(self, name: str) -> None:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] == '.git':
            raise ValueError(f'Unsafe generated path: {name}')
        candidate = self.project
        for part in path.parts:
            candidate /= part
            if candidate.is_symlink():
                raise ValueError(f'Generated path crosses a symlink: {name}')
        if candidate.exists() and not candidate.is_file():
            raise ValueError(f'Generated path is not a regular file: {name}')

    def diff(self) -> str:
        lines: list[str] = []
        for name in self.changed:
            old = (self.original[name] or b'').decode('utf-8', errors='replace').splitlines(keepends=True)
            new = (self.files[name] or b'').decode('utf-8', errors='replace').splitlines(keepends=True)
            lines.extend(difflib.unified_diff(old, new, fromfile=f'a/{name}',
                tofile='/dev/null' if self.files[name] is None else f'b/{name}'))
        return ''.join(lines)

    def apply(self) -> None:
        if git(self.project, 'rev-parse', '--verify', 'HEAD', check=False) != self.head:
            raise ValueError('HEAD changed during review; rerun setup.')
        if git(self.project, 'branch', '--show-current') != self.start_branch:
            raise ValueError('Working branch changed during review; rerun setup.')
        if git(self.project, 'diff', '--cached', '--name-only'):
            raise ValueError('Index changed during review; preserve the staged changes and rerun setup.')
        for name, original in self.original.items():
            self._safe_path(name)
            path = self.project / name
            actual = path.read_bytes() if path.exists() else None
            if actual != original:
                raise ValueError(f'{name} changed during review; refusing to overwrite it.')
        if not self.changed:
            return
        if self.branch != self.start_branch:
            git(self.project, 'switch', '-c', self.branch)
        for name in self.changed:
            path = self.project / name
            if self.files[name] is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = path.stat().st_mode & 0o777 if path.exists() else (0o755 if name.endswith('.sh') else 0o644)
            fd, staged = tempfile.mkstemp(prefix='.setup-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(self.files[name])
                os.chmod(staged, mode)
                os.replace(staged, path)
            finally:
                if os.path.exists(staged):
                    os.unlink(staged)
        self.reviewed_tree = self._build_reviewed_tree()
        git(self.project, 'add', '--', *self.changed)
        self.applied = True

    def _build_reviewed_tree(self) -> str:
        # The shared index is mutable user state, never the source of approval.
        with tempfile.TemporaryDirectory(prefix='devcontainer-index-') as temporary:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'))

            def index_git(*args: str, data: bytes | None = None) -> str:
                result = subprocess.run(['git', '-C', str(self.project), *args],
                                        input=data, env=env, capture_output=True)
                if result.returncode:
                    raise ValueError(result.stderr.decode().strip() or 'Git command failed')
                return result.stdout.decode().strip()

            index_git('read-tree', self.head or '--empty')
            filemode = git(self.project, 'config', '--bool', 'core.filemode', check=False) != 'false'
            for name in self.changed:
                if self.files[name] is None:
                    index_git('update-index', '--force-remove', '--', name)
                    continue
                original = index_git('ls-files', '--stage', '--', name)
                mode = '100755' if (self.project / name).stat().st_mode & 0o100 else '100644'
                if original and not filemode:
                    mode = original.split()[0]
                blob = index_git('hash-object', '-w', '--path', name, '--stdin', data=self.files[name])
                index_git('update-index', '--add', '--cacheinfo', mode, blob, name)
            return index_git('write-tree')

    def _review_changed(self, tree: str) -> bool:
        if git(self.project, 'write-tree') != tree:
            return True
        for name in self.changed:
            self._safe_path(name)
            path = self.project / name
            actual = path.read_bytes() if path.exists() else None
            if actual != self.files[name]:
                return True
        return False

    def _check_identity(self) -> None:
        if git(self.project, 'rev-parse', '--verify', 'HEAD', check=False) != self.head:
            raise ValueError('HEAD changed since review; inspect the repository before retrying.')
        if git(self.project, 'branch', '--show-current') != self.branch:
            raise ValueError('Working branch changed since review; inspect the repository before retrying.')

    def accept_hook_changes(self) -> None:
        self._check_identity()
        staged = set(git(self.project, 'diff', '--cached', '--name-only').splitlines())
        if staged - set(self.changed):
            raise ValueError('Hooks staged unrelated files; preserve and unstage them before retrying.')
        for name in self.changed:
            self._safe_path(name)
            path = self.project / name
            if self.files[name] is None:
                if path.exists():
                    raise ValueError('A hook recreated a retired helper; preserve it and review migration manually')
            else:
                self.files[name] = path.read_bytes()
        self.reviewed_tree = self._build_reviewed_tree()
        git(self.project, 'add', '--', *self.changed)

    def commit(self) -> str:
        self._check_identity()
        if not self.changed:
            return self.head
        if not self.applied:
            raise ValueError('Apply the confirmed candidate before committing.')
        tree = self.reviewed_tree
        if tree is None:
            raise ValueError('Apply the confirmed candidate before committing.')
        if self._review_changed(tree):
            raise ReviewChanged('Reviewed files changed before commit; review them again.')
        hooks = Path(git(self.project, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks'))
        with tempfile.TemporaryDirectory(prefix='devcontainer-hooks-') as temporary:
            root = Path(temporary)
            if hooks.is_dir():
                for path in hooks.iterdir():
                    if path.is_file():
                        (root / path.name).symlink_to(path.resolve())
            for name in ('pre-commit', 'prepare-commit-msg', 'commit-msg'):
                target = root / name
                target.unlink(missing_ok=True)
                target.write_text(_GUARD)
                target.chmod(0o755)
            manifest = root / 'review.json'
            manifest.write_text(json.dumps({name: _digest(self.files[name]) if self.files[name] is not None else None for name in self.changed}))
            env = dict(os.environ, DEVCONTAINER_ORIGINAL_HOOKS=str(hooks), DEVCONTAINER_REVIEWED_TREE=tree,
                       DEVCONTAINER_REVIEW_MANIFEST=str(manifest),
                       DEVCONTAINER_REVIEWED_HEAD=self.head, DEVCONTAINER_REVIEWED_BRANCH=self.branch)
            result = subprocess.run(['git', '-c', f'core.hooksPath={root}', 'commit', '-m',
                                     'Configure project devcontainer'], cwd=self.project,
                                    env=env, capture_output=True, text=True)
        if result.returncode:
            self._check_identity()
            if self._review_changed(tree):
                raise ReviewChanged(result.stderr.strip() or 'Hooks changed the reviewed candidate.')
            raise ValueError(result.stderr.strip() or result.stdout.strip() or 'Commit failed.')
        if self._review_changed(tree) or git(self.project, 'rev-parse', 'HEAD^{tree}') != tree:
            raise ValueError('Repository changed after commit; inspect it before pushing.')
        committed = git(self.project, 'rev-parse', 'HEAD')
        parents = git(self.project, 'show', '-s', '--format=%P', committed)
        if (git(self.project, 'branch', '--show-current') != self.branch
                or parents != self.head):
            raise ValueError('HEAD or branch changed during commit; inspect it before pushing.')
        self.head = committed
        return self.head

    def push(self) -> str:
        self._check_identity()
        head = self.head
        refs = git(self.project, 'ls-remote', '--heads', 'origin', f'refs/heads/{self.branch}')
        if refs:
            remote_sha = refs.split()[0]
            git(self.project, 'fetch', '--quiet', 'origin', f'refs/heads/{self.branch}')
            result = subprocess.run(['git', '-C', str(self.project), 'merge-base', '--is-ancestor',
                                     remote_sha, head], capture_output=True)
            if result.returncode:
                raise ValueError(f'Local and origin/{self.branch} diverge or remote is ahead. '
                                 'No push performed; review a merge plan first.')
        git(self.project, 'push', 'origin', f'{head}:refs/heads/{self.branch}')
        remote = git(self.project, 'ls-remote', '--heads', 'origin', f'refs/heads/{self.branch}')
        if not remote or remote.split()[0] != head:
            raise ValueError('Remote branch does not match the reviewed setup commit.')
        # Like push -u, record the upstream even for a single-branch fetch mapping
        # that intentionally has no origin/<setup-branch> tracking ref.
        git(self.project, 'config', f'branch.{self.branch}.remote', 'origin')
        git(self.project, 'config', f'branch.{self.branch}.merge', f'refs/heads/{self.branch}')
        return head
