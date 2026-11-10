"""Retirement is replayable at every durable boundary, with fixture state only."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync import arming, keeper, scratch, standing, state, workspace


class RetireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'repo'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'remote', 'add', 'origin', 'https://github.com/owner/project.git'], check=True)
        (self.root / '.scratch').mkdir()
        self.project = state.discover_project(self.root)
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.base / 'state'))
        self.state = state.state_dir(self.project, self.env)
        self.binding = dict(version=1, identity=self.project.identity, root=str(self.root),
                            codespace='cs-old', remote_root='/workspaces/repo')
        self.registry = dict(schema=1, project_id=self.project.project_id, identity=self.project.identity,
                             local_root=str(self.root), local_active_root=str(self.root), active_host='codespace',
                             codespace='cs-old', remote_root='/workspaces/repo', remote_active_root='/workspaces/older',
                             verified_local_historical_roots=['/local/history'],
                             verified_remote_historical_roots=['/workspaces/older'],
                             verified_historical_roots=['/local/history', '/workspaces/older'], extension='keep')
        self.reset()
        for module in (scratch, keeper):
            fake = patch.object(module, 'status', return_value={'running': False, 'phase': 'stopped'})
            fake.start()
            self.addCleanup(fake.stop)

    def reset(self):
        for name, value in (('scratch.json', self.binding), ('scratch-session.json', {'name': 'retired-session'}),
                            ('workspace.json', self.registry), ('arming.json', {'schema': 1, 'armed': True,
                             'gh_user': 'fixture', 'codespace': 'cs-old'})):
            state.atomic_write(self.state / name, json.dumps(value).encode() + b'\n')

    def snapshot(self):
        return {p.relative_to(self.state).as_posix(): p.read_bytes() for p in self.state.rglob('*')
                if p.is_file() and not p.name.endswith('.lock')}

    def assert_retired(self, archive):
        for name, original in (('scratch.json', self.binding), ('scratch-session.json', {'name': 'retired-session'}),
                               ('workspace.json', self.registry)):
            self.assertEqual(state.read_json(archive / name), original)
        self.assertEqual(state.read_json(archive / 'deleted.json')['codespace'], 'cs-old')
        self.assertFalse((self.state / 'scratch.json').exists())
        self.assertFalse((self.state / 'scratch-session.json').exists())
        self.assertFalse((self.state / 'retire-intent.json').exists())
        value = workspace.registry(self.project, self.state)
        self.assertEqual(value['active_host'], 'local')
        self.assertEqual(value['local_active_root'], str(self.root))
        self.assertEqual(value['verified_historical_roots'], ['/local/history'])
        self.assertEqual(value['verified_local_historical_roots'], ['/local/history'])
        self.assertEqual(value['extension'], 'keep')
        for name in ('codespace', 'remote_root', 'remote_active_root', 'verified_remote_historical_roots'):
            self.assertNotIn(name, value)

    def test_retire_archives_then_clears_the_pairing_and_keeps_local_history(self):
        before = (self.state / 'arming.json').read_bytes()
        archive = workspace.retire(self.project, self.state, 'cs-old')
        self.assert_retired(archive)
        self.assertEqual((self.state / 'arming.json').read_bytes(), before)
        metadata = self.root / '.devcontainer/devcontainer.json'
        metadata.parent.mkdir()
        metadata.write_text(json.dumps({'customizations': {'devcontainerSetup': {'sourceRef': 'a' * 40}}}))
        with patch.object(standing, '__file__', str(self.root / '.devcontainer/scripts/project_sync/standing.py')):
            local = standing.read(self.root, env=self.env)
        self.assertEqual(local.setup, 'installed')
        self.assertIsNone(local.pairing)

    def test_retire_refuses_another_codespace_or_a_running_worker(self):
        for target, running in (('cs-new', None), ('cs-old', scratch), ('cs-old', keeper)):
            with self.subTest(target=target, running=running), ExitStack() as stack:
                if running:
                    stack.enter_context(patch.object(running, 'status', return_value={'running': True}))
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'stop the keeper and sync worker first|no pairing for cs-new to retire'):
                    workspace.retire(self.project, self.state, target)
                self.assertEqual(self.snapshot(), before)

    def test_invalid_registry_is_refused_before_the_intent(self):
        state.atomic_write(self.state / 'workspace.json', b'[]\n')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'the pairing changed since the delete began'):
            workspace.retire(self.project, self.state, 'cs-old')
        self.assertEqual(self.snapshot(), before)

    def crash_after(self, boundary):
        write, unlink = workspace.atomic_write, Path.unlink
        count = [0]
        def tick():
            count[0] += 1
            if count[0] == boundary:
                raise OSError('fixture crash')
        def written(*args, **kwargs):
            result = write(*args, **kwargs)
            tick()
            return result
        def unlinked(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            tick()
            return result
        with patch.object(workspace, 'atomic_write', side_effect=written), patch.object(Path, 'unlink', unlinked):
            with self.assertRaisesRegex(OSError, 'fixture crash'):
                workspace.retire(self.project, self.state, 'cs-old')

    def test_retire_replays_after_each_step(self):
        # intent, three archived inputs, deletion marker, registry rewrite, two unlinks, intent unlink
        initial = self.state
        for boundary in range(1, 10):
            with self.subTest(boundary=boundary):
                self.state = initial / f'crash-{boundary}'
                self.reset()
                self.crash_after(boundary)
                archive = workspace.retire(self.project, self.state, 'cs-old')
                self.assert_retired(archive)

    def test_finished_retire_is_found_again(self):
        with state.locked(self.state / 'workspace-control.lock', blocking=True):
            archive = workspace.retire(self.project, self.state, 'cs-old', held=True)
        before = self.snapshot()
        self.assertEqual(workspace.retire(self.project, self.state, 'cs-old'), archive)
        self.assertEqual(self.snapshot(), before)

    def test_retire_replay_accepts_its_own_successor_states(self):
        self.crash_after(6)
        after = (self.state / 'workspace.json').read_bytes()
        archive = workspace.retire(self.project, self.state, 'cs-old')
        self.assert_retired(archive)
        self.assertEqual((self.state / 'workspace.json').read_bytes(), after)

    def test_a_start_during_retirement_waits_and_then_refuses(self):
        reached, resume = threading.Event(), threading.Event()
        errors, starts = [], []
        write = workspace.atomic_write
        def barrier(path, *args, **kwargs):
            if Path(path).name == 'retire-intent.json':
                reached.set()
                if not resume.wait(5):
                    raise RuntimeError('fixture barrier timed out')
            return write(path, *args, **kwargs)
        def retire():
            try:
                workspace.retire(self.project, self.state, 'cs-old')
            except BaseException as error:
                errors.append(error)
        def start(module):
            try:
                module.start(self.root, self.state)
                starts.append('launched')
            except ValueError:
                starts.append('refused')
            except BaseException as error:
                errors.append(error)
        with patch.object(workspace, 'atomic_write', side_effect=barrier), \
                patch.object(scratch, 'install_mutagen'), patch.object(state, 'launch_detached') as worker, \
                patch.object(keeper, 'launch_detached') as watcher:
            retiring = threading.Thread(target=retire)
            retiring.start()
            self.assertTrue(reached.wait(2))
            threads = [threading.Thread(target=start, args=(module,)) for module in (scratch, keeper)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(.1)
            waited = all(thread.is_alive() for thread in threads)
            resume.set()
            for thread in [retiring, *threads]:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(waited)
        self.assertEqual(starts, ['refused', 'refused'])
        worker.assert_not_called()
        watcher.assert_not_called()

    def test_replay_never_touches_a_newer_pairing(self):
        initial = self.state
        for name in ('scratch.json', 'scratch-session.json', 'workspace.json', 'archive'):
            with self.subTest(name=name):
                self.state = initial / name.replace('.', '-')
                self.reset()
                self.crash_after(6)
                target = self.state / name
                if name == 'archive':
                    target = next((self.state / 'retired-bindings').glob('*/scratch.json'))
                state.atomic_write(target, b'{"codespace":"cs-new"}\n')
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'the pairing changed since the delete began'):
                    workspace.retire(self.project, self.state, 'cs-old')
                self.assertEqual(self.snapshot(), before)

    def test_superseded_intent_is_archived_without_blocking_the_next_retire(self):
        initial = self.state
        for boundary in (1, 4, 6, 8):
            with self.subTest(boundary=boundary):
                self.state = initial / ('superseded-' + str(boundary))
                self.reset()
                self.crash_after(boundary)
                intent = (self.state / 'retire-intent.json').read_bytes()
                new_binding = dict(self.binding, codespace='cs-new', remote_root='/workspaces/new')
                new_registry = dict(self.registry, codespace='cs-new', remote_root='/workspaces/new',
                                    remote_active_root='/workspaces/new')
                for name, value in (('scratch.json', new_binding), ('workspace.json', new_registry),
                                    ('scratch-session.json', dict(name='new-session'))):
                    state.atomic_write(self.state / name, json.dumps(value).encode() + b'\n')
                before = {name: (self.state / name).read_bytes() for name in
                          ('scratch.json', 'workspace.json', 'scratch-session.json', 'arming.json')}
                with self.assertRaisesRegex(ValueError, 'retirement was superseded by a newer pairing'):
                    workspace.retire(self.project, self.state, 'cs-old')
                self.assertFalse((self.state / 'retire-intent.json').exists())
                archived = next((self.state / 'retired-bindings').glob('*/superseded-retire-intent.json'))
                self.assertEqual(archived.read_bytes(), intent)
                self.assertEqual({name: (self.state / name).read_bytes() for name in before}, before)
                archive = workspace.retire(self.project, self.state, 'cs-new')
                self.assertEqual(state.read_json(archive / 'deleted.json')['codespace'], 'cs-new')
                self.assertFalse((self.state / 'scratch.json').exists())

    def test_a_new_binding_with_the_old_registry_does_not_supersede_retirement(self):
        self.crash_after(1)
        # A crashed cutover can publish NEW's binding before its registry. Matching
        # roots alone cannot authorize archiving OLD's unfinished retirement.
        newer = dict(self.binding, codespace='cs-new')
        state.atomic_write(self.state / scratch.CONFIG, json.dumps(newer).encode() + b'\n')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'the pairing changed since the delete began'):
            workspace.retire(self.project, self.state, 'cs-old')
        self.assertEqual(self.snapshot(), before)
