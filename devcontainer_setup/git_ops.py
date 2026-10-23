"""Review and commit a bounded set of generated files without losing user work."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tempfile


class ReviewChanged(ValueError):
    """A normal Git hook changed the reviewed candidate before committing."""


def git(project: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(['git', '-C', str(project), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or 'Git command failed')
    return result.stdout.strip()


def _read(project: Path, *args: str) -> str | None:
    """Git's stripped output, or None when the command fails."""
    result = subprocess.run(['git', '-C', str(project), *args], capture_output=True, text=True)
    return None if result.returncode else result.stdout.strip()


def _succeeds(project: Path, *args: str) -> bool:
    return _read(project, *args) is not None


SETUP_BRANCH = re.compile(r'setup/devcontainer(?:-[1-9][0-9]*)?')


def default_branch(project: Path) -> str:
    # Local origin/HEAD only: never `remote set-head` against the network.
    head = git(project, 'symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD', check=False)
    return head.removeprefix('refs/remotes/origin/') if head.startswith('refs/remotes/origin/') else 'main'


def _first_free(taken) -> str:
    """The first of setup/devcontainer, setup/devcontainer-2, ... that `taken` does not claim."""
    candidate, number = 'setup/devcontainer', 2
    while taken(candidate):
        candidate, number = f'setup/devcontainer-{number}', number + 1
    return candidate


def proposed_branch(project: Path) -> str:
    """The commit branch without a GitHub origin: the current one, or a new one off a default."""
    current = git(project, 'branch', '--show-current')
    if not current:
        raise ValueError('Detached HEAD: choose a working branch before setup.')
    if current not in {default_branch(project), 'main', 'master'}:
        return current
    return _first_free(lambda name: git(project, 'rev-parse', '--verify', f'refs/heads/{name}', check=False))


def next_setup_branch(project: Path) -> str:
    listed = _read(project, 'ls-remote', 'origin', 'refs/heads/setup/devcontainer', 'refs/heads/setup/devcontainer-*')
    if listed is None:
        raise ValueError('Cannot list setup branches on origin; check your access to origin, then run setup again.')
    remote = {line.split('\t', 1)[1].removeprefix('refs/heads/') for line in listed.splitlines() if '\t' in line}
    return _first_free(lambda name: name in remote
                       or _succeeds(project, 'rev-parse', '--verify', '--quiet', f'refs/heads/{name}'))


def _fetch_default(project: Path, default: str) -> bool:
    # Only origin <default>, into its tracking ref even under a narrow fetch refspec,
    # leaving the owner's FETCH_HEAD alone.
    return _succeeds(project, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head',
                     'origin', f'+refs/heads/{default}:refs/remotes/origin/{default}')


def _worktrees(project: Path) -> dict[Path, str]:
    """Registered worktrees and the branch each holds ('' when detached)."""
    found, path = {}, None
    for line in git(project, 'worktree', 'list', '--porcelain').splitlines():
        if line.startswith('worktree '):
            path = Path(line.removeprefix('worktree ')).resolve()
            found[path] = ''
        elif line.startswith('branch ') and path:
            found[path] = line.removeprefix('branch refs/heads/')
    return found


def _tip(project: Path, branch: str) -> str:
    return git(project, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False)


def setup_worktree(project: Path, root: Path) -> tuple[Path, str]:
    """Cut a new setup branch from a freshly fetched origin/<default> into a worktree under root."""
    default = default_branch(project)
    if not _fetch_default(project, default):
        unset = '' if git(project, 'symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD', check=False) else (
            f', or, if origin\'s default branch is not {default}, run git remote set-head origin --auto')
        raise ValueError(f'Cannot fetch {default} from origin; check that origin has a {default} branch '
                         f'you can read{unset}, then run setup again.')
    start = git(project, 'rev-parse', '--verify', f'refs/remotes/origin/{default}^{{commit}}')
    branch = next_setup_branch(project)
    root = Path(root)
    for path, held in _worktrees(project).items():
        if path.is_relative_to(root.resolve()):
            # A crashed run's worktree may hold work; it is the owner's to inspect.
            print(f'Note: an earlier setup left the worktree {path} on {held or "a detached HEAD"}. Inspect it, '
                  f'then remove it with git -C {shlex.quote(str(project))} worktree remove {shlex.quote(str(path))}',
                  file=sys.stderr)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / branch.replace('/', '-')
    if path.exists() or path.is_symlink():
        path = Path(tempfile.mkdtemp(prefix=path.name + '-', dir=root))
    added = subprocess.run(['git', '-C', str(project), 'worktree', 'add', '--quiet', '--no-track', '-b', branch,
                            str(path), start], capture_output=True, text=True)
    if added.returncode:
        # `worktree add` can create the branch before it fails; drop only that empty branch.
        if _tip(project, branch) == start and branch not in _worktrees(project).values():
            _succeeds(project, 'branch', '-D', branch)
        reason = added.stderr.strip()
        raise ValueError(f'Cannot create the setup worktree {path}' + (f': {reason}' if reason else '')
                         + f'; check that {root} is writable, then run setup again.')
    return path, branch


def _manual(project: Path, steps: list[str]) -> str:
    return 'run by hand: ' + ' && '.join(f'git -C {shlex.quote(str(project))} {step}' for step in steps)


def _setup_path(path: Path, branch: str, root: Path) -> Path:
    """Refuse, before any Git call, a branch or path that setup never creates."""
    if not SETUP_BRANCH.fullmatch(branch):
        raise ValueError(f'Refusing to remove {branch}: setup removes only setup/devcontainer[-N] branches.')
    path, root = Path(path).resolve(), Path(root).resolve()
    if path.parent != root:
        raise ValueError(f'Refusing to remove {path}: setup removes only its own setup worktrees in {root}.')
    return path


def _registered(project: Path, path: Path, branch: str) -> bool:
    """Whether path is still a worktree; one holding anything but branch is refused."""
    held = _worktrees(project).get(path)
    if held is not None and held != branch:
        raise ValueError(f'{path} holds {held or "a detached HEAD"}, not {branch}; refusing to remove it '
                         'as the setup/devcontainer worktree.')
    return held is not None


def remove_worktree(project: Path, path: Path, branch: str, *, root: Path) -> None:
    """Remove a setup worktree and keep its branch, which holds the setup commit."""
    path = _setup_path(path, branch, root)
    if _registered(project, path, branch) and not _succeeds(project, 'worktree', 'remove', str(path)):
        # A plain retry would refuse again: the hand-run step forces it once inspected.
        raise ValueError(f'Could not remove the setup worktree {path}; branch {branch} keeps the setup commit. '
                         'Inspect the worktree, then '
                         + _manual(project, [f'worktree remove --force {shlex.quote(str(path))}']))


def remove_setup_worktree(project: Path, path: Path, branch: str, *, remote: bool, root: Path,
                          cut: str | None = None) -> None:
    """Remove the worktree, then the branch locally and (if remote) on origin, then verify origin.

    Only a branch whose tip is on a freshly fetched origin/<default>, or still at `cut` (the
    commit it was cut from), is deleted; anything else holds work and is refused unchanged."""
    path = _setup_path(path, branch, root)
    registered = _registered(project, path, branch)
    elsewhere = [other for other, held in _worktrees(project).items() if held == branch and other != path]
    if elsewhere:
        raise ValueError(f'Refusing to delete {branch}: it is checked out at {elsewhere[0]}; nothing was changed.')
    local, remote_tip = _tip(project, branch), ''
    if remote:
        listed = _read(project, 'ls-remote', '--heads', 'origin', f'refs/heads/{branch}')
        if listed is None:
            raise ValueError(f'Cannot read origin to delete {branch} there; nothing was changed.')
        remote_tip = listed.split('\t', 1)[0]
        if local and remote_tip and remote_tip != local:
            raise ValueError(f'Refusing to delete {branch}: origin has a different {branch} than this '
                             'repository; nothing was changed.')
    tip = local or remote_tip
    if tip and tip != cut:
        default = default_branch(project)
        if not _fetch_default(project, default):
            raise ValueError(f'Cannot fetch {default} from origin to check {branch}; nothing was changed.')
        if not _succeeds(project, 'merge-base', '--is-ancestor', tip, f'refs/remotes/origin/{default}'):
            raise ValueError(f'Refusing to delete {branch}: it holds commits not on {default}; nothing was '
                             f'changed. Merge it into {default} first.')
    steps = ([f'worktree remove --force {shlex.quote(str(path))}'] if registered else []) + (
        [f'branch -D {branch}'] if local else []) + ([f'push origin --delete {branch}'] if remote_tip else [])
    if registered and not _succeeds(project, 'worktree', 'remove', str(path)):
        raise ValueError(f'Could not remove the setup worktree {path}; nothing else was changed. Inspect it, then '
                         + _manual(project, steps))
    done = int(registered)
    if local:
        # -D, not -d (which judges the branch against whatever the owner has checked out):
        # the merge check above is what makes the deletion safe.
        if _tip(project, branch) != local or not _succeeds(project, 'branch', '-D', branch):
            raise ValueError(f'Could not delete branch {branch}; ' + _manual(project, steps[done:]))
        done += 1
    if not remote:
        return
    # The lease refuses to delete a tip on origin newer than the one checked above.
    if remote_tip and not _succeeds(project, 'push', '--quiet', f'--force-with-lease=refs/heads/{branch}:{remote_tip}',
                                    'origin', '--delete', f'refs/heads/{branch}'):
        raise ValueError(f'Could not delete {branch} on origin: its tip there is no longer {remote_tip[:12]}, '
                         'or origin refused. Check it, then ' + _manual(project, steps[done:]))
    if _read(project, 'ls-remote', '--heads', 'origin', f'refs/heads/{branch}') != '':
        raise ValueError(f'Could not confirm origin no longer has {branch}; check with '
                         + _manual(project, [f'ls-remote --heads origin {branch}']).removeprefix('run by hand: '))


def is_ancestor(project: Path, ancestor: str, descendant: str) -> bool:
    return _succeeds(project, 'merge-base', '--is-ancestor', ancestor, descendant)


def default_contains(project: Path, default: str, sha: str) -> bool:
    """Whether a freshly fetched origin/<default> contains sha."""
    return _fetch_default(project, default) and is_ancestor(project, sha, f'refs/remotes/origin/{default}')


def merged_setup_branches(project: Path, default: str) -> list[str]:
    """Local setup branches whose tips are on refs/remotes/origin/<default> (fetch it first).
    A branch some worktree holds is left out: a leftover worktree is the owner's to inspect."""
    held = set(_worktrees(project).values())
    names = git(project, 'for-each-ref', '--format=%(refname)', 'refs/heads/setup/').splitlines()
    return [name.removeprefix('refs/heads/') for name in names
            if SETUP_BRANCH.fullmatch(name.removeprefix('refs/heads/')) and name.removeprefix('refs/heads/') not in held
            and is_ancestor(project, name, f'refs/remotes/origin/{default}')]


def fast_forward_checkout(project: Path, default: str) -> bool:
    """Fast-forward the owner's checkout only when it is on <default> and clean; otherwise change
    nothing in it. Either way it may update refs/remotes/origin/<default>."""
    status = _read(project, 'status', '--porcelain', '--untracked-files=all')
    if (status is None or status or not _succeeds(project, 'check-ref-format', '--branch', default)
            or git(project, 'branch', '--show-current', check=False) != default):
        return False
    if not _fetch_default(project, default):
        return False
    # --no-overwrite-ignore: an ignored local file (devcontainer.env, say) is never replaced.
    return _succeeds(project, 'merge', '--ff-only', '--quiet', '--no-autostash', '--no-overwrite-ignore',
                     f'refs/remotes/origin/{default}')


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
