"""Replacement checks prove sealed conversation bytes using read-only cycle evidence."""
import copy
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync import (arming, forget, keeper, replication, replacing,
                                            restore, scratch, seal, sync_cycle, target_scan, verdict)
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.state import Project, atomic_write, read_json, runtime_id
from tests.test_replacing import journal
from tests.test_verdict import SID, example_scan

PARENT = '22222222-2222-4222-8222-222222222222'


def seal_snapshot(project, store, snapshot, *, operation='012345abcdef', now=None):
    """Use the real seal writer with a scan bound to every captured prefix."""
    now = time.time() if now is None else now
    manifest = store.load(snapshot)
    cutoffs = verdict.cutoffs(manifest)
    sessions = tuple(sorted((agent + '/sessions/' + name if agent == 'codex' else
                             agent + '/projects/fixture/' + name, cutoff.sha256)
                            for (agent, name), cutoff in cutoffs.items()))
    scan = replace(example_scan(), codespace='old-space', at=now, scratch=(), sessions=sessions,
                   cutoffs_sha256=target_scan.cutoffs_digest(cutoffs), checked=tuple(sorted(cutoffs)))
    receipt = dict(snapshot_id=snapshot, received_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
    inbound = sync_cycle.direction()
    inbound.update(outcome='complete', snapshot_id=snapshot, receipt=receipt)
    cycle = dict(schema=3, outcome='complete', started_at=now - 1, completed_at=now,
                 directions=dict(codespace_to_desktop=inbound), issues=[])
    evidence = verdict.Evidence(scan, True, (), (), (), True, cycle, receipt,
                                verdict.sessions_by_file(manifest), None, ())
    seal.write(store.state, store, scan, evidence=evidence, identity=project.identity,
               repository=project.identity.removeprefix('github.com/'), gh_user='octo',
               remote_root=manifest['project']['root'], operation=operation,
               cycle_id='sealed-cycle', snapshot_id=snapshot, now=now)


def write_context(project, state, *, operation='012345abcdef'):
    replacing.write(state, journal(operation=operation, phase='moved', at={'moved': time.time()},
                                   repository=project.identity.removeprefix('github.com/'),
                                   remote_root=str(project.root)))
    for name, body in [('scratch.json', dict(version=1, identity=project.identity, root=str(project.root),
                                            remote_root=str(project.root), codespace='new-space')),
                       ('herdr-machine.json', dict(machine_id='fixture-machine', codespace='new-space'))]:
        atomic_write(state / name, json.dumps(body).encode())


def check_cycle(project, state, cycle, checker, *, start=None, wait=0, reachable=True):
    """Only process/Mutagen/Herdr observations are faked; evidence and stores are real."""
    start = cycle['started_at'] - .1 if start is None else start
    clock = [start]
    runtime = dict(running=True, phase='syncing', checkpoint_version=sync_cycle.SCHEMA,
                   runtime_id=scratch._runtime_id(), heartbeat_at=cycle['completed_at'], conversations=cycle,
                   binding=read_json(state / scratch.CONFIG))
    atomic_write(state / scratch.RUNTIME, json.dumps(runtime).encode())
    output = io.StringIO()
    with ExitStack() as stack:
        stack.enter_context(patch.object(scratch, '_owned', return_value=True))
        stack.enter_context(patch.object(scratch, 'inspect_sync', return_value=dict(phase='synchronized', conflicts=[])))
        stack.enter_context(patch.object(restore.Herdr, 'reachable', return_value=reachable))
        stack.enter_context(redirect_stdout(output))
        code = checker.run(project, state, operation='012345abcdef', receipt=state / 'check.json',
                           now=lambda: clock[0], sleep=lambda delay: clock.__setitem__(0, clock[0] + delay), wait=wait)
    return code, read_json(state / 'check.json'), output.getvalue()


class ReplaceCheckTests(unittest.TestCase):
    def setUp(self):
        module = 'devcontainer_setup.project_sync.replace_check'
        self.assertIsNotNone(importlib.util.find_spec(module), 'replacement check is missing')
        self.checker = importlib.import_module(module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / 'state'
        identity = 'github.com/example/project'
        self.project = Project(self.base / 'project', 'https://' + identity, identity,
                               hashlib.sha256(identity.encode()).hexdigest()[:24])
        self.project.root.mkdir()
        self.store = Store(self.state)
        self.entry = self.entry_for(b'completed\n')
        self.snapshot = self.save([self.entry])
        seal_snapshot(self.project, self.store, self.snapshot)
        write_context(self.project, self.state)
        self.cycle = self.make_cycle([self.entry])

    def entry_for(self, data, *, parent=None):
        def file(sid, body):
            digest = self.store.put(body)
            return dict(session_id=sid, path='rollout-' + sid + '.jsonl',
                        size=len(body), sha256=digest, blob=digest)
        files = [file(SID, data)]
        if parent is not None:
            files.insert(0, file(PARENT, parent))
        return dict(agent='codex', session_id=SID, files=files)

    def save(self, entries):
        return self.store.save(dict(schema=2, project=dict(identity=self.project.identity,
            project_id=self.project.project_id, root=str(self.project.root)),
            producer=dict(format='devcontainer-project-conversations-v2'),
            conversations=dict(schema=1, entries=entries, issues=[])))

    def make_cycle(self, entries, *, issues=(), imported=True):
        snapshot = self.save(entries)
        self.store.acknowledge(self.snapshot, source='codespace')
        inbound = sync_cycle.direction()
        inbound.update(outcome='complete', snapshot_id=self.snapshot, receipt=self.store.latest('codespace'))
        outbound = sync_cycle.direction()
        outbound.update(snapshot_id=snapshot, receipt=dict(snapshot_id=snapshot,
            received_at=datetime.now(timezone.utc).isoformat()))
        publication = dict(imported=[dict(agent=e['agent'], source_session_id=e['session_id']) for e in entries]
                           if imported else [], unchanged=[], issues=list(issues))
        sync_cycle.publication_result(outbound, self.store.load(snapshot), publication)
        cycle = sync_cycle.begin(self.project, str(self.project.root))
        cycle.update(completed_at=time.time(), outcome='degraded' if issues else 'complete', stage='complete',
                     directions=dict(codespace_to_desktop=inbound, desktop_to_codespace=outbound),
                     issues=[dict(i, direction='desktop_to_codespace') for i in issues])
        return cycle

    def check(self, cycle=None, **options):
        return check_cycle(self.project, self.state, cycle or self.cycle, self.checker, **options)

    def test_old_conversations_on_new_by_content(self):
        code, result, output = self.check()
        self.assertEqual((code, result['ok'], result['sync']), (0, True, 'ok'))
        self.assertEqual(result['conversations']['on_new'], [['codex', SID]])
        self.assertIn('  History   1 conversation from old-space is on new-space', output)
        self.assertEqual(set(result), {'schema', 'operation', 'step', 'codespace', 'checked_at', 'sync',
            'cycle_completed_at', 'recovery', 'herdr', 'conversations', 'ok'})
        self.assertEqual(stat.S_IMODE((self.state / 'check.json').stat().st_mode), 0o600)

    def test_history_copy_agrees_with_the_conversation_count(self):
        verbs = dict(on_new=('is on new-space', 'are on new-space'),
                     missing=('is not on new-space', 'are not on new-space'),
                     pending=('is still being published', 'are still being published'),
                     diverged=('has a different version on this Mac', 'have a different version on this Mac'))
        identities = [('codex', 'first'), ('claude', 'second')]
        for count in (0, 1, 2):
            for category in verbs:
                if count == 0 and category != 'on_new':
                    continue
                with self.subTest(count=count, category=category):
                    conversations = {name: [] for name in verbs}
                    conversations[category] = identities[:count]
                    noun = 'conversation' if count == 1 else 'conversations'
                    verb = verbs[category][0 if count == 1 else 1]
                    expected = f'{count} {noun} from old-space {verb}'
                    if category != 'on_new':
                        expected = 'Not ready: ' + expected
                        if category != 'pending':
                            expected += ' (' + ', '.join(' '.join(key) for key in identities[:count]) + ')'
                    self.assertEqual(self.checker._history_text(conversations, 'old-space', 'new-space', None), expected)

    def test_longer_revision_counts(self):
        code, result, _ = self.check(self.make_cycle([self.entry_for(b'completed\nnext turn\n')]))
        self.assertEqual(code, 0)
        self.assertEqual(result['conversations']['on_new'], [['codex', SID]])

    def test_equal_count_different_content_is_missing(self):
        code, result, _ = self.check(self.make_cycle([self.entry_for(b'different\n')]))
        self.assertEqual(code, 1)
        self.assertEqual(result['conversations']['missing'], [['codex', SID]])

    def test_a_changed_codex_ancestor_is_not_on_new(self):
        self.entry = self.entry_for(b'completed\n', parent=b'ancestor A\n')
        self.snapshot = self.save([self.entry])
        seal_snapshot(self.project, self.store, self.snapshot)
        changed = self.entry_for(b'completed\n', parent=b'ancestor B\n')
        code, result, _ = self.check(self.make_cycle([changed]))
        self.assertEqual(code, 1)
        self.assertEqual(result['conversations']['missing'], [['codex', SID]])

    def test_an_obsolete_prefix_does_not_block(self):
        old = self.entry_for(b'completed\n')
        self.store.acknowledge(self.save([old]), source='codespace')
        newer = self.entry_for(b'completed\nnext turn\n')
        # Interrupt the actual receive's unlink after publishing the longer relay record.
        snapshot = self.save([newer])
        self.snapshot = snapshot
        seal_snapshot(self.project, self.store, snapshot)
        older_records = {path.name for path in (self.store.root / 'relay').glob('*.json')}
        from devcontainer_setup.project_sync import checkpoint_store
        unlink = checkpoint_store.os.unlink
        def interrupted(name, **kwargs):
            if name in older_records:
                raise OSError('fixture crash before retirement')
            return unlink(name, **kwargs)
        with patch.object(checkpoint_store.os, 'unlink', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.acknowledge(snapshot, source='codespace')
        entries, issues = self.store.relay_entries()
        self.assertEqual(issues, [])
        self.assertEqual(len(entries), 2)
        code, result, _ = self.check(self.make_cycle(entries))
        self.assertEqual(code, 0)
        self.assertEqual(result['conversations']['on_new'], [['codex', SID]])

    def test_incompatible_content_is_diverged(self):
        code, result, _ = self.check(self.make_cycle([self.entry, self.entry_for(b'incompatible\n')]))
        self.assertEqual(code, 1)
        self.assertEqual(result['conversations']['diverged'], [['codex', SID]])

    def test_a_real_refusal_is_kept_and_does_not_fail(self):
        from tests.test_replication import ReplicationTests
        fixture = ReplicationTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        code, result, output = fixture.replacement_relay(refuse=True)
        self.assertEqual((code, result['sync']), (0, 'ok'))
        self.assertTrue(result['conversations']['kept'])
        self.assertIn('Kept on this Mac only:', output)

    def test_a_refused_carrying_entry_is_kept_even_if_a_prefix_published(self):
        from devcontainer_setup.project_sync import conversations
        from tests.test_replication import ReplicationTests
        fixture = ReplicationTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        sid, path = fixture.conversation(fixture.local_env)
        first = replication.capture(fixture.project, fixture.store, env=fixture.local_env)
        short = fixture.store.load(first)['conversations']['entries'][0]
        row = dict(type='assistant', uuid='33333333-3333-4333-8333-333333333333',
                   sessionId=sid, cwd=str(fixture.root), version='2.1.302',
                   message=dict(role='assistant', content='Next', stop_reason='end_turn'))
        with path.open('ab') as output:
            output.write(json.dumps(row).encode() + b'\n')
        later = replication.capture(fixture.project, fixture.store, env=fixture.local_env)
        long = fixture.store.load(later)['conversations']['entries'][0]
        seal_snapshot(fixture.project, fixture.store, later)
        manifest = fixture.store.load(later)
        manifest['conversations']['entries'] = [short, long]
        outbound = fixture.store.save(manifest)
        destination = dict(fixture.local_env, CLAUDE_CONFIG_DIR=str(fixture.base / 'new-claude'))
        publish = conversations._publish_conversation
        def refuse_long(project, store, entry, **options):
            if entry == long:
                raise ValueError('fixture carrying revision refused')
            return publish(project, store, entry, **options)
        with patch.object(conversations, '_publish_conversation', side_effect=refuse_long):
            publication = conversations.publish_conversations(fixture.project, fixture.store, outbound,
                state=fixture.base / 'new-state', env=destination)
        self.assertEqual(len(publication['imported']), 1)
        self.assertEqual(len(publication['issues']), 1)
        fixture.store.acknowledge(later, source='codespace')
        inbound = sync_cycle.direction()
        inbound.update(snapshot_id=later, receipt=fixture.store.latest('codespace'), outcome='complete')
        row = sync_cycle.direction()
        row.update(snapshot_id=outbound, receipt=dict(snapshot_id=outbound, received_at=datetime.now(timezone.utc).isoformat()))
        sync_cycle.publication_result(row, manifest, publication)
        cycle = sync_cycle.begin(fixture.project, str(fixture.root))
        cycle.update(completed_at=time.time(), directions=dict(codespace_to_desktop=inbound, desktop_to_codespace=row),
                     issues=[dict(i, direction='desktop_to_codespace') for i in row['publication_issues']])
        write_context(fixture.project, fixture.store.state)
        code, result, _ = check_cycle(fixture.project, fixture.store.state, cycle, self.checker)
        self.assertEqual(code, 0)
        self.assertEqual(result['conversations']['kept'], [['claude', sid]])
        self.assertEqual(result['conversations']['on_new'], [])

    def test_any_other_cycle_issue_blocks_sync(self):
        for issue in (dict(component='memory', reason='failed'), dict(reason='remote memory failed'),
                      dict(agent='codex', session_id=SID, direction='codespace_to_desktop', reason='refused'),
                      dict(agent='codex', session_id=SID, direction='desktop_to_codespace', stage='capture', reason='failed'),
                      dict(direction='desktop_to_codespace', stage='transfer', reason='failed')):
            cycle = copy.deepcopy(self.cycle)
            cycle['issues'] = [issue]
            with self.subTest(issue=issue):
                code, result, _ = self.check(cycle)
                self.assertEqual(code, 1)
                self.assertEqual(result['sync'], 'the last cycle on new-space did not complete')

    def test_a_mac_only_refusal_does_not_block(self):
        issue = dict(agent='claude', session_id=PARENT, reason='refused')
        code, result, output = self.check(self.make_cycle([self.entry], issues=[issue]))
        self.assertEqual(code, 0)
        self.assertEqual(result['conversations']['kept_mac_only'], [['claude', PARENT]])
        self.assertIn('Kept on this Mac only: claude ' + PARENT, output)

    def test_forgotten_is_named_and_does_not_fail(self):
        atomic_write(self.state / forget.FILE, json.dumps(dict(schema=1, conversations=[dict(agent='codex',
            session_id=SID, codespace='new-space', reason='refused', forgotten_at=time.time())])).encode())
        code, result, _ = self.check(self.make_cycle([]))
        self.assertEqual(code, 0)
        self.assertEqual(result['conversations']['forgotten'], [['codex', SID]])

    def test_carried_but_unpublished_is_pending(self):
        code, result, _ = self.check(self.make_cycle([self.entry], imported=False))
        self.assertEqual(code, 1)
        self.assertEqual(result['conversations']['pending'], [['codex', SID]])

    def test_an_unbound_receipt_fails_sync(self):
        for direction in sync_cycle.DIRECTIONS:
            cycle = copy.deepcopy(self.cycle)
            cycle['directions'][direction]['receipt']['snapshot_id'] = 'f' * 64
            with self.subTest(direction=direction):
                code, result, _ = self.check(cycle)
                self.assertEqual(code, 1)
                self.assertNotEqual(result['sync'], 'ok')

    def test_cycle_must_start_after_the_check(self):
        code, result, output = self.check(start=self.cycle['completed_at'] + 1, wait=360)
        self.assertEqual(code, 1)
        self.assertEqual(result['cycle_completed_at'], None)
        self.assertEqual(result['sync'], 'no sync cycle completed in 6 minutes')
        self.assertIn('Not ready: no sync cycle completed in 6 minutes', output)

    def test_armed_needs_new_keeper(self):
        current = dict(running=True, codespace='new-space', runtime_id=runtime_id())
        for armed_space, runtime, expected in [('old-space', current, 1),
                ('new-space', dict(current, codespace='old-space'), 1),
                ('new-space', dict(current, runtime_id='old'), 1),
                ('new-space', dict(current, running=False), 1), ('new-space', current, 0)]:
            arming.write(self.state, dict(schema=1, armed=True, codespace=armed_space))
            with self.subTest(armed=armed_space, runtime=runtime), patch.object(keeper, 'status', return_value=runtime):
                code, result, _ = self.check()
                self.assertEqual(code, expected)
                self.assertEqual(result['recovery'], 'ok' if expected == 0 else
                                 'recovery still points at old-space' if armed_space == 'old-space' else
                                 'its keeper is not running')

    def test_herdr_must_be_reachable(self):
        for codespace, reachable in [('old-space', True), ('new-space', False)]:
            atomic_write(self.state / 'herdr-machine.json', json.dumps(dict(machine_id='fixture-machine', codespace=codespace)).encode())
            code, result, _ = self.check(reachable=reachable)
            self.assertEqual(code, 1)
            self.assertEqual(result['herdr'], 'Herdr cannot reach new-space')

    def test_a_seal_from_another_operation_is_not_read(self):
        path = self.state / 'retired/old-space.json'
        value = read_json(path)
        value['operation'] = 'fedcba654321'
        atomic_write(path, json.dumps(value).encode())
        code, result, output = self.check()
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertIn('Not ready: the final flush of old-space cannot be read', output)

    def test_check_writes_nothing_but_its_receipt(self):
        # Establish the worker observation before the baseline byte inventory.
        self.check()
        before = {p.relative_to(self.state): p.read_bytes() for p in self.state.rglob('*') if p.is_file() and p.name != 'check.json'}
        code, _, _ = self.check()
        after = {p.relative_to(self.state): p.read_bytes() for p in self.state.rglob('*') if p.is_file() and p.name != 'check.json'}
        self.assertEqual(code, 0)
        self.assertEqual(before, after)

    def test_rebinding_during_the_check_never_reports_ready(self):
        from devcontainer_setup.project_sync import workspace
        scope = workspace.cycle_scope
        def rebound(*args):
            result = scope(*args)
            binding = read_json(self.state / scratch.CONFIG)
            binding['codespace'] = 'third-space'
            atomic_write(self.state / scratch.CONFIG, json.dumps(binding).encode())
            return result
        with patch.object(workspace, 'cycle_scope', side_effect=rebound):
            code, result, _ = self.check()
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])

    def test_recovery_mismatch_never_stores_untrusted_text(self):
        unsafe = 'https://example.invalid/raw-output'
        arming.write(self.state, dict(schema=1, armed=True, codespace=unsafe))
        code, result, output = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(result['recovery'], 'recovery still points at old-space')
        self.assertNotIn(unsafe, output)
        self.assertNotIn(unsafe.encode(), (self.state / 'check.json').read_bytes())

    def test_mixed_case_repository_binds_but_another_repository_is_refused(self):
        import subprocess
        from devcontainer_setup.project_sync.state import discover_project
        origin = 'https://github.com/Example/Project.git'
        for args in [('init', '-q'), ('remote', 'add', 'origin', origin)]:
            subprocess.run(['git', '-C', str(self.project.root), *args], check=True, capture_output=True)
        project = discover_project(self.project.root)
        self.assertEqual(project.remote, origin)
        self.assertEqual(project.identity, 'github.com/example/project')
        mismatch = 'This replace record does not match; run the replace again.'
        for repository, matches in [('Example/Other', False), ('Example/Project', True)]:
            with self.subTest(repository=repository):
                record = dict(replacing.read(self.state), repository=repository)
                replacing.write(self.state, record)
                output = io.StringIO()
                with patch.object(self.checker, '_observe', return_value=None) as observed, \
                        patch.object(self.checker, '_herdr', return_value='ok'), redirect_stdout(output):
                    code = self.checker.run(project, self.state, operation=record['operation'],
                                            receipt=self.state / 'case-check.json', wait=0)
                self.assertEqual(observed.call_count, 1 if matches else 0)
                self.assertEqual(replacing.read(self.state)['repository'], repository)
                if matches:
                    self.assertNotIn(mismatch, output.getvalue())
                    self.assertEqual(read_json(self.state / 'case-check.json')['sync'],
                                     'no sync cycle completed in 6 minutes')
                else:
                    self.assertEqual(code, 1)
                    self.assertEqual(output.getvalue(), mismatch + '\n')
