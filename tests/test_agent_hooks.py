"""Herdr resume-hook install on a Codespace, with fakes only — no real Herdr, gh, or home state."""
import contextlib
import io
import json
import os
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


SKILL_FIXTURE = b'---\nname: herdr\ndescription: fixture\n'


def remote_body(status='', installed=None, herdr=True, skill='current', feedback='current'):
    if not herdr:
        return json.dumps({'herdr': False}).encode()
    return json.dumps({
        'herdr': True, 'installed': installed or {}, 'status': status,
        'skill': skill, 'feedback': feedback,
    }).encode()


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
            receipt = agent_hooks.ensure(connection, 'space', self.state, '/workspaces/ordinary', now=lambda: WHEN)
        return receipt, out.getvalue()

    def test_ensure_installs_outdated_agents_from_the_mac_fixture(self):
        connection = Connection([
            remote_body(MAC_STATUS),
            remote_body(CURRENT_STATUS, {'claude': 0, 'codex': 0}),
        ])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(json.loads(connection.calls[0]['payload']),
                         {'install': [], 'skill': True, 'root': '/workspaces/ordinary'})
        self.assertEqual(json.loads(connection.calls[1]['payload']),
                         {'install': ['claude', 'codex'], 'skill': False})
        self.assertEqual(connection.calls[0]['code'], agent_hooks.REMOTE)
        self.assertGreaterEqual(connection.calls[0]['timeout'], 30 + 30)
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
        self.assertEqual(json.loads(connection.calls[0]['payload']),
                         {'install': [], 'skill': True, 'root': '/workspaces/ordinary'})
        self.assertEqual(receipt['skill'], 'current')
        self.assertEqual(receipt['installed'], [])
        self.assertEqual(receipt['hooks'], {
            'claude': 'current', 'codex': 'current', 'opencode': 'current'})
        self.assertNotIn('error', receipt)
        self.assertEqual(printed, 'Herdr resume hooks are current on space (claude, codex, opencode).\n')
        self.assertNotIn('Herdr skill', printed)

    def test_ensure_prints_a_line_when_the_skill_is_installed(self):
        connection = Connection([remote_body(CURRENT_STATUS, skill='installed')])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(json.loads(connection.calls[0]['payload'])['skill'], True)
        self.assertEqual(len(connection.calls), 1)
        self.assertEqual(receipt['skill'], 'installed')
        self.assertNotIn('error', receipt)
        self.assertEqual(printed, 'Herdr resume hooks are current on space (claude, codex, opencode).\n'
                         'Installed the Herdr skill on space (the copy bundled with its Herdr).\n')

    def test_ensure_prints_a_line_when_the_skill_fails(self):
        connection = Connection([remote_body(CURRENT_STATUS, skill='failed')])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(receipt['skill'], 'failed')
        self.assertNotIn('error', receipt)
        self.assertEqual(printed, 'Herdr resume hooks are current on space (claude, codex, opencode).\n'
                         'Could not install the Herdr skill on space; agents there cannot load it. '
                         'Run task work:cs again.\n')

    def test_ensure_prints_no_skill_line_when_it_is_already_managed(self):
        connection = Connection([remote_body(CURRENT_STATUS, skill='managed elsewhere')])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(receipt['skill'], 'managed elsewhere')
        self.assertEqual(printed.splitlines(),
                         ['Herdr resume hooks are current on space (claude, codex, opencode).'])

    def test_ensure_installs_unknown_and_not_installed_in_agent_order(self):
        # Text order is not agent order; a missing line is unknown, which is not current.
        before = 'opencode: not installed (/tmp/o)\nclaude: outdated (v1 < v2) (/tmp/c)\n'
        connection = Connection([remote_body(before), remote_body(CURRENT_STATUS, {
            'claude': 0, 'codex': 0, 'opencode': 0})])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(json.loads(connection.calls[1]['payload']),
                         {'install': ['claude', 'codex', 'opencode'], 'skill': False})
        self.assertEqual(receipt['installed'], ['claude', 'codex', 'opencode'])
        self.assertIn('for claude, codex, opencode;', printed)

    def test_ensure_records_a_python_value_error(self):
        connection = Connection([ValueError(
            'Remote Python command failed (exit 2); inspect the remote checkout and Python environment.')])
        receipt, printed = self.run_ensure(connection)
        self.assertEqual(receipt['error'], 'Remote Python command failed (exit 2)')
        self.assertEqual(receipt['skill'], 'unknown')
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

    def test_ensure_records_feedback_from_the_hooks_call_and_prints_only_two_states(self):
        root = '/workspaces/ordinary'
        out = io.StringIO()
        moved = Connection([remote_body(CURRENT_STATUS, feedback='moved')])
        with contextlib.redirect_stdout(out):
            receipt = agent_hooks.ensure(moved, 'space', self.state, root, now=lambda: WHEN)
        self.assertEqual(json.loads(moved.calls[0]['payload']),
                         {'install': [], 'skill': True, 'root': root})
        self.assertEqual(len(moved.calls), 1)
        self.assertEqual(receipt['feedback'], 'moved')
        self.assertEqual(out.getvalue(),
                         'Herdr resume hooks are current on space (claude, codex, opencode).\n'
                         'Moved handoff feedback on space into .scratch/handoff-feedback so it syncs.\n')
        self.assertEqual(read_json(self.state / agent_hooks.RECEIPT)['feedback'], 'moved')

        out = io.StringIO()
        elsewhere = Connection([remote_body(CURRENT_STATUS, feedback='managed elsewhere')])
        with contextlib.redirect_stdout(out):
            receipt = agent_hooks.ensure(elsewhere, 'space', self.state, root, now=lambda: WHEN)
        self.assertEqual(receipt['feedback'], 'managed elsewhere')
        self.assertEqual(out.getvalue(),
                         'Herdr resume hooks are current on space (claude, codex, opencode).\n'
                         'Handoff feedback on space is managed elsewhere; left it unchanged.\n')

        for state in ('current', 'linked', 'no scratch', 'failed'):
            with self.subTest(state=state):
                out = io.StringIO()
                connection = Connection([remote_body(CURRENT_STATUS, feedback=state)])
                with contextlib.redirect_stdout(out):
                    receipt = agent_hooks.ensure(connection, 'space', self.state, root, now=lambda: WHEN)
                self.assertEqual(receipt['feedback'], state)
                self.assertEqual(out.getvalue().splitlines(),
                                 ['Herdr resume hooks are current on space (claude, codex, opencode).'])

    def test_ensure_sends_the_checkout_on_the_hooks_call_only(self):
        connection = Connection([
            remote_body(MAC_STATUS, feedback='linked'),
            remote_body(CURRENT_STATUS, {'claude': 0, 'codex': 0}),
        ])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            receipt = agent_hooks.ensure(connection, 'space', self.state, '/workspaces/ordinary', now=lambda: WHEN)
        self.assertEqual(len(connection.calls), 2)
        self.assertEqual(json.loads(connection.calls[0]['payload']),
                         {'install': [], 'skill': True, 'root': '/workspaces/ordinary'})
        self.assertNotIn('root', json.loads(connection.calls[1]['payload']))
        self.assertEqual(receipt['feedback'], 'linked')
        self.assertNotIn('Moved handoff', out.getvalue())
        self.assertNotIn('managed elsewhere', out.getvalue())


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
                receipt = agent_hooks.ensure_on('space', self.state, '/workspaces/ordinary')
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
                receipt = agent_hooks.ensure_on('space', self.state, '/workspaces/ordinary')
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
if sys.argv[1:] == ["--skill"]:
    path = home + "/herdr-skill.txt"
    if os.path.exists(path):
        with open(path, "rb") as skill:
            sys.stdout.buffer.write(skill.read())
    sys.exit(int(os.environ.get("HERDR_SKILL_CODE", "0")))
if sys.argv[1:3] == ["integration", "status"]:
    with open(home + "/herdr-status.txt", "rb") as status:
        sys.stdout.buffer.write(status.read())
''')
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return script

    def invoke(self, payload, path=None, extra_env=None):
        env = {'HOME': str(self.home), 'PATH': str(self.bindir if path is None else path), 'LANG': 'C'}
        if extra_env:
            env.update(extra_env)
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
        result = self.invoke('{"install": ["codex"], "skill": false}')
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertEqual(body, {'herdr': True, 'installed': {'codex': 0}, 'status': CURRENT_STATUS})
        self.assertEqual(self.argv_log(), [
            ['integration', 'install', 'codex'],
            ['integration', 'status'],
        ])

    def test_remote_code_refuses_a_name_outside_the_three(self):
        self.install_herdr(self.bindir)
        result = self.invoke('{"install": ["claude", "pi"], "skill": true}')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.argv_log(), [])
        bad = self.invoke('{"agent": "claude"}')
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.argv_log(), [])

    def test_remote_code_reports_no_herdr_when_it_is_not_on_path_or_in_local_bin(self):
        result = self.invoke('{"install": [], "skill": true}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'herdr': False})
        self.assertFalse((self.home / '.local/bin/herdr').exists())

    def test_remote_code_finds_herdr_in_local_bin(self):
        local = self.home / '.local/bin'
        local.mkdir(parents=True)
        self.install_herdr(local)
        result = self.invoke('{"install": ["opencode"], "skill": false}')
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertTrue(body['herdr'])
        self.assertEqual(body['installed'], {'opencode': 0})
        self.assertEqual(self.argv_log()[0], ['integration', 'install', 'opencode'])

    def skill_path(self):
        return self.home / '.claude' / 'skills' / 'herdr' / 'SKILL.md'

    def write_skill_fixture(self, data=SKILL_FIXTURE):
        (self.home / 'herdr-skill.txt').write_bytes(data)

    def test_remote_code_installs_a_missing_skill(self):
        self.install_herdr(self.bindir)
        self.write_skill_fixture()
        result = self.invoke('{"install": [], "skill": true}')
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertEqual(body['skill'], 'installed')
        self.assertEqual(self.skill_path().read_bytes(), SKILL_FIXTURE)
        self.assertEqual(self.argv_log()[0], ['--skill'])

    def test_remote_code_leaves_an_identical_skill_current(self):
        self.install_herdr(self.bindir)
        self.write_skill_fixture()
        target = self.skill_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(SKILL_FIXTURE)
        os.utime(target, (1_700_000_000, 1_700_000_000))
        result = self.invoke('{"install": [], "skill": true}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['skill'], 'current')
        self.assertEqual(target.read_bytes(), SKILL_FIXTURE)
        self.assertEqual(target.stat().st_mtime, 1_700_000_000)

    def test_remote_code_replaces_a_different_skill(self):
        self.install_herdr(self.bindir)
        self.write_skill_fixture()
        target = self.skill_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(b'---\nold skill\n')
        result = self.invoke('{"install": [], "skill": true}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['skill'], 'installed')
        self.assertEqual(target.read_bytes(), SKILL_FIXTURE)

    def test_remote_code_leaves_a_symlinked_skill_directory_alone(self):
        self.install_herdr(self.bindir)
        self.write_skill_fixture()
        real = self.home / 'elsewhere'
        real.mkdir()
        (real / 'SKILL.md').write_bytes(b'hand installed\n')
        os.utime(real / 'SKILL.md', (1_700_000_000, 1_700_000_000))
        link_parent = self.home / '.claude' / 'skills'
        link_parent.mkdir(parents=True)
        link = link_parent / 'herdr'
        link.symlink_to(real)
        result = self.invoke('{"install": [], "skill": true}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['skill'], 'managed elsewhere')
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(real))
        self.assertEqual((real / 'SKILL.md').read_bytes(), b'hand installed\n')
        self.assertEqual((real / 'SKILL.md').stat().st_mtime, 1_700_000_000)

    def test_remote_code_leaves_a_symlinked_skill_file_alone(self):
        self.install_herdr(self.bindir)
        self.write_skill_fixture()
        real = self.home / 'real-skill.md'
        real.write_bytes(b'hand installed\n')
        os.utime(real, (1_700_000_000, 1_700_000_000))
        target = self.skill_path()
        target.parent.mkdir(parents=True)
        target.symlink_to(real)
        result = self.invoke('{"install": [], "skill": true}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['skill'], 'managed elsewhere')
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(real))
        self.assertEqual(real.read_bytes(), b'hand installed\n')
        self.assertEqual(real.stat().st_mtime, 1_700_000_000)

    def test_remote_code_does_not_write_a_skill_when_herdr_skill_fails(self):
        self.install_herdr(self.bindir)
        self.write_skill_fixture()
        failed = self.invoke('{"install": [], "skill": true}', extra_env={'HERDR_SKILL_CODE': '1'})
        self.assertEqual(failed.returncode, 0, failed.stderr)
        self.assertEqual(json.loads(failed.stdout)['skill'], 'failed')
        self.assertFalse(self.skill_path().exists())
        self.write_skill_fixture(b'not a skill\n')
        bad = self.invoke('{"install": [], "skill": true}')
        self.assertEqual(json.loads(bad.stdout)['skill'], 'failed')
        self.assertFalse(self.skill_path().exists())

    def checkout(self):
        path = Path(self.temp.name) / 'checkout'
        path.mkdir(exist_ok=True)
        return path

    def feedback_link(self):
        return self.home / '.claude' / 'handoff-feedback'

    def feedback_target(self, checkout):
        return checkout / '.scratch' / 'handoff-feedback'

    def feedback_payload(self, checkout, **extra):
        body = {'install': [], 'skill': False, 'root': str(checkout)}
        body.update(extra)
        return json.dumps(body)

    def tree_bytes(self, root):
        found = {}
        if not root.exists():
            return found
        for path in sorted(root.rglob('*')):
            if path.is_symlink() or not path.is_file():
                continue
            found[path.relative_to(root).as_posix()] = path.read_bytes()
        return found

    def test_remote_code_links_missing_handoff_feedback_on_the_hooks_call(self):
        self.install_herdr(self.bindir)
        checkout = self.checkout()
        (checkout / '.scratch').mkdir()
        result = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertEqual(body['feedback'], 'linked')
        self.assertEqual(body['status'], CURRENT_STATUS)
        self.assertEqual(self.argv_log(), [['integration', 'status']])
        link = self.feedback_link()
        target = self.feedback_target(checkout)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(target))
        self.assertTrue(target.is_dir())
        self.assertFalse(target.is_symlink())
        info = link.lstat()
        again = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(json.loads(again.stdout)['feedback'], 'current')
        self.assertEqual(link.lstat().st_ino, info.st_ino)
        self.assertEqual(os.readlink(link), str(target))

    def test_remote_code_leaves_an_exact_handoff_feedback_link_current(self):
        self.install_herdr(self.bindir)
        checkout = self.checkout()
        target = self.feedback_target(checkout)
        target.mkdir(parents=True)
        (target / 'gaps.jsonl').write_bytes(b'mac\n')
        os.utime(target / 'gaps.jsonl', (1_700_000_000, 1_700_000_000))
        link = self.feedback_link()
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        info = link.lstat()
        result = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['feedback'], 'current')
        self.assertEqual(link.lstat().st_ino, info.st_ino)
        self.assertEqual(os.readlink(link), str(target))
        self.assertEqual((target / 'gaps.jsonl').read_bytes(), b'mac\n')
        self.assertEqual((target / 'gaps.jsonl').stat().st_mtime, 1_700_000_000)

    def test_remote_code_moves_handoff_feedback_and_keeps_both_copies(self):
        self.install_herdr(self.bindir)
        checkout = self.checkout()
        target = self.feedback_target(checkout)
        (target / 'proposals').mkdir(parents=True)
        (target / 'gaps.jsonl').write_bytes(b'mac-row\n')
        (target / 'proposals' / 'keep.md').write_bytes(b'same proposal\n')
        os.utime(target / 'gaps.jsonl', (1_700_000_000, 1_700_000_000))
        os.utime(target / 'proposals' / 'keep.md', (1_700_000_100, 1_700_000_100))
        (target / 'gaps.jsonl.codespace-1').write_bytes(b'already parked\n')
        home = self.feedback_link()
        (home / 'proposals').mkdir(parents=True)
        (home / 'gaps.jsonl').write_bytes(b'codespace-row\n')
        (home / 'proposals' / 'keep.md').write_bytes(b'same proposal\n')
        (home / 'proposals' / 'new.md').write_bytes(b'codespace proposal\n')
        (home / 'only-here.txt').write_bytes(b'only on the codespace\n')
        before = self.tree_bytes(home)
        before.update(self.tree_bytes(target))
        result = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['feedback'], 'moved')
        self.assertTrue(home.is_symlink())
        self.assertTrue(stat.S_ISLNK(home.lstat().st_mode))
        self.assertFalse(stat.S_ISDIR(home.lstat().st_mode))
        self.assertEqual(os.readlink(home), str(target))
        self.assertEqual((target / 'gaps.jsonl').read_bytes(), b'mac-row\n')
        self.assertEqual((target / 'gaps.jsonl').stat().st_mtime, 1_700_000_000)
        self.assertEqual((target / 'gaps.jsonl.codespace-1').read_bytes(), b'already parked\n')
        self.assertEqual((target / 'gaps.jsonl.codespace-2').read_bytes(), b'codespace-row\n')
        self.assertEqual((target / 'proposals' / 'keep.md').read_bytes(), b'same proposal\n')
        self.assertEqual((target / 'proposals' / 'keep.md').stat().st_mtime, 1_700_000_100)
        self.assertFalse((target / 'proposals' / 'keep.md.codespace-1').exists())
        self.assertEqual((target / 'proposals' / 'new.md').read_bytes(), b'codespace proposal\n')
        self.assertEqual((target / 'only-here.txt').read_bytes(), b'only on the codespace\n')
        after = self.tree_bytes(target)
        for name, data in before.items():
            self.assertIn(data, after.values(), name)
        from devcontainer_setup.project_sync import scratch_tree
        list(scratch_tree.entries(checkout / '.scratch'))
        again = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(json.loads(again.stdout)['feedback'], 'current')
        self.assertEqual(os.readlink(home), str(target))

    def test_remote_code_leaves_a_handoff_feedback_link_that_points_elsewhere(self):
        self.install_herdr(self.bindir)
        checkout = self.checkout()
        (checkout / '.scratch').mkdir()
        elsewhere = self.home / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'gaps.jsonl').write_bytes(b'hand\n')
        os.utime(elsewhere / 'gaps.jsonl', (1_700_000_000, 1_700_000_000))
        link = self.feedback_link()
        link.parent.mkdir(parents=True)
        link.symlink_to(elsewhere)
        result = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['feedback'], 'managed elsewhere')
        self.assertEqual(os.readlink(link), str(elsewhere))
        self.assertEqual((elsewhere / 'gaps.jsonl').read_bytes(), b'hand\n')
        self.assertEqual((elsewhere / 'gaps.jsonl').stat().st_mtime, 1_700_000_000)
        self.assertFalse(self.feedback_target(checkout).exists())

    def test_remote_code_leaves_a_non_directory_handoff_feedback(self):
        self.install_herdr(self.bindir)
        checkout = self.checkout()
        (checkout / '.scratch').mkdir()
        path = self.feedback_link()
        path.parent.mkdir(parents=True)
        path.write_bytes(b'not a directory\n')
        os.utime(path, (1_700_000_000, 1_700_000_000))
        result = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['feedback'], 'managed elsewhere')
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.read_bytes(), b'not a directory\n')
        self.assertEqual(path.stat().st_mtime, 1_700_000_000)
        self.assertFalse(self.feedback_target(checkout).exists())

    def test_remote_code_reports_no_scratch_and_changes_nothing(self):
        self.install_herdr(self.bindir)
        checkout = self.checkout()
        home = self.feedback_link()
        home.mkdir(parents=True)
        (home / 'gaps.jsonl').write_bytes(b'keep\n')
        result = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['feedback'], 'no scratch')
        self.assertFalse((checkout / '.scratch').exists())
        self.assertTrue(home.is_dir())
        self.assertFalse(home.is_symlink())
        self.assertEqual((home / 'gaps.jsonl').read_bytes(), b'keep\n')

    def test_remote_code_does_not_follow_a_symlinked_scratch_or_feedback_dir(self):
        self.install_herdr(self.bindir)
        checkout = self.checkout()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'note').write_bytes(b'stay\n')
        (checkout / '.scratch').symlink_to(outside, target_is_directory=True)
        home = self.feedback_link()
        home.mkdir(parents=True)
        (home / 'gaps.jsonl').write_bytes(b'keep\n')
        linked = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(linked.returncode, 0, linked.stderr)
        self.assertEqual(json.loads(linked.stdout)['feedback'], 'failed')
        self.assertEqual((outside / 'note').read_bytes(), b'stay\n')
        self.assertFalse((outside / 'handoff-feedback').exists())
        self.assertTrue(home.is_dir())
        self.assertEqual((home / 'gaps.jsonl').read_bytes(), b'keep\n')
        (checkout / '.scratch').unlink()
        (checkout / '.scratch').mkdir()
        (checkout / '.scratch' / 'handoff-feedback').symlink_to(outside, target_is_directory=True)
        occupied = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(json.loads(occupied.stdout)['feedback'], 'failed')
        self.assertEqual(os.readlink(checkout / '.scratch' / 'handoff-feedback'), str(outside))
        self.assertEqual((outside / 'note').read_bytes(), b'stay\n')
        self.assertEqual(set(outside.iterdir()), {outside / 'note'})
        self.assertTrue(home.is_dir())
        self.assertEqual((home / 'gaps.jsonl').read_bytes(), b'keep\n')

    def test_remote_code_renames_a_nested_conflicting_file(self):
        self.install_herdr(self.bindir)
        checkout = self.checkout()
        target = self.feedback_target(checkout)
        (target / 'proposals').mkdir(parents=True)
        (target / 'proposals' / 'note.md').write_bytes(b'mac\n')
        os.utime(target / 'proposals' / 'note.md', (1_700_000_000, 1_700_000_000))
        home = self.feedback_link()
        (home / 'proposals').mkdir(parents=True)
        (home / 'proposals' / 'note.md').write_bytes(b'codespace\n')
        result = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['feedback'], 'moved')
        self.assertEqual((target / 'proposals' / 'note.md').read_bytes(), b'mac\n')
        self.assertEqual((target / 'proposals' / 'note.md').stat().st_mtime, 1_700_000_000)
        self.assertEqual((target / 'proposals' / 'note.md.codespace-1').read_bytes(), b'codespace\n')
        self.assertTrue(home.is_symlink())
        self.assertEqual(os.readlink(home), str(target))

    def test_remote_code_does_not_move_a_symlink_or_replace_an_occupied_target(self):
        self.install_herdr(self.bindir)
        checkout = self.checkout()
        (checkout / '.scratch').mkdir()
        outside = Path(self.temp.name) / 'secret'
        outside.write_bytes(b'secret\n')
        home = self.feedback_link()
        home.mkdir(parents=True)
        (home / 'gaps.jsonl').write_bytes(b'keep\n')
        (home / 'alias').symlink_to(outside)
        blocked = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(json.loads(blocked.stdout)['feedback'], 'failed')
        self.assertFalse(self.feedback_target(checkout).exists())
        self.assertEqual(list((checkout / '.scratch').iterdir()), [])
        self.assertTrue(home.is_dir())
        self.assertFalse(home.is_symlink())
        self.assertEqual((home / 'gaps.jsonl').read_bytes(), b'keep\n')
        self.assertTrue((home / 'alias').is_symlink())
        self.assertEqual(os.readlink(home / 'alias'), str(outside))
        self.assertEqual(outside.read_bytes(), b'secret\n')
        (home / 'alias').unlink()
        target = self.feedback_target(checkout)
        target.write_bytes(b'owner file\n')
        os.utime(target, (1_700_000_000, 1_700_000_000))
        occupied = self.invoke(self.feedback_payload(checkout))
        self.assertEqual(json.loads(occupied.stdout)['feedback'], 'failed')
        self.assertEqual(target.read_bytes(), b'owner file\n')
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.stat().st_mtime, 1_700_000_000)
        self.assertTrue(home.is_dir())
        self.assertEqual((home / 'gaps.jsonl').read_bytes(), b'keep\n')

    def test_remote_code_refuses_an_unsafe_feedback_root_without_writing(self):
        self.install_herdr(self.bindir)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        result = self.invoke(json.dumps({
            'install': [], 'skill': False, 'root': str(outside) + '/../../escape'}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['feedback'], 'failed')
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.feedback_link().exists())


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
            'skill': 'current',
            'feedback': 'current',
        }
        value.update(updates)
        return value

    def test_status_line_current(self):
        line = agent_hooks.status_line(self.receipt(), 'space')
        self.assertEqual(line, 'Herdr hooks: claude, codex, opencode current on space (checked '
                         + format_time(WHEN) + '); skill current; feedback current')

    def test_status_line_lists_only_agents_that_are_not_current(self):
        hooks = {'claude': 'outdated', 'codex': 'current', 'opencode': 'not installed'}
        line = agent_hooks.status_line(self.receipt(hooks=hooks), 'space')
        self.assertEqual(line, 'Herdr hooks: claude outdated, opencode not installed on space; '
                         'task work:cs installs them; skill current; feedback current')

    def test_status_line_check_failed(self):
        line = agent_hooks.status_line(self.receipt(error='invalid hooks response', skill='unknown'), 'space')
        self.assertEqual(line, 'Herdr hooks: check failed on space (invalid hooks response); '
                         'task work:cs checks again; skill unknown; feedback current')

    def test_status_line_not_checked_for_another_codespace(self):
        line = agent_hooks.status_line(self.receipt(codespace='other'), 'space')
        self.assertEqual(line, 'Herdr hooks: not checked on space; task work:cs checks them; '
                         'skill unknown; feedback unknown')

    def test_status_line_not_checked_without_a_receipt(self):
        self.assertEqual(agent_hooks.status_line(None, 'space'),
                         'Herdr hooks: not checked on space; task work:cs checks them; '
                         'skill unknown; feedback unknown')
        self.assertIsNone(agent_hooks.status_line(self.receipt(), None))

    def test_status_text_shows_the_hooks_line_and_tolerates_a_missing_key(self):
        shown = workspace.status_text(status_value(herdr_hooks=self.receipt()))
        self.assertIn('Herdr hooks: claude, codex, opencode current on space (checked '
                      + format_time(WHEN) + ')', shown)
        self.assertIn('Env files: not checked on space; task work:cs copies them', shown)
        self.assertLess(shown.index('Herdr hooks:'), shown.index('Env files:'))
        missing = workspace.status_text(status_value())
        self.assertIn('Herdr hooks: not checked on space; task work:cs checks them', missing)
        self.assertIn('Env files: not checked on space; task work:cs copies them', missing)
        bare = status_value()
        del bare['workspace']
        self.assertNotIn('Herdr hooks:', workspace.status_text(bare))
        self.assertNotIn('Env files:', workspace.status_text(bare))

    def test_status_line_appends_each_skill_state(self):
        checked = 'Herdr hooks: claude, codex, opencode current on space (checked ' + format_time(WHEN) + ')'
        self.assertEqual(agent_hooks.status_line(self.receipt(skill='current'), 'space'),
                         checked + '; skill current; feedback current')
        self.assertEqual(agent_hooks.status_line(self.receipt(skill='installed'), 'space'),
                         checked + '; skill installed; feedback current')
        self.assertEqual(agent_hooks.status_line(self.receipt(skill='managed elsewhere'), 'space'),
                         checked + '; skill managed elsewhere; feedback current')
        self.assertEqual(agent_hooks.status_line(self.receipt(skill='failed'), 'space'),
                         checked + '; skill failed (task work:cs installs it); feedback current')
        self.assertEqual(agent_hooks.status_line(self.receipt(skill='unknown'), 'space'),
                         checked + '; skill unknown (task work:cs installs it); feedback current')
        # A line that already names the task does not grow a second hint.
        pending = self.receipt(hooks={'claude': 'outdated', 'codex': 'current', 'opencode': 'current'},
                               skill='failed')
        self.assertEqual(agent_hooks.status_line(pending, 'space'),
                         'Herdr hooks: claude outdated on space; task work:cs installs them; '
                         'skill failed; feedback current')

    def test_status_line_old_receipt_reads_skill_as_unknown(self):
        old = self.receipt()
        del old['skill']
        line = agent_hooks.status_line(old, 'space')
        self.assertEqual(line, 'Herdr hooks: claude, codex, opencode current on space (checked '
                         + format_time(WHEN) + '); skill unknown (task work:cs installs it); feedback current')

    def test_status_line_appends_each_feedback_state(self):
        checked = ('Herdr hooks: claude, codex, opencode current on space (checked '
                   + format_time(WHEN) + '); skill current')
        for state in ('current', 'linked', 'moved', 'managed elsewhere', 'no scratch'):
            with self.subTest(state=state):
                line = agent_hooks.status_line(self.receipt(feedback=state), 'space')
                self.assertEqual(line, checked + '; feedback ' + state)
        for state in ('failed', 'unknown'):
            with self.subTest(state=state):
                line = agent_hooks.status_line(self.receipt(feedback=state), 'space')
                self.assertEqual(line, checked + '; feedback ' + state + ' (task work:cs links it)')
        pending = self.receipt(hooks={'claude': 'outdated', 'codex': 'current', 'opencode': 'current'},
                               skill='failed', feedback='failed')
        self.assertEqual(agent_hooks.status_line(pending, 'space'),
                         'Herdr hooks: claude outdated on space; task work:cs installs them; '
                         'skill failed; feedback failed')

    def test_status_line_old_receipt_reads_feedback_as_unknown(self):
        old = self.receipt()
        del old['feedback']
        line = agent_hooks.status_line(old, 'space')
        self.assertEqual(line, 'Herdr hooks: claude, codex, opencode current on space (checked '
                         + format_time(WHEN) + '); skill current; feedback unknown (task work:cs links it)')

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
