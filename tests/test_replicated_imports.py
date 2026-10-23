"""Imports received over real replication cycles leave no unreferenced object in either store."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from devcontainer_setup.project_sync import conversations, replication, rpc, sessions
from devcontainer_setup.project_sync.checkpoint_store import Store
from devcontainer_setup.project_sync.state import discover_project, state_dir

from store_fixtures import assert_every_object_referenced
from test_native_checkpoints import lines

CYCLES = 3


class InProcessConnection:
    """IsolatedPythonConnection (test_replication.py) with RPC served in this process, so Codex can be faked."""
    codespace = 'fixture-cs'

    def __init__(self, env):
        self.env = env

    def python(self, code, payload=b'', timeout=60):
        if payload:
            # The remote bootstrap's request and response JSON (rpc.py), minus the module copy.
            with patch.dict(os.environ, self.env, clear=True):
                return json.dumps(rpc._dispatch(json.loads(payload)['request']), sort_keys=True).encode()
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], capture_output=True,
                                env=self.env, cwd=self.env['HOME'], timeout=timeout)
        if result.returncode:
            raise AssertionError(result.stderr.decode(errors='replace'))
        return result.stdout


class FakeAppServer:
    """`codex app-server` as measured on 0.163.0: each thread/resume appends a thread_settings_applied
    record, thread/items/list reports a persisted Extension of kind web.search as webSearch, and
    thread/list hides exec sessions."""
    resumes = []

    def __init__(self, project, *, env=None, **_):
        self.home = sessions.native_home('codex', env)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def _rows(self, sid):
        path = next(self.home.rglob('*' + sid + '.jsonl'))
        return path, [json.loads(line) for line in path.read_bytes().splitlines()]

    def call(self, method, params):
        if method == 'thread/resume':
            sid = params['threadId']
            path, rows = self._rows(sid)
            with path.open('ab') as out:
                out.write(lines([dict(timestamp=f'2026-10-21T00:46:{len(FakeAppServer.resumes):02d}.000Z',
                                      ordinal=len(rows), type='event_msg', payload=dict(
                    type='thread_settings_applied', thread_id=sid, thread_settings=dict(
                        cwd=params['cwd'], runtime_workspace_roots=params['runtimeWorkspaceRoots'])))]))
            FakeAppServer.resumes.append(sid)
            return dict(thread=dict(id=sid))
        if method == 'thread/turns/list':
            status = {}
            for row in self._rows(params['threadId'])[1]:
                event = row['payload'].get('type') if row['type'] == 'event_msg' else None
                if event in ('task_started', 'task_complete', 'turn_aborted'):
                    status[row['payload']['turn_id']] = dict(task_started='inProgress', task_complete='completed',
                                                              turn_aborted='interrupted')[event]
            return dict(data=[dict(id=turn, status=value) for turn, value in status.items()], nextCursor=None)
        if method == 'thread/items/list':
            items = []
            for row in self._rows(params['threadId'])[1]:
                if row['type'] == 'event_msg' and row['payload'].get('type') == 'item_completed':
                    item = dict(row['payload']['item'])
                    name = item.get('type', '')
                    item['type'] = ('webSearch' if name == 'Extension' and item.get('kind') == 'web.search'
                                    else name[:1].lower() + name[1:])
                    items.append(dict(item=item))
            return dict(data=items, nextCursor=None)
        if method == 'thread/list':
            visible = [sid for sid in dict.fromkeys(FakeAppServer.resumes)
                       if self._rows(sid)[1][0]['payload'].get('source') != 'exec']
            return dict(data=[dict(id=sid) for sid in visible], nextCursor=None)
        raise AssertionError('unexpected app-server method ' + method)


class ReplicatedImportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.desktop_root, self.codespace_root = self.base / 'mac/repo', self.base / 'workspaces/repo'
        for root in (self.desktop_root, self.codespace_root):
            root.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'https://github.com/example/imports.git'], check=True)
        self.project = discover_project(self.desktop_root)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'CODEX_', 'CLAUDE_', 'XDG_'))}
        def side(name):
            home = self.base / name
            home.mkdir()
            # A fixture token, so a failed verification's state read never reaches real gh.
            return dict(clean, HOME=str(home), XDG_STATE_HOME=str(home / 'state'), GH_TOKEN='fixture-token',
                        CODEX_HOME=str(home / '.codex'), CLAUDE_CONFIG_DIR=str(home / '.claude'))
        self.desktop_env, self.codespace_env = side('desktop-home'), side('codespace-home')
        self.desktop_store = Store(state_dir(self.project, self.desktop_env))
        self.codespace_store = Store(state_dir(self.project, self.codespace_env))
        self.connection = InProcessConnection(self.codespace_env)
        FakeAppServer.resumes = []
        for mocked in (patch.object(conversations.agents, 'CodexClient', FakeAppServer),
                       patch('devcontainer_setup.project_sync.transport._upstream', return_value=(500, b'{}'))):
            mocked.start()
            self.addCleanup(mocked.stop)

    def desktop_codex(self, *, web_search=False):
        sid, turn = str(uuid.uuid4()), str(uuid.uuid4())
        meta = dict(id=sid, cwd=str(self.desktop_root), runtime_workspace_roots=[str(self.desktop_root)],
                    cli_version='0.163.0', source='cli')
        rows = [dict(type='session_meta', payload=meta), dict(type='event_msg', payload=dict(type='task_started', turn_id=turn))]
        if web_search:
            # The shape Codex persists for a web search (key names from real rollouts).
            rows.append(dict(type='event_msg', payload=dict(type='item_completed', thread_id=sid, turn_id=turn, item=dict(
                type='Extension', kind='web.search', id='exec-' + str(uuid.uuid4()), query='orchid',
                action=dict(type='search', query=None, queries=['orchid']), results=[]))))
        rows += [dict(type='response_item', payload=dict(type='message', role='assistant', content=[dict(type='output_text', text='ORCHID')])),
                 dict(type='event_msg', payload=dict(type='task_complete', turn_id=turn))]
        path = Path(self.desktop_env['CODEX_HOME']) / 'sessions/2026/10/17' / f'rollout-2026-10-17T12-00-00-{sid}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(lines([dict(row, ordinal=i) for i, row in enumerate(rows)]))

    def desktop_claude(self):
        sid = str(uuid.uuid4())
        path = Path(self.desktop_env['CLAUDE_CONFIG_DIR']) / 'projects' / sessions.claude_namespace(self.desktop_root) / (sid + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [dict(type='user', uuid=str(uuid.uuid4()), message=dict(role='user', content='word?')),
                dict(type='assistant', uuid=str(uuid.uuid4()), message=dict(role='assistant', content=[dict(type='text', text='ORCHID')], stop_reason='end_turn'))]
        path.write_bytes(lines([dict(row, cwd=str(self.desktop_root), sessionId=sid, version='2.1.302') for row in rows]))

    def run_cycles(self):
        """Run real two-way cycles; after each, every object in either store is referenced by a saved manifest."""
        history = []
        for number in range(1, CYCLES + 1):
            cycle = replication.sync_project_data(self.connection, self.project, self.desktop_store.state,
                                                  str(self.codespace_root), env=self.desktop_env)
            inbound = cycle['directions']['codespace_to_desktop']
            # The Codespace capture dropped its import as unchanged rather than refusing it.
            self.assertEqual((inbound['counts']['captured'], inbound['capture_issues']), (0, []))
            for name, store in (('Codespace', self.codespace_store), ('desktop', self.desktop_store)):
                with self.subTest(cycle=number, store=name):
                    assert_every_object_referenced(self, store)
            receipts = (self.codespace_store.state / 'conversation-imports').glob('*/receipt.json')
            history.append(dict(receipts=[json.loads(path.read_bytes())['status'] for path in receipts],
                                resumes=len(FakeAppServer.resumes)))
        return history

    def test_completed_codex_import_leaves_every_object_referenced(self):
        self.desktop_codex()
        self.assertEqual([step['receipts'] for step in self.run_cycles()], [['complete']] * CYCLES)

    def test_completed_claude_import_leaves_every_object_referenced(self):
        self.desktop_claude()
        self.assertEqual([step['receipts'] for step in self.run_cycles()], [['complete']] * CYCLES)

    def test_import_with_web_search_leaves_every_object_referenced(self):
        self.desktop_codex(web_search=True)
        self.run_cycles()


if __name__ == '__main__':
    unittest.main()
