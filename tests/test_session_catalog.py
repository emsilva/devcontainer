"""Metadata rendering remains bounded and ignores native instruction envelopes."""
import json
from pathlib import Path
import tempfile
import unittest

from devcontainer_setup.project_sync.sessions import _catalog_metadata


class CatalogTests(unittest.TestCase):
    def metadata(self, rows, agent='codex'):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'session.jsonl'
            data = b''.join(json.dumps(row).encode() + b'\n' for row in rows)
            path.write_bytes(data)
            return _catalog_metadata(data, agent, path)

    def test_codex_prompt_skips_native_instruction_and_environment_records(self):
        rows = [dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='# AGENTS.md instructions for /project\n<INSTRUCTIONS>Guidance</INSTRUCTIONS>'), dict(type='input_text', text='<environment_context>Local runtime</environment_context>')])), dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='Repair conversation recovery')]))]
        self.assertEqual(self.metadata(rows)['title'], 'Repair conversation recovery')

    def test_titles_are_bounded_and_strip_terminal_and_unicode_controls(self):
        rows = [dict(type='session_meta', payload=dict(title='Title\x1b]0;bad\x07\u202e here'))]
        self.assertEqual(self.metadata(rows)['title'], 'Title here')
        rows[0]['payload']['title'] = 'x' * 200
        self.assertEqual(len(self.metadata(rows)['title']), 120)

    def test_claude_title_and_missing_title(self):
        rows = [dict(type='user', message=dict(role='user', content='First question'))]
        self.assertEqual(self.metadata(rows, 'claude')['title'], 'First question')
        rows.append(dict(type='custom-title', customTitle='My project title'))
        self.assertEqual(self.metadata(rows, 'claude')['title'], 'My project title')
        self.assertEqual(self.metadata([dict(type='user', message=dict(role='user', content=''))], 'claude')['title'], 'Untitled conversation')
