"""Shared test fixture: a process shaped like the keep-alive child, for stop-by-fact tests."""
from pathlib import Path
import subprocess
import sys
import time

from devcontainer_setup.project_sync.state import process_identity


def keepalive_decoy(test, directory, name='cs-1', *, ignore_term=False):
    """Start a process whose argv names `name` as a keep-alive child does; return it and its record.

    It leads its own process group, ends by itself within 60 s, and the test's cleanup reaps it.
    """
    ready = Path(directory) / f'decoy-ready-{name}-{time.monotonic_ns()}'
    code = ('import pathlib, signal, sys, time; '
            + ('signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if ignore_term else '')
            + 'pathlib.Path(sys.argv[1]).touch(); time.sleep(60)')
    child = subprocess.Popen([sys.executable, '-c', code, str(ready), 'codespace', 'ssh', '-c', name, '--',
                              '-i', 'key', 'exec sleep infinity'], start_new_session=True)
    test.addCleanup(child.wait)
    test.addCleanup(child.kill)
    # Read its identity only once its code runs, after any interpreter re-exec.
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    return child, {'pid': child.pid, 'process_identity': process_identity(child.pid)}
