"""Owned SSH transport with an enforced read-only Codespaces API boundary."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

SUPPORTED_GH = '2.102.0'


def _name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{0,99}', name):
        raise ValueError('Invalid Codespace name.')
    return name


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _upstream(url, token):
    request = Request(url, headers={'Authorization': 'Bearer ' + token,
                      'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            return response.status, response.read(2 * 1024 * 1024)
    except HTTPError as error:
        return error.code, b'{}'


def clean_env(env=None):
    source = os.environ if env is None else env
    return {key: value for key, value in source.items()
            if key not in {'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN',
                           'GITHUB_ENTERPRISE_TOKEN', 'GITHUB_API_URL', 'GH_DEBUG',
                           'GH_HOST', 'GH_PROMPT_DISABLED'}
            and not key.startswith('GIT_')}


def github_token(env=None):
    source = os.environ if env is None else env
    if source.get('GH_TOKEN'):
        return source['GH_TOKEN']
    args = ['gh', 'auth', 'token', '--hostname', 'github.com']
    if source.get('DEVCONTAINER_GH_USER'):
        args += ['--user', source['DEVCONTAINER_GH_USER']]
    try:
        result = subprocess.run(args, env=clean_env(source), capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise ValueError('GitHub authentication timed out; check GitHub CLI authentication and retry.') from None
    if result.returncode or not result.stdout.strip():
        raise ValueError('GitHub authentication unavailable; set GH_TOKEN or DEVCONTAINER_GH_USER.')
    return result.stdout.strip()


class ReadOnlyAPI:
    """A capability-authenticated exact-route adapter, never a generic proxy.

    The injected upstream is an offline-test seam. Production's upstream is fixed
    to api.github.com, GET only, with redirects disabled. Bodies are not logged.
    """
    def __init__(self, codespace, token, *, upstream=None):
        self.name = _name(codespace)
        self._token = token
        self._upstream = upstream or _upstream
        self.child_token = secrets.token_urlsafe(32)
        self.denied = []
        self.server = None
        self.thread = None
        self.url = ''

    def __enter__(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            do_PUT = do_POST
            do_PATCH = do_POST
            do_DELETE = do_POST
            do_HEAD = do_POST
            do_OPTIONS = do_POST

            def respond(self):
                route = '/' + owner.child_token + '/user/codespaces/' + owner.name
                auth = self.headers.get('Authorization', '')
                valid_auth = not auth or any(hmac.compare_digest(auth, prefix + owner.child_token)
                                            for prefix in ('token ', 'Bearer '))
                allowed = self.path in (route, route + '?internal=true&refresh=true')
                if self.command != 'GET' or not valid_auth or not allowed:
                    # Bound this in-memory diagnostic; never retain token/body.
                    owner.denied[:] = (owner.denied + [(self.command, self.path.split('?')[0].replace(owner.child_token, '<capability>')[:200])])[-32:]
                    status, body = 403, b'{"message":"Read-only adapter denied request"}'
                else:
                    try:
                        status, body = owner._upstream('https://api.github.com' + self.path[len(owner.child_token) + 1:], owner._token)
                        if status == 200:
                            obj = json.loads(body)
                            if obj.get('name') != owner.name:
                                raise ValueError('Mismatched Codespace identity')
                        else:
                            body = b'{"message":"Codespace read unavailable"}'
                    except Exception:
                        status, body = 502, b'{"message":"Codespace read unavailable"}'
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self.server.server_port) + '/' + self.child_token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def child_env(self, env=None):
        result = clean_env(env)
        result.update(GITHUB_API_URL=self.url + '/', GH_TOKEN=self.child_token,
                      GH_PROMPT_DISABLED='1', NO_COLOR='1')
        return result

    def __exit__(self, *exc):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
        self._token = ''


def _terminate(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def _ssh_quote(value):
    if any(c in str(value) for c in '\n\r\0'):
        raise ValueError('Invalid SSH configuration path.')
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _ssh_problem(stderr):
    """Return fixed diagnostics, never arbitrary remote or credential text."""
    text = stderr.decode(errors='replace').lower()
    if any(part in text for part in ('host key verification failed', 'host identification has changed',
                                     'host key is known', 'offending ', 'host key has changed')):
        return ('SSH host key verification failed; verify the Codespace identity and its dedicated known_hosts file.', False)
    if 'permission denied' in text or 'authentication failed' in text:
        return ('SSH authentication failed; check authorization for ~/.ssh/codespaces.auto in this Codespace.', False)
    if any(part in text for part in ('connection refused', 'connection reset', 'connection closed',
                                     'closed by remote host', 'kex_exchange_identification',
                                     'ssh_exchange_identification', 'banner exchange', 'broken pipe',
                                     'operation timed out', 'connection timed out', 'connection aborted')):
        return ('SSH connection failed; the Codespace SSH service or forwarding tunnel may still be starting.', True)
    return ('SSH connection failed; inspect Codespace connectivity and the dedicated SSH configuration.', False)


def _check_gh(env):
    try:
        version = subprocess.run(['gh', '--version'], env=clean_env(env), capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise ValueError('GitHub CLI check timed out; verify gh is responsive and retry.') from None
    except OSError as error:
        raise ValueError('GitHub CLI is required for project connections.') from error
    if not version.stdout.startswith('gh version ' + SUPPORTED_GH + ' '):
        raise ValueError('Scratch transport requires tested GitHub CLI ' + SUPPORTED_GH + '; run the offline compatibility gate before upgrading.')


def herdr_stdio(codespace: str, state: str | Path) -> int:
    """Proxy raw Herdr SSH bytes through native gh activity reporting.

    This is the owned SSH alias's entrypoint, never the background sync path.
    Its fresh API capability cannot start a Codespace during Herdr reconnect.
    """
    from .state import read_json
    child, handlers = None, {}
    def shutdown(number, *_):
        raise SystemExit(128 + number)
    try:
        name, state = _name(codespace), Path(state)
        if not state.is_absolute() or '..' in state.parts:
            raise ValueError('Invalid proxy state')
        if read_json(state / 'scratch.json').get('codespace') != name:
            raise ValueError('Codespace binding changed')
        key = Path(os.environ.get('HOME') or Path.home()) / '.ssh/codespaces.auto'
        if not key.is_file():
            raise ValueError('Paired SSH key missing')
        _check_gh(os.environ)
        for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            handlers[number] = signal.signal(number, shutdown)
        with ReadOnlyAPI(name, github_token()) as api:
            child = subprocess.Popen(['gh', 'codespace', 'ssh', '-c', name, '--stdio', '--', '-i', str(key)],
                    env=api.child_env(), stderr=subprocess.DEVNULL, start_new_session=True)
            return child.wait()
    except (OSError, ValueError):
        print('Herdr Codespace SSH proxy unavailable; run task work:cs to reconnect.', file=sys.stderr)
        return 1
    finally:
        # gh --stdio is the byte-stream process itself, with no ssh grandchild.
        # Reap it even when OpenSSH kills only this ProxyCommand wrapper.
        _terminate(child)
        for number, previous in handlers.items():
            signal.signal(number, previous)


class CodespaceConnection:
    def __init__(self, codespace: str, *, state: Path, env=None):
        self.codespace = _name(codespace)
        self.state = Path(state)
        self.env = dict(os.environ if env is None else env)
        self.child = None
        self.api = None
        self.port = None
        self.accept_new = False
        self._ready = False

    def __enter__(self):
        from .state import atomic_write
        self.key = Path(self.env.get('HOME', str(Path.home()))) / '.ssh/codespaces.auto'
        if not self.key.is_file():
            raise ValueError('Codespace SSH key missing (~/.ssh/codespaces.auto); configure SSH explicitly first.')
        _check_gh(self.env)
        # Establish private state before accepting a host key. Shared SSH config
        # and ~/.ssh/known_hosts are never read or changed.
        atomic_write(self.state / '.transport-state', b'1\n')
        known_hosts = self.state / 'known_hosts'
        if known_hosts.is_symlink():
            raise ValueError('Dedicated SSH host-key store must not be a symlink.')
        if not known_hosts.exists():
            atomic_write(known_hosts, b'')
        self.api = ReadOnlyAPI(self.codespace, github_token(self.env))
        self.api.__enter__()
        try:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                self.port = sock.getsockname()[1]
            self.child = subprocess.Popen(
                ['gh', 'codespace', 'ports', 'forward', '2222:' + str(self.port), '-c', self.codespace],
                env=self.api.child_env(self.env), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + 40
            while self.child.poll() is None:
                try:
                    with socket.create_connection(('127.0.0.1', self.port), timeout=.5):
                        return self
                except OSError:
                    if time.monotonic() > deadline:
                        break
                    time.sleep(.2)
            raise ValueError('Codespace forward unavailable; it may be stopped or authentication expired. No start request was allowed.')
        except BaseException:
            self.__exit__()
            raise

    def ssh_config(self, path: Path) -> None:
        from .state import atomic_write
        checking = 'accept-new' if self.accept_new else 'yes'
        config = f'''Host project-sync
  HostName 127.0.0.1
  Port {self.port}
  User vscode
  IdentityFile {_ssh_quote(self.key)}
  IdentitiesOnly yes
  BatchMode yes
  StrictHostKeyChecking {checking}
  HostKeyAlias {_ssh_quote(self.codespace)}
  UserKnownHostsFile {_ssh_quote(self.state / 'known_hosts')}
  GlobalKnownHostsFile /dev/null
  ConnectTimeout 15
  ConnectionAttempts 1
  ServerAliveInterval 0
  ControlMaster no
  ControlPath none
  ClearAllForwardings yes
  LogLevel ERROR
'''
        atomic_write(path, config.encode())

    def _wait_for_ssh(self, path):
        # The local forwarding socket can accept connections before the tunnel
        # or sshd is ready. Retry only this harmless probe, never an RPC payload.
        deadline = time.monotonic() + 45
        waiting = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('Codespace SSH was not ready within 45 seconds; retry task work:cs and inspect Codespace status if it persists.')
            if self.child is not None and self.child.poll() is not None:
                raise ValueError('Codespace port forward stopped before SSH became ready; retry task work:cs.')
            try:
                result = subprocess.run(
                    ['ssh', '-F', str(path), '-T', 'project-sync', 'true'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=clean_env(self.env), timeout=min(15, remaining))
            except subprocess.TimeoutExpired:
                pass
            else:
                if result.returncode == 0:
                    self._ready = True
                    return
                if result.returncode != 255:
                    raise ValueError(f'Remote SSH readiness command failed (exit {result.returncode}); inspect the remote shell.')
                message, transient = _ssh_problem(result.stderr)
                if not transient:
                    raise ValueError(message)
            if not waiting:
                print('Waiting for Codespace SSH to become ready…', file=sys.stderr)
                waiting = True
            time.sleep(min(.5, max(0, deadline - time.monotonic())))

    def python(self, code: str, payload: bytes = b'', timeout: int = 60) -> bytes:
        path = self.state / 'ssh-probe-config'
        self.ssh_config(path)
        if not self._ready:
            self._wait_for_ssh(path)
        try:
            result = subprocess.run(
                ['ssh', '-F', str(path), '-T', 'project-sync', 'python3 -I -B -c ' + shlex.quote(code)],
                input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=clean_env(self.env), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ValueError('Remote Python/SSH timed out; inspect Codespace connectivity and status before retrying.') from None
        if result.returncode:
            # Do not relay arbitrary upstream/credential-bearing stderr to logs.
            if result.returncode == 255:
                raise ValueError(_ssh_problem(result.stderr)[0])
            raise ValueError(f'Remote Python command failed (exit {result.returncode}); inspect the remote checkout and Python environment.')
        return result.stdout

    def __exit__(self, *exc):
        self._ready = False
        _terminate(self.child)
        if self.api:
            self.api.__exit__(*exc)
            self.api = None
