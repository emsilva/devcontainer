"""Arming state: ports, account, consent record and hold times."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

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
        arming.arm(self.dir, gh_user='octo', codespace='cs-1', consent=text, billable_owner='octo', ports=[3570], now=1000.0)
        raw = (self.dir / arming.ARMING).read_text()
        value = json.loads(raw)
        self.assertEqual(len(value['consent_sha256']), 64)
        self.assertNotIn('Type arm', raw)
        self.assertTrue(value['armed'])
        self.assertEqual(value['ports'], [3570])
        self.assertEqual(stat.S_IMODE((self.dir / arming.ARMING).stat().st_mode), 0o600)

    def test_rearm_clears_stale_hold_and_owner_stop(self):
        arming.write(self.dir, {'schema': 1, 'armed': False, 'hold_until': 99.0, 'owner_stop_at': 50.0})
        value = arming.arm(self.dir, gh_user='o', codespace='c', consent='t', billable_owner='o', ports=[], now=1.0)
        self.assertIsNone(value['hold_until'])
        self.assertNotIn('owner_stop_at', arming.read(self.dir))

    def test_default_ports_reads_jsonc(self):
        (self.dir / '.devcontainer').mkdir()
        path = self.dir / '.devcontainer/devcontainer.json'
        path.write_text('{ "name": "a // not a comment", /* block */\n "forwardPorts": [3570, 2222, 8787,], // tail\n}\n')
        self.assertEqual(arming.default_ports(self.dir), [3570, 8787])
        for bad in ('[1, 2]', '{"forwardPorts": [1,', '{"forwardPorts": 5}'):
            path.write_text(bad)
            with self.assertRaisesRegex(ValueError, 'devcontainer.json'):
                arming.default_ports(self.dir)

    def test_consent_text_uses_policy_constants(self):
        self.assertIn('at most 2 times in 24 hours, 30 minutes apart', arming.consent_text('p', 'c', 'o'))

    def test_account_distinguishes_failure_and_missing_gh(self):
        env = self.fake_env('codespace')
        (self.dir / 'bin/gh').write_text('#!/usr/bin/env bash\n[ "$1" = auth ] && { echo t; exit 0; }\nexit 1\n')
        with self.assertRaisesRegex(ValueError, 'rejected'):
            arming.account(env)
        with self.assertRaisesRegex(ValueError, 'not installed'):
            arming.account(dict(env, PATH='/nonexistent'))

    def test_format_time_shows_date_local_and_utc(self):
        text = arming.format_time(0.0)
        self.assertRegex(text, r'^\d{4}-\d\d-\d\d \d\d:\d\d \(UTC 1970-01-01 00:00\)$')

    def test_hold_until_next_local_occurrence(self):
        now = datetime.datetime(2026, 10, 21, 23, 0).timestamp()
        want = datetime.datetime(2026, 10, 22, 9, 30).timestamp()
        self.assertEqual(arming.hold_until('09:30', now), want)
        self.assertIsNone(arming.hold_until('off', now))
        with self.assertRaises(ValueError):
            arming.hold_until('25:99', now)

    def test_record_owner_stop(self):
        arming.record_owner_stop(self.dir, 55.0, 'cs-1')
        self.assertEqual(arming.read(self.dir)['owner_stop_at'], 55.0)
        self.assertEqual(arming.read(self.dir)['owner_stop_codespace'], 'cs-1')


class ConsentMoveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve()
        self.armed = {'schema': 1, 'armed': True, 'armed_at': 1000.0, 'consented_at': 1000.0,
                      'gh_user': 'me', 'codespace': 'old-cs', 'billable_owner': 'me',
                      'ports': [3000], 'hold_until': 5000.0, 'consent_sha256': hashlib.sha256(
                          arming.consent_text('proj', 'old-cs', 'me').encode()).hexdigest()}
        arming.write(self.state, self.armed)
        self.ok = dict(project_name='proj', gh_user='me', old='old-cs', new_owner='me')

    def test_same_account_and_owner_carries_over(self):
        self.assertTrue(arming.carries_over(self.armed, **self.ok))

    def test_changed_owner_account_policy_or_missing_owner_does_not_carry(self):
        for change in ({'new_owner': 'org'}, {'gh_user': 'other'}, {'new_owner': ''},
                       {'old': 'another-cs'}, {'project_name': 'another-project'}):
            with self.subTest(change=change):
                self.assertFalse(arming.carries_over(self.armed, **dict(self.ok, **change)))
        for change in ({'armed': False}, {'consent_sha256': '0' * 64}):
            with self.subTest(change=change):
                self.assertFalse(arming.carries_over(dict(self.armed, **change), **self.ok))
        self.assertFalse(arming.carries_over({}, **self.ok))

    def test_move_keeps_timestamps_hold_and_ports(self):
        ledger = self.state / 'keeper-ledger.jsonl'
        ledger.write_bytes(b'{"event": "started", "at": 900.0}\n')
        before_ledger = ledger.read_bytes()
        line = arming.move_consent(self.state, self.armed, project_name='proj', new='new-cs', now=2000.0)
        moved = arming.read(self.state)
        for key in ('armed_at', 'consented_at', 'ports', 'hold_until', 'gh_user', 'billable_owner'):
            self.assertEqual(moved[key], self.armed[key])
        self.assertTrue(moved['armed'])
        self.assertEqual((moved['codespace'], moved['moved_from'], moved['moved_at']),
                         ('new-cs', 'old-cs', 2000.0))
        self.assertEqual(moved['consent_sha256'], hashlib.sha256(
            arming.consent_text('proj', 'new-cs', 'me').encode()).hexdigest())
        date = datetime.datetime.fromtimestamp(self.armed['consented_at']).strftime('%Y-%m-%d')
        self.assertEqual(line, f'Recovery moved old-cs → new-cs; consent from {date} kept (me, billing me).')
        self.assertEqual(ledger.read_bytes(), before_ledger)
        self.assertEqual(arming.read(self.state)['schema'], 1)
        self.assertEqual(self.armed['codespace'], 'old-cs')
        archives = list((self.state / 'arming-history').glob('*.json'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(json.loads(archives[0].read_text()), self.armed)
        for path in (archives[0], self.state / arming.ARMING):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_move_with_ports_replaces_only_ports(self):
        arming.move_consent(self.state, self.armed, project_name='proj', new='new-cs',
                            now=2000.0, ports=[8080, 3000])
        first = arming.read(self.state)
        self.assertEqual(first['ports'], [3000, 8080])
        self.assertEqual(first, dict(self.armed, codespace='new-cs', moved_from='old-cs',
                                    moved_at=2000.0, ports=[3000, 8080], consent_sha256=hashlib.sha256(
                                        arming.consent_text('proj', 'new-cs', 'me').encode()).hexdigest()))
        archived = [json.loads(p.read_text()) for p in (self.state / 'arming-history').glob('*.json')]
        self.assertEqual(archived, [self.armed])
        arming.move_consent(self.state, first, project_name='proj', new='next-cs', now=3000.0, ports=[])
        self.assertEqual(arming.read(self.state)['ports'], [])
        archived = [json.loads(p.read_text()) for p in (self.state / 'arming-history').glob('*.json')]
        self.assertCountEqual(archived, [self.armed, first])

    def test_move_retires_owner_stop_and_pending_restore(self):
        value = dict(self.armed, owner_stop_at=1500.0, owner_stop_codespace='old-cs', pending_restore={'rid': 'r1'}, extra='kept')
        arming.write(self.state, value)
        arming.move_consent(self.state, value, project_name='proj', new='new-cs', now=2000.0)
        moved = arming.read(self.state)
        self.assertNotIn('owner_stop_at', moved)
        self.assertNotIn('owner_stop_codespace', moved)
        self.assertNotIn('pending_restore', moved)
        self.assertEqual(moved['extra'], 'kept')
        archived = [json.loads(p.read_text()) for p in (self.state / 'arming-history').glob('*.json')]
        self.assertEqual(archived, [value])

    def test_move_refuses_what_does_not_carry(self):
        before = (self.state / arming.ARMING).read_bytes()
        for change in ({'billable_owner': 'org'}, {'armed': False}, {'consent_sha256': '0' * 64},
                       {'billable_owner': ''}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    arming.move_consent(self.state, dict(self.armed, **change),
                                        project_name='proj', new='new-cs', now=2000.0)
                self.assertEqual((self.state / arming.ARMING).read_bytes(), before)
                self.assertFalse((self.state / 'arming-history').exists())

    def test_ordinary_connect_still_refuses_a_mismatched_target(self):
        from devcontainer_setup.project_sync import __main__ as cli
        from types import SimpleNamespace
        project = SimpleNamespace(root=self.state / 'proj')
        with patch.object(arming, 'account', return_value='me'), \
                patch('devcontainer_setup.project_sync.codespaces.Codespaces.space',
                      return_value={'name': 'new-cs', 'billable_owner': {'login': 'me'}}):
            self.assertEqual(cli._consent_on_file(project, self.state), 'the Codespace changed')


if __name__ == '__main__':
    unittest.main()
