"""Native stdio client and project-note context without replacing profiles."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time

MEMORY_CONFIG = {'features.memories': False, 'memories.use_memories': False, 'memories.generate_memories': False}


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
