"""Real checkpoint recovery while every remote control surface is unavailable."""
import copy
import io
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from devcontainer_setup.project_sync import workspace
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.project_files import capture_files
from devcontainer_setup.project_sync.state import discover_project, read_json


def git(root, *args, data=None):
    return subprocess.run(['git', '--no-optional-locks', '-c', 'diff.autoRefreshIndex=false', '-C', str(root), *args], input=data,
                          capture_output=True, check=True).stdout


class WorkspaceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'desktop'
        self.root.mkdir()
        git(self.root, 'init', '-b', 'work')
        git(self.root, 'config', 'user.name', 'Recovery fixture')
        git(self.root, 'config', 'user.email', 'fixture@example.com')
        git(self.root, 'remote', 'add', 'origin', 'https://github.com/example/workspace-recovery.git')
        (self.root / 'app').write_bytes(b'initial\n')
        (self.root / '.gitignore').write_text('.scratch/\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-qm', 'Initial')
        self.remote = self.base / 'codespace'
        shutil.copytree(self.root, self.remote)
        (self.root / 'app').write_bytes(b'desktop staged owner edit')
        git(self.root, 'add', 'app')
        (self.root / 'app').write_bytes(b'desktop unstaged owner edit')
        (self.remote / 'app').write_bytes(b'\x00remote staged\xff')
        git(self.remote, 'add', 'app')
        (self.remote / 'app').write_bytes(b'\x00remote current\xfe')
        (self.remote / 'untracked').write_text('remote untracked')
        (self.remote / '.scratch/memory').mkdir(parents=True)
        (self.remote / '.scratch/memory/context.md').write_text('offline handoff context')
        self.project = discover_project(self.root)
        self.state = self.base / 'state'
        self.store = Store(self.state)
        self.snapshot = self.checkpoint()
        self.store.acknowledge(self.snapshot, source='codespace')
        self.seed_registry()

    def checkpoint(self):
        remote_project = discover_project(self.remote)
        return self.store.save({'schema': 1, 'project': {
            'identity': self.project.identity, 'project_id': self.project.project_id,
            'root': str(self.remote)}, 'files': capture_files(remote_project, self.store),
            'conversations': {'entries': [], 'issues': []}, 'producer': {'fixture': True}})

    def seed_registry(self, **values):
        entry = dict(schema=1, project_id=self.project.project_id, identity=self.project.identity,
                     local_root=str(self.root), local_active_root=str(self.root),
                     remote_root=str(self.remote), remote_active_root=str(self.remote),
                     active_host='codespace')
        entry.update(values)
        workspace._write(self.state, entry)

    def source_receipt(self, root):
        return (git(root, 'rev-parse', 'HEAD'), (root / '.git/index').read_bytes(),
                git(root, 'ls-files', '--stage', '-z'), git(root, 'diff', '--binary'),
                git(root, 'diff', '--cached', '--binary'), (root / 'app').read_bytes())

    def local(self, project=None):
        actual_run = subprocess.run
        def offline(args, *a, **kw):
            self.assertEqual(Path(args[0]).name, 'git', 'offline local work invoked a non-Git process')
            self.assertFalse({'fetch', 'clone', 'push', 'pull'} & set(args))
            return actual_run(args, *a, **kw)
        with ExitStack() as stack:
            stack.enter_context(mock.patch('devcontainer_setup.project_sync.herdr.open_local', return_value=0))
            stack.enter_context(mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection',
                                          side_effect=AssertionError('SSH must be unavailable')))
            stack.enter_context(mock.patch('devcontainer_setup.project_sync.codespaces.Codespaces',
                                          side_effect=AssertionError('Codespaces API must be unavailable')))
            stack.enter_context(mock.patch.object(workspace.scratch, 'stop', return_value={'phase': 'stopped'}))
            stack.enter_context(mock.patch.object(subprocess, 'run', side_effect=offline))
            stack.enter_context(redirect_stdout(io.StringIO()))
            return workspace.local(project or self.project, self.state)

    def test_first_offline_work_recovers_staged_state_and_preserves_both_original_checkouts(self):
        desktop_before, remote_before = self.source_receipt(self.root), self.source_receipt(self.remote)
        try:
            self.assertEqual(self.local(), 0)
        except OSError as error:
            self.fail('First offline entry must create its owned recovery parents: ' + str(error))
        value = read_json(self.state / 'workspace.json')
        recovered = Path(value['local_active_root'])
        self.assertEqual(recovered.parent, self.state / 'recovered')
        self.assertEqual(value['recovered_from'], self.snapshot)
        self.assertEqual(value['active_host'], 'local')
        self.assertEqual((recovered / 'app').read_bytes(), b'\x00remote current\xfe')
        self.assertEqual(git(recovered, 'show', ':app'), b'\x00remote staged\xff')
        self.assertEqual((recovered / 'untracked').read_text(), 'remote untracked')
        self.assertEqual((recovered / '.scratch/memory/context.md').read_text(), 'offline handoff context')
        self.assertEqual(self.source_receipt(self.root), desktop_before)
        self.assertEqual(self.source_receipt(self.remote), remote_before)

    def test_repeated_local_work_reuses_owned_root_and_keeps_edits_after_new_checkpoint(self):
        self.local()
        recovered = Path(read_json(self.state / 'workspace.json')['local_active_root'])
        (recovered / 'app').write_bytes(b'recovered staged owner edit')
        git(recovered, 'add', 'app')
        (recovered / 'app').write_bytes(b'recovered unstaged owner edit')
        before = self.source_receipt(recovered)
        (self.remote / 'app').write_bytes(b'later remote state')
        newer = self.checkpoint()
        self.store.acknowledge(newer, source='codespace')
        self.assertNotEqual(newer, self.snapshot)
        self.local(discover_project(recovered))
        self.local()
        self.assertEqual(read_json(self.state / 'workspace.json')['local_active_root'], str(recovered))
        self.assertEqual(self.source_receipt(recovered), before)
        self.assertEqual(len(list((self.state / 'recovered').iterdir())), 1)

    def test_recovery_collision_preserves_existing_destination_and_uses_new_root(self):
        existing = self.state / 'recovered' / self.snapshot
        existing.mkdir(parents=True)
        (existing / 'owner').write_text('preserve collided destination')
        self.local()
        active = Path(read_json(self.state / 'workspace.json')['local_active_root'])
        self.assertNotEqual(active, existing)
        self.assertEqual((existing / 'owner').read_text(), 'preserve collided destination')
        self.assertEqual((active / 'app').read_bytes(), b'\x00remote current\xfe')

    def test_local_refuses_cross_project_pairing_without_altering_registry(self):
        self.seed_registry(identity='github.com/someone/else')
        before = (self.state / 'workspace.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'registry|project'):
            self.local()
        self.assertEqual((self.state / 'workspace.json').read_bytes(), before)
        self.assertFalse((self.state / 'recovered').exists())

    def test_local_refuses_outer_project_identity_before_materializing_recovery(self):
        manifest = self.store.load(self.snapshot)
        manifest['project']['identity'] = 'github.com/foreign/other'
        snapshot = self.store.save(manifest)
        self.store.acknowledge(snapshot, source='codespace')
        before = (self.state / 'workspace.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.local()
        self.assertEqual((self.state / 'workspace.json').read_bytes(), before)
        self.assertFalse((self.state / 'recovered').exists())

    def test_local_refuses_mismatched_file_project_inside_valid_outer_manifest(self):
        manifest = self.store.load(self.snapshot)
        foreign = copy.deepcopy(manifest['files']['project'])
        foreign.update(remote='https://github.com/foreign/other.git', identity='github.com/foreign/other')
        import hashlib
        foreign['project_id'] = hashlib.sha256(foreign['identity'].encode()).hexdigest()[:24]
        manifest['files']['project'] = foreign
        snapshot = self.store.save(manifest)
        self.store.acknowledge(snapshot, source='codespace')
        (self.state / 'recovered').mkdir()
        before = (self.state / 'workspace.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'identity|project'):
            self.local()
        self.assertEqual((self.state / 'workspace.json').read_bytes(), before)
        self.assertEqual(list((self.state / 'recovered').iterdir()), [])

    def test_local_refuses_unacknowledged_remote_snapshot(self):
        receipt = self.state / 'checkpoints/receipts/codespace.json'
        receipt.unlink()
        with self.assertRaisesRegex(ValueError, 'acknowledged'):
            self.local()
        self.assertFalse((self.state / 'recovered').exists())

    def test_registered_sibling_checkout_is_required_even_for_same_repository(self):
        sibling = self.base / 'unregistered-sibling'
        shutil.copytree(self.root, sibling)
        before = (self.state / 'workspace.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'checkout|owns|pairing'):
            self.local(discover_project(sibling))
        self.assertEqual((self.state / 'workspace.json').read_bytes(), before)

    def test_failed_background_retarget_keeps_offline_recovery_available(self):
        from devcontainer_setup.project_sync.state import atomic_write
        import json
        atomic_write(self.state / workspace.scratch.CONFIG, json.dumps({
            'version': 1, 'identity': self.project.identity, 'root': str(self.root),
            'codespace': 'unreachable-space', 'remote_root': str(self.remote)}).encode())
        with mock.patch.object(workspace.scratch, 'retarget', side_effect=OSError('offline worker')) as retarget:
            with mock.patch.object(workspace.scratch, 'start', side_effect=AssertionError('retarget did not complete')):
                self.assertEqual(self.local(), 0)
        self.assertEqual(retarget.call_count, 1)
        active = Path(read_json(self.state / 'workspace.json')['local_active_root'])
        self.assertEqual((active / 'app').read_bytes(), b'\x00remote current\xfe')
