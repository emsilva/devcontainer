"""Copy gitignored .env files onto a Codespace. Fakes and temp repos only — no network, gh, or real secrets."""
import base64
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from devcontainer_setup.project_sync import env_files, sync_cycle, workspace
from devcontainer_setup.project_sync.state import atomic_write, read_json


WHEN = 1_700_000_000.5
CANARY = 'CANARY-SECRET-VALUE-95'
MAX_BYTES = 64 * 1024


class Connection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def python(self, code, payload=b'', timeout=60):
        self.calls.append({'code': code, 'payload': payload, 'timeout': timeout})
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def init_repo(root):
    subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
    for key, value in (('user.email', 'test@example.invalid'), ('user.name', 'Test'), ('commit.gpgsign', 'false')):
        subprocess.run(['git', '-C', str(root), 'config', key, value], check=True, capture_output=True)
    (root / '.gitignore').write_text('.env\n.env.*\n')


def write_file(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)
    return path


def commit_tracked(root, relative, data):
    path = write_file(root, relative, data)
    subprocess.run(['git', '-C', str(root), 'add', '-f', '--', relative], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'track'], check=True, capture_output=True)
    return path


def status_value(**extra):
    counts = {'received': 0, 'available': 0, 'pending': 0, 'unsupported': 0}
    value = {
        'worker': {'running': False},
        'scratch': {'phase': 'stopped'},
        'conversations': {
            'outcome': 'pending',
            'stage': 'idle',
            'directions': {name: {'counts': dict(counts)} for name in sync_cycle.DIRECTIONS},
            'issues': [],
            'retained_receipts': {},
        },
        'workspace': {'codespace': 'space'},
    }
    value.update(extra)
    return value


class GitEnv(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'checkout'
        self.root.mkdir()
        self.state = Path(self.temp.name).resolve() / 'state'
        saved = {key: os.environ.get(key) for key in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')}
        os.environ['GIT_CONFIG_GLOBAL'] = '/dev/null'
        os.environ['GIT_CONFIG_NOSYSTEM'] = '1'

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def run_ensure(self, connection, root=None, remote_root='/workspaces/ordinary'):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            receipt = env_files.ensure(connection, 'space', self.state, root or self.root, remote_root, now=lambda: WHEN)
        return receipt, out.getvalue(), err.getvalue()


class FindTests(GitEnv):
    def test_find_picks_env_files_and_reports_too_large(self):
        init_repo(self.root)
        write_file(self.root, '.env', b'root-env\n')
        write_file(self.root, 'web/.env', b'web-env\n')
        write_file(self.root, '.env.local', b'local-env\n')
        write_file(self.root, '.env.example', b'example\n')
        write_file(self.root, '.env.sample', b'sample\n')
        write_file(self.root, '.env.template', b'template\n')
        write_file(self.root, 'docs/.env.example', b'docs-example\n')
        write_file(self.root, '.environment', b'not-an-env-file\n')
        write_file(self.root, 'node_modules/x/.env', b'nm\n')
        write_file(self.root, '.scratch/.env', b'scratch\n')
        write_file(self.root, 'tmp/.env', b'tmp\n')
        write_file(self.root, '.claude/worktrees/a/.env', b'worktree\n')
        write_file(self.root, 'edge/.env', b'e' * MAX_BYTES)
        write_file(self.root, 'oversized/.env', b'x' * (70 * 1024))
        commit_tracked(self.root, 'tracked/.env', b'tracked-env\n')
        target = write_file(self.root, 'link-target.txt', b'through-link\n')
        alias = self.root / 'alias'
        alias.mkdir()
        (alias / '.env').symlink_to(target)

        found = env_files.find(self.root)

        self.assertEqual(found['.env'], b'root-env\n')
        self.assertEqual(found['web/.env'], b'web-env\n')
        self.assertEqual(found['.env.local'], b'local-env\n')
        self.assertEqual(found['edge/.env'], b'e' * MAX_BYTES)
        self.assertEqual(found['oversized/.env'], 'too large')
        for skipped in ('.env.example', '.env.sample', '.env.template', 'docs/.env.example', '.environment',
                        'node_modules/x/.env', '.scratch/.env', 'tmp/.env', '.claude/worktrees/a/.env',
                        'tracked/.env', 'alias/.env', 'link-target.txt'):
            self.assertNotIn(skipped, found)
        self.assertNotIn(b'through-link', b''.join(value for value in found.values() if isinstance(value, bytes)))
        self.assertNotIn(b'tracked-env', b''.join(value for value in found.values() if isinstance(value, bytes)))

    def test_find_errors_when_more_than_twenty_files_match(self):
        init_repo(self.root)
        for number in range(21):
            write_file(self.root, f'n{number:02d}/.env', b'x\n')
        with self.assertRaisesRegex(ValueError, 'too many .env files'):
            env_files.find(self.root)


class RemoteCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.remote = self.base / 'remote'
        self.remote.mkdir()
        self.python = shutil.which('python3')
        self.assertTrue(self.python)

    def invoke(self, files, root=None):
        payload = json.dumps({
            'root': str(root or self.remote),
            'files': {path: base64.b64encode(data).decode('ascii') for path, data in files.items()},
        }).encode()
        previous = os.umask(0)
        try:
            return subprocess.run(
                [self.python, '-I', '-B', '-c', env_files.REMOTE],
                input=payload, capture_output=True, timeout=20)
        finally:
            os.umask(previous)

    def test_remote_code_copies_a_missing_file_with_mode_600(self):
        secret = b'made-up-gemini-key\n'
        (self.remote / 'web').mkdir()
        result = self.invoke({'web/.env': secret, '.env': b''})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        self.assertNotIn(secret, result.stdout)
        body = json.loads(result.stdout)
        self.assertEqual(body['files']['web/.env'], 'copied')
        self.assertEqual(body['files']['.env'], 'copied')
        target = self.remote / 'web' / '.env'
        self.assertEqual(target.read_bytes(), secret)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertFalse(target.is_symlink())
        self.assertEqual((self.remote / '.env').read_bytes(), b'')
        self.assertEqual(stat.S_IMODE((self.remote / '.env').stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in (self.remote / 'web').iterdir()), ['.env'])

    def test_remote_code_leaves_identical_bytes_current_and_mtime_unchanged(self):
        target = self.remote / '.env'
        target.write_bytes(b'same\n')
        os.chmod(target, 0o644)
        os.utime(target, (1_700_000_000, 1_700_000_000))
        result = self.invoke({'.env': b'same\n'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['files']['.env'], 'current')
        self.assertEqual(target.read_bytes(), b'same\n')
        self.assertEqual(target.stat().st_mtime, 1_700_000_000)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_remote_code_leaves_different_bytes_unchanged(self):
        target = self.remote / '.env'
        target.write_bytes(b'old\n')
        os.chmod(target, 0o640)
        os.utime(target, (1_700_000_000, 1_700_000_000))
        result = self.invoke({'.env': b'new-made-up\n'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['files']['.env'], 'differs')
        self.assertEqual(target.read_bytes(), b'old\n')
        self.assertEqual(target.stat().st_mtime, 1_700_000_000)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertNotIn(b'new-made-up', result.stdout)
        self.assertNotIn(b'new-made-up', result.stderr)

    def test_remote_code_reports_a_missing_parent_and_creates_nothing(self):
        result = self.invoke({'missing/dir/.env': b'x\n'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['files']['missing/dir/.env'], 'no folder')
        self.assertFalse((self.remote / 'missing').exists())
        self.assertEqual(list(self.remote.iterdir()), [])

    def test_remote_code_refuses_traversal_absolute_path_and_symlinked_parent(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / '.env').write_bytes(b'leave-me\n')
        os.utime(outside / '.env', (1_700_000_000, 1_700_000_000))
        (self.remote / 'ok').mkdir()

        traversal = self.invoke({'../outside/.env': b'nope\n', 'ok/.env': b'also-nope\n'})
        self.assertNotEqual(traversal.returncode, 0)
        self.assertIn(b'refused', traversal.stderr)
        self.assertNotIn(b'nope', traversal.stdout)
        self.assertEqual((outside / '.env').read_bytes(), b'leave-me\n')
        self.assertFalse((self.remote / 'ok' / '.env').exists())
        nested = self.invoke({'foo/../../outside/.env': b'nope\n'})
        self.assertNotEqual(nested.returncode, 0)
        self.assertEqual((outside / '.env').read_bytes(), b'leave-me\n')

        absolute = self.invoke({str(outside / '.env'): b'overwrite\n'})
        self.assertNotEqual(absolute.returncode, 0)
        self.assertIn(b'refused', absolute.stderr)
        self.assertEqual((outside / '.env').read_bytes(), b'leave-me\n')
        self.assertEqual((outside / '.env').stat().st_mtime, 1_700_000_000)

        real = self.remote / 'real-parent'
        real.mkdir()
        link = self.remote / 'link-parent'
        link.symlink_to(real)
        linked = self.invoke({'link-parent/.env': b'symlink-secret\n'})
        self.assertNotEqual(linked.returncode, 0)
        self.assertIn(b'refused', linked.stderr)
        self.assertNotIn(b'symlink-secret', linked.stdout)
        self.assertNotIn(b'symlink-secret', linked.stderr)
        self.assertFalse((real / '.env').exists())
        self.assertEqual(list(real.iterdir()), [])
        self.assertTrue(link.is_symlink())

        held = self.remote / 'held'
        held.write_bytes(b'keep\n')
        os.utime(held, (1_700_000_000, 1_700_000_000))
        (self.remote / '.env').symlink_to(held)
        target_link = self.invoke({'.env': b'overwrite\n'})
        self.assertNotEqual(target_link.returncode, 0)
        self.assertIn(b'refused', target_link.stderr)
        self.assertNotIn(b'overwrite', target_link.stdout)
        self.assertEqual(held.read_bytes(), b'keep\n')
        self.assertEqual(held.stat().st_mtime, 1_700_000_000)
        self.assertTrue((self.remote / '.env').is_symlink())


class EnsureTests(GitEnv):
    def test_printed_lines_match_rule_6(self):
        init_repo(self.root)
        write_file(self.root, '.env', b'root\n')
        write_file(self.root, 'bad/.env', b'bad\n')
        write_file(self.root, 'other/.env', b'other\n')
        write_file(self.root, 'web/.env', b'web\n')
        write_file(self.root, 'oversized/.env', b'x' * (70 * 1024))
        connection = Connection([json.dumps({'files': {
            '.env': 'copied',
            'bad/.env': 'failed',
            'other/.env': 'no folder',
            'web/.env': 'differs',
        }}).encode()])
        receipt, printed, err = self.run_ensure(connection)
        self.assertEqual(err, '')
        self.assertNotIn('error', receipt)
        self.assertEqual(receipt['files'], {
            '.env': 'copied',
            'bad/.env': 'failed',
            'other/.env': 'no folder',
            'oversized/.env': 'too large',
            'web/.env': 'differs',
        })
        self.assertEqual(printed, (
            'Copied .env to space (mode 600).\n'
            'Did not copy bad/.env to space: the copy failed.\n'
            "Did not copy other/.env to space: its folder doesn't exist there.\n"
            'Did not copy oversized/.env to space: it is larger than 64 KiB.\n'
            "Left web/.env on space as it is: it differs from the Mac's copy.\n"
        ))
        payload = json.loads(connection.calls[0]['payload'])
        self.assertNotIn('oversized/.env', payload['files'])
        self.assertEqual(connection.calls[0]['code'], env_files.REMOTE)
        saved = read_json(self.state / env_files.RECEIPT)
        self.assertEqual(saved, receipt)
        self.assertEqual(stat.S_IMODE((self.state / env_files.RECEIPT).stat().st_mode), 0o600)

    def test_prints_nothing_when_no_files_or_all_current(self):
        init_repo(self.root)
        empty = Connection([])
        receipt, printed, err = self.run_ensure(empty)
        self.assertEqual(empty.calls, [])
        self.assertEqual(printed, '')
        self.assertEqual(err, '')
        self.assertEqual(receipt['files'], {})
        self.assertNotIn('error', receipt)
        self.assertEqual(env_files.status_line(receipt, 'space'), 'Env files: none found')

        write_file(self.root, '.env', b'same\n')
        write_file(self.root, 'web/.env', b'web\n')
        current = Connection([json.dumps({'files': {'.env': 'current', 'web/.env': 'current'}}).encode()])
        receipt, printed, err = self.run_ensure(current)
        self.assertEqual(printed, '')
        self.assertEqual(err, '')
        self.assertEqual(receipt['files'], {'.env': 'current', 'web/.env': 'current'})
        self.assertEqual(env_files.status_line(receipt, 'space'), 'Env files: .env current; web/.env current')

    def test_canary_never_leaks(self):
        init_repo(self.root)
        write_file(self.root, '.env', CANARY + '\n')
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob('*'))
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)

        class Echo:
            def python(self, code, payload=b'', timeout=60):
                self.code, self.payload, self.timeout = code, payload, timeout
                return json.dumps({
                    'files': {'.env': 'copied'},
                    'echo': payload.decode(),
                    'debug': CANARY,
                }).encode()

        connection = Echo()
        with mock.patch('tempfile.mkstemp', side_effect=AssertionError('temp file')), \
                mock.patch('tempfile.NamedTemporaryFile', side_effect=AssertionError('temp file')):
            receipt, printed, err = self.run_ensure(connection, remote_root='/workspaces/ordinary')
        receipt_text = (self.state / env_files.RECEIPT).read_text()
        logged = ' '.join(str(record) for record in records)
        for blob in (printed, err, receipt_text, logged, connection.code):
            self.assertNotIn(CANARY, blob)
        self.assertNotIn(CANARY, connection.payload.decode())
        self.assertIn(base64.b64encode((CANARY + '\n').encode()).decode('ascii'), connection.payload.decode())
        self.assertNotIn(CANARY, json.dumps(receipt))
        self.assertEqual(receipt['files'], {'.env': 'copied'})
        self.assertEqual(set(receipt), {'codespace', 'checked_at', 'files'})
        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob('*'))
        self.assertEqual(before, after)
        for path in self.state.rglob('*'):
            if path.is_file():
                self.assertNotIn(CANARY.encode(), path.read_bytes())

    def test_ensure_records_a_value_error_without_raising(self):
        init_repo(self.root)
        write_file(self.root, '.env', b'secret\n')
        connection = Connection([ValueError(
            'Remote Python command failed (exit 2); inspect the remote checkout and Python environment.')])
        receipt, printed, err = self.run_ensure(connection)
        self.assertEqual(receipt['error'], 'Remote Python command failed (exit 2)')
        self.assertEqual(receipt['files'], {})
        self.assertEqual(printed, 'Could not check .env files on space: '
                         'Remote Python command failed (exit 2). Run task work:cs again.\n')
        self.assertEqual(err, '')
        self.assertEqual(read_json(self.state / env_files.RECEIPT)['error'], receipt['error'])
        self.assertNotIn('secret', printed)
        self.assertNotIn('secret', (self.state / env_files.RECEIPT).read_text())

    def test_ensure_records_an_os_error_without_raising(self):
        init_repo(self.root)
        write_file(self.root, '.env', b'secret\n')
        connection = Connection([OSError('ssh /Users/mannu/.ssh/codespaces.auto')])
        receipt, printed, err = self.run_ensure(connection)
        self.assertEqual(receipt['error'], 'OSError')
        self.assertEqual(receipt['files'], {})
        self.assertEqual(printed, 'Could not check .env files on space: OSError. Run task work:cs again.\n')
        self.assertNotIn('/Users/mannu', printed)
        self.assertNotIn('/Users/mannu', (self.state / env_files.RECEIPT).read_text())
        self.assertNotIn('secret', printed)
        self.assertEqual(err, '')

    def test_ensure_records_a_git_failure_and_copies_nothing(self):
        connection = Connection([])
        receipt, printed, err = self.run_ensure(connection)
        self.assertEqual(connection.calls, [])
        self.assertEqual(receipt['error'], 'git ls-files failed')
        self.assertEqual(receipt['files'], {})
        self.assertEqual(printed, 'Could not check .env files on space: git ls-files failed. '
                         'Run task work:cs again.\n')
        self.assertEqual(err, '')
        self.assertEqual(len(printed.splitlines()), 1)


class EnsureOnTests(GitEnv):
    def test_ensure_on_opens_a_fresh_connection(self):
        init_repo(self.root)
        write_file(self.root, '.env', b'root\n')
        opened = []

        class Opened:
            def __init__(self, codespace, *, state):
                opened.append((codespace, state))
                self.codespace, self.state = codespace, state

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                opened.append('closed')
                return None

            def python(self, code, payload=b'', timeout=60):
                opened.append('python')
                return json.dumps({'files': {'.env': 'copied'}}).encode()

        with mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection', Opened):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                receipt = env_files.ensure_on('space', self.state, self.root, '/workspaces/ordinary')
        self.assertEqual(opened[0], ('space', self.state))
        self.assertIn('python', opened)
        self.assertEqual(opened[-1], 'closed')
        self.assertEqual(receipt['files'], {'.env': 'copied'})
        self.assertNotIn('error', receipt)
        self.assertEqual(out.getvalue(), 'Copied .env to space (mode 600).\n')


class StatusTests(unittest.TestCase):
    def receipt(self, **updates):
        value = {'codespace': 'space', 'checked_at': WHEN, 'files': {'.env': 'copied', 'web/.env': 'current'}}
        value.update(updates)
        return value

    def test_status_line_lists_receipt_files(self):
        self.assertEqual(env_files.status_line(self.receipt(), 'space'),
                         'Env files: .env copied; web/.env current')
        self.assertEqual(env_files.status_line(self.receipt(files={
            'web/.env': 'differs', '.env': 'no folder', 'a/.env': 'too large', 'b/.env': 'failed'}), 'space'),
                         'Env files: .env no folder; a/.env too large; b/.env failed; web/.env differs')

    def test_status_line_none_found(self):
        self.assertEqual(env_files.status_line(self.receipt(files={}), 'space'), 'Env files: none found')

    def test_status_line_not_checked_for_another_codespace(self):
        self.assertEqual(env_files.status_line(self.receipt(codespace='other'), 'space'),
                         'Env files: not checked on space; task work:cs copies them')

    def test_status_line_not_checked_without_a_receipt(self):
        self.assertEqual(env_files.status_line(None, 'space'),
                         'Env files: not checked on space; task work:cs copies them')
        self.assertIsNone(env_files.status_line(self.receipt(), None))
        self.assertIsNone(env_files.status_line(self.receipt(), ''))

    def test_read_returns_none_for_an_old_state_dir_with_no_receipt(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name).resolve() / 'state'
        state.mkdir()
        self.assertIsNone(env_files.read(state))
        self.assertIsNone(env_files.read(state / 'absent'))
        atomic_write(state / env_files.RECEIPT, b'{not json\n')
        self.assertIsNone(env_files.read(state))

    def test_status_text_shows_the_env_line_next_to_hooks(self):
        shown = workspace.status_text(status_value(env_files=self.receipt()))
        self.assertIn('Env files: .env copied; web/.env current', shown)
        self.assertLess(shown.index('Herdr hooks:'), shown.index('Env files:'))
        missing = workspace.status_text(status_value())
        self.assertIn('Env files: not checked on space; task work:cs copies them', missing)
        bare = status_value()
        del bare['workspace']
        self.assertNotIn('Env files:', workspace.status_text(bare))

    def test_status_line_check_failed_does_not_echo_arbitrary_text(self):
        line = env_files.status_line(self.receipt(error='git ls-files failed', files={}), 'space')
        self.assertEqual(line, 'Env files: check failed on space (git ls-files failed); task work:cs copies them')
        tainted = self.receipt(files={'.env': CANARY})
        self.assertNotIn(CANARY, env_files.status_line(tainted, 'space'))
        self.assertIn('failed', env_files.status_line(tainted, 'space'))


if __name__ == '__main__':
    unittest.main()
