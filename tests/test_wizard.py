import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class WizardCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(Path(self.tmp.name) / 'config'),
                        XDG_CACHE_HOME=str(Path(self.tmp.name) / 'cache'))
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test User')
        self.git('config', 'user.email', 'test@example.com')
        (self.root / 'package.json').write_text('{"engines":{"node":"24"}}\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Initial')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'devcontainer_setup', '--project', str(self.root), *args],
                              cwd=SOURCE, env=self.env, capture_output=True, text=True)

    def test_scan_reports_node_and_python_without_writing(self):
        result = self.cli('--scan')
        self.assertEqual(result.returncode, 0, result.stderr)
        scan = json.loads(result.stdout)
        self.assertEqual(scan['runtimes']['node']['version'], '24')
        self.assertTrue(scan['runtimes']['python']['selected'])
        self.assertFalse((self.root / '.devcontainer').exists())
        self.assertFalse(Path(self.env['XDG_CONFIG_HOME']).exists())

    def test_dry_run_shows_generated_diff_without_mutating_checkout(self):
        before = self.git('rev-parse', 'HEAD')
        result = self.cli('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('devcontainer.json', result.stdout)
        self.assertIn('python:1', result.stdout)
        self.assertNotIn('ruby:1', result.stdout)
        self.assertFalse((self.root / '.devcontainer').exists())
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)

    def test_noninteractive_mutation_is_refused(self):
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('terminal', result.stderr.lower())
        self.assertFalse((self.root / '.devcontainer').exists())

    def test_invalid_configuration_check_fails(self):
        result = self.cli('--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('devcontainer', result.stderr.lower())


if __name__ == '__main__':
    unittest.main()
