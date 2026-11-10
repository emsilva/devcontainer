"""Setup's sole replace-journal writer; runtime effects run in the project's wrapper."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time

from . import git_ops, replace_inputs
from .codespaces import Codespaces, find_receipt, receipt_path
from .preferences import load_preferences
from .project_sync import arming, evidence, replacing
from .project_sync.state import discover_project, locked, read_json, state_dir
from .ui import Cancelled, Gum


PHASE_COPY = dict(inspected='inspect', **{'create-requested': 'create'}, created='create',
                  ready='flush and move', moved='sign in', **{'signed-in': 'check'},
                  checked='delete offer', offered='delete offer')


def launch_options(*args):
    # Lazy imports keep setup's eventual --replace entry point free of a cycle.
    from .__main__ import launch_options as options
    return options(*args)


def record_created(*args):
    from .__main__ import record_created as record
    return record(*args)


def _optional(path):
    try:
        return read_json(path)
    except FileNotFoundError:
        return None


def _launch_receipt(path):
    """Keep the launch schema only, never arbitrary external fields or command output."""
    value = _optional(path)
    if value is None:
        return None
    fields = ('codespace', 'repository', 'branch', 'commit', 'machine', 'idle_timeout_minutes',
              'location', 'retention_period_minutes', 'validated_commit', 'rebuild_pending')
    return {key: value[key] for key in fields if key in value}


class _Coordinator:
    def __init__(self, project, state, identity, *, api, ui, ask, env, runner, now, sleep):
        self.project, self.state, self.identity = project, state, identity
        self.api, self.ui, self.ask, self.env = api, ui, ask, env
        self.runner, self.now, self.sleep = runner, now, sleep
        self.journal = None
        generated = (project / '.devcontainer/scripts/project-sync.sh').is_file()
        self.runtime = project / ('.devcontainer/scripts/project-sync.sh' if generated else 'scripts/project-sync.sh')
        self.replace = shlex.join(['task', 'devcontainer:setup' if generated else 'setup', '--', '--replace'])
        self.options = None

    def choose(self, title, options):
        if self.ui is None:
            self.ui = Gum()
        return self.ui.choose(title, options)

    def write(self, **changes):
        value = dict(self.journal, **changes)
        replacing.write(self.state, value)
        self.journal = value

    def phase(self, phase, **changes):
        self.write(phase=phase, at=dict(self.journal['at'], **{phase: self.now()}), **changes)

    def stop(self, reason):
        if self.journal:
            phase = PHASE_COPY[self.journal['phase']]
            print(f'Replace stopped at {phase}: {reason}. Run {self.replace} to continue.', flush=True)
        else:
            print(reason, flush=True)
        return 1

    def latch(self):
        if replacing.move_started(self.state, self.journal):
            if self.journal['moving'] is None:
                self.write(moving=self.now())
            return True
        return False

    def runtime_step(self, *args):
        argv = ['bash', str(self.runtime), '--project', str(self.project), *args]
        # Let the foreground child finish its SIGINT cleanup and be reaped before returning.
        previous = signal.signal(signal.SIGINT, lambda number, frame: None)
        try:
            result = self.runner(argv, cwd=self.project, env=dict(self.env))
        finally:
            signal.signal(signal.SIGINT, previous)
        if result.returncode in (-signal.SIGINT, 128 + signal.SIGINT):
            raise KeyboardInterrupt
        return result.returncode

    def account(self):
        account = self.api.user()
        if account != self.journal['account']:
            raise ValueError(f"This replace was started as {self.journal['account']}, but gh is signed in as "
                             f'{account}. Switch with gh auth switch, then run the replace again.')

    def launch_values(self):
        if self.options is None:
            path = self.project / '.devcontainer/devcontainer.json'
            selected = json.loads(path.read_text()) if path.exists() else {}
            self.options = dict(launch_options(self.project, selected, load_preferences(self.env)),
                                open_after_create='none')
        return self.options

    def check_setup(self, branch, commit, resources):
        reason = replace_inputs.setup_differs(self.project, commit)
        if reason:
            raise ValueError(f"{branch} at {commit[:12]} does not carry this checkout's setup ({reason}); "
                             'nothing was created. Run task setup first.')
        repo = self.journal['repository'] if self.journal else self.identity.identity.removeprefix('github.com/')
        if not replace_inputs.offered(self.api.machines(repo, commit), resources):
            raise ValueError(f"{self.old}'s machine {resources.machine} ({resources.cpus} cores) is not offered "
                             f'for {branch} at {commit[:12]}; nothing was created.')

    def inspect(self):
        config = _optional(self.state / 'scratch.json') or {}
        if not config.get('codespace'):
            raise ValueError('No Codespace is paired, so there is nothing to replace. Create one with task setup.')
        if config.get('identity') != self.identity.identity or config.get('root') != str(self.project):
            raise ValueError('The pairing belongs to another project; nothing was created.')
        self.old = config['codespace']
        if not re.fullmatch('[A-Za-z0-9-]+', self.old):
            raise ValueError('The pairing cannot be read; nothing was created.')
        if not self.identity.identity.startswith('github.com/'):
            raise ValueError('The pairing is not for a GitHub repository; nothing was created.')
        repo = self.identity.identity.removeprefix('github.com/')
        body = self.api.get(self.old)
        resources = replace_inputs.read_resources(dict(body, name=self.old), repo)
        branch = self.env.get('SETUP_REPLACE_TEST_BRANCH') or git_ops.default_branch(self.project)
        commit = replace_inputs.pin(self.project, branch)
        self.check_setup(branch, commit, resources)
        account = self.api.user()
        consent = arming.read(self.state)
        if consent.get('armed'):
            user = consent.get('gh_user', 'unknown')
            if user == account:
                date = datetime.fromtimestamp(consent['consented_at']).strftime('%Y-%m-%d')
                print(f'Recovery moves with your consent from {date} ({user}).', flush=True)
            else:
                print(f'Recovery on NEW will need a typed arm: consent was given as {user}, '
                      f'gh is signed in as {account}.', flush=True)
        options = self.launch_values()
        state_root = Path(options['state_root'])
        branch_record = _launch_receipt(receipt_path(state_root, repo, branch))
        ref = body.get('git_status', {}).get('ref')
        old_record = _launch_receipt(receipt_path(state_root, repo, ref)) if isinstance(ref, str) else None
        if not old_record or old_record.get('codespace') != self.old:
            found = find_receipt(state_root, repo, self.old)
            old_record = _launch_receipt(found) if found else None
        self.journal = dict(schema=1, operation=secrets.token_hex(6), old=self.old, new=None, account=account,
            repository=repo, branch=branch, commit=commit,
            remote_root=options.get('workspace') or f'/workspaces/{repo.split("/")[-1]}',
            resources=asdict(resources), old_receipt=old_record, branch_receipt=branch_record,
            request=None, phase='inspected', at={'inspected': self.now()}, signins=None, check=None,
            moving=None, ended=None, ended_at=None)
        replacing.write(self.state, self.journal)
        print(f'Replacing {self.old} ({resources.machine}, {resources.cpus} cores, '
              f'idle timeout {resources.idle_timeout_minutes} minutes).', flush=True)

    def receipt(self):
        return receipt_path(Path(self.launch_values()['state_root']), self.journal['repository'], self.journal['branch'])

    def launch(self, *, resume=False, retry=False):
        journal = self.journal
        options = dict(self.launch_values(), workspace=journal['remote_root'])
        invoked = False
        before = None
        if not resume:
            self.account()
            if not retry:
                commit = replace_inputs.pin(self.project, journal['branch'])
                if commit != journal['commit']:
                    print(f"{journal['branch']} moved to {commit[:12]} since the check; checking its setup again.",
                          flush=True)
                    self.check_setup(journal['branch'], commit, replace_inputs.Resources(**journal['resources']))
                    self.write(commit=commit)
            before = [row['name'] for row in self.api.existing(journal['repository'], complete=True)]
            print(f"Creating the new Codespace from {journal['branch']} at {self.journal['commit'][:12]}.", flush=True)

        def before_create():
            nonlocal invoked
            request = self.journal['request']
            if request is None:
                request = dict(at=self.now(), before=before, retries=[])
            else:
                request = dict(request, retries=[*request['retries'], self.now()])
            self.phase('create-requested', request=request)
            invoked = True

        def on_created(name, started):
            self.phase('created', new=name)
            record_created(self.project, name, started)
            print(f'Created {name}.', flush=True)

        try:
            self.api.launch(self.journal['repository'], self.journal['branch'], self.journal['commit'],
                self.journal['resources']['machine'], f"{self.journal['resources']['idle_timeout_minutes']}m",
                **options, receipt=self.receipt(), resume=self.journal['new'] if resume else None,
                before_create=before_create, on_created=on_created, resume_hint=self.replace)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            # A failed preflight is never evidence that a POST happened, even on a retry.
            if not resume and not invoked:
                return self.stop(str(error))
            if self.journal['new'] is None:
                return self.reconcile()
            return self.stop(str(error))
        self.phase('ready')
        if retry:
            candidates, uncertain = self.candidates()
            for row in candidates + uncertain:
                if row['name'] != self.journal['new']:
                    print('Also new since the create request: ' + self.row_label(row) +
                          '. This replace did not use it.', flush=True)
        return None

    def candidates(self):
        self.account()
        candidates, uncertain = [], []
        for original in self.api.existing(self.journal['repository'], complete=True):
            name = original.get('name')
            if name in self.journal['request']['before']:
                continue
            if not isinstance(name, str) or not re.fullmatch('[A-Za-z0-9-]+', name):
                raise ValueError('the Codespace list from GitHub is incomplete')
            row = original
            for attempt in range(2):
                ref = row.get('git_status')
                ref = ref.get('ref') if isinstance(ref, dict) else None
                machine = row.get('machine')
                machine = machine.get('name') if isinstance(machine, dict) else None
                try:
                    stamp = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
                    created = stamp.timestamp() if stamp.utcoffset() is not None else None
                except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
                    created = None
                if ((isinstance(ref, str) and ref and ref != self.journal['branch'])
                        or (isinstance(machine, str) and machine and machine != self.journal['resources']['machine'])
                        or (created is not None and created < self.journal['request']['at'] - 120)):
                    break
                if (ref == self.journal['branch'] and machine == self.journal['resources']['machine']
                        and created is not None):
                    candidates.append(dict(row, name=name))
                    break
                if attempt:
                    uncertain.append(dict(row, name=name))
                else:
                    try:
                        row = self.api.get(name)
                    except (ValueError, OSError, subprocess.TimeoutExpired):
                        row = original
        return candidates, uncertain

    def row_label(self, row):
        machine = row.get('machine')
        machine = machine.get('name') if isinstance(machine, dict) else None
        try:
            stamp = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
            when = self.local_time(stamp.timestamp()) if stamp.utcoffset() is not None else 'unknown'
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
            when = 'unknown'
        return f"{row['name']} ({machine or 'unknown'}, created at {when})"

    def local_time(self, stamp):
        value = datetime.fromtimestamp(stamp)
        return value.strftime('%H:%M' if value.date() == datetime.fromtimestamp(self.now()).date() else '%Y-%m-%d %H:%M')

    def reconcile(self):
        print('GitHub did not confirm the new Codespace. Looking for it for up to 2 minutes.', flush=True)
        deadline = self.now() + 120
        while True:
            candidates, uncertain = self.candidates()
            if candidates or uncertain:
                break
            if self.now() >= deadline:
                if self.ask('No new Codespace appeared. Type create to create one, or anything else to stop.') != 'create':
                    return self.stop('no new Codespace appeared')
                return self.launch(retry=True)
            self.sleep(10)
        if len(candidates) == 1 and not uncertain:
            chosen = candidates[0]
            print('Found ' + self.row_label(chosen) + ': the only new Codespace on ' +
                  self.journal['branch'] + ' with that machine since the create request.', flush=True)
        else:
            rows = candidates + uncertain
            labels = [self.row_label(row) for row in rows]
            title = ('New Codespaces since the create request: ' + ', '.join(labels) +
                     ' Choose the one this replace keeps; the others stay as they are.')
            answer = self.choose(title, [*labels, 'cancel'])
            if answer not in labels:
                if uncertain:
                    print(f"Replace stopped at create: {uncertain[0]['name']} cannot be fully read from GitHub, "
                          'and it may be the Codespace this replace asked for. Check it on GitHub, '
                          f'then run {self.replace} again.', flush=True)
                    return 1
                return self.stop('no Codespace was selected')
            chosen = rows[labels.index(answer)]
        self.phase('created', new=chosen['name'])
        evidence.record_start(self.state, chosen['name'], self.journal['request']['at'], 'created')
        return self.launch(resume=True)

    def cutover(self):
        self.latch()  # Must be durable before the child can consume the move intents.
        path = replacing.receipt_path(self.state, self.journal['operation'], 'cutover')
        path.unlink(missing_ok=True)  # A prior attempt's receipt cannot certify this attempt.
        try:
            self.runtime_step('replace', 'cutover', '--operation', self.journal['operation'],
                              '--old', self.journal['old'], '--new', self.journal['new'], '--receipt', str(path))
        finally:
            started = self.latch()
        receipt = replacing.read_receipt(path, self.journal['operation'], 'cutover')
        moved = bool(receipt and receipt.get('old') == self.journal['old']
                     and receipt.get('new') == self.journal['new'] and receipt.get('moved') is True)
        moved = moved or (started and (_optional(self.state / 'scratch.json') or {}).get('codespace') == self.journal['new'])
        if not moved:
            return self.stop('the move did not finish')
        if replacing.PHASES.index(self.journal['phase']) < replacing.PHASES.index('moved'):
            self.phase('moved')
        return None

    def sign_in(self):
        began = self.now()
        code = self.runtime_step('work', '--signin-all')
        try:
            record = _optional(self.state / 'signins.json') or {}
        except (OSError, ValueError):
            record = {}
        tools = record.get('tools')
        if (record.get('codespace') == self.journal['new'] and type(record.get('updated_at')) in (int, float)
                and record['updated_at'] > began and isinstance(tools, dict)):
            retry = [key for key in ('plaud', 'codex', 'claude', 'opencode', 'grok') if key in tools and
                     (not isinstance(tools[key], dict) or tools[key].get('state') not in
                      ('signed-in', 'other-method', 'absent'))]
        else:
            retry = ['unknown']
        self.phase('signed-in', signins=dict(retry=retry, at=self.now()))
        if retry:
            print(f"Not signed in yet on {self.journal['new']}: {', '.join(retry)}. "
                  'Retry with task work:signin:all.', flush=True)
        if code:
            print(f"Sign-in on {self.journal['new']} did not finish; retry with task work:signin:all.", flush=True)

    def check(self):
        if self.journal['check'] is not None:
            result = self.cutover()
            if result is not None:
                return result
        path = replacing.receipt_path(self.state, self.journal['operation'], 'check')
        path.unlink(missing_ok=True)
        code = self.runtime_step('replace', 'check', '--operation', self.journal['operation'], '--receipt', str(path))
        raw = replacing.read_receipt(path, self.journal['operation'], 'check')
        launch = _launch_receipt(self.receipt()) or {}
        setup_ok = (launch.get('codespace') == self.journal['new'] and
                    launch.get('validated_commit') == self.journal['commit'] and launch.get('rebuild_pending') is False)
        print(f"  Setup     {'validated at ' + self.journal['commit'][:12] if setup_ok else 'Not ready: setup is not validated'}",
              flush=True)
        # Only the documented receipt schema crosses into the journal; no command output is stored.
        fields = ('schema', 'operation', 'step', 'codespace', 'checked_at', 'sync', 'cycle_completed_at',
                  'recovery', 'herdr', 'conversations', 'ok')
        receipt = {key: raw[key] for key in fields if key in raw} if raw else {'ok': False}
        passed = bool(code == 0 and raw and raw.get('codespace') == self.journal['new']
                      and raw.get('ok') is True and setup_ok)
        if passed:
            self.phase('checked', check=receipt)
            return None
        self.write(check=receipt)
        print(f"{self.journal['new']} is not ready: fix the lines marked Not ready, then run {self.replace} "
              f"to check it again. {self.journal['old']} stays.", flush=True)
        return 1

    def finish(self):
        old, new = self.journal['old'], self.journal['new']
        print(f'{new} is ready. Next, {old} gets the checked delete.', flush=True)
        self.runtime_step('delete', '--codespace', old)
        self.phase('offered')
        replacing.archive(self.state, self.journal, 'completed', self.now())
        print(f'Replaced {old} with {new}.', flush=True)
        if (_optional(self.state / 'deletions' / (old + '.json')) or {}).get('phase') != 'cleaned':
            print(f'{old} is kept; task work offers its delete.', flush=True)
        print('Connect with task work:cs.', flush=True)
        return 0

    def run(self):
        try:
            self.journal = replacing.read(self.state)
            if self.journal:
                self.old = self.journal['old']
                deleted = replacing.ended_by_fact(self.state, self.journal)
                if deleted:
                    replacing.archive(self.state, self.journal, 'deleted', self.now())
                    print(f'The replace of {self.old} ended: {deleted} was deleted.', flush=True)
                    return 0
                print(f"A replace of {self.old} is at {PHASE_COPY[self.journal['phase']] }.", flush=True)
                if self.journal['phase'] == 'offered':
                    replacing.archive(self.state, self.journal, 'completed', self.now())
                    return 0
                before_move = replacing.PHASES.index(self.journal['phase']) < replacing.PHASES.index('moved')
                if before_move and self.latch():
                    print(f"The move to {self.journal['new']} has started; finishing it.", flush=True)
                    result = self.cutover()
                    if result is not None:
                        return result
                elif before_move:
                    answer = self.choose(f'A replace of {self.old} is unfinished.', ['continue', 'abandon'])
                    if answer == 'abandon':
                        seal_path = self.state / 'retired' / (self.old + '.json')
                        seal = _optional(seal_path)
                        if seal and seal.get('operation') == self.journal['operation']:
                            seal_path.unlink()
                        replacing.archive(self.state, self.journal, 'abandoned', self.now())
                        new = self.journal['new']
                        if new:
                            command = shlex.join(['task', 'work:cs:delete', '--', '--codespace', new])
                            print(f'Replace abandoned. {self.old} stays paired; {new} stays listed. '
                                  f'Check and delete it with {command}.', flush=True)
                        else:
                            print(f'Replace abandoned. {self.old} stays paired; nothing was created.', flush=True)
                        return 0
                    if answer != 'continue':
                        return self.stop('cancelled')
            if self.journal is None or self.journal['phase'] == 'inspected':
                self.inspect()
                result = self.launch()
                if result is not None:
                    return result
            elif self.journal['phase'] == 'create-requested':
                result = self.reconcile()
                if result is not None:
                    return result
            elif self.journal['phase'] == 'created':
                result = self.launch(resume=True)
                if result is not None:
                    return result
            if self.journal['phase'] == 'ready':
                result = self.cutover()
                if result is not None:
                    return result
            if self.journal['phase'] == 'moved':
                self.sign_in()
            if self.journal['phase'] == 'signed-in':
                result = self.check()
                if result is not None:
                    return result
            return self.finish()
        except (KeyboardInterrupt, EOFError, Cancelled):
            return self.stop('cancelled')
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            return self.stop(str(error))


def run(project: Path, *, api=None, ui=None, ask=input, env=os.environ, tty=None,
        runner=subprocess.run, now=time.time, sleep=time.sleep) -> int:
    """Replace only the paired Codespace, preserving a durable phase after every interruption."""
    if env.get('CODESPACES') == 'true' or not (sys.stdin.isatty() if tty is None else tty):
        print('Replacing a Codespace needs a terminal on your Mac.', flush=True)
        return 1
    for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if env.get(key):
            print(f'Unset {key} before replacing a Codespace; it must use a real gh account.', flush=True)
            return 1
    try:
        identity = discover_project(Path(project))
        state = state_dir(identity, env)
        with locked(state / replacing.LOCK):
            return _Coordinator(identity.root, state, identity, api=api if api is not None else Codespaces(),
                                ui=ui, ask=ask, env=env, runner=runner, now=now, sleep=sleep).run()
    except BlockingIOError:
        print('Another replace for this project is running; wait for it to finish.', flush=True)
        return 1
    except (ValueError, OSError) as error:
        print(error, flush=True)
        return 1
