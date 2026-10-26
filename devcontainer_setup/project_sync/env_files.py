"""Copy the checkout's gitignored .env files onto a Codespace. Never overwrite, never log contents."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from .state import atomic_write, read_json


RECEIPT = 'env-files.json'
TIMEOUT = 60
MAX_FILES = 20
MAX_BYTES = 64 * 1024
_STATES = ('copied', 'current', 'differs', 'no folder', 'too large', 'failed')
_SKIP_COMPONENTS = frozenset({'node_modules', '.scratch', 'tmp'})
_EXAMPLE_SUFFIXES = ('.example', '.sample', '.template')
_GIT_OVERRIDES = (
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
)
_TOO_LARGE = 'too large'

# Runs as `python3 -I -B -c` on the Codespace. Stdin is JSON
# {"root": "<absolute checkout>", "files": {"<relative>": "<base64>"}}.
# The reply is states only. A bad path or any symlink on the way is refused
# before a byte is written.
REMOTE = r"""
import base64, hashlib, json, os, stat, sys

MAX_BYTES = 65536
STATES = ("copied", "current", "differs", "no folder", "too large", "failed")

def refuse():
    sys.stderr.write("refused\n")
    raise SystemExit(2)

def safe_relative(path):
    if not isinstance(path, str) or not path or path.endswith("/") or path.startswith("/"):
        return False
    if "\0" in path or "\\" in path:
        return False
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True

def safe_root(root):
    if not isinstance(root, str) or not os.path.isabs(root) or "\0" in root:
        return False
    if ".." in root.split("/"):
        return False
    try:
        if os.path.islink(root):
            return False
    except OSError:
        return False
    return True

def kind(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "error"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"

def preflight(root, relative):
    current = root
    for part in relative.split("/")[:-1]:
        current = os.path.join(current, part)
        found = kind(current)
        if found == "missing":
            return "no folder"
        if found == "symlink":
            return "symlink"
        if found != "dir":
            return "no folder"
    found = kind(os.path.join(current, relative.split("/")[-1]))
    if found == "symlink":
        return "symlink"
    if found == "missing":
        return "missing"
    if found == "file":
        return "exists"
    return "failed"

def compare(dirfd, name, data):
    try:
        info = os.lstat(name, dir_fd=dirfd)
    except OSError:
        return "failed"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "failed"
    if info.st_size != len(data):
        return "differs"
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except OSError:
        return "failed"
    try:
        existing = b""
        while len(existing) < len(data):
            chunk = os.read(fd, len(data) - len(existing))
            if not chunk:
                break
            existing += chunk
        extra = os.read(fd, 1)
    finally:
        os.close(fd)
    if extra or hashlib.sha256(existing).digest() != hashlib.sha256(data).digest():
        return "differs"
    return "current"

def write_new(dirfd, name, data):
    temporary = ".env-copy-" + os.urandom(8).hex() + ".tmp"
    previous = os.umask(0o077)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    except OSError:
        return "failed"
    finally:
        os.umask(previous)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                wrote = os.write(fd, view)
                if wrote <= 0:
                    raise OSError("short write")
                view = view[wrote:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temporary, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
        except FileExistsError:
            return compare(dirfd, name, data)
        return "copied"
    except OSError:
        return "failed"
    finally:
        try:
            os.unlink(temporary, dir_fd=dirfd)
        except OSError:
            pass

def place(root, relative, data):
    if len(data) > MAX_BYTES:
        return "too large"
    parts = relative.split("/")
    try:
        dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return "failed"
    try:
        for part in parts[:-1]:
            try:
                info = os.lstat(part, dir_fd=dirfd)
            except FileNotFoundError:
                return "no folder"
            except OSError:
                return "failed"
            if stat.S_ISLNK(info.st_mode):
                return "failed"
            if not stat.S_ISDIR(info.st_mode):
                return "no folder"
            try:
                nextfd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=dirfd)
            except OSError:
                return "failed"
            os.close(dirfd)
            dirfd = nextfd
        name = parts[-1]
        try:
            info = os.lstat(name, dir_fd=dirfd)
        except FileNotFoundError:
            return write_new(dirfd, name, data)
        except OSError:
            return "failed"
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return "failed"
        return compare(dirfd, name, data)
    finally:
        try:
            os.close(dirfd)
        except OSError:
            pass

def main():
    try:
        body = json.load(sys.stdin)
    except (ValueError, TypeError):
        refuse()
    if not isinstance(body, dict):
        refuse()
    root = body.get("root")
    files = body.get("files")
    if not safe_root(root) or not isinstance(files, dict):
        refuse()
    decoded = {}
    for path, text in files.items():
        if not safe_relative(path) or not isinstance(text, str):
            refuse()
        try:
            decoded[path] = base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            refuse()
    if any(preflight(root, path) == "symlink" for path in decoded):
        refuse()
    answer = {}
    for path, data in decoded.items():
        state = place(root, path, data)
        answer[path] = state if state in STATES else "failed"
    sys.stdout.write(json.dumps({"files": answer}) + "\n")

main()
"""


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
    return {'codespace': codespace, 'checked_at': checked_at, 'files': {}, 'error': reason}


def _line(path: str, state: str, codespace: str) -> str | None:
    if state == 'copied':
        return 'Copied ' + path + ' to ' + codespace + ' (mode 600).'
    if state == 'differs':
        return 'Left ' + path + ' on ' + codespace + " as it is: it differs from the Mac's copy."
    if state == 'no folder':
        return 'Did not copy ' + path + ' to ' + codespace + ": its folder doesn't exist there."
    if state == 'too large':
        return 'Did not copy ' + path + ' to ' + codespace + ': it is larger than 64 KiB.'
    if state == 'failed':
        return 'Did not copy ' + path + ' to ' + codespace + ': the copy failed.'
    return None


def _emit(receipt: dict) -> None:
    try:
        codespace = receipt.get('codespace', '')
        error = receipt.get('error')
        if isinstance(error, str) and error:
            print('Could not check .env files on ' + codespace + ': ' + error
                  + '. Run task work:cs again.', flush=True)
            return
        files = receipt.get('files') if isinstance(receipt.get('files'), dict) else {}
        for path in sorted(path for path in files if isinstance(path, str)):
            line = _line(path, files[path], codespace)
            if line:
                print(line, flush=True)
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


def _wanted(relative: str) -> bool:
    if not relative or relative.endswith('/') or '\0' in relative or '\\' in relative:
        return False
    if any(ord(char) < 32 for char in relative):
        return False
    parts = relative.split('/')
    if any(part in ('', '.', '..') for part in parts):
        return False
    base = parts[-1]
    if base != '.env' and not base.startswith('.env.'):
        return False
    if base.endswith(_EXAMPLE_SUFFIXES):
        return False
    ancestors = parts[:-1]
    for index, part in enumerate(ancestors):
        if part in _SKIP_COMPONENTS:
            return False
        if part == '.claude' and index + 1 < len(ancestors) and ancestors[index + 1] == 'worktrees':
            return False
    return True


def _git_list(root: Path) -> list[str]:
    env = os.environ.copy()
    for key in _GIT_OVERRIDES:
        env.pop(key, None)
    try:
        result = subprocess.run(
            ['git', '-C', str(root), 'ls-files', '--others', '--ignored',
             '--exclude-standard', '--directory', '-z'],
            capture_output=True, env=env, timeout=30)
    except subprocess.TimeoutExpired:
        raise ValueError('git ls-files failed') from None
    except OSError:
        raise
    if result.returncode:
        raise ValueError('git ls-files failed')
    paths = []
    for raw in result.stdout.split(b'\0'):
        if not raw:
            continue
        try:
            paths.append(raw.decode('utf-8'))
        except UnicodeDecodeError:
            raise ValueError('git ls-files failed') from None
    return paths


def _read_no_symlink(root: Path, relative: str):
    """Bytes, 'too large', or None when the path is not a regular file reached without a symlink."""
    parts = relative.split('/')
    dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                info = os.lstat(part, dir_fd=dirfd)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return None
            nextfd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=dirfd)
            os.close(dirfd)
            dirfd = nextfd
        name = parts[-1]
        try:
            info = os.lstat(name, dir_fd=dirfd)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > MAX_BYTES:
            return _TOO_LARGE
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
        try:
            data = os.read(fd, MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_BYTES:
            return _TOO_LARGE
        return data
    finally:
        os.close(dirfd)


def find(root) -> dict:
    """Gitignored .env files under root.

    Values are file bytes, or 'too large'. Skipped names are absent.
    Raises ValueError if Git fails or more than 20 files match.
    """
    listed = [path for path in _git_list(Path(root)) if _wanted(path)]
    found = {}
    for path in sorted(set(listed)):
        value = _read_no_symlink(Path(root), path)
        if value is None:
            continue
        found[path] = value
        if len(found) > MAX_FILES:
            raise ValueError('too many .env files')
    return found


def _load(raw) -> dict:
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError('invalid env files response')
    try:
        value = json.loads(bytes(raw).decode())
    except (UnicodeDecodeError, ValueError):
        raise ValueError('invalid env files response') from None
    if not isinstance(value, dict):
        raise ValueError('invalid env files response')
    return value


def _safe_root(value) -> str:
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('invalid remote checkout')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid remote checkout')
    return value


def _payload(remote_root: str, files: dict) -> bytes:
    # Contents travel only here, base64 inside JSON, as the stdin payload of python().
    encoded = {path: base64.b64encode(data).decode('ascii') for path, data in files.items()}
    return json.dumps({'root': remote_root, 'files': encoded}, sort_keys=True).encode()


def _states(raw, sent: dict) -> dict:
    files = _load(raw).get('files')
    if not isinstance(files, dict):
        raise ValueError('invalid env files response')
    return {path: files[path] if files.get(path) in _STATES else 'failed' for path in sent}


def _clock(now) -> float:
    value = now()
    if not isinstance(value, (int, float)):
        raise ValueError('invalid env files clock')
    return value


def ensure(connection, codespace, state, root, remote_root, *, now=time.time) -> dict:
    """Copy missing .env files, write the receipt, and print one line per file that is not current.

    ValueError and OSError from Git, the connection, python() or parsing become a receipt error.
    """
    try:
        checked_at = _clock(now)
        found = find(root)
        states = {path: _TOO_LARGE for path, value in found.items() if value == _TOO_LARGE}
        to_send = {path: value for path, value in found.items() if isinstance(value, bytes)}
        if to_send:
            raw = connection.python(REMOTE, _payload(_safe_root(remote_root), to_send), TIMEOUT)
            states.update(_states(raw, to_send))
        receipt = {'codespace': codespace, 'checked_at': checked_at, 'files': states}
        _store(state, receipt)
    except (ValueError, OSError) as error:
        return _record_error(codespace, state, error, now)
    _emit(receipt)
    return receipt


def ensure_on(codespace, state, root, remote_root) -> dict:
    """Open a fresh Codespace connection and copy .env files. A connect failure is a receipt error."""
    from .transport import CodespaceConnection
    try:
        connection = CodespaceConnection(codespace, state=Path(state))
        entered = connection.__enter__()
    except (ValueError, OSError) as error:
        return _record_error(codespace, state, error, time.time)
    try:
        return ensure(entered, codespace, state, root, remote_root)
    finally:
        # The check already printed its line. Closing must not fail work:cs or print another.
        try:
            connection.__exit__(None, None, None)
        except (ValueError, OSError):
            pass


def read(state) -> dict | None:
    """The last env-files receipt, or None when it is missing or cannot be read. Offline."""
    try:
        value = read_json(Path(state) / RECEIPT)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _display_state(state) -> str:
    return state if state in _STATES else 'failed'


def status_line(receipt, codespace) -> str | None:
    """One offline line for work:status, or None when there is no Codespace to name."""
    if not isinstance(codespace, str) or not codespace:
        return None
    not_checked = 'Env files: not checked on ' + codespace + '; task work:cs copies them'
    if not isinstance(receipt, dict) or receipt.get('codespace') != codespace:
        return not_checked
    error = receipt.get('error')
    if isinstance(error, str) and error:
        return ('Env files: check failed on ' + codespace + ' (' + ' '.join(error.split())[:160]
                + '); task work:cs copies them')
    files = receipt.get('files')
    if not isinstance(files, dict):
        return not_checked
    parts = [path + ' ' + _display_state(files[path])
             for path in sorted(files) if isinstance(path, str)]
    if not parts:
        return 'Env files: none found'
    return 'Env files: ' + '; '.join(parts)
