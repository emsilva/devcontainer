"""Open queued Codespace links on the Mac; own callback forwards by fact."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

from . import notify
from .state import atomic_write, locked, process_identity, read_json

_LOG = logging.getLogger('project-sync-links')
LOOPBACK = {'localhost', '127.0.0.1', '::1'}
SELF_SHOWING = {'claude', 'codex', 'opencode', 'gh', 'az'}
REMEDIES = {
    'plaud': "Stop the Mac's own `plaud login` or `plaud-mcp http`, then sign in again. Plaud has no device code",
    'codex': '`codex login --device-auth`',
    'opencode': 'Pick the device-code method, or sign in again (a new random port)',
    'claude': 'Paste the code from the page into Claude\'s "Paste code here" field',
    'az': '`az login --use-device-code`',
    'anything else': 'Name the holder; nothing else',
}


@dataclass(frozen=True)
class Request:
    id: str
    url: str
    host: str
    port: int | None
    kind: str
    program: str
    tty: str | None
    created_at: float


def _url(value):
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 8192
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError('Invalid link')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or (port is not None and not 1024 <= port <= 65535)):
            raise ValueError
        # Keep hosts safe to show in terminal messages and notifications.
        if not re.fullmatch(r'[A-Za-z0-9.:-]+', parsed.hostname):
            raise ValueError
    except ValueError:
        raise ValueError('Invalid link address or port') from None
    return parsed


def parse_request(raw: bytes) -> Request:
    try:
        if len(raw) > 65536:
            raise ValueError
        value = json.loads(raw)
        required = {'schema', 'id', 'url', 'created_at', 'program', 'pid', 'start', 'tty'}
        if (not isinstance(value, dict) or not required <= value.keys()
                or type(value['schema']) is not int or value['schema'] != 1
                or not isinstance(value['id'], str) or not re.fullmatch(r'[0-9]+-[0-9]+-[0-9a-f]{4}', value['id'])
                or type(value['pid']) is not int or value['pid'] <= 0
                or not isinstance(value['start'], str) or not value['start'].isdigit()
                or not isinstance(value['program'], str) or not re.fullmatch(r'[A-Za-z0-9_.+-]{1,128}', value['program'])
                or type(value['created_at']) not in (float, int) or not math.isfinite(value['created_at'])
                or (value['tty'] is not None and (not isinstance(value['tty'], str)
                    or not re.fullmatch(r'/dev/pts/[0-9]+', value['tty'])))):
            raise ValueError
        parsed = _url(value['url'])
        kind, port = ('loopback', parsed.port) if parsed.hostname in LOOPBACK else ('link', None)
        redirects = parse_qs(parsed.query, max_num_fields=256).get('redirect_uri', [])
        # Decode query values once only. A twice-encoded redirect is just a page link.
        if redirects and urlsplit(redirects[0]).scheme.lower() in ('http', 'https'):
            redirect = _url(redirects[0])
            if redirect.hostname in LOOPBACK:
                port = redirect.port
                kind = 'signin' if port is not None else 'link'
        return Request(value['id'], value['url'], parsed.hostname, port, kind,
                       value['program'], value['tty'], float(value['created_at']))
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise ValueError('Invalid link request') from None


def deadline(request: Request) -> float | None:
    if request.kind == 'loopback':
        return None
    return request.created_at + ((120 if request.program == 'plaud' else 600) if request.kind == 'signin' else 30)


def check_port(port: int) -> tuple[bool, str | None]:
    try:
        result = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        # Without the wildcard-listener check, a successful bind is not proof.
        return False, 'listener could not be checked'
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    for row in rows:
        if len(row) > 1 and row[1].isdigit():
            return False, f'{row[0]} (pid {row[1]})'
    if result.returncode not in (0, 1) or (result.returncode == 0 and rows):
        return False, 'unknown listener'
    for family, address in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((address, port))
        except OSError as error:
            if family == socket.AF_INET6 and error.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                continue
            return False, 'unknown listener'
    return True, None


def _forward_argv(state, port):
    return ['ssh', '-F', str(state / 'ssh-config'), '-o', 'ClearAllForwardings=no',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-N', '-L',
            f'127.0.0.1:{port}:127.0.0.1:{port}', 'project-sync']


def _signal_group(pid, number):
    try:
        os.killpg(pid, number)
    except (ProcessLookupError, PermissionError):
        pass


class Forwards:
    def __init__(self, state: Path, owner: str):
        self.state, self.owner = Path(state), owner
        self.owner_pid = os.getpid()
        self.owner_identity = process_identity(self.owner_pid)
        self.children, self.requests = {}, {}

    def _read(self):
        try:
            return read_json(self.state / 'links-forwards.json')['forwards']
        except FileNotFoundError:
            return {}

    def _record(self, port, value):
        with locked(self.state / 'links-forwards.lock', blocking=True):
            records = self._read()
            if value is None:
                old = records.get(str(port), {})
                if old.get('owner') != self.owner:
                    return
                records.pop(str(port), None)
            else:
                old = records.get(str(port), {})
                if (old.get('owner') != self.owner and old.get('process_identity')
                        and process_identity(old.get('pid')) == old['process_identity']):
                    raise ValueError('Callback forward belongs to another owner')
                records[str(port)] = value
            atomic_write(self.state / 'links-forwards.json', json.dumps({'forwards': records}).encode())

    def start(self, port: int) -> None:
        if port in self.active():
            return
        child = subprocess.Popen(_forward_argv(self.state, port), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.children[port] = child
        try:
            identity = process_identity(child.pid)
            if not identity:
                raise ValueError('Callback forward exited')
            self._record(port, dict(pid=child.pid, process_identity=identity, owner=self.owner,
                                    owner_pid=self.owner_pid, owner_identity=self.owner_identity))
            until = time.monotonic() + 5
            while child.poll() is None:
                proof = subprocess.run(['lsof', '-a', '-p', str(child.pid), '-nP',
                                        f'-iTCP:{port}', '-sTCP:LISTEN'],
                                       capture_output=True, text=True, timeout=5)
                if proof.returncode == 0 and proof.stdout.strip():
                    return
                if time.monotonic() >= until:
                    break
                time.sleep(.1)
            raise ValueError('Callback forward did not listen')
        except BaseException:
            self.stop(port)
            raise

    def stop(self, port: int) -> None:
        child = self.children.get(port)
        if child is not None:
            if child.poll() is None:
                _signal_group(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _signal_group(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
            else:
                child.wait(timeout=5)
            self.children.pop(port, None)
        self.requests.pop(port, None)
        self._record(port, None)

    def active(self) -> set[int]:
        for port, child in list(self.children.items()):
            if child.poll() is not None:
                context = self.requests.get(port)
                self.stop(port)
                if context:
                    request, project, notifier = context
                    notifier(project, request.program, request.host, f'Port {port}: callback forward ended')
                    _LOG.info('Callback forward ended: %s %s port %s', request.program, request.host, port)
        return set(self.children)

    def watch(self, request, project, notifier):
        self.requests[request.port] = (request, project, notifier)

    def tend(self, connection, *, now=None):
        now = time.time() if now is None else now
        for port in self.active():
            request, _, _ = self.requests[port]
            limit = deadline(request)
            if (limit is not None and now >= limit) or not remote_listening(connection, port):
                self.stop(port)

    def end_left_behind(self) -> list[int]:
        ended = []
        with locked(self.state / 'links-forwards.lock', blocking=True):
            records = self._read()
            for key, record in list(records.items()):
                port = int(key)
                owner = process_identity(record.get('owner_pid'))
                if owner is not None and owner == record.get('owner_identity'):
                    continue
                pid, identity = record.get('pid'), record.get('process_identity')
                marked = ' '.join(_forward_argv(self.state, port))
                if not identity or marked not in identity or process_identity(pid) != identity:
                    records.pop(key)
                    continue
                for number in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(pid, number)
                    except ProcessLookupError:
                        pass
                    until = time.monotonic() + 5
                    while process_identity(pid) == identity and time.monotonic() < until:
                        time.sleep(.1)
                    if process_identity(pid) != identity:
                        ended.append(port)
                        records.pop(key)
                        break
                else:
                    raise ValueError('A leftover callback forward did not stop')
            atomic_write(self.state / 'links-forwards.json', json.dumps({'forwards': records}).encode())
        return ended


_REMOTE_LISTENING = '''import json,sys
port=json.load(sys.stdin)['port']
listening=False
for path in ('/proc/net/tcp','/proc/net/tcp6'):
 try:
  with open(path) as stream:
   for line in stream:
    fields=line.split()
    if len(fields)>3 and fields[3]=='0A' and int(fields[1].split(':')[1],16)==port:
     listening=True
 except FileNotFoundError: pass
print(int(listening))
'''


def remote_listening(connection, port: int) -> bool:
    return connection.python(_REMOTE_LISTENING, json.dumps({'port': port}).encode(), timeout=10).strip() == b'1'


def open_url(url: str, run=subprocess.run) -> bool:
    try:
        return run(['/usr/bin/open', url], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def notify_link(project: str, program: str, host: str, detail: str = '') -> None:
    title, text = f'{project}: {program}', host + (f' — {detail}' if detail else '')
    try:
        notify.notify(title, text)
    except Exception:
        pass
    try:
        if subprocess.run(['herdr', 'status'], capture_output=True, timeout=5).returncode == 0:
            subprocess.run(['herdr', 'notification', 'show', title, '--body', text], capture_output=True, timeout=5)
    except Exception:
        pass


_REMOTE_TTY = '''import json,os,re,stat,sys
value=json.load(sys.stdin)
if re.fullmatch(r'/dev/pts/[0-9]+',value['tty']):
 fd=os.open(value['tty'],os.O_WRONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 try:
  if stat.S_ISCHR(os.fstat(fd).st_mode): os.write(fd,('\\r\\n'+value['text']+'\\r\\n').encode())
 finally: os.close(fd)
'''


def handle(request: Request, connection, forwards: Forwards, *, project: str, now=time.time,
           opener=open_url, notifier=notify_link) -> str:
    limit = deadline(request)
    if limit is not None and now() >= limit:
        return 'expired'
    if request.port is not None:
        if request.port not in forwards.active():
            free, holder = check_port(request.port)
            if not free:
                detail = f'Port {request.port} in use by {holder or "unknown listener"}'
                remedy = REMEDIES.get(request.program)
                if remedy:
                    detail += '. ' + remedy
                notifier(project, request.program, request.host, detail)
                if request.tty and request.program not in SELF_SHOWING:
                    try:
                        connection.python(_REMOTE_TTY, json.dumps(dict(tty=request.tty,
                            text=f'{request.program}: {request.host}: {detail}')).encode(), timeout=10)
                    except (OSError, ValueError):
                        pass
                _LOG.info('Busy: %s %s port %s', request.program, request.host, request.port)
                return 'busy'
            if not remote_listening(connection, request.port):
                notifier(project, request.program, request.host, f'Port {request.port} is no longer listening')
                return 'rejected'
            try:
                forwards.start(request.port)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                notifier(project, request.program, request.host, f'Port {request.port}: callback forward failed')
                return 'rejected'
        forwards.watch(request, project, notifier)
    if not opener(request.url):
        notifier(project, request.program, request.host, 'Browser could not open the link')
        if request.port is not None:
            forwards.stop(request.port)
        return 'rejected'
    notifier(project, request.program, request.host, f'Port {request.port}' if request.port else '')
    _LOG.info('Opened: %s %s port %s', request.program, request.host, request.port or 'none')
    return 'forwarded' if request.port is not None else 'opened'
