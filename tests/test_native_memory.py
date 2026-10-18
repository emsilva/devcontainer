"""Plain Claude launches keep project auto-memory in the synced scratch tree."""
import json
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest

from devcontainer_setup.project_sync import notes


class NativeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = SimpleNamespace(root=self.root)
        self.path = self.root / '.claude/settings.local.json'

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
