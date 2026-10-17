from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

from devcontainer_setup.project_sync.notes import integrate_notes


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def generate(self):
        files = {}
        integrate_notes(self.root, files, True)
        return files

    def install(self, files):
        for name, data in files.items():
            self.put(name, data)

    def test_is_pure_preserves_instruction_bytes_and_adds_only_tracked_markers(self):
        agents = b'# Owner instructions\r\nKeep me byte-for-byte.\r\n'
        claude = 'Personal instructions without final newline: ação'.encode()
        self.put('AGENTS.md', agents)
        self.put('CLAUDE.md', claude)
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        files = self.generate()
        self.assertEqual(set(files), {'AGENTS.md', 'CLAUDE.md', '.codex/config.toml', '.scratch/.gitignore', '.scratch/README.md'})
        self.assertTrue(files['AGENTS.md'].startswith(agents))
        self.assertTrue(files['CLAUDE.md'].startswith(claude))
        self.assertIn(b'.scratch/memory/', files['AGENTS.md'])
        self.assertIn(b'.scratch/memory/', files['CLAUDE.md'])
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(files['.scratch/.gitignore'], b'*\n!.gitignore\n!README.md\n')
        self.assertEqual(tomllib.loads(files['.codex/config.toml'].decode())['memories'], {'generate_memories': False, 'use_memories': False})

    def test_rerun_preserves_surrounding_edits_and_candidate_map_is_idempotent(self):
        files = self.generate()
        integrate_notes(self.root, files, True)
        self.assertEqual(files, self.generate())
        self.install(files)
        self.put('AGENTS.md', b'User prefix\n' + files['AGENTS.md'] + b'\nUser suffix\n')
        again = self.generate()
        self.assertEqual(again['AGENTS.md'], b'User prefix\n' + files['AGENTS.md'] + b'\nUser suffix\n')
        self.assertEqual(again['CLAUDE.md'], files['CLAUDE.md'])
        self.assertEqual(again['.codex/config.toml'], files['.codex/config.toml'])

    def test_edited_duplicate_or_malformed_blocks_refuse_without_partial_candidates(self):
        files = self.generate()
        for name in ('AGENTS.md', 'CLAUDE.md', '.codex/config.toml'):
            for kind in ('edited', 'duplicate', 'missing-end', 'bad-checksum'):
                with self.subTest(name=name, kind=kind):
                    self.install(files)
                    data = files[name]
                    if kind == 'edited':
                        data = data.replace(b'.scratch/memory/', b'.scratch/changed/') if name.endswith('.md') else data.replace(b'false', b'true', 1)
                    elif kind == 'duplicate':
                        data += data
                    elif kind == 'missing-end':
                        data = b'\n'.join(data.split(b'\n')[:-2]) + b'\n'
                    else:
                        data = data.replace(b'sha256=', b'sha256=x', 1)
                    self.put(name, data)
                    candidates = {'existing.txt': b'existing'}
                    with self.assertRaises(ValueError):
                        integrate_notes(self.root, candidates, True)
                    self.assertEqual(candidates, {'existing.txt': b'existing'})
        self.install(files)

    def test_existing_toml_tables_and_unrelated_memory_options_are_retained(self):
        for original in (
            b'model="normal"\n[features]\nfoo=true\n',
            b'[memories]\nmax_rollout_age_days=30\n[features]\nfoo=true\n',
            b'[memories]\ngenerate_memories=false\nmax_rollout_age_days=30\n',
            b'[memories]\ngenerate_memories=false\nuse_memories=false\n',
            b'[projects."/my/project"]\ntrust_level="trusted"\n',
        ):
            with self.subTest(original=original):
                self.put('.codex/config.toml', original)
                files = self.generate()
                proposed = files['.codex/config.toml']
                parsed = tomllib.loads(proposed.decode())
                old = tomllib.loads(original.decode())
                expected = {**old, 'memories': {**old.get('memories', {}), 'generate_memories': False, 'use_memories': False}}
                expected['features'] = {**old.get('features', {}), 'memories': False}
                self.assertEqual(parsed, expected)
                for line in original.splitlines(keepends=True):
                    self.assertIn(line, proposed)
                self.put('.codex/config.toml', proposed)
                self.assertEqual(self.generate()['.codex/config.toml'], proposed)

    def test_conflicting_or_invalid_existing_policy_refuses(self):
        for original in (b'[memories]\nuse_memories=true\n', b'[memories]\ngenerate_memories=true\n', b'memories=false\n', b'[memories]\nuse_memories="false"\n', b'[broken\n'):
            with self.subTest(original=original):
                self.put('.codex/config.toml', original)
                with self.assertRaises(ValueError):
                    self.generate()
                self.assertEqual((self.root / '.codex/config.toml').read_bytes(), original)

    def test_scratch_payload_stays_ignored_after_git_add_all(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.put('.scratch/memory/notes.md', b'private project facts')
        self.put('.scratch/data.bin', b'payload')
        self.install(self.generate())
        subprocess.run(['git', '-C', str(self.root), 'add', '--all'], check=True)
        tracked = subprocess.check_output(['git', '-C', str(self.root), 'ls-files']).decode().splitlines()
        self.assertIn('.scratch/.gitignore', tracked)
        self.assertIn('.scratch/README.md', tracked)
        self.assertNotIn('.scratch/memory/notes.md', tracked)
        self.assertNotIn('.scratch/data.bin', tracked)

    def test_symlink_ancestors_and_destinations_are_refused(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'config.toml').write_bytes(b'model="owner"\n')
        (self.root / '.codex').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.generate()
        (self.root / '.codex').unlink()
        (self.root / 'AGENTS.md').symlink_to(outside / 'config.toml')
        with self.assertRaises((ValueError, OSError)):
            self.generate()
        self.assertEqual((outside / 'config.toml').read_bytes(), b'model="owner"\n')

    def test_disabled_is_noop(self):
        self.put('AGENTS.md', b'Unmanaged notes')
        candidates = {'other': b'bytes'}
        integrate_notes(self.root, candidates, False)
        self.assertEqual(candidates, {'other': b'bytes'})


    def test_memory_feature_is_disabled_in_its_own_table_and_preserves_other_features(self):
        for original in (b'', b'[features]\nfoo=true\n', b'[features]\nmemories=false\nfoo=true\n', b'features.foo=true\n'):
            with self.subTest(original=original):
                self.put('.codex/config.toml', original)
                proposed = self.generate()['.codex/config.toml']
                parsed = tomllib.loads(proposed.decode())
                self.assertIs(parsed['features']['memories'], False)
                if b'foo' in original:
                    self.assertIs(parsed['features']['foo'], True)
                self.put('.codex/config.toml', proposed)
                self.assertEqual(self.generate()['.codex/config.toml'], proposed)
                for line in original.splitlines(keepends=True):
                    self.assertIn(line, proposed)

    def test_existing_true_or_invalid_memory_feature_refuses_without_partial_candidates(self):
        for original in (b'[features]\nmemories=true\n', b'[features]\nmemories="false"\n', b'features=false\n'):
            self.put('.codex/config.toml', original)
            candidates = {'owner.txt': b'owner'}
            with self.subTest(original=original), self.assertRaises(ValueError):
                integrate_notes(self.root, candidates, True)
            self.assertEqual(candidates, {'owner.txt': b'owner'})
            self.assertEqual((self.root / '.codex/config.toml').read_bytes(), original)

    def test_existing_memory_block_is_preserved_and_feature_block_has_separate_ownership(self):
        original = self.generate()['.codex/config.toml']
        self.assertIn(b'devcontainer-setup:memory-feature begin', original)
        self.assertIn(b'devcontainer-setup:project-memory begin', original)
        for mutation in (original.replace(b'memories = false', b'memories = true'), original + original[original.index(b'# devcontainer-setup:memory-feature begin'):]):
            self.put('.codex/config.toml', mutation)
            with self.assertRaises(ValueError):
                self.generate()
