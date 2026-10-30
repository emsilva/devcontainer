"""System PATH writes and shell lookups use isolated files and fake installers."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = ROOT / 'templates/scripts/system-path.sh'
LOCAL_BIN = '/home/vscode/.local/bin'


class SystemPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.system = self.root / 'system'
        self.etc = self.system / 'etc'
        self.etc.mkdir(parents=True)
        self.environment = self.etc / 'environment'
        self.zshenv = self.etc / 'zsh/zshenv'
        self.home = self.root / 'home'
        (self.home / '.local/bin').mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), USER='vscode',
                        DEVCONTAINER_SYSTEM_ROOT=str(self.system),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'])

    def executable(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)

    def writer(self, *args, **env):
        self.assertTrue(SYSTEM.is_file(), 'System PATH writer is missing')
        return subprocess.run(['bash', str(SYSTEM), *args], env=dict(self.env, **env),
                              capture_output=True, text=True)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def project_fixture(self):
        from devcontainer_setup.render import render
        project = self.root / 'project'
        project.mkdir()
        files = render(project, ROOT, {}, 'a' * 40)
        for name, data in files.items():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # PATH tests must not install or inspect actual coding tools.
        for name in ('install-agents.sh', 'install-tools.sh'):
            (project / '.devcontainer/scripts' / name).write_text('exit 0\n')
        check = files['.devcontainer/scripts/validate-selected.sh'].decode()
        commands = check.split('for tool in ', 1)[1].split(';', 1)[0].split()
        for name in commands:
            if name == 'python3':
                continue
            destination = self.home / '.local/bin/codex' if name == 'codex' else self.bin / name
            self.executable(destination, '#!/bin/sh\nexit 0\n')
        env = dict(self.env, PATH=str(self.home / '.local/bin') + os.pathsep + self.env['PATH'])
        return project, env

    def test_prepends_local_bin_once(self):
        self.environment.write_text('LANG=C.UTF-8\nPATH="/usr/local/bin:/usr/bin"\n')
        self.assert_ok(self.writer())
        self.assertEqual(self.environment.read_text(),
                         'LANG=C.UTF-8\nPATH="/home/vscode/.local/bin:/usr/local/bin:/usr/bin"\n')
        before = (self.environment.read_bytes(), self.zshenv.read_bytes())
        self.assert_ok(self.writer())
        self.assertEqual(before, (self.environment.read_bytes(), self.zshenv.read_bytes()))

    def test_creates_path_line_when_absent(self):
        self.environment.write_text('LANG=C.UTF-8\n')
        running_path = str(self.bin) + ':/usr/bin:/bin'
        self.assert_ok(self.writer(PATH=running_path))
        self.assertEqual(self.environment.read_text(),
                         'LANG=C.UTF-8\nPATH="' + LOCAL_BIN + ':' + running_path + '"\n')

    def test_zshenv_block_is_replaced_not_appended(self):
        self.zshenv.parent.mkdir(parents=True)
        self.zshenv.write_text('# before\n# devcontainer-setup PATH\nexport PATH="/old:$PATH"\n'
                               '# end devcontainer-setup PATH\n# after\n')
        self.assert_ok(self.writer())
        content = self.zshenv.read_text()
        self.assertEqual(content.count('# devcontainer-setup PATH'), 1)
        self.assertIn(LOCAL_BIN, content)
        self.assertNotIn('/old', content)
        self.assertTrue(content.startswith('# before\n'))
        self.assertTrue(content.endswith('# after\n'))
        self.assert_ok(self.writer())
        self.assertEqual(self.zshenv.read_text(), content)

    def test_retains_caller_path_when_sudo_resets_environment(self):
        caller_path = '/feature/node/bin:/usr/bin:/bin'
        self.assert_ok(self.writer(caller_path, PATH='/usr/bin:/bin'))
        self.assertEqual(self.environment.read_text(), 'PATH="' + LOCAL_BIN + ':' + caller_path + '"\n')

    def test_rejects_multiline_caller_path_without_writes(self):
        result = self.writer('/usr/bin\nUNEXPECTED=1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('single line', result.stderr)
        self.assertFalse(self.environment.exists())
        self.assertFalse(self.zshenv.exists())

    def test_refuses_non_root_without_test_prefix(self):
        self.executable(self.bin / 'id', '#!/bin/sh\necho 1000\n')
        result = self.writer(DEVCONTAINER_SYSTEM_ROOT='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('root', result.stderr)
        self.assertFalse(self.environment.exists())

    def test_refused_sudo_names_the_command(self):
        project, env = self.project_fixture()
        self.executable(self.bin / 'sudo', '#!/bin/sh\necho "sudo: a password is required" >&2\nexit 1\n')
        result = subprocess.run(['bash', '.devcontainer/scripts/post-create-selected.sh'],
                                cwd=project, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sudo bash .devcontainer/scripts/system-path.sh "$PATH"', result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertFalse(self.environment.exists())

    def test_fresh_shell_lookup_fails_without_system_path(self):
        project, env = self.project_fixture()
        self.environment.write_text('PATH="' + str(self.bin) + ':/usr/bin:/bin"\n')
        result = subprocess.run(['bash', '.devcontainer/scripts/validate-selected.sh'],
                                cwd=project, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, 'Inherited HOME PATH must not satisfy the fresh-shell check')
        self.assertIn('Not on the PATH of a fresh shell: codex', result.stderr)
        self.assert_ok(self.writer())
        self.assertIn(LOCAL_BIN, self.environment.read_text())
        # Map the container home to this isolated fixture after checking the writer's real prefix.
        self.environment.write_text(self.environment.read_text().replace(LOCAL_BIN, str(self.home / '.local/bin')))
        result = subprocess.run(['bash', '.devcontainer/scripts/validate-selected.sh'],
                                cwd=project, env=env, capture_output=True, text=True)
        self.assert_ok(result)
        self.assertNotIn('Not on the PATH of a fresh shell: codex', result.stderr)
