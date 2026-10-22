import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from devcontainer_setup.project_sync.state import (
    Project, atomic_write, discover_project, locked, read_json, state_dir,
)


def host_id(env):
    """Build an old host registry fixture to verify preserved namespace selection."""
    from devcontainer_setup.project_sync.state import _state_home
    directory = _state_home(env) / 'devcontainer-setup'
    path = directory / 'host.json'
    with locked(directory / 'host.lock', blocking=True):
        try:
            return read_json(path)['host_id']
        except FileNotFoundError:
            value = str(uuid.uuid4())
            atomic_write(path, json.dumps({'host_id': value}).encode() + b'\n')
            return value


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

    def test_a_detached_child_that_ignores_sigterm_at_startup_timeout_is_killed_and_reaped(self):
        from devcontainer_setup.project_sync import state as project_state
        ready = self.root / 'ignoring-sigterm'
        code = ('import pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                'pathlib.Path(' + repr(str(ready)) + ').touch(); time.sleep(60)')
        real, children = subprocess.Popen, []
        def popen(args, **kwargs):
            # The real launch options, with a child that ignores SIGTERM and never becomes owned.
            children.append(real([sys.executable, '-c', code], **kwargs))
            return children[-1]
        def status(_):
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            return {'running': False}
        with mock.patch.object(project_state.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(project_state, 'STARTUP_WAIT', .2), \
                mock.patch.object(project_state, 'TERMINATE_WAIT', .3):
            with self.assertRaisesRegex(ValueError, '^Fixture startup timed out.$'):
                project_state.launch_detached('scratch', '--supervise', self.root, self.root / 'state',
                                              env=dict(os.environ), status=status, exited='exited',
                                              timed_out='Fixture startup timed out.')
        self.assertTrue(ready.exists())
        self.assertEqual(children[0].returncode, -9)  # killed after ignoring SIGTERM, and reaped

    def test_simultaneous_first_locks_both_wait_instead_of_failing(self):
        # macOS can fail one of two simultaneous openat(O_CREAT) calls for a new
        # name with ENOENT. Two arms at once must queue on the lock, not error.
        import threading
        for attempt in range(20):
            path = self.root / ('race-' + str(attempt)) / 'control.lock'
            path.parent.mkdir()
            barrier, errors, held = threading.Barrier(2), [], []
            def take():
                barrier.wait()
                try:
                    with locked(path, blocking=True):
                        held.append(True)
                except OSError as error:
                    errors.append(error)
            threads = [threading.Thread(target=take) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(len(held), 2)


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
        (self.fallback / 'devcontainer-setup/projects').mkdir(parents=True, mode=0o700)
        for operation in (lambda: state_dir(self.project, self.env), lambda: host_id(self.env)):
            with self.assertRaisesRegex((PermissionError, ValueError), 'ambig|both|Both'):
                operation()
        self.assertFalse((self.fallback / 'devcontainer-setup/host.json').exists())
        old = json.loads((self.home / '.local/state/devcontainer-setup/host.json').read_bytes())
        self.assertEqual(old['host_id'], first)


class AtomicReadTests(unittest.TestCase):
    def test_reader_keeps_complete_opened_snapshot_after_atomic_replacement(self):
        import threading
        from devcontainer_setup.project_sync import state as module
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'runtime.json'
            old = {'generation': 1, 'payload': ['old snapshot'] * 100}
            new = {'generation': 2, 'payload': ['new snapshot'] * 100}
            atomic_write(path, json.dumps(old).encode())
            opened, replaced = threading.Event(), threading.Event()
            actual_regular = module._regular
            outcome, links = [], []
            def validate_after_replace(fd, *args, **kwargs):
                opened.set()
                if not replaced.wait(timeout=5):
                    raise AssertionError('Atomic replacement barrier did not complete')
                links.append(os.fstat(fd).st_nlink)
                return actual_regular(fd, *args, **kwargs)
            def read():
                try:
                    outcome.append(read_json(path))
                except BaseException as error:
                    outcome.append(error)
            with mock.patch.object(module, '_regular', side_effect=validate_after_replace):
                thread = threading.Thread(target=read)
                thread.start()
                try:
                    self.assertTrue(opened.wait(timeout=5), 'Reader did not open its snapshot')
                    atomic_write(path, json.dumps(new).encode())
                finally:
                    replaced.set()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), 'Reader did not finish after replacement')
            self.assertEqual(links, [0], 'Proof must exercise the unlinked original inode')
            self.assertEqual(outcome, [old])
            self.assertEqual(read_json(path), new)

    def test_unlinked_open_lock_inode_remains_refused(self):
        from devcontainer_setup.project_sync import state as module
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'lease.lock'
            actual_regular = module._regular
            links = []
            def unlink_before_validation(fd, *args, **kwargs):
                path.unlink()
                links.append(os.fstat(fd).st_nlink)
                return actual_regular(fd, *args, **kwargs)
            with mock.patch.object(module, '_regular', side_effect=unlink_before_validation):
                with self.assertRaises(ValueError):
                    with locked(path):
                        self.fail('An unlinked lock inode cannot grant ownership')
            self.assertEqual(links, [0])

    def test_json_reads_and_locks_still_refuse_multiple_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'record.json'
            path.write_bytes(b'{}')
            os.link(path, path.with_name('other-link.json'))
            with self.assertRaises(ValueError):
                read_json(path)
            with self.assertRaises(ValueError):
                with locked(path):
                    self.fail('A hardlinked lock inode cannot grant ownership')


class SharedStateProducerTests(unittest.TestCase):
    setUp = DefaultStateHomeTests.setUp

    def marker(self):
        namespace = self.home / '.local/state/devcontainer-setup'
        namespace.mkdir(mode=0o755)
        namespace.chmod(0o755)
        marker = namespace / 'dotfiles-4326-fixture'
        marker.write_bytes(b'Existing apply-dotfiles producer marker\n')
        marker.chmod(0o644)
        return marker

    def assert_marker(self, marker):
        self.assertEqual(marker.read_bytes(), b'Existing apply-dotfiles producer marker\n')
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o644)

    def test_standard_codespace_dotfiles_marker_keeps_known_fallback_identity(self):
        first = host_id(self.env)
        marker = self.marker()
        before = stat.S_IMODE(marker.parent.stat().st_mode)
        self.assertEqual(state_dir(self.project, self.env), self.fallback / 'devcontainer-setup/projects' / self.project.project_id)
        self.assertEqual(host_id(self.env), first)
        self.assert_marker(marker)
        self.assertEqual(stat.S_IMODE(marker.parent.stat().st_mode), before)
        for path in (self.home / '.local', self.home / '.local/state'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o775)

    def test_fresh_standard_codespace_with_only_dotfiles_marker_initializes_fallback(self):
        marker = self.marker()
        self.assertFalse(self.fallback.exists())
        identity = host_id(self.env)
        self.assertEqual(str(uuid.UUID(identity)), identity)
        self.assertTrue((self.fallback / 'devcontainer-setup/host.json').is_file())
        self.assertFalse((marker.parent / 'host.json').exists())
        self.assert_marker(marker)
        self.assertEqual(stat.S_IMODE(marker.parent.stat().st_mode), 0o755)

    def test_safe_default_with_only_dotfiles_marker_initializes_default(self):
        for path in (self.home / '.local', self.home / '.local/state'):
            path.chmod(0o700)
        marker = self.marker()
        identity = host_id(self.env)
        self.assertEqual(host_id(self.env), identity)
        self.assertTrue((marker.parent / 'host.json').is_file())
        self.assertFalse(self.fallback.exists())
        self.assert_marker(marker)

    def test_empty_fallback_does_not_hide_initialized_default_continuity(self):
        for path in (self.home / '.local', self.home / '.local/state'):
            path.chmod(0o700)
        identity = host_id(self.env)
        self.fallback.mkdir(mode=0o700)
        self.assertEqual(host_id(self.env), identity)
        self.assertEqual(state_dir(self.project, self.env), self.home / '.local/state/devcontainer-setup/projects' / self.project.project_id)
        self.assertFalse((self.fallback / 'devcontainer-setup').exists())

    def test_real_continuity_entries_still_make_default_and_fallback_ambiguous(self):
        identity = host_id(self.env)
        namespace = self.home / '.local/state/devcontainer-setup'
        namespace.mkdir(mode=0o700)
        for name in ('host.json', 'host.lock', 'projects'):
            path = namespace / name
            if name == 'projects':
                path.mkdir()
            else:
                path.write_bytes(b'{}')
            with self.subTest(entry=name), self.assertRaisesRegex(ValueError, 'ambig'):
                state_dir(self.project, self.env)
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        self.assertEqual(host_id(self.env), identity)

    def test_projects_only_unsafe_default_is_not_silently_bypassed(self):
        (self.home / '.local/state/devcontainer-setup/projects').mkdir(parents=True)
        with self.assertRaisesRegex(PermissionError, 'Existing|migration'):
            state_dir(self.project, self.env)
        self.assertFalse(self.fallback.exists())

    def test_shared_namespace_symlink_never_hides_an_unknown_registry(self):
        for path in (self.home / '.local', self.home / '.local/state'):
            path.chmod(0o700)
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'host.json').write_bytes(b'{}')
        (self.home / '.local/state/devcontainer-setup').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((PermissionError, OSError, ValueError)):
            state_dir(self.project, self.env)
        self.assertFalse(self.fallback.exists())
        self.assertEqual((outside / 'host.json').read_bytes(), b'{}')
