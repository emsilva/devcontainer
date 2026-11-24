"""Install Herdr resume hooks on a Codespace so a restart can resume agents.

The same remote call links ~/.claude/handoff-feedback at the checkout's
.scratch/handoff-feedback, so session-handoff feedback syncs home.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from .state import atomic_write, read_json


AGENTS = ('antigravity-cli', 'claude', 'codex', 'grok', 'opencode')
RECEIPT = 'herdr-hooks.json'
# Remote `integration install` and `integration status` bounds. The SSH call
# is a little longer so a hung command hits these, not the transport timeout.
INSTALL_TIMEOUT = 60
STATUS_TIMEOUT = 30
SKILL_TIMEOUT = 30
_CALL_MARGIN = 15
_STATES = ('not installed', 'outdated', 'current')
_SKILL_STATES = ('current', 'installed', 'managed elsewhere', 'failed', 'unknown')
_FEEDBACK_STATES = ('current', 'linked', 'moved', 'managed elsewhere', 'no scratch', 'failed')

# Runs as `python3 -I -B -c` on the Codespace. Stdin is a JSON object
# {"install": [...], "skill": true|false, "root": "<absolute checkout>"}.
# root, when present, links ~/.claude/handoff-feedback at that checkout.
# Herdr is found the same way as workspace._HERDR_PROBE. Skill install uses
# timeout=30, matching SKILL_TIMEOUT.
REMOTE = """\
import errno, json, os, shlex, shutil, stat, subprocess, sys
ALLOWED = ("antigravity-cli", "claude", "codex", "grok", "opencode")
FEEDBACK_DIR = "handoff-feedback"

def ensure_skill(herdr):
    skill_dir = os.path.join(os.path.expanduser("~"), ".claude", "skills", "herdr")
    target = os.path.join(skill_dir, "SKILL.md")
    if os.path.islink(skill_dir) or os.path.islink(target):
        return "managed elsewhere"
    try:
        result = subprocess.run(
            [herdr, "--skill"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "failed"
    data = result.stdout
    if result.returncode != 0 or not data or not data.splitlines() or data.splitlines()[0] != b"---":
        return "failed"
    temporary = None
    try:
        if os.path.islink(skill_dir) or os.path.islink(target):
            return "managed elsewhere"
        if os.path.isfile(target) and not os.path.islink(target):
            with open(target, "rb") as existing:
                if existing.read() == data:
                    return "current"
        os.makedirs(skill_dir, exist_ok=True)
        if os.path.islink(skill_dir) or os.path.islink(target):
            return "managed elsewhere"
        temporary = os.path.join(skill_dir, ".herdr-skill-" + os.urandom(8).hex() + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        temporary = None
    except OSError:
        return "failed"
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return "installed"

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

def safe_root(root):
    if not isinstance(root, str) or not os.path.isabs(root):
        return False
    if any(char in root for char in (chr(0), chr(10), chr(13))):
        return False
    if ".." in root.split("/"):
        return False
    try:
        if os.path.islink(root):
            return False
    except OSError:
        return False
    return True

def same_bytes(left, right):
    try:
        left_fd = os.open(left, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            right_fd = os.open(right, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return False
        try:
            while True:
                left_chunk = os.read(left_fd, 65536)
                right_chunk = os.read(right_fd, 65536)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
        finally:
            os.close(right_fd)
    finally:
        os.close(left_fd)

def copy_file(src, dst):
    src_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(src_fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a file")
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, info.st_mode & 0o777)
        try:
            while True:
                chunk = os.read(src_fd, 65536)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    wrote = os.write(dst_fd, view)
                    if wrote <= 0:
                        raise OSError("short write")
                    view = view[wrote:]
            os.fsync(dst_fd)
        except Exception:
            os.close(dst_fd)
            try:
                os.unlink(dst)
            except OSError:
                pass
            raise
        os.close(dst_fd)
    finally:
        os.close(src_fd)

def relocate(src, dst):
    if kind(dst) != "missing":
        raise OSError("destination exists")
    src_kind = kind(src)
    if src_kind == "dir":
        try:
            os.rename(src, dst)
            return
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
        os.mkdir(dst)
        for name in sorted(os.listdir(src)):
            relocate(os.path.join(src, name), os.path.join(dst, name))
        os.rmdir(src)
        return
    if src_kind != "file":
        raise OSError("unsupported")
    try:
        os.link(src, dst, follow_symlinks=False)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        copy_file(src, dst)
    os.unlink(src)

def has_unsafe(directory):
    for name in os.listdir(directory):
        found = kind(os.path.join(directory, name))
        if found == "dir":
            if has_unsafe(os.path.join(directory, name)):
                return True
        elif found != "file":
            return True
    return False

def park(src, directory, name):
    # Differing bytes are renamed, never overwritten. A spare name that already
    # holds the same bytes is the copy; the source name is then the duplicate.
    n = 1
    while n <= 1000:
        candidate = name + ".codespace-" + str(n)
        path = os.path.join(directory, candidate)
        found = kind(path)
        if found == "missing":
            relocate(src, path)
            return
        if kind(src) == "file" and found == "file" and same_bytes(src, path):
            os.unlink(src)
            return
        n += 1
    raise OSError("no spare name")

def place(src, directory, name):
    dst = os.path.join(directory, name)
    src_kind = kind(src)
    dst_kind = kind(dst)
    if dst_kind == "missing":
        relocate(src, dst)
        return
    if src_kind == "dir" and dst_kind == "dir":
        for child in sorted(os.listdir(src)):
            place(os.path.join(src, child), dst, child)
        if os.listdir(src):
            raise OSError("not empty")
        os.rmdir(src)
        return
    if src_kind == "file" and dst_kind == "file" and same_bytes(src, dst):
        os.unlink(src)
        return
    park(src, directory, name)

def ensure_real_dir(path):
    found = kind(path)
    if found == "dir":
        return True
    if found != "missing":
        return False
    try:
        os.mkdir(path)
    except FileExistsError:
        pass
    except OSError:
        return False
    return kind(path) == "dir"

def ensure_parent(path):
    parent = os.path.dirname(path)
    found = kind(parent)
    if found == "missing":
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            return False
        found = kind(parent)
    if found == "dir":
        return True
    if found != "symlink":
        return False
    try:
        return os.path.isdir(parent)
    except OSError:
        return False

def link_feedback(root):
    if not safe_root(root):
        return "failed"
    scratch = os.path.join(root, ".scratch")
    scratch_kind = kind(scratch)
    if scratch_kind == "missing":
        return "no scratch"
    if scratch_kind != "dir":
        return "failed"
    target = os.path.join(scratch, FEEDBACK_DIR)
    link = os.path.join(os.path.expanduser("~"), ".claude", FEEDBACK_DIR)
    link_kind = kind(link)
    if link_kind == "error":
        return "failed"
    if link_kind == "symlink":
        try:
            pointed = os.readlink(link)
        except OSError:
            return "failed"
        if pointed != target:
            return "managed elsewhere"
        if not ensure_real_dir(target):
            return "failed"
        return "current"
    if link_kind in ("file", "other"):
        return "managed elsewhere"
    if link_kind == "missing":
        if not ensure_parent(link) or not ensure_real_dir(target):
            return "failed"
        try:
            os.symlink(target, link)
        except FileExistsError:
            if kind(link) == "symlink" and os.readlink(link) == target:
                return "current"
            return "failed"
        except OSError:
            return "failed"
        return "linked"
    if link_kind != "dir":
        return "managed elsewhere"
    try:
        if has_unsafe(link):
            return "failed"
    except OSError:
        return "failed"
    if not ensure_real_dir(target) or kind(target) != "dir":
        return "failed"
    for name in sorted(os.listdir(link)):
        place(os.path.join(link, name), target, name)
    if os.listdir(link):
        raise OSError("not empty")
    os.rmdir(link)
    try:
        os.symlink(target, link)
    except OSError:
        try:
            os.mkdir(link)
        except OSError:
            pass
        raise
    return "moved"

def ensure_feedback(root):
    try:
        state = link_feedback(root)
    except Exception:
        return "failed"
    if state in ("current", "linked", "moved", "managed elsewhere", "no scratch", "failed"):
        return state
    return "failed"

def ensure_agy_config_dir():
    config_dir = os.path.join(os.path.expanduser("~"), ".gemini", "config")
    gemini_dir = os.path.dirname(config_dir)
    if os.path.islink(gemini_dir) or os.path.islink(config_dir):
        return False
    try:
        os.makedirs(config_dir, exist_ok=True)
    except OSError:
        return False
    return kind(config_dir) == "dir"

AGY_ROOT_HOOK_KEY = "devcontainer-agy-root-session"
AGY_ROOT_HOOK_SCRIPT = '''#!/bin/sh
# managed by devcontainer-setup; keeps Herdr pinned to the top-level Antigravity
# conversation so internal subagents (/teamwork, workers) never overwrite the
# pane's resume session ID.
set -eu

emit_and_exit() {
  echo '{}'
  exit 0
}

event="${1:-}"
[ "${HERDR_ENV:-}" = "1" ] || emit_and_exit
[ -n "${HERDR_SOCKET_PATH:-}" ] || emit_and_exit
[ -n "${HERDR_PANE_ID:-}" ] || emit_and_exit
command -v python3 >/dev/null 2>&1 || emit_and_exit

payload=$(cat)
[ -n "$payload" ] || emit_and_exit

python3 - "$event" "$payload" <<'DEVCONTAINER_AGY_ROOT_PY' 2>/dev/null || true
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote

event = sys.argv[1] if len(sys.argv) > 1 else ""
raw_payload = sys.argv[2] if len(sys.argv) > 2 else ""
try:
    payload = json.loads(raw_payload)
except Exception:
    raise SystemExit(0)
if not isinstance(payload, dict):
    raise SystemExit(0)

cid = payload.get("conversationId")
if not isinstance(cid, str) or not cid:
    raise SystemExit(0)

pane_id = os.environ.get("HERDR_PANE_ID", "")
sock_path = os.environ.get("HERDR_SOCKET_PATH", "")
if not pane_id or not sock_path:
    raise SystemExit(0)

override = os.environ.get("ANTIGRAVITY_CLI_HOME") or os.environ.get("AGY_HOME")
app_dir = Path(override) if override else (Path.home() / ".gemini/antigravity-cli")
db_path = app_dir / "conversation_summaries.db"

def lookup_root(conversation_id):
    if not db_path.is_file() or db_path.is_symlink():
        return None, None
    encoded = quote(str(db_path), safe="/")
    con = None
    for query in ("mode=ro", "mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(f"file:{encoded}?{query}", uri=True, timeout=0.3)
            break
        except sqlite3.Error:
            con = None
    if con is None:
        return None, None
    try:
        current = conversation_id
        seen = set()
        found_any = False
        is_subagent = False
        for _ in range(16):
            if current in seen:
                break
            seen.add(current)
            row = con.execute(
                "SELECT COALESCE(parent_conversation_id, ''), COALESCE(nesting_depth, 0) "
                "FROM conversation_summaries WHERE conversation_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                break
            found_any = True
            parent, depth = row[0], row[1]
            if parent:
                is_subagent = True
                current = parent
                continue
            if depth and depth > 0:
                is_subagent = True
                return None, True
            return current, is_subagent
        return (current if found_any and not is_subagent else None), (is_subagent if found_any else None)
    except sqlite3.Error:
        return None, None
    finally:
        con.close()

def send_session(root_id):
    seq = time.time_ns()
    params = {
        "pane_id": pane_id,
        "source": "herdr:antigravity_cli",
        "agent": "agy",
        "seq": seq,
        "agent_session_id": root_id,
    }
    transcript = app_dir / "brain" / root_id / ".system_generated/logs/transcript.jsonl"
    if transcript.is_file() and not transcript.is_symlink():
        params["agent_session_path"] = str(transcript)
    request = json.dumps({
        "id": f"herdr:antigravity_cli:{seq}",
        "method": "pane.report_agent_session",
        "params": params,
    })
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(sock_path)
            client.sendall((request + chr(10)).encode())
            client.recv(4096)
    except Exception:
        pass

if event == "pre-invocation-delayed":
    time.sleep(0.25)
    root_id, is_subagent = lookup_root(cid)
    if root_id and (is_subagent or root_id != cid):
        send_session(root_id)
    raise SystemExit(0)

root_id, is_subagent = lookup_root(cid)
if event == "pre-invocation":
    # Herdr also hooks PreInvocation; re-run this script shortly after in a detached
    # child so root_id always wins if a subagent (or a brand-new subagent not yet
    # flushed to SQLite) just overwrote Herdr's pane session ID.
    if is_subagent is True or root_id is None:
        script_file = os.path.join(os.path.expanduser("~"), ".gemini", "config", "hooks", "devcontainer-agy-root-session.sh")
        try:
            proc = subprocess.Popen(
                ["bash", script_file, "pre-invocation-delayed"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
            if proc.stdin is not None:
                proc.stdin.write(raw_payload.encode("utf-8"))
                proc.stdin.close()
        except Exception:
            if root_id and root_id != cid:
                send_session(root_id)
elif root_id:
    send_session(root_id)
DEVCONTAINER_AGY_ROOT_PY

emit_and_exit
'''

def ensure_agy_root_hook(force=False):
    config_dir = os.path.join(os.path.expanduser("~"), ".gemini", "config")
    hooks_dir = os.path.join(config_dir, "hooks")
    herdr_hook = os.path.join(hooks_dir, "herdr-agent-state.sh")
    hooks_json = os.path.join(config_dir, "hooks.json")
    if not force and kind(herdr_hook) != "file":
        return
    if not ensure_agy_config_dir():
        return
    if os.path.islink(hooks_dir) or os.path.islink(hooks_json):
        return
    try:
        os.makedirs(hooks_dir, exist_ok=True)
        script_path = os.path.join(hooks_dir, AGY_ROOT_HOOK_KEY + ".sh")
        if os.path.islink(script_path):
            return
        data = AGY_ROOT_HOOK_SCRIPT.encode("utf-8")
        write_script = True
        if kind(script_path) == "file":
            with open(script_path, "rb") as existing:
                if existing.read() == data and (os.stat(script_path).st_mode & 0o111):
                    write_script = False
        if write_script:
            tmp = os.path.join(hooks_dir, "." + AGY_ROOT_HOOK_KEY + "-" + os.urandom(6).hex() + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, script_path)
        current = {}
        if kind(hooks_json) == "file":
            with open(hooks_json, "rb") as fh:
                loaded = json.loads(fh.read().decode("utf-8"))
            if not isinstance(loaded, dict):
                return
            current = loaded
        elif kind(hooks_json) != "missing":
            return
        quoted = shlex.quote(script_path)
        desired = {
            "SessionStart": [{"type": "command", "command": f"bash {quoted} session-start", "timeout": 10}],
            "PreInvocation": [{"type": "command", "command": f"bash {quoted} pre-invocation", "timeout": 10}],
            "PostInvocation": [{"type": "command", "command": f"bash {quoted} post-invocation", "timeout": 10}],
            "Stop": [{"type": "command", "command": f"bash {quoted} stop", "timeout": 10}],
        }
        if current.get(AGY_ROOT_HOOK_KEY) != desired:
            current[AGY_ROOT_HOOK_KEY] = desired
            encoded = (json.dumps(current, indent=2, sort_keys=True) + "\\n").encode("utf-8")
            tmp = os.path.join(config_dir, ".hooks-" + os.urandom(6).hex() + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, hooks_json)
    except Exception:
        pass

def main():
    try:
        body = json.load(sys.stdin)
    except (ValueError, TypeError):
        body = None
    names = body.get("install") if isinstance(body, dict) else None
    skill = body.get("skill") if isinstance(body, dict) else None
    root = body.get("root") if isinstance(body, dict) else None
    if (not isinstance(names, list)
            or any(not isinstance(name, str) or name not in ALLOWED for name in names)
            or (skill is not None and skill is not True and skill is not False)
            or (root is not None and not isinstance(root, str))):
        sys.stderr.write("refused\\n")
        raise SystemExit(2)
    answer = {}
    if isinstance(root, str):
        answer["feedback"] = ensure_feedback(root)
    herdr = shutil.which("herdr", path=os.pathsep.join([os.environ.get("PATH", ""), os.path.expanduser("~/.local/bin")]))
    if not herdr:
        answer["herdr"] = False
        print(json.dumps(answer))
        return
    answer["herdr"] = True
    if skill is True:
        answer["skill"] = ensure_skill(herdr)
    installed = {}
    for name in names:
        if name == "antigravity-cli":
            ensure_agy_config_dir()
        result = subprocess.run(
            [herdr, "integration", "install", name],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        installed[name] = result.returncode
    ensure_agy_root_hook(force="antigravity-cli" in names)
    status = subprocess.run(
        [herdr, "integration", "status"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    answer["installed"] = installed
    answer["status"] = status.stdout.decode("utf-8", "replace")
    print(json.dumps(answer))

main()
"""


def _python_timeout(count: int, skill: bool = False) -> int:
    return STATUS_TIMEOUT + INSTALL_TIMEOUT * count + (SKILL_TIMEOUT if skill else 0) + _CALL_MARGIN


def _state(rest: str) -> str:
    rest = rest.lstrip()
    for name in _STATES:
        if rest == name or rest.startswith(name + ' ') or rest.startswith(name + '('):
            return name
    return 'unknown'


def parse(text: str) -> dict[str, str]:
    """Map antigravity-cli, claude, codex, grok and opencode to current, outdated, not installed, or unknown."""
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
        'skill': 'unknown',
        'feedback': 'unknown',
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
    return 'Herdr resume hooks are current on ' + codespace + ' (' + ', '.join(AGENTS) + ').'


def _skill_line(receipt: dict) -> str | None:
    skill = receipt.get('skill')
    codespace = receipt.get('codespace', '')
    if skill == 'installed':
        return 'Installed the Herdr skill on ' + codespace + ' (the copy bundled with its Herdr).'
    if skill == 'failed':
        return ('Could not install the Herdr skill on ' + codespace
                + '; agents there cannot load it. Run task work:cs again.')
    return None


def _feedback_line(receipt: dict) -> str | None:
    feedback = receipt.get('feedback')
    codespace = receipt.get('codespace', '')
    if not isinstance(codespace, str):
        codespace = ''
    if feedback == 'moved':
        return ('Moved handoff feedback on ' + codespace
                + ' into .scratch/handoff-feedback so it syncs.')
    if feedback == 'managed elsewhere':
        return 'Handoff feedback on ' + codespace + ' is managed elsewhere; left it unchanged.'
    return None


def _emit(receipt: dict) -> None:
    try:
        print(_message(receipt), flush=True)
        extra = _skill_line(receipt)
        if extra:
            print(extra, flush=True)
        feedback = _feedback_line(receipt)
        if feedback:
            print(feedback, flush=True)
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


def _payload(names, skill: bool, remote_root: str | None = None) -> bytes:
    body = {'install': list(names), 'skill': skill}
    if remote_root is not None:
        body['root'] = remote_root
    return json.dumps(body).encode()


def _skill_state(value) -> str:
    if value in _SKILL_STATES:
        return value
    return 'failed'


def _feedback_state(value) -> str:
    if value in _FEEDBACK_STATES:
        return value
    return 'failed'


def _safe_root(value) -> str:
    if not isinstance(value, str) or not value or '\0' in value or '\n' in value or '\r' in value:
        raise ValueError('invalid remote checkout')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid remote checkout')
    return value


def _call(connection, names: list[str], *, skill: bool = False, remote_root: str | None = None) -> dict:
    raw = connection.python(REMOTE, _payload(names, skill, remote_root), _python_timeout(len(names), skill))
    value = _load(raw)
    if value.get('herdr') is False:
        raise ValueError('Herdr is not installed there')
    if value.get('herdr') is not True or not isinstance(value.get('status'), str):
        raise ValueError('invalid hooks response')
    if remote_root is not None and not isinstance(value.get('feedback'), str):
        raise ValueError('invalid hooks response')
    return value


def _check(connection, codespace, checked_at, remote_root) -> dict:
    # Skill and the feedback link ride the status call, so a Codespace that is
    # already current still costs one call. The install call does not repeat it.
    first = _call(connection, [], skill=True, remote_root=remote_root)
    skill = _skill_state(first.get('skill'))
    feedback = _feedback_state(first.get('feedback'))
    hooks = parse(first['status'])
    missing = [agent for agent in AGENTS if hooks.get(agent) != 'current']
    installed: list[str] = []
    if missing:
        second = _call(connection, missing, skill=False)
        hooks = parse(second['status'])
        installed = list(missing)
    return {
        'codespace': codespace,
        'checked_at': checked_at,
        'hooks': {agent: hooks.get(agent, 'unknown') for agent in AGENTS},
        'installed': installed,
        'skill': skill,
        'feedback': feedback,
    }


def ensure(connection, codespace, state, remote_root, *, now=time.time) -> dict:
    """Check hooks, the bundled Herdr skill and the feedback link; write the receipt.

    ValueError and OSError from the connection or the response become a receipt error.
    The hook line is unchanged. A skill line is added only when it was installed or the install failed.
    A feedback line is added only when feedback was moved or is managed elsewhere.
    """
    try:
        checked_at = now()
        if not isinstance(checked_at, (int, float)):
            raise ValueError('invalid hooks clock')
        receipt = _check(connection, codespace, checked_at, _safe_root(remote_root))
        _store(state, receipt)
    except (ValueError, OSError) as error:
        return _record_error(codespace, state, error, now)
    _emit(receipt)
    return receipt


def ensure_on(codespace, state, remote_root) -> dict:
    """Open a fresh Codespace connection and ensure hooks and the feedback link.

    A connect failure is a receipt error.
    """
    from .transport import CodespaceConnection
    try:
        connection = CodespaceConnection(codespace, state=Path(state))
        entered = connection.__enter__()
    except (ValueError, OSError) as error:
        return _record_error(codespace, state, error, time.time)
    try:
        return ensure(entered, codespace, state, remote_root)
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


def _known_skill(receipt) -> str:
    if isinstance(receipt, dict) and receipt.get('skill') in _SKILL_STATES:
        return receipt['skill']
    return 'unknown'


def _known_feedback(receipt) -> str:
    if isinstance(receipt, dict) and receipt.get('feedback') in (*_FEEDBACK_STATES, 'unknown'):
        return receipt['feedback']
    return 'unknown'


def _with_skill(line: str, receipt) -> str:
    skill = _known_skill(receipt)
    feedback = _known_feedback(receipt)
    text = line + '; skill ' + skill
    # A line that already tells the owner to run work:cs does not grow a second hint.
    if skill in ('failed', 'unknown') and 'task work:cs' not in line:
        text += ' (task work:cs installs it)'
    text += '; feedback ' + feedback
    if feedback in ('failed', 'unknown') and 'task work:cs' not in line:
        text += ' (task work:cs links it)'
    return text


def status_line(receipt, codespace) -> str | None:
    """One offline line for work:status, or None when there is no Codespace to name."""
    if not isinstance(codespace, str) or not codespace:
        return None
    not_checked = 'Herdr hooks: not checked on ' + codespace + '; task work:cs checks them'
    if not isinstance(receipt, dict) or receipt.get('codespace') != codespace:
        return _with_skill(not_checked, None)
    error = receipt.get('error')
    if isinstance(error, str) and error:
        return _with_skill('Herdr hooks: check failed on ' + codespace + ' (' + error
                           + '); task work:cs checks again', receipt)
    hooks = receipt.get('hooks') if isinstance(receipt.get('hooks'), dict) else {}
    pending = [agent + ' ' + str(hooks.get(agent, 'unknown'))
               for agent in AGENTS if hooks.get(agent) != 'current']
    if pending:
        return _with_skill('Herdr hooks: ' + ', '.join(pending) + ' on ' + codespace
                           + '; task work:cs installs them', receipt)
    checked_at = receipt.get('checked_at')
    if not isinstance(checked_at, (int, float)):
        return _with_skill(not_checked, None)
    from .arming import format_time
    try:
        checked = format_time(checked_at)
    except (TypeError, ValueError, OSError, OverflowError):
        return _with_skill(not_checked, None)
    return _with_skill('Herdr hooks: ' + ', '.join(AGENTS) + ' current on ' + codespace
                       + ' (checked ' + checked + ')', receipt)
