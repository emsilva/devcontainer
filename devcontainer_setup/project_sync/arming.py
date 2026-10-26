"""Per-project recovery arming: the owner's consent, account, ports and hold."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

from . import lifecycle
from .state import atomic_write, read_json
from .transport import clean_env

ARMING = 'arming.json'
SCOPE_ERROR = 'The selected GitHub account lacks the codespace scope; run gh auth refresh -h github.com -s codespace.'


def read(state: Path) -> dict:
    try:
        value = read_json(Path(state) / ARMING)
    except FileNotFoundError:
        return {'schema': 1, 'armed': False}
    if value.get('schema') != 1:
        raise ValueError('Unsupported arming.json schema; leave it for review')
    return value


def write(state: Path, value: dict) -> None:
    atomic_write(Path(state) / ARMING, json.dumps(value, sort_keys=True).encode() + b'\n')


def strip_jsonc(text: str) -> str:
    """Drop comments outside strings and trailing commas, so json.loads can parse the rest."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith('//', i):
            while i < n and text[i] != '\n':
                i += 1
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r',(\s*[\]}])', r'\1', ''.join(out))


def forward_ports(text: str) -> list[int]:
    """Return forwardPorts minus 2222, as ints, sorted and deduplicated."""
    config = json.loads(strip_jsonc(text))
    ports = config.get('forwardPorts', [])
    return sorted({p for p in ports if isinstance(p, int) and p != 2222})


def default_ports(root: Path) -> list[int]:
    path = Path(root) / '.devcontainer/devcontainer.json'
    try:
        text = path.read_text()
    except OSError:
        return []
    try:
        return forward_ports(text)
    except (ValueError, AttributeError, TypeError):
        raise ValueError('Cannot read forwardPorts from .devcontainer/devcontainer.json; fix the file or pass --port') from None


def resolve_ports(defaults: list[int], add: list[int], remove: list[int]) -> list[int]:
    return sorted((set(defaults) | set(add)) - set(remove))


def consent_text(project_name: str, codespace: str, billable_owner: str) -> str:
    return (f'Arm {project_name}: keep {codespace} running while this Mac is up, and start it again '
            f"after GitHub's 12-hour stop (at most {lifecycle.MAX_RESTARTS} times in "
            f'{lifecycle.RESTART_PERIOD // 3600} hours, {lifecycle.MIN_GAP // 60} minutes apart). '
            f'Usage is billed to {billable_owner}. Type arm to confirm.')


def account(env: Mapping) -> str:
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if env.get(name):
            raise ValueError(f'Unset {name} before arming; consent must name a real gh account.')
    user = env.get('DEVCONTAINER_GH_USER', '')
    clean = clean_env(env)
    args = ['gh', 'auth', 'token', '--hostname', 'github.com'] + (['--user', user] if user else [])
    try:
        token = subprocess.run(args, env=clean, capture_output=True, text=True, timeout=20)
        if token.returncode or not token.stdout.strip():
            raise ValueError('GitHub authentication unavailable; run gh auth login or set DEVCONTAINER_GH_USER.')
        reply = subprocess.run(['gh', 'api', '-i', '--hostname', 'github.com', 'user'],
                               env=dict(clean, GH_TOKEN=token.stdout.strip()),
                               capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise ValueError('The GitHub CLI (gh) is not installed or not on PATH.') from None
    except subprocess.TimeoutExpired:
        raise ValueError('GitHub authentication timed out; check GitHub CLI authentication and retry.') from None
    head, _, body = reply.stdout.replace('\r\n', '\n').partition('\n\n')
    if reply.returncode:
        raise ValueError('GitHub rejected the account check; the token may be expired or the network down. Retry after gh auth status.')
    scopes = re.search(r'^x-oauth-scopes:(.*)$', head, re.I | re.M)
    if not scopes or 'codespace' not in {x.strip() for x in scopes[1].split(',')}:
        raise ValueError(SCOPE_ERROR)
    if user:
        return user
    try:
        login = json.loads(body).get('login')
    except ValueError:
        login = None
    if not isinstance(login, str) or not login:
        raise ValueError('GitHub did not name the active account; set DEVCONTAINER_GH_USER.')
    return login


def arm(state: Path, *, gh_user: str, codespace: str, consent: str, billable_owner: str,
        ports: list[int], now: float) -> dict:
    """Record fresh consent; only its hash is kept, and any old hold or owner stop is cleared."""
    value = {'schema': 1, 'armed': True, 'armed_at': now, 'gh_user': gh_user, 'codespace': codespace,
             'consent_sha256': hashlib.sha256(consent.encode()).hexdigest(), 'consented_at': now,
             'billable_owner': billable_owner, 'ports': sorted(ports), 'hold_until': None}
    write(state, value)
    return value


def format_time(when: float) -> str:
    """A time for people: the Mac's local zone, then UTC."""
    local = datetime.fromtimestamp(when).strftime('%Y-%m-%d %H:%M')
    utc = datetime.fromtimestamp(when, timezone.utc).strftime('%Y-%m-%d %H:%M')
    return f'{local} (UTC {utc})'


def hold_until(value: str, now: float) -> float | None:
    if value.strip().lower() == 'off':
        return None
    match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', value.strip())
    if not match:
        raise ValueError('Hold takes a local time like 09:30, or off')
    at = datetime.fromtimestamp(now).replace(hour=int(match[1]), minute=int(match[2]), second=0, microsecond=0)
    if at.timestamp() <= now:
        at = (at + timedelta(days=1)).replace(hour=int(match[1]), minute=int(match[2]))
    return at.timestamp()


def record_owner_stop(state: Path, now: float) -> None:
    write(state, dict(read(state), owner_stop_at=now))


def clear_pending_restore(state: Path, rid: str) -> None:
    """Drop the planned restart `rid` the keeper consumed; a newer one stays."""
    value = read(state)
    pending = value.get('pending_restore')
    if isinstance(pending, dict) and pending.get('rid') == rid:
        write(state, {key: item for key, item in value.items() if key != 'pending_restore'})
