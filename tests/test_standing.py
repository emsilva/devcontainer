"""Offline local facts, pinned to the setup producers they mirror."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from devcontainer_setup import __main__ as setup, codespaces, git_ops, root_tasks
from devcontainer_setup.project_sync import arming, keeper, power, state, sync_cycle


class StandingTests(unittest.TestCase):
    def setUp(self):
        # Import per test so every missing-feature failure has its own receipt.
        self.standing = importlib.import_module('devcontainer_setup.project_sync.standing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.env = dict(os.environ, HOME=str(self.base / 'home'),
                        XDG_STATE_HOME=str(self.base / 'state'), XDG_CACHE_HOME=str(self.base / 'cache'))
        self.env.pop('CODESPACES', None)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'core.hooksPath', '/dev/null')
        self.git('remote', 'add', 'origin', 'https://github.com/Example/Project.git')
        self.commit('initial')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
        self.project = state.discover_project(self.root)
        self.state = state.state_dir(self.project, self.env)
        self.generated = patch.object(self.standing, '__file__',
                                      str(self.root / '.devcontainer/scripts/project_sync/standing.py'))
        self.generated.start()
        self.addCleanup(self.generated.stop)
        self.identity = patch.object(state, 'process_identity', return_value=None)
        self.identity.start()
        self.addCleanup(self.identity.stop)

    def git(self, *args, root=None, env=None):
        return subprocess.run(['git', '-C', str(root or self.root), *args], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, text):
        (self.root / 'file').write_text(text)
        self.git('add', 'file')
        self.git('commit', '--allow-empty', '-qm', text)
        return self.git('rev-parse', 'HEAD')

    def write(self, name, value):
        state.atomic_write(self.state / name, json.dumps(value).encode())

    def bind(self, name='new-cs'):
        self.binding = dict(schema=1, codespace=name, identity=self.project.identity,
                            root=str(self.root), remote_root='/workspaces/project')
        self.write('scratch.json', self.binding)
        return self.binding

    def metadata(self, value=None):
        path = self.root / '.devcontainer/devcontainer.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value if value is not None else {
            'customizations': {'devcontainerSetup': {'sourceRef': 'a' * 40}}}))
        return path

    def read(self, **kwargs):
        return self.standing.read(self.root, env=self.env, **kwargs)

    def live_keeper(self, **extra):
        value = dict(dict(pid=4242, nonce='n' * 48, codespace='new-cs', gh_user='fixture',
                          heartbeat_at=time.time()), **extra)
        value['process_identity'] = f'fixture .keeper --keep {self.state} ' + value['nonce']
        self.write(keeper.RUNTIME, value)
        state.process_identity.return_value = value['process_identity']
        return value

    def cycle(self):
        self.bind()
        cycle = sync_cycle.begin(self.project, self.binding['remote_root'])
        cycle['completed_at'] = 1000.0
        row = cycle['directions']['desktop_to_codespace']
        row.update(snapshot_id='a' * 64, receipt={'snapshot_id': 'a' * 64,
                   'received_at': '2026-11-08T12:00:00+00:00'},
                   publication_issues=[dict(agent='codex', session_id='session-1', reason='refused')])
        self.runtime = dict(binding=self.binding, conversations=cycle)
        self.write('scratch-runtime.json', self.runtime)
        return cycle, row

    def snapshot(self):
        # Include Git's index and refs, state and cache; reading may not change any bytes.
        return {str(p.relative_to(self.base)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.base.rglob('*') if p.is_file()}

    def test_reading_creates_and_changes_nothing(self):
        before = self.snapshot()
        self.read()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.state.exists())
        self.bind()
        self.cycle()
        self.metadata()
        (Path(self.env['XDG_CACHE_HOME'])).mkdir()
        (Path(self.env['XDG_CACHE_HOME']) / 'sentinel').write_text('keep')
        before = self.snapshot()
        self.read()
        self.assertEqual(self.snapshot(), before)

    def test_setup_states(self):
        self.assertEqual(self.read().setup, 'missing')
        path = self.metadata({})
        self.assertEqual(self.read().setup, 'unmanaged')
        for bad in ('{', '[]', '{"customizations": []}',
                    '{"customizations":{"devcontainerSetup":{"sourceRef":"bad"}}}'):
            path.write_text(bad)
            self.assertEqual(self.read().setup, 'unknown')
        self.metadata()
        self.assertEqual(self.read().setup, 'partial')
        for name in ('workspace.json', 'scratch.json'):
            self.write(name, {})
            self.assertEqual(self.read().setup, 'installed')
            (self.state / name).unlink()
        with patch.object(self.standing, '__file__', str(self.base / 'source/standing.py')):
            self.assertEqual(self.read().setup, 'source')

    def test_records_for_another_codespace_are_ignored(self):
        self.bind()
        self.live_keeper(codespace='old-cs', reading={'ok': True, 'state': 'Shutdown', 'at': 99},
                         last_up_at=88, down={'kind': 'OWNER', 'first_at': 99})
        self.write('arming.json', dict(schema=1, armed=True, codespace='old-cs', gh_user='old',
                                      owner_stop_codespace='old-cs', owner_stop_at=90, hold_until=100))
        cycle, _ = self.cycle()
        self.runtime['binding'] = dict(self.binding, codespace='old-cs')
        self.write('scratch-runtime.json', self.runtime)
        local = self.read()
        for field in ('reading', 'owner_stop_at', 'hold_until', 'gh_user', 'last_up_at', 'cycle_at', 'down_kind'):
            self.assertIsNone(getattr(local, field), field)
        self.assertEqual(local.refusals, ())
        self.assertFalse(local.armed)
        self.assertEqual(local.keeper, 'stopped')

    def test_unarmed_owner_stop_names_its_codespace(self):
        self.bind()
        api = Mock()
        api.post_stop.return_value = 202
        with patch.dict(os.environ, {'CODESPACES': '', 'GH_TOKEN': '', 'GITHUB_TOKEN': ''}):
            power.stop(self.state, api=api, now=lambda: 90.0)
        saved = arming.read(self.state)
        self.assertEqual(saved['owner_stop_codespace'], 'new-cs')
        self.assertEqual(self.read().owner_stop_at, 90.0)
        self.assertFalse(self.read().armed)

    def test_legacy_owner_stop_needs_a_matching_armed_target(self):
        self.bind()
        for value, want in ((dict(schema=1, armed=False, owner_stop_at=90), None),
                            (dict(schema=1, armed=True, codespace='new-cs', owner_stop_at=90), 90.0),
                            (dict(schema=1, armed=True, codespace='old-cs', owner_stop_at=90), None)):
            self.write('arming.json', value)
            self.assertEqual(self.read().owner_stop_at, want)

    def test_stopped_keeper_gives_no_stop_classification(self):
        self.bind()
        self.live_keeper(down={'kind': 'OWNER', 'first_at': 99})
        state.process_identity.return_value = None
        self.assertIsNone(self.read().down_kind)
        self.assertIsNone(self.read().down_at)
        self.live_keeper(heartbeat_at=time.time() - keeper.HEARTBEAT_STALE - 1,
                         down={'kind': 'OWNER', 'first_at': 99})
        self.assertEqual(self.read().keeper, 'stale')
        self.assertIsNone(self.read().down_kind)

    def test_failed_reading_falls_back_to_last_ok(self):
        self.bind()
        self.live_keeper(reading={'ok': False, 'state': None, 'at': 100},
                         last_ok={'state': 'Available', 'at': 90})
        self.assertEqual(self.read().reading, ('Available', 90.0))
        self.live_keeper(reading={'ok': True, 'state': 'Shutdown', 'at': 100},
                         last_ok={'state': 'Available', 'at': 90})
        self.assertEqual(self.read().reading, ('Shutdown', 100.0))

    def test_unreadable_arming_is_unknown_not_unarmed(self):
        self.bind()
        state.atomic_write(self.state / 'arming.json', b'{')
        local = self.read()
        self.assertIsNone(local.armed)
        self.assertIn('arming.json cannot be read', local.problems)

    def test_empty_arming_is_unknown_not_unarmed(self):
        self.bind()
        self.write('arming.json', {})
        local = self.read()
        self.assertIsNone(local.armed)
        self.assertIn('arming.json cannot be read', local.problems)

    def test_process_check_timeout_is_unknown(self):
        self.bind()
        self.live_keeper()
        state.process_identity.side_effect = subprocess.TimeoutExpired('ps', .01)
        self.assertEqual(self.read(timeout=.01).keeper, 'unknown')
        self.assertIn('keeper-runtime.json cannot be read', self.read().problems)

    def test_process_timeout_is_forwarded(self):
        self.identity.stop()
        with patch.object(state.subprocess, 'run', return_value=Mock(returncode=0, stdout='owned')) as run:
            self.assertEqual(state.process_identity(4242, timeout=.03), 'owned')
            self.assertEqual(run.call_args.kwargs['timeout'], .03)
        with patch.object(state, 'process_identity', return_value=None) as identity:
            self.assertFalse(state.owned_process({}, self.state, flag='--keep', module='.keeper', timeout=.02))
            identity.assert_called_once_with(None, timeout=.02)

    def test_refusals_need_a_matching_cycle(self):
        cycle, row = self.cycle()
        want = (self.standing.Refusal('codex', 'session-1', 'refused'),)
        self.assertEqual(self.read().refusals, want)
        self.assertEqual(self.standing.refusals(self.state, self.project, self.binding), want)
        for field, wrong in (('project_id', 'foreign'), ('identity', 'foreign'), ('local_root', '/other'),
                             ('remote_root', '/other'), ('schema', 2), ('completed_at', None)):
            original = cycle[field]
            cycle[field] = wrong
            self.write('scratch-runtime.json', self.runtime)
            self.assertEqual(self.read().refusals, (), field)
            cycle[field] = original
        row['receipt']['snapshot_id'] = 'b' * 64
        self.write('scratch-runtime.json', self.runtime)
        self.assertEqual(self.read().refusals, ())
        row['receipt']['snapshot_id'] = 'a' * 64
        row['capture_issues'], row['publication_issues'] = row['publication_issues'], []
        self.write('scratch-runtime.json', self.runtime)
        self.assertEqual(self.read().refusals, ())

    def test_invalid_refusal_ids_are_not_actions(self):
        _, row = self.cycle()
        row['publication_issues'] += [dict(agent='Codex', session_id='ok', reason='bad'),
                                     dict(agent='codex', session_id='../bad', reason='bad')]
        self.write('scratch-runtime.json', self.runtime)
        local = self.read()
        self.assertEqual(len(local.refusals), 1)
        self.assertTrue(any('publication' in problem for problem in local.problems))

    def checkout_fixture(self, relation):
        initial = self.git('rev-parse', 'HEAD')
        second = self.commit('second')
        third = self.commit('third')
        self.git('update-ref', 'refs/remotes/origin/main', initial if relation in ('ahead', 'current') else third)
        self.git('reset', '--hard', initial)
        if relation == 'ahead':
            self.git('reset', '--hard', second)
        elif relation == 'diverged':
            self.commit('different')
        elif relation == 'detached':
            self.git('checkout', '--detach', '-q')
        elif relation == 'other-branch':
            self.git('checkout', '-qb', 'topic')
        elif relation == 'no-remote-ref':
            self.git('update-ref', '-d', 'refs/remotes/origin/main')
        return initial, third

    def test_checkout_relations(self):
        # Reset the same local fixture without touching a network remote.
        for relation, behind, ahead in (('current', 0, 0), ('behind', 2, 0), ('ahead', 0, 1),
                                        ('diverged', 2, 1), ('detached', 0, 0),
                                        ('other-branch', 0, 0), ('no-remote-ref', 0, 0)):
            with self.subTest(relation=relation):
                self.git('checkout', '-q', 'main')
                self.checkout_fixture(relation)
                checkout = self.read().checkout
                self.assertEqual((checkout.relation, checkout.behind, checkout.ahead), (relation, behind, ahead))

    def test_copy_time_is_the_tracking_refs_own(self):
        first = self.read().checkout.copied_at
        self.assertIsInstance(first, float)
        commit = self.commit('next')
        local_origin = self.base / 'origin.git'
        self.git('clone', '-q', '--bare', str(self.root), str(local_origin), root=self.base)
        self.git('remote', 'set-url', 'origin', str(local_origin))
        # Deliberately separate reflog timestamps without sleeping.
        env = dict(os.environ, GIT_COMMITTER_DATE='2030-01-02T03:04:05+00:00')
        self.git('fetch', '--no-write-fetch-head', 'origin', '+main:refs/remotes/origin/main', env=env)
        copied = self.read().checkout.copied_at
        self.assertNotEqual(copied, first)
        self.git('fetch', 'origin', f'{commit}:refs/remotes/origin/another',
                 env=dict(env, GIT_COMMITTER_DATE='2031-01-02T03:04:05+00:00'))
        self.assertEqual(self.read().checkout.copied_at, copied)

    def test_checkout_matches_setup_decision(self):
        for relation in ('behind', 'current', 'diverged', 'detached', 'other-branch'):
            with self.subTest(relation=relation):
                self.git('checkout', '-q', 'main')
                self.checkout_fixture(relation)
                tip = self.git('rev-parse', 'refs/remotes/origin/main')
                origin = self.base / (relation + '-origin.git')
                self.git('init', '-q', '--bare', str(origin), root=self.base)
                self.git('push', '-q', str(origin), f'{tip}:refs/heads/main')
                self.git('remote', 'set-url', 'origin', str(origin))
                before = self.snapshot()
                self.assertEqual(self.read().checkout.relation, relation)
                self.assertEqual(before, self.snapshot())
                copied = self.base / (relation + '-copy')
                shutil.copytree(self.root, copied)
                result = git_ops.checkout_update(copied, 'main')
                self.assertEqual(result['state'] if result['state'] != 'refused' else result['reason'],
                                 'updated' if relation == 'behind' else relation)

    def test_metadata_matches_setup(self):
        from devcontainer_setup.render import render
        files = render(self.root, Path(__file__).resolve().parents[1],
                       dict(runtimes={}, tools=[], agents=False, dotfiles=False, ssh=False,
                            ports=[], package_managers={}, polyglot=False), 'a' * 40)
        for name, content in files.items():
            if content is not None:
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        self.assertEqual(self.read().source_ref, setup._metadata(self.root)['sourceRef'])

    def test_launch_receipt_matches_setup(self):
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/release/main')
        for explicit in (True, False):
            env = dict(self.env)
            if not explicit:
                env['XDG_STATE_HOME'] = 'relative'
            home = Path(env['HOME'])
            state_home = Path(env['XDG_STATE_HOME']) if explicit else home / '.local/state'
            path = codespaces.receipt_path(state_home, 'Example/Project', 'release/main')
            path.parent.mkdir(parents=True, exist_ok=True)
            for changes, complete in (({}, True), ({'validated_commit': None}, False),
                                      ({'rebuild_pending': True}, False), ({'branch': 'other'}, None)):
                record = dict(repository='Example/Project', branch='release/main', codespace='new-cs',
                              commit='a' * 40, validated_commit='a' * 40, rebuild_pending=False, machine='basic')
                record.update(changes)
                path.write_text(json.dumps(record))
                with patch.dict(os.environ, env), patch.object(Path, 'home', return_value=home):
                    local = self.standing.read(self.root, env=env)
                    history = setup._launch_history(self.root, 'release/main')
                self.assertEqual(None if local.launch is None else local.launch.complete, complete)
                self.assertEqual(bool(local.launch and local.launch.complete), bool(history))

    def test_repository_spelling_matches_setup(self):
        for url in ('https://github.com/Example/Project.git', 'git@github.com:EXample/PRoject.git',
                    'ssh://git@github.com/EXAMPLE/Project/'):
            self.git('remote', 'set-url', 'origin', url)
            self.assertEqual(self.read().repository, setup._github_repository(self.root))

    def test_managed_aliases_match_root_tasks(self):
        from devcontainer_setup.taskfile_mapping import NAMES
        for name in NAMES:
            path = self.root / name
            path.write_text(root_tasks.integrate("version: '3'\ntasks:\n"))
            local = self.read()
            self.assertEqual(local.taskfile, name)
            self.assertEqual(local.managed, frozenset(root_tasks.ALIASES))
            path.unlink()
        path = self.root / 'Taskfile.yml'
        text = root_tasks.integrate("version: '3'\ntasks:\n  work:cs:\n    cmds: ['true']\n")
        path.write_text(text)
        self.assertNotIn('work:cs', self.read().managed)
        for bad in (text.replace('devcontainer:work:status', 'owner:work:status'),
                    text.replace('tasks:\n', 'vars:\n') + "tasks:\n  work:cs:\n    cmds: ['true']\n"):
            path.write_text(bad)
            self.assertIsNone(self.read().managed)
            self.assertIn('The managed work tasks in Taskfile.yml were edited or moved', self.read().problems)
            with self.assertRaises(ValueError):
                root_tasks.integrate(bad)
        path.write_text(text)
        (self.root / 'Taskfile.yaml').write_text(text)
        self.assertIsNone(self.read().managed)

    def test_unresolvable_state_home_is_a_problem(self):
        with patch.object(state, 'state_dir', side_effect=ValueError('fixture state refusal')):
            local = self.read()
        self.assertIsNone(local.state)
        self.assertIn('fixture state refusal', local.problems)

    def test_subprocesses_are_bounded_and_read_only(self):
        run = subprocess.run
        calls = []
        def observe(argv, **kwargs):
            calls.append((argv, kwargs))
            return run(argv, **kwargs)
        with patch.object(self.standing.subprocess, 'run', side_effect=observe):
            self.read(timeout=.7)
        self.assertTrue(calls)
        for argv, options in calls:
            self.assertEqual(options['timeout'], .7)
            self.assertEqual(options['env']['GIT_OPTIONAL_LOCKS'], '0')
            self.assertEqual(options['env']['GIT_TERMINAL_PROMPT'], '0')
            self.assertFalse(set(argv) & {'status', 'fetch', 'merge'})
        with patch.object(self.standing.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', .01)):
            self.assertEqual(self.read(timeout=.01).checkout.relation, 'unknown')

    def test_valid_pairing_facts_and_malformed_records(self):
        self.bind()
        self.write('arming.json', dict(schema=1, armed=True, codespace='new-cs', gh_user='fixture',
                                      hold_until=200, owner_stop_at=100, owner_stop_codespace='new-cs'))
        self.write('start-evidence.json', {'new-cs': {'started_at': 50, 'stopped_at': 70}})
        self.live_keeper(last_up_at=60, reading={'ok': True, 'state': 'Shutdown', 'at': 110},
                         down={'kind': 'OWNER', 'first_at': 105})
        local = self.read()
        self.assertEqual((local.armed, local.gh_user, local.hold_until, local.started_at, local.last_up_at),
                         (True, 'fixture', 200.0, 50.0, 60.0))
        self.assertEqual((local.keeper, local.down_kind, local.down_at), ('running', 'OWNER', 105.0))
        self.live_keeper(last_ok={'state': 'Deleted', 'at': 120})
        self.assertEqual(self.read().deleted_at, 120.0)
        for name in ('arming.json', 'keeper-runtime.json', 'scratch-runtime.json', 'start-evidence.json'):
            state.atomic_write(self.state / name, b'[]')
            self.assertIn(name + ' cannot be read', self.read().problems)
