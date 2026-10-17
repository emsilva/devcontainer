"""Exercise the distributed command entrypoint, including isolated execution."""
import json
import os
from pathlib import Path
import subprocess
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

    def test_work_status_is_offline_and_does_not_create_state(self):
        result = self.run_cli('work', '--status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout)['last_mac_checkpoint'])
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
