import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

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


class NamespacePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.namespace = self.root / 'state/devcontainer-setup'
        self.directory = self.namespace / 'projects' / ('a' * 24)
        self.directory.mkdir(parents=True)
        for path in (self.root, self.root / 'state'):
            path.chmod(0o755)
        for path in (self.namespace, self.namespace / 'projects', self.directory):
            path.chmod(0o777)

    def test_existing_public_namespace_cannot_split_a_held_lock(self):
        lock = self.directory / 'lease.lock'
        # The child models a different principal: it exploits unlink/recreate
        # only when directory permissions grant that principal write access.
        # This actually acquires a second lock inode on the vulnerable version.
        code = """from pathlib import Path
import stat, sys
from devcontainer_setup.project_sync.state import locked
path = Path(sys.argv[1])
if stat.S_IMODE(path.parent.stat().st_mode) & 0o022:
    path.unlink()
    with locked(path):
        sys.exit(42)
try:
    with locked(path):
        sys.exit(43)
except BlockingIOError:
    sys.exit(23)
"""
        with locked(lock):
            inode = lock.stat().st_ino
            child = subprocess.run([sys.executable, '-c', code, str(lock)])
            self.assertEqual(child.returncode, 23, 'Public namespace allowed a second independently held lock inode')
            self.assertEqual(lock.stat().st_ino, inode)
        for path in (self.namespace, self.namespace / 'projects', self.directory):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.root / 'state').stat().st_mode), 0o755)

    def test_host_identity_secures_namespace_without_changing_home_or_xdg_parent(self):
        env = {'HOME': str(self.root), 'XDG_STATE_HOME': str(self.root / 'state')}
        original = host_id(env)
        code = """from pathlib import Path
import stat, sys
path = Path(sys.argv[1])
if stat.S_IMODE(path.parent.stat().st_mode) & 0o022:
    path.unlink()
    path.write_text('{"host_id":"00000000-0000-0000-0000-000000000000"}')
    sys.exit(42)
sys.exit(0)
"""
        child = subprocess.run([sys.executable, '-c', code, str(self.namespace / 'host.json')])
        self.assertEqual(child.returncode, 0, 'Public namespace permitted host identity replacement')
        self.assertEqual(host_id(env), original)
        self.assertEqual(stat.S_IMODE(self.namespace.stat().st_mode), 0o700)
        for parent in (self.root, self.root / 'state'):
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)

    def test_atomic_write_secures_all_namespace_directories(self):
        atomic_write(self.directory / 'record.json', b'{}')
        for path in (self.namespace, self.namespace / 'projects', self.directory):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)

    def test_pure_notes_reader_leaves_user_project_permissions_unchanged(self):
        from devcontainer_setup.project_sync.notes import integrate_notes
        project = self.directory / 'project'
        project.mkdir()
        project.chmod(0o755)
        integrate_notes(project, {}, True)
        for path in (self.namespace, self.namespace / 'projects', self.directory):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o777)
        self.assertEqual(stat.S_IMODE(project.stat().st_mode), 0o755)

    def test_foreign_owned_namespace_refuses_without_chmod(self):
        # Read the real inode metadata, changing only effective ownership for
        # the policy check; an ordinary user cannot chown a fixture elsewhere.
        with mock.patch('devcontainer_setup.project_sync.state.os.geteuid', return_value=os.geteuid() + 1):
            with self.assertRaises(PermissionError):
                atomic_write(self.directory / 'record.json', b'{}')
        self.assertEqual(stat.S_IMODE(self.namespace.stat().st_mode), 0o777)
        self.assertFalse((self.directory / 'record.json').exists())

    def test_public_unmanaged_parent_refuses_without_chmod(self):
        (self.root / 'state').chmod(0o777)
        with self.assertRaises(PermissionError):
            with locked(self.directory / 'lease.lock'):
                pass
        self.assertEqual(stat.S_IMODE((self.root / 'state').stat().st_mode), 0o777)
        self.assertFalse((self.directory / 'lease.lock').exists())

    def test_read_json_refuses_public_namespace_without_permission_mutation(self):
        (self.directory / 'record.json').write_bytes(b'{}')
        with self.assertRaises(PermissionError):
            read_json(self.directory / 'record.json')
        self.assertEqual(stat.S_IMODE(self.namespace.stat().st_mode), 0o777)


class LockDescriptorTests(unittest.TestCase):
    def test_lock_exposes_the_live_descriptor_until_context_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            with locked(Path(directory).resolve() / 'guardian.lock') as descriptor:
                self.assertIsInstance(descriptor, int)
                self.assertTrue(stat.S_ISREG(os.fstat(descriptor).st_mode))
            with self.assertRaises(OSError):
                os.fstat(descriptor)


class DefaultStateHomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.home.chmod(0o755)
        (self.home / '.local/state').mkdir(parents=True)
        (self.home / '.local').chmod(0o775)
        (self.home / '.local/state').chmod(0o775)
        self.project = Project(self.home / 'project', 'git@github.com:a/b', 'github.com/a/b', 'a' * 24)
        self.env = {'HOME': str(self.home)}
        self.fallback = self.home / '.devcontainer-state'

    def test_standard_codespace_default_uses_private_fallback_without_chmod(self):
        expected = self.fallback / 'devcontainer-setup/projects' / self.project.project_id
        before = list(self.home.rglob('*'))
        self.assertEqual(state_dir(self.project, self.env), expected)
        self.assertEqual(list(self.home.rglob('*')), before)
        identity = host_id(self.env)
        self.assertEqual(str(uuid.UUID(identity)), identity)
        self.assertEqual(host_id(self.env), identity)
        self.assertTrue((self.fallback / 'devcontainer-setup/host.json').is_file())
        for path in (self.home / '.local', self.home / '.local/state'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.fallback.stat().st_mode), 0o700)

    def test_existing_fallback_preserves_identity_after_default_permissions_repaired(self):
        first = host_id(self.env)
        (self.home / '.local').chmod(0o700)
        (self.home / '.local/state').chmod(0o700)
        self.assertEqual(state_dir(self.project, self.env), self.fallback / 'devcontainer-setup/projects' / self.project.project_id)
        self.assertEqual(host_id(self.env), first)
        self.assertFalse((self.home / '.local/state/devcontainer-setup').exists())

    def test_explicit_unsafe_xdg_remains_authoritative_and_refuses(self):
        env = dict(self.env, XDG_STATE_HOME=str(self.home / '.local/state'))
        expected = self.home / '.local/state/devcontainer-setup/projects' / self.project.project_id
        self.assertEqual(state_dir(self.project, env), expected)
        with self.assertRaises(PermissionError):
            host_id(env)
        self.assertFalse(self.fallback.exists())
        self.assertEqual(stat.S_IMODE((self.home / '.local').stat().st_mode), 0o775)

    def test_default_symlink_ancestor_refuses_unknown_state_without_following_it(self):
        (self.home / '.local/state').rmdir()
        (self.home / '.local').rmdir()
        outside = self.home / 'outside'
        outside.mkdir()
        (self.home / '.local').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex((PermissionError, ValueError), 'symlink|selection|migration'):
            state_dir(self.project, self.env)
        self.assertFalse(self.fallback.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_fallback_refuses_without_writing_outside(self):
        outside = self.home / 'outside'
        outside.mkdir()
        self.fallback.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            host_id(self.env)
        self.assertEqual(list(outside.iterdir()), [])

    def test_safe_default_keeps_existing_xdg_fallback_contract(self):
        (self.home / '.local').chmod(0o700)
        (self.home / '.local/state').chmod(0o700)
        self.assertEqual(state_dir(self.project, self.env), self.home / '.local/state/devcontainer-setup/projects' / self.project.project_id)
        self.assertFalse(self.fallback.exists())

    def test_initialized_default_refuses_fallback_after_permissions_regress(self):
        (self.home / '.local').chmod(0o700)
        (self.home / '.local/state').chmod(0o700)
        first = host_id(self.env)
        project_state = state_dir(self.project, self.env)
        pairing = project_state / 'scratch.json'
        atomic_write(pairing, b'{"codespace":"existing-pair","phase":"running"}')
        original_host = (self.home / '.local/state/devcontainer-setup/host.json').read_bytes()
        for path in (self.home / '.local', self.home / '.local/state'):
            path.chmod(0o775)
        for operation in (lambda: state_dir(self.project, self.env), lambda: host_id(self.env)):
            with self.assertRaisesRegex((PermissionError, ValueError), 'existing|Existing|migration'):
                operation()
        self.assertFalse(self.fallback.exists(), 'A permission regression must not create a new identity namespace')
        self.assertEqual(pairing.read_bytes(), b'{"codespace":"existing-pair","phase":"running"}')
        self.assertEqual((self.home / '.local/state/devcontainer-setup/host.json').read_bytes(), original_host)
        for path in (self.home / '.local', self.home / '.local/state'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o775)
            path.chmod(0o700)
        self.assertEqual(host_id(self.env), first)
        self.assertEqual(state_dir(self.project, self.env), project_state)
        self.assertEqual(read_json(pairing)['codespace'], 'existing-pair')

    def test_both_default_and_fallback_namespaces_refuse_ambiguous_identity(self):
        (self.home / '.local').chmod(0o700)
        (self.home / '.local/state').chmod(0o700)
        first = host_id(self.env)
        self.fallback.mkdir(mode=0o700)
        for operation in (lambda: state_dir(self.project, self.env), lambda: host_id(self.env)):
            with self.assertRaisesRegex((PermissionError, ValueError), 'ambig|both|Both'):
                operation()
        self.assertFalse((self.fallback / 'devcontainer-setup').exists())
        old = json.loads((self.home / '.local/state/devcontainer-setup/host.json').read_bytes())
        self.assertEqual(old['host_id'], first)
