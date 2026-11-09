"""Turn immutable project facts into bounded lines and explicit, safe argv actions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import shlex
import time
import unicodedata

from .probe import Remote, Space
from .standing import Local


_UNSAFE = frozenset(('Cc', 'Cf', 'Zl', 'Zp'))


def cells(text: str) -> int:
    """Count terminal cells, treating combining marks as zero and wide text as two."""
    return sum(0 if unicodedata.category(char).startswith('M') else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in text)


def fit(text: str, width: int = 78) -> str:
    """Drop terminal controls and fit the remaining text with a final ellipsis."""
    clean = ''.join(char for char in text if unicodedata.category(char) not in _UNSAFE)
    if width <= 0:
        return ''
    if cells(clean) <= width:
        return clean
    result, used = [], 0
    for char in clean:
        size = cells(char)
        if used + size > width - 1:
            break
        result.append(char)
        used += size
    return ''.join(result) + '…'


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    argv: tuple[str, ...]

    @property
    def command(self) -> str:
        return shlex.join(self.argv)

    @property
    def item(self) -> str:
        return f'{fit(self.label)}  ({self.command})'


@dataclass(frozen=True)
class View:
    lines: tuple[str, ...]
    actions: tuple[Action, ...]


def task_command(local: Local, alias: str, *args: str) -> tuple[str, ...]:
    if local.generated and (alias == 'setup' or alias not in (local.managed or ())):
        alias = 'devcontainer:' + alias
    return ('task', alias, *args)


def next_lines(view: View) -> list[str]:
    return ['Next: ' + action.command for action in view.actions]


def normalize(raw: str) -> str:
    if raw == 'Available':
        return 'available'
    if raw == 'Shutdown':
        return 'stopped'
    if raw == 'ShuttingDown':
        return 'stopping'
    if raw in ('Starting', 'Provisioning', 'Queued', 'Rebuilding', 'Awaiting', 'Exporting', 'Updating'):
        return 'starting'
    if raw in ('Failed', 'Unavailable', 'Archived'):
        return 'failed'
    if raw == 'Deleted':
        return 'deleted'
    return 'other'


def _when(stamp: float, now: float) -> str:
    date = datetime.fromtimestamp(stamp)
    return date.strftime('%H:%M' if date.date() == datetime.fromtimestamp(now).date() else '%Y-%m-%d %H:%M')


@dataclass(frozen=True)
class _Pair:
    kind: str
    raw: str = ''
    suffix: str = ''
    at: float | None = None
    space: Space | None = None
    reason: str = ''


def _pair(local: Local, remote: Remote | None, now: float) -> _Pair:
    if remote is not None and remote.spaces.status == 'ok':
        for space in remote.spaces.value:
            if space.name == local.pairing:
                at = remote.spaces.at
                suffix = 'checked ' + _when(at, now) if at is not None else 'check time unavailable'
                return _Pair(normalize(space.state), space.state, suffix, at, space)
    if remote is not None and remote.names.status == 'ok':
        if local.pairing in remote.names.value:
            return _Pair('elsewhere')
        if (remote.account.status == 'ok' and local.gh_user is not None
                and remote.account.value == local.gh_user):
            return _Pair('deleted', at=remote.names.at)
    if local.deleted_at is not None:
        return _Pair('keeper-deleted', at=local.deleted_at)
    if remote is not None and remote.names.status == 'ok':
        return _Pair('missing')
    if local.reading is not None:
        raw, at = local.reading
        return _Pair(normalize(raw), raw, 'as of ' + _when(at, now), at)
    if remote is None:
        return _Pair('unknown', reason='checking GitHub…')
    reason = remote.spaces.reason or remote.names.reason or 'GitHub state is unknown'
    if reason == 'no answer within 2 s':
        reason = 'GitHub did not answer within 2 s'
    return _Pair('unknown', reason=reason)


def _stop(local: Local, pair: _Pair) -> tuple[str | None, float | None]:
    def later(stamp):
        return ((local.started_at is not None and local.started_at > stamp)
                or (pair.space is not None and pair.space.last_used_at is not None
                    and pair.space.last_used_at > stamp))

    if local.keeper == 'running' and local.down_kind in ('OWNER', 'LIFETIME', 'OTHER') and local.down_at is not None:
        return (None, None) if later(local.down_at) else (local.down_kind, local.down_at)
    if local.owner_stop_at is not None and not later(local.owner_stop_at):
        return 'OWNER', local.owner_stop_at
    return None, None


def view(local: Local, remote: Remote | None, *,
         forgotten: frozenset[tuple[str, str]] = frozenset(), now: float | None = None) -> View:
    """Choose one primary situation, then rank overlays without reading or changing state."""
    now = time.time() if now is None else now
    facts: list[tuple[int, str]] = []
    candidates: list[Action] = []

    def line(rank, text):
        facts.append((rank, text))

    def action(key, label, alias, *args):
        candidates.append(Action(key, label, task_command(local, alias, *args)))

    if local.in_codespace:
        return View((fit("This is the Codespace; task work shows the project's state on your Mac."),),
                    (Action('stop', 'Stop the Codespace', task_command(local, 'work:cs:stop')),))

    pair = _pair(local, remote, now)
    spaces = remote.spaces.value if remote is not None and remote.spaces.status == 'ok' else None
    prs = sorted(set(remote.setup_prs.value)) if remote is not None and remote.setup_prs.status == 'ok' else []
    launch = local.launch
    if local.setup in ('missing', 'unmanaged', 'unknown', 'partial'):
        situation = 'setup'
    elif prs:
        situation = 'prs'
    elif pair.kind == 'failed':
        situation = 'failed'
    elif launch is not None and not launch.complete and (not local.pairing or launch.codespace == local.pairing):
        situation = 'launch'
    elif not local.pairing:
        situation = 'unpaired'
    else:
        situation = pair.kind

    recovery_off = (local.armed is True and local.keeper == 'stopped'
                    and pair.kind not in ('deleted', 'keeper-deleted', 'missing', 'elsewhere'))
    if recovery_off:
        line(0, 'Recovery is off until you connect')

    name = local.pairing
    if situation == 'setup':
        line(1, 'Setup did not finish; run setup again to complete it' if local.setup == 'partial'
             else 'This project is not set up yet')
        action('setup', 'Set up this project', 'setup')
    elif situation == 'prs':
        if len(prs) == 1:
            line(1, f'Setup pull request #{prs[0]} is open; resume setup to finish it')
            action('setup', 'Resume setup', 'setup')
        else:
            line(1, f'{len(prs)} setup pull requests are open; close all but one in GitHub')
            for number in prs[:3]:
                candidates.append(Action(f'pr:{number}', f'Open setup pull request #{number}',
                                         ('gh', 'pr', 'view', str(number), '--web')))
    elif situation in ('failed', 'launch'):
        target = launch.codespace if situation == 'launch' else name
        if situation == 'failed':
            line(1, f'Codespace {target} failed on GitHub; see its creation logs')
        else:
            line(1, f'Setup of Codespace {target} did not finish validation')
        logs = Action('creation-logs', 'Show creation logs', ('gh', 'codespace', 'logs', '-c', target))
        resume = Action('resume', f'Resume setup of {target}', task_command(local, 'setup', '--', '--resume', target))
        candidates.extend((logs, resume) if situation == 'failed' else (resume, logs))
    elif situation in ('deleted', 'keeper-deleted', 'missing', 'elsewhere'):
        if situation in ('deleted', 'keeper-deleted'):
            attribution = 'confirmed' if situation == 'deleted' else 'found by the recovery keeper'
            when = _when(pair.at, now) if pair.at is not None else 'time unavailable'
            line(1, f'Codespace {name} was deleted ({attribution} {when})')
        elif situation == 'missing':
            account = remote.account.value if remote.account.status == 'ok' else 'unknown account'
            line(1, f"Codespace {name} is not among {account}'s Codespaces; it may be deleted or under another account")
        else:
            line(1, f"Codespace {name} is now listed under another repository; pair this one's Codespace")
        others = sorted((space for space in (spaces or ()) if space.name != name),
                        key=lambda space: (space.last_used_at is not None,
                                           space.last_used_at if space.last_used_at is not None else 0), reverse=True)
        for space in others[:3]:
            action('pair:' + space.name, 'Pair ' + space.name, 'work:cs', '--', '--rebind', space.name)
        action('create', 'Create a Codespace', 'setup')
    elif situation == 'unpaired':
        line(2, 'No Codespace is paired with this checkout')
        if spaces is not None:
            count = len(spaces)
            quantity = 'no Codespace' if not count else '1 Codespace' if count == 1 else f'{count} Codespaces'
            line(2, f'This repository has {quantity} on GitHub')
        if spaces is None or spaces:
            action('connect', 'Connect', 'work:cs')
        action('create', 'Create a Codespace', 'setup')
    else:
        if situation == 'stopped':
            kind, stamp = _stop(local, pair)
            if kind == 'OWNER':
                text = f'Codespace {name} is stopped; you stopped it at {_when(stamp, now)}'
            elif kind == 'LIFETIME':
                text = f"Codespace {name} was stopped by GitHub's 12-hour limit at {_when(stamp, now)}"
            elif kind == 'OTHER':
                text = f'Codespace {name} stopped at {_when(stamp, now)}, not by you'
            else:
                text = f'Codespace {name} is stopped ({pair.suffix})'
            action('start', 'Start and connect', 'work:cs')
        else:
            if situation in ('available', 'starting', 'stopping'):
                text = f'Codespace {name} is {situation} ({pair.suffix})'
            elif situation == 'other':
                text = f'Codespace {name} is in state {pair.raw} ({pair.suffix})'
            else:
                text = f'Codespace {name}: {pair.reason}'
            action('connect', 'Connect', 'work:cs')
            if situation == 'available':
                if local.armed is False:
                    action('arm', 'Connect and arm recovery', 'work:cs:armed')
                action('stop', 'Stop the Codespace', 'work:cs:stop')
        line(2, text)

    refusals = tuple(refusal for refusal in local.refusals
                     if (refusal.agent, refusal.session_id) not in forgotten)
    if refusals:
        count = len(refusals)
        quantity, pronoun = ('1 conversation', 'it') if count == 1 else (f'{count} conversations', 'them')
        line(3, f'{name} refused {quantity}; forget {pronoun} to clear the sync warning')
        for refusal in refusals[:2]:
            action(f'forget:{refusal.agent}:{refusal.session_id}',
                   f'Forget {refusal.agent} conversation {refusal.session_id[:8]}',
                   'work:forget', '--', '--agent', refusal.agent, '--session', refusal.session_id)
    if local.keeper == 'stale':
        duration = (f' for {int(local.keeper_heartbeat_age // 60)} min'
                    if local.keeper_heartbeat_age is not None else '')
        line(4, f'The recovery keeper has not answered{duration}; connect to restart it')
    elif local.keeper == 'unknown':
        line(4, 'Recovery state is unknown (process check timed out)')
    if name and local.worker == 'stopped':
        line(4, 'Sync is stopped; connect to start it')
    if local.armed is True and local.keeper == 'running':
        text = f'Recovery is armed as {local.gh_user or "unknown account"}'
        if local.hold_until is not None and local.hold_until > now:
            text += ', on hold until ' + _when(local.hold_until, now)
        line(5, text)
    elif local.armed is False and name:
        line(5, 'Recovery is not armed')

    checkout = local.checkout
    if checkout.relation == 'behind' and checkout.branch == checkout.default:
        copied = 'local copy' + (' from ' + _when(checkout.copied_at, now) if checkout.copied_at is not None else '')
        line(7, f'Checkout is {checkout.behind} commits behind origin/{checkout.default} ({copied})')
        candidates.append(Action('update-checkout', 'Update the checkout',
                                 ('git', '-C', str(local.root), 'merge', '--ff-only', 'origin/' + checkout.default)))
    elif checkout.relation == 'diverged':
        line(7, f'Checkout and origin/{checkout.default} have diverged; review before merging')
    if (local.generated and local.setup == 'installed' and local.source_ref is not None
            and remote is not None and remote.upstream.status == 'ok' and remote.upstream.value != local.source_ref):
        # The probe exposes the SHA only, not the configured symbolic ref's name.
        line(6, f'Generator upstream is at {remote.upstream.value[:7]}; this project uses {local.source_ref[:7]}')
        candidates.append(Action('upgrade', 'Review the generator upgrade', ('task', 'devcontainer:upgrade:self')))
    if local.cycle_at is not None:
        line(8, 'Sync last completed ' + _when(local.cycle_at, now))

    details = Action('details', 'Show details', task_command(local, 'work:status'))
    more = details.command + ' shows more'
    for problem in local.problems:
        line(1, problem + '; ' + more)
    unique = {}
    for candidate in candidates:
        if any(unicodedata.category(char) in _UNSAFE for arg in candidate.argv for char in arg):
            line(1, f'Action {candidate.key} withheld for unsafe text; {more}')
        else:
            unique.setdefault(candidate.key, candidate)
    actions = list(unique.values())[:5]
    if recovery_off:
        actions.sort(key=lambda candidate: candidate.key not in ('connect', 'start'))
    if len(actions) < 3:
        actions.append(Action('logs', 'Show recovery and sync logs', task_command(local, 'work:logs')))
    actions.append(details)
    lines = tuple(fit(text) for _, text in sorted(facts, key=lambda fact: fact[0])[:6])
    return View(lines, tuple(actions))
