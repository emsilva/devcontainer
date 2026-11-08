"""Decide recovery moves before locking, then journal and finish them forward."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import time

from . import arming, scratch, workspace
from .state import atomic_write, locked, read_json
from .transport import _name

INTENT = 'recovery-move.json'
CHANGED = 'Recovery changed while you were deciding; nothing moved. Run the command again.'


@dataclass(frozen=True)
class Decision:
    old: str
    new: str
    gh_user: str
    billable_owner: str
    carry: bool
    consent_sha256: str
    arming_sha256: str
    ports: tuple[int, ...]


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def decide(project, state: Path, new: str, *, ask=input, add_ports=(), remove_ports=()) -> Decision | None:
    """Read NEW and obtain any fresh consent without holding the workspace lock."""
    state = Path(state)
    current = arming.read(state)
    if not current.get('armed') or new == current.get('codespace'):
        return None
    new = _name(new)
    raw = (state / arming.ARMING).read_bytes()
    if json.loads(raw) != current:
        raise ValueError(CHANGED)
    value = workspace.registry(project, state)
    primary = workspace._project(value['local_root'], project.identity)
    binding = read_json(state / scratch.CONFIG)
    if binding.get('identity') != project.identity:
        raise ValueError('Codespace pairing belongs to another project')
    old = binding['codespace']
    user = arming.account(os.environ)
    from .codespaces import Codespaces
    service = Codespaces(primary, state)
    row = service._api('GET', 'user/codespaces/' + new, service._api_env())
    if row.get('name') != new or not service._matches_repository(row):
        raise ValueError('No Codespace ' + new + ' for this repository; existing binding preserved')
    owner = (row.get('billable_owner') or {}).get('login')
    if not owner:
        raise ValueError(f'{new} does not name a billing owner; check it in GitHub. Nothing moved.')
    carry = arming.carries_over(current, project_name=primary.root.name, gh_user=user, old=old, new_owner=owner)
    text = arming.consent_text(primary.root.name, new, owner)
    if not carry:
        print(text)
        try:
            answer = ask('> ')
        except (EOFError, KeyboardInterrupt):
            raise ValueError('Move cancelled; nothing moved.') from None
        if answer.strip() != 'arm':
            raise ValueError(f'Not moved: the confirmation was not "arm"; recovery stays on {old}.')
    return Decision(old, new, user, owner, carry, hashlib.sha256(text.encode()).hexdigest(),
                    hashlib.sha256(raw).hexdigest(),
                    tuple(arming.resolve_ports(current.get('ports', []), add_ports, remove_ports)))


def begin(state: Path, decision: Decision, now: float) -> None:
    """Publish the authorized predecessor under the caller's workspace lock."""
    state = Path(state)
    if _digest(state / arming.ARMING) != decision.arming_sha256:
        raise ValueError(CHANGED)
    atomic_write(state / INTENT, json.dumps(dict(asdict(decision), at=now), sort_keys=True).encode() + b'\n')


def commit(primary, state: Path, now: float) -> str:
    """Publish consent once the binding and registry have moved, with the lock held."""
    state = Path(state)
    intent = read_json(state / INTENT)
    ports = list(intent['ports'])
    if intent['carry']:
        current = arming.read(state)
        options = {'ports': ports} if ports != current.get('ports', []) else {}
        line = arming.move_consent(state, current, project_name=primary.root.name,
                                  new=intent['new'], now=now, **options)
    else:
        text = arming.consent_text(primary.root.name, intent['new'], intent['billable_owner'])
        if hashlib.sha256(text.encode()).hexdigest() != intent['consent_sha256']:
            raise ValueError('Recovery consent text changed; run task work:cs:armed to confirm it again.')
        arming.arm(state, gh_user=intent['gh_user'], codespace=intent['new'], consent=text,
                   billable_owner=intent['billable_owner'], ports=ports, now=now)
        line = (f'Armed as {intent["gh_user"]} for {intent["new"]}; ports '
                + (' '.join(str(port + 10000) for port in ports) or 'none'))
    (state / INTENT).unlink()
    return line


def finish_pending(project, state: Path) -> None:
    """Respect later owner edits while finishing a crash-interrupted publication."""
    state = Path(state)
    # Ordinary connects with no journal stay read-only until workspace.remote.
    if not (state / INTENT).exists():
        return
    with locked(state / 'workspace-control.lock', blocking=True):
        try:
            intent = read_json(state / INTENT)
        except FileNotFoundError:
            return
        current = arming.read(state)
        if (current.get('codespace') == intent['new']
                and current.get('consent_sha256') == intent['consent_sha256']):
            (state / INTENT).unlink()
            return
        try:
            binding = read_json(state / scratch.CONFIG)
        except FileNotFoundError:
            binding = {}
        name = binding.get('codespace')
        if name == intent['new'] and _digest(state / arming.ARMING) == intent['arming_sha256']:
            value = workspace.registry(project, state)
            primary = workspace._project(value['local_root'], project.identity)
            print(commit(primary, state, now=time.time()))
            return
        (state / INTENT).unlink()
        if name == intent['old']:
            print(f'The move to {intent["new"]} did not finish; recovery stays on {intent["old"]}.')
        elif name == intent['new']:
            print(f'The move to {intent["new"]} was interrupted and recovery settings changed since; '
                  f'run task work:cs:armed to arm {intent["new"]}.')
