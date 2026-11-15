"""Turn immutable project facts into bounded lines and explicit, safe argv actions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import unicodedata

from . import forget, probe, replacing, standing, terminal
from .probe import Remote, Space
from .standing import Local


_UNSAFE = frozenset(('Cc', 'Cf', 'Zl', 'Zp'))
DEFAULT = object()


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
        return fit(self.label)


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


_ROWS = 5  # under the title line: #87's six lines
_SLOTS = 5  # menu actions before Show details; run adds Exit
_GONE = ('deleted', 'keeper-deleted', 'missing', 'elsewhere')  # the pairing is not there to recover


def _clean(text: str) -> str:
    return ''.join(char for char in text if unicodedata.category(char) not in _UNSAFE)


def _title(folder: str, name: str | None) -> str:
    """Name the Codespace once; a long line cuts the folder, never the name."""
    tail = ' · ' + (_clean(name) if name else 'no Codespace paired')
    folder = _clean(folder)
    if cells(folder + tail) > 78:
        folder = fit(folder, max(1, 78 - cells(tail)))
    return fit(folder + tail)


def _row(label: str, text: str) -> str:
    return fit(f'{label:<9}  {text}')


def _names(labels: tuple[str, ...]) -> str:
    return labels[0] if len(labels) == 1 else ', '.join(labels[:-1]) + ' and ' + labels[-1]


def _recovery(local: Local, situation: str, pair: _Pair, now: float) -> tuple[int, str] | None:
    """The Recovery row's keep rank and text, first match wins; None draws no row."""
    if local.armed is None:
        return None  # The Problem row carries arming.json cannot be read.
    if local.armed is False:
        return 12, 'not armed'
    if local.keeper == 'stale':
        age = local.keeper_heartbeat_age
        return 6, (f'not answering for {int(age // 60)} min; connect to restart it' if age is not None
                   else 'not answering; connect to restart it')
    if local.keeper == 'unknown':
        return 6, 'state unknown (process check timed out)'
    if local.keeper == 'stopped' and pair.kind in _GONE:
        return None
    stopped = situation == 'stopped'
    kind = _stop(local, pair)[0] if stopped else None
    if local.hold_until is not None and local.hold_until > now:
        return (6 if stopped else 12), 'armed; on hold until ' + _when(local.hold_until, now)
    if stopped:
        if local.keeper_phase == 'restarting':
            return 6, 'armed; restarting it now'
        if kind == 'OWNER':
            return 12, 'armed; not restarting: you stopped it'
        if local.hold_at_stop is not None:
            return 6, 'armed; not restarting: it stopped during your hold'
        if local.restarts_free_at is not None and local.restarts_free_at > now:
            return 6, 'armed; out of restarts until ' + _when(local.restarts_free_at, now)
        if kind == 'OTHER':
            return 6, 'armed; not restarting: this was not the 12-hour stop'
        if kind == 'LIFETIME':
            return 6, 'armed; not restarted yet; Show details has the reason'
        return 6, 'armed; checking why it stopped'
    if local.next_stop_at is None:
        return 12, 'armed; next 12-hour stop not known yet'
    if local.next_stop_at > now:
        return 12, 'armed; next 12-hour stop about ' + _when(local.next_stop_at, now)
    return 12, 'armed; the 12-hour stop was due about ' + _when(local.next_stop_at, now)


def view(local: Local, remote: Remote | None, *,
         forgotten: frozenset[tuple[str, str]] = frozenset(), now: float | None = None) -> View:
    """Name the Codespace once, then labelled rows, without reading or changing state."""
    now = time.time() if now is None else now
    rows: list[tuple[int, int, str]] = []
    candidates: list[Action] = []

    def row(place, keep, label, text):
        rows.append((place, keep, _row(label, text)))

    def action(key, label, alias, *args):
        candidates.append(Action(key, label, task_command(local, alias, *args)))

    if local.in_codespace:
        return View((fit("This is the Codespace; task work shows the project's state on your Mac."),),
                    (Action('stop', 'Stop the Codespace', task_command(local, 'work:cs:stop')),))

    pair = _pair(local, remote, now)
    spaces = remote.spaces.value if remote is not None and remote.spaces.status == 'ok' else None
    prs = sorted(set(remote.setup_prs.value)) if remote is not None and remote.setup_prs.status == 'ok' else []
    launch = local.launch
    if local.setup in ('missing', 'unmanaged', 'unknown'):
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

    name = local.pairing
    recovery_off = (local.armed is True and local.keeper == 'stopped'
                    and pair.kind not in _GONE)
    # The sync runs for the paired Codespace (standing reads it for that binding only).
    connected = bool(name) and local.worker == 'running'
    if recovery_off:
        row(1, 1, 'Recovery', 'off until you connect')
    if local.replacing is not None:
        row(2, 2, 'Replace', f'stopped at {replacing.PHASE_WORDS[local.replacing["phase"]]}; finish it')

    if situation == 'setup':
        row(5, 5, 'Setup', 'not done yet')
        action('setup', 'Set up this project', 'setup')
    elif situation == 'prs':
        if len(prs) == 1:
            row(5, 5, 'Setup', f'pull request #{prs[0]} is open; resume setup to finish it')
            action('setup', 'Resume setup', 'setup')
        else:
            row(5, 5, 'Setup', f'{len(prs)} pull requests are open; close all but one in GitHub')
            for number in prs[:3]:
                candidates.append(Action(f'pr:{number}', f'Open setup pull request #{number}',
                                         ('gh', 'pr', 'view', str(number), '--web')))
    elif situation in ('failed', 'launch'):
        target = launch.codespace if situation == 'launch' else name
        if situation == 'failed':
            row(5, 5, 'Codespace', 'failed on GitHub; see its creation logs')
        elif target == name:
            row(5, 5, 'Codespace', 'setup did not finish validation; resume it')
        else:
            row(5, 5, 'Setup', f'{target} did not finish validation')
        logs = Action('creation-logs', 'Show creation logs', ('gh', 'codespace', 'logs', '-c', target))
        resume = Action('resume', 'Resume the Codespace setup',
                        task_command(local, 'setup', '--', '--resume', target))
        candidates.extend((logs, resume) if situation == 'failed' else (resume, logs))
    elif situation in _GONE:
        if situation in ('deleted', 'keeper-deleted'):
            attribution = 'confirmed' if situation == 'deleted' else 'found by Recovery'
            when = ' ' + _when(pair.at, now) if pair.at is not None else ''
            row(5, 5, 'Codespace', f'deleted ({attribution}{when})')
        elif situation == 'missing':
            account = remote.account.value if remote.account.status == 'ok' else 'your account'
            row(5, 5, 'Codespace', f'not listed for {account}; deleted, or under another account')
        else:
            row(5, 5, 'Codespace', "now listed under another repository; pair this one's Codespace")
        others = sorted((space for space in (spaces or ()) if space.name != name),
                        key=lambda space: (space.last_used_at is not None,
                                           space.last_used_at if space.last_used_at is not None else 0), reverse=True)
        for space in others[:3]:
            action('pair:' + space.name, 'Pair ' + space.name, 'work:cs', '--', '--rebind', space.name)
        action('create', 'Create a Codespace', 'setup')
    elif situation == 'unpaired':
        if spaces is not None:
            count = len(spaces)
            row(5, 5, 'Codespace', f'GitHub lists {count or "none"} for this repository')
        if spaces is None or spaces:
            action('connect', 'Connect', 'work:cs')
        action('create', 'Create a Codespace', 'setup')
    else:
        if situation == 'stopped':
            kind, stamp = _stop(local, pair)
            if kind == 'OWNER':
                text = f'stopped; you stopped it at {_when(stamp, now)}'
            elif kind == 'LIFETIME':
                text = f"stopped by GitHub's 12-hour limit at {_when(stamp, now)}"
            elif kind == 'OTHER':
                text = f'stopped at {_when(stamp, now)}, not by you'
            else:
                text = f'stopped ({pair.suffix})'
            action('start', 'Start and connect', 'work:cs')
        else:
            if situation in ('available', 'starting', 'stopping'):
                text = f'{situation} ({pair.suffix})'
            elif situation == 'other':
                text = f'in state {pair.raw} ({pair.suffix})'
            else:
                text = pair.reason
            # Only while recovery runs or is not armed; off, not answering or unknown keeps Connect,
            # which is what restarts it.
            recovering = local.armed is False or (local.armed is True and local.keeper == 'running')
            action('connect', 'Open in Herdr' if connected and recovering else 'Connect', 'work:cs')
            if situation == 'available':
                if local.armed is False:
                    action('arm', 'Connect and arm recovery', 'work:cs:armed')
                action('stop', 'Stop the Codespace', 'work:cs:stop')
        row(5, 5, 'Codespace', text)
    # The paired Codespace is one the menu can connect to: Replace and the sign-in item need one.
    reachable = bool(name) and any(candidate.key in ('connect', 'start') for candidate in candidates)
    # Signing in also needs the Codespace up, since work:signin:all cannot start it: not while GitHub lists it
    # stopped, stopping or starting. A state GitHub did not name or a slow read keeps the item.
    signin = (Action('signin', 'Sign in to the tools', task_command(local, 'work:signin:all'))
              if connected and reachable and situation in ('available', 'other', 'unknown') else None)

    replace_action = None
    if reachable and local.replacing is None:
        replace_action = Action('replace', 'Replace this Codespace', task_command(local, 'setup', '--', '--replace'))

    if name and not recovery_off:
        recovery = _recovery(local, situation, pair, now)
        if recovery is not None:
            row(6, recovery[0], 'Recovery', recovery[1])
    if name and local.worker == 'stopped':
        row(7, 7, 'Sync', 'stopped; connect to start it')
    elif local.cycle_at is not None:
        row(7, 14, 'Sync', 'last completed ' + _when(local.cycle_at, now))

    refusals = tuple(refusal for refusal in local.refusals
                     if (refusal.agent, refusal.session_id) not in forgotten)
    if refusals:
        count = len(refusals)
        quantity, pronoun = ('1 conversation', 'it') if count == 1 else (f'{count} conversations', 'them')
        row(8, 8, 'History', f'{quantity} refused; forget {pronoun} to clear the sync warning')
        for refusal in refusals[:2]:
            action(f'forget:{refusal.agent}:{refusal.session_id}',
                   f'Forget {refusal.agent} conversation {refusal.session_id[:8]}',
                   'work:forget', '--', '--agent', refusal.agent, '--session', refusal.session_id)
    elif local.history_pending_since is not None and now - local.history_pending_since > 300:
        row(8, 8, 'History', f'not synced yet, {int((now - local.history_pending_since) // 60)} min so far')
    if local.signed_out:
        are = 'is' if len(local.signed_out) == 1 else 'are'
        unchecked = f'; {len(local.unchecked)} not checked' if local.unchecked else ''
        row(9, 9, 'Sign-ins', f'{_names(local.signed_out)} {are} not signed in{unchecked}')
    elif local.unchecked:
        row(9, 9, 'Sign-ins', f'{_names(local.unchecked)} could not be checked')

    checkout = local.checkout
    if checkout.relation in ('other-branch', 'detached'):
        tail = '; Codespaces start from ' + checkout.default
        branch = ('on ' + fit(checkout.branch or '', 78 - cells('Checkout   on ' + tail))
                  if checkout.relation == 'other-branch' else 'on a detached HEAD')
        row(11, 11, 'Checkout', branch + tail)
    elif checkout.relation == 'behind' and checkout.branch == checkout.default:
        copied = 'local copy' + (' from ' + _when(checkout.copied_at, now) if checkout.copied_at is not None else '')
        row(11, 11, 'Checkout', f'{checkout.behind} commits behind origin/{checkout.default} ({copied})')
        candidates.append(Action('update-checkout', 'Update the checkout',
                                 ('git', '-C', str(local.root), 'merge', '--ff-only', 'origin/' + checkout.default)))
    elif checkout.relation == 'diverged':
        row(11, 11, 'Checkout', f'diverged from origin/{checkout.default}; review before merging')
    if (local.generated and local.setup == 'installed' and local.source_ref is not None
            and remote is not None and remote.upstream.status == 'ok' and remote.upstream.value != local.source_ref):
        # The probe exposes the SHA only, not the configured symbolic ref's name.
        row(12, 13, 'Generator', f'upstream is at {remote.upstream.value[:7]}; this project uses {local.source_ref[:7]}')
        candidates.append(Action('upgrade', 'Review the generator upgrade', ('task', 'devcontainer:upgrade:self')))

    priority = []
    if recovery_off:
        connect = next((candidate for candidate in candidates if candidate.key in ('connect', 'start')), None)
        if connect is not None:
            candidates.remove(connect)
        else:
            connect = Action('connect', 'Connect', task_command(local, 'work:cs'))
        priority.append(connect)
    if local.replacing is not None:
        priority.append(Action('finish-replace', 'Finish the replace', task_command(local, 'setup', '--', '--replace')))
    if local.cleanup:
        target = local.cleanup[0]
        priority.append(Action('cleanup:' + target, 'Finish cleaning up after the delete',
                               task_command(local, 'work:cs:delete', '--', '--codespace', target)))
        row(3, 3, 'Cleanup', f'not finished for {target}')
    listed = {space.name for space in spaces or ()}
    unchecked_old = (local.replacing['old'] if local.replacing is not None and
                     replacing.PHASES.index(local.replacing['phase']) < replacing.PHASES.index('checked') else None)
    retired = next((target for target in local.retired
                    if target != name and target != unchecked_old and target in listed), None)
    delete = None
    if retired is not None:
        delete = Action('delete:' + retired, 'Check and delete the replaced Codespace',
                        task_command(local, 'work:cs:delete', '--', '--codespace', retired))
        row(10, 10, 'Replaced', 'still on GitHub; check and delete it when ready')

    problems = []
    unique = {}
    for candidate in [*priority, *candidates, *filter(None, (signin, replace_action, delete))]:
        if any(unicodedata.category(char) in _UNSAFE for arg in candidate.argv for char in arg):
            # Withheld first: it is why the menu lacks an action the situation calls for.
            if 'an action was withheld for unsafe text' not in problems:
                problems.insert(0, 'an action was withheld for unsafe text')
        else:
            unique.setdefault(candidate.key, candidate)
    replace_action = unique.pop('replace', None)
    signin = unique.pop('signin', None)
    delete = unique.pop(delete.key, None) if delete is not None else None
    problems += [problem for problem in local.problems if problem not in problems]
    if problems:
        tail = (f' (+{len(problems) - 1} more)' if len(problems) > 1 else '') + '; Show details has more'
        row(4, 4, 'Problem', fit(problems[0], 78 - cells('Problem    ' + tail)) + tail)

    # Replace and the retired delete keep their slots, so a busy state never hides them.
    reserved = [found for found in (replace_action, delete) if found is not None]
    actions = list(unique.values())
    # Directly above Replace, or right after the first action while a tool is signed out and a slot is free; never
    # reserved, so in a full menu it gives way before any action a row asks for.
    up = bool(local.signed_out) and len(actions) < _SLOTS - len(reserved)
    if signin is not None:
        actions.insert(1 if up else len(actions), signin)
    actions = actions[:_SLOTS - len(reserved)]
    if local.logs and len(actions) + len(reserved) < 3:
        at = actions.index(signin) if signin in actions and not up else len(actions)
        actions.insert(at, Action('logs', 'Show recovery and sync logs', task_command(local, 'work:logs')))
    actions += reserved
    actions.append(Action('details', 'Show details', task_command(local, 'work:status')))

    kept = sorted(sorted(rows, key=lambda found: (found[1], found[0]))[:_ROWS])
    lines = (_title(local.root.name, name), *(text for _, _, text in kept))
    return View(lines, _unique_labels(actions))


def _unique_labels(actions: list[Action]) -> tuple[Action, ...]:
    """Menu items are matched by text, so a repeated item gets (2), (3) that no cut can drop."""
    used, result = set(), []
    for found in actions:
        number, label = 1, found.label
        while fit(label) in used:
            number += 1
            suffix = f' ({number})'
            label = fit(found.label, 78 - cells(suffix)) + suffix
        used.add(fit(label))
        result.append(found if label == found.label else replace(found, label=label))
    return tuple(result)


def run(root: Path, *, env: Mapping[str, str] = os.environ, stdin=sys.stdin, stdout=sys.stdout,
        deadline: float = 2.0, gum=DEFAULT, call=subprocess.call) -> int:
    """Print without changing state; only an explicit menu choice runs a command."""
    handle = None
    try:
        local = standing.read(root, env=env)
        forgotten = frozenset()
        if local.state is not None:
            try:
                forgotten = forget.forgotten(local.state)
            except forget.Unreadable as error:
                local = replace(local, problems=(*local.problems, str(error)))
        if local.in_codespace:
            frame = view(local, None, forgotten=forgotten)
            print(*frame.lines, sep='\n', file=stdout, flush=True)
            print('', *next_lines(frame), sep='\n', file=stdout, flush=True)
            return 0

        request = probe.Request(local.repository, local.checkout.default, want_prs=local.generated,
                                generator=probe.generator_request(env) if local.generated else None)
        handle = probe.start(request, env=env, deadline=deadline)
        interactive = stdin.isatty() and stdout.isatty()
        if interactive:
            initial = view(local, None, forgotten=forgotten)
            print(*initial.lines, sep='\n', file=stdout, flush=True)
        remote = handle.join()
        frame = view(local, remote, forgotten=forgotten)
        if interactive:
            stdout.write(f'\x1b[{len(initial.lines)}F\x1b[J')
        print(*frame.lines, sep='\n', file=stdout, flush=True)

        if not interactive:
            print('', *next_lines(frame), sep='\n', file=stdout, flush=True)
            return 0
        if gum is DEFAULT:
            gum = terminal.installed_gum()
        if gum is None:
            print('', *next_lines(frame), sep='\n', file=stdout)
            print('Gum is not installed, so there is no menu; run one of the commands above.',
                  file=stdout, flush=True)
            return 0
        try:
            choice = terminal.Gum(gum).choose('What next?', [action.item for action in frame.actions] + ['Exit'])
        except (terminal.Cancelled, KeyboardInterrupt):
            choice = 'Exit'
        if choice == 'Exit':
            print('Nothing ran.', file=stdout, flush=True)
            return 0
        action = next(action for action in frame.actions if action.item == choice)
        print('Running: ' + action.command, file=stdout, flush=True)
        return call(list(action.argv), cwd=root)
    finally:
        # join and cancel both reap owned children; cancel is also safe after join.
        if handle is not None:
            handle.cancel()
