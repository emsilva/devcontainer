"""Offline judgments from scanner, receiver and push-proof observations."""
import copy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync import overview, pushed, scratch, sync_cycle, target_scan, workspace
from devcontainer_setup.project_sync.state import Project, atomic_write
from devcontainer_setup.project_sync.standing import Local, Refusal


IDENTITY = 'github.com/example/project'
PROJECT_ID = hashlib.sha256(IDENTITY.encode()).hexdigest()[:24]
NOW = datetime.now().replace(hour=11, minute=44, second=0, microsecond=0).timestamp()
SID = '11111111-1111-4111-8111-111111111111'
NAME = 'rollout-' + SID + '.jsonl'
DIGEST = hashlib.sha256(b'completed\n').hexdigest()


def example_scan(**updates):
    tree = target_scan.Worktree('/workspaces/project', 'a' * 40, 'main', 0, (), True)
    repo = target_scan.Repository(tree.path, IDENTITY, (('main', 'a' * 40),), 0, (tree,), False, True)
    scan = target_scan.Scan('space', NOW, (repo,), True, (('note.txt', DIGEST),), True,
                           (('codex/sessions/' + NAME, DIGEST),), True,
                           target_scan.cutoffs_digest({('codex', NAME): target_scan.Cutoff(10, DIGEST, True)}),
                           (('codex', NAME),), (), (),
                           (('claude', 0), ('codex', 0), ('grok', 0), ('herdr', 1), ('opencode', 0)), True, ())
    return replace(scan, **updates)


def manifest(store, data=b'completed\n', *, identity=IDENTITY):
    digest = store.put(data)
    return dict(schema=2, project=dict(identity=identity, project_id=hashlib.sha256(identity.encode()).hexdigest()[:24],
                                      root='/workspaces/project'),
                conversations=dict(schema=1, entries=[dict(agent='codex', session_id=SID,
                    files=[dict(session_id=SID, path=NAME, size=len(data), sha256=digest, blob=digest)])], issues=[]),
                producer=dict(format='devcontainer-project-conversations-v2'))


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.v = importlib.import_module('devcontainer_setup.project_sync.verdict')
        self.seals = importlib.import_module('devcontainer_setup.project_sync.seal')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / 'state'
        self.project = Project(self.base / 'project', 'https://' + IDENTITY, IDENTITY, PROJECT_ID)
        self.local = Local(self.project.root)
        self.target = self.v.Target('space', 'paired', '/workspaces/project', None, None)
        self.receipt = dict(snapshot_id='b' * 64, received_at=datetime.now(timezone.utc).isoformat())
        inbound = sync_cycle.direction()
        inbound.update(outcome='complete', snapshot_id='b' * 64, receipt=self.receipt)
        self.cycle = dict(schema=3, cycle_id='cycle', outcome='complete', started_at=NOW - 1,
                          completed_at=NOW, stage='complete', issues=[],
                          directions=dict(codespace_to_desktop=inbound, desktop_to_codespace=sync_cycle.direction()))
        self.evidence = self.v.Evidence(example_scan(), True,
            (pushed.Proof('/workspaces/project', 1, (), (), False),), (('note.txt', DIGEST),), (), True,
            self.cycle, self.receipt, (('codex', NAME, SID),), None, ())

    def judge(self, category, evidence=None, target=None):
        return next(row for row in self.v.judge(target or self.target, evidence or self.evidence, now=NOW)
                    if row.name == category)

    def result(self, target=None, evidence=None, **updates):
        target, evidence = target or self.target, evidence or self.evidence
        return self.v.Verdict(target, NOW, 'octo', 'example/project',
                              self.v.judge(target, evidence, now=NOW), evidence.kept,
                              target_scan.fingerprint(evidence.scan), None, **updates)

    def write(self, path, value):
        atomic_write(path, json.dumps(value).encode())

    def test_wrap_keeps_the_label_and_first_column_for_long_first_words(self):
        for message, prefix in [('  Git      /workspaces/' + 'r' * 90 + ' has 2 commits not on GitHub', '  Git      '),
                                ('界' * 60 + ' has more text', '界')]:
            with self.subTest(prefix=prefix):
                lines = self.v.wrap(message)
                self.assertTrue(lines[0].startswith(prefix), lines)
                self.assertTrue(all(overview.cells(line) <= 78 for line in lines), lines)
                self.assertEqual(''.join(lines).replace(' ', ''), message.replace(' ', ''))
                indent = ' ' * (11 if prefix.startswith(' ') else 2)
                self.assertTrue(all(line.startswith(indent) for line in lines[1:]), lines)

    def seal(self):
        return self.seals.Seal('space', IDENTITY, 'example/project', 'octo', '/workspaces/project',
                               'replace-1', NOW, 'cycle', 'b' * 64,
                               self.evidence.scan.scratch, self.evidence.scan.sessions)

    def test_resolve_kinds(self):
        self.assertEqual(self.v.resolve(self.project, self.state, 'space').kind, 'unpaired')
        with self.assertRaisesRegex(ValueError, 'No Codespace is paired'):
            self.v.resolve(self.project, self.state, None)
        for archive, root in [('100', '/workspaces/old'), ('200', '/workspaces/new')]:
            self.write(self.state / 'retired-bindings' / archive / 'scratch.json',
                       dict(identity=IDENTITY, codespace='space', remote_root=root))
        moved = self.v.resolve(self.project, self.state, 'space')
        self.assertEqual((moved.kind, moved.remote_root, moved.archive.name), ('moved', '/workspaces/new', '200'))
        self.write(self.state / 'retired' / 'space.json', dict(schema=1, **self.seal().__dict__))
        self.assertEqual(self.v.resolve(self.project, self.state, 'space').kind, 'sealed')
        self.assertEqual(self.v.resolve(self.project, self.state, 'space').archive, moved.archive)
        self.write(self.state / scratch.CONFIG, dict(identity=IDENTITY, codespace='space', remote_root='/workspaces/current'))
        self.assertEqual(self.v.resolve(self.project, self.state, None).kind, 'paired')
        self.assertEqual(self.v.resolve(self.project, self.state, 'space').remote_root, '/workspaces/current')
        self.write(self.state / scratch.CONFIG, dict(identity='github.com/other/project', codespace='space'))
        with self.assertRaises(ValueError):
            self.v.resolve(self.project, self.state, 'space')

    def test_dirty_second_worktree_or_stash_is_unsafe(self):
        repo = self.evidence.scan.repositories[0]
        for path in ('/workspaces/second', '/workspaces/.cache/old', '/outside/project'):
            dirty = target_scan.Worktree(path, 'c' * 40, None, 4, ('a', 'b', 'c', 'd'), True)
            scan = replace(self.evidence.scan, repositories=(replace(repo, worktrees=(*repo.worktrees, dirty)),))
            row = self.judge('Git', replace(self.evidence, scan=scan))
            self.assertEqual(row.verdict, self.v.UNSAFE)
            self.assertIn('4 changed files (a, b, c and 1 more)', row.text)
        row = self.judge('Git', replace(self.evidence,
                         scan=replace(self.evidence.scan, repositories=(replace(repo, stashes=1),))))
        self.assertIn('1 stash', row.text)
        claims = self.v.claims(scan)
        self.assertEqual(claims[0].refs, (('main', 'a' * 40), ('HEAD', 'c' * 40)))

    def test_unpushed_commit_is_unsafe_and_unknown_proof_is_unknown(self):
        for proof, expected, text in [
                (pushed.Proof('/workspaces/project', 0, (('topic', 'c' * 12),), (), False), 'unsafe', 'not on GitHub'),
                (pushed.Proof('/workspaces/project', 0, (), (('topic', 'GitHub could not be read from this Mac'),), False),
                 'unknown', 'GitHub could not be read'),
                (pushed.Proof('/workspaces/project', 0, (), (), True), 'unsafe', 'no remote')]:
            with self.subTest(proof=proof):
                row = self.judge('Git', replace(self.evidence, proofs=(proof,)))
                self.assertEqual(row.verdict, expected)
                self.assertIn(text, row.text)
        self.assertEqual(self.judge('Git', replace(self.evidence, proofs=())).verdict, 'unknown')
        repo = self.evidence.scan.repositories[0]
        bad = replace(repo, ok=False)
        self.assertEqual(self.judge('Git', replace(self.evidence,
                         scan=replace(self.evidence.scan, repositories=(bad,)))).verdict, 'unknown')
        dirty = replace(repo.worktrees[0], changes=1, changed=('changed',))
        scan = replace(self.evidence.scan, git_complete=False, repositories=(replace(repo, worktrees=(dirty,)),))
        self.assertEqual(self.judge('Git', replace(self.evidence, scan=scan)).verdict, 'unknown')

    def test_unsteady_evidence_is_unknown_everywhere(self):
        rows = self.v.judge(self.target, replace(self.evidence, steady=False), now=NOW)
        self.assertEqual(tuple(row.name for row in rows), self.v.CATEGORIES)
        self.assertTrue(all(row.verdict == 'unknown' and 'changed while it was being checked' in row.text for row in rows))
        unpaired = replace(self.target, kind='unpaired', remote_root=None)
        self.assertFalse(self.result(target=unpaired).deletable)

    def test_paired_scratch_needs_the_macs_copy(self):
        self.assertEqual(self.judge('Scratch').verdict, 'safe')
        for changes, verdict, text in [
                (dict(mac_scratch=(('note.txt', 'c' * 64),)), 'unsafe', "differs from this Mac's copy"),
                (dict(mac_scratch=None), 'unknown', 'could not read'),
                (dict(conflicts=('note.txt',)), 'unsafe', '1 conflict'),
                (dict(sync_running=False), 'unknown', 'run task work:cs'),
                (dict(mac_scratch=(*self.evidence.mac_scratch, ('mac-only', DIGEST))), 'safe', 'synchronized')]:
            row = self.judge('Scratch', replace(self.evidence, **changes))
            self.assertEqual(row.verdict, verdict)
            self.assertIn(text, row.text)
        self.assertEqual(self.judge('Scratch', replace(self.evidence,
                         scan=replace(self.evidence.scan, scratch_complete=False))).verdict, 'unknown')

    def test_paired_history_reads_only_the_inbound_row(self):
        cycle = copy.deepcopy(self.cycle)
        cycle.update(outcome='degraded', issues=[dict(reason='outbound refusal')])
        cycle['directions']['desktop_to_codespace'].update(outcome='degraded',
            publication_issues=[dict(agent='codex', session_id=SID, reason='refused')])
        row = self.judge('History', replace(self.evidence, cycle=cycle))
        self.assertEqual(row.verdict, 'safe')
        cycle['directions']['desktop_to_codespace'] = None
        self.assertEqual(self.judge('History', replace(self.evidence, cycle=cycle)).verdict, 'safe')
        with patch('devcontainer_setup.project_sync.standing.refusals', return_value=(Refusal('codex', SID, 'refused'),)), \
                patch('devcontainer_setup.project_sync.forget.forgotten', return_value=frozenset({('claude', 'forgotten')})):
            kept = self.v.kept_conversations(self.state, self.project, {'codespace': 'space'})
        self.assertEqual(kept, (('claude', 'forgotten'), ('codex', SID)))
        rendered = self.v.lines(replace(self.result(), kept=kept), self.local)
        self.assertTrue(any(line.startswith('Kept on this Mac only:') for line in rendered))
        for change, expected, text in [
                (dict(counts=dict(captured=1, pending=1)), 'unsafe', 'not reached'),
                (dict(capture_issues=[dict(agent='codex', session_id=SID)]), 'unsafe', 'could not be read'),
                (dict(receipt=None), 'unknown', 'has not finished a cycle'),
                (dict(outcome='pending'), 'unknown', 'has not finished a cycle')]:
            cycle = copy.deepcopy(self.cycle)
            cycle['directions']['codespace_to_desktop'].update(change)
            row = self.judge('History', replace(self.evidence, cycle=cycle))
            self.assertEqual(row.verdict, expected)
            self.assertIn(text, row.text)
        for changes, text in [(dict(cycle=None), 'has not finished'),
                              (dict(cycle=dict(self.cycle, outcome='failed')), 'last sync cycle failed'),
                              (dict(receipt=dict(self.receipt, snapshot_id='c' * 64)), 'has not finished'),
                              (dict(sync_running=False), 'sync is not running')]:
            self.assertIn(text, self.judge('History', replace(self.evidence, **changes)).text)
        cycle = copy.deepcopy(self.cycle)
        cycle['directions']['codespace_to_desktop'].update(
            receipt=None, counts=dict(captured=1, pending=0, available=1))
        self.assertEqual(self.judge('History', replace(self.evidence, cycle=cycle)).verdict, 'unknown')
        incomplete = replace(self.evidence.scan, sessions_complete=False)
        self.assertEqual(self.judge('History', replace(self.evidence, scan=incomplete)).verdict, 'unknown')

    def test_unfinished_turn_or_differing_prefix_is_unsafe_and_named(self):
        for field, phrase in [('tails', 'a turn that never finished'), ('mismatched', "differs from this Mac's copy")]:
            for root in ('sessions', 'archived_sessions'):
                scan = replace(self.evidence.scan, sessions=((f'codex/{root}/' + NAME, DIGEST),),
                               **{field: (('codex', NAME),)})
                row = self.judge('History', replace(self.evidence, scan=scan))
                self.assertEqual(row.verdict, 'unsafe')
                self.assertIn(phrase, row.text)
                self.assertIn('codex ' + SID, row.text)

    def test_cutoffs_read_own_and_dependency_files_one_way(self):
        entry = dict(agent='codex', session_id='child', files=[
            dict(path='child.jsonl', session_id='child', size=20, sha256='a' * 64),
            dict(path='parent.jsonl', session_id='parent', size=10, sha256='b' * 64)])
        manifest = dict(conversations=dict(entries=[entry]))
        self.assertEqual(self.v.cutoffs(manifest), {
            ('codex', 'child.jsonl'): target_scan.Cutoff(20, 'a' * 64, True),
            ('codex', 'parent.jsonl'): target_scan.Cutoff(10, 'b' * 64, False)})
        self.assertEqual(self.v.sessions_by_file(manifest),
                         (('codex', 'child.jsonl', 'child'), ('codex', 'parent.jsonl', 'parent')))
        manifest['conversations']['entries'].append(dict(agent='codex', session_id='parent', files=[
            dict(path='parent.jsonl', session_id='parent', size=30, sha256='c' * 64)]))
        self.assertEqual(self.v.cutoffs(manifest)[('codex', 'parent.jsonl')], target_scan.Cutoff(30, 'c' * 64, True))
        manifest['conversations']['entries'].reverse()
        self.assertEqual(self.v.cutoffs(manifest)[('codex', 'parent.jsonl')], target_scan.Cutoff(30, 'c' * 64, True))
        manifest['conversations']['entries'][0]['files'][0]['size'] = 10
        for _ in range(2):
            self.assertEqual(self.v.cutoffs(manifest)[('codex', 'parent.jsonl')], target_scan.Cutoff(10, 'c' * 64, True))
            manifest['conversations']['entries'].reverse()
        entry['files'][0]['path'] = '../child.jsonl'
        with self.assertRaises(ValueError):
            self.v.cutoffs(manifest)

    def test_json_for_every_outcome(self):
        for outcome in ('checked', 'unreachable', 'cleanup-pending', 'cleaned', 'already-deleted', 'refused'):
            result = self.result()
            if outcome == 'unreachable':
                result = replace(result, categories=self.v.unreachable(self.target, 'space is stopped'), fingerprint=None)
            obj = self.v.as_json(result, outcome=outcome, message='Nothing was deleted.', phase='deleted', cleanup={'pairing': 'done'})
            self.assertEqual(json.loads(json.dumps(obj)), obj)
            self.assertEqual(obj['outcome'], outcome)
            self.assertEqual(obj['phase'], 'deleted')
            self.assertEqual(len(obj['categories']), 4 if outcome in ('checked', 'unreachable') else 0)
            self.assertEqual(obj['message'], None if outcome == 'checked' else 'Nothing was deleted.')
            self.assertEqual(obj['deletable'], outcome == 'checked')

    def test_sealed_late_scratch_or_newer_turn_is_unsafe(self):
        target = replace(self.target, kind='sealed', seal=self.seal())
        evidence = replace(self.evidence, capture_loads=True)
        self.assertTrue(self.result(target, evidence).deletable)
        self.assertEqual(self.judge('Scratch', replace(evidence,
                         scan=replace(evidence.scan, scratch=())), target).verdict, 'safe')
        scan = replace(evidence.scan, scratch=(*evidence.scan.scratch, ('late.txt', DIGEST)))
        self.assertIn('changed or added since the final flush', self.judge('Scratch', replace(evidence, scan=scan), target).text)
        for path in ('codex/sessions/' + NAME, 'codex/archived_sessions/' + NAME):
            scan = replace(evidence.scan, sessions=((path, 'c' * 64),))
            self.assertEqual(self.judge('History', replace(evidence, scan=scan), target).verdict, 'unsafe')

    def test_sealed_lost_capture_is_unknown(self):
        target = replace(self.target, kind='sealed', seal=self.seal())
        for available in (False, None):
            row = self.judge('History', replace(self.evidence, capture_loads=available), target)
            self.assertEqual(row.verdict, 'unknown')
            self.assertIn('final capture is no longer on this Mac', row.text)
        scan = replace(self.evidence.scan, sessions=(('codex/sessions/' + NAME, 'c' * 64),))
        self.assertEqual(self.judge('History', replace(self.evidence, scan=scan, capture_loads=False), target).verdict,
                         'unsafe')

    def test_moved_target_cannot_be_deleted(self):
        result = self.result(target=replace(self.target, kind='moved'))
        self.assertEqual(tuple(row.verdict for row in result.categories), ('safe', 'unknown', 'unknown', 'safe'))
        self.assertFalse(result.deletable)
        self.assertIn('moved with --rebind', result.categories[1].text)

    def test_failed_process_read_is_unknown(self):
        self.assertEqual(self.judge('Agents').items, ('herdr (1)',))
        self.assertEqual(self.v.as_json(self.result())['categories'][3]['items'], ['herdr (1)'])
        row = self.judge('Agents', replace(self.evidence, scan=replace(self.evidence.scan, agents_complete=False)))
        self.assertEqual(row.verdict, 'unknown')
        self.assertIn('could not read processes on space', row.text)
        scan = replace(self.evidence.scan, agents=(('claude', 2), ('codex', 1), ('herdr', 1)))
        row = self.judge('Agents', replace(self.evidence, scan=scan))
        self.assertEqual(row.verdict, 'unsafe')
        self.assertIn('claude (2), codex (1) running: end them, then check again', row.text)

    def test_verdict_lines_match_the_copy(self):
        result = replace(self.result(), kept=(('codex', 'session'), ('claude', 'other')))
        self.assertEqual(self.v.lines(result, self.local), [
            'Checked space at 11:44:',
            '  Git      1 repository, 1 worktree: no changes, stashes or unpushed commits',
            '  Scratch  synchronized at 11:44 (1 file)',
            '  History  nothing waiting to reach this Mac',
            '  Agents   none running',
            'Kept on this Mac only: codex session, claude other',
            'Not checked: files Git ignores, home-folder data outside sync,',
            'Grok and OpenCode conversations.',
            'Type delete to delete it.'])
        sealed = replace(result, target=replace(self.target, kind='sealed', seal=self.seal()),
                         categories=self.v.judge(replace(self.target, kind='sealed', seal=self.seal()),
                                                 replace(self.evidence, capture_loads=True), now=NOW))
        self.assertIn('  Scratch  unchanged since the final flush at 11:44 (1 file)', self.v.lines(sealed, self.local))
        no_repos = replace(self.evidence, scan=replace(self.evidence.scan, repositories=()), proofs=())
        self.assertEqual(self.judge('Git', no_repos).text, 'no repositories under /workspaces')
        unknown = replace(result, categories=self.v.unreachable(self.target, 'space is stopped'),
                          fingerprint=None, action='To check it, restart it: task work:cs:restart')
        self.assertIn('  Git      Unknown: space is stopped', self.v.lines(unknown, self.local))
        self.assertNotIn('Type delete to delete it.', self.v.lines(unknown, self.local))

    def test_long_verdict_lines_wrap_without_losing_items(self):
        texts = (
            'Not safe: project: 2 changed files (docs/plan.md, src/app.py); project: feature/login at 0123456789ab is not on GitHub',
            "Not safe: 2 files differ from this Mac's copy (tmp/report-2026-11-09.md, memory/decisions.md)",
            'Not safe: 1 conversation has a turn that never finished (claude ' + SID +
            '); resume each and let it finish, then check again',
            'Unknown: ' + '界' * 80 + ' e\u0301clair')
        rows = tuple(self.v.Category(name, 'unsafe', text, ()) for name, text in zip(self.v.CATEGORIES, texts))
        value = replace(self.result(), categories=rows,
                        kept=(('codex', 'long-session-' * 12),))
        rendered = self.v.lines(value, self.local)
        self.assertTrue(all(overview.cells(line) <= 78 for line in rendered))
        joined = ' '.join(line.strip() for line in rendered)
        for text in texts[:3]:
            self.assertIn(text, joined)
        self.assertEqual(''.join(rendered).count('界'), 80)
        self.assertIn('long-session-' * 12, ''.join(line.strip() for line in rendered))
        for line in rendered:
            if line.lstrip().startswith(('at 0123456789ab', 'resume each')):
                self.assertTrue(line.startswith(' ' * 11))
        self.assertNotIn('…', joined)

    def test_history_verbs_agree_with_one_conversation(self):
        for field, expected in (('tails', '1 conversation has a turn'),
                                ('mismatched', '1 conversation differs from')):
            scan = replace(self.evidence.scan, **{field: (('codex', NAME),)})
            self.assertIn(expected, self.judge('History', replace(self.evidence, scan=scan)).text)
        inbound = self.cycle['directions']['codespace_to_desktop']
        inbound['counts']['pending'] = 1
        self.assertIn('1 conversation has not reached', self.judge('History').text)
        scan = replace(self.evidence.scan, scratch=(('changed.txt', DIGEST),))
        self.assertIn('1 file differs from', self.judge('Scratch', replace(self.evidence, scan=scan)).text)

    def test_wrapping_preserves_whitespace_inside_paths(self):
        for item in ('tmp/report  final.md', 'tmp/report\u00a0final.md'):
            with self.subTest(item=item):
                message = "  Scratch  Not safe: 2 files differ from this Mac's copy (" + item + \
                          ', memory/decisions-about-a-long-project.md)'
                rendered = self.v.wrap(message)
                self.assertIn(item, ' '.join(line.strip() for line in rendered))
                self.assertTrue(all(overview.cells(line) <= 78 for line in rendered))

    def test_untrusted_names_never_reach_the_terminal_raw(self):
        bad = '\x1b[31munsafe\r\n\u202e'
        repo = self.evidence.scan.repositories[0]
        dirty = replace(repo.worktrees[0], path='/workspaces/' + bad, changes=1, changed=(bad,))
        scan = replace(self.evidence.scan, repositories=(replace(repo, path=dirty.path, worktrees=(dirty,)),))
        evidence = replace(self.evidence, scan=scan,
                           proofs=(pushed.Proof(dirty.path, 0, ((bad, 'c' * 12),), (), False),), kept=(('codex', bad),))
        for line in self.v.lines(self.result(evidence=evidence), self.local):
            self.assertLessEqual(overview.cells(line), 78)
            for char in ('\x1b', '\r', '\n', '\u202e'):
                self.assertNotIn(char, line)
        obj = self.v.as_json(self.result(evidence=evidence))
        for row in obj['categories']:
            self.assertNotIn('\x1b', row['text'])

    def test_json_has_exactly_the_documented_fields_and_no_urls(self):
        obj = self.v.as_json(self.result())
        self.assertEqual(set(obj), {'schema', 'outcome', 'message', 'codespace', 'target', 'checked_at', 'account',
            'repository', 'categories', 'kept_on_this_mac_only', 'not_checked', 'deletable', 'fingerprint', 'phase', 'cleanup'})
        self.assertEqual(tuple(row['name'] for row in obj['categories']), ('git', 'scratch', 'history', 'agents'))
        self.assertEqual(obj['not_checked'], list(self.v.NOT_CHECKED))
        self.assertEqual(obj['checked_at'], datetime.fromtimestamp(NOW, timezone.utc).isoformat())
        self.assertNotIn('https://', json.dumps(obj))
        self.assertNotIn('remote', json.dumps(obj))
        row = self.v.Category('Git', 'unsafe', 'many', tuple(str(i) for i in range(101)))
        self.assertEqual(len(self.v.as_json(replace(self.result(), categories=(row,)))['categories'][0]['items']), 100)

    def test_cycle_scope_matches_status(self):
        value = dict(local_root=str(self.project.root), remote_root='/workspaces/project')
        cycle = dict(self.cycle, project_id=PROJECT_ID, identity=IDENTITY,
                     local_root=str(self.project.root), remote_root=value['remote_root'])
        for valid in (True, False):
            for sync in (dict(phase='synchronized'), dict(phase='waiting')):
                runtime = dict(running=True, conversations=cycle)
                with patch.object(workspace, '_current_worker', return_value=valid), \
                        patch.object(workspace, 'registry', return_value=value), \
                        patch.object(workspace, '_conversation_receipt', return_value=(self.receipt, {})), \
                        patch.object(scratch, 'status', return_value=runtime), \
                        patch.object(scratch, 'inspect_sync', return_value=sync), \
                        patch.object(workspace, 'recovery', return_value={}), \
                        patch('devcontainer_setup.project_sync.agent_hooks.read', return_value={}), \
                        patch('devcontainer_setup.project_sync.env_files.read', return_value={}):
                    expected = sync_cycle.summary(cycle, current=valid and sync['phase'] == 'synchronized',
                                                  local_receipt=self.receipt)
                    expected['retained_receipts'] = dict(codespace_to_desktop=self.receipt)
                    status = workspace.status(self.project, self.state)
                    self.assertEqual(status, dict(workspace=value, worker=runtime, scratch=sync,
                        last_mac_checkpoint=self.receipt, conversations=expected, recovery={}, herdr_hooks={}, env_files={},
                        synchronization='Scratch and project conversations only; Git is managed manually'))
                    self.assertEqual(workspace.cycle_scope(self.project, value, runtime, sync),
                                     (cycle, valid and sync['phase'] == 'synchronized'))
        with patch.object(workspace, '_current_worker', return_value=True):
            for key in ('project_id', 'identity', 'local_root', 'remote_root'):
                runtime = dict(conversations=dict(cycle, **{key: 'other'}))
                self.assertFalse(workspace.cycle_scope(self.project, value, runtime, dict(phase='synchronized'))[1])
