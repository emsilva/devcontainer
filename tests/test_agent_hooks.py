"""Herdr resume-hook install on a Codespace, with fakes only — no real Herdr, gh, or home state."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from devcontainer_setup.project_sync import agent_hooks
from devcontainer_setup.project_sync.arming import format_time
from devcontainer_setup.project_sync.state import read_json
from devcontainer_setup.project_sync import sync_cycle, workspace


# Owner's Mac, measured 2026-10-26: claude and codex outdated.
MAC_STATUS = """\
pi: not installed (/Users/mannu/.pi/agent/extensions/herdr-agent-state.ts)
omp: not installed (/Users/mannu/.omp/agent/extensions/herdr-omp-agent-state.ts)
claude: outdated (v10 < v11) (/Users/mannu/.claude/hooks/herdr-agent-state.sh)
codex: outdated (v8 < v10) (/Users/mannu/.codex/herdr-agent-state.sh)
copilot: not installed (/Users/mannu/.copilot/hooks/herdr-agent-state.sh)
opencode: current (v13) (/Users/mannu/.config/opencode/plugins/herdr-agent-state.js)
kilo: not installed (/Users/mannu/.config/kilo/plugin/herdr-agent-state.js)
grok: not installed (/Users/mannu/.grok/hooks/herdr-agent-state.sh)
letta (experimental): not installed (/Users/mannu/.letta/hooks/herdr-agent-session.sh)
"""

# A Codespace after the hand fix.
CURRENT_STATUS = """\
claude: current (v11) (/home/vscode/.claude/hooks/herdr-agent-state.sh)
codex: current (v10) (/home/vscode/.codex/herdr-agent-state.sh)
opencode: current (v13) (/home/vscode/.config/opencode/plugins/herdr-agent-state.js)
"""

WHEN = 1_700_000_000.5


def remote_body(status='', installed=None, herdr=True):
    if not herdr:
        return json.dumps({'herdr': False}).encode()
    return json.dumps({'herdr': True, 'installed': installed or {}, 'status': status}).encode()


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


class ParseTests(unittest.TestCase):
    def test_parse_maps_the_owner_mac_fixture(self):
        self.assertEqual(agent_hooks.parse(MAC_STATUS), {
            'claude': 'outdated', 'codex': 'outdated', 'opencode': 'current'})

    def test_parse_maps_the_codespace_fixture(self):
        self.assertEqual(agent_hooks.parse(CURRENT_STATUS), {
            'claude': 'current', 'codex': 'current', 'opencode': 'current'})

    def test_parse_missing_codex_line_is_unknown(self):
        text = MAC_STATUS.replace('codex: outdated (v8 < v10) (/Users/mannu/.codex/herdr-agent-state.sh)\n', '')
        self.assertNotIn('\ncodex:', '\n' + text)
        parsed = agent_hooks.parse(text)
        self.assertEqual(parsed['codex'], 'unknown')
        self.assertEqual(parsed['claude'], 'outdated')
        self.assertEqual(parsed['opencode'], 'current')

    def test_parse_ignores_a_parenthetical_name(self):
        text = (
            'letta (experimental): not installed (/Users/mannu/.letta/hooks/herdr-agent-session.sh)\n'
            'note codex: current (v1) (/tmp/not-a-codex-line)\n'
            '  opencode: current (v13) (/tmp/indented)\n'
        )
        self.assertEqual(agent_hooks.parse(text), {
            'claude': 'unknown', 'codex': 'unknown', 'opencode': 'unknown'})
        # A recognized word is required; a lookalike is not current.
        self.assertEqual(agent_hooks.parse('claude: currently fine\ncodex: not installed (/tmp)\n'), {
            'claude': 'unknown', 'codex': 'not installed', 'opencode': 'unknown'})


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Resolve: macOS tempfile lives under /var, a symlink atomic_write will not follow.
        self.state = Path(self.temp.name).resolve() / 'state'

    def run_ensure(self, connection):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            receipt = agent_hooks.ensure(connection, 'space', self.state, now=lambda: WHEN)
        return receipt, out.getvalue()

    def test_ensure_installs_outdated_agents_from_the_mac_fixture(self):
        connection = Connection([
            remote_body(MAC_STATUS),
            remote_body(CURRENT_STATUS, {'claude': 0, 'codex': 0}),
        ])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual([call['payload'] for call in connection.calls],
                         [b'[]', b'["claude", "codex"]'])
        self.assertEqual(connection.calls[0]['code'], agent_hooks.REMOTE)
        self.assertGreaterEqual(connection.calls[0]['timeout'], 30)
        self.assertGreaterEqual(connection.calls[1]['timeout'], 30 + 60 * 2)
        self.assertEqual(receipt['codespace'], 'space')
        self.assertEqual(receipt['checked_at'], WHEN)
        self.assertEqual(receipt['hooks'], {
            'claude': 'current', 'codex': 'current', 'opencode': 'current'})
        self.assertEqual(receipt['installed'], ['claude', 'codex'])
        self.assertNotIn('error', receipt)
        self.assertEqual(printed, 'Installed Herdr resume hooks on space for claude, codex; '
                         'its agents can now come back after a restart.\n')
        saved = read_json(self.state / agent_hooks.RECEIPT)
        self.assertEqual(saved, receipt)
        text = (self.state / agent_hooks.RECEIPT).read_text()
        self.assertNotIn('/Users/mannu', text)
        self.assertNotIn('herdr-agent-state', text)

    def test_ensure_skips_install_when_hooks_are_current(self):
        connection = Connection([remote_body(CURRENT_STATUS)])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(len(connection.calls), 1)
        self.assertEqual(connection.calls[0]['payload'], b'[]')
        self.assertEqual(receipt['installed'], [])
        self.assertEqual(receipt['hooks'], {
            'claude': 'current', 'codex': 'current', 'opencode': 'current'})
        self.assertNotIn('error', receipt)
        self.assertEqual(printed, 'Herdr resume hooks are current on space (claude, codex, opencode).\n')

    def test_ensure_installs_unknown_and_not_installed_in_agent_order(self):
        # Text order is not agent order; a missing line is unknown, which is not current.
        before = 'opencode: not installed (/tmp/o)\nclaude: outdated (v1 < v2) (/tmp/c)\n'
        connection = Connection([remote_body(before), remote_body(CURRENT_STATUS, {
            'claude': 0, 'codex': 0, 'opencode': 0})])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(connection.calls[1]['payload'], b'["claude", "codex", "opencode"]')
        self.assertEqual(receipt['installed'], ['claude', 'codex', 'opencode'])
        self.assertIn('for claude, codex, opencode;', printed)

    def test_ensure_records_a_python_value_error(self):
        connection = Connection([ValueError(
            'Remote Python command failed (exit 2); inspect the remote checkout and Python environment.')])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(receipt['error'], 'Remote Python command failed (exit 2)')
        self.assertEqual(receipt['installed'], [])
        self.assertEqual(printed, 'Could not check Herdr resume hooks on space '
                         '(Remote Python command failed (exit 2)); its agents may not come back '
                         'after a restart. Run task work:cs again.\n')
        self.assertEqual(read_json(self.state / agent_hooks.RECEIPT)['error'], receipt['error'])

    def test_ensure_records_an_os_error_without_raising(self):
        connection = Connection([OSError('ssh /Users/mannu/.ssh/codespaces.auto')])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(receipt['error'], 'OSError')
        self.assertNotIn('/Users/mannu', printed)
        self.assertNotIn('/Users/mannu', (self.state / agent_hooks.RECEIPT).read_text())
        self.assertIn('Could not check Herdr resume hooks on space (OSError);', printed)

    def test_ensure_records_a_missing_herdr(self):
        connection = Connection([remote_body(herdr=False)])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(len(connection.calls), 1)
        self.assertEqual(receipt['error'], 'Herdr is not installed there')
        self.assertEqual(printed, 'Could not check Herdr resume hooks on space '
                         '(Herdr is not installed there); its agents may not come back '
                         'after a restart. Run task work:cs again.\n')

    def test_ensure_reports_an_agent_still_outdated_after_install(self):
        after = ('claude: outdated (v10 < v11) (/tmp/claude)\n'
                 'codex: current (v10) (/tmp/codex)\n'
                 'opencode: current (v13) (/tmp/opencode)\n')
        connection = Connection([
            remote_body(MAC_STATUS),
            remote_body(after, {'claude': 1, 'codex': 0}),
        ])
        receipt, printed = self.run_ensure(connection)
        self.assertNotIn('error', receipt)
        self.assertEqual(receipt['hooks']['claude'], 'outdated')
        self.assertEqual(receipt['hooks']['codex'], 'current')
        self.assertEqual(receipt['installed'], ['claude', 'codex'])
        self.assertEqual(printed, 'Herdr resume hooks are not current on space (claude outdated); '
                         'its agents may not come back after a restart. Run task work:cs again.\n')

    def test_ensure_records_unparseable_json(self):
        connection = Connection([b'not json'])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(receipt['error'], 'invalid hooks response')
        self.assertIn('(invalid hooks response);', printed)
        self.assertEqual(len(printed.splitlines()), 1)


class EnsureOnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state'

    def test_ensure_on_turns_a_connection_error_into_a_receipt(self):
        class Closed:
            def __init__(self, codespace, *, state):
                self.codespace, self.state = codespace, state

            def __enter__(self):
                raise ValueError('Codespace SSH key missing (~/.ssh/codespaces.auto); configure SSH explicitly first.')

            def __exit__(self, *exc):
                return None

        with mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection', Closed):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                receipt = agent_hooks.ensure_on('space', self.state)
        self.assertEqual(receipt['error'], 'Codespace SSH key missing (~/.ssh/codespaces.auto)')
        self.assertIn('Could not check Herdr resume hooks on space', out.getvalue())
        self.assertEqual(read_json(self.state / agent_hooks.RECEIPT)['codespace'], 'space')

    def test_ensure_on_opens_a_fresh_connection_for_the_codespace(self):
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
                return remote_body(CURRENT_STATUS)

        with mock.patch('devcontainer_setup.project_sync.transport.CodespaceConnection', Opened):
            with contextlib.redirect_stdout(io.StringIO()):
                receipt = agent_hooks.ensure_on('space', self.state)
        self.assertEqual(opened[0], ('space', self.state))
        self.assertEqual(opened[-1], 'closed')
        self.assertEqual(receipt['hooks']['claude'], 'current')
        self.assertNotIn('error', receipt)


class RemoteCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / 'home'
        self.bindir = root / 'bin'
        self.home.mkdir()
        self.bindir.mkdir()
        self.python = shutil.which('python3')
        self.assertTrue(self.python)

    def install_herdr(self, directory, status=CURRENT_STATUS):
        (self.home / 'herdr-status.txt').write_text(status)
        script = directory / 'herdr'
        script.write_text('#!' + self.python + '''
import json, os, sys
home = os.environ["HOME"]
with open(home + "/herdr-argv.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["integration", "status"]:
    with open(home + "/herdr-status.txt", "rb") as status:
        sys.stdout.buffer.write(status.read())
''')
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return script

    def invoke(self, payload, path=None):
        env = {'HOME': str(self.home), 'PATH': str(self.bindir if path is None else path), 'LANG': 'C'}
        return subprocess.run(
            [self.python, '-I', '-B', '-c', agent_hooks.REMOTE],
            input=payload.encode() if isinstance(payload, str) else payload,
            capture_output=True, env=env, timeout=20)

    def argv_log(self):
        path = self.home / 'herdr-argv.log'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def test_remote_code_installs_only_the_requested_agents(self):
        self.install_herdr(self.bindir)
        self.assertIn('timeout=60', agent_hooks.REMOTE)
        self.assertIn('timeout=30', agent_hooks.REMOTE)
        result = self.invoke('["codex"]')
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertEqual(body, {'herdr': True, 'installed': {'codex': 0}, 'status': CURRENT_STATUS})
        self.assertEqual(self.argv_log(), [
            ['integration', 'install', 'codex'],
            ['integration', 'status'],
        ])

    def test_remote_code_refuses_a_name_outside_the_three(self):
        self.install_herdr(self.bindir)
        result = self.invoke('["claude", "pi"]')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.argv_log(), [])
        bad = self.invoke('{"agent": "claude"}')
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.argv_log(), [])

    def test_remote_code_reports_no_herdr_when_it_is_not_on_path_or_in_local_bin(self):
        result = self.invoke('[]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'herdr': False})
        self.assertFalse((self.home / '.local/bin/herdr').exists())

    def test_remote_code_finds_herdr_in_local_bin(self):
        local = self.home / '.local/bin'
        local.mkdir(parents=True)
        self.install_herdr(local)
        result = self.invoke('["opencode"]')
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertTrue(body['herdr'])
        self.assertEqual(body['installed'], {'opencode': 0})
        self.assertEqual(self.argv_log()[0], ['integration', 'install', 'opencode'])


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


class StatusTests(unittest.TestCase):
    def receipt(self, **updates):
        value = {
            'codespace': 'space',
            'checked_at': WHEN,
            'hooks': {'claude': 'current', 'codex': 'current', 'opencode': 'current'},
            'installed': [],
        }
        value.update(updates)
        return value

    def test_status_line_current(self):
        line = agent_hooks.status_line(self.receipt(), 'space')
        self.assertEqual(line, 'Herdr hooks: claude, codex, opencode current on space (checked '
                         + format_time(WHEN) + ')')

    def test_status_line_lists_only_agents_that_are_not_current(self):
        hooks = {'claude': 'outdated', 'codex': 'current', 'opencode': 'not installed'}
        line = agent_hooks.status_line(self.receipt(hooks=hooks), 'space')
        self.assertEqual(line, 'Herdr hooks: claude outdated, opencode not installed on space; '
                         'task work:cs installs them')

    def test_status_line_check_failed(self):
        line = agent_hooks.status_line(self.receipt(error='invalid hooks response'), 'space')
        self.assertEqual(line, 'Herdr hooks: check failed on space (invalid hooks response); '
                         'task work:cs checks again')

    def test_status_line_not_checked_for_another_codespace(self):
        line = agent_hooks.status_line(self.receipt(codespace='other'), 'space')
        self.assertEqual(line, 'Herdr hooks: not checked on space; task work:cs checks them')

    def test_status_line_not_checked_without_a_receipt(self):
        self.assertEqual(agent_hooks.status_line(None, 'space'),
                         'Herdr hooks: not checked on space; task work:cs checks them')
        self.assertIsNone(agent_hooks.status_line(self.receipt(), None))

    def test_status_text_shows_the_hooks_line_and_tolerates_a_missing_key(self):
        shown = workspace.status_text(status_value(herdr_hooks=self.receipt()))
        self.assertIn('Herdr hooks: claude, codex, opencode current on space (checked '
                      + format_time(WHEN) + ')', shown)
        missing = workspace.status_text(status_value())
        self.assertIn('Herdr hooks: not checked on space; task work:cs checks them', missing)
        bare = status_value()
        del bare['workspace']
        self.assertNotIn('Herdr hooks:', workspace.status_text(bare))

    def test_read_returns_none_when_the_receipt_is_missing_or_unreadable(self):
        self.assertIsNone(agent_hooks.read(Path(tempfile.mkdtemp()).resolve() / 'absent'))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name).resolve() / 'state'
        from devcontainer_setup.project_sync.state import atomic_write
        atomic_write(state / agent_hooks.RECEIPT, b'{not json\n')
        self.assertIsNone(agent_hooks.read(state))


if __name__ == '__main__':
    unittest.main()
