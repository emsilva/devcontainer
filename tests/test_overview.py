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

# Run 3's real names (plan, "Names in tests and drawings"): a short fixture name would hide a cut.
CS = 'bookish-space-meme-v66pqg6wpvvqcpw7j'
OLD = 'literate-pancake-jjjrq4j9rqwrc5qp6'
TITLE = 'consult-sm-innovate · ' + CS
UNPAIRED = 'consult-sm-innovate · no Codespace paired'


def label(line):
    return line[:9].rstrip()


class OverviewTests(unittest.TestCase):
    def setUp(self):
        # Each missing-feature test gets its own failing output line before implementation.
        self.overview = importlib.import_module('devcontainer_setup.project_sync.overview')
        self.today = datetime(2026, 11, 8, 12, 0)
        self.now = self.today.timestamp()
        self.at = self.today.replace(hour=9, minute=10).timestamp()
        self.local = Local(
            root=Path('/fixture/consult-sm-innovate'), repository='Example/Project', generated=True,
            setup='installed', source_ref='a' * 40, taskfile='Taskfile.yml',
            managed=frozenset(('work:cs', 'work:cs:armed', 'work:cs:stop', 'work:status',
                               'work:logs', 'work:forget', 'work:signin:all')),
            pairing=CS, armed=False, gh_user='fixture-user', keeper='running',
            worker='running', checkout=Checkout(default='main', branch='main', relation='current'))
        self.remote = Remote(
            self.ok('fixture-user'), self.ok(frozenset((CS,))),
            self.ok((Space(CS, 'Available', self.at - 100),)),
            self.ok(()), self.ok('a' * 40))

    def ok(self, value, at=None):
        return Observation('ok', value, self.at if at is None else at, None)

    def failed(self, reason='request failed', status='failed'):
        return Observation(status, None, None, reason)

    def unavailable(self):
        return Remote(*(self.failed() for _ in range(5)))

    def remote_state(self, state, *, last_used_at=None):
        return replace(self.remote, spaces=self.ok((Space(CS, state, last_used_at),)))

    def frame(self, local=None, remote=None, **kwargs):
        return self.overview.view(local or self.local, remote or self.remote, now=self.now, **kwargs)

    def argv(self, *aliases):
        return [('task', alias) for alias in aliases]

    def keys(self, frame):
        return [action.key for action in frame.actions]

    def rows(self, frame, name):
        return [line for line in frame.lines[1:] if label(line) == name]

    def assert_actions(self, frame, expected):
        self.assertEqual([action.argv for action in frame.actions], expected)
        self.assertEqual(len({action.key for action in frame.actions}), len(frame.actions))
        for action in frame.actions:
            self.assertEqual(shlex.split(action.command), list(action.argv))
            self.assertEqual(action.item, self.overview.fit(action.label))

    def retirement_local(self, **kwargs):
        self.assertTrue(hasattr(self.local, 'retired'), 'Local needs retired facts')
        self.assertTrue(hasattr(self.local, 'cleanup'), 'Local needs cleanup facts')
        return replace(self.local, **kwargs)

    def replacing_local(self, **kwargs):
        self.assertTrue(hasattr(self.local, 'replacing'), 'Local needs the offline replacing fact')
        return replace(self.local, replacing={'old': 'old-cs', 'new': 'new-cs', 'phase': 'ready'}, **kwargs)

    def test_unfinished_replace_comes_first(self):
        words = self.overview.replacing.PHASE_WORDS
        for phase in self.overview.replacing.PHASES:
            local = self.replacing_local()
            local = replace(local, replacing=dict(local.replacing, phase=phase))
            frame = self.frame(local)
            self.assertEqual(frame.lines[0], TITLE)
            self.assertEqual(frame.lines[1], f'Replace    stopped at {words[phase]}; finish it')
            self.assertEqual(frame.actions[0].label, 'Finish the replace')
            self.assertEqual(frame.actions[0].argv, ('task', 'devcontainer:setup', '--', '--replace'))
            self.assertNotIn('replace', self.keys(frame))
            recovery = self.frame(replace(local, armed=True, keeper='stopped'))
            self.assertEqual(recovery.lines[1], 'Recovery   off until you connect')
            self.assertEqual(recovery.lines[2], frame.lines[1])
            self.assertEqual(self.keys(recovery)[:2], ['connect', 'finish-replace'])
            self.assertNotIn('replace', self.keys(recovery))

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
            paired = next(a for a in self.frame(replace(local, root=root, replacing=None)).actions
                          if a.key == 'replace')
            self.assertEqual(paired.command, coordinator.replace)

    def test_replace_sits_above_the_delete_and_details(self):
        self.replacing_local()
        replace_argv = ('task', 'devcontainer:setup', '--', '--replace')
        listed = (Space('other-cs', 'Shutdown', None),)
        for status, retired in product(('Available', 'Shutdown', 'Starting', 'ShuttingDown', 'Moved', 'Failed'),
                                       (False, True)):
            with self.subTest(status=status, retired=retired):
                remote = self.remote_state(status)
                remote = replace(remote, spaces=self.ok(remote.spaces.value + listed))
                local = self.retirement_local(retired=('other-cs',) if retired else ())
                frame = self.frame(local, remote)
                keys = self.keys(frame)
                if status == 'Failed':
                    self.assertNotIn('replace', keys)
                    continue
                after = next(index for index, key in enumerate(keys)
                             if key.startswith('delete:') or key == 'details')
                self.assertEqual(keys.index('replace'), after - 1)
                self.assertEqual(frame.actions[after - 1].label, 'Replace this Codespace')
                self.assertEqual(frame.actions[after - 1].argv, replace_argv)
                self.assertEqual(keys[-2:], ['delete:other-cs', 'details'] if retired else ['replace', 'details'])
        busy = replace(self.local, signed_out=('Plaud',),
                       refusals=(Refusal('codex', 'session-1', 'refused'), Refusal('codex', 'session-2', 'refused')))
        frame = self.frame(busy, replace(self.remote, upstream=self.ok('b' * 40)))
        self.assertEqual(self.keys(frame), ['connect', 'arm', 'stop', 'signin', 'replace', 'details'])
        self.assertEqual(frame.actions[-2].argv, replace_argv)
        for local, remote in ((replace(self.local, setup='missing'), self.remote),
                              (replace(self.local, launch=Launch(CS, False)), self.remote),
                              (self.local, replace(self.remote, setup_prs=self.ok((7,)))),
                              (replace(self.local, pairing=None), self.remote)):
            self.assertNotIn('replace', self.keys(self.frame(local, remote)))

    def test_ended_replace_is_not_shown(self):
        self.replacing_local()
        frame = self.frame()
        self.assertEqual(self.rows(frame, 'Replace'), [])
        self.assertNotIn('finish-replace', self.keys(frame))

    def test_every_overlay_together_keeps_the_ranked_actions(self):
        local = self.replacing_local(armed=True, keeper='stopped', cleanup=('cleanup-cs',),
            retired=('other-cs',), refusals=(Refusal('codex', 'session-1', 'refused'),),
            checkout=replace(self.local.checkout, relation='behind', behind=2))
        remote = replace(self.remote, setup_prs=self.ok((7, 9)), upstream=self.ok('b' * 40),
                         spaces=self.ok((*self.remote.spaces.value, Space('other-cs', 'Shutdown', None))))
        frame = self.frame(local, remote)
        self.assertEqual(self.keys(frame),
                         ['connect', 'finish-replace', 'cleanup:cleanup-cs', 'pr:7', 'delete:other-cs', 'details'])
        self.assertEqual(frame.lines[1], 'Recovery   off until you connect')
        self.assertEqual(frame.lines[2], 'Replace    stopped at ' + self.overview.replacing.PHASE_WORDS['ready'] + '; finish it')
        self.assertEqual(frame.lines[3], 'Cleanup    not finished for cleanup-cs')
        self.assertEqual(len(frame.lines), 6)
        self.assertTrue(all(self.overview.cells(line) <= 78 for line in frame.lines))

    def test_old_delete_waits_for_checked_and_keeps_the_other_seals_slot(self):
        for phase in self.overview.replacing.PHASES:
            with self.subTest(phase=phase):
                local = replace(self.replacing_local(retired=('old-cs', 'other-cs')),
                                pairing='new-cs',
                                replacing={'old': 'old-cs', 'new': 'new-cs', 'phase': phase})
                remote = replace(self.remote, spaces=self.ok(tuple(
                    Space(name, 'Available', self.at) for name in ('new-cs', 'old-cs', 'other-cs'))))
                frame = self.frame(local, remote)
                target = 'old-cs' if phase in ('checked', 'offered') else 'other-cs'
                self.assertEqual(frame.actions[-2].key, 'delete:' + target)
                self.assertEqual(frame.actions[-1].key, 'details')
                if phase not in ('checked', 'offered'):
                    self.assertNotIn('delete:old-cs', self.keys(frame))

    def test_cleanup_pending_comes_first_or_after_recovery(self):
        local = self.retirement_local(cleanup=('oldest-cs', 'later-cs'))
        for recovery, state in product((False, True), ('Available', 'Shutdown')):
            current = replace(local, armed=recovery, keeper='stopped' if recovery else 'running')
            frame = self.frame(current, self.remote_state(state))
            index = 1 if recovery else 0
            self.assertEqual(frame.actions[index].label, 'Finish cleaning up after the delete')
            self.assertEqual(frame.actions[index].argv,
                             ('task', 'devcontainer:work:cs:delete', '--', '--codespace', 'oldest-cs'))
            if recovery:
                self.assertIn(frame.actions[0].key, ('connect', 'start'))
            self.assertIn('Cleanup    not finished for oldest-cs', frame.lines)
            self.assertFalse(any('later-cs' in action.argv for action in frame.actions))
            self.assertGreaterEqual(len(frame.actions), 2)
            self.assertLessEqual(len(frame.actions), 6)

    def test_the_oldest_listed_seal_is_offered_even_when_an_older_one_is_unlisted(self):
        local = self.retirement_local(retired=('unlisted-cs', 'listed-cs', 'later-cs'))
        remote = replace(self.remote, spaces=self.ok((*self.remote.spaces.value,
                         Space('later-cs', 'Shutdown', None), Space('listed-cs', 'Available', None))))
        for setup in ('installed', 'missing'):
            with self.subTest(setup=setup):
                frame = self.frame(replace(local, setup=setup), remote)
                self.assertEqual(frame.actions[-2].label, 'Check and delete the replaced Codespace')
                self.assertEqual(frame.actions[-2].argv,
                                 ('task', 'devcontainer:work:cs:delete', '--', '--codespace', 'listed-cs'))
                self.assertFalse(any('unlisted-cs' in line for line in frame.lines))

    def test_listed_sealed_retired_codespace_takes_the_last_slot(self):
        local = self.retirement_local(
            retired=('old-cs',), cleanup=('cleanup-cs', 'later-cleanup-cs'),
            checkout=replace(self.local.checkout, relation='behind', behind=2),
            refusals=tuple(Refusal('codex', f'session-{n}', 'refused') for n in range(2)))
        remote = replace(self.remote, names=self.ok(frozenset()), upstream=self.ok('b' * 40),
                         spaces=self.ok(tuple(Space(name, 'Available', None)
                                              for name in ('one-cs', 'two-cs', 'three-cs', 'old-cs'))))
        frame = self.frame(local, remote)
        self.assertEqual(frame.actions[0].label, 'Finish cleaning up after the delete')
        self.assertEqual(frame.actions[-2].label, 'Check and delete the replaced Codespace')
        self.assertEqual(frame.actions[-2].argv[-1], 'old-cs')
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
            self.assertEqual(self.rows(frame, 'Replaced'), [])

    def test_replaced_codespace_is_still_on_github(self):
        local = self.retirement_local(retired=(OLD,))
        listed = replace(self.remote, spaces=self.ok((*self.remote.spaces.value, Space(OLD, 'Shutdown', None))))
        frame = self.frame(local, listed)
        self.assertEqual(self.rows(frame, 'Replaced'), ['Replaced   still on GitHub; check and delete it when ready'])
        delete = next(action for action in frame.actions if action.key == 'delete:' + OLD)
        self.assertEqual(delete.label, 'Check and delete the replaced Codespace')
        self.assertEqual(delete.argv, ('task', 'devcontainer:work:cs:delete', '--', '--codespace', OLD))
        unlisted = self.frame(local, self.remote)
        self.assertEqual(self.rows(unlisted, 'Replaced'), [])
        self.assertFalse(any(action.key.startswith('delete:') for action in unlisted.actions))

    def test_the_current_pairing_is_never_offered_for_delete(self):
        local = self.retirement_local(retired=(CS, 'old-cs'))
        remote = replace(self.remote, spaces=self.ok((*self.remote.spaces.value, Space('old-cs', 'Available', None))))
        for generated in (False, True):
            current = replace(local, generated=generated, managed=frozenset(('work:cs:delete',)))
            frame = self.frame(current, remote)
            deletes = [action for action in frame.actions if action.key.startswith('delete:')]
            self.assertEqual([action.argv for action in deletes],
                             [('task', 'work:cs:delete', '--', '--codespace', 'old-cs')])
            inside = self.frame(replace(current, in_codespace=True, cleanup=('old-cs',)), remote)
            self.assertEqual(self.keys(inside), ['stop'])

    def test_each_situation_has_its_lines_and_actions(self):
        details = self.argv('work:status')
        logs = self.argv('work:logs')
        setup = [('task', 'devcontainer:setup')]
        create = setup + details
        replace_command = [('task', 'devcontainer:setup', '--', '--replace')]
        paired = self.argv('work:cs') + replace_command + details
        creation = ('gh', 'codespace', 'logs', '-c', CS)
        resume = ('task', 'devcontainer:setup', '--', '--resume', CS)
        cases = [
            (replace(self.local, setup='missing'), self.remote, TITLE, 'Setup      not done yet', create),
            (replace(self.local, setup='missing', pairing=None), self.remote, UNPAIRED,
             'Setup      not done yet', create),
            (replace(self.local, setup='missing', logs=True), self.remote, TITLE,
             'Setup      not done yet', setup + logs + details),
            (replace(self.local, setup='unmanaged'), self.remote, TITLE, 'Setup      not done yet', create),
            (replace(self.local, setup='unknown'), self.remote, TITLE, 'Setup      not done yet', create),
            (self.local, replace(self.remote, setup_prs=self.ok((7,))), TITLE,
             'Setup      pull request #7 is open; resume setup to finish it', create),
            (self.local, replace(self.remote, setup_prs=self.ok((9, 7))), TITLE,
             'Setup      2 pull requests are open; close all but one in GitHub',
             [('gh', 'pr', 'view', '7', '--web'), ('gh', 'pr', 'view', '9', '--web')] + details),
            (self.local, self.remote_state('Failed'), TITLE,
             'Codespace  failed on GitHub; see its creation logs', [creation, resume] + details),
            (replace(self.local, launch=Launch(CS, False)), self.remote, TITLE,
             'Codespace  setup did not finish validation; resume it', [resume, creation] + details),
            (replace(self.local, launch=Launch(OLD, False), pairing=None), self.remote, UNPAIRED,
             f'Setup      {OLD} did not finish validation',
             [('task', 'devcontainer:setup', '--', '--resume', OLD), ('gh', 'codespace', 'logs', '-c', OLD)]
             + details),
            (self.local, replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(())), TITLE,
             'Codespace  deleted (confirmed 09:10)', create),
            (replace(self.local, deleted_at=self.at), self.unavailable(), TITLE,
             'Codespace  deleted (found by Recovery 09:10)', create),
            (self.local, replace(self.remote, account=self.ok('mannu_kyndryl'), names=self.ok(frozenset()),
                                spaces=self.ok(())), TITLE,
             'Codespace  not listed for mannu_kyndryl; deleted, or under another account', create),
            (self.local, replace(self.remote, account=self.failed(), names=self.ok(frozenset()),
                                spaces=self.ok(())), TITLE,
             'Codespace  not listed for your account; deleted, or under another account', create),
            (self.local, replace(self.remote, spaces=self.ok(())), TITLE,
             "Codespace  now listed under another repository; pair this one's Codespace", create),
            (replace(self.local, pairing=None), replace(self.remote, spaces=self.ok(())), UNPAIRED,
             'Codespace  GitHub lists none for this repository', create),
            (replace(self.local, pairing=None), self.remote, UNPAIRED,
             'Codespace  GitHub lists 1 for this repository', self.argv('work:cs') + setup + details),
            (replace(self.local, pairing=None),
             replace(self.remote, spaces=self.ok((Space(CS, 'Available', None), Space(OLD, 'Shutdown', None)))),
             UNPAIRED, 'Codespace  GitHub lists 2 for this repository', self.argv('work:cs') + setup + details),
            (self.local, self.remote, TITLE,
             'Codespace  available (checked 09:10)',
             self.argv('work:cs', 'work:cs:armed', 'work:cs:stop') + replace_command + details),
            (self.local, self.remote_state('Shutdown'), TITLE, 'Codespace  stopped (checked 09:10)', paired),
            (replace(self.local, logs=True), self.remote_state('Shutdown'), TITLE,
             'Codespace  stopped (checked 09:10)', self.argv('work:cs', 'work:logs') + replace_command + details),
            (replace(self.local, owner_stop_at=self.at), self.remote_state('Shutdown'), TITLE,
             'Codespace  stopped; you stopped it at 09:10', paired),
            (replace(self.local, down_kind='OWNER', down_at=self.at), self.remote_state('Shutdown'), TITLE,
             'Codespace  stopped; you stopped it at 09:10', paired),
            (replace(self.local, down_kind='LIFETIME', down_at=self.at), self.remote_state('Shutdown'), TITLE,
             "Codespace  stopped by GitHub's 12-hour limit at 09:10", paired),
            (replace(self.local, down_kind='OTHER', down_at=self.at), self.remote_state('Shutdown'), TITLE,
             'Codespace  stopped at 09:10, not by you', paired),
            (self.local, self.remote_state('Starting'), TITLE, 'Codespace  starting (checked 09:10)', paired),
            (self.local, self.remote_state('ShuttingDown'), TITLE, 'Codespace  stopping (checked 09:10)', paired),
            (self.local, self.remote_state('Moved'), TITLE, 'Codespace  in state Moved (checked 09:10)', paired),
            (self.local, replace(self.unavailable(), spaces=self.failed('no answer within 2 s', 'unknown')), TITLE,
             'Codespace  GitHub did not answer within 2 s', paired),
            (self.local, self.unavailable(), TITLE, 'Codespace  request failed', paired),
        ]
        for local, remote, title, situation, actions in cases:
            with self.subTest(situation=situation, setup=local.setup):
                frame = self.frame(local, remote)
                self.assertEqual(frame.lines[0], title)
                self.assertEqual(frame.lines[1], situation)
                self.assert_actions(frame, actions)
        inside = self.frame(replace(self.local, in_codespace=True))
        self.assertEqual(inside.lines, ("This is the Codespace; task work shows the project's state on your Mac.",))
        self.assert_actions(inside, self.argv('work:cs:stop'))
        silent = self.frame(replace(self.local, pairing=None), self.unavailable())
        self.assertEqual(silent.lines, (UNPAIRED,))
        self.assert_actions(silent, self.argv('work:cs') + setup + details)

    def test_recovery_row_says_what_to_plan_around(self):
        armed = replace(self.local, armed=True, keeper='running')
        shutdown = self.remote_state('Shutdown')
        later = self.today.replace(hour=17, minute=45).timestamp()
        tomorrow = datetime(2026, 11, 9, 20, 12).timestamp()
        deleted = replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(()))
        cases = [
            ('1 unreadable arming', replace(self.local, armed=None), self.remote, None),
            ('2 not armed', self.local, self.remote, 'not armed'),
            ('3 stale', replace(armed, keeper='stale', keeper_heartbeat_age=181), self.remote,
             'not answering for 3 min; connect to restart it'),
            ('3 stale, no age', replace(armed, keeper='stale'), self.remote, 'not answering; connect to restart it'),
            ('4 unknown', replace(armed, keeper='unknown'), self.remote, 'state unknown (process check timed out)'),
            ('5 stopped, pairing deleted', replace(armed, keeper='stopped'), deleted, None),
            ('6 hold, stopped', replace(armed, hold_until=self.now + 3600), shutdown, 'armed; on hold until 13:00'),
            ('6 hold, running', replace(armed, hold_until=self.now + 3600), self.remote,
             'armed; on hold until 13:00'),
            ('7 restarting', replace(armed, keeper_phase='restarting', down_kind='LIFETIME', down_at=self.at),
             shutdown, 'armed; restarting it now'),
            ('8 owner', replace(armed, down_kind='OWNER', down_at=self.at), shutdown,
             'armed; not restarting: you stopped it'),
            ('9 stopped during hold', replace(armed, hold_at_stop=self.at, down_kind='LIFETIME', down_at=self.at),
             shutdown, 'armed; not restarting: it stopped during your hold'),
            ('10 out of restarts', replace(armed, restarts_free_at=tomorrow, down_kind='LIFETIME', down_at=self.at),
             shutdown, 'armed; out of restarts until 2026-11-09 20:12'),
            ('11 other', replace(armed, down_kind='OTHER', down_at=self.at), shutdown,
             'armed; not restarting: this was not the 12-hour stop'),
            ('12 lifetime', replace(armed, down_kind='LIFETIME', down_at=self.at), shutdown,
             'armed; not restarted yet; Show details has the reason'),
            ('13 no kind yet', armed, shutdown, 'armed; checking why it stopped'),
            ('13 every field missing', replace(armed, restarts_free_at=self.now - 1), shutdown,
             'armed; checking why it stopped'),
            ('14 next stop', replace(armed, next_stop_at=later), self.remote, 'armed; next 12-hour stop about 17:45'),
            ('15 stop was due', replace(armed, next_stop_at=self.at), self.remote,
             'armed; the 12-hour stop was due about 09:10'),
            ('16 no anchor', armed, self.remote, 'armed; next 12-hour stop not known yet'),
        ]
        for name, local, remote, text in cases:
            with self.subTest(row=name):
                rows = self.rows(self.frame(local, remote), 'Recovery')
                self.assertEqual(rows, [] if text is None else ['Recovery   ' + text])
        off = self.frame(replace(armed, keeper='stopped'), shutdown)
        self.assertEqual(self.rows(off, 'Recovery'), ['Recovery   off until you connect'])

    def test_rows_give_way_by_keep_rank(self):
        busy = self.replacing_local(
            armed=True, keeper='stopped', cleanup=('cleanup-cs',), problems=('arming.json cannot be read',),
            worker='stopped', refusals=(Refusal('codex', 'session-1', 'refused'),), signed_out=('Plaud',),
            retired=('other-cs',), checkout=Checkout(default='main', branch='docs/x', relation='other-branch'))
        remote = replace(self.remote, upstream=self.ok('b' * 40),
                         spaces=self.ok((*self.remote.spaces.value, Space('other-cs', 'Shutdown', None))))
        frame = self.frame(busy, remote)
        self.assertEqual(len(frame.lines), 6)
        self.assertEqual([label(line) for line in frame.lines[1:]],
                         ['Recovery', 'Replace', 'Cleanup', 'Problem', 'Codespace'])
        fine = replace(busy, keeper='running', replacing=None, cleanup=(), problems=())
        frame = self.frame(fine, remote)
        self.assertEqual([label(line) for line in frame.lines[1:]],
                         ['Codespace', 'Sync', 'History', 'Sign-ins', 'Replaced'])
        self.assertEqual(frame.lines[2], 'Sync       stopped; connect to start it')
        # The same rows with nothing pressing are drawn in place order, Recovery included.
        calm = replace(fine, worker='running', refusals=(), signed_out=(), retired=(), cycle_at=self.at)
        frame = self.frame(calm, remote)
        self.assertEqual([label(line) for line in frame.lines[1:]],
                         ['Codespace', 'Recovery', 'Sync', 'Checkout', 'Generator'])

    def test_frames_match_the_plan_drawings(self):
        today = datetime(2026, 11, 14, 9, 0)
        now, at = today.timestamp(), lambda hour, minute: today.replace(hour=hour, minute=minute).timestamp()
        base = replace(self.local, armed=True, keeper='running')
        available = replace(self.remote, spaces=Observation('ok', (Space(CS, 'Available', None),), at(8, 30), None))
        stopped = replace(self.remote, spaces=Observation('ok', (Space(CS, 'Shutdown', None),), at(8, 30), None))
        lifetime = dict(down_kind='LIFETIME', down_at=at(7, 55))
        retired = replace(stopped, spaces=Observation(
            'ok', (Space(CS, 'Shutdown', None), Space(OLD, 'Shutdown', None)), at(8, 30), None))
        drawings = [
            (replace(base, next_stop_at=at(17, 45), cycle_at=at(8, 29)), available,
             (TITLE, 'Codespace  available (checked 08:30)', 'Recovery   armed; next 12-hour stop about 17:45',
              'Sync       last completed 08:29')),
            (replace(base, keeper_phase='restarting', cycle_at=at(7, 54), signed_out=('Plaud',),
                     checkout=Checkout(default='main', branch='docs/design-20261023', relation='other-branch'),
                     **lifetime), stopped,
             (TITLE, "Codespace  stopped by GitHub's 12-hour limit at 07:55", 'Recovery   armed; restarting it now',
              'Sync       last completed 07:54', 'Sign-ins   Plaud is not signed in',
              'Checkout   on docs/design-20261023; Codespaces start from main')),
            (replace(base, keeper='stopped', replacing={'old': CS, 'new': OLD, 'phase': 'ready'}, cleanup=(OLD,),
                     problems=('arming.json cannot be read', 'keeper-runtime.json cannot be read',
                               'signins.json cannot be read'),
                     owner_stop_at=datetime(2026, 11, 13, 7, 55).timestamp()), stopped,
             (TITLE, 'Recovery   off until you connect', 'Replace    stopped at the final sync; finish it',
              f'Cleanup    not finished for {OLD}',
              'Problem    arming.json cannot be read (+2 more); Show details has more',
              'Codespace  stopped; you stopped it at 2026-11-13 07:55')),
            (replace(self.local, pairing=None, checkout=Checkout(
                default='main', branch='docs/design-ux1-high-5e4c-20261114', relation='other-branch')),
             available, (UNPAIRED, 'Codespace  GitHub lists 1 for this repository',
                         'Checkout   on docs/design-ux1-high-5e4c-20261114; Codespaces start from main')),
            (replace(self.local, pairing=None, setup='missing'), available, (UNPAIRED, 'Setup      not done yet')),
            (replace(base, worker='stopped', retired=(OLD,), restarts_free_at=datetime(2026, 11, 15, 20, 12).timestamp(),
                     refusals=tuple(Refusal('codex', f'session-{n:02}', 'refused') for n in range(12)), **lifetime),
             retired,
             (TITLE, "Codespace  stopped by GitHub's 12-hour limit at 07:55",
              'Recovery   armed; out of restarts until 2026-11-15 20:12', 'Sync       stopped; connect to start it',
              'History    12 conversations refused; forget them to clear the sync warning',
              'Replaced   still on GitHub; check and delete it when ready')),
        ]
        for local, remote, lines in drawings:
            with self.subTest(drawing=lines[1]):
                frame = self.overview.view(local, remote, now=now)
                self.assertEqual(frame.lines, lines)
                self.assertTrue(all(self.overview.cells(line) <= 78 for line in frame.lines))

    def test_simultaneous_facts_resolve_one_way(self):
        local = replace(self.local, armed=True, keeper='stopped', owner_stop_at=self.at,
                        checkout=replace(self.local.checkout, relation='behind', behind=2),
                        refusals=(Refusal('codex', 'session-1', 'refused'),))
        frame = self.frame(local, self.remote_state('Shutdown'))
        self.assertEqual(frame.lines, (
            TITLE,
            'Recovery   off until you connect',
            'Codespace  stopped; you stopped it at 09:10',
            'History    1 conversation refused; forget it to clear the sync warning',
            'Checkout   2 commits behind origin/main (local copy)'))
        self.assert_actions(frame, [
            ('task', 'work:cs'),
            ('task', 'work:forget', '--', '--agent', 'codex', '--session', 'session-1'),
            ('git', '-C', '/fixture/consult-sm-innovate', 'merge', '--ff-only', 'origin/main'),
            ('task', 'devcontainer:setup', '--', '--replace'),
            ('task', 'work:status')])
        failed = self.frame(replace(self.local, launch=Launch(CS, False)), self.remote_state('Failed'))
        self.assertEqual(failed.actions[0].key, 'creation-logs')
        missing_remote = replace(self.remote, account=self.failed(), names=self.ok(frozenset()),
                                 spaces=self.ok(()))
        for armed in (True, False):
            missing = self.frame(replace(self.local, armed=armed, keeper='stopped'), missing_remote)
            self.assertNotIn('Recovery   off until you connect', missing.lines)
            self.assertNotIn('connect', self.keys(missing))
            self.assertIn('not listed for', missing.lines[1])
        prs = self.frame(remote=replace(self.remote, setup_prs=self.ok((9, 7))))
        self.assertEqual(self.keys(prs), ['pr:7', 'pr:9', 'details'])
        foreign = self.frame(replace(self.local, launch=Launch('another-cs', False)))
        self.assertIn('available', foreign.lines[1])
        # A setup situation must not hide the evidence that recovery has no current target.
        for remote in (missing_remote, replace(self.remote, spaces=self.ok(())),
                       replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(()))):
            for setup, prs in (('missing', ()), ('installed', (7,))):
                frame = self.frame(replace(self.local, armed=True, keeper='stopped', setup=setup),
                                   replace(remote, setup_prs=self.ok(prs)))
                self.assertNotIn('Recovery   off until you connect', frame.lines)

    def test_frame_is_at_most_six_lines_of_78_cells_and_two_to_six_actions(self):
        situations = [
            (self.local, self.remote_state(state)) for state in
            ('Available', 'Shutdown', 'Starting', 'ShuttingDown', 'Failed', 'Moved')]
        situations += [(replace(self.local, setup=setup), self.remote) for setup in
                       ('missing', 'unmanaged', 'unknown')]
        situations += [
            (replace(self.local, in_codespace=True), self.remote),
            (replace(self.local, pairing=None), replace(self.remote, spaces=self.ok(()))),
            (replace(self.local, pairing=None), self.remote),
            (replace(self.local, pairing=None), self.unavailable()),
            (replace(self.local, launch=Launch(CS, False)), self.remote),
            (replace(self.local, launch=Launch(OLD, False), pairing=None), self.remote),
            (replace(self.local, deleted_at=self.at), self.unavailable()),
            (self.local, self.unavailable()),
            (self.local, replace(self.remote, setup_prs=self.ok((7,)))),
            (self.local, replace(self.remote, setup_prs=self.ok((9, 7)))),
            (self.local, replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(()))),
            (self.local, replace(self.remote, account=self.failed(), names=self.ok(frozenset()),
                                spaces=self.ok(()))),
            (self.local, replace(self.remote, spaces=self.ok(()))),
        ]
        branch = 'docs/design-ux1-high-5e4c-20261114'  # 34 characters
        self.assertEqual(len(branch), 34)
        checkouts = (replace(self.local.checkout, relation='current'),
                     replace(self.local.checkout, relation='behind', behind=2),
                     Checkout(default='main', branch=branch, relation='other-branch'))
        everyone = ('Plaud', 'Codex', 'Claude', 'OpenCode', 'Grok')
        for (local, remote), armed, keeper, count, checkout, upgrade, extras in product(
                situations, (True, False, None), ('running', 'stopped', 'stale', 'unknown'),
                (0, 3), checkouts, (False, True), (False, True)):
            local = replace(local, armed=armed, keeper=keeper, checkout=checkout,
                            refusals=tuple(Refusal('codex', f'session-{n}', 'refused') for n in range(count)))
            if extras:
                local = replace(local, signed_out=everyone, history_pending_since=self.now - 900,
                                next_stop_at=self.now + 3600, restarts_free_at=self.now + 7200, logs=True)
            remote = replace(remote, upstream=self.ok(('b' if upgrade else 'a') * 40))
            frame = self.frame(local, remote)
            self.assertLessEqual(len(frame.lines), 6)
            for line in frame.lines:
                self.assertLessEqual(self.overview.cells(line), 78, line)
                self.assertFalse(line.endswith('…'), line)
            if local.in_codespace:
                self.assertEqual(self.keys(frame), ['stop'])
                self.assertEqual(len(frame.lines), 1)
            else:
                self.assertGreaterEqual(len(frame.actions), 2)
                self.assertLessEqual(len(frame.actions), 6)
                self.assertEqual(frame.actions[-1].key, 'details')
                self.assertEqual(len({action.item for action in frame.actions}), len(frame.actions))

    def test_line_one_cuts_the_folder_never_the_codespace(self):
        local = replace(self.local, root=Path('/fixture/consult-sm-strata-mainframe-modernisation'))
        title = self.frame(local).lines[0]
        self.assertLessEqual(self.overview.cells(title), 78)
        self.assertTrue(title.endswith(' · ' + CS), title)
        self.assertTrue(title.startswith('consult-sm-strata-mainframe'), title)
        self.assertEqual(self.frame(replace(local, root=Path('/fixture/consult-sm-innovate'))).lines[0], TITLE)
        long = replace(local, pairing='a' * 100)
        title = self.frame(long, replace(self.remote, spaces=self.ok((Space('a' * 100, 'Available', None),)))).lines[0]
        self.assertEqual(self.overview.cells(title), 78)
        self.assertTrue(title.endswith('…'), title)
        self.assertEqual(self.frame(replace(local, pairing=None)).lines[0],
                         'consult-sm-strata-mainframe-modernisation · no Codespace paired')

    def test_a_long_problem_keeps_its_pointer(self):
        problem = f'retired/{CS}.json cannot be read'
        for problems, tail in (((problem,), '; Show details has more'),
                               ((problem, 'arming.json cannot be read', 'signins.json cannot be read'),
                                ' (+2 more); Show details has more')):
            with self.subTest(count=len(problems)):
                row, = self.rows(self.frame(replace(self.local, problems=problems)), 'Problem')
                self.assertLessEqual(self.overview.cells(row), 78)
                self.assertTrue(row.endswith(tail), row)
                self.assertTrue(row.startswith('Problem    retired/bookish-space'), row)
        row, = self.rows(self.frame(replace(self.local, problems=('arming.json cannot be read',))), 'Problem')
        self.assertEqual(row, 'Problem    arming.json cannot be read; Show details has more')

    def test_menu_items_show_labels_only(self):
        frames = [self.frame(), self.frame(remote=self.remote_state('Shutdown')),
                  self.frame(replace(self.local, setup='missing')),
                  self.frame(self.replacing_local(cleanup=('cleanup-cs',)))]
        for frame in frames:
            self.assertEqual(self.overview.next_lines(frame), ['Next: ' + action.command for action in frame.actions])
            for action in frame.actions:
                self.assertEqual(action.item, self.overview.fit(action.label))
                self.assertNotIn(action.command, action.item)
                self.assertNotIn('(task', action.item)
                self.assertNotIn(CS, action.item)
        labels = {action.key: action.label for frame in frames for action in frame.actions}
        self.assertEqual(labels['connect'], 'Open in Herdr')  # connected; Connect otherwise (FR2)
        self.assertEqual(labels['start'], 'Start and connect')
        self.assertEqual(labels['arm'], 'Connect and arm recovery')
        self.assertEqual(labels['stop'], 'Stop the Codespace')
        self.assertEqual(labels['replace'], 'Replace this Codespace')
        self.assertEqual(labels['finish-replace'], 'Finish the replace')
        self.assertEqual(labels['cleanup:cleanup-cs'], 'Finish cleaning up after the delete')
        resume = self.frame(replace(self.local, launch=Launch(CS, False)))
        self.assertEqual(next(a.label for a in resume.actions if a.key == 'resume'), 'Resume the Codespace setup')

    def test_connect_reads_open_in_herdr_while_connected(self):
        # FR2: a running sync means connected; recovery off keeps Connect, since only a connect turns it on.
        connect = ('task', 'work:cs')
        cases = [
            ('armed', replace(self.local, armed=True), self.remote, 'Open in Herdr'),
            ('not armed', self.local, self.remote, 'Open in Herdr'),
            ('starting', replace(self.local, armed=True), self.remote_state('Starting'), 'Open in Herdr'),
            ('sync stopped', replace(self.local, worker='stopped'), self.remote, 'Connect'),
            ('sync unknown', replace(self.local, worker='unknown'), self.remote, 'Connect'),
            ('recovery off', replace(self.local, armed=True, keeper='stopped'), self.remote, 'Connect'),
        ]
        for name, local, remote, expected in cases:
            with self.subTest(name=name):
                frame = self.frame(local, remote)
                found, = (action for action in frame.actions if action.key == 'connect')
                self.assertEqual((found.label, found.argv), (expected, connect))
                self.assertLessEqual(self.overview.cells(found.item), 78)
        armed = self.frame(replace(self.local, armed=False))
        self.assertEqual([action.label for action in armed.actions][:3],
                         ['Open in Herdr', 'Connect and arm recovery', 'Stop the Codespace'])
        stopped = self.frame(remote=self.remote_state('Shutdown'))
        self.assertEqual(stopped.actions[0].label, 'Start and connect')
        unpaired = self.frame(replace(self.local, pairing=None))
        self.assertEqual(unpaired.actions[0].label, 'Connect')

    def test_menu_labels_are_unique(self):
        refusals = (Refusal('codex', 'abcdefgh-1', 'refused'), Refusal('codex', 'abcdefgh-2', 'refused'))
        frame = self.frame(replace(self.local, armed=True, refusals=refusals))
        forgets = [action for action in frame.actions if action.key.startswith('forget:')]
        self.assertEqual(len(forgets), 2)
        self.assertEqual([action.item for action in forgets],
                         ['Forget codex conversation abcdefgh', 'Forget codex conversation abcdefgh (2)'])
        items = [action.item for action in frame.actions]
        self.assertEqual(len(set(items)), len(items))
        # Labels that differ only past 78 cells still give two items, and the suffix survives the cut.
        long = 'x' * 90
        unique = self.overview._unique_labels([
            self.overview.Action('one', long + '1', ('true',)), self.overview.Action('two', long + '2', ('true',))])
        self.assertNotEqual(unique[0].item, unique[1].item)
        self.assertTrue(unique[1].item.endswith('… (2)'), unique[1].item)
        self.assertLessEqual(self.overview.cells(unique[1].item), 78)

    def test_logs_appear_only_with_a_log(self):
        for logs in (False, True):
            self.assertEqual(self.keys(self.frame(replace(self.local, setup='missing', logs=logs)))[-1:], ['details'])
        self.assertEqual(self.keys(self.frame(replace(self.local, setup='missing'))), ['setup', 'details'])
        stopped = self.remote_state('Shutdown')
        self.assertEqual(self.keys(self.frame(replace(self.local, logs=True), stopped)),
                         ['start', 'logs', 'replace', 'details'])
        self.assertEqual(self.keys(self.frame(replace(self.local, logs=False), stopped)),
                         ['start', 'replace', 'details'])
        # Nothing pads a menu that already has three actions.
        self.assertNotIn('logs', self.keys(self.frame(replace(self.local, logs=True))))

    def test_other_branch_checkout_is_named(self):
        def row(branch, relation='other-branch'):
            local = replace(self.local, checkout=Checkout(default='main', branch=branch, relation=relation))
            return self.rows(self.frame(local), 'Checkout')
        self.assertEqual(row('docs/x'), ['Checkout   on docs/x; Codespaces start from main'])
        long, = row('docs/' + 'b' * 55)
        self.assertLessEqual(self.overview.cells(long), 78)
        self.assertTrue(long.endswith('…; Codespaces start from main'), long)
        self.assertEqual(row(None, 'detached'), ['Checkout   on a detached HEAD; Codespaces start from main'])
        self.assertEqual(row('main', 'current'), [])

    def test_sign_in_is_offered_only_while_connected(self):
        for worker in ('running', 'stopped'):
            with self.subTest(worker=worker):
                frame = self.frame(replace(self.local, signed_out=('Plaud',), worker=worker))
                self.assertEqual(self.rows(frame, 'Sign-ins'), ['Sign-ins   Plaud is not signed in'])
                signin = [action for action in frame.actions if action.key == 'signin']
                if worker == 'running':
                    self.assertEqual([(a.label, a.argv) for a in signin], [('Sign in', ('task', 'work:signin:all'))])
                    self.assertEqual(self.keys(frame)[:4], ['connect', 'arm', 'stop', 'signin'])
                else:
                    self.assertEqual(signin, [])
        for names, text in ((('Plaud', 'Codex'), 'Plaud and Codex are not signed in'),
                            (('Plaud', 'Codex', 'Claude'), 'Plaud, Codex and Claude are not signed in')):
            self.assertEqual(self.rows(self.frame(replace(self.local, signed_out=names)), 'Sign-ins'),
                             ['Sign-ins   ' + text])
        self.assertEqual(self.rows(self.frame(), 'Sign-ins'), [])
        unpaired = self.frame(replace(self.local, pairing=None, signed_out=('Plaud',)))
        self.assertNotIn('signin', self.keys(unpaired))

    def test_history_row_waits_five_minutes(self):
        self.assertEqual(self.rows(self.frame(replace(self.local, history_pending_since=self.now - 299)),
                                   'History'), [])
        self.assertEqual(self.rows(self.frame(replace(self.local, history_pending_since=self.now - 301)),
                                   'History'), ['History    not synced yet, 5 min so far'])
        refused = replace(self.local, history_pending_since=self.now - 3600,
                          refusals=(Refusal('codex', 'session-1', 'refused'),))
        self.assertEqual(self.rows(self.frame(refused), 'History'),
                         ['History    1 conversation refused; forget it to clear the sync warning'])

    def test_untrusted_text_never_reaches_the_terminal(self):
        unsafe_categories = {'Cc', 'Cf', 'Zl', 'Zp'}
        names = ('cs\x1b[2J', 'cs\nname', 'cs‮name', 'コードスペース' * 15,
                 'cs👩‍💻', 'cs name', 'cs name')
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
                self.assertNotIn('update-checkout', self.keys(frame))
                problem, = self.rows(frame, 'Problem')
                self.assertTrue(problem.startswith('Problem    an action was withheld for unsafe'), problem)
                self.assertTrue(problem.endswith(' (+1 more); Show details has more'), problem)
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
                self.assertFalse(any('Codespace  deleted' in line for line in self.frame(remote=remote).lines))
        local = replace(self.local, gh_user=None)
        frame = self.frame(local, replace(self.remote, names=self.ok(frozenset()), spaces=self.ok(())))
        self.assertIn('not listed for', frame.lines[1])

    def test_old_owner_stop_does_not_label_a_later_run(self):
        for kind in (None, 'OWNER', 'LIFETIME', 'OTHER'):
            local = replace(self.local, owner_stop_at=self.at, down_kind=kind,
                            down_at=self.at if kind else None)
            for source in ('local', 'remote'):
                newer = replace(local, started_at=self.at + 1) if source == 'local' else local
                remote = self.remote_state('Shutdown', last_used_at=self.at + 1 if source == 'remote' else None)
                with self.subTest(kind=kind, source=source):
                    self.assertEqual(self.frame(newer, remote).lines[1], 'Codespace  stopped (checked 09:10)')

    def test_asynchronous_stop_is_still_yours(self):
        local = replace(self.local, owner_stop_at=self.at, last_up_at=self.at + 10)
        remote = replace(self.remote_state('Shutdown', last_used_at=self.at - 10),
                         spaces=self.ok((Space(CS, 'Shutdown', self.at - 10),), self.at + 20))
        self.assertEqual(self.frame(local, remote).lines[1], 'Codespace  stopped; you stopped it at 09:10')

    def test_several_setup_prs_offer_each_pull_request(self):
        frame = self.frame(remote=replace(self.remote, setup_prs=self.ok((9, 7))))
        self.assert_actions(frame, [('gh', 'pr', 'view', '7', '--web'),
                                    ('gh', 'pr', 'view', '9', '--web'), ('task', 'work:status')])
        many = self.frame(remote=replace(self.remote, setup_prs=self.ok((11, 9, 7, 10))))
        self.assertEqual(self.keys(many), ['pr:7', 'pr:9', 'pr:10', 'details'])

    def test_cached_state_says_as_of(self):
        for dt in (self.today.replace(hour=9, minute=10),
                   self.today.replace(hour=9, minute=10) - timedelta(days=1)):
            stamp = dt.strftime('%H:%M' if dt.date() == self.today.date() else '%Y-%m-%d %H:%M')
            for raw, text in (('Available', 'available'), ('Shutdown', 'stopped'),
                              ('Starting', 'starting'), ('ShuttingDown', 'stopping')):
                frame = self.frame(replace(self.local, reading=(raw, dt.timestamp())), self.unavailable())
                self.assertEqual(frame.lines[1], f'Codespace  {text} (as of {stamp})')
                self.assertNotIn('checked', frame.lines[1])

    def test_newer_sighting_overrides_the_keepers_deleted(self):
        local = replace(self.local, deleted_at=self.at - 1)
        self.assertIn('available', self.frame(local).lines[1])
        elsewhere = self.frame(local, replace(self.remote, spaces=self.ok(())))
        self.assertIn('another repository', elsewhere.lines[1])
        self.assertFalse(any('Codespace  deleted' in line for line in elsewhere.lines))

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
        self.assertNotIn('upgrade', self.keys(self.frame(source, replace(self.remote, upstream=self.ok('b' * 40)))))

    def test_unmapped_codespace_state_is_other(self):
        for raw in ('Moved', 'Created', 'Unknown', 'FutureState'):
            frame = self.frame(remote=self.remote_state(raw))
            self.assertEqual(frame.lines[1], f'Codespace  in state {raw} (checked 09:10)')
            self.assertEqual(frame.actions[0].key, 'connect')

    def test_forgotten_refusals_are_not_shown(self):
        refusals = (Refusal('codex', 'session-1', 'bad'), Refusal('claude', 'session-2', 'bad'),
                    Refusal('codex', 'session-3', 'bad'))
        local = replace(self.local, refusals=refusals)
        frame = self.frame(local, forgotten=frozenset((('codex', 'session-1'),)))
        self.assertIn('History    2 conversations refused; forget them to clear the sync warning', frame.lines)
        self.assertNotIn('forget:codex:session-1', self.keys(frame))
        # Replace keeps its reserved slot, leaving the first remaining forget action above it.
        self.assertEqual(self.keys(frame), ['connect', 'arm', 'stop', 'forget:claude:session-2', 'replace', 'details'])
        cleared = self.frame(local, forgotten=frozenset((r.agent, r.session_id) for r in refusals))
        self.assertFalse(any('refused' in line for line in cleared.lines))
        self.assertFalse(any(a.key.startswith('forget:') for a in cleared.actions))

    def test_pending_remote_reads_checking(self):
        frame = self.overview.view(self.local, None, now=self.now)
        self.assertEqual(frame.lines[1], 'Codespace  checking GitHub…')
        self.assertNotIn('unknown', ' '.join(frame.lines))
        cached = self.overview.view(replace(self.local, reading=('Available', self.at)), None, now=self.now)
        self.assertEqual(cached.lines[1], 'Codespace  available (as of 09:10)')
        deleted = self.overview.view(replace(self.local, deleted_at=self.at, armed=True, keeper='stopped'),
                                     None, now=self.now)
        self.assertEqual(deleted.lines[1], 'Codespace  deleted (found by Recovery 09:10)')
        self.assertFalse(any(line.startswith('Recovery') for line in deleted.lines))

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
                  Space(CS, 'Deleted', self.at + 100), Space('new-cs', 'Available', self.at),
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
            TITLE,
            'Problem    arming.json cannot be read; Show details has more',
            'Codespace  available (checked 09:10)',
            'Recovery   not answering for 3 min; connect to restart it',
            'Sync       stopped; connect to start it',
            'Checkout   2 commits behind origin/main (local copy from 09:10)'))
        self.assertIn(('task', 'devcontainer:upgrade:self'), [a.argv for a in frame.actions])
        generator = self.frame(replace(self.local, cycle_at=self.at), remote)
        self.assertIn('Generator  upstream is at bbbbbbb; this project uses aaaaaaa', generator.lines)
        held = self.frame(replace(self.local, armed=True, hold_until=self.now + 3600, cycle_at=self.at))
        self.assertIn('Recovery   armed; on hold until 13:00', held.lines)
        self.assertIn('Sync       last completed 09:10', held.lines)
        expired = self.frame(replace(self.local, armed=True, hold_until=self.at))
        self.assertIn('Recovery   armed; next 12-hour stop not known yet', expired.lines)
        self.assertNotIn('upgrade', self.keys(self.frame(replace(self.local, setup='missing'), remote)))
        diverged = self.frame(replace(self.local, checkout=replace(self.local.checkout, relation='diverged')))
        self.assertIn('Checkout   diverged from origin/main; review before merging', diverged.lines)
        self.assertNotIn('update-checkout', self.keys(diverged))
        other = self.frame(replace(self.local, checkout=replace(self.local.checkout, branch='topic', relation='behind')))
        self.assertNotIn('update-checkout', self.keys(other))

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
