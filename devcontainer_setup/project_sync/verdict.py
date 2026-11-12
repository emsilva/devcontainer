"""Pure, per-category deletion judgments and bounded, content-free output."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import shlex

from . import forget, overview, pushed, replacing, seal, sessions, standing, sync_cycle, target_scan
from .seal import Seal
from .state import read_json
from .transport import _name


SAFE, UNSAFE, UNKNOWN = 'safe', 'unsafe', 'unknown'
CATEGORIES = ('Git', 'Scratch', 'History', 'Agents')
NOT_CHECKED = ('files Git ignores', 'home-folder data outside sync', 'Grok and OpenCode conversations')
_SYNC_STOPPED = 'sync is not running; run task work:cs, then check again'
_NO_CYCLE = 'sync has not finished a cycle since the check began; check again in a minute'


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    remote_root: str | None
    seal: Seal | None
    archive: Path | None


@dataclass(frozen=True)
class Evidence:
    scan: target_scan.Scan
    steady: bool
    proofs: tuple[pushed.Proof, ...]
    mac_scratch: tuple[tuple[str, str], ...] | None
    conflicts: tuple[str, ...]
    sync_running: bool
    cycle: dict | None
    receipt: dict | None
    sessions_by_file: tuple[tuple[str, str, str], ...]
    capture_loads: bool | None
    kept: tuple[tuple[str, str], ...]
    incomplete: tuple[str, ...] = ()


@dataclass(frozen=True)
class Category:
    name: str
    verdict: str
    text: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    target: Target
    checked_at: float
    account: str | None
    repository: str | None
    categories: tuple[Category, ...]
    kept: tuple[tuple[str, str], ...]
    fingerprint: str | None
    action: str | None

    @property
    def deletable(self):
        return (tuple(row.name for row in self.categories) == CATEGORIES
                and all(row.verdict == SAFE for row in self.categories) and self.fingerprint is not None)


def _clean(value):
    return overview.fit(value, max(78, len(value) * 2))


def _category(name, verdict, text, items=()):
    prefix = {SAFE: '', UNSAFE: 'Not safe: ', UNKNOWN: 'Unknown: '}[verdict]
    return Category(name, verdict, prefix + _clean(text), tuple(_clean(item) for item in items))


def _quantity(count, singular, plural=None):
    return str(count) + ' ' + (singular if count == 1 else plural or singular + 's')


def _listed(items):
    items = list(items)
    return ', '.join(items[:3]) + (f' and {len(items) - 3} more' if len(items) > 3 else '')


def _when(stamp, now):
    return overview._when(stamp, now)


def _path(path):
    try:
        return str(Path(path).relative_to('/workspaces'))
    except ValueError:
        return path


def resolve(project, state, name: str | None) -> Target:
    state = Path(state)
    try:
        binding = read_json(state / 'scratch.json')
    except FileNotFoundError:
        binding = None
    if binding is not None and binding.get('identity') != project.identity:
        raise ValueError('Codespace pairing belongs to another project')
    if name is None:
        if not binding:
            raise ValueError('No Codespace is paired. Name one: task work:cs:delete -- --codespace NAME')
        name = binding.get('codespace')
    name = _name(name)
    if binding and binding.get('codespace') == name:
        return Target(name, 'paired', binding.get('remote_root'), None, None)
    record = seal.read(state, name)
    if record is not None and record.identity != project.identity:
        raise ValueError('The final-flush record for ' + name + ' cannot be read')
    newest = None
    remote_root = None
    for archive in sorted((state / 'retired-bindings').glob('*'), reverse=True):
        try:
            previous = read_json(archive / 'scratch.json')
        except FileNotFoundError:
            continue
        if previous.get('codespace') == name and previous.get('identity') == project.identity:
            newest, remote_root = archive, previous.get('remote_root')
            break
    if record is not None:
        return Target(name, 'sealed', record.remote_root, record, newest)
    if newest is not None:
        return Target(name, 'moved', remote_root, None, newest)
    journal = replacing.naming_new(state, name)
    if journal is not None:
        if journal['repository'].casefold() != project.identity.removeprefix('github.com/').casefold():
            raise ValueError('The replace record belongs to another project')
        return Target(name, 'replacement', journal['remote_root'], None, None)
    return Target(name, 'unpaired', None, None, None)


def claims(scan) -> tuple[pushed.Claim, ...]:
    result = []
    for repository in scan.repositories:
        refs = list(repository.branches)
        for tree in repository.worktrees:
            ref = (tree.branch or 'HEAD', tree.head)
            if tree.head is not None and ref not in refs:
                refs.append(ref)
        result.append(pushed.Claim(repository.path, repository.identity, tuple(refs)))
    return tuple(result)


def _manifest_files(manifest):
    """One mapping for prefix checks and their display identities, own or inherited."""
    result = {}
    try:
        conversations = manifest['conversations']
        entries = conversations['entries']
        completed = conversations.get('completed_files', [])
        if not isinstance(entries, list) or not isinstance(completed, list):
            raise ValueError()
        for entry in [*entries, *completed]:
            agent, owner = entry['agent'], entry['session_id']
            if agent not in ('codex', 'claude') or not isinstance(owner, str) or not owner:
                raise ValueError()
            for item in entry['files']:
                name, session, size, digest = (item[key] for key in ('path', 'session_id', 'size', 'sha256'))
                if (not isinstance(name, str) or Path(name).name != name or name in ('.', '..')
                        or not isinstance(session, str) or not session or type(size) is not int or size < 0
                        or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
                    raise ValueError()
                key, own = (agent, name), session == owner
                previous = result.get(key)
                if previous is not None:
                    old, old_session = previous
                    if old_session != session:
                        raise ValueError()
                    # An entry's own completed cutoff takes precedence over an
                    # ancestor prefix. Among dependency prefixes keep the longest.
                    if old.own and not own:
                        continue
                    if old.own == own:
                        if ((old.size == size and old.sha256 != digest)
                                or (own and (old.size, old.sha256) != (size, digest))):
                            raise ValueError()
                        if old.size >= size:
                            continue
                result[key] = (target_scan.Cutoff(size, digest, own), session)
    except (TypeError, KeyError, ValueError):
        raise ValueError('Conversation cutoffs cannot be read') from None
    return result


def cutoffs(manifest) -> dict[tuple[str, str], target_scan.Cutoff]:
    return {key: value[0] for key, value in sorted(_manifest_files(manifest).items())}


def sessions_by_file(manifest) -> tuple[tuple[str, str, str], ...]:
    return tuple((*key, value[1]) for key, value in sorted(_manifest_files(manifest).items()))


def unreachable(target, reason) -> tuple[Category, ...]:
    return tuple(_category(name, UNKNOWN, reason) for name in CATEGORIES)


def _git(scan, proofs):
    unsafe, unknown = [], []
    if not scan.git_complete or any(not repo.ok or any(not tree.ok for tree in repo.worktrees)
                                    for repo in scan.repositories):
        unknown.append('could not read every repository under /workspaces')
    for repo in scan.repositories:
        for tree in repo.worktrees:
            if tree.changes:
                unsafe.append(f'{_path(tree.path)}: {_quantity(tree.changes, "changed file")} '
                              f'({_listed(tree.changed)})')
        if repo.stashes:
            unsafe.append(f'{_path(repo.path)}: {_quantity(repo.stashes, "stash", "stashes")}')
        if repo.path + ': its origin cannot be read' in scan.problems:
            unknown.append(_path(repo.path) + ': its origin cannot be read')
    expected = {claim.path: claim for claim in claims(scan)}
    seen = set()
    allowed_reasons = {'GitHub could not be read from this Mac', 'not a GitHub repository',
                       "this Mac's checkout is shallow or partial"}
    for proof in proofs:
        if proof.path not in expected or proof.path in seen:
            unknown.append('could not read every repository under /workspaces')
            continue
        seen.add(proof.path)
        path = _path(proof.path)
        if proof.no_remote:
            unsafe.append(path + ': no remote, so its commits exist only there')
        unsafe.extend(f'{path}: {ref} at {oid[:12]} is not on GitHub' for ref, oid in proof.unpushed)
        for ref, reason in proof.unknown:
            if reason not in allowed_reasons and not re.fullmatch(
                    r'this Mac has no checkout of [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+ to compare', reason):
                reason = 'GitHub could not be read from this Mac'
            unknown.append(f'{path}: {ref}: {reason}')
        if (not proof.no_remote and proof.pushed + len(proof.unpushed) + len(proof.unknown)
                != len(expected[proof.path].refs)):
            unknown.append('could not read every repository under /workspaces')
    if set(expected) != seen:
        unknown.append('could not read every repository under /workspaces')
    unsafe, unknown = list(dict.fromkeys(unsafe)), list(dict.fromkeys(unknown))
    if unsafe or unknown:
        return _category('Git', UNSAFE if unsafe else UNKNOWN, '; '.join((unsafe + unknown)[:3]) +
                         (f' and {len(unsafe + unknown) - 3} more' if len(unsafe + unknown) > 3 else ''), unsafe + unknown)
    if not scan.repositories:
        return _category('Git', SAFE, 'no repositories under /workspaces')
    trees = sum(len(repo.worktrees) for repo in scan.repositories)
    return _category('Git', SAFE, f'{_quantity(len(scan.repositories), "repository", "repositories")}, '
                     f'{_quantity(trees, "worktree")}: no changes, stashes or unpushed commits')


def _scratch(target, evidence, now):
    scan = evidence.scan
    if target.kind == 'moved':
        return _category('Scratch', UNKNOWN, 'moved with --rebind, so no final flush was recorded')
    if not scan.scratch_complete:
        return _category('Scratch', UNKNOWN, 'could not read .scratch on ' + target.name)
    if target.kind == 'replacement':
        paths = tuple(path for path, _ in scan.scratch)
        if paths:
            return _category('Scratch', UNSAFE, _quantity(len(paths), 'file') +
                             (' exists' if len(paths) == 1 else ' exist') + ' only on ' + target.name +
                             ' (' + _listed(paths) + ')', paths)
        return _category('Scratch', SAFE, 'no files in .scratch')
    if target.kind == 'paired':
        if not evidence.sync_running:
            return _category('Scratch', UNKNOWN, _SYNC_STOPPED)
        if evidence.conflicts:
            return _category('Scratch', UNSAFE, _quantity(len(evidence.conflicts), 'conflict') +
                             ' (' + _listed(evidence.conflicts) + ')', evidence.conflicts)
        if evidence.mac_scratch is None:
            return _category('Scratch', UNKNOWN, 'could not read .scratch on ' + target.name)
        saved = set(evidence.mac_scratch)
        suffix = "differ from this Mac's copy"
        fine = 'synchronized at ' + _when(scan.at, now)
    else:
        if target.seal is None:
            return _category('Scratch', UNKNOWN, 'the final-flush record cannot be read')
        saved = set(target.seal.scratch)
        suffix = 'changed or added since the final flush'
        fine = 'unchanged since the final flush at ' + _when(target.seal.sealed_at, now)
    changed = tuple(path for path, digest in scan.scratch if (path, digest) not in saved)
    if changed:
        if len(changed) == 1:
            suffix = suffix.replace('differ from', 'differs from')
        return _category('Scratch', UNSAFE, _quantity(len(changed), 'file') + ' ' + suffix +
                         ' (' + _listed(changed) + ')', changed)
    return _category('Scratch', SAFE, fine + ' (' + _quantity(len(scan.scratch), 'file') + ')')


def _history(target, evidence, now):
    scan = evidence.scan
    if target.kind == 'moved':
        return _category('History', UNKNOWN, 'moved with --rebind, so no final flush was recorded')
    if not scan.sessions_complete:
        return _category('History', UNKNOWN, 'could not read every conversation on ' + target.name)
    if target.kind == 'replacement':
        paths = tuple(path for path, _ in scan.sessions)
        if paths:
            return _category('History', UNSAFE, _quantity(len(paths), 'conversation file') +
                             (' exists' if len(paths) == 1 else ' exist') + ' only on ' + target.name, paths)
        return _category('History', SAFE, 'no conversations on ' + target.name)
    if target.kind == 'sealed':
        if target.seal is None:
            return _category('History', UNKNOWN, 'the final capture is no longer on this Mac')
        saved = set(target.seal.sessions)
        changed = tuple(path for path, digest in scan.sessions if (path, digest) not in saved)
        if changed:
            return _category('History', UNSAFE, _quantity(len(changed), 'conversation file') +
                             ' changed since the final capture', changed)
        if evidence.capture_loads is not True:
            return _category('History', UNKNOWN, 'the final capture is no longer on this Mac')
        return _category('History', SAFE, 'no turn newer than the final capture at ' + _when(target.seal.sealed_at, now))
    if not evidence.sync_running:
        return _category('History', UNKNOWN, _SYNC_STOPPED)
    cycle = evidence.cycle
    if cycle is None:
        return _category('History', UNKNOWN, _NO_CYCLE)
    if cycle.get('outcome') == 'failed':
        return _category('History', UNKNOWN, 'the last sync cycle failed; check again in a minute')
    # summary supplies receipt validation, but its whole-cycle healthy flag also
    # includes outbound refusals. Those conversations already live on this Mac.
    inbound = cycle.get('directions', {}).get('codespace_to_desktop', {})
    scoped = dict(cycle, directions={'codespace_to_desktop': inbound})
    row = sync_cycle.summary(scoped, current=True, local_receipt=evidence.receipt)['directions']['codespace_to_desktop']
    # summary resets pending to captured when custody is unproved. That is a
    # health projection, not evidence of conversations still awaiting transfer.
    pending = inbound.get('counts', {}).get('pending', 0)
    if pending > 0:
        return _category('History', UNSAFE, _quantity(pending, 'conversation') +
                         (' has' if pending == 1 else ' have') + ' not reached this Mac yet')
    issues = row.get('capture_issues', [])
    if issues:
        items = []
        for issue in issues:
            agent = sessions.display_text(issue.get('agent'))
            session = sessions.display_text(issue.get('session_id'))
            if agent and session:
                items.append(agent + ' ' + session)
        names = ' (' + _listed(items) + ')' if items else ''
        unfinished = {'No validated completed native turn yet', 'No validated completed Claude turn yet'}
        if all(issue.get('reason') in unfinished for issue in issues):
            count = len(issues)
            return _category('History', UNSAFE, _quantity(count, 'conversation') + ' on ' + target.name +
                             (' has an unfinished turn' if count == 1 else ' have unfinished turns') + names +
                             ('; resume and finish it' if count == 1 else '; resume and finish them') +
                             ', then check again', items)
        return _category('History', UNSAFE, _quantity(len(issues), 'conversation') +
                         ' could not be read on ' + target.name + names, items)
    if (row.get('outcome') != 'complete' or not sync_cycle.receipt_matches(row.get('receipt'), row.get('snapshot_id'))
            or row.get('receipt') != evidence.receipt or cycle.get('completed_at') is None):
        return _category('History', UNKNOWN, _NO_CYCLE)
    ids = {(agent, name): session for agent, name, session in evidence.sessions_by_file}
    for keys, text in ((scan.mismatched, "differ from this Mac's copy"),
                       (scan.tails, 'have a turn that never finished')):
        if keys:
            items = tuple(agent + ' ' + ids.get((agent, name), name) for agent, name in keys)
            suffix = '; resume each and let it finish, then check again' if keys is scan.tails else ''
            if len(keys) == 1:
                text = text.replace('differ from', 'differs from').replace('have a turn', 'has a turn')
            return _category('History', UNSAFE, _quantity(len(keys), 'conversation') + ' ' + text +
                             ' (' + _listed(items) + ')' + suffix, items)
    return _category('History', SAFE, 'nothing waiting to reach this Mac')


def _agents(target, scan):
    if not scan.agents_complete:
        return _category('Agents', UNKNOWN, 'could not read processes on ' + target.name)
    counts = dict(scan.agents)
    running = tuple(f'{agent} ({counts.get(agent, 0)})' for agent in ('claude', 'codex', 'grok', 'opencode')
                    if counts.get(agent, 0) > 0)
    if running:
        return _category('Agents', UNSAFE, _listed(running) + ' running: end them, then check again', running)
    return _category('Agents', SAFE, 'none running')


def incomplete(scan) -> tuple[str, ...]:
    flags = (scan.git_complete and all(repo.ok and all(tree.ok for tree in repo.worktrees)
                                      for repo in scan.repositories),
             scan.scratch_complete, scan.sessions_complete, scan.agents_complete)
    return tuple(name for name, complete in zip(CATEGORIES, flags) if not complete)


def judge(target, evidence, *, now) -> tuple[Category, ...]:
    if target.kind == 'unpaired':
        return unreachable(target, 'this Mac has never connected to ' + target.name + '; pair it first')
    unreadable = set(evidence.incomplete) | set(incomplete(evidence.scan))
    if unreadable:
        reasons = ('could not read every repository under /workspaces',
                   'could not read .scratch on ' + target.name,
                   'could not read every conversation on ' + target.name,
                   'could not read processes on ' + target.name)
        return tuple(_category(name, UNKNOWN, reason if name in unreadable else
                               'not judged until every part of ' + target.name + ' can be read')
                     for name, reason in zip(CATEGORIES, reasons))
    if not evidence.steady:
        return unreachable(target, target.name + ' changed while it was being checked; check again when nothing is running on it')
    return (_git(evidence.scan, evidence.proofs), _scratch(target, evidence, now),
            _history(target, evidence, now), _agents(target, evidence.scan))


def kept_conversations(state, project, binding) -> tuple[tuple[str, str], ...]:
    refusals = standing.refusals(state, project, binding)
    return tuple(sorted({(row.agent, row.session_id) for row in refusals} | set(forget.forgotten(state))))


def wrap(message) -> list[str]:
    """Keep all sanitized text, wrapping by terminal cells at its text column."""
    message = _clean(message)
    if overview.cells(message) <= 78:
        return [message]
    category = message.startswith(tuple(f'  {name:<8} ' for name in CATEGORIES))
    prefix = message[:11] if category else ''
    continuation = ' ' * (11 if category else 2)
    result, line = [], prefix
    for word in re.split(r'(?<! ) (?! )', message[len(prefix):]):
        separator = ' ' if line != prefix else ''
        if overview.cells(line + separator + word) <= 78:
            line += separator + word
            continue
        if line != prefix:
            result.append(line)
            prefix = continuation
            line = prefix
        available = 78 - len(prefix)
        while overview.cells(word) > available:
            used, end = 0, 0
            for index, char in enumerate(word):
                size = overview.cells(char)
                if used + size > available:
                    break
                used += size
                end = index + 1
            result.append(line + word[:end])
            word = word[end:]
            prefix = continuation
            line = prefix
            available = 78 - len(prefix)
        line += word
    if line != prefix:
        result.append(line)
    return result


def lines(verdict, local, *, check=False) -> list[str]:
    result = [f'Checked {verdict.target.name} at {_when(verdict.checked_at, verdict.checked_at)}:']
    for category in verdict.categories:
        text = category.text
        if text == 'Unknown: ' + _SYNC_STOPPED:
            text = text.replace('task work:cs', shlex.join(overview.task_command(local, 'work:cs')))
        result.append(f'  {category.name:<8} {text}')
    if verdict.kept:
        result.append('Kept on this Mac only: ' + _listed(agent + ' ' + session for agent, session in verdict.kept))
    result.extend(['Not checked: ' + ', '.join(NOT_CHECKED[:2]) + ',', NOT_CHECKED[2] + '.'])
    if verdict.action:
        result.append(verdict.action)
    if verdict.deletable:
        result.append('To delete it: ' + shlex.join(overview.task_command(local, 'work:cs:delete', '--',
                      '--codespace', verdict.target.name)) if check else 'Type delete to delete it.')
    else:
        result.append('Nothing was deleted. Resolve the lines marked Not safe or Unknown, then check again.')
    return [part for line in result for part in wrap(line)]


def as_json(verdict, *, outcome='checked', message=None, phase=None, cleanup=None) -> dict:
    if outcome not in ('checked', 'unreachable', 'cleanup-pending', 'cleaned', 'already-deleted', 'refused'):
        raise ValueError('Invalid verdict outcome')
    observed = outcome in ('checked', 'unreachable')
    categories = verdict.categories if observed else ()
    if outcome == 'unreachable':
        categories = unreachable(verdict.target, message) if message else tuple(
            _category(row.name, UNKNOWN, row.text.removeprefix('Unknown: ')) for row in categories)
    return dict(schema=1, outcome=outcome, message=None if outcome == 'checked' else message,
                codespace=_clean(verdict.target.name), target=verdict.target.kind,
                checked_at=datetime.fromtimestamp(verdict.checked_at, timezone.utc).isoformat(),
                account=verdict.account, repository=verdict.repository,
                categories=[dict(name=row.name.lower(), verdict=row.verdict, text=_clean(row.text),
                                 items=[_clean(item) for item in row.items[:100]]) for row in categories],
                kept_on_this_mac_only=[dict(agent=_clean(agent), session_id=_clean(session))
                                      for agent, session in verdict.kept[:100]],
                not_checked=list(NOT_CHECKED), deletable=outcome == 'checked' and verdict.deletable,
                fingerprint=verdict.fingerprint if observed else None, phase=phase, cleanup=cleanup)
