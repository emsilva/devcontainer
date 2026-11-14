import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

# The module, not its classes: a class imported by name here would run all of its tests again.
from tests import test_wizard

SOURCE = Path(__file__).resolve().parents[1]
CASES = SOURCE / 'tests/fixtures/default-holds-cases.json'
CASES_SHA256 = '723a5b9988f497d883225fd1ff8cab3e13521e1fc232a1347c16b1c0c5404293'

BRANCH_SCREEN = (
    "GitHub's main holds 0 of the 10 files tracked on develop.\n"
    'A Codespace starts from main, so it would hold almost none of this project.\n'
    'Merge the project into main first, then run the setup from a checkout on main.')
MAIN_SCREEN = (
    "GitHub's main holds 2 of the 12 files tracked on this Mac's main.\n"
    'A Codespace starts from GitHub, so it would hold almost none of this project.\n'
    "Push this Mac's main to GitHub, or merge it there, first.")


class Terminal(io.StringIO):
    def isatty(self):
        return True


def cases():
    return {case['name']: case for case in json.loads(CASES.read_text())['cases']}


def expand(paths):
    """The case table's notation: 'src/module_*3' stands for src/module_0, src/module_1, src/module_2."""
    found = []
    for path in paths:
        stem, star, count = path.partition('*')
        found.extend([f'{stem}{index}' for index in range(int(count))] if star else [path])
    return found


class CaseRepositories(unittest.TestCase):
    """Each case as a checkout whose origin is a GitHub URL served offline by a local bare repository.

    The origin stays ssh://git@github.com/... (through the wizard tests' fake ssh) rather than an
    insteadOf rewrite: `git remote get-url` prints the rewritten path, so the wizard would not see GitHub.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        ssh = self.base / 'fake-ssh.py'
        ssh.write_text(test_wizard.FAKE_SSH)
        (self.base / 'home').mkdir()
        env = {'HOME': str(self.base / 'home'), 'XDG_CONFIG_HOME': str(self.base / 'config'),
               'XDG_CACHE_HOME': str(self.base / 'cache'), 'XDG_STATE_HOME': str(self.base / 'state'),
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_SSH_VARIANT': 'simple',
               'GIT_SSH_COMMAND': f'{shlex.quote(sys.executable)} -I {shlex.quote(str(ssh))}',
               'FAKE_GITHUB': str(self.base / 'github'),
               'GIT_AUTHOR_NAME': 'Owner', 'GIT_AUTHOR_EMAIL': 'owner@example.com',
               'GIT_COMMITTER_NAME': 'Owner', 'GIT_COMMITTER_EMAIL': 'owner@example.com'}
        environment = patch.dict(os.environ, env)
        environment.start()
        self.addCleanup(environment.stop)
        for name in [name for name in os.environ if name.startswith('GIT_') and name not in env]:
            del os.environ[name]
        self.built = 0

    def git(self, repo, *args, stdin=None, env=None):
        return subprocess.run(['git', '-C', str(repo), *args], input=stdin, capture_output=True, text=True,
                              check=True, env=env).stdout.strip()

    def build(self, case, default='main', checkout=True):
        """GitHub's <default> as the case's commits, fetched; then this checkout as the case's local side."""
        self.built += 1
        name = f'case-{self.built}'
        bare = self.base / 'github/acme' / f'{name}.git'
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '--quiet', '--bare', '-b', default, str(bare)], check=True)
        repo = self.base / name
        subprocess.run(['git', 'init', '--quiet', '-b', default, str(repo)], check=True)
        self.git(repo, 'remote', 'add', 'origin', f'ssh://git@github.com/acme/{name}.git')
        blob = self.git(repo, 'hash-object', '-w', '--stdin', stdin='x\n')
        commits, parent = [], None
        for number, paths in enumerate(case['main_commits']):
            # Each commit's tree is exactly its paths: a later commit drops the earlier one's files.
            index = dict(os.environ, GIT_INDEX_FILE=str(self.base / f'{name}-index-{number}'))
            self.git(repo, 'update-index', '--add', '--index-info',
                     stdin=''.join(f'100644 {blob}\t{path}\n' for path in expand(paths)), env=index)
            tree = self.git(repo, 'write-tree', env=index)
            parent = self.git(repo, 'commit-tree', tree, *(['-p', parent] if parent else []), '-m', f'Main {number}')
            commits.append(parent)
        self.git(repo, 'push', '--quiet', 'origin', f'{parent}:refs/heads/{default}')
        self.git(repo, 'fetch', '--quiet', 'origin')
        self.git(repo, 'remote', 'set-head', 'origin', default)
        if not checkout:
            return repo
        local = case['local']
        if local['start'] == 'orphan':
            self.git(repo, 'switch', '--quiet', '--orphan', local['branch'])
        else:
            start = commits[0] if local['start'] == 'first' else f'refs/remotes/origin/{default}'
            self.git(repo, 'checkout', '--quiet', '-B', default, start)
            if local['branch'] not in ('main', default):
                self.git(repo, 'switch', '--quiet', '-c', local['branch'])
        if local['add']:
            for path in expand(local['add']):
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                (repo / path).write_text(path + '\n')
            self.git(repo, 'add', '--all')
            self.git(repo, 'commit', '--quiet', '-m', 'Local work')
        return repo


class DefaultHoldsTests(CaseRepositories):
    def test_cases_file_is_the_shared_copy(self):
        self.assertEqual(hashlib.sha256(CASES.read_bytes()).hexdigest(), CASES_SHA256,
                         'tests/fixtures/default-holds-cases.json changed: the dotfiles repo holds the same bytes '
                         '(ready.py tests), so change that copy too and pin the new sha256 in both repos.')

    def test_shared_cases(self):
        from devcontainer_setup.default_holds import measure, refusal
        for case in cases().values():
            with self.subTest(case['name']):
                repo = self.build(case)
                holding = measure(repo)
                if case['held'] is None:
                    self.assertIsNone(holding)
                else:
                    self.assertEqual((holding.held, holding.tracked), (case['held'], case['tracked']))
                    self.assertEqual(holding.refused, case['expect'] == 'refuse')
                self.assertEqual(refusal(repo) is None, case['expect'] == 'pass', refusal(repo))

    def test_refusal_names_the_fix_for_a_branch(self):
        from devcontainer_setup.default_holds import refusal
        repo = self.build(cases()['project on an unrelated branch, template on main'])
        self.assertEqual(refusal(repo), BRANCH_SCREEN)

    def test_refusal_names_the_fix_for_main(self):
        from devcontainer_setup.default_holds import refusal
        repo = self.build(cases()["this Mac's main holds the project, GitHub's main only the template"])
        self.assertEqual(refusal(repo), MAIN_SCREEN)

    def test_refusal_names_the_real_default_branch(self):
        from devcontainer_setup.default_holds import refusal
        branch = self.build(cases()['project on an unrelated branch, template on main'], default='trunk')
        self.assertEqual(refusal(branch), BRANCH_SCREEN.replace('main', 'trunk'))
        local = dict(cases()["this Mac's main holds the project, GitHub's main only the template"])
        local['local'] = dict(local['local'], branch='trunk')
        self.assertEqual(refusal(self.build(local, default='trunk')), MAIN_SCREEN.replace('main', 'trunk'))

    def test_refusal_on_a_detached_checkout(self):
        from devcontainer_setup.default_holds import refusal
        repo = self.build(cases()['branch that merged the template in, main still lacks the project'])
        self.git(repo, 'switch', '--quiet', '--detach')
        self.assertEqual(refusal(repo), BRANCH_SCREEN.replace('0 of the 10', '2 of the 12')
                         .replace('on develop.', 'on this detached checkout.'))

    def test_one_tracked_file_is_one_file(self):
        from devcontainer_setup.default_holds import refusal
        case = dict(cases()['project on an unrelated branch, template on main'])
        case['local'] = dict(case['local'], add=['src/only.py'])
        self.assertEqual(refusal(self.build(case)).splitlines()[0],
                         "GitHub's main holds 0 of the 1 file tracked on develop.")

    def test_gate_reads_a_fresh_fetch(self):
        from devcontainer_setup.default_holds import refusal
        repo = self.build(cases()['project on an unrelated branch, template on main'])
        self.assertIsNotNone(refusal(repo))
        # The project lands on GitHub's main from elsewhere; this checkout's origin/main is now stale.
        bare = self.base / 'github/acme' / f'case-{self.built}.git'
        self.git(bare, 'fetch', '--quiet', str(repo), '+refs/heads/develop:refs/heads/main')
        self.assertIsNone(refusal(repo))

    def test_gate_needs_positive_evidence(self):
        from devcontainer_setup.default_holds import measure, refusal
        from devcontainer_setup.git_ops import _fetch_default
        table = cases()
        # (a) GitHub stops answering after one fetch: the last fetch decides, both ways.
        for name in ('project on an unrelated branch, template on main', 'feature branch with a small addition'):
            with self.subTest(f'unreachable after a fetch: {name}'):
                repo = self.build(table[name])
                verdict, holding = refusal(repo), measure(repo)
                self.git(repo, 'remote', 'set-url', 'origin', str(self.base / 'missing.git'))
                self.assertFalse(_fetch_default(repo, 'main'))
                self.assertEqual((refusal(repo), measure(repo)), (verdict, holding))
                self.assertEqual(verdict is None, table[name]['expect'] == 'pass')
        # (b) origin/main was never fetched and cannot be: nothing to compare.
        with self.subTest('never fetched'):
            repo = self.base / 'never-fetched'
            subprocess.run(['git', 'init', '--quiet', '-b', 'develop', str(repo)], check=True)
            (repo / 'src').mkdir()
            for number in range(10):
                (repo / f'src/module_{number}').write_text('x\n')
            self.git(repo, 'add', '--all')
            self.git(repo, 'commit', '--quiet', '-m', 'Local work')
            self.git(repo, 'remote', 'add', 'origin', 'ssh://git@github.com/acme/absent.git')
            self.assertFalse(_fetch_default(repo, 'main'))
            self.assertEqual((measure(repo), refusal(repo)), (None, None))
        # (c) no commit yet on this checkout, while GitHub's main holds commits.
        with self.subTest('no commit yet'):
            repo = self.build(table['up to date on main'], checkout=False)
            self.assertEqual((measure(repo), refusal(repo)), (None, None))

    def test_read_only_modes_are_not_gated(self):
        from devcontainer_setup.__main__ import main
        repo = self.build(cases()['project on an unrelated branch, template on main'])
        for mode in ('--dry-run', '--scan', '--check', '--replace'):
            with self.subTest(mode):
                output = Terminal()
                with patch('devcontainer_setup.default_holds.refusal', Mock(return_value=BRANCH_SCREEN)) as gate, \
                        patch('devcontainer_setup.replacement.run', Mock(return_value=0)), \
                        patch('devcontainer_setup.__main__.interactive', Mock(return_value=0)) as interactive, \
                        patch.object(sys, 'stdin', Terminal()), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    main(['--project', str(repo), mode])
                gate.assert_not_called()
                interactive.assert_not_called()
                self.assertNotIn(BRANCH_SCREEN, output.getvalue())


class SetupGateTests(unittest.TestCase):
    """The gate through main(), in the wizard's own offline-GitHub fixture."""

    def setUp(self):
        self.fixture = test_wizard.SetupFromDefaultBranchTests('setUp')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def main(self, *extra):
        from devcontainer_setup.__main__ import main
        stdout, stderr = Terminal(), io.StringIO()
        self.gum, self.handoff, self.interactive = (Mock(side_effect=AssertionError('asked a question')), Mock(),
                                                    Mock(return_value=0))
        with patch('devcontainer_setup.__main__.Gum', self.gum), \
                patch('devcontainer_setup.__main__.upgrade_handoff', self.handoff), \
                patch('devcontainer_setup.__main__.interactive', self.interactive), \
                patch.object(sys, 'stdin', Terminal()), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--project', str(self.fixture.root), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_refused_setup_asks_writes_and_pushes_nothing(self):
        fixture = self.fixture
        fixture.checkout_feature()
        before = fixture.snapshot()
        screen = ("GitHub's main holds 1 of the 3 files tracked on feature/local.\n"
                  'A Codespace starts from main, so it would hold almost none of this project.\n'
                  'Merge the project into main first, then run the setup from a checkout on main.\n')
        # --upgrade too: the gate comes before the upgrade hand-off, so nothing is fetched into the cache.
        for extra in ((), ('--upgrade',)):
            with self.subTest(' '.join(extra) or 'setup'):
                code, stdout, stderr = self.main(*extra)
                self.assertEqual(code, 1, stdout + stderr)
                self.assertEqual(stderr, screen)
                self.assertEqual(stdout, '')
                self.gum.assert_not_called()
                self.handoff.assert_not_called()
                self.interactive.assert_not_called()
                self.assertEqual(json.loads(fixture.gh_state.read_text())['calls'], [])
                self.assertFalse(fixture.state.exists())
                fixture.assert_nothing_left()
                self.assertEqual(fixture.snapshot(), before)

    def test_no_github_origin_is_not_gated(self):
        fixture = self.fixture
        fixture.checkout_feature()
        steps = (('origin on another host', ('remote', 'set-url', 'origin', 'https://gitlab.com/owner/project.git')),
                 ('no origin', ('remote', 'remove', 'origin')))
        for label, change in steps:
            with self.subTest(label):
                fixture.git(*change)
                with patch('devcontainer_setup.default_holds.refusal', Mock(return_value='refused')) as gate:
                    code, stdout, stderr = self.main()
                self.assertEqual(code, 0, stdout + stderr)
                self.interactive.assert_called_once()
                gate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
