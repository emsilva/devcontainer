"""Per-project recovery arming: the owner's consent, account, ports and hold."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import subprocess

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


def default_ports(root: Path) -> list[int]:
    try:
        text = (Path(root) / '.devcontainer/devcontainer.json').read_text()
    except OSError:
        return []
    try:
        config = json.loads(text)
    except ValueError:
        config = json.loads(re.sub(r'^\s*//.*$', '', text, flags=re.M))
    return sorted({p for p in config.get('forwardPorts', []) if isinstance(p, int) and p != 2222})


def resolve_ports(defaults: list[int], add: list[int], remove: list[int]) -> list[int]:
    return sorted((set(defaults) | set(add)) - set(remove))


def consent_text(project_name: str, codespace: str, billable_owner: str) -> str:
    return (f'Arm {project_name}: keep {codespace} running while this Mac is up, and start it again '
            "after GitHub's 12-hour stop (at most 2 times in 24 hours, 30 minutes apart). "
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
    except subprocess.TimeoutExpired:
        raise ValueError('GitHub authentication timed out; check GitHub CLI authentication and retry.') from None
    head, _, body = reply.stdout.replace('\r\n', '\n').partition('\n\n')
    scopes = re.search(r'^x-oauth-scopes:(.*)$', head, re.I | re.M)
    if reply.returncode or not scopes:
        raise ValueError(SCOPE_ERROR)
    if 'codespace' not in {s.strip() for s in scopes[1].split(',')}:
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


def arm(state: Path, *, gh_user: str, consent: str, billable_owner: str, ports: list[int], now: float) -> dict:
    """Record arming; only the hash of the consent text is kept."""
    value = dict(read(state), schema=1, armed=True, armed_at=now, gh_user=gh_user,
                 consent_sha256=hashlib.sha256(consent.encode()).hexdigest(), consented_at=now,
                 billable_owner=billable_owner, ports=sorted(ports))
    value.setdefault('hold_until', None)
    write(state, value)
    return value


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
