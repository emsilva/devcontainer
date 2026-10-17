"""Source-preserving, offline Git tree recovery."""
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    from devcontainer_setup.project_sync.checkpoint_store import Store
    from devcontainer_setup.project_sync import project_files
except ImportError:
    Store = project_files = None
from devcontainer_setup.project_sync.state import discover_project


def git(root, *args, data=None):
    return subprocess.run(['git', '-C', str(root), *args], input=data, capture_output=True, check=True).stdout


class ProjectFileTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(project_files, "Project file recovery has not been implemented")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'source'
        self.root.mkdir()
        git(self.root, 'init', '-b', 'topic')
        git(self.root, 'config', 'user.name', 'Fixture')
        git(self.root, 'config', 'user.email', 'fixture@example.com')
        git(self.root, 'remote', 'add', 'origin', 'git@github.com:example/recovery.git')
        (self.root / '.gitignore').write_text('.scratch/\nignored-cache/\n')
        for name in ('edited', 'deleted', 'staged-delete', 'mode', 'link-target'):
            (self.root / name).write_bytes(b'original\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-qm', 'initial')
        self.project = discover_project(self.root)
        self.store = Store(self.base / 'state')
        self.destination = self.base / 'restored'

    def setup_edits(self):
        (self.root / 'edited').write_bytes(b'\x00staged\xff')
        (self.root / 'new-binary').write_bytes(b'\x00new\xff')
        git(self.root, 'add', 'edited', 'new-binary')
        (self.root / 'edited').write_bytes(b'\x00unstaged\xfe')
        (self.root / 'deleted').unlink()
        git(self.root, 'rm', 'staged-delete')
        (self.root / 'mode').chmod(0o755)
        (self.root / 'untracked').write_bytes(b'unsaved file')
        (self.root / 'safe-link').symlink_to('link-target')
        (self.root / '.scratch/memory').mkdir(parents=True)
        (self.root / '.scratch/memory/handoff.md').write_text('recover notes')
        (self.root / 'ignored-cache').mkdir()
        (self.root / 'ignored-cache/skip').write_text('cache')

    def receipt(self, root):
        return (git(root, 'rev-parse', 'HEAD'), git(root, 'symbolic-ref', 'HEAD'),
                (root / '.git/index').read_bytes(), git(root, 'ls-files', '--stage', '-z'),
                git(root, 'diff', '--binary'), git(root, 'diff', '--cached', '--binary'))

    def test_offline_roundtrip_preserves_staged_unstaged_deleted_modes_and_source(self):
        self.setup_edits()
        before = self.receipt(self.root)
        part = project_files.capture_files(self.project, self.store)
        snapshot = self.store.save({'files': part})
        # Offline reconstruction is enforced at the actual Git process seam.
        real = project_files.subprocess.run
        commands = []
        def offline(args, *a, **kw):
            commands.append(args)
            self.assertFalse(any(x in args for x in ('clone', 'fetch', 'push', 'pull')))
            return real(args, *a, **kw)
        with mock.patch.object(project_files.subprocess, 'run', side_effect=offline):
            restored = project_files.restore_files(self.store, self.store.load(snapshot)['files'], self.destination)
        self.assertEqual(restored.root, self.destination)
        self.assertEqual(restored.identity, self.project.identity)
        after = self.receipt(self.destination)
        self.assertEqual(before[:2], after[:2])
        self.assertEqual(before[3:], after[3:])
        self.assertEqual(self.receipt(self.root), before)
        self.assertEqual((self.destination / 'edited').read_bytes(), b'\x00unstaged\xfe')
        self.assertEqual((self.destination / 'untracked').read_bytes(), b'unsaved file')
        self.assertEqual((self.destination / '.scratch/memory/handoff.md').read_text(), 'recover notes')
        self.assertFalse((self.destination / 'ignored-cache').exists())
        self.assertFalse((self.destination / 'deleted').exists())
        self.assertFalse((self.destination / 'staged-delete').exists())
        self.assertEqual(os.readlink(self.destination / 'safe-link'), 'link-target')
        self.assertEqual((self.destination / 'mode').stat().st_mode & 0o111, 0o111)
        self.assertTrue(commands)

    def test_capture_reuses_bundle_per_head_and_keeps_source_index_bytes(self):
        before = self.receipt(self.root)
        first = project_files.capture_files(self.project, self.store)
        real = project_files.subprocess.run
        def no_bundle(args, *a, **kw):
            self.assertNotIn('bundle', args, 'unchanged HEAD must reuse its retained bundle')
            return real(args, *a, **kw)
        with mock.patch.object(project_files.subprocess, 'run', side_effect=no_bundle):
            second = project_files.capture_files(self.project, self.store)
        self.assertEqual(first, second)
        self.assertEqual(self.receipt(self.root), before)

    def test_existing_destination_is_never_modified(self):
        part = project_files.capture_files(self.project, self.store)
        self.destination.mkdir()
        (self.destination / 'owner').write_bytes(b'preserve')
        with self.assertRaises((ValueError, FileExistsError)):
            project_files.restore_files(self.store, part, self.destination)
        self.assertEqual((self.destination / 'owner').read_bytes(), b'preserve')
        self.assertEqual(list(self.destination.iterdir()), [self.destination / 'owner'])

    def test_capture_refuses_symlink_escape_special_file_and_submodule(self):
        (self.root / 'escape').symlink_to('../private')
        with self.assertRaisesRegex(ValueError, 'symlink|escape'):
            project_files.capture_files(self.project, self.store)
        (self.root / 'escape').unlink()
        os.mkfifo(self.root / 'pipe')
        with self.assertRaisesRegex(ValueError, 'special|regular|unsupported'):
            project_files.capture_files(self.project, self.store)
        (self.root / 'pipe').unlink()
        head = git(self.root, 'rev-parse', 'HEAD').decode().strip()
        git(self.root, 'update-index', '--add', '--cacheinfo', f'160000,{head},module')
        with self.assertRaisesRegex(ValueError, 'submodule'):
            project_files.capture_files(self.project, self.store)

    def test_restore_refuses_manifest_path_escape_before_creating_destination(self):
        part = project_files.capture_files(self.project, self.store)
        for bad in ('../outside', '/absolute', '.git/config', 'nested/../../outside'):
            poisoned = copy.deepcopy(part)
            poisoned['files'][0]['path'] = bad
            with self.subTest(path=bad), self.assertRaises(ValueError):
                project_files.restore_files(self.store, poisoned, self.destination)
            self.assertFalse(self.destination.exists())
        poisoned = copy.deepcopy(part)
        poisoned['files'].append({'path': 'escape', 'kind': 'symlink', 'target': '../outside', 'mode': '120000'})
        with self.assertRaises(ValueError):
            project_files.restore_files(self.store, poisoned, self.destination)
        self.assertFalse(self.destination.exists())

    def test_restore_refuses_symlink_destination_parent(self):
        part = project_files.capture_files(self.project, self.store)
        outside = self.base / 'outside'
        outside.mkdir()
        link = self.base / 'alias'
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            project_files.restore_files(self.store, part, link / 'restore')
        self.assertEqual(list(outside.iterdir()), [])

    def test_corrupt_blob_refuses_restore_before_destination_exists(self):
        part = project_files.capture_files(self.project, self.store)
        digest = part['files'][0]['blob']
        path = next((self.base / 'state/checkpoints/objects').rglob(digest))
        path.chmod(0o600)
        path.write_bytes(b'truncated')
        with self.assertRaises(ValueError):
            project_files.restore_files(self.store, part, self.destination)
        self.assertFalse(self.destination.exists())

    def test_unmerged_index_stages_and_working_conflict_are_recovered(self):
        oid = git(self.root, 'hash-object', '-w', '--stdin', data=b'conflicting version').strip()
        path = b'edited'
        original = git(self.root, 'rev-parse', 'HEAD:edited').strip()
        updates = b'0 ' + (b'0' * 40) + b'\tedited\0' + b''.join(
            b'100644 ' + value + b' ' + str(stage).encode() + b'\t' + path + b'\0'
            for stage, value in ((1, original), (2, oid), (3, original)))
        git(self.root, 'update-index', '-z', '--index-info', data=updates)
        (self.root / 'edited').write_text('<<<< conflict working tree >>>>')
        part = project_files.capture_files(self.project, self.store)
        project_files.restore_files(self.store, part, self.destination)
        self.assertEqual(git(self.destination, 'ls-files', '--stage', '-z'), git(self.root, 'ls-files', '--stage', '-z'))
        self.assertEqual((self.destination / 'edited').read_bytes(), (self.root / 'edited').read_bytes())

    def test_capture_refuses_content_changed_during_snapshot(self):
        real = project_files._bundle
        def concurrent_edit(*args, **kwargs):
            bundle = real(*args, **kwargs)
            (self.root / 'edited').write_bytes(b'concurrent edit')
            return bundle
        with mock.patch.object(project_files, '_bundle', side_effect=concurrent_edit):
            with self.assertRaisesRegex(ValueError, 'Unstable'):
                project_files.capture_files(self.project, self.store)
        self.assertIsNone(self.store.latest('local'))

    def test_index_flags_and_detached_head_roundtrip(self):
        git(self.root, 'checkout', '--detach', '-q')
        git(self.root, 'update-index', '--skip-worktree', 'edited')
        git(self.root, 'update-index', '--assume-unchanged', 'mode')
        before = git(self.root, 'ls-files', '-v', '-z')
        part = project_files.capture_files(self.project, self.store)
        project_files.restore_files(self.store, part, self.destination)
        self.assertEqual(git(self.destination, 'ls-files', '-v', '-z'), before)
        self.assertEqual(git(self.destination, 'rev-parse', '--abbrev-ref', 'HEAD').strip(), b'HEAD')

    def test_partial_restore_failure_does_not_acknowledge(self):
        part = project_files.capture_files(self.project, self.store)
        real = project_files._git
        def fail_import(root, *args, **kwargs):
            if args[:2] == ('bundle', 'unbundle'):
                raise OSError('receiver interrupted')
            return real(root, *args, **kwargs)
        with mock.patch.object(project_files, '_git', side_effect=fail_import):
            with self.assertRaises(OSError):
                project_files.restore_files(self.store, part, self.destination)
        self.assertIsNone(self.store.latest('codespace'))
        self.assertTrue(self.destination.is_dir(), 'Incomplete recovery is preserved for inspection')

    def test_tracked_file_directory_transitions_preserve_deletions_and_new_files(self):
        (self.root / 'edited').unlink()
        (self.root / 'edited').mkdir()
        (self.root / 'edited/new').write_bytes(b'new child')
        (self.root / 'tracked-dir').mkdir()
        (self.root / 'tracked-dir/old').write_bytes(b'old child')
        git(self.root, 'add', 'tracked-dir/old')
        git(self.root, 'commit', '-qm', 'add tracked directory')
        (self.root / 'tracked-dir/old').unlink()
        (self.root / 'tracked-dir').rmdir()
        (self.root / 'tracked-dir').write_bytes(b'new parent file')
        before = (git(self.root, 'diff', '--binary'), git(self.root, 'ls-files', '--stage', '-z'))
        try:
            part = project_files.capture_files(self.project, self.store)
        except (ValueError, OSError) as error:
            self.fail('Ordinary file/directory replacement must be recoverable: ' + str(error))
        project_files.restore_files(self.store, part, self.destination)
        self.assertEqual((self.destination / 'edited/new').read_bytes(), b'new child')
        self.assertEqual((self.destination / 'tracked-dir').read_bytes(), b'new parent file')
        self.assertEqual((git(self.destination, 'diff', '--binary'), git(self.destination, 'ls-files', '--stage', '-z')), before)

    def test_restore_rejects_case_colliding_paths_before_writing(self):
        part = project_files.capture_files(self.project, self.store)
        extra = copy.deepcopy(part['files'][0])
        extra['path'] = part['files'][0]['path'].upper()
        part['files'].append(extra)
        with self.assertRaisesRegex(ValueError, 'case|collision'):
            project_files.restore_files(self.store, part, self.destination)
        self.assertFalse(self.destination.exists())
