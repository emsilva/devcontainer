import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import devcontainer_setup.git_ops as operations

from devcontainer_setup.git_ops import (GitPlan, ReviewChanged, default_branch, default_contains,
                                        fast_forward_checkout, merged_setup_branches, next_setup_branch,
                                        proposed_branch, remove_setup_worktree, remove_worktree, setup_worktree)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def run(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)


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

    def test_reviewed_deletion_is_committed_without_other_edits(self):
        plan = GitPlan(self.root, {'app.txt': None})
        self.assertIn('/dev/null', plan.diff())
        plan.apply()
        sha = plan.commit()
        self.assertFalse((self.root / 'app.txt').exists())
        self.assertEqual(git(self.root, 'diff-tree', '--no-commit-id', '--name-status', '-r', sha), 'D\tapp.txt')

    def test_hook_cannot_recreate_retired_helper_or_gain_review_acceptance(self):
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nprintf resurrected > app.txt\ngit add app.txt\n')
        hook.chmod(0o755)
        plan = GitPlan(self.root, {'app.txt': None})
        plan.apply()
        with self.assertRaises(ReviewChanged):
            plan.commit()
        with self.assertRaisesRegex(ValueError, 'recreated'):
            plan.accept_hook_changes()
        self.assertEqual((self.root / 'app.txt').read_text(), 'resurrected')

    def test_commit_contains_only_reviewed_files_and_preserves_other_edits(self):
        (self.root / 'app.txt').write_text('owner edit\n')
        plan = GitPlan(self.root, self.files, branch=proposed_branch(self.root))
        self.assertIn('Test', plan.diff())
        plan.apply()
        sha = plan.commit()
        self.assertEqual(git(self.root, 'show', '--format=', '--name-only', sha), '.devcontainer/devcontainer.json')
        self.assertEqual((self.root / 'app.txt').read_text(), 'owner edit\n')
        self.assertEqual(git(self.root, 'branch', '--show-current'), 'setup/devcontainer')

    def test_reviewed_files_under_a_path_the_project_ignores_are_committed(self):
        # A project that ignores all of /.scratch/ still gets the reviewed scratch markers.
        (self.root / '.gitignore').write_text('/.scratch/\n')
        git(self.root, 'add', '.gitignore')
        git(self.root, 'commit', '-m', 'Ignore scratch')
        files = {'.scratch/.gitignore': b'*\n!.gitignore\n!README.md\n', '.scratch/README.md': b'# Project scratch\n'}
        plan = GitPlan(self.root, files, branch=proposed_branch(self.root))
        plan.apply()
        sha = plan.commit()
        self.assertEqual(git(self.root, 'show', '--format=', '--name-only', sha).splitlines(),
                         ['.scratch/.gitignore', '.scratch/README.md'])
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

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


    def snapshot(self):
        return (git(self.root, 'branch', '--show-current'), git(self.root, 'rev-parse', 'HEAD'),
                git(self.root, 'write-tree'), git(self.root, 'status', '--porcelain'),
                (self.root / '.devcontainer/devcontainer.json').read_bytes())

    def test_commit_refuses_changed_identity_after_apply(self):
        for change in ('branch', 'head'):
            with self.subTest(change=change):
                plan = GitPlan(self.root, self.files, branch='setup/devcontainer')
                plan.apply()
                if change == 'branch':
                    git(self.root, 'switch', '-c', 'owner-work')
                else:
                    parent = git(self.root, 'rev-parse', 'HEAD')
                    sha = subprocess.check_output(['git', '-C', str(self.root), 'commit-tree',
                        f'{parent}^{{tree}}', '-p', parent], input='Owner history\n', text=True).strip()
                    git(self.root, 'update-ref', 'HEAD', sha)
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'HEAD|branch'):
                    plan.commit()
                self.assertEqual(self.snapshot(), before)
                # Restore only test-owned fixture state for the second case.
                git(self.root, 'reset', '--hard', plan.head)
                git(self.root, 'switch', 'main')
                git(self.root, 'branch', '-D', 'setup/devcontainer')
                if change == 'branch':
                    git(self.root, 'branch', '-D', 'owner-work')

    def test_commit_hook_branch_switch_is_refused_and_recovery_preserves_it(self):
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\ngit switch -c owner-work\n')
        hook.chmod(0o755)
        plan = GitPlan(self.root, self.files, branch='setup/devcontainer')
        plan.apply()
        head = plan.head
        tree = git(self.root, 'write-tree')
        with self.assertRaisesRegex(ValueError, 'branch|review|Review'):
            plan.commit()
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.root, 'write-tree'), tree)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'branch'):
            plan.accept_hook_changes()
        self.assertEqual(self.snapshot(), before)

    def remote(self):
        remote = Path(self.tmp.name) / 'remote.git'
        git(self.root, 'init', '--bare', str(remote))
        git(self.root, 'remote', 'add', 'origin', str(remote))
        return remote

    def test_push_refuses_branch_switch(self):
        remote = self.remote()
        plan = GitPlan(self.root, self.files, branch='setup/devcontainer')
        plan.apply()
        plan.commit()
        git(self.root, 'switch', '-c', 'owner-work')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'branch'):
            plan.push()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(git(remote, 'for-each-ref'), '')

    def test_push_publishes_exact_reviewed_sha_during_concurrent_head_advance(self):
        remote = self.remote()
        plan = GitPlan(self.root, self.files, branch='setup/devcontainer')
        plan.apply()
        reviewed = plan.commit()
        real_git = operations.git
        advanced = []
        def boundary(root, *args, **kwargs):
            result = real_git(root, *args, **kwargs)
            if args[:1] == ('ls-remote',) and not advanced:
                (self.root / 'owner.txt').write_text('Unreviewed owner work\n')
                git(self.root, 'add', 'owner.txt')
                git(self.root, 'commit', '-m', 'Owner concurrent work')
                advanced.append(git(self.root, 'rev-parse', 'HEAD'))
            return result
        with patch.object(operations, 'git', side_effect=boundary):
            plan.push()
        self.assertEqual(git(remote, 'rev-parse', 'refs/heads/setup/devcontainer'), reviewed)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), advanced[0])
        self.assertEqual(git(self.root, 'rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/setup/devcontainer')

    def test_normal_push_sets_upstream_to_reviewed_commit(self):
        remote = self.remote()
        plan = GitPlan(self.root, self.files, branch='setup/devcontainer')
        plan.apply()
        sha = plan.commit()
        self.assertEqual(plan.push(), sha)
        self.assertEqual(git(remote, 'rev-parse', 'refs/heads/setup/devcontainer'), sha)
        self.assertEqual(git(self.root, 'rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/setup/devcontainer')


    def test_unborn_commit_and_unchanged_plan_keep_the_reviewed_identity(self):
        unborn = self.root / 'unborn'
        unborn.mkdir()
        git(unborn, 'init', '-b', 'main')
        git(unborn, 'config', 'user.name', 'Test User')
        git(unborn, 'config', 'user.email', 'test@example.com')
        plan = GitPlan(unborn, self.files, branch='setup/devcontainer')
        plan.apply()
        sha = plan.commit()
        self.assertEqual(git(unborn, 'show', '-s', '--format=%P', sha), '')
        unchanged = GitPlan(unborn, self.files, branch='unused-proposed-branch')
        unchanged.branch = unchanged.start_branch  # interactive() no-change handoff
        unchanged.apply()
        self.assertEqual(unchanged.commit(), sha)
        self.assertEqual(git(unborn, 'branch', '--show-current'), 'setup/devcontainer')


    def test_push_with_single_branch_fetch_preserves_refspec_and_sets_upstream(self):
        remote = self.remote()
        narrow = '+refs/heads/main:refs/remotes/origin/main'
        git(self.root, 'config', 'remote.origin.fetch', narrow)
        plan = GitPlan(self.root, self.files, branch='setup/devcontainer')
        plan.apply()
        sha = plan.commit()
        self.assertEqual(plan.push(), sha)
        self.assertEqual(git(remote, 'rev-parse', 'refs/heads/setup/devcontainer'), sha)
        self.assertEqual(git(self.root, 'config', '--get-all', 'remote.origin.fetch'), narrow)
        self.assertEqual(git(self.root, 'config', 'branch.setup/devcontainer.remote'), 'origin')
        self.assertEqual(git(self.root, 'config', 'branch.setup/devcontainer.merge'), 'refs/heads/setup/devcontainer')


class SetupWorktreeTests(unittest.TestCase):
    """Fixture repos with a local bare origin; the owner's Git config never applies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        isolated = patch.dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        isolated.start()
        self.addCleanup(isolated.stop)
        self.root = self.base / 'state/setup-worktrees/project'
        self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.project = self.make_project('project')
        self.clones = 0

    def make_project(self, name):
        project, origin = self.base / name, self.base / f'{name}.git'
        git(self.base, 'init', '--quiet', '--bare', '-b', 'main', str(origin))
        project.mkdir()
        git(project, 'init', '--quiet', '-b', 'main')
        git(project, 'config', 'user.name', 'Test User')
        git(project, 'config', 'user.email', 'test@example.com')
        (project / 'app.txt').write_text('original\n')
        git(project, 'add', 'app.txt')
        git(project, 'commit', '--quiet', '-m', 'Initial')
        git(project, 'remote', 'add', 'origin', str(origin))
        git(project, 'push', '--quiet', '-u', 'origin', 'main')
        return project

    def origin(self, project):
        return Path(git(project, 'remote', 'get-url', 'origin'))

    def push_to_origin(self, project, name, text='remote\n', ref='refs/heads/main'):
        """Commit on origin from another clone, as another machine would; return the new SHA."""
        self.clones += 1
        clone = self.base / f'clone-{self.clones}'
        git(self.base, 'clone', '--quiet', str(self.origin(project)), str(clone))
        git(clone, 'config', 'user.name', 'Other User')
        git(clone, 'config', 'user.email', 'other@example.com')
        (clone / name).write_text(text)
        git(clone, 'add', '-f', name)
        git(clone, 'commit', '--quiet', '-m', f'Add {name}')
        git(clone, 'push', '--quiet', 'origin', f'HEAD:{ref}')
        return git(clone, 'rev-parse', 'HEAD')

    def snapshot(self, project):
        files = {path.relative_to(project).as_posix(): path.read_bytes() for path in project.rglob('*')
                 if path.is_file() and '.git' not in path.relative_to(project).parts}
        heads = [line for line in git(project, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads').splitlines()
                 if not line.startswith('refs/heads/setup/')]
        return {'branch': git(project, 'branch', '--show-current'), 'head': git(project, 'rev-parse', 'HEAD'),
                'status': git(project, 'status', '--porcelain', '--untracked-files=all'),
                'index': git(project, 'ls-files', '--stage'), 'files': files, 'heads': heads,
                'stash': (git(project, 'stash', 'list'),
                          run(project, 'rev-parse', '--verify', '--quiet', 'refs/stash').stdout.strip())}

    def recorded(self):
        """Record each Git command the helpers run, minus `git -C <project>`."""
        calls, real = [], subprocess.run

        def record(args, **kwargs):
            if args[:2] == ['git', '-C']:
                calls.append(tuple(args[3:]))
            return real(args, **kwargs)
        return calls, patch.object(operations.subprocess, 'run', side_effect=record)

    @contextlib.contextmanager
    def namespace_checkout(self, state_home):
        from devcontainer_setup.__main__ import setup_checkout
        from devcontainer_setup.project_sync.state import Project
        project = Project(self.project, 'git@github.com:owner/project', 'github.com/owner/project', 'a' * 24)
        # Only identity discovery is synthetic: Git uses the fixture's local
        # bare origin, including the wizard's actual creation and cleanup path.
        with patch.dict(os.environ, XDG_STATE_HOME=str(state_home)), \
                patch('devcontainer_setup.__main__._github_repository', return_value='owner/project'), \
                patch('devcontainer_setup.project_sync.state.discover_project', return_value=project):
            with setup_checkout(self.project) as (checkout, default):
                self.assertEqual(default, 'main')
                yield project, checkout

    def test_first_setup_creates_a_private_namespace(self):
        from devcontainer_setup.project_sync.state import read_json, state_dir
        home = self.base / 'fresh/state'
        namespace = home / 'devcontainer-setup'
        previous = os.umask(0o022)
        try:
            with self.namespace_checkout(home) as (project, checkout):
                root = namespace / 'setup-worktrees' / project.project_id
                self.assertEqual(checkout.parent, root)
                for path in (home, namespace, root.parent, root):
                    mode = stat.S_IMODE(path.stat().st_mode)
                    self.assertEqual(mode, 0o700, f'{path} mode {mode:04o}; expected 0700')
                with self.assertRaises(FileNotFoundError):
                    read_json(state_dir(project, {'XDG_STATE_HOME': str(home)}) / 'missing.json')
                self.assertEqual((checkout / 'app.txt').read_text(), 'original\n')
        finally:
            os.umask(previous)
        self.assertEqual(git(self.project, 'branch', '--list', 'setup/*'), '')

    def test_setup_repairs_a_loose_namespace_it_owns(self):
        home = self.base / 'loose'
        namespace = home / 'devcontainer-setup'
        root = namespace / 'setup-worktrees' / ('a' * 24)
        root.mkdir(parents=True)
        home.chmod(0o755)
        for mode in (0o755, 0o777, 0o2750):
            with self.subTest(mode=f'{mode:04o}'):
                for path in (namespace, root.parent, root):
                    path.chmod(mode)
                with self.namespace_checkout(home):
                    for path in (namespace, root.parent, root):
                        actual = stat.S_IMODE(path.stat().st_mode)
                        self.assertEqual(actual, 0o700, f'{path} mode {actual:04o}; expected 0700')
                    self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o755)

    def test_setup_refuses_a_symlinked_or_foreign_namespace(self):
        components = ('', 'devcontainer-setup', 'devcontainer-setup/setup-worktrees',
                      'devcontainer-setup/setup-worktrees/' + 'a' * 24)
        for index, component in enumerate(components):
            with self.subTest(symlink=component or 'state home'):
                home, outside = self.base / f'linked-{index}', self.base / f'outside-{index}'
                outside.mkdir(mode=0o755)
                link = home / component
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(OSError):
                    with self.namespace_checkout(home):
                        pass
                self.assertEqual(list(outside.iterdir()), [])
                self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
        for index, component in enumerate(components[1:]):
            with self.subTest(foreign=component):
                home = self.base / f'foreign-{index}'
                directory = home / component
                directory.mkdir(parents=True)
                directory.chmod(0o755)
                target = directory.stat()
                real_fstat = os.fstat
                def foreign_owner(fd):
                    info = real_fstat(fd)
                    if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                        fields = list(info)
                        fields[4] = os.geteuid() + 1
                        return os.stat_result(fields)
                    return info
                with patch('devcontainer_setup.project_sync.state.os.fstat', side_effect=foreign_owner):
                    with self.assertRaises(PermissionError) as caught:
                        with self.namespace_checkout(home):
                            pass
                self.assertIn(str(directory), str(caught.exception))
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
                self.assertEqual(list(directory.iterdir()), [])

    def test_default_branch_reads_origin_head_without_the_network(self):
        git(self.project, 'remote', 'set-url', 'origin', str(self.base / 'unreachable.git'))
        calls, recording = self.recorded()
        with recording:
            self.assertEqual(default_branch(self.project), 'main')
            git(self.project, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
            self.assertEqual(default_branch(self.project), 'trunk')
        self.assertTrue(calls)
        self.assertFalse([call for call in calls if call[0] in {'remote', 'ls-remote', 'fetch'}], calls)

    def test_setup_branch_is_cut_from_origin_default_not_current(self):
        project = self.project
        git(project, 'switch', '--quiet', '-c', 'feature/local')
        (project / 'feature.txt').write_text('unpushed\n')
        git(project, 'add', 'feature.txt')
        git(project, 'commit', '--quiet', '-m', 'Unpushed feature')
        (project / 'app.txt').write_text('stashed owner edit\n')
        git(project, 'stash', 'push', '--quiet', '-m', 'owner stash')
        (project / 'app.txt').write_text('owner edit\n')
        (project / 'notes.txt').write_text('untracked owner work\n')
        stale = git(project, 'rev-parse', 'refs/remotes/origin/main')
        fresh = self.push_to_origin(project, 'remote.txt')
        self.push_to_origin(project, 'other.txt', ref='refs/heads/other')
        git(self.origin(project), 'tag', 'v1', fresh)
        before, published = self.snapshot(project), git(self.origin(project), 'for-each-ref')

        path, branch = setup_worktree(project, self.root)

        self.assertEqual(branch, 'setup/devcontainer')
        self.assertEqual(path, self.root / 'setup-devcontainer')
        self.assertNotEqual(stale, fresh)
        self.assertEqual(git(path, 'rev-parse', 'HEAD'), fresh)
        self.assertEqual(git(path, 'branch', '--show-current'), branch)
        self.assertEqual((path / 'app.txt').read_text(), 'original\n')
        self.assertTrue((path / 'remote.txt').exists())
        self.assertFalse((path / 'feature.txt').exists())
        self.assertFalse((path / 'notes.txt').exists())
        self.assertEqual(self.snapshot(project), before)
        self.assertTrue(before['stash'][1])
        self.assertEqual((project / 'notes.txt').read_text(), 'untracked owner work\n')
        # Fetches only origin <default>, sets no upstream and never pushes.
        self.assertEqual(git(project, 'rev-parse', 'refs/remotes/origin/main'), fresh)
        self.assertNotEqual(run(project, 'rev-parse', '--verify', 'refs/remotes/origin/other').returncode, 0)
        self.assertEqual(git(project, 'tag', '--list'), '')
        self.assertNotEqual(run(project, 'config', f'branch.{branch}.merge').returncode, 0)
        self.assertEqual(git(self.origin(project), 'for-each-ref'), published)

    def test_next_setup_branch_skips_local_and_remote_names(self):
        project = self.project
        self.assertEqual(next_setup_branch(project), 'setup/devcontainer')
        git(project, 'branch', 'setup/devcontainer')
        git(project, 'push', '--quiet', 'origin', 'main:refs/heads/setup/devcontainer-2')
        git(project, 'push', '--quiet', 'origin', 'main:refs/heads/other/setup/devcontainer-3')
        # Only origin knows the name now; a local tracking ref must not be needed.
        git(project, 'update-ref', '-d', 'refs/remotes/origin/setup/devcontainer-2')
        self.assertEqual(next_setup_branch(project), 'setup/devcontainer-3')

    def test_setup_worktree_never_reuses_or_deletes_a_leftover(self):
        project = self.project
        first, first_branch = setup_worktree(project, self.root)  # A crashed run leaves this behind.
        (first / 'half-done.txt').write_text('leftover\n')
        stray = self.root / 'setup-devcontainer-2'
        stray.mkdir()
        (stray / 'keep.txt').write_text('stray\n')
        detached = self.root / 'detached-leftover'
        git(project, 'worktree', 'add', '--quiet', '--detach', str(detached), 'main')
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            second, second_branch = setup_worktree(project, self.root)
        self.assertEqual(second_branch, 'setup/devcontainer-2')
        self.assertNotIn(second, (first, stray))
        self.assertEqual(second.parent, self.root)
        self.assertEqual(git(second, 'branch', '--show-current'), second_branch)
        self.assertEqual((stray / 'keep.txt').read_text(), 'stray\n')
        self.assertEqual((first / 'half-done.txt').read_text(), 'leftover\n')
        self.assertEqual(git(first, 'branch', '--show-current'), first_branch)
        self.assertIn(str(first), error.getvalue())
        self.assertIn(first_branch, error.getvalue())
        self.assertIn(f'{detached} on a detached HEAD', error.getvalue())
        self.assertTrue(detached.exists())

    def test_setup_worktree_failure_carries_gits_own_reason(self):
        hook = self.project / '.git/hooks/post-checkout'
        hook.write_text('#!/bin/sh\necho "hook says no: lint failed" >&2\nexit 1\n')
        hook.chmod(0o755)
        with self.assertRaises(ValueError) as caught:
            setup_worktree(self.project, self.root)
        self.assertIn('hook says no: lint failed', str(caught.exception))
        self.assertIn('check that', str(caught.exception))

    def commit_in(self, path, name='setup.txt'):
        (path / name).write_text('setup\n')
        git(path, 'add', name)
        git(path, 'commit', '--quiet', '-m', 'Setup commit')
        return git(path, 'rev-parse', 'HEAD')

    def merge_on_origin(self, project, branch):
        """Merge origin's <branch> into origin's main from another clone, as a PR merge would."""
        self.clones += 1
        clone = self.base / f'clone-{self.clones}'
        git(self.base, 'clone', '--quiet', str(self.origin(project)), str(clone))
        git(clone, 'config', 'user.name', 'Other User')
        git(clone, 'config', 'user.email', 'other@example.com')
        git(clone, 'merge', '--quiet', '--no-ff', '-m', f'Merge {branch}', f'origin/{branch}')
        git(clone, 'push', '--quiet', 'origin', 'HEAD:refs/heads/main')
        return git(clone, 'rev-parse', 'HEAD')

    def refs(self, project):
        return (git(project, 'worktree', 'list'), git(project, 'for-each-ref', '--exclude=refs/remotes/origin/main'),
                git(self.origin(project), 'for-each-ref'))

    def test_remove_setup_worktree_deletes_branch_after_worktree(self):
        project, origin = self.project, self.origin(self.project)
        git(project, 'push', '--quiet', 'origin', 'main:refs/heads/setup/devcontainer-9')
        git(project, 'branch', 'setup/devcontainer-9')
        path, branch = setup_worktree(project, self.root)
        self.commit_in(path)
        git(path, 'push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}')
        self.merge_on_origin(project, branch)
        calls, recording = self.recorded()
        with recording:
            remove_setup_worktree(project, path, branch, remote=True, root=self.root)
        steps = [next(index for index, call in enumerate(calls) if call[:len(prefix)] == prefix)
                 for prefix in (('worktree', 'remove'), ('branch', '-D'), ('push',))]
        self.assertEqual(steps, sorted(steps), calls)
        self.assertEqual(calls[-1][0], 'ls-remote', calls)
        self.assertIn(branch, calls[-1][-1])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(project, 'worktree', 'list'))
        self.assertNotEqual(run(project, 'rev-parse', '--verify', f'refs/heads/{branch}').returncode, 0)
        self.assertEqual(git(origin, 'for-each-ref', '--format=%(refname)', f'refs/heads/{branch}'), '')
        # Never any other branch.
        self.assertTrue(git(project, 'rev-parse', '--verify', 'refs/heads/setup/devcontainer-9'))
        self.assertTrue(git(origin, 'rev-parse', '--verify', 'refs/heads/setup/devcontainer-9'))
        self.assertTrue(git(origin, 'rev-parse', '--verify', 'refs/heads/main'))
        self.assertEqual(git(project, 'branch', '--show-current'), 'main')

    def test_remove_setup_worktree_keeps_an_unmerged_commit(self):
        # Commit only leaves exactly this: a kept branch whose worktree is gone (and, in the
        # interim launch, the same commit on origin). A later cleanup must not destroy it.
        project, origin = self.project, self.origin(self.project)
        path, branch = setup_worktree(project, self.root)
        cut = git(path, 'rev-parse', 'HEAD')
        sha = self.commit_in(path)
        remove_worktree(project, path, branch, root=self.root)
        git(project, 'push', '--quiet', 'origin', f'{branch}:refs/heads/{branch}')
        before = self.refs(project)
        for remote in (True, False):
            with self.subTest(remote=remote):
                with self.assertRaisesRegex(ValueError, f'{branch}.*not on main'):
                    remove_setup_worktree(project, path, branch, remote=remote, root=self.root, cut=cut)
                self.assertEqual(self.refs(project), before)
        self.assertEqual(git(project, 'rev-parse', f'refs/heads/{branch}'), sha)
        self.assertEqual(git(origin, 'rev-parse', f'refs/heads/{branch}'), sha)

    def test_remove_setup_worktree_without_remote_makes_no_network_call(self):
        project = self.project
        path, branch = setup_worktree(project, self.root)
        cut = git(path, 'rev-parse', 'HEAD')
        git(path, 'push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}')
        calls, recording = self.recorded()
        with recording:
            remove_setup_worktree(project, path, branch, remote=False, root=self.root, cut=cut)
        self.assertFalse([call for call in calls if call[0] in {'ls-remote', 'push', 'fetch'}], calls)
        self.assertFalse(path.exists())
        self.assertNotEqual(run(project, 'rev-parse', '--verify', f'refs/heads/{branch}').returncode, 0)
        self.assertTrue(git(self.origin(project), 'rev-parse', '--verify', f'refs/heads/{branch}'))

    def test_remove_setup_worktree_accepts_a_branch_origin_already_deleted(self):
        # GitHub can delete a merged head branch itself.
        project, origin = self.project, self.origin(self.project)
        path, branch = setup_worktree(project, self.root)
        self.commit_in(path)
        git(path, 'push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}')
        self.merge_on_origin(project, branch)
        git(origin, 'update-ref', '-d', f'refs/heads/{branch}')
        remove_setup_worktree(project, path, branch, remote=True, root=self.root)
        self.assertFalse(path.exists())
        self.assertNotEqual(run(project, 'rev-parse', '--verify', f'refs/heads/{branch}').returncode, 0)

    def test_remote_delete_never_removes_a_newer_remote_tip(self):
        project, origin = self.project, self.origin(self.project)
        path, branch = setup_worktree(project, self.root)
        sha = self.commit_in(path)
        git(path, 'push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}')
        self.merge_on_origin(project, branch)
        newer = self.push_to_origin(project, 'later.txt', ref=f'refs/heads/{branch}')
        # Newer before the call: refused up front, nothing changed.
        before = self.refs(project)
        with self.assertRaisesRegex(ValueError, f'origin has a different {branch}'):
            remove_setup_worktree(project, path, branch, remote=True, root=self.root)
        self.assertEqual(self.refs(project), before)
        # Newer between the check and the delete: the lease refuses it.
        git(origin, 'update-ref', f'refs/heads/{branch}', sha)
        real, raced = subprocess.run, []

        def race(args, **kwargs):
            result = real(args, **kwargs)
            if args[3:4] == ['ls-remote'] and not raced:
                raced.append(real(['git', '-C', str(origin), 'update-ref', f'refs/heads/{branch}', newer]))
            return result
        with patch.object(operations.subprocess, 'run', side_effect=race), \
                self.assertRaises(ValueError) as caught:
            remove_setup_worktree(project, path, branch, remote=True, root=self.root)
        self.assertTrue(raced)
        self.assertIn(f'push origin --delete {branch}', str(caught.exception))
        self.assertNotIn('branch -D', str(caught.exception))
        self.assertEqual(git(origin, 'rev-parse', f'refs/heads/{branch}'), newer)
        self.assertNotEqual(run(project, 'rev-parse', '--verify', f'refs/heads/{branch}').returncode, 0)

    def test_remove_setup_worktree_refuses_any_other_branch(self):
        project = self.project
        path, branch = setup_worktree(project, self.root)
        git(project, 'branch', 'setup/devcontainer-2')
        # Real branches each refusal must leave alone (setup/devcontainer/extra cannot sit beside
        # setup/devcontainer, so it is refused by name only).
        for name in ('owner-work', 'feature/setup/devcontainer', 'setup/devcontainer-x', 'setup/devcontainer-0'):
            git(project, 'branch', name)
            git(project, 'push', '--quiet', 'origin', f'{name}:refs/heads/{name}')
        before = self.refs(project)
        invalid = ('main', 'owner-work', 'feature/setup/devcontainer', 'setup/devcontainer-x',
                   'setup/devcontainer-0', 'setup/devcontainer/extra')
        # Against the setup worktree and against a path under the root that is no worktree.
        cases = [(name, target) for name in invalid for target in (path, self.root / 'no-such-worktree')]
        cases.append(('setup/devcontainer-2', path))  # A valid name, but not the branch this worktree holds.
        for name, target in cases:
            with self.subTest(name=name, target=target), self.assertRaisesRegex(ValueError, 'setup/devcontainer'):
                remove_setup_worktree(project, target, name, remote=True, root=self.root)
        self.assertEqual(self.refs(project), before)
        self.assertTrue(path.exists())

    def test_remove_refuses_a_path_outside_the_root_before_any_git_call(self):
        project = self.project
        path, branch = setup_worktree(project, self.root)
        git(project, 'switch', '--quiet', '-c', 'setup/devcontainer-2')  # The owner's checkout on a setup name.
        before = self.refs(project)
        calls, recording = self.recorded()
        with recording:
            for target in (project, self.base / 'elsewhere', self.root, path / 'nested'):
                for remove in (lambda target: remove_worktree(project, target, 'setup/devcontainer-2', root=self.root),
                               lambda target: remove_setup_worktree(project, target, 'setup/devcontainer-2',
                                                                    remote=True, root=self.root)):
                    with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'setup worktrees'):
                        remove(target)
        self.assertEqual(calls, [])
        self.assertEqual(self.refs(project), before)
        self.assertTrue((project / 'app.txt').exists())

    def test_remove_setup_worktree_failure_keeps_state_and_names_the_commands(self):
        project = self.project
        path, branch = setup_worktree(project, self.root)
        cut = git(path, 'rev-parse', 'HEAD')
        git(path, 'push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}')
        (path / 'untracked.txt').write_text('not committed\n')
        with self.assertRaises(ValueError) as caught:
            remove_setup_worktree(project, path, branch, remote=True, root=self.root, cut=cut)
        message = str(caught.exception)
        for step in ('worktree remove --force', f'branch -D {branch}', f'push origin --delete {branch}'):
            self.assertIn(step, message)
        self.assertEqual((path / 'untracked.txt').read_text(), 'not committed\n')
        self.assertTrue(git(project, 'rev-parse', '--verify', f'refs/heads/{branch}'))
        # An origin that cannot be read is found before anything changes.
        (path / 'untracked.txt').unlink()
        reachable = self.origin(project)
        git(project, 'remote', 'set-url', 'origin', str(self.base / 'unreachable.git'))
        before = (git(project, 'worktree', 'list'), git(project, 'for-each-ref'), git(reachable, 'for-each-ref'))
        with self.assertRaisesRegex(ValueError, 'nothing was changed'):
            remove_setup_worktree(project, path, branch, remote=True, root=self.root, cut=cut)
        self.assertEqual((git(project, 'worktree', 'list'), git(project, 'for-each-ref'),
                          git(reachable, 'for-each-ref')), before)
        self.assertTrue(path.exists())

    def test_remove_worktree_keeps_the_setup_commit(self):
        path, branch = setup_worktree(self.project, self.root)
        sha = self.commit_in(path)
        remove_worktree(self.project, path, branch, root=self.root)
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(self.project, 'worktree', 'list'))
        self.assertEqual(git(self.project, 'rev-parse', f'refs/heads/{branch}'), sha)

    def test_failed_worktree_add_leaves_no_branch(self):
        self.root.mkdir(parents=True)
        self.root.chmod(0o500)
        self.addCleanup(self.root.chmod, 0o700)
        before = self.refs(self.project)
        with self.assertRaises(ValueError):
            setup_worktree(self.project, self.root)
        self.assertEqual(git(self.project, 'branch', '--list', 'setup/*'), '')
        self.assertEqual(self.refs(self.project), before)

    def test_setup_fetches_leave_fetch_head_alone(self):
        project = self.project
        fetch_head = project / '.git/FETCH_HEAD'
        fetch_head.write_text('owner fetch head\n')
        self.push_to_origin(project, 'remote.txt')
        path, branch = setup_worktree(project, self.root)
        self.assertTrue(fast_forward_checkout(project, 'main'))
        remove_setup_worktree(project, path, branch, remote=False, root=self.root)  # Fetches to check merged.
        self.assertEqual(fetch_head.read_text(), 'owner fetch head\n')

    def test_fetch_failure_without_origin_head_suggests_set_head(self):
        project, origin = self.project, self.origin(self.project)
        git(origin, 'branch', '-m', 'main', 'trunk')
        git(origin, 'symbolic-ref', 'HEAD', 'refs/heads/trunk')
        with self.assertRaisesRegex(ValueError, 'git remote set-head origin --auto'):
            setup_worktree(project, self.root)
        git(project, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/gone')
        with self.assertRaises(ValueError) as caught:
            setup_worktree(project, self.root)
        self.assertNotIn('set-head', str(caught.exception))
        # Once origin/HEAD names the real default, as set-head would, the cut works.
        git(project, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
        path, branch = setup_worktree(project, self.root)
        self.assertEqual(git(path, 'rev-parse', 'HEAD'), git(origin, 'rev-parse', 'trunk'))
        self.assertEqual(git(project, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/setup'), branch)

    def test_merged_setup_branches_skip_held_and_unmerged_ones(self):
        project = self.project
        merged_path, merged = setup_worktree(project, self.root)
        self.commit_in(merged_path)
        git(merged_path, 'push', '--quiet', 'origin', f'HEAD:refs/heads/{merged}')
        remove_worktree(project, merged_path, merged, root=self.root)
        sha = self.merge_on_origin(project, merged)
        held_path, held = setup_worktree(project, self.root)       # a leftover worktree, at the cut point
        kept_path, kept = setup_worktree(project, self.root)       # Commit only: a commit not on main
        self.commit_in(kept_path, 'kept.txt')
        remove_worktree(project, kept_path, kept, root=self.root)
        git(project, 'branch', 'setup/devcontainerX', 'main')      # not a setup name
        self.assertTrue(default_contains(project, 'main', sha))
        self.assertFalse(default_contains(project, 'main', git(project, 'rev-parse', kept)))
        self.assertEqual(merged_setup_branches(project, 'main'), [merged])
        self.assertTrue(held_path.exists())

    def test_fast_forward_only_when_clean_on_default(self):
        for case in ('clean', 'dirty', 'untracked', 'feature', 'detached', 'diverged', 'ignored'):
            with self.subTest(case=case):
                project = self.make_project(f'ff-{case}')
                if case == 'ignored':
                    (project / '.gitignore').write_text('local.env\n')
                    git(project, 'add', '.gitignore')
                    git(project, 'commit', '--quiet', '-m', 'Ignore local settings')
                    git(project, 'push', '--quiet', 'origin', 'main')
                incoming = self.push_to_origin(project, 'local.env' if case == 'ignored' else 'remote.txt',
                                               'tracked upstream\n')
                if case == 'dirty':
                    (project / 'app.txt').write_text('owner edit\n')
                elif case == 'untracked':
                    (project / 'notes.txt').write_text('owner notes\n')
                elif case == 'feature':
                    git(project, 'switch', '--quiet', '-c', 'feature/local')
                elif case == 'detached':
                    git(project, 'switch', '--quiet', '--detach')
                elif case == 'diverged':
                    (project / 'app.txt').write_text('local commit\n')
                    git(project, 'commit', '--quiet', '-am', 'Local only')
                elif case == 'ignored':
                    (project / 'local.env').write_text('PERSONAL_PAT=owner secret\n')
                before = self.snapshot(project)
                local_main = git(project, 'rev-parse', 'refs/heads/main')
                if case in ('clean', 'untracked'):
                    self.assertTrue(fast_forward_checkout(project, 'main'))
                    self.assertEqual(git(project, 'rev-parse', 'HEAD'), incoming)
                    if case == 'untracked':
                        self.assertEqual((project / 'notes.txt').read_text(), 'owner notes\n')
                        self.assertEqual(git(project, 'status', '--porcelain', '--untracked-files=all'), '?? notes.txt')
                    else:
                        self.assertEqual(git(project, 'status', '--porcelain', '--untracked-files=all'), '')
                    continue
                self.assertFalse(fast_forward_checkout(project, 'main'))
                self.assertEqual(self.snapshot(project), before)
                self.assertEqual(git(project, 'rev-parse', 'refs/heads/main'), local_main)
                if case == 'ignored':
                    self.assertEqual((project / 'local.env').read_text(), 'PERSONAL_PAT=owner secret\n')

    def checkout_update(self, project, default='main'):
        self.assertTrue(callable(getattr(operations, 'checkout_update', None)),
                        'checkout_update must return the checkout state and refusal reason')
        return operations.checkout_update(project, default)

    def test_untracked_notes_do_not_block_the_update(self):
        project = self.project
        incoming = self.push_to_origin(project, 'remote.txt')
        notes = project / 'notes.txt'
        notes.write_bytes(b'owner notes\n\x00keep these bytes\xff')
        before = notes.read_bytes()
        result = self.checkout_update(project)
        self.assertEqual(result, {'state': 'updated', 'reason': None,
                                  'head': git(project, 'rev-parse', '--short', incoming), 'behind': 0})
        self.assertEqual(git(project, 'rev-parse', 'HEAD'), incoming)
        self.assertEqual(notes.read_bytes(), before)
        self.assertTrue(fast_forward_checkout(project, 'main'))

    def test_fast_forward_reports_each_refusal_reason(self):
        cases = {'unstaged': 'tracked-changes', 'staged': 'tracked-changes',
                 'deletion': 'tracked-changes', 'rename': 'tracked-changes',
                 'untracked': 'collision', 'directory': 'collision', 'ignored': 'collision',
                 'feature': 'other-branch', 'detached': 'detached', 'diverged': 'diverged',
                 'ahead': 'diverged', 'fetch': 'fetch', 'invalid-default': 'fetch'}
        for case, reason in cases.items():
            with self.subTest(case=case):
                project = self.make_project(f'refusal-{case}')
                if case == 'ignored':
                    (project / '.gitignore').write_text('incoming.txt\n')
                    git(project, 'add', '.gitignore')
                    git(project, 'commit', '--quiet', '-m', 'Ignore local file')
                    git(project, 'push', '--quiet', 'origin', 'main')
                if case != 'ahead':
                    self.push_to_origin(project, 'incoming.txt')
                if case in ('unstaged', 'staged', 'diverged', 'ahead'):
                    (project / 'app.txt').write_bytes(b'owner edit\n')
                    if case == 'staged':
                        git(project, 'add', 'app.txt')
                    elif case in ('diverged', 'ahead'):
                        git(project, 'commit', '--quiet', '-am', 'Local work')
                elif case == 'deletion':
                    (project / 'app.txt').unlink()
                elif case == 'rename':
                    git(project, 'mv', 'app.txt', 'renamed.txt')
                elif case in ('untracked', 'ignored'):
                    (project / 'incoming.txt').write_bytes(b'owner bytes\n\xff')
                elif case == 'directory':
                    (project / 'incoming.txt').mkdir()
                    (project / 'incoming.txt/notes').write_bytes(b'owner bytes\n')
                elif case == 'feature':
                    git(project, 'switch', '--quiet', '-c', 'feature/local')
                elif case == 'detached':
                    git(project, 'switch', '--quiet', '--detach')
                elif case == 'fetch':
                    git(project, 'remote', 'set-url', 'origin', str(self.base / 'unreachable.git'))
                before = self.snapshot(project)
                result = self.checkout_update(project, '--invalid' if case == 'invalid-default' else 'main')
                self.assertEqual((result['state'], result['reason']), ('refused', reason))
                self.assertEqual(result['head'], git(project, 'rev-parse', '--short', 'HEAD'))
                expected_behind = 0 if case == 'ahead' else 1 if reason in ('collision', 'diverged') else None
                self.assertEqual(result['behind'], expected_behind)
                self.assertEqual(self.snapshot(project), before)
                self.assertFalse(fast_forward_checkout(project, 'main' if case != 'invalid-default' else '--invalid'))
                self.assertEqual(self.snapshot(project), before)

    def test_fast_forward_reports_other_merge_failures(self):
        project = self.project
        self.push_to_origin(project, 'incoming.txt')
        lock = project / '.git/index.lock'
        lock.write_bytes(b'held by another git process\n')
        before = self.snapshot(project)
        result = self.checkout_update(project)
        self.assertEqual((result['state'], result['reason']), ('refused', 'merge-failed'))
        self.assertIn('index.lock', result['detail'])
        self.assertEqual(len(result['detail'].splitlines()), 1)
        self.assertLessEqual(len(result['detail']), 200)
        self.assertEqual(result['behind'], 1)
        self.assertEqual(self.snapshot(project), before)
        self.assertEqual(lock.read_bytes(), b'held by another git process\n')

    def test_checkout_reports_current_without_mutating_notes(self):
        project = self.project
        (project / 'notes.txt').write_bytes(b'untracked notes\n')
        before = self.snapshot(project)
        self.assertEqual(self.checkout_update(project), {'state': 'current', 'reason': None,
                         'head': git(project, 'rev-parse', '--short', 'HEAD'), 'behind': 0})
        self.assertEqual(self.snapshot(project), before)


if __name__ == '__main__':
    unittest.main()
