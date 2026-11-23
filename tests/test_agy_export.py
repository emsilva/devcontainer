"""Tests for automatic read-only Antigravity CLI (agy) history export into .scratch/history/agy/."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from devcontainer_setup.project_sync import agy_export, replication, rpc
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.state import discover_project, state_dir


def _init_agy_db(app_dir: Path) -> Path:
    app_dir.mkdir(parents=True, exist_ok=True)
    db_path = app_dir / 'conversation_summaries.db'
    con = sqlite3.connect(db_path)
    con.execute('''
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            preview TEXT,
            step_count INTEGER,
            last_modified_time TEXT,
            workspace_uris TEXT,
            status TEXT,
            parent_conversation_id TEXT,
            nesting_depth INTEGER,
            last_user_input_time TEXT
        )
    ''')
    con.commit()
    con.close()
    return db_path


def _insert_conversation(
    app_dir: Path,
    cid: str,
    *,
    title: str,
    preview: str,
    workspace_uris: list[str],
    step_count: int = 4,
    last_modified_time: str = '2026-11-23T12:00:00Z',
    status: str = 'IDLE',
    parent_conversation_id: str = '',
    nesting_depth: int = 0,
    rows: list[dict] | None = None,
    transcript_name: str = 'transcript_full.jsonl',
) -> Path:
    db_path = _init_agy_db(app_dir)
    con = sqlite3.connect(db_path)
    con.execute(
        '''
        INSERT OR REPLACE INTO conversation_summaries (
            conversation_id, title, preview, step_count, last_modified_time,
            workspace_uris, status, parent_conversation_id, nesting_depth, last_user_input_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            cid, title, preview, step_count, last_modified_time,
            json.dumps(workspace_uris), status, parent_conversation_id,
            nesting_depth, last_modified_time,
        ),
    )
    con.commit()
    con.close()

    transcript = app_dir / 'brain' / cid / '.system_generated' / 'logs' / transcript_name
    if rows is not None:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        payload = ''.join(json.dumps(row) + '\n' for row in rows)
        transcript.write_text(payload, encoding='utf-8')
    return transcript


class AgyExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'repo'
        self.root.mkdir()
        for args in (
            ('init', '-q'),
            ('remote', 'add', 'origin', 'https://github.com/example/agy-history.git'),
            ('config', 'user.email', 'fixture@example.com'),
            ('config', 'user.name', 'Fixture'),
        ):
            subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        (self.root / 'README.md').write_text('# fixture\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'init'], check=True, capture_output=True)
        self.project = discover_project(self.root)
        self.scratch = self.root / '.scratch'
        self.scratch.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.app_dir = self.home / '.gemini/antigravity-cli'
        self.env = {'HOME': str(self.home), 'XDG_STATE_HOME': str(self.base / 'state')}

    def test_missing_agy_store_is_noop_and_creates_nothing(self):
        result = agy_export.export_project_history(self.project, host='mac', env=self.env)
        self.assertTrue(result['skipped'])
        self.assertEqual(result['exported'], 0)
        self.assertFalse((self.scratch / 'history').exists())

    def test_missing_or_symlinked_scratch_is_refused_silently(self):
        cid = str(uuid.uuid4())
        _insert_conversation(
            self.app_dir, cid, title='Test', preview='Hello',
            workspace_uris=[self.root.as_uri()],
        )
        self.scratch.rmdir()
        result = agy_export.export_project_history(self.project, host='mac', env=self.env)
        self.assertTrue(result['skipped'])
        outside = self.base / 'outside'
        outside.mkdir()
        self.scratch.symlink_to(outside, target_is_directory=True)
        result = agy_export.export_project_history(self.project, host='mac', env=self.env)
        self.assertTrue(result['skipped'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_exports_only_project_top_level_sessions_and_cleans_turns(self):
        other_repo = self.base / 'other-repo'
        other_repo.mkdir()
        proj_cid = str(uuid.uuid4())
        subdir_cid = str(uuid.uuid4())
        subagent_cid = str(uuid.uuid4())
        foreign_cid = str(uuid.uuid4())

        _insert_conversation(
            self.app_dir,
            proj_cid,
            title='Fix sync bug',
            preview='Fixed the sync bug',
            workspace_uris=[self.root.as_uri()],
            last_modified_time='2026-11-23T12:10:00Z',
            step_count=6,
            rows=[
                {
                    'source': 'USER_EXPLICIT',
                    'type': 'USER_INPUT',
                    'status': 'DONE',
                    'created_at': '2026-11-23T12:00:00Z',
                    'content': (
                        '<USER_REQUEST>\nPlease fix the retry loop.\n</USER_REQUEST>\n'
                        '<ADDITIONAL_METADATA>\nSecret clock metadata\n</ADDITIONAL_METADATA>\n'
                        '<USER_SETTINGS_CHANGE>\nModel changed\n</USER_SETTINGS_CHANGE>'
                    ),
                },
                {
                    'source': 'MODEL',
                    'type': 'PLANNER_RESPONSE',
                    'status': 'DONE',
                    'created_at': '2026-11-23T12:01:00Z',
                    'thinking': 'Internal private chain of thought should never leak',
                    'tool_calls': [
                        {'name': 'edit_file', 'args': {'Path': json.dumps(str(self.root / 'src/retry.py'))}},
                        {'name': 'run_command', 'args': {'CommandLine': '"pytest"'}},
                    ],
                },
                {
                    'source': 'MODEL',
                    'type': 'GENERIC',
                    'status': 'DONE',
                    'created_at': '2026-11-23T12:02:00Z',
                    'content': 'HUGE_RAW_TOOL_OUTPUT_SHOULD_NOT_APPEAR',
                },
                {
                    'source': 'MODEL',
                    'type': 'PLANNER_RESPONSE',
                    'status': 'DONE',
                    'created_at': '2026-11-23T12:05:00Z',
                    'content': 'Updated `src/retry.py` and verified all tests pass.',
                },
            ],
        )
        _insert_conversation(
            self.app_dir,
            subdir_cid,
            title='Subdir work',
            preview='Worked inside docs',
            workspace_uris=[(self.root / 'docs').as_uri()],
            last_modified_time='2026-11-23T11:00:00Z',
            transcript_name='transcript.jsonl',
            rows=[
                {'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE', 'content': 'Update docs'},
                {'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': 'Docs updated.'},
            ],
        )
        _insert_conversation(
            self.app_dir,
            subagent_cid,
            title='Internal worker',
            preview='Subagent output',
            workspace_uris=[self.root.as_uri()],
            parent_conversation_id=proj_cid,
            nesting_depth=1,
        )
        _insert_conversation(
            self.app_dir,
            foreign_cid,
            title='Unrelated secret project',
            preview='Do not leak',
            workspace_uris=[other_repo.as_uri()],
        )

        db_before = (self.app_dir / 'conversation_summaries.db').read_bytes()
        result = agy_export.export_project_history(self.project, host='codespace', env=self.env)
        self.assertEqual(result, dict(exported=2, unchanged=0, pruned=0, total=2, skipped=False))
        # Source SQLite DB must never be mutated.
        self.assertEqual((self.app_dir / 'conversation_summaries.db').read_bytes(), db_before)

        out_dir = self.scratch / 'history/agy/codespace'
        index = (out_dir / 'INDEX.md').read_text(encoding='utf-8')
        self.assertIn('Fix sync bug', index)
        self.assertIn('Subdir work', index)
        self.assertNotIn('Internal worker', index)
        self.assertNotIn('Unrelated secret project', index)
        self.assertLess(index.index(proj_cid), index.index(subdir_cid))

        doc = (out_dir / f'{proj_cid}.md').read_text(encoding='utf-8')
        self.assertIn('Please fix the retry loop.', doc)
        self.assertIn('Updated `src/retry.py` and verified all tests pass.', doc)
        self.assertIn('`src/retry.py`', doc)
        self.assertNotIn('Secret clock metadata', doc)
        self.assertNotIn('Model changed', doc)
        self.assertNotIn('Internal private chain of thought', doc)
        self.assertNotIn('HUGE_RAW_TOOL_OUTPUT_SHOULD_NOT_APPEAR', doc)
        self.assertFalse((out_dir / f'{subagent_cid}.md').exists())
        self.assertFalse((out_dir / f'{foreign_cid}.md').exists())

        # Incremental re-run skips unchanged files without rewriting them.
        mtime_before = (out_dir / f'{proj_cid}.md').stat().st_mtime_ns
        again = agy_export.export_project_history(self.project, host='codespace', env=self.env)
        self.assertEqual(again, dict(exported=0, unchanged=2, pruned=0, total=2, skipped=False))
        self.assertEqual((out_dir / f'{proj_cid}.md').stat().st_mtime_ns, mtime_before)

    def test_prunes_beyond_limit_and_caps_large_transcript_preserving_latest_turns(self):
        cids = [str(uuid.uuid4()) for _ in range(4)]
        for idx, cid in enumerate(cids):
            rows = []
            if idx == 3:
                for turn in range(40):
                    rows.append({
                        'source': 'USER_EXPLICIT',
                        'type': 'USER_INPUT',
                        'status': 'DONE',
                        'content': f'<USER_REQUEST>\nTurn {turn}: ' + ('x' * 3000) + '\n</USER_REQUEST>',
                    })
                    rows.append({
                        'source': 'MODEL',
                        'type': 'PLANNER_RESPONSE',
                        'status': 'DONE',
                        'content': f'Reply {turn}: ' + ('y' * 3000),
                    })
            _insert_conversation(
                self.app_dir,
                cid,
                title=f'Session {idx}',
                preview=f'Preview {idx}',
                workspace_uris=[self.root.as_uri()],
                last_modified_time=f'2026-11-23T1{idx}:00:00Z',
                rows=rows,
            )

        result = agy_export.export_project_history(self.project, host='mac', env=self.env, max_conversations=2)
        self.assertEqual(result['total'], 2)
        out_dir = self.scratch / 'history/agy/mac'
        self.assertTrue((out_dir / f'{cids[3]}.md').is_file())
        self.assertTrue((out_dir / f'{cids[2]}.md').is_file())
        self.assertFalse((out_dir / f'{cids[0]}.md').exists())

        # Create a stale file and verify pruning removes it while keeping active exports.
        stale = out_dir / 'stale-old-session.md'
        stale.write_text('stale\n', encoding='utf-8')
        second = agy_export.export_project_history(self.project, host='mac', env=self.env, max_conversations=2)
        self.assertEqual(second['pruned'], 1)
        self.assertFalse(stale.exists())

        big_bytes = (out_dir / f'{cids[3]}.md').read_bytes()
        self.assertLessEqual(len(big_bytes), agy_export.MAX_EXPORT_BYTES + 256)
        big_text = big_bytes.decode('utf-8')
        self.assertIn('Earlier turns omitted', big_text)
        self.assertIn('Reply 39:', big_text)
        self.assertNotIn('Turn 0:', big_text)

    def test_corrupt_db_or_symlinked_transcript_fails_gracefully(self):
        self.app_dir.mkdir(parents=True, exist_ok=True)
        (self.app_dir / 'conversation_summaries.db').write_bytes(b'not a sqlite database')
        result = agy_export.export_project_history(self.project, host='mac', env=self.env)
        self.assertTrue(result['skipped'])

        # Symlinked transcript is skipped safely (falls back to summary without reading outside file).
        (self.app_dir / 'conversation_summaries.db').unlink()
        secret = self.base / 'outside-secret.txt'
        secret.write_text('TOP_SECRET_DATA', encoding='utf-8')
        cid = str(uuid.uuid4())
        _insert_conversation(
            self.app_dir, cid, title='Linked', preview='Summary preview',
            workspace_uris=[self.root.as_uri()],
        )
        logs = self.app_dir / 'brain' / cid / '.system_generated' / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        (logs / 'transcript_full.jsonl').symlink_to(secret)
        result = agy_export.export_project_history(self.project, host='mac', env=self.env)
        self.assertEqual(result['exported'], 1)
        exported = (self.scratch / 'history/agy/mac' / f'{cid}.md').read_text(encoding='utf-8')
        self.assertIn('Summary preview', exported)
        self.assertNotIn('TOP_SECRET_DATA', exported)

    def test_sync_cycle_exports_both_mac_and_codespace_agy_history_without_conflict(self):
        from tests.test_replication import IsolatedPythonConnection

        store = Store(self.base / 'local-state')
        connection = IsolatedPythonConnection(self.base / 'remote-home')
        local_home = self.base / 'local-home'
        local_home.mkdir()
        local_env = dict(
            connection.env,
            HOME=str(local_home),
            CODEX_HOME=str(local_home / '.codex'),
            CLAUDE_CONFIG_DIR=str(local_home / '.claude'),
        )

        mac_cid = str(uuid.uuid4())
        cs_cid = str(uuid.uuid4())
        _insert_conversation(
            local_home / '.gemini/antigravity-cli',
            mac_cid,
            title='Mac session',
            preview='Worked on Mac',
            workspace_uris=[self.root.as_uri()],
            rows=[
                {'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE', 'content': '<USER_REQUEST>Local question</USER_REQUEST>'},
                {'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': 'Local answer.'},
            ],
        )
        _insert_conversation(
            connection.home / '.gemini/antigravity-cli',
            cs_cid,
            title='Codespace session',
            preview='Worked in Codespace',
            workspace_uris=[self.root.as_uri()],
            rows=[
                {'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE', 'content': '<USER_REQUEST>Remote question</USER_REQUEST>'},
                {'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': 'Remote answer.'},
            ],
        )

        with patch('devcontainer_setup.project_sync.transport._upstream', return_value=(500, b'{}')):
            cycle = replication.sync_project_data(
                connection, self.project, store.state, str(self.root), env=local_env
            )

        self.assertEqual(cycle['outcome'], 'complete')
        mac_doc = (self.scratch / 'history/agy/mac' / f'{mac_cid}.md').read_text(encoding='utf-8')
        cs_doc = (self.scratch / 'history/agy/codespace' / f'{cs_cid}.md').read_text(encoding='utf-8')
        self.assertIn('Local answer.', mac_doc)
        self.assertIn('Remote answer.', cs_doc)
        self.assertIn('Mac session', (self.scratch / 'history/agy/mac/INDEX.md').read_text(encoding='utf-8'))
        self.assertIn('Codespace session', (self.scratch / 'history/agy/codespace/INDEX.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
