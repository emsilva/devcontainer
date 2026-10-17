"""Native launch and paginated history inspection without replacing profiles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import sys
import time

from .ownership import managed_lease, run_native_child
from .sessions import _codex_index, _metadata, _parent, _read, assert_quiescent, claude_namespace, native_home, session_uuid

MEMORY_CONFIG = {'features.memories': False, 'memories.use_memories': False, 'memories.generate_memories': False}


def project_context(project):
    directory = project.root / '.scratch/memory'
    text = ['Current project notes from .scratch/memory/ follow. Use only these project notes for this checkout.']
    total = 0
    if directory.is_symlink():
        raise ValueError('Project memory directory cannot be a symlink')
    if directory.exists():
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():
                raise ValueError('Project memory cannot contain symlinks')
            if not path.is_file():
                continue
            data = _read(path)
            total += len(data)
            if total > 256 * 1024:
                raise ValueError('Project notes exceed native launch context limit (256 KiB)')
            try:
                value = data.decode('utf-8')
            except UnicodeError as exc:
                raise ValueError('Project notes must contain UTF-8 text') from exc
            text.extend(['\n--- ' + path.relative_to(project.root).as_posix() + ' ---', value])
    return '\n'.join(text)


class CodexClient:
    """A dedicated stdio native process; owns and closes only its own child."""
    def __init__(self, project, *, env=None, executable=('codex',), timeout=60):
        self.timeout = timeout
        self.sequence = 0
        self.messages = queue.Queue()
        self.notifications = []
        self.errors = []
        command = [*executable, '-c', 'features.memories=false', '-c', 'memories.use_memories=false', '-c', 'memories.generate_memories=false', 'app-server', '--listen', 'stdio://']
        self.process = subprocess.Popen(command, cwd=project.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        def read(stream, sink):
            try:
                for line in stream:
                    sink(line)
            finally:
                if stream is self.process.stdout:
                    self.messages.put(None)
        self.readers = [threading.Thread(target=read, args=(self.process.stdout, self.messages.put), daemon=True), threading.Thread(target=read, args=(self.process.stderr, self._error), daemon=True)]
        for thread in self.readers:
            thread.start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'devcontainer-project-state', 'version': '1'}, 'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def _error(self, line):
        self.errors.append(line)
        del self.errors[:-50]

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def call(self, method, params):
        self.sequence += 1
        self.send(dict(id=self.sequence, method=method, params=params))
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                line = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise ValueError('Native Codex RPC timed out: ' + method) from exc
            if line is None:
                raise ValueError('Native Codex process exited during ' + method)
            message = json.loads(line)
            if message.get('id') == self.sequence and 'method' not in message:
                if 'error' in message:
                    raise ValueError('Native Codex ' + method + ': ' + json.dumps(message['error']))
                return message['result']
            if 'id' in message and 'method' in message:
                self.send(dict(id=message['id'], error=dict(code=-32601, message='Interactive request unsupported during history inspection')))
            else:
                self.notifications.append(message)
                del self.notifications[:-100]

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _runtime(project):
    return dict(cwd=str(project.root), runtimeWorkspaceRoots=[str(project.root)], config=MEMORY_CONFIG.copy())


def _history(client, session_id):
    result = dict(session_id=session_id)
    for name in ('turns', 'items'):
        rows, seen, cursor, pages = [], set(), None, 0
        while True:
            params = dict(threadId=session_id, limit=100, sortDirection='asc')
            if name == 'turns':
                params['itemsView'] = 'full'
            if cursor is not None:
                params['cursor'] = cursor
            page = client.call('thread/' + name + '/list', params)
            if not isinstance(page.get('data'), list):
                raise ValueError('Invalid native history page')
            rows.extend(page['data'])
            pages += 1
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen or pages >= 10000:
                raise ValueError('Native history pagination stalled or exceeded limit')
            seen.add(cursor)
        result[name] = rows
        result[name + '_pages'] = pages
    return result


def _codex_dependencies(home, session_id):
    index = _codex_index(Path(home))
    if session_id not in index:
        return []
    chain, seen, sid = [], set(), session_id
    while sid is not None:
        if sid in seen or sid not in index:
            raise ValueError('Missing or cyclic native ancestor dependency')
        seen.add(sid)
        chain.append((sid, index[sid]))
        _, metadata = _metadata(index[sid])
        sid = _parent(metadata)
    return list(reversed(chain))


def _resume_ancestors(client, project, dependencies):
    # Native fork pagination resolves ancestors through native indexes. Merely
    # copying their files does not populate those indexes on a fresh host.
    for sid, _ in dependencies[:-1]:
        response = client.call('thread/resume', dict(threadId=sid, **_runtime(project)))
        if response['thread']['id'] != sid:
            raise ValueError('Native ancestor reconciliation selected wrong session')


def native_history(project, session_id, *, env=None, executable=('codex',), timeout=60):
    """Rebuild native indexes by exact-ID resume and drain every history page.

    Caller must hold the managed lease or pending transfer custody. This native
    API may append runtime settings; call before export and before acceptance.
    """
    session_uuid(session_id)
    dependencies = _codex_dependencies(native_home('codex', env), session_id)
    assert_quiescent(path for _, path in dependencies)
    with CodexClient(project, env=env, executable=executable, timeout=timeout) as client:
        _resume_ancestors(client, project, dependencies)
        resumed = client.call('thread/resume', dict(threadId=session_id, **_runtime(project)))
        if resumed['thread']['id'] != session_id or resumed['thread']['cwd'] != str(project.root):
            raise ValueError('Native resume selected wrong session or runtime cwd')
        if resumed.get('runtimeWorkspaceRoots') != [str(project.root)]:
            raise ValueError('Native resume did not select destination workspace roots')
        return dict(_history(client, session_id), cwd=resumed['thread']['cwd'], runtime_workspace_roots=resumed['runtimeWorkspaceRoots'])


def create_codex_session(project, *, state, host, env=None, executable=('codex',)):
    """Materialize a same-ID native session without issuing a model request."""
    context = project_context(project)
    client = CodexClient(project, env=env, executable=executable)
    try:
        response = client.call('thread/start', dict(historyMode='paginated', **_runtime(project)))
        session_id = session_uuid(response['thread']['id'])
        with managed_lease(state, 'codex', session_id, host, new=True):
            try:
                client.call('thread/inject_items', dict(threadId=session_id, items=[dict(type='message', role='developer', content=[dict(type='input_text', text=context)])]))
            finally:
                # Flush native persistence and close the writer before releasing
                # ownership. Metadata-only thread/start is not durable.
                client.close()
        return session_id
    except BaseException:
        if client.process.poll() is None:
            client.close()
        raise


def inject_project_context(project, session_id, *, env=None, executable=('codex',)):
    """Append current project context through native history, without a model turn."""
    session_uuid(session_id)
    context = project_context(project)
    dependencies = _codex_dependencies(native_home('codex', env), session_id)
    assert_quiescent(path for _, path in dependencies)
    with CodexClient(project, env=env, executable=executable) as client:
        _resume_ancestors(client, project, dependencies)
        response = client.call('thread/resume', dict(threadId=session_id, **_runtime(project)))
        if response['thread']['id'] != session_id or response.get('runtimeWorkspaceRoots') != [str(project.root)]:
            raise ValueError('Native resume selected wrong identity or runtime roots')
        client.call('thread/inject_items', dict(threadId=session_id, items=[dict(type='message', role='developer', content=[dict(type='input_text', text=context)])]))


def native_paths(project, agent, session_id, home, *, state=None):
    """Exact session files for quiescence checks; snapshot validates closure."""
    session_uuid(session_id)
    home = Path(home)
    if agent == 'codex':
        index = _codex_index(home)
        if session_id not in index:
            raise ValueError('Exact native session UUID does not exist')
        return [path for _, path in _codex_dependencies(home, session_id)]
    if agent == 'claude':
        parent = home / 'projects' / claude_namespace(project.root) / (session_id + '.jsonl')
        if not parent.exists():
            raise ValueError('Exact native session UUID does not exist in this project')
        return [parent, *(path for path in parent.with_suffix('').rglob('*') if path.is_file()), *(home / 'tasks' / session_id).glob('*')]
    raise ValueError('Unsupported agent')


def _launch_owned(project, agent, session_id, *, state, host, home=None, new=False, extra_args=(), env=None, executable=None):
    """Run the ordinary interactive CLI under a durable process-held lease."""
    session_uuid(session_id)
    values = dict(os.environ if env is None else env)
    resolved_home = native_home(agent, values)
    if home is not None and Path(home) != resolved_home:
        raise ValueError('Explicit native home differs from normal process profile')
    if agent == 'codex' and new:
        raise ValueError('Create the Codex UUID with create_codex_session before launch')
    context = project_context(project)
    command = list(executable or (agent,))
    if agent == 'codex':
        command += ['resume', '--cd', str(project.root), '-c', 'features.memories=false', '-c', 'memories.use_memories=false', '-c', 'memories.generate_memories=false', session_id]
    elif agent == 'claude':
        command += ['--session-id' if new else '--resume', session_id, '--system-prompt-snapshot', 'off', '--append-system-prompt', context]
        values['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] = '1'
    else:
        raise ValueError('Unsupported agent')
    # Native session selectors, cwd switches and background launch break custody.
    forbidden = {'--resume', '--session-id', '--fork-session', '--continue', '--last', '--remote', '--bg', '--background', '--cd', '-C', '--system-prompt-snapshot'}
    if any(arg.split('=', 1)[0] in forbidden for arg in extra_args):
        raise ValueError('Extra arguments may not override managed session identity, cwd or process lifetime')
    command.extend(extra_args)
    with managed_lease(state, agent, session_id, host, new=new, _with_fd=True) as (receipt, lease_fd):
        if not new:
            assert_quiescent(native_paths(project, agent, session_id, resolved_home, state=state))
            if agent == 'codex':
                inject_project_context(project, session_id, env=values)
        elif agent == 'claude':
            candidate = resolved_home / 'projects' / claude_namespace(project.root) / (session_id + '.jsonl')
            if candidate.exists():
                raise ValueError('New Claude session UUID already exists')
        return run_native_child(state, agent, session_id, command, cwd=project.root, env=values, receipt=receipt, lease_fd=lease_fd)


def _guardian_main(descriptor):
    from .state import Project
    with os.fdopen(descriptor, 'rb') as source:
        payload = json.load(source)
    project_data = payload.pop('project')
    project_data['root'] = Path(project_data['root'])
    payload['state'] = Path(payload['state'])
    return _launch_owned(Project(**project_data), env=dict(os.environ), **payload)


def launch(project, agent, session_id, *, state, host, home=None, new=False, extra_args=(), env=None, executable=None):
    """Launch a foreground guardian which owns custody for the native lifetime.

    The calling wrapper never owns the native lease. Killing that wrapper cannot
    release it. Stdin/stdout/stderr and the terminal process group are inherited.
    """
    session_uuid(session_id)
    payload = dict(project=dict(root=str(project.root), remote=project.remote, identity=project.identity, project_id=project.project_id), agent=agent, session_id=session_id, state=str(state), host=host, home=str(home) if home is not None else None, new=new, extra_args=list(extra_args), executable=list(executable) if executable else None)
    read_fd, write_fd = os.pipe()
    module_root = Path(__file__).resolve().parents[len(__package__.split('.'))]
    code = 'import sys; sys.path.insert(0, ' + repr(str(module_root)) + '); from ' + __package__ + '.agents import _guardian_main; sys.exit(_guardian_main(int(sys.argv[1])))'
    try:
        guardian = subprocess.Popen([sys.executable, '-c', code, str(read_fd)], env=env, pass_fds=(read_fd,))
        os.close(read_fd)
        read_fd = -1
        with os.fdopen(write_fd, 'wb') as output:
            write_fd = -1
            output.write(json.dumps(payload).encode())
        while True:
            try:
                return guardian.wait()
            except KeyboardInterrupt:
                # The foreground native process receives terminal SIGINT too;
                # its independent guardian retains custody until it exits.
                continue
    finally:
        for descriptor in (read_fd, write_fd):
            if descriptor >= 0:
                os.close(descriptor)
