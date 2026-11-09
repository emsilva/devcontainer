"""Commit custody proofs against local bare repositories, never a network remote."""
import importlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from devcontainer_setup.project_sync.state import Project


class PushedTests(unittest.TestCase):
    def setUp(self):
        # Import here so every new test records the missing module independently.
        self.pushed = importlib.import_module('devcontainer_setup.project_sync.pushed')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'source'
        self.origin = self.base / 'origin.git'
        self.mac = self.base / 'mac'
        self.remote = self.base / 'target'
        self.source.mkdir()
        self.git(self.source, 'init', '-q', '-b', 'main')
        self.configure(self.source)
        self.initial = self.commit(self.source, 'initial')
        self.git(self.base, 'clone', '-q', '--bare', str(self.source), str(self.origin))
        for root in (self.mac, self.remote):
            self.git(self.base, 'clone', '-q', str(self.origin), str(root))
            self.configure(root)
        self.project = Project(self.mac, str(self.origin), 'github.com/example/project', 'a' * 24)
        # Proofs retain owner settings, so supply only fixture Git configuration.
        # Even a configured URL rewrite must not let a fixture use a network protocol.
        values = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        values.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                      GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='protocol.allow', GIT_CONFIG_VALUE_0='never',
                      GIT_CONFIG_KEY_1='protocol.file.allow', GIT_CONFIG_VALUE_1='always')
        environment = patch.dict(os.environ, values, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, root, *args, input=None, env=None):
        values = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        values.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                      GIT_TERMINAL_PROMPT='0', LC_ALL='C')
        if env:
            values.update(env)
        return subprocess.run(['git', '-C', str(root), *args], input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              check=True, timeout=60, env=values).stdout.strip()

    def configure(self, root):
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('core.hooksPath', '/dev/null'), ('commit.gpgSign', 'false'),
                           ('credential.helper', '')):
            self.git(root, 'config', key, value)

    def commit(self, root, message):
        (root / 'file').write_text(message + '\n')
        self.git(root, 'add', 'file')
        self.git(root, 'commit', '-qm', message)
        return self.git(root, 'rev-parse', 'HEAD')

    def claim(self, *refs, path='project', identity=None):
        return self.pushed.Claim(path, identity or self.project.identity,
                                 tuple(refs) or (('refs/heads/main', self.initial),))

    def proof(self, claim=None):
        return self.pushed.prove(self.project, [claim or self.claim()])[0]

    def assert_private_refs_removed(self):
        self.assertEqual(self.git(self.mac, 'for-each-ref', 'refs/project-sync-check/'), '')

    def test_branch_at_a_remote_head_is_pushed(self):
        self.assertEqual(self.proof(), self.pushed.Proof('project', 1, (), (), False))
        self.assert_private_refs_removed()

    def test_ancestor_of_a_remote_head_is_pushed(self):
        self.commit(self.remote, 'second')
        self.commit(self.remote, 'third')
        self.git(self.remote, 'push', '-q', 'origin', 'main')
        self.assertEqual(self.proof().pushed, 1)
        self.assert_private_refs_removed()

    def test_commit_the_mac_cannot_find_is_unpushed(self):
        oid = self.commit(self.remote, 'target only')
        proof = self.proof(self.claim(('refs/heads/main', oid)))
        self.assertEqual((proof.pushed, proof.unpushed, proof.unknown),
                         (0, (('refs/heads/main', oid[:12]),), ()))

    def test_branch_deleted_on_the_remote_is_unpushed_on_a_later_check(self):
        self.git(self.remote, 'checkout', '-qb', 'topic')
        oid = self.commit(self.remote, 'topic only')
        self.git(self.remote, 'push', '-q', 'origin', 'topic')
        claim = self.claim(('refs/heads/topic', oid))
        self.assertEqual(self.proof(claim).pushed, 1)
        self.git(self.remote, 'push', '-q', 'origin', '--delete', 'topic')
        self.assertEqual(self.proof(claim).unpushed, (('refs/heads/topic', oid[:12]),))
        self.assert_private_refs_removed()

    def test_owner_refs_tags_and_fetch_head_are_untouched(self):
        self.git(self.mac, 'config', 'fetch.pruneTags', 'true')
        self.git(self.mac, 'config', 'remote.origin.pruneTags', 'true')
        self.git(self.mac, 'tag', 'local-only')
        self.git(self.mac, 'update-ref', 'refs/owner/keep', self.initial)
        self.git(self.mac, 'update-ref', self.pushed.CHECK_NAMESPACE + 'stale', self.initial)
        (self.mac / '.git/FETCH_HEAD').write_bytes(b'owner fetch receipt\n')
        (self.mac / 'file').write_text('owner staged\n')
        self.git(self.mac, 'add', 'file')
        (self.mac / 'file').write_text('owner unstaged\n')
        (self.mac / 'untracked').write_text('owner untracked\n')

        def snapshot():
            refs = self.git(self.mac, 'for-each-ref', '--format=%(refname) %(objectname)',
                            'refs/heads/', 'refs/remotes/', 'refs/tags/', 'refs/owner/')
            files = tuple((self.mac / name).read_bytes() for name in
                          ('.git/HEAD', '.git/index', '.git/FETCH_HEAD', 'file', 'untracked'))
            return refs, files

        before = snapshot()
        tip = self.commit(self.remote, 'new origin tip')
        self.git(self.remote, 'push', '-q', 'origin', 'main')
        proof = self.proof(self.claim(('refs/heads/main', tip)))
        self.assertEqual(proof.pushed, 1)
        self.assertEqual(snapshot(), before)
        self.assert_private_refs_removed()

    def test_failed_fetch_is_unknown_not_unpushed(self):
        self.git(self.mac, 'remote', 'set-url', 'origin', str(self.base / 'absent.git'))
        self.git(self.mac, 'update-ref', self.pushed.CHECK_NAMESPACE + 'old', self.initial)
        proof = self.proof()
        self.assertEqual((proof.pushed, proof.unpushed, proof.unknown),
                         (0, (), (('refs/heads/main', 'GitHub could not be read from this Mac'),)))
        self.assert_private_refs_removed()

    def test_private_symbolic_refs_cannot_move_or_delete_owner_refs(self):
        tip = self.commit(self.remote, 'advanced origin')
        self.git(self.remote, 'push', '-q', 'origin', 'main')
        alias = self.pushed.CHECK_NAMESPACE + 'main'
        for fetch_ok in (False, True):
            with self.subTest(fetch_ok=fetch_ok):
                self.git(self.mac, 'update-ref', 'refs/heads/owner', self.initial)
                self.git(self.mac, 'symbolic-ref', alias, 'refs/heads/owner')
                url = self.origin if fetch_ok else self.base / 'absent.git'
                self.git(self.mac, 'remote', 'set-url', 'origin', str(url))
                try:
                    proof = self.proof(self.claim(('refs/heads/main', tip)))
                    self.assertEqual(self.git(self.mac, 'for-each-ref', '--format=%(objectname)',
                                               'refs/heads/owner'), self.initial)
                    self.assertFalse((self.mac / '.git' / alias).exists())
                    self.assertEqual(proof.pushed, int(fetch_ok))
                finally:
                    self.git(self.mac, 'update-ref', '--no-deref', '-d', alias)
        self.assert_private_refs_removed()

    def test_a_local_graft_proves_nothing(self):
        tree = self.git(self.mac, 'rev-parse', 'HEAD^{tree}')
        oid = self.git(self.mac, 'commit-tree', tree, input='unrelated local commit\n')
        graft = self.mac / '.git/info/grafts'
        graft.write_text(f'{self.initial} {oid}\n')
        advertised = self.pushed.CHECK_NAMESPACE + 'fixture'
        self.git(self.mac, 'update-ref', advertised, self.initial)
        self.assertTrue(self.git(self.mac, 'for-each-ref', '--contains', oid, advertised))
        for inherited in (False, True):
            with self.subTest(inherited=inherited):
                with patch.dict(os.environ, {'GIT_GRAFT_FILE': str(graft)} if inherited else {}):
                    proof = self.proof(self.claim(('refs/heads/local', oid)))
                self.assertEqual((proof.pushed, proof.unpushed, proof.unknown),
                                 (0, (('refs/heads/local', oid[:12]),), ()))
        self.assert_private_refs_removed()

    def test_shallow_or_partial_checkout_is_unknown(self):
        for mode in ('shallow', 'promisor', 'partialClone'):
            with self.subTest(mode=mode):
                if mode == 'shallow':
                    shallow = self.base / 'shallow'
                    self.git(self.base, 'clone', '-q', '--depth=1', self.origin.as_uri(), str(shallow))
                    project = Project(shallow, str(self.origin), self.project.identity, 'a' * 24)
                else:
                    key = 'remote.origin.promisor' if mode == 'promisor' else 'extensions.partialClone'
                    self.git(self.mac, 'config', key, 'true' if mode == 'promisor' else 'origin')
                    project = self.project
                try:
                    proof = self.pushed.prove(project, [self.claim()])[0]
                    self.assertEqual((proof.pushed, proof.unpushed, proof.unknown),
                                     (0, (), (('refs/heads/main', "this Mac's checkout is shallow or partial"),)))
                finally:
                    if mode != 'shallow':
                        self.git(self.mac, 'config', '--unset', key)

    def test_no_remote_with_commits_is_flagged(self):
        claim = self.pushed.Claim('local', None, (('HEAD', self.initial),))
        self.assertEqual(self.proof(claim), self.pushed.Proof('local', 0, (), (), True))

    def test_other_repository_needs_an_equal_head(self):
        tip = self.commit(self.remote, 'new head')
        self.git(self.remote, 'push', '-q', 'origin', 'main')
        claim = self.claim(('refs/heads/main', tip), ('refs/heads/old', self.initial),
                           identity='github.com/example/other')
        with patch.object(self.pushed, '_github_url', return_value=str(self.origin)):
            proof = self.proof(claim)
        self.assertEqual((proof.pushed, proof.unpushed, proof.unknown),
                         (1, (), (('refs/heads/old', 'this Mac has no checkout of example/other to compare'),)))

    def test_detached_head_is_proved_as_HEAD(self):
        self.git(self.remote, 'checkout', '-q', '--detach')
        proof = self.proof(self.claim(('HEAD', self.git(self.remote, 'rev-parse', 'HEAD'))))
        self.assertEqual((proof.pushed, proof.unpushed, proof.unknown), (1, (), ()))

    def test_one_fetch_and_one_batch_for_all_matching_claims_in_order(self):
        run = subprocess.run
        calls = []

        def record(args, **kwargs):
            calls.append((args, kwargs))
            return run(args, **kwargs)

        claims = [self.claim(path='second'), self.claim(('HEAD', self.initial), path='first')]
        with patch.object(self.pushed.subprocess, 'run', side_effect=record):
            proofs = self.pushed.prove(self.project, claims, timeout=7)
        self.assertEqual([proof.path for proof in proofs], ['second', 'first'])
        self.assertEqual([proof.pushed for proof in proofs], [1, 1])
        self.assertEqual(sum('fetch' in args for args, _ in calls), 1)
        self.assertEqual(sum('cat-file' in args for args, _ in calls), 1)
        for _, kwargs in calls:
            self.assertEqual(kwargs['timeout'], 7)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)

    def test_git_environment_discards_overrides_but_keeps_credential_configuration(self):
        run = subprocess.run
        calls = []
        self.git(self.mac, 'config', 'credential.interactive', 'always')

        def record(args, **kwargs):
            calls.append((args, kwargs))
            return run(args, **kwargs)

        overrides = dict(GIT_DIR=str(self.remote / '.git'), GIT_WORK_TREE=str(self.remote),
                         GIT_GRAFT_FILE=str(self.base / 'absent-graft'), GIT_NO_REPLACE_OBJECTS='0')
        with patch.dict(os.environ, overrides), patch.object(self.pushed.subprocess, 'run', side_effect=record):
            self.assertEqual(self.proof().pushed, 1)
        for _, kwargs in calls:
            values = kwargs['env']
            self.assertFalse('GIT_DIR' in values, 'GIT_DIR')
            self.assertFalse('GIT_WORK_TREE' in values, 'GIT_WORK_TREE')
            for key, value in dict(GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1',
                                   GIT_GRAFT_FILE='/dev/null').items():
                self.assertTrue(values.get(key) == value, key)
            self.assertEqual(kwargs['env']['LC_ALL'], 'C')
        args = next(args for args, _ in calls if 'fetch' in args)
        self.assertIn('credential.interactive=never', args)
        self.assertIn('maintenance.auto=false', args)
        self.assertIn('gc.auto=0', args)
        self.assertNotIn('credential.helper=', args)
        self.assertEqual(self.git(self.mac, 'config', '--get', 'credential.interactive'), 'always')

    def test_owner_authentication_variables_are_kept(self):
        ssh = self.base / 'fixture-ssh'
        askpass = self.base / 'fixture-askpass'
        helper = self.base / 'fixture-helper'
        for stub in (ssh, askpass, helper):
            stub.write_text('#!/bin/sh\nexit 0\n')
            stub.chmod(0o700)
        kept = dict(GIT_SSH_COMMAND=str(ssh), GIT_SSH=str(ssh), GIT_ASKPASS=str(askpass),
                    GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0=str(helper),
                    GIT_CONFIG_GLOBAL=str(self.base / 'fixture-global'),
                    GIT_CONFIG_SYSTEM=str(self.base / 'fixture-system'), GIT_CONFIG_NOSYSTEM='1',
                    SSH_ASKPASS=str(askpass))
        dropped = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_INDEX_VERSION',
                   'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_QUARANTINE_PATH',
                   'GIT_NAMESPACE', 'GIT_SHALLOW_FILE', 'GIT_REPLACE_REF_BASE', 'GIT_CONFIG',
                   'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM', 'GIT_CURL_VERBOSE',
                   'GIT_TRACE', 'GIT_TRACE_PACKET', 'GIT_TRACE2_EVENT', 'GIT_TRACE_FUTURE')
        forced = dict(GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1', GIT_GRAFT_FILE='/dev/null',
                      GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', LC_ALL='C')
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(kept)
        env.update(dict.fromkeys(dropped, 'fixture-layout-or-trace'))
        env.update(dict.fromkeys(forced, 'fixture-override'))
        calls = []

        def check_environment(values):
            # Assertion messages contain key names only, never a preserved value.
            for key, value in kept.items():
                self.assertTrue(values.get(key) == value, key)
            for key in dropped:
                self.assertFalse(key in values, key)
            for key, value in forced.items():
                self.assertTrue(values.get(key) == value, key)

        def fixture_git(args, **kwargs):
            calls.append((args, kwargs))
            if 'fetch' in args:
                check_environment(kwargs['env'])
            if 'rev-parse' in args:
                output = 'false\n'
            elif 'config' in args:
                return subprocess.CompletedProcess(args, 1, '')
            elif 'cat-file' in args:
                output = self.initial + ' commit 123\n'
            elif '--contains' in args:
                output = self.pushed.CHECK_NAMESPACE + 'main\n'
            else:
                output = ''
            return subprocess.CompletedProcess(args, 0, output)

        with patch.dict(os.environ, env, clear=True), patch.object(self.pushed.subprocess, 'run',
                                                                  side_effect=fixture_git):
            proof = self.proof()
        self.assertEqual((proof.pushed, proof.unpushed, proof.unknown), (1, (), ()))
        self.assertEqual(sum('fetch' in args for args, _ in calls), 1)
        for _, kwargs in calls:
            check_environment(kwargs['env'])
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)

    def test_replacement_objects_prove_nothing(self):
        tree = self.git(self.mac, 'rev-parse', 'HEAD^{tree}')
        oid = self.git(self.mac, 'commit-tree', tree, input='unrelated local commit\n')
        replacement = self.git(self.mac, 'commit-tree', tree, '-p', oid, input='replacement\n')
        self.git(self.mac, 'replace', self.initial, replacement)
        proof = self.proof(self.claim(('refs/heads/local', oid)))
        self.assertEqual((proof.pushed, proof.unpushed), (0, (('refs/heads/local', oid[:12]),)))

    def test_non_commit_or_failed_batch_is_unknown_and_cleans_private_refs(self):
        run = subprocess.run
        blob = self.git(self.mac, 'rev-parse', 'HEAD:file')
        self.assertEqual(self.proof(self.claim(('refs/heads/invalid', blob))).unknown,
                         (('refs/heads/invalid', 'GitHub could not be read from this Mac'),))

        def fail_batch(args, **kwargs):
            if 'cat-file' in args:
                return subprocess.CompletedProcess(args, 128, '')
            return run(args, **kwargs)

        with patch.object(self.pushed.subprocess, 'run', side_effect=fail_batch):
            proof = self.proof()
        self.assertEqual((proof.pushed, proof.unpushed, len(proof.unknown)), (0, (), 1))
        self.assert_private_refs_removed()

    def test_timeout_or_containment_failure_is_unknown_and_cleans_private_refs(self):
        run = subprocess.run
        for failure in ('fetch', 'contains'):
            with self.subTest(failure=failure):
                def fail(args, **kwargs):
                    if failure == 'fetch' and 'fetch' in args:
                        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
                    if failure == 'contains' and '--contains' in args:
                        return subprocess.CompletedProcess(args, 128, '')
                    return run(args, **kwargs)

                with patch.object(self.pushed.subprocess, 'run', side_effect=fail):
                    proof = self.proof()
                self.assertEqual((proof.pushed, proof.unpushed, proof.unknown),
                                 (0, (), (('refs/heads/main', 'GitHub could not be read from this Mac'),)))
                self.assert_private_refs_removed()

    def test_other_repository_failure_is_unknown_and_runs_outside_the_checkout(self):
        run = subprocess.run
        calls = []

        def record(args, **kwargs):
            calls.append((args, kwargs))
            return run(args, **kwargs)

        claim = self.claim(identity='github.com/example/other')
        with patch.object(self.pushed, '_github_url', return_value=str(self.base / 'absent.git')):
            with patch.object(self.pushed.subprocess, 'run', side_effect=record):
                proof = self.proof(claim)
        self.assertEqual(proof.unknown, (('refs/heads/main', 'GitHub could not be read from this Mac'),))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['cwd'], '/')
        self.assertIn('--heads', calls[0][0])

    def test_empty_claims_and_non_github_identities_need_no_git(self):
        claims = [self.pushed.Claim('empty', None, ()),
                  self.pushed.Claim('foreign', 'example.invalid/owner/repo', (('HEAD', self.initial),))]
        with patch.object(self.pushed.subprocess, 'run', side_effect=AssertionError('unexpected Git')):
            proofs = self.pushed.prove(self.project, claims)
        self.assertEqual(proofs, (self.pushed.Proof('empty', 0, (), (), False),
                                 self.pushed.Proof('foreign', 0, (),
                                                   (('HEAD', 'not a GitHub repository'),), False)))

    def test_github_url_is_fixed_https_without_credentials(self):
        self.assertEqual(self.pushed._github_url('github.com/example/other'),
                         'https://github.com/example/other.git')


if __name__ == '__main__':
    unittest.main()
