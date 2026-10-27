"""Codespace lifecycle core: timed reads, ledgered start requests, one start routine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .state import _parent
from .transport import TokenTimeout, _NoRedirect, _deleted_from_list, _name, github_token

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
RETRY_GAP = 30
UNREADABLE_NOTICE = 3
TERMINAL = ('Failed', 'Unavailable', 'Deleted', 'Archived')
OWNER_RID = 'o-'  # the owner's planned restart, outside the budget (D6)
# How the token errors begin; status matches on them to name the fix.
NO_TOKEN = 'GitHub authentication unavailable'
TOKEN_TIMEOUT = 'Reading the GitHub token'
BAD_REQUEST = 'The GitHub request could not be built'
_BASE = 'https://api.github.com/user/codespaces/'


@dataclass(frozen=True)
class Reading:
    at: float
    ok: bool
    state: str | None
    pending: bool | None
    last_used_at: float | None
    http: int = 0


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


def owner_rid(now):
    return OWNER_RID + new_rid(now)[2:]


class Ledger:
    """Append-only JSONL of start intents and results; never holds a token or a body."""

    def __init__(self, path, now=time.time):
        self.path, self._now = Path(path), now

    def rows(self):
        try:
            with _parent(self.path, private=True) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, 'rb') as source:
                    data = source.read()
        except FileNotFoundError:
            return []
        rows = []
        for line in data.splitlines():
            try:
                row = json.loads(line.decode())
            except ValueError:  # includes UnicodeDecodeError
                continue
            # A malformed row is dropped, so one corrupt line cannot make the gate raise.
            if (isinstance(row, dict) and row.get('kind') in ('intent', 'result')
                    and isinstance(row.get('rid'), str) and isinstance(row.get('at'), (int, float))
                    and not isinstance(row.get('at'), bool)):
                rows.append(row)
        return rows

    def append(self, kind, rid, **fields):
        if kind not in ('intent', 'result'):
            raise ValueError('Ledger kind must be intent or result.')
        row = dict(fields, kind=kind, rid=rid, at=self._now())
        with _parent(self.path, create=True, private=True) as (parent, name):
            fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
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
    """Direct-HTTPS client for four routes: GET state/list, POST start, POST stop."""

    def __init__(self, name, gh_user=None, *, upstream=None, token_reader=None):
        self.name, self._gh_user = _name(name), gh_user
        self._upstream = upstream or _request
        self._reader = token_reader or (lambda: github_token(
            dict(os.environ, **({'DEVCONTAINER_GH_USER': gh_user} if gh_user else {}))))
        self._token = None

    @property
    def token_usable(self):
        """A token in hand, from a fresh read or the last good one (D10); the gate asks (spec 6.4 step 4)."""
        return bool(self._token)

    def _fetch(self):
        # Read per call. The last good token only covers a reader that fails later
        # (spec D10); a reader that never worked raises its fixed-text error.
        try:
            self._token = self._reader() or self._token
        except ValueError as error:
            if not self._token:
                if self._gh_user:
                    raise self._no_token(timed_out=isinstance(error, TokenTimeout)) from None
                raise
        if not self._token:
            raise self._no_token()
        return self._token

    def _no_token(self, timed_out=False):
        # A selected account (the keeper's) can only be fixed through gh itself. A read
        # that timed out usually waits on a locked keychain; logging in again will not help.
        if self._gh_user and timed_out:
            return ValueError(f'{TOKEN_TIMEOUT} for {self._gh_user} timed out; unlock the Mac and its login '
                              'keychain, then retry.')
        if self._gh_user:
            return ValueError(f'{NO_TOKEN} for {self._gh_user}; run gh auth status --hostname github.com, '
                              'and gh auth login --hostname github.com if it is missing.')
        return ValueError(NO_TOKEN + '; set GH_TOKEN or DEVCONTAINER_GH_USER.')

    def _call(self, method, suffix='', *, inventory=False, token=None):
        token = token or self._fetch()
        try:
            url = _BASE.rstrip('/') + '?per_page=100' if inventory else _BASE + self.name + suffix
            return self._upstream(method, url, token)
        except (URLError, OSError, TimeoutError, http.client.HTTPException):
            return 0, b''
        except ValueError:
            # http.client's text can echo a header value, the token included.
            raise ValueError(BAD_REQUEST + '; check the token with gh auth status --hostname github.com.') from None

    def read(self):
        at = time.time()
        status, body = self._call('GET')
        bad = Reading(at, False, None, None, None, status)
        if status == 404 and self.confirm_deleted() is True:
            return Reading(at, True, 'Deleted', None, None, 404)
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
                       parse_time(obj.get('last_used_at')), status)

    def confirm_deleted(self) -> bool | None:
        status, body = self._call('GET', inventory=True, token=self._token)
        return _deleted_from_list(status, body, self.name)

    def post_start(self):
        return self._call('POST', '/start')[0]

    def post_stop(self):
        return self._call('POST', '/stop')[0]


def classify(*, last_up_at, first_down_at, anchors, owner_stop_at):
    """Spec 6.4 step 3: 'OWNER', 'LIFETIME' or 'OTHER' for a stop first seen at first_down_at.

    The possible stop interval is [last_up_at, first_down_at], so a stop seen late
    (Mac asleep) still counts when it could have fallen inside the lifetime window.
    LIFETIME when that interval fits any candidate. OWNER uses the earliest candidate,
    so an owner stop between two plausible starts is still the owner's. An empty list
    behaves as no anchor.
    """
    earliest = min(anchors) if anchors else None
    if (owner_stop_at is not None and (earliest is None or owner_stop_at >= earliest)
            and owner_stop_at <= first_down_at):
        return 'OWNER'
    start_at = first_down_at if last_up_at is None else last_up_at
    for anchor_at in anchors:
        if start_at <= anchor_at + LIFETIME + WINDOW and first_down_at >= anchor_at + LIFETIME - WINDOW:
            return 'LIFETIME'
    return 'OTHER'


def _budgeted(row):
    return row.get('kind') == 'intent' and not row['rid'].startswith(OWNER_RID)


def restart_rids(rows, now):
    """Distinct keeper rids with an intent row inside RESTART_PERIOD, oldest first; never an owner's o- rid."""
    found = []
    for row in rows:
        if _budgeted(row) and now - row['at'] <= RESTART_PERIOD and row['rid'] not in found:
            found.append(row['rid'])
    return found


def gate(*, arming, rows, now):
    """Spec 6.4 step 4: None when a restart is allowed, else the reason it is not."""
    from .arming import format_time  # arming imports this module
    if not arming.get('armed'):
        return 'not armed'
    hold = arming.get('hold_until')
    if hold is not None and hold > now:
        return 'on hold until ' + format_time(hold)
    if len(restart_rids(rows, now)) >= MAX_RESTARTS:
        return f'{MAX_RESTARTS} restarts already in 24 hours'
    intents = [row['at'] for row in rows if _budgeted(row)]
    if intents and now - max(intents) < MIN_GAP:
        return 'last restart was less than 30 minutes ago'
    return None


def start(client, *, ledger=None, rid=None, posts=0, gate=None, now=time.time, sleep=time.sleep, say=None,
          tick=None):
    """Spec 6.4 step 5: POST start only from Shutdown with nothing pending.

    Waits for a clean Shutdown (20 min), posts, re-posts once if still Shutdown 2 min
    after an accepted post, retries 409/5xx within MAX_POSTS (a 409 retry needs a clean
    Shutdown reading RETRY_GAP later), then waits for Available (45 min).
    `posts` counts the rid's earlier POSTs, so a continued rid keeps MAX_POSTS. `gate`, when
    given, is asked before each POST; a reason it returns ends the start as 'refused'.
    """
    rid = rid or new_rid(now())
    posted_at, accepted_at, last_state, status, reason = None, None, None, None, None
    reposted, waiting, extended, unreadable, not_before = False, False, False, 0, 0
    deadline = now() + SHUTDOWN_WAIT

    def result(outcome):
        return {'outcome': outcome, 'posted_at': posted_at, 'posts': posts,
                'last_state': last_state, 'status': status, 'reason': reason}

    while True:
        reading = client.read()
        if tick:
            tick()
        t = now()
        if not reading.ok:
            unreadable += 1
            if unreadable == UNREADABLE_NOTICE and say:
                say('Codespace state unreadable; check GitHub authentication and network')
        else:
            unreadable = 0
            if reading.state != last_state and say:
                say('Codespace state: ' + str(reading.state))
            last_state = reading.state
            if last_state == 'Available':
                return result('available')
            if last_state in TERMINAL:
                return result('failed')
            clean = last_state == 'Shutdown' and reading.pending is not True
            if not waiting:
                if last_state not in ('Shutdown', 'ShuttingDown') and not extended:
                    # Another start may be under way; allow it the full Available wait once.
                    extended, deadline = True, max(deadline, t + AVAILABLE_WAIT)
            elif clean and not reposted and posts < MAX_POSTS and t - accepted_at >= REPOST_AFTER:
                reposted, waiting, deadline = True, False, t + SHUTDOWN_WAIT
            if not waiting and clean and t >= not_before:
                if posts >= MAX_POSTS:
                    return result('failed')
                # Asked at the POST itself: a hold, disarm or owner stop during the wait counts.
                reason = gate() if gate else None
                if reason is not None:
                    return result('refused')
                if ledger:
                    try:
                        ledger.append('intent', rid, post=posts + 1, state=last_state)
                    except OSError:
                        # Spec 6.4 step 4: no start request without its intent row.
                        reason = f'the restart ledger {ledger.path} cannot be written; check its directory, then ' \
                                 'task work:status'
                        return result('refused')
                posts += 1
                status = client.post_start()
                if ledger:
                    ledger.append('result', rid, post=posts, status=status)
                t = now()
                if 200 <= status < 300:
                    posted_at = accepted_at = t
                    waiting, deadline = True, t + AVAILABLE_WAIT
                elif status == 409 or status >= 500 or status == 0:
                    if posts >= MAX_POSTS:
                        return result('failed')
                    deadline = t + SHUTDOWN_WAIT
                    if status == 409:
                        not_before = t + RETRY_GAP
                else:
                    return result('failed')
        if t >= deadline:
            return result('timeout')
        sleep(min(POLL, max(0, deadline - t)))
