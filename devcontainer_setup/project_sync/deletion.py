"""Check, confirm, stop and delete one target, then replay its owned cleanup."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import time

from . import (arming, herdr, keeper, lifecycle, overview, power, pushed, restore,
               scratch, scratch_tree, ssh_hosts, standing, target_scan, verdict, workspace)
from .checkpoint_store import Store
from .state import atomic_write, locked, read_json
from .transport import CodespaceConnection


PHASES = ('confirmed', 'stop-recorded', 'stop-requested', 'shutdown', 'delete-requested', 'deleted', 'cleaned')
STEPS = {'paired': ('keeper', 'sync worker', 'pairing', 'Herdr connections', 'recovery', 'SSH alias',
                    'Herdr profile', 'sync session'),
         'sealed': ('Herdr connections', 'sync session'), 'moved': ('Herdr connections', 'sync session')}
_DONE = ('done', 'kept', 'moved')


def _optional(path):
    try:
        return read_json(path)
    except FileNotFoundError:
        return {}


def _mac_scratch(root):
    result = []
    try:
        for path, relative in scratch_tree.entries(root / '.scratch'):
            if path.is_file():
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                result.append((relative.as_posix(), digest.hexdigest()))
    except (OSError, ValueError):
        return None
    return tuple(sorted(result))


def gather(project, state, target, connection, *, now=time.time, sleep=time.sleep) -> verdict.Evidence:
    """Bracket a later inbound capture with scans, comparing its exact prefix map."""
    state = Path(state)
    first = target_scan.scan(connection, target.name, target.remote_root, now=now)
    finished = now()
    scan, steady = first, True
    cycle, receipt, sessions, capture_loads = None, None, (), None
    running, conflicts, mac = False, (), None
    binding = _optional(state / scratch.CONFIG)
    kept = verdict.kept_conversations(state, project, binding)
    if target.kind == 'paired':
        synced_root = Path(binding.get('root', project.root))
        cutoffs = None
        deadline = finished + 6 * 60
        while True:
            try:
                registry = workspace.registry(project, state)
                runtime = scratch.status(state)
                running = bool(runtime.get('running'))
                sync = scratch.inspect_sync(synced_root, state) if running else runtime
                conflicts = tuple(sync.get('conflicts', ()))
                observed, current = workspace.cycle_scope(project, registry, runtime, sync)
                started, completed = observed.get('started_at'), observed.get('completed_at')
                if (current and type(started) in (int, float) and started > finished
                        and type(completed) in (int, float)):
                    cycle = observed
                    receipt, conversations = workspace._conversation_receipt(project, state)
                    if receipt is not None:
                        manifest = {'conversations': conversations}
                        cutoffs, sessions = verdict.cutoffs(manifest), verdict.sessions_by_file(manifest)
                    break
            except (OSError, ValueError, KeyError, TypeError):
                receipt = None
                break
            if not running or now() >= deadline:
                break
            sleep(min(2, deadline - now()))
        mac = _mac_scratch(synced_root)
        scan = target_scan.scan(connection, target.name, target.remote_root, cutoffs=cutoffs, now=now)
        if cutoffs is not None and scan.cutoffs_sha256 != target_scan.cutoffs_digest(cutoffs):
            cycle = None
        first_fingerprint = target_scan.fingerprint(first)
        steady = first_fingerprint is not None and first_fingerprint == target_scan.fingerprint(scan)
    elif target.kind == 'sealed':
        try:
            manifest = Store(state).load(target.seal.snapshot_id)
            from .replication import validate_manifest
            validate_manifest(manifest, project.identity, project.project_id)
            capture_loads = True
        except (OSError, ValueError):
            capture_loads = False
    return verdict.Evidence(scan, steady, pushed.prove(project, verdict.claims(scan)), mac, conflicts,
                            running, cycle, receipt, sessions, capture_loads, kept)


def _journal(state, name):
    value = _optional(state / 'deletions' / (name + '.json'))
    if not value:
        return None
    try:
        if (value['schema'] != 1 or value['codespace'] != name or value['phase'] not in PHASES
                or value['kind'] not in STEPS or not isinstance(value['account'], str)
                or not isinstance(value['repository'], str) or not isinstance(value['at'], dict)
                or not isinstance(value['cleanup'], dict)
                or not re.fullmatch('[0-9a-f]{64}', value['fingerprint'])):
            raise ValueError
        if any(key not in PHASES or type(stamp) not in (int, float) or not math.isfinite(stamp)
               for key, stamp in value['at'].items()):
            raise ValueError
        if any(key not in STEPS[value['kind']] or not isinstance(result, str)
               for key, result in value['cleanup'].items()):
            raise ValueError
        if value['archive'] is not None:
            archive = Path(value['archive'])
            if archive.parent != state / 'retired-bindings' or archive.name in ('.', '..'):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError('The delete journal cannot be read; leave it for review.') from None
    return value


def _save(state, journal, now, phase=None):
    value = dict(journal, at=dict(journal['at']))
    if phase is not None:
        value['phase'] = phase
        value['at'][phase] = now()
    atomic_write(state / 'deletions' / (journal['codespace'] + '.json'),
                 json.dumps(value, sort_keys=True).encode() + b'\n')
    journal.update(value)


def _new_journal(target, account, repository, fingerprint):
    return dict(schema=1, codespace=target.name, kind=target.kind, account=account, repository=repository,
                fingerprint=fingerprint, phase='confirmed', at={},
                archive=str(target.archive) if target.archive else None, cleanup={})


def _archive(project, state, name):
    for archive in sorted((state / 'retired-bindings').glob('*'), reverse=True):
        previous = _optional(archive / scratch.CONFIG)
        if previous.get('codespace') == name and previous.get('identity') == project.identity:
            return archive
    return None


def _cleanup(project, state, journal, *, now, say):
    """Use the recorded kind, keeping every successful primitive durable on replay."""
    name, kind, results = journal['codespace'], journal['kind'], journal['cleanup']

    def perform(step, action):
        if results.get(step) in _DONE:
            return
        try:
            result = action()
            results[step] = 'kept' if result == 'kept' else 'done'
            if step == 'Herdr profile' and result == 'kept':
                say('Herdr profile kept: setup did not create it.')
        except (ValueError, OSError) as error:
            # These primitives expose fixed reasons, never subprocess stderr.
            results[step] = str(error) if isinstance(error, ValueError) else 'the local step could not finish'
        _save(state, journal, now)

    if kind == 'paired':
        with locked(state / 'workspace-control.lock', blocking=True):
            binding = _optional(state / scratch.CONFIG)
            moved = binding.get('codespace')
            if moved and moved != name:
                for step in STEPS[kind]:
                    if step not in ('sync session', 'Herdr connections') and results.get(step) not in _DONE:
                        results[step] = 'moved'
                say(f'The project moved to {moved} meanwhile; its keeper, pairing, SSH alias and Herdr profile stay.')
                _save(state, journal, now)
                perform('Herdr connections', lambda: restore.end_target_proxies(name, state))
            else:
                perform('keeper', lambda: keeper.stop(state))
                perform('sync worker', lambda: scratch.stop(state))

                def retire():
                    archive = workspace.retire(project, state, name, held=True)
                    journal['archive'] = str(archive)
                    _save(state, journal, now)

                perform('pairing', retire)
                perform('Herdr connections', lambda: restore.end_target_proxies(name, state))

                def disarm():
                    value = arming.read(state)
                    if value.get('armed'):
                        if value.get('codespace') != name:
                            raise ValueError('the armed Codespace changed; recovery was kept')
                        arming.write(state, dict(value, armed=False))
                        say('Recovery is off: ' + name + ' was deleted.')

                perform('recovery', disarm)
                perform('SSH alias', lambda: ssh_hosts.remove(state))
                perform('Herdr profile', lambda: herdr.remove_profile(project, state))
    else:
        perform('Herdr connections', lambda: restore.end_target_proxies(name, state))

    def terminate():
        if kind == 'paired' and results.get('pairing') not in _DONE:
            raise ValueError('the pairing has not been retired')
        archive = Path(journal['archive']) if journal.get('archive') else _archive(project, state, name)
        if archive is None:
            return
        journal['archive'] = str(archive)
        receipt = _optional(archive / scratch.SESSION)
        if receipt:
            scratch.terminate_retired(state, receipt)

    perform('sync session', terminate)
    pending = next((step for step in STEPS[kind] if results.get(step) not in _DONE), None)
    if pending is None:
        _save(state, journal, now, 'cleaned')
    return pending


def _wait(read, predicate, seconds, *, now, sleep):
    deadline = now() + seconds
    while True:
        if predicate(read()):
            return True
        if now() >= deadline:
            return False
        sleep(min(5, deadline - now()))


def run(project, state, *, codespace: str | None, check: bool, as_json: bool, start: bool,
        env=os.environ, ask=input, tty=None, api_factory=lifecycle.LifecycleAPI,
        connect=CodespaceConnection, now=time.time, sleep=time.sleep) -> int:
    """Every refusal/check is observational; cleanup requires a terminal and consent."""
    state = Path(state)
    local = standing.read(project.root, env=env)
    repository = project.identity.removeprefix('github.com/')
    target = verdict.Target(codespace or '', 'unpaired', None, None, None)
    account, journal = None, None
    have_delete_lock = mutation_started = delete_attempted = deleted_confirmed = start_attempted = False
    terminal = tty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())

    def command(*args):
        return shlex.join(overview.task_command(local, 'work:cs:delete', *args))

    def say(message):
        if not as_json:
            print(overview.fit(message, max(78, len(message) * 2)))

    def result(outcome, message, code=1, *, observed=None):
        value = observed or verdict.Verdict(target, now(), account, repository, (), (), None, None)
        if as_json:
            print(json.dumps(verdict.as_json(value, outcome=outcome, message=message,
                             phase=journal.get('phase') if journal else None,
                             cleanup=journal.get('cleanup') if journal else None), sort_keys=True))
        else:
            if observed:
                for line in verdict.lines(observed, local):
                    print(line)
            if message:
                say(message)
        return code

    def unavailable(reason, state_name=None):
        action = None
        if state_name == 'Shutdown':
            if target.kind == 'paired':
                action = 'To check it, restart it: ' + shlex.join(overview.task_command(local, 'work:cs:restart'))
            elif target.kind in ('sealed', 'moved'):
                action = 'To check it, start it: ' + command('--', '--codespace', target.name, '--start')
        elif state_name:
            action = 'Check again once it is Available.'
        value = verdict.Verdict(target, now(), account, repository, verdict.unreachable(target, reason), (), None, action)
        return result('unreachable', reason if as_json else None, observed=value)

    def pending_cleanup():
        steps = STEPS.get(journal['kind'] if journal else target.kind, ())
        step = next((item for item in steps if not journal or journal['cleanup'].get(item) not in _DONE),
                    steps[0] if steps else 'sync session')
        reason = journal['cleanup'].get(step, 'not finished') if journal else 'not finished'
        message = f'Deleted {target.name}, but cleanup is not finished: {step} ({reason}). Run ' + \
                  command('--', '--codespace', target.name) + ' in a terminal to finish it.'
        return result('cleanup-pending', message)

    def finish_cleanup():
        pending = _cleanup(project, state, journal, now=now, say=say)
        if pending:
            return pending_cleanup()
        say('Deleted ' + target.name + '.')
        say('Cleaned up: ' + ', '.join(STEPS[journal['kind']]) + '.')
        return 0

    def consent():
        try:
            return ask('> ').strip() == 'delete'
        except EOFError:
            return False

    try:
        if env.get('CODESPACES') == 'true':
            return result('refused', 'Run ' + command() + " on your Mac; it needs the Mac's key and state.")
        for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
            if env.get(key):
                return result('refused', f'Unset {key} before deleting a Codespace; it must use a real gh account.')
        with locked(state / 'delete.lock'):
            have_delete_lock = True
            account = arming.account(env)
            if codespace is None and not _optional(state / scratch.CONFIG):
                return result('refused', 'No Codespace is paired. Name one: ' +
                              command('--', '--codespace', 'NAME'))
            target = verdict.resolve(project, state, codespace)
            journal = _journal(state, target.name)
            if journal and (journal['account'] != account or journal['repository'].casefold() != repository.casefold()):
                return result('refused', f"{target.name}'s delete was started as {journal['account']} for "
                              f"{journal['repository']}; switch to that account and repository, then try again.")
            armed = arming.read(state) if target.kind == 'paired' else {}
            owner = (armed.get('gh_user') if armed.get('armed') else None) or \
                    (target.seal.gh_user if target.seal else None)
            if owner is not None and owner != account:
                return result('refused', f'{target.name} was paired as {owner}, but gh is signed in as {account}. '
                              'Switch with gh auth switch, then run the delete again.')
            if journal and journal['phase'] in ('deleted', 'cleaned'):
                if start:
                    return result('refused', target.name + ' was deleted.')
                if journal['phase'] == 'cleaned':
                    at = overview._when(journal['at'].get('deleted', journal['at']['cleaned']), now())
                    return result('cleaned', f'{target.name} was deleted at {at}; nothing is left to clean up.', 0)
                if check or not terminal():
                    return pending_cleanup()
                return finish_cleanup()

            api = api_factory(target.name, gh_user=account)
            reading = api.read()
            if not reading.ok:
                return unavailable('GitHub did not answer about ' + target.name)
            if reading.state != 'Deleted' and (reading.repository or '').casefold() != repository.casefold():
                return result('refused', target.name + ' belongs to another repository; nothing was checked.')
            if start:
                if target.kind == 'paired':
                    return result('refused', 'To start the paired Codespace, use ' +
                                  shlex.join(overview.task_command(local, 'work:cs:restart')) + '.')
                if not terminal():
                    return result('refused', '--start needs a terminal.')
                if target.kind not in ('sealed', 'moved') or reading.state != 'Shutdown' or reading.pending is True:
                    return unavailable(target.name + ' is ' + reading.state, reading.state)
                start_attempted = True
                status = api.post_start()
                if not 200 <= status < 300 or not _wait(api.read, lambda row: row.ok and row.state == 'Available'
                        and (row.repository or '').casefold() == repository.casefold(), 600, now=now, sleep=sleep):
                    return result('refused', target.name + ' did not become Available within 10 minutes.')
                say('Started ' + target.name + '. Check it: ' + command('--', '--codespace', target.name))
                return 0
            if reading.state == 'Deleted':
                deleted_confirmed = True
                kept = journal or target.kind == 'paired' or target.archive is not None
                if not kept:
                    return result('already-deleted', target.name + ' is already deleted.', 0)
                if check or not terminal():
                    return pending_cleanup()
                if journal is None:
                    say(target.name + ' is already deleted. This Mac still keeps: ' + ', '.join(STEPS[target.kind]) + '.')
                    say('Type delete to remove them.')
                    if not consent():
                        say('Nothing was deleted.')
                        return 1
                    journal = _new_journal(target, account, repository, '0' * 64)
                _save(state, journal, now, 'deleted')
                return finish_cleanup()
            if target.kind == 'unpaired':
                return unavailable('this Mac has never connected to ' + target.name + '; pair it first')
            if reading.state != 'Available':
                return unavailable(target.name + (' is stopped' if reading.state == 'Shutdown' else ' is ' + reading.state),
                                   reading.state)

            with ExitStack() as connections:
                try:
                    connection = connections.enter_context(connect(target.name, state=state))
                except ValueError:
                    return unavailable('could not connect to ' + target.name)
                evidence = gather(project, state, target, connection, now=now, sleep=sleep)
                value = verdict.Verdict(target, now(), account, repository, verdict.judge(target, evidence, now=now()),
                                        evidence.kept, target_scan.fingerprint(evidence.scan), None)
                if check:
                    return result('checked', None, 0 if value.deletable else 1, observed=value)
                for line in verdict.lines(value, local):
                    say(line)
                if not value.deletable:
                    return 1
                if not terminal():
                    say('Nothing was deleted: deleting needs a terminal. Run ' +
                        command('--', '--codespace', target.name) + ' in one.')
                    return 1
                if not consent():
                    say('Nothing was deleted.')
                    return 1

                def changed(what):
                    say(f'{target.name} changed since the check ({what}); nothing was deleted. '
                        'Run the delete again to check it again.')
                    return 1

                if arming.account(env) != account:
                    return changed('the GitHub account')
                try:
                    fresh = verdict.resolve(project, state, codespace)
                except ValueError:
                    return changed('its pairing')
                if (fresh.name, fresh.kind, fresh.remote_root, fresh.seal) != \
                        (target.name, target.kind, target.remote_root, target.seal):
                    return changed('its pairing')
                reading = api.read()
                if not reading.ok:
                    return changed('the connection to it')
                if reading.state != 'Available':
                    return changed('its state')
                if (reading.repository or '').casefold() != repository.casefold():
                    return changed('its pairing')
                again = gather(project, state, fresh, connection, now=now, sleep=sleep)
                if target_scan.fingerprint(again.scan) != value.fingerprint:
                    return changed('its files, repositories, conversations or agents')
                checked = replace(value, categories=verdict.judge(fresh, again, now=now()))
                if not checked.deletable:
                    return changed('what GitHub or this Mac holds for it')
                journal = _new_journal(target, account, repository, value.fingerprint)
                _save(state, journal, now, 'confirmed')
                mutation_started = True
                try:
                    if target.kind == 'paired':
                        power.stop(state, expected=target.name, api=api, now=now, sleep=sleep,
                                   after_record=lambda _: _save(state, journal, now, 'stop-recorded'))
                    else:
                        _save(state, journal, now, 'stop-recorded')
                        if not 200 <= api.post_stop() < 300:
                            raise ValueError('GitHub refused the stop')
                except ValueError:
                    say(target.name + ' could not be stopped (the stop request failed); nothing was deleted.')
                    return 1
                _save(state, journal, now, 'stop-requested')
                if not _wait(api.read, lambda row: row.ok and row.state == 'Shutdown' and row.pending is not True
                             and (row.repository or '').casefold() == repository.casefold(),
                             600, now=now, sleep=sleep):
                    say(target.name + ' has not reached Shutdown after 10 minutes; nothing was deleted. '
                        'Run the delete again to check it again.')
                    return 1
                _save(state, journal, now, 'shutdown')
                _save(state, journal, now, 'delete-requested')
                delete_attempted = True
                status = api.delete()
                if status >= 400 and status != 404:
                    say(f'GitHub refused to delete {target.name} (HTTP {status}). It stays stopped; nothing else changed.')
                    return 1
                if not _wait(api.confirm_deleted, lambda gone: gone is True, 120, now=now, sleep=sleep):
                    say('GitHub accepted the delete but does not show ' + target.name + ' as gone yet. '
                        'Run the delete again to finish.')
                    return 1
                deleted_confirmed = True
                _save(state, journal, now, 'deleted')
            return finish_cleanup()
    except (OSError, ValueError) as error:
        if deleted_confirmed or (journal and journal['phase'] in ('deleted', 'cleaned')):
            return pending_cleanup()
        if delete_attempted:
            say('GitHub accepted the delete but does not show ' + target.name + ' as gone yet. '
                'Run the delete again to finish.')
            return 1
        if mutation_started:
            say(target.name + ' could not be stopped (its stop could not be confirmed); nothing was deleted.')
            return 1
        if start_attempted:
            return unavailable('GitHub did not answer about ' + target.name)
        if isinstance(error, BlockingIOError) and not have_delete_lock:
            return result('refused', 'Another delete for this project is running; wait for it to finish.')
        return result('refused', 'The delete could not read its account or local evidence; nothing was deleted.')
