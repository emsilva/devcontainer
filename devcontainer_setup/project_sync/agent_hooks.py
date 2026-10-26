"""Install Herdr resume hooks on a Codespace so a restart can resume agents."""
from __future__ import annotations

import json
from pathlib import Path
import time

from .state import atomic_write, read_json


AGENTS = ('claude', 'codex', 'opencode')
RECEIPT = 'herdr-hooks.json'
# Remote `integration install` and `integration status` bounds. The SSH call
# is a little longer so a hung command hits these, not the transport timeout.
INSTALL_TIMEOUT = 60
STATUS_TIMEOUT = 30
_CALL_MARGIN = 15
_STATES = ('not installed', 'outdated', 'current')

# Runs as `python3 -I -B -c` on the Codespace. Stdin is a JSON list of agent
# names. Herdr is found the same way as workspace._HERDR_PROBE.
REMOTE = """\
import json, os, shutil, subprocess, sys
ALLOWED = ("claude", "codex", "opencode")

def main():
    herdr = shutil.which("herdr", path=os.pathsep.join([os.environ.get("PATH", ""), os.path.expanduser("~/.local/bin")]))
    if not herdr:
        print(json.dumps({"herdr": False}))
        return
    try:
        names = json.load(sys.stdin)
    except (ValueError, TypeError):
        names = None
    if not isinstance(names, list) or any(not isinstance(name, str) or name not in ALLOWED for name in names):
        sys.stderr.write("refused\\n")
        raise SystemExit(2)
    installed = {}
    for name in names:
        result = subprocess.run(
            [herdr, "integration", "install", name],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        installed[name] = result.returncode
    status = subprocess.run(
        [herdr, "integration", "status"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    print(json.dumps({"herdr": True, "installed": installed, "status": status.stdout.decode("utf-8", "replace")}))

main()
"""


def _python_timeout(count: int) -> int:
    return STATUS_TIMEOUT + INSTALL_TIMEOUT * count + _CALL_MARGIN


def _state(rest: str) -> str:
    rest = rest.lstrip()
    for name in _STATES:
        if rest == name or rest.startswith(name + ' ') or rest.startswith(name + '('):
            return name
    return 'unknown'


def parse(text: str) -> dict[str, str]:
    """Map claude, codex and opencode to current, outdated, not installed, or unknown."""
    found = {}
    for line in text.splitlines():
        for agent in AGENTS:
            prefix = agent + ':'
            if line.startswith(prefix) and agent not in found:
                found[agent] = _state(line[len(prefix):])
                break
    return {agent: found.get(agent, 'unknown') for agent in AGENTS}


def _reason(error: BaseException) -> str:
    # ValueError text is already scrubbed by transport; anything else by type, so a path cannot leak.
    if isinstance(error, ValueError):
        text = str(error).split(';')[0].strip()
    else:
        text = type(error).__name__
    text = ' '.join(text.split())
    return (text or type(error).__name__)[:160]


def _stamp(now) -> float:
    try:
        value = now()
    except (ValueError, OSError):
        return 0
    return value if isinstance(value, (int, float)) else 0


def _store(state, receipt: dict) -> None:
    atomic_write(Path(state) / RECEIPT, json.dumps(receipt, sort_keys=True).encode() + b'\n')


def _failed(codespace, checked_at, reason: str) -> dict:
    return {
        'codespace': codespace,
        'checked_at': checked_at,
        'hooks': {agent: 'unknown' for agent in AGENTS},
        'installed': [],
        'error': reason,
    }


def _message(receipt: dict) -> str:
    codespace = receipt.get('codespace', '')
    error = receipt.get('error')
    if error:
        return ('Could not check Herdr resume hooks on ' + codespace + ' (' + error
                + '); its agents may not come back after a restart. Run task work:cs again.')
    hooks = receipt.get('hooks') if isinstance(receipt.get('hooks'), dict) else {}
    pending = [agent for agent in AGENTS if hooks.get(agent) != 'current']
    if pending:
        detail = ', '.join(agent + ' ' + str(hooks.get(agent, 'unknown')) for agent in pending)
        return ('Herdr resume hooks are not current on ' + codespace + ' (' + detail
                + '); its agents may not come back after a restart. Run task work:cs again.')
    installed = [agent for agent in receipt.get('installed') or [] if isinstance(agent, str)]
    if installed:
        return ('Installed Herdr resume hooks on ' + codespace + ' for ' + ', '.join(installed)
                + '; its agents can now come back after a restart.')
    return 'Herdr resume hooks are current on ' + codespace + ' (claude, codex, opencode).'


def _emit(receipt: dict) -> None:
    try:
        print(_message(receipt), flush=True)
    except OSError:
        pass


def _record_error(codespace, state, error: BaseException, now) -> dict:
    receipt = _failed(codespace, _stamp(now), _reason(error))
    try:
        _store(state, receipt)
    except (ValueError, OSError):
        pass
    _emit(receipt)
    return receipt


def _load(raw) -> dict:
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError('invalid hooks response')
    try:
        value = json.loads(bytes(raw).decode())
    except (UnicodeDecodeError, ValueError):
        raise ValueError('invalid hooks response') from None
    if not isinstance(value, dict):
        raise ValueError('invalid hooks response')
    return value


def _call(connection, names: list[str]) -> dict:
    raw = connection.python(REMOTE, json.dumps(names).encode(), _python_timeout(len(names)))
    value = _load(raw)
    if value.get('herdr') is False:
        raise ValueError('Herdr is not installed there')
    if value.get('herdr') is not True or not isinstance(value.get('status'), str):
        raise ValueError('invalid hooks response')
    return value


def _check(connection, codespace, checked_at) -> dict:
    first = _call(connection, [])
    hooks = parse(first['status'])
    missing = [agent for agent in AGENTS if hooks.get(agent) != 'current']
    installed: list[str] = []
    if missing:
        second = _call(connection, missing)
        hooks = parse(second['status'])
        installed = list(missing)
    return {
        'codespace': codespace,
        'checked_at': checked_at,
        'hooks': {agent: hooks.get(agent, 'unknown') for agent in AGENTS},
        'installed': installed,
    }


def ensure(connection, codespace, state, *, now=time.time) -> dict:
    """Check hooks, install any that are not current, write the receipt, and print one line.

    ValueError and OSError from the connection or the response become a receipt error.
    """
    try:
        checked_at = now()
        if not isinstance(checked_at, (int, float)):
            raise ValueError('invalid hooks clock')
        receipt = _check(connection, codespace, checked_at)
        _store(state, receipt)
    except (ValueError, OSError) as error:
        return _record_error(codespace, state, error, now)
    _emit(receipt)
    return receipt


def ensure_on(codespace, state) -> dict:
    """Open a fresh Codespace connection and ensure hooks. A connect failure is a receipt error."""
    from .transport import CodespaceConnection
    try:
        connection = CodespaceConnection(codespace, state=Path(state))
        entered = connection.__enter__()
    except (ValueError, OSError) as error:
        return _record_error(codespace, state, error, time.time)
    try:
        return ensure(entered, codespace, state)
    finally:
        # The check already printed its line. Closing must not fail work:cs or print another.
        try:
            connection.__exit__(None, None, None)
        except (ValueError, OSError):
            pass


def read(state) -> dict | None:
    """The last hooks receipt, or None when it is missing or cannot be read. Offline."""
    try:
        value = read_json(Path(state) / RECEIPT)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def status_line(receipt, codespace) -> str | None:
    """One offline line for work:status, or None when there is no Codespace to name."""
    if not isinstance(codespace, str) or not codespace:
        return None
    not_checked = 'Herdr hooks: not checked on ' + codespace + '; task work:cs checks them'
    if not isinstance(receipt, dict) or receipt.get('codespace') != codespace:
        return not_checked
    error = receipt.get('error')
    if isinstance(error, str) and error:
        return ('Herdr hooks: check failed on ' + codespace + ' (' + error
                + '); task work:cs checks again')
    hooks = receipt.get('hooks') if isinstance(receipt.get('hooks'), dict) else {}
    pending = [agent + ' ' + str(hooks.get(agent, 'unknown'))
               for agent in AGENTS if hooks.get(agent) != 'current']
    if pending:
        return ('Herdr hooks: ' + ', '.join(pending) + ' on ' + codespace
                + '; task work:cs installs them')
    checked_at = receipt.get('checked_at')
    if not isinstance(checked_at, (int, float)):
        return not_checked
    from .arming import format_time
    try:
        checked = format_time(checked_at)
    except (TypeError, ValueError, OSError, OverflowError):
        return not_checked
    return ('Herdr hooks: claude, codex, opencode current on ' + codespace
            + ' (checked ' + checked + ')')
