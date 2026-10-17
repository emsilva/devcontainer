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

    def fail_write(self, destination):
        original = ssh_hosts.atomic_write
        def interrupted(path, data, mode=0o600):
            if Path(path) == destination:
                raise OSError('injected interrupted owned write')
            return original(path, data, mode=mode)
        return mock.patch.object(ssh_hosts, 'atomic_write', side_effect=interrupted)

    def test_initial_global_write_interruption_replays_before_current_forward(self):
        with self.fail_write(self.config), self.assertRaisesRegex(OSError, 'injected'):
            ssh_hosts.configure_ssh(self.project, self.state)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertTrue((self.state / 'herdr-ssh.pending.json').exists())
        self.forward(33000)
        alias = ssh_hosts.configure_ssh(self.project, self.state)
        self.assertTrue(self.config.read_bytes().endswith(self.original))
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o644)
        self.assertFalse((self.state / 'herdr-ssh.pending.json').exists())
        resolved = subprocess.run(['ssh', '-G', '-F', str(self.config), alias],
                                  text=True, capture_output=True, check=True).stdout
        self.assertIn('port 33000\n', resolved)

    def test_refresh_receipt_interruption_replays_old_target_then_latest_forward(self):
        alias = ssh_hosts.configure_ssh(self.project, self.state)
        config_before = self.config.read_bytes()
        receipt_path = self.state / 'herdr-ssh.json'
        self.forward(32000)
        with self.fail_write(receipt_path), self.assertRaisesRegex(OSError, 'injected'):
            ssh_hosts.refresh_ssh(self.state)
        interrupted_digest = __import__('hashlib').sha256((self.state / 'herdr-ssh-config').read_bytes()).hexdigest()
        self.forward(34000)
        real_write = ssh_hosts.atomic_write
        written_receipts = []
        def observe(path, data, mode=0o600):
            if Path(path) == receipt_path:
                written_receipts.append(json.loads(data)['sha256'])
            return real_write(path, data, mode=mode)
        with mock.patch.object(ssh_hosts, 'atomic_write', side_effect=observe):
            ssh_hosts.refresh_ssh(self.state)
        self.assertEqual(written_receipts[0], interrupted_digest)
        self.assertEqual(len(written_receipts), 2)
        self.assertNotEqual(written_receipts[0], written_receipts[1])
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertFalse((self.state / 'herdr-ssh.pending.json').exists())
        resolved = subprocess.run(['ssh', '-G', '-F', str(self.config), alias],
                                  text=True, capture_output=True, check=True).stdout
        self.assertIn('port 34000\n', resolved)

    def test_unknown_owner_edits_to_pending_artifacts_are_preserved_and_refused(self):
        with self.fail_write(self.config), self.assertRaises(OSError):
            ssh_hosts.configure_ssh(self.project, self.state)
        paths = [self.config, self.state / 'herdr-ssh-config', self.state / 'herdr-ssh.json']
        baseline = {path: path.read_bytes() if path.exists() else None for path in paths}
        for target in paths:
            with self.subTest(target=target.name):
                for path, data in baseline.items():
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(data)
                target.write_bytes(b'{"owner": "edited"}\n' if target.name.endswith('.json') else b'# owner edit\n')
                before = {path: path.read_bytes() if path.exists() else None for path in paths}
                with self.assertRaisesRegex(ValueError, 'pending|transaction|modified|ownership'):
                    ssh_hosts.configure_ssh(self.project, self.state)
                self.assertEqual({path: path.read_bytes() if path.exists() else None for path in paths}, before)
                self.assertTrue((self.state / 'herdr-ssh.pending.json').exists())


if __name__ == '__main__':
    unittest.main()
