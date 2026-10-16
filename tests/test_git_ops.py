from pathlib import Path
import subprocess
import tempfile
import unittest

from devcontainer_setup.git_ops import GitPlan, ReviewChanged, proposed_branch


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


class GitPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.name', 'Test User')
        git(self.root, 'config', 'user.email', 'test@example.com')
        (self.root / 'app.txt').write_text('original\n')
        git(self.root, 'add', 'app.txt')
        git(self.root, 'commit', '-m', 'Initial')
        self.files = {'.devcontainer/devcontainer.json': b'{"name":"Test"}\n'}

    def test_commit_contains_only_reviewed_files_and_preserves_other_edits(self):
        (self.root / 'app.txt').write_text('owner edit\n')
        plan = GitPlan(self.root, self.files, branch=proposed_branch(self.root))
        self.assertIn('Test', plan.diff())
        plan.apply()
        sha = plan.commit()
        self.assertEqual(git(self.root, 'show', '--format=', '--name-only', sha), '.devcontainer/devcontainer.json')
        self.assertEqual((self.root / 'app.txt').read_text(), 'owner edit\n')
        self.assertEqual(git(self.root, 'branch', '--show-current'), 'setup/devcontainer')

    def test_unrelated_staged_changes_are_refused_before_writing(self):
        (self.root / 'app.txt').write_text('owner edit\n')
        git(self.root, 'add', 'app.txt')
        with self.assertRaisesRegex(ValueError, 'staged'):
            GitPlan(self.root, self.files)
        self.assertFalse((self.root / '.devcontainer').exists())

    def test_unrelated_changes_staged_after_apply_are_preserved_and_refused(self):
        head = git(self.root, 'rev-parse', 'HEAD')
        plan = GitPlan(self.root, self.files)
        plan.apply()
        (self.root / 'app.txt').write_text('owner staged edit\n')
        git(self.root, 'add', 'app.txt')
        (self.root / 'app.txt').write_text('owner unstaged edit\n')
        index = git(self.root, 'write-tree')

        with self.assertRaises(ReviewChanged):
            plan.commit()

        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.root, 'write-tree'), index)
        self.assertEqual(git(self.root, 'show', ':app.txt'), 'owner staged edit')
        self.assertEqual((self.root / 'app.txt').read_text(), 'owner unstaged edit\n')

    def test_generated_path_index_must_match_reviewed_worktree(self):
        head = git(self.root, 'rev-parse', 'HEAD')
        plan = GitPlan(self.root, self.files)
        plan.apply()
        name = '.devcontainer/devcontainer.json'
        target = self.root / name
        target.write_bytes(b'{"name":"Unreviewed"}\n')
        git(self.root, 'add', name)
        target.write_bytes(self.files[name])
        index = git(self.root, 'write-tree')

        with self.assertRaises(ReviewChanged):
            plan.commit()

        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.root, 'write-tree'), index)
        self.assertEqual(git(self.root, 'show', f':{name}'), '{"name":"Unreviewed"}')
        self.assertEqual(target.read_bytes(), self.files[name])

    def test_reviewed_path_changed_during_review_is_not_overwritten(self):
        plan = GitPlan(self.root, self.files)
        target = self.root / '.devcontainer/devcontainer.json'
        target.parent.mkdir()
        target.write_text('concurrent edit')
        with self.assertRaisesRegex(ValueError, 'changed'):
            plan.apply()
        self.assertEqual(target.read_text(), 'concurrent edit')

    def test_symlink_parent_cannot_escape_project(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / '.devcontainer').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                GitPlan(self.root, self.files)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_hook_rewrite_is_stopped_before_commit_then_can_be_reviewed(self):
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nprintf "{\\"name\\":\\"Formatted\\"}\\n" > .devcontainer/devcontainer.json\ngit add .devcontainer/devcontainer.json\n')
        hook.chmod(0o755)
        head = git(self.root, 'rev-parse', 'HEAD')
        plan = GitPlan(self.root, self.files)
        plan.apply()
        with self.assertRaises(ReviewChanged):
            plan.commit()
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        plan.accept_hook_changes()
        self.assertIn('Formatted', plan.diff())
        plan.commit()
        self.assertIn('Formatted', git(self.root, 'show', 'HEAD:.devcontainer/devcontainer.json'))

    def test_hook_cannot_include_unrelated_changes_in_commit(self):
        hook = self.root / '.git/hooks/commit-msg'
        hook.write_text('#!/bin/sh\nprintf "hook edit\\n" > app.txt\ngit add app.txt\n')
        hook.chmod(0o755)
        head = git(self.root, 'rev-parse', 'HEAD')
        plan = GitPlan(self.root, self.files)
        plan.apply()
        with self.assertRaises(ReviewChanged):
            plan.commit()
        with self.assertRaisesRegex(ValueError, 'unrelated'):
            plan.accept_hook_changes()
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_failing_hook_blocks_commit(self):
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\necho "test hook failed" >&2\nexit 1\n')
        hook.chmod(0o755)
        plan = GitPlan(self.root, self.files)
        plan.apply()
        with self.assertRaisesRegex(ValueError, 'test hook failed'):
            plan.commit()

    def test_identical_candidate_does_not_create_empty_commit(self):
        plan = GitPlan(self.root, {'app.txt': b'original\n'})
        self.assertFalse(plan.changed)

    def test_changed_head_invalidates_review(self):
        plan = GitPlan(self.root, self.files)
        git(self.root, 'commit', '--allow-empty', '-m', 'Concurrent commit')
        with self.assertRaisesRegex(ValueError, 'HEAD.*changed'):
            plan.apply()

    def test_push_refuses_remote_divergence(self):
        remote = self.root / 'remote.git'
        git(self.root, 'init', '--bare', str(remote))
        git(self.root, 'remote', 'add', 'origin', str(remote))
        git(self.root, 'push', '-u', 'origin', 'main')
        plan = GitPlan(self.root, self.files)
        plan.apply()
        plan.commit()
        old = git(self.root, 'rev-parse', 'HEAD~1')
        other = subprocess.check_output(['git', '-C', str(self.root), 'commit-tree', f'{old}^{{tree}}', '-p', old], input='Remote edit\n', text=True).strip()
        git(self.root, 'push', 'origin', f'{other}:refs/heads/main')
        with self.assertRaisesRegex(ValueError, 'diverg'):
            plan.push()


if __name__ == '__main__':
    unittest.main()
