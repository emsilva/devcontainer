import importlib
import json
import os
import select
import pty
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'
        self.target = self.root / 'target'
        self.sid = str(uuid.uuid4())
        self.api = importlib.import_module('devcontainer_setup.project_sync.ownership')

    def test_unknown_owner_cannot_resume_and_new_lease_is_exclusive(self):
        with self.assertRaisesRegex(ValueError, 'unknown|Unknown'):
            with self.api.managed_lease(self.source, 'codex', self.sid, 'a'):
                pass
        with self.api.managed_lease(self.source, 'codex', self.sid, 'a', new=True):
            code = 'from pathlib import Path; from devcontainer_setup.project_sync.ownership import managed_lease; import sys\nwith managed_lease(Path(sys.argv[1]), "codex", sys.argv[2], "a"): pass'
            attempt = subprocess.run([sys.executable, '-c', code, str(self.source), self.sid], capture_output=True, text=True)
            self.assertNotEqual(attempt.returncode, 0)
            self.assertIn('lease', attempt.stderr)
        self.assertEqual(self.api.owner_status(self.source, 'codex', self.sid)['status'], 'idle')

    def test_four_stage_transfer_blocks_both_sides_until_committed(self):
        with self.api.managed_lease(self.source, 'codex', self.sid, 'a', new=True):
            pass
        transfer = self.api.begin_transfer(self.source, 'codex', self.sid, 'a', 'b')
        receipt = dict(status='complete', session_id=self.sid, agent='codex', source_host='a', generation=transfer['generation'], bundle_id='abc')
        ack = self.api.accept_transfer(self.target, 'codex', self.sid, 'b', transfer, receipt)
        for state, host in ((self.source, 'a'), (self.target, 'b')):
            with self.assertRaises(ValueError):
                with self.api.managed_lease(state, 'codex', self.sid, host):
                    pass
        committed = self.api.complete_transfer(self.source, 'codex', self.sid, 'a', ack)
        self.api.activate_transfer(self.target, 'codex', self.sid, 'b', committed)
        with self.api.managed_lease(self.target, 'codex', self.sid, 'b'):
            pass
        with self.assertRaises(ValueError):
            with self.api.managed_lease(self.source, 'codex', self.sid, 'a'):
                pass
        # A lost final response can be recovered by repeating exact receipts.
        self.api.activate_transfer(self.target, 'codex', self.sid, 'b', committed)

    def test_receive_reserves_destination_before_import_and_is_retryable(self):
        with self.api.managed_lease(self.source, 'codex', self.sid, 'a', new=True):
            pass
        transfer = self.api.begin_transfer(self.source, 'codex', self.sid, 'a', 'b')
        pending = self.api.prepare_receive(self.target, 'codex', self.sid, 'b', transfer)
        self.assertEqual(pending['status'], 'receiving')
        self.assertEqual(self.api.prepare_receive(self.target, 'codex', self.sid, 'b', transfer), pending)
        with self.assertRaises(ValueError):
            with self.api.managed_lease(self.target, 'codex', self.sid, 'b'):
                pass
        receipt = dict(status='complete', session_id=self.sid, agent='codex', source_host='a', generation=transfer['generation'], bundle_id='abc')
        ack = self.api.accept_transfer(self.target, 'codex', self.sid, 'b', transfer, receipt)
        self.assertEqual(ack['status'], 'accepted')

    def test_wrong_nonce_incomplete_import_and_live_transfer_refused(self):
        with self.api.managed_lease(self.source, 'claude', self.sid, 'a', new=True):
            with self.assertRaises(ValueError):
                self.api.begin_transfer(self.source, 'claude', self.sid, 'a', 'b')
        transfer = self.api.begin_transfer(self.source, 'claude', self.sid, 'a', 'b')
        with self.assertRaises(ValueError):
            self.api.accept_transfer(self.target, 'claude', self.sid, 'b', transfer, dict(status='prepared'))
        with self.assertRaises(ValueError):
            self.api.complete_transfer(self.source, 'claude', self.sid, 'a', dict(transfer, nonce='wrong', status='accepted'))

    def test_crashed_lease_requires_explicit_recovery_and_preserves_evidence(self):
        code = 'from pathlib import Path; from devcontainer_setup.project_sync.ownership import managed_lease; import os,sys\nwith managed_lease(Path(sys.argv[1]), "codex", sys.argv[2], "a", new=True): os._exit(23)'
        child = subprocess.run([sys.executable, '-c', code, str(self.source), self.sid], capture_output=True)
        self.assertEqual(child.returncode, 23)
        with self.assertRaisesRegex(ValueError, 'recover|uncertain'):
            with self.api.managed_lease(self.source, 'codex', self.sid, 'a'):
                pass
        recovered = self.api.recover_local(self.source, 'codex', self.sid, 'a', native_paths=[])
        self.assertEqual(recovered['status'], 'idle')
        self.assertEqual(len(list((self.source / 'owners').glob('*.recovery-*.json'))), 1)

    def test_transfer_return_generation_replay_refused(self):
        with self.api.managed_lease(self.source, 'codex', self.sid, 'a', new=True):
            pass
        def move(source, dest, a, b):
            transfer = self.api.begin_transfer(source, 'codex', self.sid, a, b)
            receipt = dict(status='complete', session_id=self.sid, agent='codex', source_host=a, generation=transfer['generation'], bundle_id='abc')
            ack = self.api.accept_transfer(dest, 'codex', self.sid, b, transfer, receipt)
            commit = self.api.complete_transfer(source, 'codex', self.sid, a, ack)
            self.api.activate_transfer(dest, 'codex', self.sid, b, commit)
            return transfer, receipt
        old, receipt = move(self.source, self.target, 'a', 'b')
        move(self.target, self.source, 'b', 'a')
        with self.assertRaisesRegex(ValueError, 'generation|stale'):
            self.api.accept_transfer(self.target, 'codex', self.sid, 'b', old, receipt)


if __name__ == '__main__':
    unittest.main()

class NativeAgentTests(unittest.TestCase):
    def setUp(self):
        from devcontainer_setup.project_sync.state import Project
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = Project(self.root, 'https://github.com/test/project', 'github.com/test/project', 'a' * 24)
        self.state = self.root / 'state'
        self.sid = str(uuid.uuid4())
        self.api = importlib.import_module('devcontainer_setup.project_sync.agents')

    def fake_server(self):
        path = self.root / 'server.py'
        path.write_text('''import json, os, pathlib, sys
sid = os.environ['FIXTURE_ID']
for line in sys.stdin:
    request = json.loads(line)
    with open(os.environ['FIXTURE_LOG'], 'a') as log: log.write(line)
    method, params = request['method'], request.get('params', {})
    if 'id' not in request: continue
    if method == 'initialize': result = {}
    elif method in ('thread/start', 'thread/resume'):
        result = {'thread': {'id': params.get('threadId', sid), 'cwd': params['cwd']}, 'runtimeWorkspaceRoots': params['runtimeWorkspaceRoots']}
    elif method == 'thread/inject_items': result = {}
    elif method in ('thread/turns/list', 'thread/items/list'):
        page = params.get('cursor', 'first')
        result = {'data': [{'id': method + page}], 'nextCursor': 'second' if page == 'first' else None}
    else: raise RuntimeError(method)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
''')
        env = dict(os.environ, FIXTURE_ID=self.sid, FIXTURE_LOG=str(self.root / 'requests.jsonl'), CODEX_HOME=str(self.root / 'codex'))
        return (sys.executable, str(path)), env

    def test_native_history_drains_every_page_and_selects_destination_runtime(self):
        executable, env = self.fake_server()
        value = self.api.native_history(self.project, self.sid, env=env, executable=executable)
        self.assertEqual(value['session_id'], self.sid)
        self.assertEqual(len(value['turns']), 2)
        self.assertEqual(len(value['items']), 2)
        requests = [json.loads(line) for line in (self.root / 'requests.jsonl').read_text().splitlines()]
        resume = next(row['params'] for row in requests if row['method'] == 'thread/resume')
        self.assertEqual(resume['runtimeWorkspaceRoots'], [str(self.root)])
        self.assertEqual(resume['cwd'], str(self.root))
        self.assertFalse(resume['config']['memories.use_memories'])

    def test_native_history_rebuilds_ancestor_indexes_before_fork(self):
        executable, env = self.fake_server()
        parent = str(uuid.uuid4())
        directory = Path(env['CODEX_HOME']) / 'sessions'
        directory.mkdir(parents=True)
        for sid, ancestor in ((parent, None), (self.sid, parent)):
            metadata = dict(id=sid, cwd=str(self.root), cli_version='0.164.0', source='cli')
            if ancestor:
                metadata['history_base'] = dict(thread_id=ancestor, end_byte_offset=0, end_ordinal_exclusive=0)
            (directory / ('rollout-' + sid + '.jsonl')).write_text(json.dumps(dict(type='session_meta', payload=metadata)) + '\n')
        self.api.native_history(self.project, self.sid, env=env, executable=executable)
        requests = [json.loads(line) for line in (self.root / 'requests.jsonl').read_text().splitlines()]
        resumed = [row['params']['threadId'] for row in requests if row['method'] == 'thread/resume']
        self.assertEqual(resumed, [parent, self.sid])

    def test_inject_current_context_does_not_start_model_turn(self):
        executable, env = self.fake_server()
        self.api.inject_project_context(self.project, self.sid, env=env, executable=executable)
        requests = [json.loads(line) for line in (self.root / 'requests.jsonl').read_text().splitlines()]
        methods = [row['method'] for row in requests]
        self.assertIn('thread/resume', methods)
        self.assertIn('thread/inject_items', methods)
        self.assertNotIn('turn/start', methods)

    def test_codex_bootstrap_injects_notes_without_starting_model_turn(self):
        notes = self.root / '.scratch/memory/CONTEXT.md'
        notes.parent.mkdir(parents=True)
        notes.write_text('Scoped bootstrap marker')
        executable, env = self.fake_server()
        sid = self.api.create_codex_session(self.project, state=self.state, host='a', env=env, executable=executable)
        self.assertEqual(sid, self.sid)
        requests = [json.loads(line) for line in (self.root / 'requests.jsonl').read_text().splitlines()]
        self.assertNotIn('turn/start', [row['method'] for row in requests])
        injection = next(row for row in requests if row['method'] == 'thread/inject_items')
        self.assertIn('Scoped bootstrap marker', json.dumps(injection))
        from devcontainer_setup.project_sync.ownership import owner_status
        self.assertEqual(owner_status(self.state, 'codex', self.sid)['status'], 'idle')

    def test_managed_claude_launch_holds_lease_normal_environment_and_project_notes(self):
        notes = self.root / '.scratch/memory/project.md'
        notes.parent.mkdir(parents=True)
        notes.write_text('Project scoped launch marker')
        child = self.root / 'child.py'
        child.write_text('''import json,os,pathlib,sys
from devcontainer_setup.project_sync.ownership import managed_lease
blocked=False
try:
    with managed_lease(pathlib.Path(os.environ['FIXTURE_STATE']), 'claude', os.environ['FIXTURE_ID'], 'a'): pass
except ValueError: blocked=True
pathlib.Path(os.environ['FIXTURE_OUTPUT']).write_text(json.dumps({'args':sys.argv[1:],'cwd':os.getcwd(),'profile':os.environ['CLAUDE_CONFIG_DIR'],'blocked':blocked,'memory':os.environ.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY')}))
''')
        env = dict(os.environ, PYTHONPATH=str(Path.cwd()), FIXTURE_STATE=str(self.state), FIXTURE_ID=self.sid, FIXTURE_OUTPUT=str(self.root / 'result.json'), CLAUDE_CONFIG_DIR=str(self.root / 'normal-claude'))
        code = self.api.launch(self.project, 'claude', self.sid, state=self.state, host='a', new=True, env=env, executable=(sys.executable, str(child)))
        self.assertEqual(code, 0)
        result = json.loads((self.root / 'result.json').read_text())
        self.assertTrue(result['blocked'])
        self.assertEqual(result['cwd'], str(self.root))
        self.assertEqual(result['profile'], env['CLAUDE_CONFIG_DIR'])
        self.assertIn('--session-id', result['args'])
        self.assertIn(self.sid, result['args'])
        self.assertIn('Project scoped launch marker', ' '.join(result['args']))
        self.assertEqual(result['memory'], '1')
        self.assertEqual(result['args'][result['args'].index('--system-prompt-snapshot') + 1], 'off')

    def test_native_writer_survives_wrapper_and_guardian_crash_without_takeover(self):
        child = self.root / 'idle_native.py'
        child.write_text("import os,sys\nfor fd in range(3,256):\n try: os.close(fd)\n except OSError: pass\nprint(str(os.getpid())+' '+str(os.getppid()),flush=True)\nsys.stdin.readline()\n")
        launcher = self.root / 'launcher.py'
        launcher.write_text("from pathlib import Path\nfrom devcontainer_setup.project_sync.agents import launch\nfrom devcontainer_setup.project_sync.state import Project\nimport os,sys\np=Path(sys.argv[1]); launch(Project(p,'https://github.com/test/project','github.com/test/project','a'*24),'claude',sys.argv[2],state=p/'state',host='a',new=True,executable=(sys.executable,str(p/'idle_native.py')))\n")
        wrapper = subprocess.Popen([sys.executable, str(launcher), str(self.root), self.sid], env=dict(os.environ, PYTHONPATH=str(Path.cwd()), CLAUDE_CONFIG_DIR=str(self.root / 'native-home')), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child_pid = guardian_pid = None
        try:
            ready, _, _ = select.select([wrapper.stdout], [], [], 10)
            self.assertTrue(ready, 'Native process did not reach readiness barrier')
            line = wrapper.stdout.readline()
            self.assertTrue(line, 'Native launch exited before readiness')
            child_pid, guardian_pid = map(int, line.split())
            wrapper.kill(); wrapper.wait(timeout=5)
            from devcontainer_setup.project_sync.ownership import recover_local, managed_lease
            with self.assertRaises(ValueError):
                recover_local(self.state, 'claude', self.sid, 'a', native_paths=[])
            with self.assertRaises(ValueError):
                with managed_lease(self.state, 'claude', self.sid, 'a'):
                    pass
            self.assertNotEqual(guardian_pid, wrapper.pid)
            os.kill(guardian_pid, signal.SIGKILL)
            with self.assertRaises(ValueError):
                recover_local(self.state, 'claude', self.sid, 'a', native_paths=[])
            os.kill(child_pid, 0)
        finally:
            if wrapper.poll() is None:
                wrapper.kill(); wrapper.wait(timeout=5)
            for pid in (child_pid, guardian_pid):
                if pid and pid != wrapper.pid:
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
            wrapper.stdin.close(); wrapper.stdout.close(); wrapper.stderr.close()

    def test_native_launch_failure_releases_guardian_and_records_idle(self):
        runner = 'from pathlib import Path; from devcontainer_setup.project_sync.agents import launch; from devcontainer_setup.project_sync.state import Project; import sys; p=Path(sys.argv[1]); sys.exit(launch(Project(p,"https://github.com/test/project","github.com/test/project","a"*24),"claude",sys.argv[2],state=p/"state",host="a",new=True,executable=("/nonexistent/native-agent-fixture",)))'
        failed = subprocess.run([sys.executable, '-c', runner, str(self.root), self.sid], capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(Path.cwd()), CLAUDE_CONFIG_DIR=str(self.root / 'native')))
        self.assertNotEqual(failed.returncode, 0)
        from devcontainer_setup.project_sync.ownership import owner_status, managed_lease
        receipt = owner_status(self.state, 'claude', self.sid)
        self.assertEqual(receipt['status'], 'idle')
        self.assertIsNone(receipt['native_pid'])
        with managed_lease(self.state, 'claude', self.sid, 'a'):
            pass

    def test_guardian_keeps_native_process_on_foreground_terminal(self):
        child = self.root / 'terminal.py'
        child.write_text('import json,os,sys\nline=sys.stdin.readline().strip()\nprint("TERMINAL_RESULT "+json.dumps(dict(tty=os.isatty(0) and os.isatty(1), foreground=os.tcgetpgrp(0)==os.getpgrp(), line=line)),flush=True)\n')
        pid, master = pty.fork()
        if pid == 0:
            try:
                code = self.api.launch(self.project, 'claude', self.sid, state=self.state, host='a', new=True, env=dict(os.environ, PYTHONPATH=str(Path.cwd()), CLAUDE_CONFIG_DIR=str(self.root / 'native')), executable=(sys.executable, str(child)))
            except BaseException:
                os._exit(99)
            os._exit(code)
        try:
            os.write(master, b'foreground-input-marker\n')
            output = b''
            while b'TERMINAL_RESULT ' not in output or b'foreground-input-marker"}' not in output:
                ready, _, _ = select.select([master], [], [], 10)
                self.assertTrue(ready, 'Native terminal did not respond')
                output += os.read(master, 65536)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            row = next(line for line in output.decode().splitlines() if line.startswith('TERMINAL_RESULT '))
            result = json.loads(row.removeprefix('TERMINAL_RESULT '))
            self.assertTrue(result['tty'])
            self.assertTrue(result['foreground'])
            self.assertEqual(result['line'], 'foreground-input-marker')
        finally:
            os.close(master)
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
