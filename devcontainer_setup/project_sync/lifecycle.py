"""Codespace lifecycle core: timed reads, ledgered start requests, one start routine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from .transport import _NoRedirect, _name, github_token

LIFETIME = 12 * 3600
WINDOW = 15 * 60
DOWN_READINGS = 3
MAX_RESTARTS = 2
RESTART_PERIOD = 24 * 3600
MIN_GAP = 30 * 60
SHUTDOWN_WAIT = 20 * 60
REPOST_AFTER = 120
MAX_POSTS = 3
AVAILABLE_WAIT = 45 * 60
POLL = 10
TERMINAL = ('Failed', 'Unavailable', 'Deleted', 'Archived')
_BASE = 'https://api.github.com/user/codespaces/'


@dataclass(frozen=True)
class Reading:
    at: float
    ok: bool
    state: str | None
    pending: bool | None
    last_used_at: float | None


def parse_time(value):
    """UTC epoch seconds from an API timestamp (Z or +-HH:MM); None if unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def new_rid(now):
    return 'r-' + time.strftime('%H%M%S', time.gmtime(now)) + 'Z'


class Ledger:
    """Append-only JSONL of start intents and results; never holds a token or a body."""

    def __init__(self, path):
        self.path = Path(path)

    def rows(self):
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def append(self, kind, rid, **fields):
        if kind not in ('intent', 'result'):
            raise ValueError('Ledger kind must be intent or result.')
        row = dict(fields, kind=kind, rid=rid, at=time.time())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(row, sort_keys=True) + '\n').encode())
            os.fsync(fd)
        finally:
            os.close(fd)


def _request(method, url, token):
    request = Request(url, method=method, data=b'' if method == 'POST' else None, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            return response.status, response.read(2 * 1024 * 1024)
    except HTTPError as error:
        return error.code, b'{}'


class LifecycleAPI:
    """Direct-HTTPS client for exactly three routes: GET, POST start, POST stop."""

    def __init__(self, name, gh_user=None, *, upstream=None, token_reader=None):
        self.name = _name(name)
        self._upstream = upstream or _request
        self._reader = token_reader or (lambda: github_token(
            dict(os.environ, **({'DEVCONTAINER_GH_USER': gh_user} if gh_user else {}))))
        self._token = None

    def _fetch(self):
        # Read per call; the last good token only covers a failing reader (spec D10).
        try:
            self._token = self._reader() or self._token
        except Exception:
            pass
        return self._token

    def _call(self, method, suffix=''):
        token = self._fetch()
        if not token:
            return 0, b''
        try:
            return self._upstream(method, _BASE + self.name + suffix, token)
        except Exception:
            return 0, b''

    def read(self):
        at = time.time()
        bad = Reading(at, False, None, None, None)
        status, body = self._call('GET')
        if status != 200:
            return bad
        try:
            obj = json.loads(body)
        except ValueError:
            return bad
        if not isinstance(obj, dict) or obj.get('name') != self.name or not isinstance(obj.get('state'), str):
            return bad
        pending = obj.get('pending_operation')
        return Reading(at, True, obj['state'], pending if isinstance(pending, bool) else None,
                       parse_time(obj.get('last_used_at')))

    def post_start(self):
        return self._call('POST', '/start')[0]

    def post_stop(self):
        return self._call('POST', '/stop')[0]


def start(client, *, ledger=None, rid=None, now=time.time, sleep=time.sleep, say=None, tick=None):
    """Spec 6.4 step 5: POST start only from Shutdown with nothing pending.

    Waits for a clean Shutdown (20 min), posts, re-posts once if still Shutdown 2 min
    after an accepted post, retries 409/5xx within MAX_POSTS, then waits for Available (45 min).
    """
    rid = rid or new_rid(now())
    posts, posted_at, accepted_at, last_state, status = 0, None, None, None, None
    reposted, phase = False, 'post'
    deadline = now() + SHUTDOWN_WAIT

    def result(outcome):
        return {'outcome': outcome, 'posted_at': posted_at, 'posts': posts,
                'last_state': last_state, 'status': status}

    while True:
        reading = client.read()
        if tick:
            tick()
        t = now()
        if reading.ok:
            if reading.state != last_state and say:
                say('Codespace state: ' + str(reading.state))
            last_state = reading.state
            if last_state == 'Available':
                return result('available')
            if last_state in TERMINAL:
                return result('failed')
            down = last_state in ('Shutdown', 'ShuttingDown')
            if phase == 'post' and not down:
                # Someone else's start is under way; do not add a request.
                phase, deadline = 'wait', t + AVAILABLE_WAIT
            elif phase == 'wait' and last_state == 'Shutdown' and accepted_at is not None \
                    and not reposted and t - accepted_at >= REPOST_AFTER and reading.pending is not True:
                reposted, phase, deadline = True, 'post', t + SHUTDOWN_WAIT
            if phase == 'post' and last_state == 'Shutdown' and reading.pending is not True:
                if posts >= MAX_POSTS:
                    return result('failed')
                posts += 1
                if ledger:
                    ledger.append('intent', rid, post=posts, state=last_state)
                status = client.post_start()
                if ledger:
                    ledger.append('result', rid, post=posts, status=status)
                t = now()
                if 200 <= status < 300:
                    posted_at = accepted_at = t
                    phase, deadline = 'wait', t + AVAILABLE_WAIT
                elif status == 409 or status >= 500 or status == 0:
                    if posts >= MAX_POSTS:
                        return result('failed')
                    deadline = t + SHUTDOWN_WAIT
                else:
                    return result('failed')
        if t >= deadline:
            return result('timeout')
        sleep(min(POLL, max(0, deadline - t)))
