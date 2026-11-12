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
from test_native_checkpoints import NativeFixture, lines


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

    def test_claude_format_details_pass_and_only_malformed_records_refuse(self):
        # Claude Code changes these shapes between releases; they bound no path,
        # memory or session, so sync copies them as they are (#40).
        malformed = []
        for good in self.inline_attachments():
            for key, value in good.items():
                if key == 'type': continue
                missing = copy.deepcopy(good); missing.pop(key); malformed.append(missing)
                changed = copy.deepcopy(good); changed[key] = None; malformed.append(changed)
        drop = self.thinking_drop(); drop['newlyDropped'] = 'future'; malformed.append(drop)
        rows = [dict(type='user', sessionId=self.sid, cwd=self.root)]
        for attachment in malformed:
            with self.subTest(kind=attachment['type']):
                sessions._validate_claude(rows + [dict(type='attachment', attachment=attachment)], self.sid, [self.root])
        for row in (dict(type='ai-title', aiTitle=None), dict(type='permission-mode'), dict(type='file-history-delta', trackingPath=1)):
            with self.subTest(kind=row['type']):
                sessions._validate_claude(rows + [row], self.sid, [self.root])
        for attachment in (dict(type=['hook_success']), dict(type={'name': 'prompt_snapshot'}), dict(content='no kind'), 'inline'):
            with self.subTest(attachment=attachment), self.assertRaisesRegex(ValueError, 'attachment'):
                sessions._validate_claude(rows + [dict(type='attachment', attachment=attachment)], self.sid, [self.root])
        with self.assertRaisesRegex(ValueError, 'session ID'):
            sessions._validate_claude(rows + [dict(type='user', sessionId=str(uuid.uuid4()), cwd=self.root)], self.sid, [self.root])

    def test_inline_attachments_and_agent_calls_pass_but_memory_refuses(self):
        for attachment in self.inline_attachments():
            attachment['unknownPreservedField'] = '<oai-mem-citation>private</oai-mem-citation>'
            data = (json.dumps(dict(type='attachment', attachment=attachment)) + '\n').encode().replace(b'<', b'\\u003c')
            with self.subTest(kind=attachment['type']), self.assertRaisesRegex(ValueError, 'memory'):
                sessions._records(data, 'claude')
        rows = self.requests()
        inline = [dict(type='attachment', attachment=value) for value in self.inline_attachments()]
        # New attachment kinds and agent/task calls arrive with Claude Code releases.
        for good in (dict(type='attachment', attachment=dict(type='future-inline-variant', content='inline')),
                     dict(type='assistant', message=dict(role='assistant', content=[dict(type='tool_use', name='Agent', id='child', input={})]))):
            with self.subTest(kind=good['type']):
                sessions._validate_claude(rows + inline + [good], self.sid, [self.root])

    def test_unknown_claude_records_pass_but_keep_scope_and_memory_refusals(self):
        rows = self.requests()
        sessions._validate_claude(rows + [dict(type='future-record', cwd=self.root, note='newer release')], self.sid, [self.root])
        self.assertEqual(sessions._records(b'{"type":"future-record","note":"kept"}\n', 'claude')[0]['note'], 'kept')
        with self.assertRaisesRegex(ValueError, 'scope|project'):
            sessions._validate_claude(rows + [dict(type='future-record', cwd='/unrelated')], self.sid, [self.root])
        with self.assertRaisesRegex(ValueError, 'memory'):
            sessions._records(b'{"type":"future-record","note":"\\u003coai-mem-citation\\u003eprivate"}\n', 'claude')
        for data in (b'{"note":"no type"}\n', b'["not a record"]\n'):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, 'record type'):
                sessions._records(data, 'claude')
        self.assertEqual(sessions._records(b'{"type":"future-record","payload":{}}\n', 'codex'),
                         [dict(type='future-record', payload={})])

    def test_claude_sessions_may_work_below_the_project_root_but_not_beside_it(self):
        worktree = self.root + '/.claude/worktrees/feature'
        environment = dict(type='attachment', attachment=dict(type='environment', snapshot=dict(workingDirectory=worktree, additionalWorkingDirectories=[])))
        rows = [dict(type='user', sessionId=self.sid, cwd=self.root), dict(type='user', sessionId=self.sid, cwd=worktree + '/web'), environment]
        sessions._validate_claude(rows, self.sid, [self.root])
        for outside in (self.root + '-other', '/workspaces'):
            bad = copy.deepcopy(rows); bad[1]['cwd'] = outside
            with self.subTest(cwd=outside), self.assertRaisesRegex(ValueError, 'another project checkout'):
                sessions._validate_claude(bad, self.sid, [self.root])
            bad = copy.deepcopy(rows); bad[2]['attachment']['snapshot']['workingDirectory'] = outside
            with self.subTest(workingDirectory=outside), self.assertRaisesRegex(ValueError, 'another project checkout'):
                sessions._validate_claude(bad, self.sid, [self.root])
        # --add-dir records its directories here; another project's must not export.
        added = copy.deepcopy(rows); added[2]['attachment']['snapshot']['additionalWorkingDirectories'] = [self.root + '/web']
        sessions._validate_claude(added, self.sid, [self.root])
        added[2]['attachment']['snapshot']['additionalWorkingDirectories'] = ['/workspaces/other']
        with self.assertRaisesRegex(ValueError, 'another project checkout'):
            sessions._validate_claude(added, self.sid, [self.root])
        for cwd in (self.root + '/../other', 'workspaces/project', None, 1):
            bad = copy.deepcopy(rows); bad[1]['cwd'] = cwd
            with self.subTest(cwd=cwd), self.assertRaisesRegex(ValueError, 'scope'):
                sessions._validate_claude(bad, self.sid, [self.root])
        with self.assertRaisesRegex(ValueError, 'Cannot establish Claude project scope'):
            sessions._validate_claude([dict(type='user', sessionId=self.sid), environment], self.sid, [self.root])

    def thinking_drop(self):
        return dict(type='thinking_drop', model='fixture-model', querySource='fixture-query', requestId='fixture-request',
                    thinkingBlocksSent=2, thinkingTurnsSent=1,
                    newlyDropped=dict(blockCount=2, turnCount=1, reason='prefix_mismatch', first=dict(messageIndex=100, blockIndex=2), last=dict(messageIndex=100, blockIndex=4), reasonCounts=dict(prefix_mismatch=1, other=1)),
                    blockHashes=['0abc123', 'zz9'], firstReportForThreadInProcess=True,
                    clientChange=dict(kinds='none', firstChangedMessageIndex=-1, baseline='none', callNumber=1))

    def test_thinking_drop_preserves_telemetry_and_original_thinking_without_lookup(self):
        attachment = self.thinking_drop()
        attachment['futureField'] = dict(historicalPath='/unrelated/provenance', value='preserved')
        rows = self.requests() + [dict(type='attachment', attachment=attachment),
                dict(type='assistant', message=dict(role='assistant', content=[dict(type='thinking', thinking='synthetic thinking', signature='synthetic-signature'), dict(type='redacted_thinking', data='synthetic-redacted'), dict(type='text', text='Answer')]))]
        original = copy.deepcopy(rows)
        data = b''.join(json.dumps(row).encode() + b'\n' for row in rows)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('telemetry requires no blob/file lookup')), patch.object(sessions.subprocess, 'run', side_effect=AssertionError('telemetry is not executable')):
            parsed = sessions._records(data, 'claude')
            sessions._validate_claude(parsed, self.sid, [self.root])
        self.assertEqual(parsed, original)
        self.assertEqual(rows, original)
        attachment['futureField'] = '<oai-mem-citation>private</oai-mem-citation>'
        data = (json.dumps(dict(type='attachment', attachment=attachment)) + '\n').encode().replace(b'<', b'\\u003c')
        with self.assertRaisesRegex(ValueError, 'memory'):
            sessions._records(data, 'claude')

    def test_claude_request_records_are_copied_without_checking_their_chain(self):
        # The chain is Claude Code's own cache bookkeeping; its format moves with
        # each release (one unresolved tail digest per lead session, 2026-10-24).
        rows = self.requests()
        for defect in ('missing_shape', 'missing_blob', 'hash', 'keep', 'digest', 'cache_path', 'scope'):
            broken = copy.deepcopy(rows)
            if defect == 'missing_shape': broken.pop(3)
            elif defect == 'missing_blob': broken.pop(4)
            elif defect == 'hash': broken[4]['message']['content'][0]['text'] = 'Different'
            elif defect == 'keep': broken[5]['keep'] = 1
            elif defect == 'digest': broken[5]['requestDigest'] = '0' * 64
            elif defect == 'cache_path': broken[5]['cacheControl'][0]['path'] = ['system', 99]
            else: broken[1]['attachment']['snapshot']['workingDirectory'] = '/unrelated'
            with self.subTest(defect=defect):
                if defect == 'scope':
                    with self.assertRaisesRegex(ValueError, 'another project checkout'):
                        sessions._validate_claude(broken, self.sid, [self.root])
                else:
                    sessions._validate_claude(broken, self.sid, [self.root])

    def test_structural_codex_scope_cannot_escape_project(self):
        variants = [dict(type='turn_context', payload=dict(cwd='/another-project')), dict(type='turn_context', payload=dict(cwd=self.root + '/../another-project')), dict(type='event_msg', payload=dict(type='thread_settings_applied', thread_settings=dict(cwd='/another-project'))), dict(type='world_state', payload=dict(full=True, state=dict(environments=dict(environments=dict(local=dict(cwd='/another-project')))))), dict(type='turn_context', payload=dict(cwd=self.root, runtime_workspace_roots=['/another-project']))]
        for row in variants:
            with self.subTest(row=row), self.assertRaises(ValueError):
                sessions._validate_codex_scope([row], [self.root])

    def test_malformed_native_records_and_subagent_are_explicit_refusals(self):
        bad = [{}, [], dict(type=None, payload={}), dict(type=1, payload={}),
               dict(type='future-record', payload=None), dict(type='response_item', payload=['unsupported'])]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                sessions._records(json.dumps(row).encode() + b'\n', 'codex')
        for name in ('spawn_agent', 'functions.spawn_agent', 'spawn_agents_on_csv'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'subagent'):
                sessions._validate_codex_dependencies([dict(type='response_item', payload=dict(type='function_call', name=name))], dict(id=self.sid), self.sid)

    def test_unknown_codex_records_pass_but_memory_markers_still_refuse(self):
        for row in (dict(type='future-record', payload=dict(note='preserved')),
                    dict(type='response_item', payload=dict(type='future-response', note='preserved'))):
            with self.subTest(record=row):
                original = copy.deepcopy(row)
                self.assertEqual(sessions._records(json.dumps(row).encode() + b'\n', 'codex'), [original])
                self.assertEqual(row, original)
                for marker in sessions.MEMORY_MARKERS:
                    for in_key in (False, True):
                        memory = copy.deepcopy(row)
                        memory['payload']['future'] = {marker.decode(): 'private'} if in_key else [marker.decode()]
                        encoded = (json.dumps(memory) + '\n').encode().replace(b'<', b'\\u003c')
                        with self.subTest(marker=marker, in_key=in_key), self.assertRaisesRegex(ValueError, 'memory'):
                            sessions._records(encoded, 'codex')

    def test_unknown_codex_payloads_walk_scope_and_known_message_payloads_do_not(self):
        for row in (dict(type='future-record', payload={}),
                    dict(type='response_item', payload=dict(type='future-response')),
                    dict(type='response_item', payload=dict(type=None)),
                    dict(type='response_item', payload=dict(type=['future-response']))):
            for root in (self.root, self.root + '/web', self.root + '-other'):
                scopes = [dict(cwd=root), dict(runtime_workspace_roots=[root]),
                          dict(runtimeWorkspaceRoots=[root]), dict(workspace_roots=[root]),
                          dict(filesystem='<filesystem><workspace_roots><root>' + root +
                               '</root></workspace_roots></filesystem>')]
                for scope in scopes:
                    with self.subTest(record=row, scope=scope):
                        scoped = copy.deepcopy(row)
                        scoped['payload']['nested'] = [scope]
                        original = copy.deepcopy(scoped)
                        if root.endswith('-other'):
                            with self.assertRaisesRegex(ValueError, 'another project checkout'):
                                sessions._validate_codex_scope([scoped], [self.root])
                        else:
                            parsed = sessions._records(json.dumps(scoped).encode() + b'\n', 'codex')
                            sessions._validate_codex_scope(parsed, [self.root])
                            self.assertEqual(parsed, [original])
                        self.assertEqual(scoped, original)
        for row in (dict(type='response_item', payload=dict(type='message')),
                    dict(type='response_item', payload=dict(type='function_call')),
                    dict(type='event_msg', payload=dict(type='future-event'))):
            row['payload']['historical'] = dict(cwd='/unrelated', workspace_roots=['/unrelated'])
            original = copy.deepcopy(row)
            sessions._validate_codex_scope([row], [self.root])
            self.assertEqual(row, original)

    def test_version_boundary_and_global_memory_are_explicit(self):
        accepted = {
            'codex': ('0.163.0', '0.164.0', '0.164.0-alpha.1', '0.165.0', '0.166.0', '0.170.3'),
            'claude': ('2.1.0', '2.1.316', '2.2.0'),
        }
        refused = {
            'codex': ('0.162.9', '0.99.999', '0.164.0-alpha.2', '0.165.0-alpha.1', '0.166.0-beta',
                      '0.166.0+build.1', '1.0.0', '9.0.0', '0.166', 'x', None),
            'claude': ('2.0.9', '3.0.0', '2.2.0-beta', '2.2.0+build.1', 'x'),
        }
        for agent, versions in accepted.items():
            for version in versions:
                with self.subTest(agent=agent, accepted=version):
                    try:
                        sessions._version(agent, version)
                    except ValueError as error:
                        self.fail(str(error))
        for agent, versions in refused.items():
            for version in versions:
                with self.subTest(agent=agent, refused=version), self.assertRaises(ValueError) as error:
                    sessions._version(agent, version)
                self.assertEqual(str(error.exception), f'Unsupported {agent} native version: {version!r}')
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


class NativeVersionCaptureTests(NativeFixture):
    def codex_release(self, version='0.166.0', *, attached_thread=False, dynamic_tools=False):
        path = self.codex()
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        rows[0]['payload'].update(cli_version=version, attached_thread=attached_thread)
        if dynamic_tools:
            rows.insert(-1, dict(type='dynamic_tools_updated', payload=dict(thread_id=self.sid, tools=[])))
        path.write_bytes(lines([dict(row, ordinal=i) for i, row in enumerate(rows)]))
        return path

    def test_plain_completed_codex_0166_session_is_captured(self):
        path = self.codex_release()
        original = path.read_bytes()
        result = self.capture()
        self.assertEqual(len(result['entries']), 1, result['issues'])
        self.assertEqual(result['issues'], [])
        entry = result['entries'][0]
        self.assertEqual(entry['session_id'], self.sid)
        self.assertEqual(self.store.read(entry['files'][0]['blob']), original)
        self.assertEqual(path.read_bytes(), original)

    def test_codex_0166_dynamic_tools_update_is_captured(self):
        path = self.codex_release(dynamic_tools=True)
        original = path.read_bytes()
        result = self.capture()
        self.assertEqual(result['issues'], [])
        self.assertEqual([entry['session_id'] for entry in result['entries']], [self.sid])
        self.assertEqual(self.store.read(result['entries'][0]['files'][0]['blob']), original)
        self.assertEqual(path.read_bytes(), original)

    def test_codex_attached_thread_is_refused(self):
        for version in ('0.164.0', '0.166.0'):
            with self.subTest(version=version):
                path = self.codex_release(version, attached_thread=True)
                original = path.read_bytes()
                result = self.capture()
                self.assertEqual(len(result['entries']), 0, result['issues'])
                self.assertTrue(any('attached-thread' in issue['reason'] for issue in result['issues']), result['issues'])
                self.assertEqual(self.store.objects, {})
                self.assertEqual(path.read_bytes(), original)
