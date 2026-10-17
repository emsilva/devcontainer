import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from devcontainer_setup.project_sync.state import Project, atomic_write
from devcontainer_setup.project_sync import ssh_hosts


class SSHHostsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'home with spaces'
        self.home.mkdir()
        self.state = self.home / 'state/devcontainer-setup/projects' / ('a' * 24)
        self.project = Project(self.home / 'repo', 'git@github.com:a/b', 'github.com/a/b', 'a' * 24)
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = self.home / '.ssh/config'
        self.config.parent.mkdir(mode=0o700)
        self.original = b'# user configuration, keep bytes\nHost other\n  User me\n'
        self.config.write_bytes(self.original)
        self.config.chmod(0o644)
        self.forward(31000)

    def forward(self, port):
        text = f'''Host project-sync
  HostName 127.0.0.1
  Port {port}
  User vscode
  IdentityFile "{self.home}/.ssh/codespaces.auto"
  IdentitiesOnly yes
  BatchMode yes
  StrictHostKeyChecking yes
  HostKeyAlias "test-codespace"
  UserKnownHostsFile "{self.state}/known_hosts"
  GlobalKnownHostsFile /dev/null
  ConnectTimeout 15
  ConnectionAttempts 1
  ServerAliveInterval 0
  ControlMaster no
  ControlPath none
  ClearAllForwardings yes
  LogLevel ERROR
'''
        atomic_write(self.state / 'ssh-config', text.encode())

    def test_owned_alias_is_idempotent_preserves_user_bytes_and_real_ssh_resolves(self):
        alias = ssh_hosts.configure_ssh(self.project, self.state)
        first = self.config.read_bytes()
        self.assertTrue(first.endswith(self.original))
        self.assertEqual(ssh_hosts.configure_ssh(self.project, self.state), alias)
        self.assertEqual(first, self.config.read_bytes())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o644)
        resolved = subprocess.run(['ssh', '-G', '-F', str(self.config), alias], text=True,
                                  capture_output=True, check=True).stdout
        self.assertIn('hostname 127.0.0.1\n', resolved)
        self.assertIn('port 31000\n', resolved)
        self.assertIn('stricthostkeychecking true\n', resolved)
        self.assertIn('hostkeyalias test-codespace\n', resolved)

    def test_refresh_only_updates_previously_owned_alias(self):
        ssh_hosts.refresh_ssh(self.state)
        self.assertEqual(self.config.read_bytes(), self.original)
        alias = ssh_hosts.configure_ssh(self.project, self.state)
        original = self.config.read_bytes()
        self.forward(32000)
        ssh_hosts.refresh_ssh(self.state)
        self.assertEqual(self.config.read_bytes(), original)
        result = subprocess.run(['ssh', '-G', '-F', str(self.config), alias],
                                capture_output=True, text=True, check=True)
        self.assertIn('port 32000\n', result.stdout)

    def test_modified_owned_include_or_root_block_is_refused(self):
        ssh_hosts.configure_ssh(self.project, self.state)
        receipt = json.loads((self.state / 'herdr-ssh.json').read_text())
        include = Path(receipt['include'])
        saved = include.read_bytes()
        include.write_bytes(saved + b'# edit\n')
        with self.assertRaisesRegex(ValueError, 'modified|Edited|ownership'):
            ssh_hosts.refresh_ssh(self.state)
        include.write_bytes(saved)
        self.config.write_bytes(self.original)
        with self.assertRaisesRegex(ValueError, 'modified|missing|ownership'):
            ssh_hosts.configure_ssh(self.project, self.state)

    def test_symlink_and_unsafe_forward_cannot_touch_user_config(self):
        self.config.unlink()
        outside = self.home / 'outside'
        outside.write_bytes(self.original)
        self.config.symlink_to(outside)
        with self.assertRaises((ValueError, OSError)):
            ssh_hosts.configure_ssh(self.project, self.state)
        self.assertEqual(outside.read_bytes(), self.original)
        self.config.unlink()
        self.config.write_bytes(self.original)
        atomic_write(self.state / 'ssh-config', (self.state / 'ssh-config').read_bytes() + b'  ProxyCommand evil\n')
        with self.assertRaisesRegex(ValueError, 'forward|SSH'):
            ssh_hosts.configure_ssh(self.project, self.state)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_global_wildcard_does_not_redirect_owned_local_forward(self):
        self.config.write_bytes(b'Host *\n  ProxyCommand surprise-command\n  PermitLocalCommand yes\n')
        alias = ssh_hosts.configure_ssh(self.project, self.state)
        result = subprocess.run(['ssh', '-G', '-F', str(self.config), alias],
                                capture_output=True, text=True, check=True)
        self.assertIn('permitlocalcommand no\n', result.stdout)
        self.assertNotIn('surprise-command', result.stdout)


if __name__ == '__main__':
    unittest.main()
