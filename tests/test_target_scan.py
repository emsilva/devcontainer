"""Offline scan proofs use real Git and an isolated, non-writing Python transport."""
from dataclasses import asdict, replace
import hashlib
import importlib
import importlib.util
import json
import os
import shlex
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LocalConnection:
    def __init__(self, home, proc):
        self.home, self.proc = home, proc
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('GIT_', 'CODEX_', 'CLAUDE_', 'GH_', 'GITHUB_'))}
        self.env.update(HOME=str(home), CODEX_HOME=str(home / '.codex'),
                        CLAUDE_CONFIG_DIR=str(home / '.claude'),
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        self.calls, self.raw = [], b''
        self.self_process = False

    def python(self, code, payload=b'', timeout=60):
        self.calls.append((code, json.loads(payload), timeout))
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, cwd=self.home)
        if self.self_process:
            for pid, parent in ((child.pid, os.getpid()), (os.getpid(), 765432), (765432, 0)):
                target = self.proc / str(pid); target.mkdir(exist_ok=True)
                (target / 'stat').write_text(f'{pid} (fixture with ) parentheses) S {parent} 0 0')
                (target / 'cmdline').write_bytes(b'codex\0')
        try:
            self.raw, err = child.communicate(payload, timeout=timeout)
        except BaseException:
            child.kill(); child.communicate(); raise
        if child.returncode:
            raise AssertionError(err.decode(errors='replace'))
        return self.raw


class TargetScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.ws = self.base / 'workspaces'; self.ws.mkdir()
        self.home = self.base / 'home'; self.home.mkdir()
        self.proc = self.base / 'proc'; self.proc.mkdir()
        self.root = self.ws / 'project'; self.root.mkdir()
        self.connection = LocalConnection(self.home, self.proc)

    @property
    def api(self):
        name = 'devcontainer_setup.project_sync.target_scan'
        self.assertIsNotNone(importlib.util.find_spec(name), 'target_scan feature is not implemented')
        return importlib.import_module(name)

    def scan(self, **kwargs):
        return self.api.scan(self.connection, 'fixture-cs', str(self.root),
                             workspaces=str(self.ws), proc=str(self.proc), now=lambda: 123.0, **kwargs)

    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], env=self.connection.env,
                                       stderr=subprocess.DEVNULL).decode().strip()

    def repo(self, relative, bare=False):
        root = self.ws / relative; root.mkdir(parents=True, exist_ok=True)
        self.git(root, 'init', '-q', *(['--bare'] if bare else []))
        if not bare:
            self.git(root, 'config', 'user.name', 'Fixture')
            self.git(root, 'config', 'user.email', 'fixture@example.com')
            (root / 'tracked').write_text('base')
            self.git(root, 'add', '.'); self.git(root, 'commit', '-qm', 'Initial')
        return root

    def session(self, agent, relative, data=b'{}\n'):
        path = self.home / ('.codex' if agent == 'codex' else '.claude/projects') / relative
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        return path

    def process(self, pid, argv, parent=0):
        path = self.proc / str(pid); path.mkdir()
        (path / 'cmdline').write_bytes(argv)
        (path / 'stat').write_text(f'{pid} (fixture) S {parent} 0 0')
        return path

    def cutoff(self, data, own=True):
        return self.api.Cutoff(len(data), hashlib.sha256(data).hexdigest(), own)

    def test_every_repository_and_worktree_under_workspaces_is_found(self):
        self.api
        from devcontainer_setup.project_sync import notes
        root = self.repo('project')
        (root / '.gitignore').write_text('node_modules/\ntmp/\n')
        (root / '.scratch').mkdir()
        (root / '.scratch/.gitignore').write_bytes(notes._IGNORE)
        (root / '.scratch/README.md').write_bytes(notes._README)
        self.git(root, 'add', '.'); self.git(root, 'commit', '-qm', 'Ignore dependencies')
        linked = self.ws / 'linked'; self.git(root, 'worktree', 'add', '-qb', 'linked', str(linked))
        (linked / 'new').write_text('untracked')
        expected = {'project', 'project/nested', '.devcontainer-work/old', 'project/.scratch/repo',
                    'project/tmp/clone', 'project/sub/deep-ignored/hidden', '.cache/manual-project', 'bare'}
        for name in expected - {'project', 'bare'}:
            self.repo(name)
        self.repo('bare', bare=True); self.repo('project/node_modules/hidden')
        with (root / '.gitignore').open('a') as stream:
            stream.write('sub/deep-ignored/\n')
        (root / 'sub/visible.txt').write_text('not ignored')
        (self.ws / '.cache/manual-project/dirty').write_text('local work')
        self.git(root, 'add', '.'); self.git(root, 'commit', '-qm', 'Track nested repositories')
        result = self.scan()
        self.assertTrue(result.git_complete)
        self.assertEqual({str(Path(row.path).relative_to(self.ws)) for row in result.repositories}, expected)
        repository = next(row for row in result.repositories if row.path == str(root))
        self.assertEqual({row.path for row in repository.worktrees}, {str(root), str(linked)})
        self.assertEqual(next(row for row in repository.worktrees if row.path == str(root)).changes, 0)
        cached = next(row for row in result.repositories if row.path.endswith('/.cache/manual-project'))
        self.assertEqual(cached.worktrees[0].changes, 1)
        dirty = next(row for row in repository.worktrees if row.path == str(linked))
        self.assertEqual((dirty.changes, dirty.changed), (1, ('new',)))
        self.assertTrue(next(row for row in result.repositories if row.path.endswith('/bare')).bare)
        self.assertEqual(len(self.connection.calls), 1)
        self.assertEqual(self.connection.calls[0][2], self.api.LIMITS.seconds + 30)
        self.assertTrue(result.scratch_complete)
        self.assertEqual([path for path, _ in result.scratch], ['repo/tracked'])

    def test_ignored_repository_search_has_its_own_entry_budget(self):
        root = self.repo('project')
        (root / '.gitignore').write_text('tmp/\n')
        hidden = root / 'tmp'
        hidden.mkdir()
        for name in ('one', 'two', 'three'):
            (hidden / name).write_text('ignored')
        result = self.scan(limits=replace(self.api.LIMITS, ignored_entries=2))
        self.assertFalse(result.git_complete)
        self.assertIn('could not read every repository under /workspaces', result.problems)
        self.assertTrue(self.scan(limits=replace(self.api.LIMITS, entries=8)).git_complete)

    def test_stash_and_detached_head(self):
        self.api
        root = self.repo('project'); oid = self.git(root, 'rev-parse', 'HEAD')
        (root / 'tracked').write_text('stash'); self.git(root, 'stash', 'push', '-qm', 'Saved')
        self.git(root, 'checkout', '--detach', '-q')
        row = self.scan().repositories[0]
        self.assertEqual(row.stashes, 1)
        self.assertEqual((row.worktrees[0].branch, row.worktrees[0].head), (None, oid))

    def test_prunable_linked_worktree_is_skipped(self):
        root = self.repo('project')
        linked = self.ws / 'linked'
        self.git(root, 'worktree', 'add', '-qb', 'linked', str(linked))
        shutil.rmtree(linked)
        scan = self.scan()
        self.assertTrue(scan.git_complete)
        self.assertTrue(scan.repositories[0].ok)
        self.assertEqual([tree.path for tree in scan.repositories[0].worktrees], [str(root)])

    def test_edits_hidden_by_index_flags_count_once_and_absent_sparse_files_pass(self):
        root = self.repo('project')
        for name in ('skip', 'assume', 'absent', 'unchanged'):
            (root / name).write_text('base')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-qm', 'Index flags')
        self.git(root, 'update-index', '--skip-worktree', 'skip', 'absent', 'unchanged')
        self.git(root, 'update-index', '--assume-unchanged', 'assume')
        for name in ('skip', 'assume', 'tracked'):
            (root / name).write_text('only here')
        (root / 'absent').unlink()
        row = self.scan().repositories[0].worktrees[0]
        self.assertTrue(row.ok)
        self.assertEqual((row.changes, row.changed), (3, ('assume', 'skip', 'tracked')))

    def test_stopped_rebase_with_autostash_is_a_change(self):
        root = self.repo('project')
        (root / 'second').write_text('second')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-qm', 'Second')
        (root / 'tracked').write_text('only in the autostash')
        env = dict(self.connection.env, GIT_SEQUENCE_EDITOR="sed -i.bak -e '1s/^pick/edit/'")
        subprocess.run(['git', '-C', str(root), 'rebase', '-q', '-i', '--autostash', 'HEAD~1'],
                       env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue((root / '.git/rebase-merge/autostash').exists())
        row = self.scan().repositories[0].worktrees[0]
        self.assertTrue(row.ok)
        self.assertEqual(row.changes, 1)

    def test_each_operation_in_progress_is_a_change(self):
        root = self.repo('project')
        for name in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                     'MERGE_AUTOSTASH', 'REBASE_AUTOSTASH', 'CUSTOM_AUTOSTASH'):
            with self.subTest(name=name):
                marker = root / '.git' / name
                marker.mkdir() if name.startswith('rebase-') else marker.write_text(self.git(root, 'rev-parse', 'HEAD') + '\n')
                try:
                    row = self.scan().repositories[0].worktrees[0]
                    self.assertTrue(row.ok)
                    self.assertEqual(row.changes, 1)
                finally:
                    marker.rmdir() if marker.is_dir() else marker.unlink()

    def test_origin_with_credentials_is_unreadable_and_never_leaves(self):
        self.api
        from devcontainer_setup.project_sync.state import _identity
        for name, url in [('project', 'https://user:secret@github.com/o/r.git'),
                          ('https', 'https://github.com/o/r.git'), ('ssh', 'git@github.com:o/r.git')]:
            self.git(self.repo(name), 'remote', 'add', 'origin', url)
        result = self.scan()
        row = next(row for row in result.repositories if row.path == str(self.root))
        self.assertFalse(row.ok); self.assertIsNone(row.identity)
        self.assertTrue(result.git_complete)
        self.assertIn(str(self.root) + ': its origin cannot be read', result.problems)
        self.assertNotIn(b'secret', self.connection.raw)
        for row in result.repositories:
            if row.path != str(self.root):
                self.assertEqual(row.identity, _identity('git@github.com:o/r.git'))

    def test_scratch_map_follows_the_sync_policy(self):
        self.api
        root = self.root / '.scratch'; (root / 'notes').mkdir(parents=True)
        (root / 'README.md').write_text('marker'); (root / 'notes/a.md').write_bytes(b'note')
        (root / 'node_modules').mkdir(); (root / 'node_modules/x').write_text('ignored')
        result = self.scan()
        self.assertTrue(result.scratch_complete)
        self.assertEqual(result.scratch, (('notes/a.md', hashlib.sha256(b'note').hexdigest()),))

    def test_scratch_symlink_makes_only_scratch_incomplete(self):
        self.api
        root = self.root / '.scratch'; root.mkdir(); (root / 'link').symlink_to(self.home)
        result = self.scan()
        self.assertFalse(result.scratch_complete)
        self.assertTrue(result.git_complete and result.sessions_complete and result.agents_complete)

    def test_conversation_files_are_hashed_for_codex_and_claude(self):
        self.api
        keys = [('codex', 'sessions/one.jsonl'), ('codex', 'archived_sessions/two.jsonl'), ('claude', 'project/three.jsonl')]
        for agent, path in keys:
            self.session(agent, path)
        result = self.scan()
        self.assertTrue(result.sessions_complete)
        self.assertEqual(result.sessions, tuple(sorted((agent + '/' + path, hashlib.sha256(b'{}\n').hexdigest()) for agent, path in keys)))

    def test_prefix_digests_are_compared(self):
        self.api
        for name, data in [('equal', b'base'), ('changed', b'Base'), ('short', b'ba'), ('dependency', b'basetail')]:
            self.session('codex', 'sessions/' + name, data)
        cutoffs = {('codex', name): self.cutoff(b'base', own=name != 'dependency')
                   for name in ('equal', 'changed', 'short', 'dependency', 'missing')}
        result = self.scan(cutoffs=cutoffs)
        self.assertEqual(result.checked, tuple(sorted(key for key in cutoffs if key[1] != 'missing')))
        self.assertEqual(result.mismatched, (('codex', 'changed'), ('codex', 'short')))
        self.assertEqual(result.tails, ())

    def test_unfinished_turns_after_the_cutoff(self):
        self.api
        prefix = b'{"type":"assistant","message":{"stop_reason":"end_turn"}}\n'
        metadata = [{'type': kind} for kind in ('system', 'last-prompt', 'cost-state')]
        metadata += [{'type': 'user', 'isMeta': True},
                     {'type': 'user', 'isMeta': True, 'message': {'content': {'unknown': True}}}]
        for wrapper in ('<command-message>help</command-message>', '<command-name>/help</command-name>',
                        '<local-command-stdout>ok'):
            for content in (wrapper, [{'type': 'text', 'text': wrapper[:5]},
                                      {'type': 'text', 'text': wrapper[5:]}]):
                metadata.append({'type': 'user', 'message': {'content': content}})
        active = [{'type': 'user', 'message': {'content': 'prompt'}},
                  {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'prompt'}]}},
                  {'type': 'user', 'message': {'content': '[Request interrupted by user]'}},
                  {'type': 'user', 'message': {'content': [{'type': 'text', 'text': '[Request interrupted by user]'}]}},
                  {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'result'}]}},
                  {'type': 'user', 'message': {'content': [{'type': 'text', 'text': '<command-name>/help'},
                                                         {'type': 'tool_result', 'content': 'result'}]}},
                  {'type': 'user', 'message': {'content': [{'type': 'text', 'text': '<command-name>/help'},
                                                         {'type': 'text', 'text': 42}]}},
                  {'type': 'user', 'message': {'content': [{'type': 'text', 'text': '<command-name>/help'}, None]}},
                  {'type': 'user', 'message': {'content': {'text': '<command-name>/help'}}},
                  {'type': 'user', 'message': {'content': []}},
                  {'type': 'user', 'message': None},
                  {'type': 'user', 'isMeta': 1},
                  {'type': 'assistant'}]
        cutoffs = {}; tails = []
        for index, row in enumerate(metadata + active):
            name = str(index) + '.jsonl'
            self.session('claude', 'project/' + name, prefix + json.dumps(row).encode() + b'\n')
            cutoffs[('claude', name)] = self.cutoff(prefix)
            if index >= len(metadata):
                tails.append(('claude', name))
        self.session('claude', 'project/partial', prefix + b'{')
        cutoffs[('claude', 'partial')] = self.cutoff(prefix); tails.append(('claude', 'partial'))
        for root in ('sessions', 'archived_sessions'):
            name = root + '.jsonl'; self.session('codex', root + '/' + name, prefix + b' ')
            cutoffs[('codex', name)] = self.cutoff(prefix); tails.append(('codex', name))
        result = self.scan(cutoffs=cutoffs)
        self.assertEqual(result.tails, tuple(sorted(tails)))
        result = self.scan()
        self.assertEqual((result.checked, result.mismatched, result.tails), ((), (), ()))
        self.assertIsNone(result.cutoffs_sha256)

    def test_a_claude_tail_longer_than_the_bound_is_unfinished(self):
        self.api
        limits = self.api.Limits(output=4096)
        prefix = b'{"type":"assistant","message":{"stop_reason":"end_turn"}}\n'
        tail = json.dumps({'type': 'system', 'padding': 'x' * 6000}).encode() + b'\n'
        self.assertGreater(len(tail), limits.output)
        data = prefix + tail
        self.session('claude', 'project/large.jsonl', data)
        self.session('claude', 'project/dependency.jsonl', data)
        self.session('codex', 'sessions/large.jsonl', data)
        cutoffs = {('claude', 'large.jsonl'): self.cutoff(prefix),
                   ('claude', 'dependency.jsonl'): self.cutoff(prefix, own=False),
                   ('codex', 'large.jsonl'): self.cutoff(prefix)}
        result = self.scan(cutoffs=cutoffs, limits=limits)
        self.assertTrue(result.sessions_complete)
        self.assertIsNotNone(self.api.fingerprint(result))
        self.assertEqual(result.checked, tuple(sorted(cutoffs)))
        self.assertEqual(result.mismatched, ())
        self.assertEqual(result.cutoffs_sha256, self.api.cutoffs_digest(cutoffs))
        self.assertEqual(result.tails, (('claude', 'large.jsonl'), ('codex', 'large.jsonl')))
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(result.sessions, (('claude/project/dependency.jsonl', digest),
                                           ('claude/project/large.jsonl', digest),
                                           ('codex/sessions/large.jsonl', digest)))
        self.assertLessEqual(len(self.connection.raw), limits.output)

    def test_cutoffs_digest_is_recorded(self):
        self.assertIn('cutoffs_sha256', self.api.Scan.__dataclass_fields__,
                      'Scan does not bind the cutoff map it compared')
        prefix = b'{}\n'
        self.session('codex', 'sessions/own.jsonl', prefix)
        cutoffs = {('codex', 'own.jsonl'): self.cutoff(prefix),
                   ('claude', 'dependency.jsonl'): self.cutoff(prefix, own=False)}
        canonical = {'codex/own.jsonl': [len(prefix), hashlib.sha256(prefix).hexdigest(), True],
                     'claude/dependency.jsonl': [len(prefix), hashlib.sha256(prefix).hexdigest(), False]}
        expected = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(self.api.cutoffs_digest(cutoffs), expected)
        self.assertEqual(self.api.cutoffs_digest(dict(reversed(list(cutoffs.items())))), expected)
        result = self.scan(cutoffs=cutoffs)
        self.assertTrue(result.sessions_complete)
        self.assertEqual(result.cutoffs_sha256, expected)
        self.assertEqual(json.loads(self.connection.raw)['cutoffs_sha256'], expected)
        self.assertEqual(result.checked, (('codex', 'own.jsonl'),))
        for field, value in (('sha256', '0' * 64), ('size', len(prefix) + 1), ('own', False)):
            changed = dict(cutoffs)
            changed[('codex', 'own.jsonl')] = replace(cutoffs[('codex', 'own.jsonl')], **{field: value})
            with self.subTest(field=field):
                self.assertNotEqual(self.api.cutoffs_digest(changed), expected)
                self.assertEqual(self.scan(cutoffs=changed).cutoffs_sha256, self.api.cutoffs_digest(changed))
        empty = self.scan(cutoffs={})
        self.assertEqual(empty.cutoffs_sha256, hashlib.sha256(b'{}').hexdigest())
        self.assertEqual(empty.cutoffs_sha256, self.api.cutoffs_digest({}))
        self.assertIsNone(self.scan(cutoffs=None).cutoffs_sha256)
        self.scan(cutoffs=cutoffs)
        compared = json.loads(self.connection.raw)
        remote_python = self.connection.python
        for value in (None, True, 1, '', '0' * 63, 'A' * 64, '0' * 64):
            with self.subTest(response_digest=value):
                self.connection.python = lambda *args, value=value, **kwargs: json.dumps(
                    dict(compared, cutoffs_sha256=value)).encode()
                refused = self.scan(cutoffs=cutoffs)
                self.assertIsNone(self.api.fingerprint(refused))
                self.assertIsNone(refused.cutoffs_sha256)
        # The receipt must describe the map consumed remotely, not a host-side stamp.
        def different_map(code, payload=b'', **kwargs):
            request = json.loads(payload)
            request['cutoffs'][0][2]['sha256'] = '1' * 64
            return remote_python(code, payload=json.dumps(request).encode(), **kwargs)
        self.connection.python = different_map
        refused = self.scan(cutoffs=cutoffs)
        self.assertIsNone(self.api.fingerprint(refused))
        self.assertIsNone(refused.cutoffs_sha256)

    def test_agents_match_any_argv_basename(self):
        self.api
        self.process(100001, b'node\0/usr/lib/bin/claude\0claude\0')
        self.process(100002, b'/usr/bin/codex\0exec\0')
        self.process(100003, b'herdr\0server\0')
        result = self.scan()
        self.assertTrue(result.agents_complete)
        self.assertEqual(dict(result.agents), dict(claude=1, codex=1, grok=0, opencode=0, herdr=1))
        self.assertNotIn(b'/usr/lib/bin/claude', self.connection.raw)

    def test_the_scan_itself_is_not_an_agent(self):
        self.api
        self.connection.self_process = True
        self.assertEqual(sum(count for _, count in self.scan().agents), 0)

    def test_unreadable_process_is_incomplete_but_a_vanished_one_is_skipped(self):
        self.api
        vanished = self.proc / '100001'; vanished.mkdir()
        self.assertTrue(self.scan().agents_complete)
        (vanished / 'cmdline').mkdir()
        self.assertFalse(self.scan().agents_complete)

    def test_a_limit_makes_its_part_incomplete(self):
        self.api
        for index in range(10):
            (self.ws / str(index)).mkdir()
        result = self.scan(limits=self.api.Limits(entries=5))
        self.assertFalse(result.git_complete)
        self.assertTrue(result.scratch_complete and result.sessions_complete and result.agents_complete)

    def test_failed_connection_is_incomplete_not_empty(self):
        self.api
        def fail(*args, **kwargs):
            raise ValueError('secret transport diagnostic')
        self.connection.python = fail
        result = self.scan()
        self.assertFalse(any((result.git_complete, result.scratch_complete, result.sessions_complete, result.agents_complete)))
        self.assertEqual(len(result.problems), 1)
        self.assertNotIn('secret', repr(result))

    def test_fingerprint(self):
        self.api
        root = self.repo('project'); baseline = self.scan(); digest = self.api.fingerprint(baseline)
        self.assertEqual(digest, self.api.fingerprint(self.scan()))
        self.assertEqual(digest, self.api.fingerprint(replace(baseline, at=999, tails=(('codex', 'tail'),))))
        self.assertEqual(digest, self.api.fingerprint(replace(baseline, cutoffs_sha256='0' * 64)))
        for field in ('git_complete', 'scratch_complete', 'sessions_complete', 'agents_complete'):
            self.assertIsNone(self.api.fingerprint(replace(baseline, **{field: False})))
        for action in ('edit', 'branch', 'stash', 'agent'):
            before = self.api.fingerprint(self.scan())
            if action == 'edit':
                (root / 'tracked').write_text('edit')
            elif action == 'branch':
                self.git(root, 'branch', 'new-branch')
            elif action == 'stash':
                self.git(root, 'stash', 'push', '-qm', 'Saved')
            else:
                self.process(100001, b'codex\0')
            self.assertNotEqual(before, self.api.fingerprint(self.scan()), action)
        body = {name: asdict(baseline)[name] for name in ('repositories', 'scratch', 'sessions', 'agents')}
        self.assertEqual(digest, hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest())

    def test_scan_changes_nothing(self):
        self.api
        self.repo('project'); self.session('codex', 'sessions/one.jsonl')
        tracked = self.root / 'tracked'
        info = tracked.stat()
        os.utime(tracked, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        (self.root / '.scratch').mkdir(); (self.root / '.scratch/note').write_text('note')
        def snapshot():
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for root in (self.ws, self.home) for path in root.rglob('*') if path.is_file()}
        before = snapshot(); result = self.scan()
        self.assertTrue(all((result.git_complete, result.scratch_complete, result.sessions_complete, result.agents_complete)))
        self.assertEqual(before, snapshot())

    def filtered(self, root, kind='clean'):
        (root / '.gitattributes').write_text('tracked filter=fixture\n')
        self.git(root, 'add', '.gitattributes'); self.git(root, 'commit', '-qm', 'Configure attribute')
        marker = self.home / ('filter-marker-' + root.name)
        command = 'touch ' + shlex.quote(str(marker)) + ('; cat' if kind == 'clean' else '; exit 1')
        self.git(root, 'config', 'filter.fixture.' + kind, command)
        info = (root / 'tracked').stat()
        os.utime(root / 'tracked', ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        return marker

    def submodule(self):
        source = self.repo('module-source')
        parent = self.repo('project')
        self.git(parent, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(source), 'nested-module')
        child = parent / 'nested-module'
        self.git(child, 'config', 'user.name', 'Fixture')
        self.git(child, 'config', 'user.email', 'fixture@example.com')
        self.git(child, 'remote', 'set-url', 'origin', 'https://github.com/o/module.git')
        self.git(parent, 'add', '.'); self.git(parent, 'commit', '-qm', 'Add submodule')
        return parent, child

    def test_used_clean_filters_never_run(self):
        self.api
        root = self.repo('project'); marker = self.filtered(root)
        self.git(root, 'config', 'filter.fixture.required', 'true')
        result = self.scan()
        self.assertFalse(marker.exists(), 'scan executed a clean helper')
        self.assertTrue(result.git_complete)
        self.assertTrue(result.repositories[0].ok)
        self.assertEqual(result.repositories[0].worktrees[0].changes, 0)
        self.assertNotIn(b'touch ', self.connection.raw)
        (root / 'tracked').write_text('changed')
        result = self.scan()
        self.assertFalse(marker.exists(), 'scan executed a clean helper for changed bytes')
        self.assertTrue(result.repositories[0].ok)
        self.assertEqual(result.repositories[0].worktrees[0].changes, 1)
        self.assertNotIn(b'touch ', self.connection.raw)

    def test_used_process_filters_never_run(self):
        self.api
        root = self.repo('project'); marker = self.filtered(root, 'process')
        # Process drivers take precedence over clean, as git-lfs configures them.
        self.git(root, 'config', 'filter.fixture.clean', 'touch ' + shlex.quote(str(marker)) + '; cat')
        self.git(root, 'config', 'filter.fixture.required', 'true')
        result = self.scan()
        self.assertFalse(marker.exists(), 'scan executed a process helper')
        self.assertTrue(result.git_complete)
        self.assertTrue(result.repositories[0].ok)
        self.assertEqual(result.repositories[0].worktrees[0].changes, 0)
        self.assertNotIn(b'touch ', self.connection.raw)
        (root / 'tracked').write_text('changed')
        result = self.scan()
        self.assertFalse(marker.exists(), 'scan executed a process helper for changed bytes')
        self.assertTrue(result.repositories[0].ok)
        self.assertEqual(result.repositories[0].worktrees[0].changes, 1)
        self.assertNotIn(b'touch ', self.connection.raw)

    def test_an_unused_global_filter_never_runs_and_proves_clean(self):
        self.api
        self.repo('project')
        marker = self.home / 'unused-filter-marker'
        config = self.home / '.gitconfig'
        self.connection.env['GIT_CONFIG_GLOBAL'] = str(config)
        self.git(self.root, 'config', '--global', 'filter.unused.clean',
                 'touch ' + shlex.quote(str(marker)) + '; cat')
        self.git(self.root, 'config', '--global', 'filter.unused.process',
                 'touch ' + shlex.quote(str(marker)) + '; exit 1')
        result = self.scan()
        self.assertFalse(marker.exists())
        self.assertTrue(result.git_complete)
        self.assertTrue(result.repositories[0].ok)
        self.assertEqual(result.repositories[0].worktrees[0].changes, 0)
        self.assertNotIn(b'touch ', self.connection.raw)

    def test_a_filter_name_with_an_equals_sign_is_refused(self):
        self.api
        root = self.repo('project')
        (root / '.gitattributes').write_text('tracked filter=fixture\n')
        self.git(root, 'add', '.gitattributes'); self.git(root, 'commit', '-qm', 'Configure attribute')
        marker = self.home / 'injected-filter-marker'
        injected = 'fixture.clean=touch ' + shlex.quote(str(marker)) + ' #'
        for name in (injected, '', 'tab\tcontrol', 'delete\x7fcontrol', 'c1\x85control'):
            with self.subTest(driver=name):
                key = 'filter.' + name + '.clean'
                self.git(root, 'config', key, 'unused synthetic helper')
                try:
                    info = (root / 'tracked').stat()
                    os.utime(root / 'tracked', ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
                    result = self.scan()
                    self.assertFalse(marker.exists(), 'scan executed an injected filter helper')
                    self.assertTrue(result.git_complete)
                    self.assertFalse(result.repositories[0].worktrees[0].ok)
                    self.assertFalse(result.repositories[0].ok)
                    self.assertNotIn(b'touch ', self.connection.raw)
                finally:
                    self.git(root, 'config', '--unset', key)
                    marker.unlink(missing_ok=True)

    def test_nested_repository_filter_is_not_executed_by_parent_status(self):
        self.api
        parent = self.repo('project'); child = self.repo('project/nested')
        marker = self.filtered(child)
        self.git(parent, 'add', '.'); self.git(parent, 'commit', '-qm', 'Track nested repository')
        # The parent add above can inspect the gitlink; re-trigger content checking.
        marker.unlink(missing_ok=True)
        info = (child / 'tracked').stat()
        os.utime(child / 'tracked', ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        result = self.scan()
        self.assertFalse(marker.exists(), 'parent status executed a nested helper')
        rows = {row.path: row for row in result.repositories}
        self.assertTrue(rows[str(parent)].ok)
        self.assertTrue(rows[str(child)].ok)

    def test_submodule_filter_is_not_executed_by_parent_status(self):
        self.api
        parent, child = self.submodule(); marker = self.filtered(child, 'process')
        self.git(parent, 'add', 'nested-module'); self.git(parent, 'commit', '-qm', 'Record child head')
        marker.unlink(missing_ok=True)
        info = (child / 'tracked').stat()
        os.utime(child / 'tracked', ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        result = self.scan()
        self.assertFalse(marker.exists(), 'parent status executed a submodule helper')
        rows = {row.path: row for row in result.repositories}
        self.assertTrue(rows[str(parent)].ok)
        self.assertTrue(rows[str(child)].ok)

    def test_submodule_dirt_and_changed_head_are_still_found(self):
        self.api
        parent, child = self.submodule()
        (child / 'tracked').write_text('dirty child')
        rows = {row.path: row for row in self.scan().repositories}
        self.assertEqual(rows[str(parent)].worktrees[0].changes, 0)
        self.assertEqual(rows[str(child)].worktrees[0].changes, 1)
        self.git(child, 'add', 'tracked'); self.git(child, 'commit', '-qm', 'Change child head')
        rows = {row.path: row for row in self.scan().repositories}
        self.assertEqual(rows[str(parent)].worktrees[0].changes, 1)
        self.assertEqual(rows[str(parent)].worktrees[0].changed, ('nested-module',))
        self.assertEqual(rows[str(child)].worktrees[0].changes, 0)
        self.assertEqual(rows[str(child)].worktrees[0].head, self.git(child, 'rev-parse', 'HEAD'))

    def test_git_command_output_limit_makes_git_incomplete(self):
        self.api
        root = self.repo('project')
        for index in range(100):
            (root / (str(index) + '-' + 'x' * 60)).write_text('untracked')
        result = self.scan(limits=self.api.Limits(output=2048))
        self.assertFalse(result.git_complete)
        self.assertTrue(result.scratch_complete and result.sessions_complete and result.agents_complete)
        self.assertFalse(result.repositories[0].ok)

    def test_nondefault_origin_ports_keep_the_canonical_identity(self):
        self.api
        from devcontainer_setup.project_sync.state import _identity
        urls = {'project': 'ssh://git@github.com:443/o/r.git',
                'https-port': 'https://github.com:22/o/r.git'}
        for name, url in urls.items():
            self.git(self.repo(name), 'remote', 'add', 'origin', url)
        result = self.scan()
        self.assertTrue(result.git_complete)
        self.assertEqual({Path(row.path).name: row.identity for row in result.repositories},
                         {name: _identity(url) for name, url in urls.items()})

    def test_symlink_git_marker_is_refused(self):
        self.api
        source = self.repo('real')
        marker = self.ws / 'linked-metadata'; marker.mkdir()
        (marker / '.git').symlink_to(source / '.git', target_is_directory=True)
        result = self.scan()
        self.assertFalse(result.git_complete)
        self.assertNotIn(str(marker), {row.path for row in result.repositories})

    def test_scan_time_records_completion(self):
        self.api
        ticks = iter((100.0, 200.0))
        result = self.api.scan(self.connection, 'fixture-cs', str(self.root),
                               workspaces=str(self.ws), proc=str(self.proc), now=lambda: next(ticks))
        self.assertEqual(result.at, 200.0)

    def test_strict_decoder_rejects_malformed_answers(self):
        self.api
        self.repo('project'); valid = json.loads(self.scan() and self.connection.raw)
        variants = [None, {}, dict(valid, git_complete=1), dict(valid, at=float('nan')),
                    dict(valid, agents=[['codex', True]]), dict(valid, unexpected='data')]
        variants += [dict(valid, cutoffs_sha256=value) for value in (True, 1, '', '0' * 63, 'G' * 64, '0' * 64)]
        variants += [{key: value for key, value in valid.items() if key != 'cutoffs_sha256'}]
        row = valid['repositories'][0]
        variants += [dict(valid, repositories=[dict(row, path='bad\npath')]),
                     dict(valid, repositories=[dict(row, branches=[['main', 'bad']])]),
                     dict(valid, repositories=[dict(row, stashes=True)]),
                     dict(valid, repositories=[dict(row, worktrees=[dict(row['worktrees'][0], head='bad')])]),
                     dict(valid, scratch=[['../escape', '0' * 64]]),
                     dict(valid, problems=['secret diagnostic']),
                     dict(valid, repositories=[dict(row, identity='https://user:secret@github.com/a/b')])]
        for value in variants:
            with self.subTest(value=value):
                self.connection.python = lambda *args, value=value, **kwargs: json.dumps(value).encode()
                result = self.scan()
                self.assertFalse(any((result.git_complete, result.scratch_complete, result.sessions_complete, result.agents_complete)))
                self.assertEqual(len(result.problems), 1)
        for raw in (b'{', b'\xff', b'{"git_complete":true,"git_complete":false}'):
            self.connection.python = lambda *args, raw=raw, **kwargs: raw
            self.assertIsNone(self.api.fingerprint(self.scan()))
        self.connection.python = lambda *args, **kwargs: b'x' * 20
        self.assertIsNone(self.api.fingerprint(self.scan(limits=self.api.Limits(output=10))))

    def test_duplicate_session_basename_or_symlink_is_incomplete(self):
        self.api
        self.session('codex', 'sessions/a/same'); self.session('codex', 'archived_sessions/same')
        self.assertFalse(self.scan(cutoffs={('codex', 'same'): self.cutoff(b'{}\n')}).sessions_complete)
        (self.home / '.codex/archived_sessions/same').unlink()
        (self.home / '.codex/archived_sessions/link').symlink_to(self.home)
        self.assertFalse(self.scan().sessions_complete)

    def test_repeated_basenames_outside_the_cutoffs_are_complete(self):
        self.session('claude', 'project/relay/20261101/checklist.json')
        self.session('claude', 'project/relay/20261102/checklist.json')
        for cutoffs in (None, {}):
            with self.subTest(cutoffs=cutoffs):
                scan = self.scan(cutoffs=cutoffs)
                self.assertTrue(scan.sessions_complete)
                self.assertIsNotNone(self.api.fingerprint(scan))

    def test_rename_status_counts_one_change_and_unborn_head_is_none(self):
        self.api
        root = self.repo('project'); self.git(root, 'mv', 'tracked', 'renamed')
        unborn = self.ws / 'unborn'; unborn.mkdir(); self.git(unborn, 'init', '-q')
        result = self.scan()
        row = next(row for row in result.repositories if row.path == str(root)).worktrees[0]
        self.assertEqual((row.changes, row.changed), (1, ('renamed',)))
        row = next(row for row in result.repositories if row.path == str(unborn)).worktrees[0]
        self.assertIsNone(row.head); self.assertTrue(row.ok)

    def test_repository_worktree_file_byte_time_and_output_limits(self):
        self.api
        self.repo('project'); self.repo('second')
        for limit in (self.api.Limits(repositories=1), self.api.Limits(worktrees=1)):
            self.assertFalse(self.scan(limits=limit).git_complete)
        self.session('codex', 'sessions/one', b'12345')
        for limit in (self.api.Limits(files=0), self.api.Limits(hashed_bytes=4)):
            self.assertFalse(self.scan(limits=limit).sessions_complete)
        result = self.scan(limits=self.api.Limits(seconds=0))
        self.assertIsNone(self.api.fingerprint(result))
        self.assertIsNone(self.api.fingerprint(self.scan(limits=self.api.Limits(output=100))))

    def test_prunable_detached_head_is_still_claimed(self):
        from devcontainer_setup.project_sync import verdict
        root = self.repo('project')
        linked = self.ws / 'linked'
        self.git(root, 'worktree', 'add', '-q', '--detach', str(linked))
        (linked / 'tracked').write_text('detached work')
        self.git(linked, 'commit', '-qam', 'Detached work')
        head = self.git(linked, 'rev-parse', 'HEAD')
        shutil.rmtree(linked)
        scan = self.scan()
        self.assertTrue(scan.repositories[0].ok)
        self.assertIn(head, [oid for claim in verdict.claims(scan) for _, oid in claim.refs])

    def test_prunable_worktree_that_still_exists_is_not_skipped(self):
        root = self.repo('project')
        linked = self.ws / 'linked'
        self.git(root, 'worktree', 'add', '-qb', 'linked', str(linked))
        (linked / 'only-here').write_text('local work')
        (linked / '.git').unlink()
        self.assertIn('prunable', self.git(root, 'worktree', 'list', '--porcelain'))
        self.assertFalse(self.scan().repositories[0].ok)



if __name__ == '__main__':
    unittest.main()
