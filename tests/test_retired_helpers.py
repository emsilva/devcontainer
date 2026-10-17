"""Upgrade the previous producer's helpers without discarding owner changes."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from devcontainer_setup.__main__ import check_candidate, integrate_continuity, integrate_taskfile
from devcontainer_setup.git_ops import GitPlan, ReviewChanged
from devcontainer_setup.render import CONFIG, NAMESPACE, RETIRED_HELPERS, render, validate


SOURCE = Path(__file__).resolve().parents[1]
REF = 'b' * 40


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


class RetiredHelpersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name).resolve()
        self.root, self.source = directory / 'consumer', directory / 'producer'
        self.root.mkdir()
        for name in ('.devcontainer', 'templates', 'devcontainer_setup'):
            shutil.copytree(SOURCE / name, self.source / name,
                            ignore=shutil.ignore_patterns('__pycache__'))
        # Test a completed producer retirement even while other workers are
        # still extracting native functionality from the old source modules.
        for name in RETIRED_HELPERS:
            (self.source / 'devcontainer_setup/project_sync' / Path(name).name).unlink(missing_ok=True)
        self.selected = dict(runtimes={}, tools=[], agents=True, dotfiles=False, ssh=True,
                             ports=[], package_managers={}, polyglot=False, continuity=True)
        files = render(self.root, self.source, self.selected, 'a' * 40)
        config = json.loads(files[CONFIG])
        self.old_contents = {}
        for index, name in enumerate(sorted(RETIRED_HELPERS)):
            content = f'# previous generated {index}\n'.encode()
            files[name] = self.old_contents[name] = content
            config['customizations'][NAMESPACE]['managedFiles'][name] = hashlib.sha256(content).hexdigest()
        self.retained = '.devcontainer/scripts/previous-still-needed.py'
        files[self.retained] = b'# retained unrelated producer helper\n'
        config['customizations'][NAMESPACE]['managedFiles'][self.retained] = hashlib.sha256(files[self.retained]).hexdigest()
        files[CONFIG] = json.dumps(config).encode()
        self.custom = '  "work:codex:cs":\n    cmds: ["echo owner shortcut"]\n'
        legacy = ('  agent:handoff:\n    interactive: true\n    cmds:\n'
                  '      - task: devcontainer:agent:handoff\n'
                  '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n')
        files['Taskfile.yml'] = ("version: '3'\ntasks:\n" + self.custom + legacy
                                 + '  up:\n    cmds: ["echo owner stack"]\n').encode()
        self.install(files)
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.name', 'Test User')
        git(self.root, 'config', 'user.email', 'test@example.com')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'Previous generated setup')

    def install(self, files):
        for name, content in files.items():
            path = self.root / name
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

    def candidate(self):
        files = render(self.root, self.source, self.selected, REF)
        integrate_taskfile(self.root, files)
        integrate_continuity(self.root, files, True)
        return files

    def test_upgrade_reviews_only_owned_retirements_and_preserves_other_helpers(self):
        files = self.candidate()
        self.assertEqual({name for name, data in files.items() if data is None}, RETIRED_HELPERS)
        registered = json.loads(files[CONFIG])['customizations'][NAMESPACE]['managedFiles']
        self.assertFalse(RETIRED_HELPERS & registered.keys())
        self.assertIn(self.retained, registered)
        self.assertNotIn(self.retained, files)
        check_candidate(files, self.root)
        for name, content in self.old_contents.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_disk_index_and_commit_remove_helpers_without_owner_edits(self):
        owner = self.root / 'owner.txt'
        owner.write_text('uncommitted project work\n')
        files = self.candidate()
        plan = GitPlan(self.root, files)
        self.assertEqual(plan.diff().count('+++ /dev/null'), len(RETIRED_HELPERS))
        plan.apply()
        self.assertTrue(all(not (self.root / name).exists() for name in RETIRED_HELPERS))
        indexed = set(git(self.root, 'ls-files').splitlines())
        self.assertFalse(RETIRED_HELPERS & indexed)
        sha = plan.commit()
        tree = set(git(self.root, 'ls-tree', '-r', '--name-only', sha).splitlines())
        self.assertFalse(RETIRED_HELPERS & tree)
        self.assertIn(self.retained, tree)
        self.assertEqual(owner.read_text(), 'uncommitted project work\n')
        self.assertEqual(validate(self.root), [])

    def test_repeated_upgrade_has_no_more_deletions_or_owned_root_drift(self):
        first = self.candidate()
        self.install(first)
        again = self.candidate()
        self.assertFalse(any(data is None for data in again.values()))
        self.assertEqual(again[CONFIG], first[CONFIG])
        self.assertEqual(again['Taskfile.yml'], first['Taskfile.yml'])
        check_candidate(again, self.root)

    def test_edited_retired_helper_refuses_before_any_candidate_write(self):
        name = sorted(RETIRED_HELPERS)[0]
        path = self.root / name
        edited = path.read_bytes() + b'# owner patch\n'
        path.write_bytes(edited)
        before = (self.root / CONFIG).read_bytes()
        with self.assertRaisesRegex(ValueError, 'Modified or missing managed file'):
            self.candidate()
        self.assertEqual(path.read_bytes(), edited)
        self.assertEqual((self.root / CONFIG).read_bytes(), before)

    def test_symlink_retired_helper_is_refused_without_following_target(self):
        name = sorted(RETIRED_HELPERS)[0]
        path = self.root / name
        outside = self.root.parent / 'outside.py'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.candidate()
        self.assertEqual(outside.read_bytes(), self.old_contents[name])
        self.assertTrue(path.is_symlink())

    def test_unregistered_retired_name_is_preserved_as_owner_content(self):
        name = sorted(RETIRED_HELPERS)[0]
        config = json.loads((self.root / CONFIG).read_bytes())
        del config['customizations'][NAMESPACE]['managedFiles'][name]
        (self.root / CONFIG).write_text(json.dumps(config))
        files = self.candidate()
        self.assertNotIn(name, files)
        self.assertEqual((self.root / name).read_bytes(), self.old_contents[name])

    def test_legacy_delegates_removed_but_custom_and_stack_tasks_preserved(self):
        files = self.candidate()
        text = files['Taskfile.yml'].decode()
        self.assertIn(self.custom, text)
        self.assertIn('cmds: ["echo owner stack"]', text)
        self.assertNotIn('devcontainer:agent:handoff', text)
        self.assertIn('devcontainer:work:cs', text)
        self.assertIn('devcontainer-setup:work-tasks begin sha256=', text)

    def test_recreated_retired_helper_stops_commit_and_cannot_be_accepted(self):
        name = sorted(RETIRED_HELPERS)[0]
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text(f'#!/bin/sh\nprintf owner-resurrection > {name}\ngit add -- {name}\n')
        hook.chmod(0o755)
        head = git(self.root, 'rev-parse', 'HEAD')
        plan = GitPlan(self.root, self.candidate())
        plan.apply()
        with self.assertRaises(ReviewChanged):
            plan.commit()
        with self.assertRaisesRegex(ValueError, 'recreated'):
            plan.accept_hook_changes()
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.root / name).read_text(), 'owner-resurrection')


if __name__ == '__main__':
    unittest.main()
