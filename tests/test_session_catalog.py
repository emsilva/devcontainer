import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from devcontainer_setup.project_sync.sessions import list_sessions, claude_namespace


class CatalogTests(unittest.TestCase):
    def test_titles_recency_scope_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = SimpleNamespace(root=home / 'repo')
            directory = home / 'sessions'
            directory.mkdir()
            ids = [str(uuid.uuid4()) for _ in range(4)]
            for i, sid in enumerate(ids):
                rows = [dict(type='session_meta', payload=dict(id=sid, cwd=str(project.root if i != 2 else home / 'other'), cli_version='0.164.0')),
                        dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='Fix\x1b[31m it\nnow')]))]
                path = directory / (sid + '.jsonl')
                path.write_text(''.join(json.dumps(r) + '\n' for r in rows) if i != 3 else '{bad}\n')
                os.utime(path, (100 + i, 100 + i))
            result = list_sessions(project, 'codex', home)
            self.assertEqual([r['session_id'] for r in result], ids[1::-1])
            self.assertEqual(result[0]['title'], 'Fix it now')
            self.assertEqual(result[0]['updated_at'], 101)

    def test_claude_title_and_missing_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = SimpleNamespace(root=home / 'repo')
            directory = home / 'projects' / claude_namespace(project.root)
            directory.mkdir(parents=True)
            sid = str(uuid.uuid4())
            path = directory / (sid + '.jsonl')
            rows = [dict(type='user', cwd=str(project.root), sessionId=sid, message=dict(role='user', content='First question'))]
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            self.assertEqual(list_sessions(project, 'claude', home)[0]['title'], 'First question')
            rows.append(dict(type='custom-title', customTitle='My title' + 'x'*200))
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            self.assertEqual(len(list_sessions(project, 'claude', home)[0]['title']), 120)
            rows[0]['message']['content'] = ''
            path.write_text(json.dumps(rows[0])+'\n')
            self.assertEqual(list_sessions(project, 'claude', home)[0]['title'], 'Untitled conversation')

    def test_codex_native_title_over_prompt_and_control_sanitization(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = SimpleNamespace(root=home / 'repo')
            sid = str(uuid.uuid4())
            path = home / 'sessions' / (sid + '.jsonl')
            path.parent.mkdir()
            rows = [dict(type='session_meta', payload=dict(id=sid, cwd=str(project.root), cli_version='0.164.0', title='Title\x1b]0;bad\x07\u202e here'))]
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            self.assertEqual(list_sessions(project, 'codex', home)[0]['title'], 'Title here')
