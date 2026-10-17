"""Durable one-writer receipts and process-held session leases.

Transfers are deliberately four-stage: prepare, accept verified import, commit
source release, activate target. Network uncertainty never grants ownership.
"""
from contextlib import contextmanager
import json
import os
import subprocess
import sys
from pathlib import Path
import uuid

from .sessions import assert_quiescent, session_uuid
from .state import atomic_write, locked, read_json


def _paths(state, agent, session_id):
    session_uuid(session_id)
    if agent not in ('codex', 'claude'):
        raise ValueError('Unsupported agent')
    base = Path(state) / 'owners' / f'{agent}-{session_id}'
    return base.with_suffix('.json'), base.with_suffix('.lock')


def _write(path, receipt):
    atomic_write(path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
    return receipt


def owner_status(state, agent, session_id):
    path, _ = _paths(state, agent, session_id)
    try:
        return read_json(path)
    except FileNotFoundError:
        return dict(status='unknown', agent=agent, session_id=session_id)


@contextmanager
def _lease(state, agent, session_id):
    _, lock = _paths(state, agent, session_id)
    try:
        with locked(lock) as descriptor:
            yield descriptor
    except BlockingIOError as exc:
        raise ValueError('Session lease is active; stop the managed writer first') from exc


def _owned(receipt, host):
    if receipt['status'] == 'unknown':
        raise ValueError('Unknown session owner; existing sessions require explicit local adoption')
    if receipt.get('host') != host or receipt['status'] != 'idle':
        raise ValueError('Session ownership is foreign or uncertain; explicit recovery/transfer required')


@contextmanager
def managed_lease(state, agent, session_id, host, *, new=False, _with_fd=False):
    path, _ = _paths(state, agent, session_id)
    with _lease(state, agent, session_id) as descriptor:
        receipt = owner_status(state, agent, session_id)
        if new:
            if receipt['status'] != 'unknown':
                raise ValueError('New session UUID already has an ownership receipt')
            receipt = dict(schema=1, agent=agent, session_id=session_id, host=host, generation=0, status='idle')
        _owned(receipt, host)
        receipt = dict(receipt, status='running', pid=os.getpid())
        _write(path, receipt)
        try:
            yield (receipt, descriptor) if _with_fd else receipt
        except BaseException:
            # Uncertain child state must not become an automatic relaunch.
            _write(path, dict(receipt, status='uncertain'))
            raise
        else:
            _write(path, dict(receipt, status='idle', pid=None, native_pid=None))


def run_native_child(state, agent, session_id, command, *, cwd, env, receipt, lease_fd):
    """Gate native exec until the durable receipt names its PID.

    The dedicated guardian owns the process lease and survives launcher death.
    The child inherits the lease as a second defense. Even a native runtime that
    closes inherited FDs remains protected by its live PID in the receipt.
    """
    path, _ = _paths(state, agent, session_id)
    gate_read, gate_write = os.pipe()
    child = None
    try:
        gate = 'import os,sys; fd=int(sys.argv[1]); token=os.read(fd,1); os.close(fd); token==b"1" or sys.exit(125); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)'
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', gate, str(gate_read), *command], cwd=cwd, env=env, pass_fds=(gate_read, lease_fd))
        os.close(gate_read)
        gate_read = -1
        receipt['native_pid'] = child.pid
        _write(path, receipt)
        os.write(gate_write, b'1')
        os.close(gate_write)
        gate_write = -1
        while True:
            try:
                return child.wait()
            except KeyboardInterrupt:
                # Terminal SIGINT also reaches the native foreground child. Keep
                # custody until that child actually exits.
                continue
    finally:
        for descriptor in (gate_read, gate_write):
            if descriptor >= 0:
                os.close(descriptor)
        if child is not None and child.poll() is None:
            # On an exception before gate release the shim exits at EOF. After
            # release, retain the live PID in the uncertain receipt.
            receipt['native_pid'] = child.pid


def adopt_local(state, agent, session_id, host, *, native_paths):
    """Explicit first adoption. Caller has established the exact project scope."""
    path, _ = _paths(state, agent, session_id)
    with _lease(state, agent, session_id):
        if owner_status(state, agent, session_id)['status'] != 'unknown':
            raise ValueError('Existing ownership cannot be adopted or replaced')
        if not native_paths:
            raise ValueError('Adoption needs the exact existing native session files')
        assert_quiescent(native_paths)
        return _write(path, dict(schema=1, agent=agent, session_id=session_id, host=host, generation=0, status='idle', adopted=True))


def recover_local(state, agent, session_id, host, *, native_paths):
    """Recover only a crashed local process, never an uncertain remote transfer."""
    path, _ = _paths(state, agent, session_id)
    with _lease(state, agent, session_id):
        receipt = owner_status(state, agent, session_id)
        if receipt.get('host') != host or receipt['status'] not in ('running', 'uncertain'):
            raise ValueError('Only a crashed local writer can be recovered; transfers need peer receipts')
        for pid in {receipt.get('pid'), receipt.get('native_pid')} - {None}:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise ValueError('Cannot establish whether previous writer is alive') from exc
            else:
                raise ValueError('Previous writer or native child process is alive; refusing recovery')
        assert_quiescent(native_paths)
        _write(path.with_name(path.stem + '.recovery-' + uuid.uuid4().hex + '.json'), receipt)
        return _write(path, dict(receipt, status='idle', pid=None))


def begin_transfer(state, agent, session_id, host, destination_host, *, prepare=None):
    """Capture native state and transfer custody under one uninterrupted lease."""
    if not destination_host or destination_host == host:
        raise ValueError('Transfer needs a distinct destination host')
    path, _ = _paths(state, agent, session_id)
    with _lease(state, agent, session_id):
        current = owner_status(state, agent, session_id)
        # Retry is safe only for the exact still-pending destination.
        if current['status'] == 'transferring' and current.get('host') == host and current.get('destination_host') == destination_host:
            return current
        _owned(current, host)
        if prepare is not None:
            prepare()
        return _write(path, dict(current, status='transferring', nonce=str(uuid.uuid4()), destination_host=destination_host, generation=current['generation'] + 1, pid=None))


def _transfer(receipt, agent, session_id):
    if (receipt.get('schema') != 1 or receipt.get('agent') != agent or receipt.get('session_id') != session_id
            or not isinstance(receipt.get('generation'), int) or receipt['generation'] < 1
            or not receipt.get('host') or not receipt.get('destination_host')):
        raise ValueError('Invalid ownership transfer receipt')
    session_uuid(receipt.get('nonce'))


def prepare_receive(state, agent, session_id, host, transfer):
    """Reserve destination custody before any native import/reconciliation writes."""
    _transfer(transfer, agent, session_id)
    if transfer['status'] != 'transferring' or transfer['destination_host'] != host:
        raise ValueError('Wrong transfer destination/state')
    path, _ = _paths(state, agent, session_id)
    with _lease(state, agent, session_id):
        current = owner_status(state, agent, session_id)
        if current['status'] in ('receiving', 'accepted') and current.get('nonce') == transfer['nonce']:
            return current
        if current.get('generation', -1) >= transfer['generation']:
            raise ValueError('Stale ownership generation')
        if current['status'] != 'unknown' and not (current['status'] == 'transferred' and current.get('destination_host') == transfer['host']):
            raise ValueError('Destination has active or uncertain ownership')
        return _write(path, dict(transfer, status='receiving'))


def accept_transfer(state, agent, session_id, host, transfer, import_receipt):
    _transfer(transfer, agent, session_id)
    if transfer['status'] != 'transferring' or transfer['destination_host'] != host:
        raise ValueError('Wrong transfer destination/state')
    if (import_receipt.get('status') != 'complete' or import_receipt.get('session_id') != session_id
            or import_receipt.get('agent') != agent or import_receipt.get('source_host') != transfer['host']
            or import_receipt.get('generation') != transfer['generation'] or not import_receipt.get('bundle_id')):
        raise ValueError('Verified matching completed import required before acknowledgment')
    path, _ = _paths(state, agent, session_id)
    with _lease(state, agent, session_id):
        current = owner_status(state, agent, session_id)
        if current['status'] == 'accepted' and current.get('nonce') == transfer['nonce']:
            if current.get('bundle_id') != import_receipt['bundle_id']:
                raise ValueError('Transfer bundle changed')
            return current
        receiving = current['status'] == 'receiving' and current.get('nonce') == transfer['nonce'] and current.get('generation') == transfer['generation']
        if not receiving:
            if current.get('generation', -1) >= transfer['generation']:
                raise ValueError('Stale ownership generation')
            if current['status'] != 'unknown' and not (current['status'] == 'transferred' and current.get('destination_host') == transfer['host']):
                raise ValueError('Destination has active or uncertain ownership')
        return _write(path, dict(transfer, status='accepted', bundle_id=import_receipt['bundle_id']))


def complete_transfer(state, agent, session_id, host, acknowledgment):
    _transfer(acknowledgment, agent, session_id)
    path, _ = _paths(state, agent, session_id)
    with _lease(state, agent, session_id):
        current = owner_status(state, agent, session_id)
        if (current.get('host') != host or current['status'] not in ('transferring', 'transferred')
                or acknowledgment.get('status') != 'accepted' or not acknowledgment.get('bundle_id')
                or any(current.get(key) != acknowledgment.get(key) for key in ('nonce', 'generation', 'destination_host', 'host'))):
            raise ValueError('Wrong or uncertain transfer acknowledgment')
        return _write(path, dict(current, status='transferred', bundle_id=acknowledgment['bundle_id']))


def activate_transfer(state, agent, session_id, host, committed):
    _transfer(committed, agent, session_id)
    path, _ = _paths(state, agent, session_id)
    with _lease(state, agent, session_id):
        current = owner_status(state, agent, session_id)
        if (committed['status'] != 'transferred' or committed['destination_host'] != host
                or current['status'] not in ('accepted', 'idle')
                or any(current.get(key) != committed.get(key) for key in ('nonce', 'generation', 'bundle_id'))):
            raise ValueError('Source release receipt is missing or mismatched')
        return _write(path, dict(current, host=host, previous_host=committed['host'], status='idle', pid=None))
