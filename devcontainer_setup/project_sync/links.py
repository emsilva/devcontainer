"""Open queued Codespace links on the Mac; own callback forwards by fact."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import errno
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import selectors
import shlex
import signal
import socket
import subprocess
import threading
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

    def stop(self, port: int, *, reason: str = 'stopped by owner') -> None:
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
        context = self.requests.pop(port, None)
        self._record(port, None)
        if context:
            request, _, _ = context
            _LOG.info('Callback forward ended: %s %s port %s (%s)',
                      request.program, request.host, port, reason)

    def active(self) -> set[int]:
        for port, child in list(self.children.items()):
            if child.poll() is not None:
                context = self.requests.get(port)
                self.stop(port, reason='callback forward exited')
                if context:
                    request, project, notifier = context
                    notifier(project, request.program, request.host, f'Port {port}: callback forward ended')
        return set(self.children)

    def watch(self, request, project, notifier):
        self.requests[request.port] = (request, project, notifier)

    def tend(self, connection, *, now=None):
        now = time.time() if now is None else now
        ports = self.active()
        # Local deadlines remain enforceable even while a remote probe fails.
        for port in ports:
            request, _, _ = self.requests[port]
            limit = deadline(request)
            if limit is not None and now >= limit:
                self.stop(port, reason='deadline')
        for port in ports:
            if port in self.children and not remote_listening(connection, port):
                self.stop(port, reason='remote stopped listening')

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
        _LOG.info('Expired: %s %s port %s', request.program, request.host, request.port or 'none')
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
                _LOG.info('Busy: %s %s port %s holder %s',
                          request.program, request.host, request.port, holder or 'unknown listener')
                return 'busy'
            if not remote_listening(connection, request.port):
                notifier(project, request.program, request.host, f'Port {request.port} is no longer listening')
                _LOG.info('Rejected: %s %s port %s (remote port is not listening)',
                          request.program, request.host, request.port)
                return 'rejected'
            try:
                forwards.start(request.port)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                notifier(project, request.program, request.host, f'Port {request.port}: callback forward failed')
                _LOG.info('Forward start failed: %s %s port %s', request.program, request.host, request.port)
                return 'rejected'
        forwards.watch(request, project, notifier)
    if not opener(request.url):
        notifier(project, request.program, request.host, 'Browser could not open the link')
        _LOG.info('Browser could not open: %s %s port %s',
                  request.program, request.host, request.port or 'none')
        if request.port is not None:
            forwards.stop(request.port, reason='browser could not open')
        return 'rejected'
    notifier(project, request.program, request.host, f'Port {request.port}' if request.port else '')
    _LOG.info('Opened: %s %s port %s', request.program, request.host, request.port or 'none')
    return 'forwarded' if request.port is not None else 'opened'


# stdin stays open for the worker's stream. At EOF the same source is a one-shot
# snapshot; SIGNINS_ONLY leaves ordinary links queued for the connected worker.
REMOTE_WATCHER = r'''import json,math,os,select,stat,sys,time
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

base=Path.home()/'.local/state/devcontainer-links'
requests=base/'requests'
claimed=base/'claimed'
for directory in (base,requests,claimed):
 directory.mkdir(mode=0o700,parents=True,exist_ok=True)
 if directory.is_symlink() or not directory.is_dir(): raise ValueError('Unsafe link state')
 directory.chmod(0o700)

def read(path):
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 try:
  info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_size>65536: raise ValueError('Invalid file')
  raw=os.read(fd,65537)
  if len(raw)>65536: raise ValueError('Invalid file')
  value=json.loads(raw)
  if (not isinstance(value,dict) or type(value.get('schema')) is not int or value['schema']!=1
      or not {'id','url','created_at','program','pid','start','tty'}<=value.keys()
      or type(value['created_at']) not in (int,float) or not math.isfinite(value['created_at'])):
   raise ValueError('Invalid request')
  return value
 finally: os.close(fd)

def signin(value):
 try:
  redirect=parse_qs(urlsplit(value['url']).query,max_num_fields=256).get('redirect_uri',[''])[0]
  parsed=urlsplit(redirect)
  return (parsed.scheme in ('http','https') and parsed.hostname in ('localhost','127.0.0.1','::1')
          and parsed.port is not None and 1024<=parsed.port<=65535)
 except (ValueError,TypeError,KeyError): return False

def discard(path):
 try:
  if stat.S_ISDIR(path.lstat().st_mode): path.rmdir()
  else: path.unlink()
 except OSError: pass

due=0
while True:
 if time.monotonic()>=due:
  fd=os.open(base/'mac-listening',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
  try:
   if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('Invalid heartbeat')
   os.utime(fd,None)
  finally: os.close(fd)
  due=time.monotonic()+5
 for source in requests.glob('*.json'):
  if globals().get('SIGNINS_ONLY',False):
   try:
    value=read(source)
    if time.time()-value['created_at']<=600 and not signin(value): continue
   except Exception: pass
  target=claimed/(str(time.time_ns())+'-'+str(os.getpid())+'-'+os.urandom(4).hex()+'.json')
  try: source.rename(target)
  except FileNotFoundError: continue
  try:
   value=read(target)
   if time.time()-value['created_at']<=600:
    print(json.dumps(value,separators=(',',':')),flush=True)
  except Exception:
   print('Skipped invalid link request',file=sys.stderr,flush=True)
  finally: discard(target)
 if select.select([sys.stdin],[],[],.2)[0] and not os.read(sys.stdin.fileno(),4096): break
'''


def watcher_live(state: Path) -> bool:
    try:
        value = read_json(Path(state) / 'links-watcher.json')
        age = time.time() - value['heartbeat_at']
        return bool(0 <= age <= 15 and value.get('process_identity')
                    and process_identity(value.get('pid')) == value['process_identity'])
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return False


def _stop_owned(forwards):
    failure = None
    for port in list(forwards.children):
        try:
            forwards.stop(port)
        except Exception as error:
            failure = error
    if failure is not None:
        raise ValueError('An owned callback forward could not be stopped') from None


@contextmanager
def _defer_signals():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    caught = []
    previous = {sig: signal.signal(sig, lambda number, _: caught.append(number))
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if caught:
            signal.raise_signal(caught[0])


class Watcher(threading.Thread):
    def __init__(self, connection, state: Path, project: str, logger, *, now=time.time):
        super().__init__(name='project-links', daemon=True)
        self.connection, self.state, self.project, self.logger = connection, Path(state), project, logger
        self.now = now
        self.owner = 'watcher-' + secrets.token_hex(16)
        self.identity = process_identity(os.getpid())
        self.forwards = Forwards(self.state, self.owner)
        self._halt = threading.Event()
        self._stream_lock = threading.Lock()
        self._stream = None

    def _heartbeat(self):
        value = dict(pid=os.getpid(), process_identity=self.identity,
                     owner=self.owner, heartbeat_at=self.now())
        atomic_write(self.state / 'links-watcher.json', json.dumps(value).encode())

    def _clear(self):
        try:
            if read_json(self.state / 'links-watcher.json').get('owner') == self.owner:
                (self.state / 'links-watcher.json').unlink()
        except FileNotFoundError:
            pass

    def _close_stream(self):
        with self._stream_lock:
            child, self._stream = self._stream, None
        if child is None:
            return
        try:
            child.stdin.close()
        except (OSError, ValueError):
            pass
        if child.poll() is None:
            _signal_group(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _signal_group(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        finally:
            child.stdout.close()
            child.stderr.close()

    def _serve(self):
        command = 'python3 -I -B -c ' + shlex.quote(REMOTE_WATCHER)
        child = subprocess.Popen(['ssh', '-F', str(self.state / 'ssh-config'), '-T', 'project-sync', command],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
        with self._stream_lock:
            self._stream = child
        buffers, dropping = {child.stdout: b'', child.stderr: b''}, set()
        beat = check = 0
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(child.stdout, selectors.EVENT_READ)
                ready.register(child.stderr, selectors.EVENT_READ)
                while not self._halt.is_set():
                    if self.now() >= beat:
                        self._heartbeat()
                        beat = self.now() + 5
                    if self.now() >= check:
                        self.forwards.tend(self.connection, now=self.now())
                        check = self.now() + 2
                    for key, _ in ready.select(.2):
                        stream = key.fileobj
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            if stream is child.stdout:
                                return
                            ready.unregister(stream)
                            continue
                        for part in chunk.splitlines(keepends=True):
                            if self._halt.is_set():
                                return
                            if stream not in dropping:
                                buffers[stream] += part
                            if len(buffers[stream]) > 65536:
                                buffers[stream] = b''
                                dropping.add(stream)
                                self.logger.info('Skipped oversized link request')
                            if not part.endswith(b'\n'):
                                continue
                            raw, buffers[stream] = buffers[stream], b''
                            if stream in dropping:
                                dropping.remove(stream)
                                continue
                            if stream is child.stderr:
                                self.logger.info('Link watcher reported a skipped request or stream error')
                                continue
                            try:
                                request = parse_request(raw)
                            except ValueError:
                                self.logger.info('Skipped invalid link request')
                                continue
                            handle(request, self.connection, self.forwards, project=self.project)
        finally:
            self._close_stream()

    def _pause(self, delay):
        if not self.forwards.active():
            return self._halt.wait(delay)
        until = self.now() + delay
        while self.now() < until:
            if self._halt.wait(min(2, until - self.now())):
                return True
            try:
                self.forwards.tend(self.connection, now=self.now())
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self.logger.info('Callback listener check unavailable; waiting to retry')
        return False

    def run(self):
        try:
            with locked(self.state / 'links-watcher.lock'):
                self.forwards.end_left_behind()
                backoff = 2
                while not self._halt.is_set() and self.connection.child.poll() is None:
                    try:
                        self._serve()
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        self.logger.info('Link watcher stream ended; waiting to reconnect')
                    finally:
                        self._clear()
                    if self._pause(backoff):
                        break
                    backoff = min(backoff * 2, 60)
        finally:
            self._close_stream()
            _stop_owned(self.forwards)
            self._clear()

    def stop(self) -> None:
        with _defer_signals():
            self._halt.set()
            with self._stream_lock:
                if self._stream is not None and self._stream.poll() is None:
                    _signal_group(self._stream.pid, signal.SIGTERM)
            if self.is_alive() and threading.current_thread() is not self:
                self.join(timeout=60)
                if self.is_alive():
                    raise ValueError('Link watcher has not stopped')
            _stop_owned(self.forwards)
            self._clear()


class _SigninStop(BaseException):
    def __init__(self, number):
        self.number = number


@contextmanager
def _signin_config(connection, state):
    """Borrow the shared config and restore it only while we still own its bytes.

    The worker uses this same lock for its config publication and alias refresh.
    A reconnect published during sign-in always wins over our saved version.
    """
    from .ssh_hosts import _read
    path = state / 'ssh-config'
    def current():
        try:
            return _read(path, private=True)
        except FileNotFoundError:
            return None
    published = False
    try:
        with _defer_signals(), locked(state / 'links-ssh-config.lock', blocking=True):
            allowed = not watcher_live(state)
            if allowed:
                before = current()
                try:
                    connection.ssh_config(path)
                finally:
                    # Establish custody even if publication is interrupted after replacement.
                    ours = current()
                    published = True
        yield allowed
    finally:
        if published:
            with _defer_signals(), locked(state / 'links-ssh-config.lock', blocking=True):
                if current() == ours:
                    if before is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, before)


def signin_main(root: Path, state: Path) -> int:
    from .scratch import _config
    from .transport import CodespaceConnection
    root, state = Path(root), Path(state)
    if watcher_live(state):
        print('The connected worker is already handling links.')
        return 1
    config = _config(root, state)
    forwards, previous = None, {}
    def interrupted(number, _):
        raise _SigninStop(number)
    try:
        with locked(state / 'links-signin.lock'):
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[number] = signal.signal(number, interrupted)
            with CodespaceConnection(config['codespace'], state=state) as connection:
                with _signin_config(connection, state) as allowed:
                    if not allowed:
                        print('The connected worker is already handling links.')
                        return 1
                    try:
                        forwards = Forwards(state, 'signin-' + secrets.token_hex(16))
                        forwards.end_left_behind()
                        while True:
                            batch = connection.python('SIGNINS_ONLY=True\n' + REMOTE_WATCHER, timeout=30)
                            for raw in batch.splitlines():
                                try:
                                    request = parse_request(raw)
                                except ValueError:
                                    _LOG.info('Skipped invalid sign-in request')
                                    continue
                                if request.kind == 'signin':
                                    handle(request, connection, forwards, project=root.name)
                            if not forwards.active():
                                return 0
                            time.sleep(2)
                            forwards.tend(connection)
                    finally:
                        with _defer_signals():
                            if forwards is not None:
                                _stop_owned(forwards)
    except _SigninStop as stopped:
        return 128 + stopped.number
    finally:
        try:
            with _defer_signals():
                if forwards is not None:
                    _stop_owned(forwards)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
