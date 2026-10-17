import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid

from devcontainer_setup.project_sync.state import (
    Project, atomic_write, discover_project, host_id, locked, read_json, state_dir,
)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def repo(self, name, remote=None):
        root = self.root / name
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        if remote:
            subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', remote], check=True)
        return root

    def test_github_urls_and_worktrees_share_identity_without_losing_checkout(self):
        first = self.repo('a', 'git@GitHub.com:Owner/Repo.git')
        second = self.repo('b', 'https://github.com/owner/repo')
        third = self.repo('c', 'ssh://git@github.com/Owner/Repo.git')
        found = discover_project(first)
        self.assertEqual(found.identity, 'github.com/owner/repo')
        self.assertEqual(found.project_id, hashlib.sha256(found.identity.encode()).hexdigest()[:24])
        self.assertEqual(found.project_id, discover_project(second).project_id)
        self.assertEqual(found.project_id, discover_project(third).project_id)
        subprocess.run(['git', '-C', str(first), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-qm', 'Initial'], check=True)
        worktree = self.root / 'linked'
        subprocess.run(['git', '-C', str(first), 'worktree', 'add', '-qb', 'linked', str(worktree)], check=True)
        nested = worktree / 'nested'
        nested.mkdir()
        linked = discover_project(nested)
        self.assertEqual(linked.root, worktree)
        self.assertEqual(linked.project_id, found.project_id)

    def test_same_basename_different_origins_are_isolated(self):
        a = self.repo('one/repo', 'https://github.com/one/repo.git')
        b = self.repo('two/repo', 'https://github.com/two/repo.git')
        self.assertNotEqual(discover_project(a).project_id, discover_project(b).project_id)

    def test_missing_or_unsafe_origins_are_refused(self):
        for index, remote in enumerate((None, 'https://user:secret@github.com/a/b', 'https://token@github.com/a/b', 'ssh://user:secret@github.com/a/b', 'git@github.com:a/../b', 'https://github.com/a/b\nother', 'https://github.com/a/b\n', '/local/repository', 'https://github.com/a/b?token=secret')):
            with self.subTest(remote=remote):
                with self.assertRaises(ValueError):
                    discover_project(self.repo(str(index), remote))

    def test_state_directory_is_absolute_private_namespace_and_does_not_create(self):
        project = discover_project(self.repo('repo', 'git@github.com:a/b'))
        home = self.root / 'home'
        for override in ('', 'relative', '../other', str(self.root / 'state')):
            env = {'HOME': str(home), 'XDG_STATE_HOME': override}
            expected = Path(override) if override.startswith('/') else home / '.local/state'
            found = state_dir(project, env)
            self.assertEqual(found, expected / 'devcontainer-setup/projects' / project.project_id)
            self.assertFalse(found.exists())
        with self.assertRaises(ValueError):
            state_dir(Project(project.root, project.remote, project.identity, '../escape'), {'HOME': str(home)})

    def test_atomic_write_is_private_replaces_and_read_requires_object(self):
        path = self.root / 'state' / 'record.json'
        atomic_write(path, b'{"v":1}')
        self.assertEqual(read_json(path), {'v': 1})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        atomic_write(path, b'{"v":2}')
        self.assertEqual(read_json(path), {'v': 2})
        self.assertEqual(list(path.parent.iterdir()), [path])
        atomic_write(path, b'[]')
        with self.assertRaises(ValueError):
            read_json(path)

    def test_state_mutations_and_reads_refuse_symlinks_and_traversal(self):
        outside = self.root / 'outside'
        outside.mkdir()
        target = outside / 'target'
        target.write_text('{}')
        (self.root / 'link').symlink_to(outside, target_is_directory=True)
        (self.root / 'file-link').symlink_to(target)
        for path in (self.root / 'link/target', self.root / 'file-link', self.root / 'absent/../target'):
            for operation in (lambda: atomic_write(path, b'new'), lambda: read_json(path), lambda: locked(path).__enter__()):
                with self.subTest(path=path, operation=operation):
                    with self.assertRaises((ValueError, OSError)):
                        operation()
        self.assertEqual(target.read_bytes(), b'{}')
        self.assertFalse((self.root / 'absent').exists())

    def test_lock_excludes_other_process_and_releases_after_exception(self):
        path = self.root / 'private' / 'lease.lock'
        code = ('from pathlib import Path; from devcontainer_setup.project_sync.state import locked; '
                'import sys\ntry:\n with locked(Path(sys.argv[1])): pass\nexcept BlockingIOError: sys.exit(23)')
        with self.assertRaisesRegex(RuntimeError, 'release'):
            with locked(path):
                child = subprocess.run([sys.executable, '-c', code, str(path)])
                self.assertEqual(child.returncode, 23)
                raise RuntimeError('release')
        with locked(path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(path.exists())  # Do not unlink lock inodes and split ownership.

    def test_host_id_is_stable_private_and_environment_specific(self):
        a = {'HOME': str(self.root), 'XDG_STATE_HOME': str(self.root / 'a')}
        b = {'HOME': str(self.root), 'XDG_STATE_HOME': str(self.root / 'b')}
        first = host_id(a)
        self.assertEqual(str(uuid.UUID(first)), first)
        self.assertEqual(host_id(a), first)
        self.assertNotEqual(host_id(b), first)
        files = [p for p in (self.root / 'a').rglob('*') if p.is_file()]
        self.assertTrue(files)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files))
