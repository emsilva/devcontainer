"""Exercise the distributed command entrypoint, including isolated execution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class ProjectSyncCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'remote', 'add', 'origin',
                        'git@github.com:example/project.git'], check=True)
        self.state = Path(self.temp.name).resolve() / 'state'
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.state))

    def run_cli(self, *args):
        return subprocess.run(['bash', str(SOURCE / 'scripts/project-sync.sh'),
                               '--project', str(self.root), *args], env=self.env,
                              capture_output=True, text=True)

    def test_readonly_status_does_not_create_state_or_load_checkout_python(self):
        (self.root / 'sitecustomize.py').write_text('raise RuntimeError("checkout injection")\n')
        result = self.run_cli('scratch', 'status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['phase'], 'stopped')
        self.assertFalse(self.state.exists())

    def test_missing_pairing_has_actionable_error_without_traceback(self):
        result = self.run_cli('scratch', 'start')
        self.assertEqual(result.returncode, 1)
        self.assertIn('scratch:configure', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_old_handoff_surface_removed(self):
        result = self.run_cli('agent', 'recover')
        self.assertEqual(result.returncode, 2)
        self.assertIn('invalid choice', result.stderr)

    def test_agent_launchers_and_implicit_local_recovery_are_removed(self):
        for args in (('work',), ('work', '--agent', 'codex'),
                     ('work', '--agent', 'claude', '--resume')):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.state.exists())

    def test_work_status_is_offline_and_does_not_create_state(self):
        result = self.run_cli('work', '--status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('History:', result.stdout)
        self.assertLess(len(result.stdout.splitlines()), 9)
        detail = self.run_cli('work', '--status', '--json')
        self.assertEqual(detail.returncode, 0, detail.stderr)
        self.assertIsNone(json.loads(detail.stdout)['last_mac_checkpoint'])
        self.assertFalse(self.state.exists())

    def test_disconnect_is_project_scoped_idempotent_and_needs_no_terminal_or_pairing(self):
        from devcontainer_setup.project_sync.state import discover_project
        project = discover_project(self.root)
        alias, session = 'devcontainer-' + project.project_id, 'project-' + project.project_id
        catalog = self.root.parent / 'machines.json'
        calls = self.root.parent / 'calls.jsonl'
        rows = [
            dict(id='other-project', target='other-alias', session=session, enabled=True),
            dict(id='other-session', target=alias, session='default', enabled=True),
            dict(id='project-one', target=alias, session=session, enabled=True),
            dict(id='project-two', target=alias, session=session, enabled=True),
        ]
        catalog.write_text(json.dumps(rows))
        binary = self.root.parent / 'bin'
        binary.mkdir()
        (binary / 'herdr').write_text('#!' + sys.executable + '\n' + '''import json,os,pathlib,sys
catalog=pathlib.Path(os.environ['TEST_CATALOG'])
rows=json.loads(catalog.read_text())
args=sys.argv[1:]
with open(os.environ['TEST_CALLS'],'a') as stream:stream.write(json.dumps(args)+'\\n')
if args==['machine','list','--json']:print(json.dumps(rows))
elif len(args)==3 and args[:2]==['machine','disable']:
 for row in rows:
  if row['id']==args[2]:row['enabled']=False
 catalog.write_text(json.dumps(rows))
 print('Disabled machine')
else:sys.exit(12)
''')
        (binary / 'herdr').chmod(0o700)
        self.env.update(PATH=str(binary) + os.pathsep + os.environ['PATH'],
                        TEST_CATALOG=str(catalog), TEST_CALLS=str(calls))
        for _ in range(2):
            result = self.run_cli('work', '--disconnect')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('task work:cs', result.stdout)
        actual = json.loads(catalog.read_text())
        self.assertEqual([row['id'] for row in actual if row['enabled']], ['other-project', 'other-session'])
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(commands.count(['machine', 'disable', 'project-one']), 1)
        self.assertEqual(commands.count(['machine', 'disable', 'project-two']), 1)
        self.assertTrue(all(command[:2] in (['machine', 'list'], ['machine', 'disable']) for command in commands))
        self.assertFalse(self.state.exists())

    def test_disconnect_refuses_to_run_in_the_codespace(self):
        self.env['CODESPACES'] = 'true'
        result = self.run_cli('work', '--disconnect')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Mac', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(self.state.exists())

    def test_help_and_invalid_project_never_touch_normal_profiles(self):
        result = self.run_cli('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('scratch', result.stdout)
        (self.root / '.git/config').write_text('[core]\n repositoryformatversion=0\n')
        result = self.run_cli('work', '--status')
        self.assertEqual(result.returncode, 1)
        self.assertIn('origin', result.stderr)
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
