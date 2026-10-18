"""Scope and native dependency validation survives retirement of custody transfer."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
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

    def inline_attachments(self):
        return [
            dict(type='hook_success', command='historical-command', content='inline result', stdout='ok', stderr='', hookName='fixture', hookEvent='SessionStart', toolUseID='fixture-tool', durationMs=1.5, exitCode=0),
            dict(type='hook_additional_context', content=['inline context'], hookName='fixture', hookEvent='SessionStart', toolUseID='fixture-tool'),
            dict(type='deferred_tools_delta', **{key: ['fixture'] for key in ('addedLines', 'addedNames', 'readdedNames', 'removedNames', 'wireHiddenNames', 'pendingMcpServers', 'failedMcpServers', 'needsAuthMcpServers')}, toolSearchAbsent=False),
            dict(type='mcp_instructions_delta', addedBlocks=['inline instructions'], addedNames=['fixture'], removedNames=[]),
            dict(type='instructions', files=[dict(path='/historical-outside-checkout/CLAUDE.md', content='inline user instructions', type='User'), dict(path=self.root + '/CLAUDE.md', content='inline project instructions', type='Project')]),
            dict(type='prompt_snapshot', systemPrompt=['inline system prompt'], contextRendering='inline context', echoWireToolInputs=False, reminderFold=True, inlineTools=True, keptReminders=False, systemTurns=True, toolChangeHeader=False, cliPrefix='historical prefix', tools=[dict(name='fixture-tool', description='inline tool', schema=dict(type='object', properties={}))]),
        ]

    def test_six_inline_claude_attachments_preserve_fields_without_dereferencing(self):
        for attachment in self.inline_attachments():
            with self.subTest(kind=attachment['type']):
                attachment['futureInlineField'] = dict(path='/unrelated/historical/path', body='preserved')
                rows = [dict(type='user', sessionId=self.sid, cwd=self.root, message=dict(role='user', content='Project request')),
                        dict(type='attachment', attachment=attachment)]
                before = copy.deepcopy(rows)
                encoded = b''.join(json.dumps(row).encode() + b'\n' for row in rows)
                with patch.object(sessions.subprocess, 'run', side_effect=AssertionError('must not execute inline command')), patch.object(Path, 'read_bytes', side_effect=AssertionError('must not dereference provenance')):
                    parsed = sessions._records(encoded, 'claude')
                    try:
                        sessions._validate_claude(parsed, self.sid, [self.root])
                    except ValueError as error:
                        self.fail(str(error))
                self.assertEqual(parsed, before)
                self.assertEqual(rows, before)
                # Provenance does not authorize another structural runtime root.
                parsed[0]['cwd'] = '/unrelated/historical/path'
                with self.assertRaises(ValueError):
                    sessions._validate_claude(parsed, self.sid, [self.root])

    def test_inline_claude_attachments_reject_malformed_known_shapes(self):
        broken = [dict(type=['hook_success']), dict(type={'name': 'prompt_snapshot'})]
        for good in self.inline_attachments():
            for key, value in good.items():
                if key == 'type': continue
                missing = copy.deepcopy(good); missing.pop(key)
                # Snapshot rendering/tool fields are optional, but typed when present.
                if good['type'] != 'prompt_snapshot' or key in ('systemPrompt', 'contextRendering'):
                    broken.append(missing)
                malformed = copy.deepcopy(good)
                malformed[key] = 1 if isinstance(value, bool) else (False if isinstance(value, (int, float)) else None)
                broken.append(malformed)
        for kind, key in ((0, 'durationMs'), (0, 'exitCode')):
            bad = self.inline_attachments()[kind]; bad[key] = True; broken.append(bad)
        for duration in (-1, float('nan'), float('inf')):
            bad = self.inline_attachments()[0]; bad['durationMs'] = duration; broken.append(bad)
        for bad_file in (dict(path='historic', type='Project'), dict(path='historic', content='inline', type='Future'), dict(path=123, content='inline', type='User')):
            bad = self.inline_attachments()[4]; bad['files'] = [bad_file]; broken.append(bad)
        for bad_tool in (dict(name='tool', description='inline', schema=[]), dict(name='tool', schema={}), 'external-tool-reference'):
            bad = self.inline_attachments()[5]; bad['tools'] = [bad_tool]; broken.append(bad)
        for kind in (1, 2, 3, 5):
            bad = self.inline_attachments()[kind]
            key = {1: 'content', 2: 'addedNames', 3: 'addedBlocks', 5: 'systemPrompt'}[kind]
            bad[key] = ['inline', {}]; broken.append(bad)
        for attachment in broken:
            with self.subTest(kind=attachment['type']), self.assertRaises(ValueError):
                rows = [dict(type='user', sessionId=self.sid, cwd=self.root), dict(type='attachment', attachment=attachment)]
                sessions._validate_claude(rows, self.sid, [self.root])

    def test_inline_attachments_keep_memory_unknown_dependency_and_request_refusals(self):
        for attachment in self.inline_attachments():
            attachment['unknownPreservedField'] = '<oai-mem-citation>private</oai-mem-citation>'
            data = (json.dumps(dict(type='attachment', attachment=attachment)) + '\n').encode().replace(b'<', b'\\u003c')
            with self.subTest(kind=attachment['type']), self.assertRaisesRegex(ValueError, 'memory'):
                sessions._records(data, 'claude')
        rows = self.requests()
        inline = [dict(type='attachment', attachment=value) for value in self.inline_attachments()]
        for bad in (dict(type='attachment', attachment=dict(type='future-inline-variant', content='inline')),
                    dict(type='assistant', message=dict(role='assistant', content=[dict(type='tool_use', name='Agent', id='child', input={})])),
                    dict(type='api-request', shapeHash='0'*64, keep=0, tail=[], params={}, requestDigest='0'*64)):
            with self.subTest(kind=bad['type']), self.assertRaises(ValueError):
                sessions._validate_claude(rows + inline + [bad], self.sid, [self.root])
        corrupted = copy.deepcopy(rows); corrupted[-1]['requestDigest'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'digest'):
            sessions._validate_claude(corrupted + inline, self.sid, [self.root])

    def thinking_drop(self):
        return dict(type='thinking_drop', model='fixture-model', querySource='fixture-query', requestId='fixture-request',
                    thinkingBlocksSent=2, thinkingTurnsSent=1,
                    newlyDropped=dict(blockCount=2, turnCount=1, reason='prefix_mismatch', first=dict(messageIndex=100, blockIndex=2), last=dict(messageIndex=100, blockIndex=4), reasonCounts=dict(prefix_mismatch=1, other=1)),
                    blockHashes=['0abc123', 'zz9'], firstReportForThreadInProcess=True,
                    clientChange=dict(kinds='none', firstChangedMessageIndex=-1, baseline='none', callNumber=1))

    def test_thinking_drop_preserves_telemetry_and_original_thinking_without_lookup(self):
        variants = [self.thinking_drop() for _ in range(4)]
        variants[1].pop('requestId'); variants[1]['clientChange'].pop('callNumber')
        variants[2]['requestedBehavior'] = 'fixture-behavior'
        variants[3].update(thinkingBlocksSent=0, thinkingTurnsSent=0)
        for attachment in variants:
            with self.subTest(optional_fields=sorted(attachment)):
                attachment['futureField'] = dict(historicalPath='/unrelated/provenance', value='preserved')
                rows = self.requests() + [dict(type='attachment', attachment=attachment),
                        dict(type='assistant', message=dict(role='assistant', content=[dict(type='thinking', thinking='synthetic thinking', signature='synthetic-signature'), dict(type='redacted_thinking', data='synthetic-redacted'), dict(type='text', text='Answer')]))]
                original = copy.deepcopy(rows)
                data = b''.join(json.dumps(row).encode() + b'\n' for row in rows)
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('telemetry requires no blob/file lookup')), patch.object(sessions.subprocess, 'run', side_effect=AssertionError('telemetry is not executable')):
                    parsed = sessions._records(data, 'claude')
                    try:
                        sessions._validate_claude(parsed, self.sid, [self.root])
                    except ValueError as error:
                        self.fail(str(error))
                self.assertEqual(parsed, original)
                self.assertEqual(rows, original)

    def test_thinking_drop_rejects_malformed_nested_shapes_counters_hashes(self):
        defects = []
        for key, value in self.thinking_drop().items():
            if key == 'type': continue
            if key != 'requestId':
                bad = self.thinking_drop(); bad.pop(key); defects.append(bad)
            bad = self.thinking_drop(); bad[key] = None; defects.append(bad)
        for key in ('thinkingBlocksSent', 'thinkingTurnsSent'):
            for value in (-1, True, 1.5):
                bad = self.thinking_drop(); bad[key] = value; defects.append(bad)
        for key in ('blockCount', 'turnCount'):
            for value in (0, -1, True, 1.5):
                bad = self.thinking_drop(); bad['newlyDropped'][key] = value; defects.append(bad)
        for key in ('first', 'last'):
            for index in ('messageIndex', 'blockIndex'):
                for value in (-1, True, 1.5, None):
                    bad = self.thinking_drop(); bad['newlyDropped'][key][index] = value; defects.append(bad)
            bad = self.thinking_drop(); bad['newlyDropped'][key] = []; defects.append(bad)
        for counts in ({'future_reason': 2}, {'prefix_mismatch': True, 'other': 1}, {'other': -2}, {'other': 1}, {'other': 3}, []):
            bad = self.thinking_drop(); bad['newlyDropped']['reasonCounts'] = counts; defects.append(bad)
        for hashes in (None, [], ['0abc123'], ['0abc123', 'zz9', 'third'], ['UPPER', 'zz9'], ['abcdefghijklmn', 'zz9'], ['bad/hash', 'zz9'], ['', 'zz9'], [123, 'zz9']):
            bad = self.thinking_drop(); bad['blockHashes'] = hashes; defects.append(bad)
        for key in ('kinds', 'firstChangedMessageIndex', 'baseline', 'callNumber'):
            bad = self.thinking_drop(); bad['clientChange'][key] = True; defects.append(bad)
        bad = self.thinking_drop(); bad['clientChange']['callNumber'] = -1; defects.append(bad)
        bad = self.thinking_drop(); bad['newlyDropped']['turnCount'] = 3; defects.append(bad)
        bad = self.thinking_drop(); bad['newlyDropped']['first']['messageIndex'] = 101; defects.append(bad)
        bad = self.thinking_drop(); bad['newlyDropped']['first']['blockIndex'] = 5; defects.append(bad)
        bad = self.thinking_drop(); bad['newlyDropped']['reason'] = 'future_reason'; defects.append(bad)
        bad = self.thinking_drop(); bad['requestedBehavior'] = []; defects.append(bad)
        bad = self.thinking_drop(); bad['firstReportForThreadInProcess'] = 1; defects.append(bad)
        for number, attachment in enumerate(defects):
            with self.subTest(defect=number), self.assertRaises(ValueError):
                sessions._validate_claude(self.requests() + [dict(type='attachment', attachment=attachment)], self.sid, [self.root])

    def test_thinking_drop_keeps_privacy_scope_request_and_external_dependency_refusals(self):
        attachment = self.thinking_drop()
        attachment['futureField'] = '<oai-mem-citation>private</oai-mem-citation>'
        data = (json.dumps(dict(type='attachment', attachment=attachment)) + '\n').encode().replace(b'<', b'\\u003c')
        with self.assertRaisesRegex(ValueError, 'memory'):
            sessions._records(data, 'claude')
        rows = self.requests() + [dict(type='attachment', attachment=self.thinking_drop())]
        for extra, reason in ((dict(type='attachment', attachment=dict(type='future-thinking-report')), 'attachment dependency'),
                              (dict(type='assistant', message=dict(role='assistant', content=[dict(type='tool_use', name='Agent', input={})])), 'agent/task'),
                              (dict(type='user', cwd='/unrelated'), 'scope|project')):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                sessions._validate_claude(rows + [extra], self.sid, [self.root])
        rows[5]['requestDigest'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'digest'):
            sessions._validate_claude(rows, self.sid, [self.root])

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

    def test_native_reader_rejects_symlink_swap_after_path_preflight(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            native = root / 'native.jsonl'
            private = root / 'unrelated-private.json'
            native.write_bytes(b'project transcript')
            private.write_bytes(b'fixture unrelated private contents')
            safe = sessions._safe
            def swap(path):
                checked = safe(path)
                native.unlink()
                native.symlink_to(private)
                return checked
            with patch.object(sessions, '_safe', side_effect=swap), self.assertRaises((ValueError, OSError)):
                sessions._read(native)
