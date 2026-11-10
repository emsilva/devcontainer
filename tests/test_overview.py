"""Pure state and command selection from immutable local and remote fixtures."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
import importlib
from itertools import product
from pathlib import Path
import shlex
import unicodedata
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync.probe import Observation, Remote, Space
from devcontainer_setup.project_sync.standing import Checkout, Launch, Local, Refusal


class OverviewTests(unittest.TestCase):
    def setUp(self):
        # Each missing-feature test gets its own failing output line before implementation.
        self.overview = importlib.import_module('devcontainer_setup.project_sync.overview')
        self.today = datetime(2026, 11, 8, 12, 0)
        self.now = self.today.timestamp()
        self.at = self.today.replace(hour=9, minute=10).timestamp()
        self.local = Local(
            root=Path('/fixture/project'), repository='Example/Project', generated=True,
            setup='installed', source_ref='a' * 40, taskfile='Taskfile.yml',
            managed=frozenset(('work:cs', 'work:cs:armed', 'work:cs:stop', 'work:status',
                               'work:logs', 'work:forget')),
            pairing='paired-cs', armed=False, gh_user='fixture-user', keeper='running',
            worker='running', checkout=Checkout(default='main', branch='main', relation='current'))
        self.remote = Remote(
            self.ok('fixture-user'), self.ok(frozenset(('paired-cs',))),
            self.ok((Space('paired-cs', 'Available', self.at - 100),)),
            self.ok(()), self.ok('a' * 40))

    def ok(self, value, at=None):
        return Observation('ok', value, self.at if at is None else at, None)

    def failed(self, reason='request failed', status='failed'):
        return Observation(status, None, None, reason)

    def unavailable(self):
        return Remote(*(self.failed() for _ in range(5)))

    def remote_state(self, state, *, last_used_at=None):
        return replace(self.remote, spaces=self.ok((Space('paired-cs', state, last_used_at),)))

    def frame(self, local=None, remote=None, **kwargs):
        return self.overview.view(local or self.local, remote or self.remote, now=self.now, **kwargs)

    def argv(self, *aliases):
        return [('task', alias) for alias in aliases]

    def assert_actions(self, frame, expected):
        self.assertEqual([action.argv for action in frame.actions], expected)
        self.assertEqual(len({action.key for action in frame.actions}), len(frame.actions))
        for action in frame.actions:
            self.assertEqual(shlex.split(action.command), list(action.argv))
            self.assertEqual(action.item, f'{self.overview.fit(action.label)}  ({action.command})')

    def retirement_local(self, **kwargs):
        self.assertTrue(hasattr(self.local, 'retired'), 'Local needs retired facts')
        self.assertTrue(hasattr(self.local, 'cleanup'), 'Local needs cleanup facts')
        return replace(self.local, **kwargs)

    def replacing_local(self, **kwargs):
        self.assertTrue(hasattr(self.local, 'replacing'), 'Local needs the offline replacing fact')
        return replace(self.local, replacing={'old': 'old-cs', 'new': 'new-cs', 'phase': 'ready'}, **kwargs)

    def test_unfinished_replace_comes_first(self):
        phases = {'inspected': 'inspect', 'create-requested': 'create', 'created': 'create',
                  'ready': 'flush and move', 'moved': 'sign in', 'signed-in': 'check',
                  'checked': 'delete offer', 'offered': 'delete offer'}
        for phase, text in phases.items():
            local = self.replacing_local()
            local = replace(local, replacing=dict(local.replacing, phase=phase))
            frame = self.frame(local)
            self.assertEqual(frame.lines[0], self.overview.fit(
                f'Replacing old-cs: stopped at {text}. Finish it: task devcontainer:setup -- --replace'))
            self.assertEqual(frame.actions[0].label, 'Finish replacing old-cs')
            self.assertEqual(frame.actions[0].argv, ('task', 'devcontainer:setup', '--', '--replace'))
            recovery = self.frame(replace(local, armed=True, keeper='stopped'))
            self.assertEqual(recovery.lines[0], 'Recovery is off until you connect')
            self.assertEqual(recovery.lines[1], frame.lines[0])
            self.assertEqual([a.key for a in recovery.actions[:2]], ['connect', 'finish-replace'])

    def test_replace_command_matches_the_generated_project_coordinator(self):
        import tempfile
        from devcontainer_setup import replacement
        local = self.replacing_local()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrapper = root / '.devcontainer/scripts/project-sync.sh'
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text('# fixture wrapper\n')
            coordinator = replacement._Coordinator(root, root / 'state', None, api=None, ui=None,
                ask=None, env={}, runner=None, now=None, sleep=None)
            self.assertEqual(coordinator.replace, 'task devcontainer:setup -- --replace')
            finish = self.frame(replace(local, root=root)).actions[0]
            self.assertEqual(finish.command, coordinator.replace)
            paired = next(a for a in self.frame(replace(local, root=root)).actions if a.key == 'replace')
            self.assertEqual(paired.command, coordinator.replace)

    def test_replace_is_offered_after_connect_or_start_only(self):
        self.replacing_local()
        for status in ('Available', 'Shutdown', 'Starting', 'ShuttingDown', 'Moved', 'Failed'):
            frame = self.frame(remote=self.remote_state(status))
            keys = [a.key for a in frame.actions]
            if status == 'Failed':
                self.assertNotIn('replace', keys)
            else:
                self.assertEqual(keys[1], 'replace')
                self.assertEqual(frame.actions[1].label, 'Replace paired-cs')
        for local, remote in ((replace(self.local, setup='missing'), self.remote),
                              (replace(self.local, launch=Launch('paired-cs', False)), self.remote),
                              (self.local, replace(self.remote, setup_prs=self.ok((7,)))),
                              (replace(self.local, pairing=None), self.remote)):
            self.assertNotIn('replace', [a.key for a in self.frame(local, remote).actions])

    def test_ended_replace_is_not_shown(self):
        self.replacing_local()
        frame = self.frame()
        self.assertFalse(any('Replacing ' in line for line in frame.lines))
        self.assertNotIn('finish-replace', [a.key for a in frame.actions])

    def test_every_overlay_together_keeps_the_ranked_actions(self):
        local = self.replacing_local(armed=True, keeper='stopped', cleanup=('cleanup-cs',),
            retired=('old-cs',), refusals=(Refusal('codex', 'session-1', 'refused'),),
            checkout=replace(self.local.checkout, relation='behind', behind=2))
        remote = replace(self.remote, setup_prs=self.ok((7, 9)), upstream=self.ok('b' * 40),
                         spaces=self.ok((*self.remote.spaces.value, Space('old-cs', 'Shutdown', None))))
        frame = self.frame(local, remote)
        self.assertEqual([a.key for a in frame.actions],
                         ['connect', 'finish-replace', 'cleanup:cleanup-cs', 'pr:7', 'delete:old-cs', 'details'])
        self.assertEqual(frame.lines[0], 'Recovery is off until you connect')
        self.assertTrue(frame.lines[1].startswith('Replacing old-cs:'))
        self.assertEqual(frame.lines[2], 'Cleanup after deleting cleanup-cs is not finished.')
        self.assertEqual(len(frame.lines), 6)
        self.assertTrue(all(self.overview.cells(line) <= 78 for line in frame.lines))

    def test_cleanup_pending_comes_first_or_after_recovery(self):
        local = self.retirement_local(cleanup=('oldest-cs', 'later-cs'))
        for recovery, state in product((False, True), ('Available', 'Shutdown')):
            current = replace(local, armed=recovery, keeper='stopped' if recovery else 'running')
            frame = self.frame(current, self.remote_state(state))
            index = 1 if recovery else 0
            self.assertEqual(frame.actions[index].label, 'Finish cleaning up after oldest-cs')
            self.assertEqual(frame.actions[index].argv,
                             ('task', 'devcontainer:work:cs:delete', '--', '--codespace', 'oldest-cs'))
            if recovery:
                self.assertIn(frame.actions[0].key, ('connect', 'start'))
            self.assertIn('Cleanup after deleting oldest-cs is not finished.', frame.lines)
            self.assertFalse(any('later-cs' in action.argv for action in frame.actions))
            self.assertGreaterEqual(len(frame.actions), 3)
            self.assertLessEqual(len(frame.actions), 6)

    def test_the_oldest_listed_seal_is_offered_even_when_an_older_one_is_unlisted(self):
        local = self.retirement_local(retired=('unlisted-cs', 'listed-cs', 'later-cs'))
        remote = replace(self.remote, spaces=self.ok((*self.remote.spaces.value,
                         Space('later-cs', 'Shutdown', None), Space('listed-cs', 'Available', None))))
        for setup in ('installed', 'missing'):
            with self.subTest(setup=setup):
                frame = self.frame(replace(local, setup=setup), remote)
                self.assertEqual(frame.actions[-2].label, 'Check and delete listed-cs')
                self.assertEqual(frame.actions[-2].argv,
                                 ('task', 'devcontainer:work:cs:delete', '--', '--codespace', 'listed-cs'))
                self.assertFalse(any('unlisted-cs' in line and 'deleted' in line for line in frame.lines))

    def test_listed_sealed_retired_codespace_takes_the_last_slot(self):
        local = self.retirement_local(
            retired=('old-cs',), cleanup=('cleanup-cs', 'later-cleanup-cs'),
            checkout=replace(self.local.checkout, relation='behind', behind=2),
            refusals=tuple(Refusal('codex', f'session-{n}', 'refused') for n in range(2)))
        remote = replace(self.remote, names=self.ok(frozenset()), upstream=self.ok('b' * 40),
                         spaces=self.ok(tuple(Space(name, 'Available', None)
                                              for name in ('one-cs', 'two-cs', 'three-cs', 'old-cs'))))
        frame = self.frame(local, remote)
        self.assertEqual(frame.actions[0].label, 'Finish cleaning up after cleanup-cs')
        self.assertEqual(frame.actions[-2].label, 'Check and delete old-cs')
        self.assertEqual(frame.actions[-1].key, 'details')
        self.assertEqual(len(frame.actions), 6)
        self.assertLessEqual(len(frame.lines), 6)
        self.assertTrue(all(self.overview.cells(line) <= 78 for line in frame.lines))

    def test_unlisted_or_unsealed_retired_offers_nothing(self):
        listed = replace(self.remote, spaces=self.ok((*self.remote.spaces.value,
                                                      Space('unsealed-cs', 'Available', None))))
        for retired, remote in ((('unlisted-cs',), self.remote), ((), listed),
                                (('old-cs',), self.unavailable())):
            local = self.retirement_local(retired=retired)
            frame = self.frame(local, remote)
            self.assertFalse(any(action.argv[1].endswith('work:cs:delete') for action in frame.actions))
            self.assertFalse(any('delete' in action.label.lower() for action in frame.actions))

    def test_the_current_pairing_is_never_offered_for_delete(self):
        local = self.retirement_local(retired=('paired-cs', 'old-cs'))
        remote = replace(self.remote, spaces=self.ok((*self.remote.spaces.value, Space('old-cs', 'Available', None))))
        for generated in (False, True):
            current = replace(local, generated=generated, managed=frozenset(('work:cs:delete',)))
            frame = self.frame(current, remote)
            deletes = [action for action in frame.actions if action.label.startswith('Check and delete')]
            self.assertEqual([action.argv for action in deletes],
                             [('task', 'work:cs:delete', '--', '--codespace', 'old-cs')])
            inside = self.frame(replace(current, in_codespace=True, cleanup=('old-cs',)), remote)
            self.assertEqual([action.key for action in inside.actions], ['stop'])

    def test_each_situation_has_its_lines_and_actions(self):
        fallback = self.argv('work:logs', 'work:status')
        setup = [('task', 'devcontainer:setup')]
        create = setup + fallback
        replace_command = [('task', 'devcontainer:setup', '--', '--replace')]
        paired = self.argv('work:cs') + replace_command + fallback
        logs = ('gh', 'codespace', 'logs', '-c', 'paired-cs')
        resume = ('task', 'devcontainer:setup', '--', '--resume', 'paired-cs')
        cases = [
            (replace(self.local, in_codespace=True), self.remote,
             "This is the Codespace; task work shows the project's state on your Mac.",
             self.argv('work:cs:stop')),
            (replace(self.local, setup='missing'), self.remote,
             'This project is not set up yet', create),
            (replace(self.local, setup='unmanaged'), self.remote,
             'This project is not set up yet', create),
            (replace(self.local, setup='unknown'), self.remote,
             'This project is not set up yet', create),
            (replace(self.local, setup='partial'), self.remote,
             'Setup did not finish; run setup again to complete it', create),
            (self.local, replace(self.remote, setup_prs=self.ok((7,))),
             'Setup pull request #7 is open; resume setup to finish it', create),
            (self.local, replace(self.remote, setup_prs=self.ok((9, 7))),
             '2 setup pull requests are open; close all but one in GitHub',
             [('gh', 'pr', 'view', '7', '--web'), ('gh', 'pr', 'view', '9', '--web')] + fallback),
            (self.local, self.remote_state('Failed'),
             'Codespace paired-cs failed on GitHub; see its creation logs', [logs, resume] + fallback),
            (replace(self.local, launch=Launch('paired-cs', False)), self.remote,
             'Setup of Codespace paired-cs did not finish validation', [resume, logs] + fallback),
            (self.local, replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(())),
             'Codespace paired-cs was deleted (confirmed 09:10)', create),
            (replace(self.local, deleted_at=self.at), self.unavailable(),
             'Codespace paired-cs was deleted (found by the recovery keeper 09:10)', create),
            (self.local, replace(self.remote, account=self.failed(), names=self.ok(frozenset()),
                                spaces=self.ok(())),
             self.overview.fit("Codespace paired-cs is not among unknown account's Codespaces; "
                               'it may be deleted or under another account'), create),
            (self.local, replace(self.remote, spaces=self.ok(())),
             self.overview.fit("Codespace paired-cs is now listed under another repository; "
                               "pair this one's Codespace"), create),
            (replace(self.local, pairing=None), replace(self.remote, spaces=self.ok(())),
             'No Codespace is paired with this checkout', create),
            (replace(self.local, pairing=None), self.remote,
             'No Codespace is paired with this checkout', self.argv('work:cs') + setup + fallback),
            (self.local, self.remote,
             'Codespace paired-cs is available (checked 09:10)',
             self.argv('work:cs') + replace_command + self.argv('work:cs:armed', 'work:cs:stop', 'work:status')),
            (self.local, self.remote_state('Shutdown'),
             'Codespace paired-cs is stopped (checked 09:10)', paired),
            (replace(self.local, owner_stop_at=self.at), self.remote_state('Shutdown'),
             'Codespace paired-cs is stopped; you stopped it at 09:10', paired),
            (replace(self.local, down_kind='OWNER', down_at=self.at), self.remote_state('Shutdown'),
             'Codespace paired-cs is stopped; you stopped it at 09:10', paired),
            (replace(self.local, down_kind='LIFETIME', down_at=self.at), self.remote_state('Shutdown'),
             "Codespace paired-cs was stopped by GitHub's 12-hour limit at 09:10",
             paired),
            (replace(self.local, down_kind='OTHER', down_at=self.at), self.remote_state('Shutdown'),
             'Codespace paired-cs stopped at 09:10, not by you', paired),
            (self.local, self.remote_state('Starting'),
             'Codespace paired-cs is starting (checked 09:10)', paired),
            (self.local, self.remote_state('ShuttingDown'),
             'Codespace paired-cs is stopping (checked 09:10)', paired),
            (self.local, self.remote_state('Moved'),
             'Codespace paired-cs is in state Moved (checked 09:10)', paired),
            (self.local, replace(self.unavailable(), spaces=self.failed('no answer within 2 s', 'unknown')),
             'Codespace paired-cs: GitHub did not answer within 2 s', paired),
            (self.local, self.unavailable(), 'Codespace paired-cs: request failed',
             paired),
        ]
        for local, remote, first, actions in cases:
            with self.subTest(first=first, setup=local.setup):
                frame = self.frame(local, remote)
                self.assertEqual(frame.lines[0], first)
                self.assert_actions(frame, actions)

    def test_simultaneous_facts_resolve_one_way(self):
        local = replace(self.local, armed=True, keeper='stopped', owner_stop_at=self.at,
                        checkout=replace(self.local.checkout, relation='behind', behind=2),
                        refusals=(Refusal('codex', 'session-1', 'refused'),))
        frame = self.frame(local, self.remote_state('Shutdown'))
        self.assertEqual(frame.lines, (
            'Recovery is off until you connect',
            'Codespace paired-cs is stopped; you stopped it at 09:10',
            'paired-cs refused 1 conversation; forget it to clear the sync warning',
            'Checkout is 2 commits behind origin/main (local copy)'))
        self.assert_actions(frame, [
            ('task', 'work:cs'),
            ('task', 'devcontainer:setup', '--', '--replace'),
            ('task', 'work:forget', '--', '--agent', 'codex', '--session', 'session-1'),
            ('git', '-C', '/fixture/project', 'merge', '--ff-only', 'origin/main'),
            ('task', 'work:status')])
        failed = self.frame(replace(self.local, launch=Launch('paired-cs', False)),
                            self.remote_state('Failed'))
        self.assertEqual(failed.actions[0].key, 'creation-logs')
        missing_remote = replace(self.remote, account=self.failed(), names=self.ok(frozenset()),
                                 spaces=self.ok(()))
        for armed in (True, False):
            missing = self.frame(replace(self.local, armed=armed, keeper='stopped'), missing_remote)
            self.assertNotIn('Recovery is off until you connect', missing.lines)
            self.assertNotIn('connect', [a.key for a in missing.actions])
            self.assertIn('is not among', missing.lines[0])
        prs = self.frame(remote=replace(self.remote, setup_prs=self.ok((9, 7))))
        self.assertEqual([a.key for a in prs.actions], ['pr:7', 'pr:9', 'logs', 'details'])
        foreign = self.frame(replace(self.local, launch=Launch('another-cs', False)))
        self.assertIn('is available', foreign.lines[0])
        # A setup situation must not hide the evidence that recovery has no current target.
        for remote in (missing_remote, replace(self.remote, spaces=self.ok(())),
                       replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(()))):
            for setup, prs in (('partial', ()), ('installed', (7,))):
                frame = self.frame(replace(self.local, armed=True, keeper='stopped', setup=setup),
                                   replace(remote, setup_prs=self.ok(prs)))
                self.assertNotIn('Recovery is off until you connect', frame.lines)

    def test_frame_is_at_most_six_lines_of_78_cells_and_three_to_six_actions(self):
        situations = [
            (self.local, self.remote_state(state)) for state in
            ('Available', 'Shutdown', 'Starting', 'ShuttingDown', 'Failed', 'Moved')]
        situations += [(replace(self.local, setup=setup), self.remote) for setup in
                       ('missing', 'unmanaged', 'unknown', 'partial')]
        situations += [
            (replace(self.local, in_codespace=True), self.remote),
            (replace(self.local, pairing=None), replace(self.remote, spaces=self.ok(()))),
            (replace(self.local, pairing=None), self.remote),
            (replace(self.local, launch=Launch('paired-cs', False)), self.remote),
            (replace(self.local, deleted_at=self.at), self.unavailable()),
            (self.local, self.unavailable()),
            (self.local, replace(self.remote, setup_prs=self.ok((7,)))),
            (self.local, replace(self.remote, setup_prs=self.ok((9, 7)))),
            (self.local, replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(()))),
            (self.local, replace(self.remote, account=self.failed(), names=self.ok(frozenset()),
                                spaces=self.ok(()))),
            (self.local, replace(self.remote, spaces=self.ok(()))),
        ]
        for (local, remote), armed, keeper, count, behind, upgrade in product(
                situations, (True, False, None), ('running', 'stopped', 'stale', 'unknown'),
                (0, 3), (False, True), (False, True)):
            local = replace(local, armed=armed, keeper=keeper,
                            refusals=tuple(Refusal('codex', f'session-{n}', 'refused') for n in range(count)),
                            checkout=replace(local.checkout, relation='behind' if behind else 'current',
                                             behind=2 if behind else 0))
            remote = replace(remote, upstream=self.ok(('b' if upgrade else 'a') * 40))
            frame = self.frame(local, remote)
            self.assertLessEqual(len(frame.lines), 6)
            self.assertTrue(all(self.overview.cells(line) <= 78 for line in frame.lines))
            if local.in_codespace:
                self.assertEqual([a.key for a in frame.actions], ['stop'])
                self.assertEqual(len(frame.lines), 1)
            else:
                self.assertGreaterEqual(len(frame.actions), 3)
                self.assertLessEqual(len(frame.actions), 6)
                self.assertEqual(frame.actions[-1].key, 'details')

    def test_untrusted_text_never_reaches_the_terminal(self):
        unsafe_categories = {'Cc', 'Cf', 'Zl', 'Zp'}
        names = ('cs\x1b[2J', 'cs\nname', 'cs\u202ename', 'コードスペース' * 15,
                 'cs👩\u200d💻', 'cs\u2028name', 'cs\u2029name')
        for name in names:
            local = replace(self.local, pairing=name, root=Path('/fixture/project\npath'),
                            problems=(f'Problem with {name}',),
                            checkout=replace(self.local.checkout, relation='behind', behind=1))
            remote = replace(self.remote, names=self.ok(frozenset()),
                             spaces=self.ok((Space(name, 'Available', self.at),
                                             Space('other-' + name, 'Shutdown', self.at))))
            frame = self.frame(local, remote)
            with self.subTest(name=name):
                for text in (*frame.lines, *(a.item for a in frame.actions),
                             *self.overview.next_lines(frame)):
                    self.assertFalse(any(unicodedata.category(c) in unsafe_categories for c in text))
                self.assertTrue(all(self.overview.cells(line) <= 78 for line in frame.lines))
                self.assertNotIn('update-checkout', [a.key for a in frame.actions])
                self.assertTrue(any('unsafe' in line.lower() for line in frame.lines))
        path = '/fixture/' + 'long path/' * 30 + 'project'
        local = replace(self.local, root=Path(path),
                        checkout=replace(self.local.checkout, relation='behind', behind=1))
        frame = self.frame(local)
        update = next(a for a in frame.actions if a.key == 'update-checkout')
        self.assertEqual(shlex.split(update.command)[2], str(local.root))
        self.assertIn('Next: ' + update.command, self.overview.next_lines(frame))
        self.assertGreater(len(update.command), 78)

    def test_bare_404_or_other_account_is_not_deleted(self):
        cases = [
            replace(self.remote, account=self.ok('other-user'), names=self.ok(frozenset()), spaces=self.ok(())),
            replace(self.remote, account=self.failed(), names=self.ok(frozenset()), spaces=self.ok(())),
            replace(self.unavailable(), spaces=self.failed('HTTP 404')),
            replace(self.unavailable(), spaces=self.ok(())),
        ]
        for remote in cases:
            with self.subTest(remote=remote):
                self.assertFalse(any('was deleted' in line for line in self.frame(remote=remote).lines))
        local = replace(self.local, gh_user=None)
        frame = self.frame(local, replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(())))
        self.assertIn('is not among', frame.lines[0])

    def test_old_owner_stop_does_not_label_a_later_run(self):
        for kind in (None, 'OWNER', 'LIFETIME', 'OTHER'):
            local = replace(self.local, owner_stop_at=self.at, down_kind=kind,
                            down_at=self.at if kind else None)
            for source in ('local', 'remote'):
                newer = replace(local, started_at=self.at + 1) if source == 'local' else local
                remote = self.remote_state('Shutdown', last_used_at=self.at + 1 if source == 'remote' else None)
                with self.subTest(kind=kind, source=source):
                    self.assertEqual(self.frame(newer, remote).lines[0],
                                     'Codespace paired-cs is stopped (checked 09:10)')

    def test_asynchronous_stop_is_still_yours(self):
        local = replace(self.local, owner_stop_at=self.at, last_up_at=self.at + 10)
        remote = replace(self.remote_state('Shutdown', last_used_at=self.at - 10),
                         spaces=self.ok((Space('paired-cs', 'Shutdown', self.at - 10),), self.at + 20))
        self.assertEqual(self.frame(local, remote).lines[0],
                         'Codespace paired-cs is stopped; you stopped it at 09:10')

    def test_several_setup_prs_offer_each_pull_request(self):
        frame = self.frame(remote=replace(self.remote, setup_prs=self.ok((9, 7))))
        self.assert_actions(frame, [('gh', 'pr', 'view', '7', '--web'),
                                    ('gh', 'pr', 'view', '9', '--web'),
                                    ('task', 'work:logs'), ('task', 'work:status')])
        many = self.frame(remote=replace(self.remote, setup_prs=self.ok((11, 9, 7, 10))))
        self.assertEqual([a.key for a in many.actions], ['pr:7', 'pr:9', 'pr:10', 'details'])

    def test_cached_state_says_as_of(self):
        for dt in (self.today.replace(hour=9, minute=10),
                   self.today.replace(hour=9, minute=10) - timedelta(days=1)):
            stamp = dt.strftime('%H:%M' if dt.date() == self.today.date() else '%Y-%m-%d %H:%M')
            for raw, text in (('Available', 'available'), ('Shutdown', 'stopped'),
                              ('Starting', 'starting'), ('ShuttingDown', 'stopping')):
                frame = self.frame(replace(self.local, reading=(raw, dt.timestamp())), self.unavailable())
                self.assertEqual(frame.lines[0], f'Codespace paired-cs is {text} (as of {stamp})')
                self.assertNotIn('checked', frame.lines[0])

    def test_newer_sighting_overrides_the_keepers_deleted(self):
        local = replace(self.local, deleted_at=self.at - 1)
        self.assertIn('is available', self.frame(local).lines[0])
        elsewhere = self.frame(local, replace(self.remote, spaces=self.ok(())))
        self.assertIn('another repository', elsewhere.lines[0])
        self.assertFalse(any('was deleted' in line for line in elsewhere.lines))

    def test_owner_alias_is_not_assumed_managed(self):
        for managed in (None, frozenset(), frozenset(('work:logs',))):
            local = replace(self.local, managed=managed)
            self.assertEqual(self.overview.task_command(local, 'work:cs'),
                             ('task', 'devcontainer:work:cs'))
            self.assertEqual(self.frame(local).actions[0].argv, ('task', 'devcontainer:work:cs'))
        self.assertEqual(self.overview.task_command(self.local, 'work:cs'), ('task', 'work:cs'))
        source = replace(self.local, generated=False, managed=None, setup='source')
        self.assertEqual(self.overview.task_command(source, 'work:cs'), ('task', 'work:cs'))
        self.assertEqual(self.overview.task_command(source, 'setup'), ('task', 'setup'))
        self.assertEqual(self.overview.task_command(self.local, 'setup'), ('task', 'devcontainer:setup'))
        self.assertEqual(self.overview.task_command(self.local, 'work:cs', '--', '--rebind', 'another-cs'),
                         ('task', 'work:cs', '--', '--rebind', 'another-cs'))
        self.assertNotIn('upgrade', [a.key for a in self.frame(
            source, replace(self.remote, upstream=self.ok('b' * 40))).actions])

    def test_unmapped_codespace_state_is_other(self):
        for raw in ('Moved', 'Created', 'Unknown', 'FutureState'):
            frame = self.frame(remote=self.remote_state(raw))
            self.assertEqual(frame.lines[0], f'Codespace paired-cs is in state {raw} (checked 09:10)')
            self.assertEqual(frame.actions[0].key, 'connect')

    def test_forgotten_refusals_are_not_shown(self):
        refusals = (Refusal('codex', 'session-1', 'bad'), Refusal('claude', 'session-2', 'bad'),
                    Refusal('codex', 'session-3', 'bad'))
        local = replace(self.local, refusals=refusals)
        frame = self.frame(local, forgotten=frozenset((('codex', 'session-1'),)))
        self.assertIn('paired-cs refused 2 conversations; forget them to clear the sync warning', frame.lines)
        self.assertNotIn('forget:codex:session-1', [a.key for a in frame.actions])
        # Replace occupies one slot, leaving the first remaining forget action before Details.
        self.assertEqual([a.key for a in frame.actions],
                         ['connect', 'replace', 'arm', 'stop', 'forget:claude:session-2', 'details'])
        cleared = self.frame(local, forgotten=frozenset((r.agent, r.session_id) for r in refusals))
        self.assertFalse(any('refused' in line for line in cleared.lines))
        self.assertFalse(any(a.key.startswith('forget:') for a in cleared.actions))

    def test_pending_remote_reads_checking(self):
        frame = self.overview.view(self.local, None, now=self.now)
        self.assertEqual(frame.lines[0], 'Codespace paired-cs: checking GitHub…')
        self.assertNotIn('unknown', ' '.join(frame.lines))
        cached = self.overview.view(replace(self.local, reading=('Available', self.at)), None, now=self.now)
        self.assertEqual(cached.lines[0], 'Codespace paired-cs is available (as of 09:10)')
        deleted = self.overview.view(replace(self.local, deleted_at=self.at, armed=True, keeper='stopped'),
                                     None, now=self.now)
        self.assertEqual(deleted.lines[0],
                         'Codespace paired-cs was deleted (found by the recovery keeper 09:10)')
        self.assertNotIn('Recovery is off until you connect', deleted.lines)

    def test_cells_and_fit_handle_wide_combining_and_control_characters(self):
        self.assertEqual(self.overview.cells('A界e\u0301'), 4)
        self.assertEqual(self.overview.fit('A界e\u0301', 4), 'A界e\u0301')
        self.assertEqual(self.overview.fit('A界BC', 4), 'A界…')
        self.assertEqual(self.overview.fit('A\nB\x1b\u202e\u2028\u2029C'), 'ABC')
        self.assertEqual(self.overview.fit('abc', 1), '…')
        self.assertEqual(self.overview.fit('abc', 0), '')

    def test_normalize_covers_the_declared_states(self):
        groups = {'available': ('Available',), 'stopped': ('Shutdown',), 'stopping': ('ShuttingDown',),
                  'starting': ('Starting', 'Provisioning', 'Queued', 'Rebuilding', 'Awaiting', 'Exporting', 'Updating'),
                  'failed': ('Failed', 'Unavailable', 'Archived'), 'deleted': ('Deleted',),
                  'other': ('Created', 'Moved', '')}
        for expected, values in groups.items():
            for value in values:
                self.assertEqual(self.overview.normalize(value), expected)

    def test_pair_actions_are_newest_first_target_qualified_and_bounded(self):
        spaces = (Space('old-cs', 'Shutdown', None), Space('middle-cs', 'Available', self.at - 1),
                  Space('paired-cs', 'Deleted', self.at + 100), Space('new-cs', 'Available', self.at),
                  Space('older-cs', 'Shutdown', self.at - 2))
        remote = replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(spaces[0:2] + spaces[3:]))
        frame = self.frame(remote=remote)
        self.assertEqual([a.key for a in frame.actions],
                         ['pair:new-cs', 'pair:middle-cs', 'pair:older-cs', 'create', 'details'])
        self.assertEqual(frame.actions[0].argv, ('task', 'work:cs', '--', '--rebind', 'new-cs'))

    def test_overlays_are_ranked_and_generator_requires_an_installed_generated_project(self):
        local = replace(self.local, armed=True, keeper='stale', keeper_heartbeat_age=181,
                        worker='stopped', cycle_at=self.at, problems=('arming.json cannot be read',),
                        checkout=replace(self.local.checkout, relation='behind', behind=2, copied_at=self.at))
        remote = replace(self.remote, upstream=self.ok('b' * 40))
        frame = self.frame(local, remote)
        self.assertEqual(frame.lines, (
            'arming.json cannot be read; task work:status shows more',
            'Codespace paired-cs is available (checked 09:10)',
            'The recovery keeper has not answered for 3 min; connect to restart it',
            'Sync is stopped; connect to start it',
            'Generator upstream is at bbbbbbb; this project uses aaaaaaa',
            'Checkout is 2 commits behind origin/main (local copy from 09:10)'))
        self.assertEqual(frame.actions[-2].argv, ('task', 'devcontainer:upgrade:self'))
        held = self.frame(replace(self.local, armed=True, hold_until=self.now + 3600, cycle_at=self.at))
        self.assertIn('Recovery is armed as fixture-user, on hold until 13:00', held.lines)
        self.assertIn('Sync last completed 09:10', held.lines)
        expired = self.frame(replace(self.local, armed=True, hold_until=self.at))
        self.assertIn('Recovery is armed as fixture-user', expired.lines)
        for setup in ('partial', 'missing'):
            self.assertNotIn('upgrade', [a.key for a in self.frame(replace(self.local, setup=setup), remote).actions])
        diverged = self.frame(replace(self.local, checkout=replace(self.local.checkout, relation='diverged')))
        self.assertIn('Checkout and origin/main have diverged; review before merging', diverged.lines)
        self.assertNotIn('update-checkout', [a.key for a in diverged.actions])
        other = self.frame(replace(self.local, checkout=replace(self.local.checkout, branch='topic', relation='behind')))
        self.assertNotIn('update-checkout', [a.key for a in other.actions])

    def test_view_is_pure_and_its_output_is_immutable(self):
        with patch('builtins.open', side_effect=AssertionError('file read')), \
                patch('pathlib.Path.open', side_effect=AssertionError('file read')), \
                patch('subprocess.Popen', side_effect=AssertionError('process started')), \
                patch('devcontainer_setup.project_sync.state.locked', side_effect=AssertionError('lock')):
            frame = self.frame()
            self.assertEqual(frame, self.frame())
            self.assertEqual(self.overview.next_lines(frame), ['Next: ' + a.command for a in frame.actions])
        with self.assertRaises(FrozenInstanceError):
            frame.lines = ()
        with self.assertRaises(FrozenInstanceError):
            frame.actions[0].key = 'changed'
