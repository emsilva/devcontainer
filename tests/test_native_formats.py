"""Scope and native dependency validation survives retirement of custody transfer."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from devcontainer_setup.project_sync import sessions
from devcontainer_setup.project_sync.agents import _history


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class NativeFormatTests(unittest.TestCase):
    def setUp(self):
        self.root = '/workspaces/project'
        self.sid = str(uuid.uuid4())

    def requests(self):
        shape = dict(system=[dict(type='text', text='Project instructions')], tools=[])
        message = dict(role='user', content=[dict(type='text', text='Project request')])
        rows = [dict(type='user', sessionId=self.sid, cwd=self.root, message=dict(role='user', content='Original project prompt')), dict(type='attachment', sessionId=self.sid, cwd=self.root, attachment=dict(type='environment', snapshot=dict(workingDirectory=self.root, additionalWorkingDirectories=[]))), dict(type='atis-latch', sessionId=self.sid, atis='fixture'), dict(type='api-request-shape', sessionId=self.sid, shapeHash=digest(shape), shape=shape), dict(type='api-request-blob', sessionId=self.sid, hash=digest(message), message=message)]
        for count in (1, 2):
            request = copy.deepcopy(dict(shape, messages=[message] * count))
            request['system'][0]['cache_control'] = dict(type='ephemeral')
            rows.append(dict(type='api-request', sessionId=self.sid, id=str(uuid.uuid4()), shapeHash=digest(shape), keep=count - 1, tail=[digest(message)], params={}, cacheControl=[dict(path=['system', 0], value=dict(type='ephemeral'))], requestDigest=digest(request)))
        return rows

    def test_claude_request_shape_blob_delta_and_cache_control_are_validated(self):
        rows = self.requests()
        sessions._validate_claude(rows, self.sid, [self.root])
        for defect in ('missing_shape', 'missing_blob', 'hash', 'keep', 'scope', 'digest', 'cache_path'):
            broken = copy.deepcopy(rows)
            if defect == 'missing_shape': broken.pop(3)
            elif defect == 'missing_blob': broken.pop(4)
            elif defect == 'hash': broken[4]['message']['content'][0]['text'] = 'Different'
            elif defect == 'keep': broken[5]['keep'] = 1
            elif defect == 'scope': broken[1]['attachment']['snapshot']['workingDirectory'] = '/unrelated'
            elif defect == 'digest': broken[5]['requestDigest'] = '0' * 64
            else: broken[5]['cacheControl'][0]['path'] = ['system', 99]
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                sessions._validate_claude(broken, self.sid, [self.root])

    def test_claude_resume_resolves_native_messages_without_explicit_blob_rows(self):
        rows = self.requests()[:6]
        assistant = dict(role='assistant', content=[dict(type='text', text='Earlier project reply')])
        user = dict(role='user', content=[dict(type='text', text='Resume this project')])
        rows.extend([dict(type='assistant', sessionId=self.sid, cwd=self.root, message=dict(assistant, id='native-message', model='native-model')), dict(type='user', sessionId=self.sid, cwd=self.root, message=dict(role='user', content='Resume this project')), dict(type='mode', sessionId=self.sid, mode='default')])
        messages = [rows[4]['message'], assistant, user]
        request = dict(rows[3]['shape'], messages=messages)
        rows.append(dict(type='api-request', sessionId=self.sid, shapeHash=rows[3]['shapeHash'], keep=0, tail=[digest(message) for message in messages], params={}, cacheControl=[], requestDigest=digest(request)))
        sessions._validate_claude(rows, self.sid, [self.root])

    def test_structural_codex_scope_cannot_escape_project(self):
        variants = [dict(type='turn_context', payload=dict(cwd='/another-project')), dict(type='turn_context', payload=dict(cwd=self.root + '/../another-project')), dict(type='event_msg', payload=dict(type='thread_settings_applied', thread_settings=dict(cwd='/another-project'))), dict(type='world_state', payload=dict(full=True, state=dict(environments=dict(environments=dict(local=dict(cwd='/another-project')))))), dict(type='turn_context', payload=dict(cwd=self.root, runtime_workspace_roots=['/another-project']))]
        for row in variants:
            with self.subTest(row=row), self.assertRaises(ValueError):
                sessions._validate_codex_scope([row], [self.root])

    def test_unknown_native_record_payload_and_subagent_are_explicit_refusals(self):
        bad = [dict(type='future-record', payload={}), dict(type='response_item', payload=['unsupported']), dict(type='response_item', payload=dict(type='future-response'))]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                sessions._records(json.dumps(row).encode() + b'\n', 'codex')
        for name in ('spawn_agent', 'functions.spawn_agent', 'spawn_agents_on_csv'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'subagent'):
                sessions._validate_codex_dependencies([dict(type='response_item', payload=dict(type='function_call', name=name))], dict(id=self.sid), self.sid)

    def test_version_boundary_and_global_memory_are_explicit(self):
        for version in ('0.163.0', '0.164.0', '0.164.0-alpha.1'):
            sessions._version('codex', version)
        for version in ('9.0.0', '0.164.0-alpha.2', '0.165.0-alpha.1'):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'version'):
                sessions._version('codex', version)
        for marker in sessions.MEMORY_MARKERS:
            row = dict(type='response_item', payload=dict(type='message', role='developer', content=[dict(type='input_text', text=marker.decode())]))
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, 'memory'):
                sessions._records(json.dumps(row).encode() + b'\n', 'codex')

    def test_native_history_drains_all_pages_and_rejects_stalled_cursor(self):
        class Client:
            def call(inner, method, params):
                page = int(params.get('cursor', 0))
                return dict(data=[dict(number=page)], nextCursor=str(page + 1) if page < 12 else None)
        result = _history(Client(), self.sid)
        self.assertEqual(result['turns_pages'], 13)
        self.assertEqual(result['items_pages'], 13)
        self.assertEqual(len(result['turns']), 13)
        class Stalled:
            def call(inner, method, params):
                return dict(data=[], nextCursor='same')
        with self.assertRaisesRegex(ValueError, 'stalled'):
            _history(Stalled(), self.sid)
