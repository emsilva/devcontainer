#!/usr/bin/env bash
set -euo pipefail

# An optional caller PATH retains Feature directories that sudo's secure_path drops.
# It is data for /etc/environment, never the root process's command lookup path.
if [ "$#" -gt 1 ]; then
  echo 'Usage: system-path.sh [caller-path]' >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ] && [ -z "${DEVCONTAINER_SYSTEM_ROOT:-}" ]; then
  # Keep PATH literal so the user expands it when running the recovery command.
  # shellcheck disable=SC2016
  echo 'System PATH setup must run as root; use sudo bash .devcontainer/scripts/system-path.sh "$PATH".' >&2
  exit 1
fi

python3 - "${DEVCONTAINER_SYSTEM_ROOT:-}" "${1:-$PATH}" <<'PY'
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
source_path = caller_path if existing_path is None else existing_path
entries = [entry for entry in source_path.split(':') if entry != local_bin]
path_value = ':'.join([local_bin, *entries])
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
