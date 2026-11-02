"""Fresh, foreground sign-ins through the paired worker's existing link watcher."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import re
import shlex
import subprocess

from .state import atomic_write


@dataclass(frozen=True)
class Tool:
    key: str
    label: str
    binary: str
    status: tuple[str, ...]
    login: tuple[str, ...]
    deadline: int


TOOLS = (
    Tool('plaud', 'Plaud', 'plaud', ('plaud', 'me'), ('plaud', 'login'), 150),
    Tool('codex', 'Codex', 'codex', ('codex', 'login', 'status'), ('codex', 'login'), 600),
    Tool('claude', 'Claude', 'claude', ('claude', 'auth', 'status', '--json'),
         ('claude', 'auth', 'login', '--claudeai'), 600),
    Tool('opencode', 'OpenCode', 'opencode', ('opencode', 'auth', 'list', '--format', 'json'),
         ('opencode', 'auth', 'login', 'openai', '--method', 'chatgpt-token-sharing'), 600),
    Tool('grok', 'Grok', 'grok', ('grok', 'models'), ('grok', 'login', '--device-auth'), 600),
)
SIGNED_IN, SIGNED_OUT, OTHER_METHOD, UNKNOWN, ABSENT = (
    'signed-in', 'signed-out', 'other-method', 'unknown', 'absent')


def _classify(key: str, code: int, out: str, err: str) -> tuple[str, str | None]:
    # Self-contained: only this function's source is shipped to the Codespace.
    import json
    if key == 'plaud':
        return ({0: 'signed-in', 2: 'signed-out'}.get(code, 'unknown'), None)
    if key == 'codex':
        if code == 0:
            return ('signed-in' if 'Logged in using ChatGPT' in err.splitlines() else 'other-method', None)
        if code == 1 and 'Not logged in' in err.splitlines():
            return ('signed-out', None)
    if key in ('claude', 'opencode'):
        try:
            value = json.loads(out)
        except (ValueError, TypeError):
            return ('unknown', None)
        if key == 'claude' and isinstance(value, dict):
            if value.get('loggedIn') is False:
                return ('signed-out', None)
            if value.get('loggedIn') is True:
                if value.get('authMethod') == 'claude.ai':
                    email = value.get('email')
                    # Do not send arbitrary status text back as identity.
                    if (not isinstance(email, str) or len(email) > 254 or email.count('@') != 1
                            or any(c.isspace() or ord(c) < 32 for c in email) or '/' in email):
                        email = None
                    return ('signed-in', email)
                return ('other-method', None)
        if key == 'opencode' and code == 0 and isinstance(value, list):
            found = any(isinstance(item, dict) and item.get('id') == 'openai' for item in value)
            return ('signed-in' if found else 'signed-out', None)
    if key == 'grok':
        if 'You are logged in with grok.com.' in out:
            return ('signed-in', None)
        if 'You are not authenticated.' in out:
            return ('signed-out', None)
    return ('unknown', None)


REMOTE_PROBE = inspect.getsource(_classify) + '''
import json, shutil, subprocess, sys
results = {}
for key, argv in json.loads(sys.argv[1]).items():
    state, identity = 'unknown', None
    if shutil.which(argv[0]) is None:
        state = 'absent'
    else:
        try:
            answer = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            state, identity = _classify(key, answer.returncode, answer.stdout, answer.stderr)
        except (subprocess.TimeoutExpired, OSError, UnicodeError):
            pass
    results[key] = {'state': state, 'identity': identity}
print(json.dumps(results))
'''


def _result(key, value):
    value = value if isinstance(value, dict) else {}
    state = value.get('state')
    if state not in (SIGNED_IN, SIGNED_OUT, OTHER_METHOD, UNKNOWN, ABSENT):
        state = UNKNOWN
    identity = value.get('identity')
    if (key != 'claude' or state != SIGNED_IN or not isinstance(identity, str)
            or len(identity) > 254 or re.fullmatch(r"[\w.!#$%&'*+?^`{|}~=-]+@[\w.-]+", identity) is None):
        identity = None
    return {'state': state, 'identity': identity}


def _ssh(state, command, *, terminal=False):
    return ['ssh', '-F', str(Path(state) / 'ssh-config'), '-t' if terminal else '-T',
            'project-sync', 'bash', '-lc', shlex.quote(command)]


def probe(state: Path, keys: Sequence[str]) -> dict[str, dict]:
    tools = {tool.key: tool.status for tool in TOOLS if tool.key in keys}
    command = shlex.join(['python3', '-I', '-B', '-c', REMOTE_PROBE, json.dumps(tools)])
    try:
        answer = subprocess.run(_ssh(state, command), capture_output=True, text=True, timeout=120)
        value = json.loads(answer.stdout) if answer.returncode == 0 else {}
        if not isinstance(value, dict):
            value = {}
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        value = {}
    return {key: _result(key, value.get(key)) for key in keys}


def write_record(state: Path, codespace: str, results: dict[str, dict], now: float) -> None:
    tools = {}
    for tool in TOOLS:
        if tool.key not in results:
            continue
        result = _result(tool.key, results[tool.key])
        row = {'state': result['state'], 'checked_at': now}
        if result['identity'] is not None:
            row['identity'] = result['identity']
        tools[tool.key] = row
    value = {'schema': 1, 'codespace': codespace, 'updated_at': now, 'tools': tools}
    atomic_write(Path(state) / 'signins.json', json.dumps(value).encode() + b'\n')
