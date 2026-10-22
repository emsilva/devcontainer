"""Mac notifications; a missing or failing osascript never breaks a caller."""
from __future__ import annotations

import subprocess

SCRIPT = ('on run argv', 'display notification (item 2 of argv) with title (item 1 of argv)', 'end run')


def notify(title: str, message: str, *, run=subprocess.run) -> bool:
    args = ['osascript']
    for line in SCRIPT:
        args += ['-e', line]
    try:
        # Text travels as arguments, so quotes in it cannot become script.
        return run(args + [title, message], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False
