"""Plain Claude launches keep project auto-memory in the synced scratch tree."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync import notes


class NativeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = SimpleNamespace(root=self.root, project_id='a' * 24)
        self.path = self.root / '.claude/settings.local.json'
        self.env = dict(os.environ, HOME=str(self.root / 'home'),
                        CLAUDE_CONFIG_DIR=str(self.root / 'native-claude'),
                        XDG_STATE_HOME=str(self.root / 'state'))
        self.env.pop('CLAUDE_CODE_PROJECT_DIR_NAME', None)
        context = patch.dict(os.environ, self.env, clear=True)
        context.start()
        self.addCleanup(context.stop)
        from devcontainer_setup.project_sync.sessions import claude_namespace
        self.source = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects' / claude_namespace(self.root) / 'memory'
        self.destination = self.root / '.scratch/memory/claude'

    def legacy(self):
        self.source.mkdir(parents=True)
        (self.source / 'MEMORY.md').write_bytes(b'# Existing project memory\nSee [topic](topics/decision.md).\n')
        (self.source / 'topics').mkdir()
        (self.source / 'topics/decision.md').write_bytes('Existing decision: ação\n'.encode())
        return {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}

    def test_copies_index_and_nested_notes_before_switch_and_preserves_source(self):
        expected = self.legacy()
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({'autoMemoryDirectory': str(self.source), 'autoMemoryEnabled': False}))
        original = notes.atomic_write
        def observe(path, data, **kwargs):
            if path == self.path:
                for name, body in expected.items():
                    self.assertEqual((self.destination / name).read_bytes(), body)
            return original(path, data, **kwargs)
        with patch.object(notes, 'atomic_write', side_effect=observe):
            notes.configure_memory(self.project)
        self.assertEqual(json.loads(self.path.read_bytes()), {
            'autoMemoryDirectory': str(self.destination), 'autoMemoryEnabled': False})
        for name, body in expected.items():
            self.assertEqual((self.source / name).read_bytes(), body)
            self.assertEqual((self.destination / name).read_bytes(), body)

    def test_backfills_already_redirected_empty_directory(self):
        expected = self.legacy()
        self.destination.mkdir(parents=True)
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({'autoMemoryDirectory': str(self.destination)}))
        before = self.path.read_bytes()
        notes.configure_memory(self.project)
        self.assertEqual(self.path.read_bytes(), before)
        for name, body in expected.items():
            self.assertEqual((self.destination / name).read_bytes(), body)

    def test_completed_migration_preserves_later_edits_and_deletions(self):
        self.legacy()
        notes.configure_memory(self.project)
        (self.destination / 'MEMORY.md').write_text('New scratch index\n')
        (self.destination / 'topics/decision.md').unlink()
        (self.source / 'MEMORY.md').write_text('Old session wrote here later\n')
        notes.configure_memory(self.project)
        self.assertEqual((self.destination / 'MEMORY.md').read_text(), 'New scratch index\n')
        self.assertFalse((self.destination / 'topics/decision.md').exists())

    def test_conflicting_note_refuses_before_copy_or_setting_change(self):
        self.legacy()
        self.destination.mkdir(parents=True)
        (self.destination / 'topics').mkdir()
        (self.destination / 'topics/decision.md').write_text('Owner scratch decision\n')
        self.path.parent.mkdir()
        self.path.write_text('{"permissions":{"allow":[]}}')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'conflict'):
            notes.configure_memory(self.project)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.destination / 'MEMORY.md').exists())
        self.assertEqual((self.destination / 'topics/decision.md').read_text(), 'Owner scratch decision\n')

    def test_failed_settings_write_can_retry_without_losing_copied_notes(self):
        expected = self.legacy()
        original = notes.atomic_write
        def fail_setting(path, data, **kwargs):
            if path == self.path:
                raise OSError('fixture setting write interrupted')
            return original(path, data, **kwargs)
        with patch.object(notes, 'atomic_write', side_effect=fail_setting):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                notes.configure_memory(self.project)
        self.assertFalse(self.path.exists())
        notes.configure_memory(self.project)
        for name, body in expected.items():
            self.assertEqual((self.destination / name).read_bytes(), body)

    def test_only_this_projects_memory_is_copied(self):
        expected = self.legacy()
        foreign = self.source.parent.parent / 'another-project/memory'
        foreign.mkdir(parents=True)
        (foreign / 'MEMORY.md').write_text('Other project private notes\n')
        notes.configure_memory(self.project)
        actual = {p.relative_to(self.destination): p.read_bytes() for p in self.destination.rglob('*') if p.is_file()}
        self.assertEqual(actual, expected)

    def test_existing_identical_notes_and_unrelated_scratch_notes_are_preserved(self):
        expected = self.legacy()
        self.destination.mkdir(parents=True)
        (self.destination / 'MEMORY.md').write_bytes(expected[Path('MEMORY.md')])
        (self.destination / 'scratch-only.md').write_text('New project note\n')
        notes.configure_memory(self.project)
        for name, data in expected.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
        self.assertEqual((self.destination / 'scratch-only.md').read_text(), 'New project note\n')

    def test_custom_project_or_global_memory_choice_is_not_redirected(self):
        self.legacy()
        for base, name in ((self.root, '.claude/settings.json'),
                           (Path(self.env['CLAUDE_CONFIG_DIR']), 'settings.json')):
            with self.subTest(name=str(base / name)):
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"autoMemoryDirectory":"/owner/custom-memory"}')
                with self.assertRaisesRegex(ValueError, 'memory directory differs'):
                    notes.configure_memory(self.project)
                self.assertFalse(self.path.exists())
                self.assertFalse(self.destination.exists())
                self.assertEqual(path.read_text(), '{"autoMemoryDirectory":"/owner/custom-memory"}')
                path.unlink()

    def test_shared_project_namespace_override_is_not_copied(self):
        self.legacy()
        with patch.dict(os.environ, {'CLAUDE_CODE_PROJECT_DIR_NAME': 'shared'}):
            with self.assertRaisesRegex(ValueError, 'Shared Claude project directory'):
                notes.configure_memory(self.project)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.destination.exists())

    def test_linked_worktree_uses_canonical_repositories_existing_memory(self):
        expected = self.legacy()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '--allow-empty', '-qm', 'Fixture'], check=True)
        linked = self.root / 'linked'
        subprocess.run(['git', '-C', str(self.root), 'worktree', 'add', '-qb', 'linked', str(linked)],
                       check=True, capture_output=True)
        notes.configure_memory(SimpleNamespace(root=linked, project_id='b' * 24))
        for name, data in expected.items():
            self.assertEqual((linked / '.scratch/memory/claude' / name).read_bytes(), data)

    def test_source_change_during_copy_does_not_switch_setting(self):
        self.legacy()
        original = notes._copy_memory
        def changing(path, data):
            original(path, data)
            (self.source / 'MEMORY.md').write_text('Concurrent legacy edit\n')
        with patch.object(notes, '_copy_memory', side_effect=changing):
            with self.assertRaisesRegex(ValueError, 'changed during migration'):
                notes.configure_memory(self.project)
        self.assertFalse(self.path.exists())
        self.assertEqual((self.source / 'MEMORY.md').read_text(), 'Concurrent legacy edit\n')

    @unittest.skipIf(os.geteuid() == 0, 'Root can read mode-000 directories')
    def test_unreadable_nested_notes_refuse_without_redirecting(self):
        self.legacy()
        directory = self.source / 'topics'
        directory.chmod(0)
        try:
            with self.assertRaises(OSError):
                notes.configure_memory(self.project)
            self.assertFalse(self.path.exists())
            self.assertFalse(self.destination.exists())
        finally:
            directory.chmod(0o700)

    def test_concurrent_destination_creation_never_overwrites_owner_note(self):
        self.legacy()
        original = notes._copy_memory
        def racing(path, data):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Concurrent scratch edit\n')
            original(path, data)
        with patch.object(notes, '_copy_memory', side_effect=racing):
            with self.assertRaisesRegex(ValueError, 'conflict'):
                notes.configure_memory(self.project)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.destination / 'MEMORY.md').exists())
        self.assertEqual((self.destination / 'topics/decision.md').read_text(), 'Concurrent scratch edit\n')

    def test_symlinked_source_or_destination_does_not_change_setting(self):
        for side in ('source', 'destination'):
            with self.subTest(side=side):
                target = self.root / ('private-' + side)
                target.mkdir()
                (target / 'MEMORY.md').write_text('Do not follow\n')
                link = self.source if side == 'source' else self.destination
                if side == 'destination' and not self.source.exists():
                    self.legacy()
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
                with self.assertRaises((OSError, ValueError)):
                    notes.configure_memory(self.project)
                self.assertFalse(self.path.exists())
                self.assertEqual((target / 'MEMORY.md').read_text(), 'Do not follow\n')
                link.unlink()

    def test_project_local_settings_preserve_owner_options_and_are_idempotent(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({'permissions': {'allow': ['Bash(task test)']}, 'autoMemoryEnabled': False}))
        expected = str(self.root / '.scratch/memory/claude')
        notes.configure_memory(self.project)
        configured = self.path.read_bytes()
        self.assertEqual(json.loads(configured), {
            'permissions': {'allow': ['Bash(task test)']},
            'autoMemoryEnabled': False, 'autoMemoryDirectory': expected})
        notes.configure_memory(self.project)
        self.assertEqual(self.path.read_bytes(), configured)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_new_configuration_is_scoped_to_project_without_global_profile_writes(self):
        notes.configure_memory(self.project)
        self.assertEqual(json.loads(self.path.read_bytes()), {
            'autoMemoryDirectory': str(self.root / '.scratch/memory/claude')})
        self.assertEqual([p.relative_to(self.root).as_posix() for p in self.root.rglob('*') if p.is_file()],
                         ['.claude/settings.local.json'])

    def test_conflicting_memory_directory_and_symlink_preserve_originals(self):
        self.path.parent.mkdir()
        self.path.write_text('{"autoMemoryDirectory":"/owner/custom-memory"}')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'memory directory'):
            notes.configure_memory(self.project)
        self.assertEqual(self.path.read_bytes(), before)
        target = self.root / 'owner.json'
        target.write_text('{}')
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            notes.configure_memory(self.project)
        self.assertEqual(target.read_text(), '{}')

    def test_generated_ignore_keeps_machine_specific_configuration_out_of_git(self):
        files = {}
        notes.integrate_notes(self.root, files, True)
        self.assertIn(b'/.claude/settings.local.json', files['.gitignore'])

    def test_tracked_local_settings_are_not_changed_by_runtime_sync(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.path.parent.mkdir()
        self.path.write_text('{"permissions":{"allow":[]}}')
        subprocess.run(['git', '-C', str(self.root), 'add', '-f', '.claude/settings.local.json'], check=True)
        before = self.path.read_bytes()
        index = (self.root / '.git/index').read_bytes()
        with self.assertRaisesRegex(ValueError, 'tracked'):
            notes.configure_memory(self.project)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.root / '.git/index').read_bytes(), index)
