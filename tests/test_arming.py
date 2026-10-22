"""Arming state: ports, account, consent record and hold times."""
import datetime
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from devcontainer_setup.project_sync import arming

FAKE_GH = """#!/usr/bin/env bash
if [ "$1 $2" = "auth token" ]; then echo tok-secret-value; exit 0; fi
if [ "$1 $2" = "api -i" ]; then
  printf 'HTTP/2.0 200 OK\\r\\nX-OAuth-Scopes: %s\\r\\n\\r\\n{"login": "octo"}' "$FAKE_SCOPES"
  exit 0
fi
exit 1
"""


class ArmingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name).resolve()

    def fake_env(self, scopes, **extra):
        bin_dir = self.dir / 'bin'
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / 'gh'
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        return dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', FAKE_SCOPES=scopes, **extra)

    def test_read_defaults_when_missing(self):
        self.assertEqual(arming.read(self.dir), {'schema': 1, 'armed': False})

    def test_default_ports_drop_2222(self):
        (self.dir / '.devcontainer').mkdir()
        (self.dir / '.devcontainer/devcontainer.json').write_text(
            '{\n  // forwarded\n  "forwardPorts": [2222, 8787, 3570]\n}\n')
        self.assertEqual(arming.default_ports(self.dir), [3570, 8787])
        self.assertEqual(arming.default_ports(self.dir / 'missing'), [])

    def test_resolve_ports_adds_and_removes(self):
        self.assertEqual(arming.resolve_ports([3570, 8787], [3571], [8787]), [3570, 3571])

    def test_account_refuses_environment_tokens(self):
        for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                arming.account(self.fake_env('codespace', **{name: 'ghp_sekret'}))
            self.assertIn(name, str(caught.exception))
            self.assertNotIn('ghp_sekret', str(caught.exception))

    def test_account_requires_codespace_scope(self):
        with self.assertRaisesRegex(ValueError, 'codespace'):
            arming.account(self.fake_env('repo'))

    def test_account_returns_active_login_or_selected_user(self):
        self.assertEqual(arming.account(self.fake_env('repo, codespace')), 'octo')
        self.assertEqual(arming.account(self.fake_env('codespace', DEVCONTAINER_GH_USER='other')), 'other')

    def test_consent_is_stored_as_hash(self):
        text = arming.consent_text('proj', 'cs-1', 'octo')
        self.assertIn('Type arm to confirm', text)
        arming.arm(self.dir, gh_user='octo', consent=text, billable_owner='octo', ports=[3570], now=1000.0)
        raw = (self.dir / arming.ARMING).read_text()
        value = json.loads(raw)
        self.assertEqual(len(value['consent_sha256']), 64)
        self.assertNotIn('Type arm', raw)
        self.assertTrue(value['armed'])
        self.assertEqual(value['ports'], [3570])
        self.assertEqual(stat.S_IMODE((self.dir / arming.ARMING).stat().st_mode), 0o600)

    def test_hold_until_next_local_occurrence(self):
        now = datetime.datetime(2026, 10, 21, 23, 0).timestamp()
        want = datetime.datetime(2026, 10, 22, 9, 30).timestamp()
        self.assertEqual(arming.hold_until('09:30', now), want)
        self.assertIsNone(arming.hold_until('off', now))
        with self.assertRaises(ValueError):
            arming.hold_until('25:99', now)

    def test_record_owner_stop(self):
        arming.record_owner_stop(self.dir, 55.0)
        self.assertEqual(arming.read(self.dir)['owner_stop_at'], 55.0)


if __name__ == '__main__':
    unittest.main()
