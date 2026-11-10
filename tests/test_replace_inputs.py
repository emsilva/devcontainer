import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from devcontainer_setup.render import render, validate


SOURCE = Path(__file__).resolve().parents[1]


class ReplaceInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        environment = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'state')})
        environment.start()
        self.addCleanup(environment.stop)
        self.body = {'name': 'old-space', 'repository': {'full_name': 'owner/project'},
                     'machine': {'name': 'premiumLinux', 'cpus': 8}, 'idle_timeout_minutes': 120,
                     'billable_owner': {'login': 'owner'}}

    def inputs(self):
        self.assertIsNotNone(importlib.util.find_spec('devcontainer_setup.replace_inputs'),
                             'replacement inspection primitives are not implemented')
        return importlib.import_module('devcontainer_setup.replace_inputs')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.project), *args], text=True).strip()

    def install(self, files):
        for name, content in files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def test_premium_120_minutes_is_read_from_the_live_body(self):
        inputs = self.inputs()
        self.assertEqual(inputs.read_resources(self.body, 'OWNER/PROJECT'),
                         inputs.Resources('premiumLinux', 8, 120, 'owner'))

    def test_each_missing_field_is_named(self):
        inputs = self.inputs()
        mutations = [('machine', 'machine', value) for value in (None, {}, {'name': ''}, 'premiumLinux')]
        mutations += [('core count', 'cpus', value) for value in (None, 0, '8', True, -1)]
        mutations += [('idle timeout', 'idle_timeout_minutes', value) for value in (None, 0, '120', True, -1)]
        mutations += [('billing owner', 'billable_owner', value) for value in (None, {}, {'login': ''}, 'owner')]
        for field, key, value in mutations:
            with self.subTest(field=field, value=value):
                body = copy.deepcopy(self.body)
                target = body['machine'] if key == 'cpus' else body
                target[key] = value
                with self.assertRaises(ValueError) as caught:
                    inputs.read_resources(body, 'owner/project')
                self.assertEqual(str(caught.exception),
                                 f"old-space's {field} cannot be read from GitHub; nothing was created.")
                target.pop(key)
                with self.assertRaisesRegex(ValueError, field):
                    inputs.read_resources(body, 'owner/project')

    def test_another_repository_is_refused(self):
        inputs = self.inputs()
        self.body['repository']['full_name'] = 'owner/other'
        with self.assertRaises(ValueError) as caught:
            inputs.read_resources(self.body, 'owner/project')
        self.assertEqual(str(caught.exception), 'old-space belongs to another repository; nothing was created.')

    def test_offered_needs_the_same_cores(self):
        inputs = self.inputs()
        resources = inputs.read_resources(self.body, 'owner/project')
        self.assertTrue(inputs.offered([{'name': 'premiumLinux', 'cpus': 8}], resources))
        for machines in ([], [{'name': 'premiumLinux', 'cpus': 4}], [{'name': 'other', 'cpus': 8}],
                         [{'name': 'premiumLinux', 'cpus': '8'}]):
            with self.subTest(machines=machines):
                self.assertFalse(inputs.offered(machines, resources))

    def test_pin_reads_the_fetched_tip(self):
        inputs = self.inputs()
        origin = self.root / 'origin.git'
        subprocess.check_call(['git', 'init', '--quiet', '--bare', '-b', 'main', str(origin)])
        self.git('init', '--quiet', '-b', 'main')
        self.git('config', 'user.name', 'Test User')
        self.git('config', 'user.email', 'test@example.com')
        (self.project / 'file').write_text('first\n')
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'Initial')
        self.git('remote', 'add', 'origin', str(origin))
        self.git('push', '--quiet', '-u', 'origin', 'main')
        clone = self.root / 'clone'
        subprocess.check_call(['git', 'clone', '--quiet', str(origin), str(clone)])
        old = self.git('rev-parse', 'HEAD')
        (self.project / 'file').write_text('second\n')
        self.git('commit', '--quiet', '-am', 'Advance')
        self.git('push', '--quiet', 'origin', 'main')
        tip = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(tip, old)
        fetch_head = clone / '.git/FETCH_HEAD'
        before = fetch_head.read_bytes() if fetch_head.exists() else None
        self.assertEqual(inputs.pin(clone, 'main'), tip)
        self.assertEqual(fetch_head.read_bytes() if fetch_head.exists() else None, before)
        with self.assertRaisesRegex(ValueError, 'could not read missing from GitHub'):
            inputs.pin(clone, 'missing')

    def test_setup_differs_uses_the_renderers_rules(self):
        inputs = self.inputs()
        self.git('init', '--quiet', '-b', 'main')
        self.git('config', 'user.name', 'Test User')
        self.git('config', 'user.email', 'test@example.com')
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        selected = dict(runtimes={'python': '3.13'}, tools=[], agents=False, dotfiles=False,
                        ssh=False, ports=[], package_managers={}, polyglot=True)
        files = render(self.project, SOURCE, selected, 'a' * 40)
        # The renderer permits the Dev Containers CLI to normalize a legacy lock without changing its marker.
        config_name, lock_name = '.devcontainer/devcontainer.json', '.devcontainer/devcontainer-lock.json'
        config, lock = json.loads(files[config_name]), json.loads(files[lock_name])
        legacy = (json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode()
        config['customizations']['devcontainerSetup']['managedFiles'][lock_name] = hashlib.sha256(legacy).hexdigest()
        files.update({config_name: json.dumps(config).encode(), lock_name: legacy})
        self.install(files)
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'Rendered setup')
        baseline = self.git('rev-parse', 'HEAD')
        worktrees = self.git('worktree', 'list', '--porcelain')
        self.assertIsNone(inputs.setup_differs(self.project, baseline))
        # A preexisting detached sibling belongs to another operation and must survive all checks.
        sibling = self.root / 'sibling'
        self.git('worktree', 'add', '--quiet', '--detach', str(sibling), baseline)
        self.addCleanup(lambda: self.git('worktree', 'remove', '--force', str(sibling)))

        for change in ('command', 'version', 'source', 'selections', 'user', 'native-lock'):
            with self.subTest(change=change):
                self.install(files)
                modified = json.loads(files[config_name])
                if change == 'command':
                    modified['postCreateCommand'] = {'project': 'echo mine'}
                elif change == 'version':
                    modified['features']['ghcr.io/devcontainers/features/python:1']['version'] = '3.9'
                elif change == 'source':
                    modified['customizations']['devcontainerSetup']['sourceRef'] = 'b' * 40
                elif change == 'selections':
                    modified['customizations']['devcontainerSetup']['selections']['ports'] = [8080]
                elif change == 'user':
                    modified['customizations']['vscode'] = {'settings': {'editor.wordWrap': 'on'}}
                (self.project / config_name).write_text(json.dumps(modified))
                if change == 'native-lock':
                    (self.project / lock_name).write_bytes(render(self.project, SOURCE, selected, 'a' * 40)[lock_name])
                errors = validate(self.project)
                self.git('add', '.')
                self.git('commit', '--quiet', '-m', f'Fixture {change}')
                tip = self.git('rev-parse', 'HEAD')
                self.install(files)
                reason = inputs.setup_differs(self.project, tip)
                expected = (errors[0] if change in ('command', 'version') else
                            'a different generator version' if change == 'source' else
                            'different selections' if change == 'selections' else None)
                self.assertEqual(reason, expected)
                self.assertTrue(sibling.is_dir())
                self.assertEqual(len([line for line in self.git('worktree', 'list', '--porcelain').splitlines()
                                      if line.startswith('worktree ')]), 2)

        # Local managed changes must also refuse even when the pinned tree is valid.
        (self.project / '.devcontainer/scripts/validate-selected.sh').write_text('changed\n')
        self.assertEqual(inputs.setup_differs(self.project, baseline), validate(self.project)[0])
        self.assertIn('Modified managed helper/file', inputs.setup_differs(self.project, baseline))
        self.assertIn(str(self.project), worktrees)

    def test_setup_differs_cleans_a_worktree_after_a_checkout_hook_fails(self):
        inputs = self.inputs()
        self.git('init', '--quiet', '-b', 'main')
        self.git('config', 'user.name', 'Test User')
        self.git('config', 'user.email', 'test@example.com')
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        self.install(render(self.project, SOURCE, {}, 'a' * 40))
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'Rendered setup')
        hook = self.project / '.git/hooks/post-checkout'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o700)
        for state in (self.root / 'state', self.root / 'state\nquoted'):
            with self.subTest(state=state), patch.dict(os.environ, {'XDG_STATE_HOME': str(state)}):
                with self.assertRaises(ValueError):
                    inputs.setup_differs(self.project, self.git('rev-parse', 'HEAD'))
                listed = self.git('worktree', 'list', '--porcelain')
                self.assertEqual(sum(line.startswith('worktree ') for line in listed.splitlines()), 1)
                self.assertFalse(list((state / 'devcontainer-setup/setup-worktrees').glob('*/replace-inspect-*')))
