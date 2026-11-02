"""Fresh, foreground sign-ins through the paired worker's existing link watcher."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time

from .links import _defer_signals, watcher_live
from .state import atomic_write, locked, read_json


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


NOT_CONNECTED = ('Sign-in needs this project connected to its Codespace. Run task work:cs, '
                 'then task work:signin:all.')
NOT_LISTENING = ("The Codespace is not reaching this Mac's link helper yet. Run task work:cs again, "
                 'then task work:signin:all.')
_HEARTBEAT = '''
from pathlib import Path
import time
try:
    print(time.time() - (Path.home() / '.local/state/devcontainer-links/mac-listening').stat().st_mtime)
except OSError:
    print(-1)
'''


def ready(state: Path, *, wait: float = 30) -> str | None:
    deadline = time.monotonic() + wait
    while True:
        connected = watcher_live(state)
        if connected:
            try:
                command = shlex.join(['python3', '-I', '-B', '-c', _HEARTBEAT])
                answer = subprocess.run(_ssh(state, command), capture_output=True, text=True,
                                        timeout=max(0.1, min(5, deadline - time.monotonic())))
                if answer.returncode == 0 and 0 <= float(answer.stdout) <= 15 and watcher_live(state):
                    return None
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return NOT_LISTENING if connected else NOT_CONNECTED
        time.sleep(min(1, remaining))


class _SigninStop(BaseException):
    def __init__(self, number):
        self.number = number


def _end_login(child, number=signal.SIGTERM):
    if child.poll() is not None:
        child.wait()
        return
    try:
        child.send_signal(number)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def login(state: Path, tool: Tool) -> int:
    child = None
    try:
        # Establish ownership before a deferred cancellation can be delivered.
        with _defer_signals():
            child = subprocess.Popen(_ssh(state, shlex.join(tool.login), terminal=True), start_new_session=False)
        code = child.wait(timeout=tool.deadline)
        return code if code >= 0 else 128 - code
    except OSError:
        if child is not None:
            with _defer_signals():
                _end_login(child)
        return 1
    except subprocess.TimeoutExpired:
        with _defer_signals():
            _end_login(child)
        return 124
    except BaseException as error:
        number = error.number if isinstance(error, _SigninStop) else signal.SIGINT
        if child is not None:
            with _defer_signals():
                _end_login(child, number)
        raise


def _line(tool, result, *, after=False):
    state, identity = result['state'], result.get('identity')
    if state == SIGNED_IN:
        text = 'signed in' if after else 'already signed in'
        if tool.key == 'claude' and identity:
            text += ' as ' + identity
    elif after:
        text = 'not signed in (timed out or callback port busy: see the Mac notice)'
    else:
        text = {OTHER_METHOD: 'signed in another way; left as is', UNKNOWN: 'could not check sign-in; skipped',
                ABSENT: 'not installed; skipped'}[state]
    print(tool.label + ': ' + text, flush=True)


def _summary(results):
    signed, missing = [], []
    for tool in TOOLS:
        state = results.get(tool.key, {}).get('state', UNKNOWN)
        if state in (SIGNED_IN, OTHER_METHOD):
            signed.append(tool.label)
        elif state != ABSENT:
            missing.append(tool.label)
    print('Signed in: ' + (', '.join(signed) or 'none') + '.', flush=True)
    if missing:
        print('Not signed in: ' + ', '.join(missing) + '. Retry with task work:signin:all.', flush=True)
    return bool(missing)


def run(project, state: Path, codespace: str) -> int:
    state = Path(state)
    # Catch contention only at acquisition, not a BlockingIOError from a tool.
    lock = locked(state / 'signins.lock')
    try:
        lock.__enter__()
    except BlockingIOError:
        print('Another sign-in is already running for this project.')
        return 1
    previous, results, current, cancelled = {}, {}, None, 0

    def interrupted(number, _):
        raise _SigninStop(number)

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, interrupted)
        try:
            # A command may have read the binding just before a connect changed it.
            try:
                binding = read_json(state / 'scratch.json')
            except FileNotFoundError:
                binding = {}
            if binding.get('codespace') != codespace:
                print(NOT_CONNECTED)
                return 1
            refusal = ready(state)
            if refusal:
                print(refusal)
                return 1
            results = probe(state, [tool.key for tool in TOOLS])
            for tool in TOOLS:
                current = tool
                if results[tool.key]['state'] != SIGNED_OUT:
                    _line(tool, results[tool.key])
                    continue
                # A previous approval can take ten minutes: recheck both heartbeats.
                refusal = ready(state)
                if refusal:
                    print(refusal)
                    break
                text = f'Signing in to {tool.label}. Approve the page that opens on this Mac.'
                if tool.key == 'grok':
                    text += ' Enter the code shown here.'
                print(text, flush=True)
                code = login(state, tool)
                if code in (128 + signal.SIGINT, 128 + signal.SIGTERM, 128 + signal.SIGHUP):
                    cancelled = code
                    break
                results[tool.key] = probe(state, [tool.key])[tool.key]
                _line(tool, results[tool.key], after=True)
        except (KeyboardInterrupt, _SigninStop) as error:
            cancelled = 128 + (error.number if isinstance(error, _SigninStop) else signal.SIGINT)
        cancellation = 'Sign-in cancelled' + (' during ' + current.label if current else '') + '.'
        reported = False
        try:
            # Finish the receipt and summary even when cancellation arrives during fsync.
            with _defer_signals():
                if cancelled:
                    print(cancellation, flush=True)
                    reported = True
                write_record(state, codespace, results, time.time())
                missing = _summary(results)
        except (KeyboardInterrupt, _SigninStop) as error:
            cancelled = 128 + (error.number if isinstance(error, _SigninStop) else signal.SIGINT)
            if not reported:
                print(cancellation, flush=True)
        return cancelled or int(missing)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        lock.__exit__(None, None, None)
