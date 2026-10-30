#!/usr/bin/env bash
set -euo pipefail

# The caller PATH retains Feature directories that sudo's secure_path drops.
# It is data for /etc/environment, never the root process's command lookup path.
if [ "$#" -ne 2 ]; then
  echo 'Usage: system-path.sh <caller-path> <absolute-python-executable>' >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ] && [ -z "${DEVCONTAINER_SYSTEM_ROOT:-}" ]; then
  # Keep PATH literal so the user expands it when running the recovery command.
  # shellcheck disable=SC2016
  echo 'System PATH setup must run as root; use sudo bash .devcontainer/scripts/system-path.sh "$PATH" "$(command -v python3)".' >&2
  exit 1
fi

case "$2" in
  /*) ;;
  *) echo 'The Python executable must be an absolute path.' >&2; exit 2 ;;
esac
"$2" - "${DEVCONTAINER_SYSTEM_ROOT:-}" "$1" <<'PY'
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile

prefix, caller_path = sys.argv[1:]
root = Path(prefix) if prefix else Path('/')
if not root.is_absolute():
    sys.exit('DEVCONTAINER_SYSTEM_ROOT must be absolute.')
if '\n' in caller_path or '\r' in caller_path:
    sys.exit('The caller PATH must be a single line.')
local_bin = '/home/vscode/.local/bin'
environment = root / 'etc/environment'
zshenv = root / 'etc/zsh/zshenv'


def replace_or_append(content, pattern, replacement):
    parts = pattern.split(content)
    if len(parts) > 1:
        return parts[0] + replacement + ''.join(parts[1:])
    separator = '\n' if content and not content.endswith('\n') else ''
    return content + separator + replacement


original_environment = environment.read_text() if environment.exists() else ''
lines = original_environment.splitlines(keepends=True)
existing_path = None
for line in lines:
    if line.lstrip().startswith('PATH='):
        fields = shlex.split(line.strip()[5:], comments=True)
        existing_path = fields[0] if fields else ''
        break
blocked = ('/tmp', '/var/tmp', '/vscode')
caller_entries = [entry for entry in caller_path.split(':')
                  if Path(entry).is_absolute() and Path(entry).is_dir()
                  and not any(entry == p or entry.startswith(p + '/') for p in blocked)]
existing_entries = existing_path.split(':') if existing_path is not None else []
path_value = ':'.join(dict.fromkeys([local_bin, *caller_entries, *existing_entries]))
escaped = path_value.replace('\\', '\\\\').replace('"', '\\"')
path_line = 'PATH="' + escaped + '"\n'
path_lines = re.compile(r'^[ \t]*PATH=.*(?:\n|$)', re.M)
new_environment = replace_or_append(original_environment, path_lines, path_line)

block = '''# devcontainer-setup PATH
path=(/home/vscode/.local/bin ${path:#/home/vscode/.local/bin})
export PATH
# end devcontainer-setup PATH
'''
original_zshenv = zshenv.read_text() if zshenv.exists() else ''
marked = re.compile(r'^# devcontainer-setup PATH\n.*?^# end devcontainer-setup PATH(?:\n|$)', re.M | re.S)
new_zshenv = replace_or_append(original_zshenv, marked, block)


def atomic_write(path, content):
    if path.exists() and path.read_text() == content:
        return
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o755)
        path.parent.chmod(0o755)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


atomic_write(environment, new_environment)
atomic_write(zshenv, new_zshenv)
print('Configured system PATH for /home/vscode/.local/bin.')
PY
