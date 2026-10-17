"""Daily intention-based conversation menu; all mutations go through Workflows."""
from __future__ import annotations

from datetime import datetime
from .sessions import display_text
from .terminal import Gum, Cancelled


def _say(message):
    print(display_text(str(message), 1200), flush=True)


def _label(row):
    stamp = row.get('updated_at')
    try:
        stamp = datetime.fromtimestamp(float(stamp)).strftime('%b %d %H:%M')
    except (ValueError, TypeError, OverflowError, OSError):
        stamp = ''
    return f"{display_text(row.get('title')) or 'Untitled conversation'} · {display_text(row.get('agent'))} · {stamp}"


def _pair(service, ui):
    if service.binding():
        return True
    spaces = service.spaces()
    if not spaces:
        raise ValueError('No matching Codespace. Create one with task devcontainer:setup first.')
    labels = [display_text(s['name']) for s in spaces]
    name = spaces[0]['name'] if len(spaces) == 1 else spaces[labels.index(ui.choose('Choose Codespace', labels))]['name']
    selected_space = next(s for s in spaces if s['name'] == name)
    if str(selected_space.get('state', '')).lower() not in ('available', 'running'):
        if ui.choose('Codespace is stopped', ['Start Codespace and continue', 'Back']) == 'Back':
            return False
        service.start_codespace(name)
    roots = service.roots(name)
    if not roots:
        raise ValueError('No matching checkout found in this Codespace.')
    root = roots[0] if len(roots) == 1 else roots[[display_text(r) for r in roots].index(ui.choose('Choose project checkout', [display_text(r) for r in roots]))]
    service.pair(name, root)
    return True


def _remote_ready(service, ui):
    if not _pair(service, ui):
        return False
    space = service.space()
    if space and str(space.get('state', '')).lower() not in ('available', 'running'):
        choice = ui.choose('Codespace is stopped', ['Start Codespace and continue', 'Back'])
        if choice == 'Back':
            return False
        service.start_codespace()
    return True


def _continue(service, ui, row, destination, source):
    ownership = row.get('ownership') or {}
    status = ownership.get('status', 'unknown')
    if status in ('running', 'uncertain'):
        raise ValueError('Conversation has an active or uncertain writer. Exit the native agent on its current host, then use the ownership recovery commands if needed.')
    if status == 'unknown':
        here = 'codespace' if service.is_remote else 'mac'
        if source != here:
            raise ValueError('Adopt this conversation explicitly on its current host first.')
        if not ui.confirm('Adopt this existing conversation for managed continuation?', default=False):
            return
        service.adopt(row['agent'], row['session_id'])
        row['ownership'] = dict(ownership, status='idle')
    needs_remote = destination == 'codespace' or source == 'codespace' or status in ('transferred', 'transferring')
    if needs_remote and not service.is_remote:
        if not _remote_ready(service, ui):
            return
        service.ensure_sync()
    service.remember(row['agent'], row['session_id'], destination)
    code = service.continue_session(row['agent'], row['session_id'], destination)
    if code:
        raise ValueError(f'Agent exited with status {code}. Your selection is saved; retry when ready.')
    return code


def shortcut(project, state, *, agent, destination=None, resume=False, ui=None, service=None):
    """One explicit new/resume action, then return to the launching shell."""
    if service is None:
        from .workflows import Workflows
        service = Workflows(project, state)
    here = 'codespace' if service.is_remote else 'mac'
    destination = destination or here
    if agent not in ('codex', 'claude') or destination not in ('mac', 'codespace'):
        raise ValueError('Choose a supported agent and machine')
    if service.is_remote and destination == 'mac':
        raise ValueError('Run this shortcut on your Mac to return a conversation there')
    try:
        paired = False
        if not service.is_remote:
            try:
                paired = bool(service.binding())
                if paired:
                    service.ensure_sync()
            except (ValueError, OSError, RuntimeError) as exc:
                _say('Scratch sync unavailable: ' + str(exc))
        if destination != here:
            ui = ui or Gum()
            if not _remote_ready(service, ui):
                return 0
            service.ensure_sync()
            paired = True

        def start_new():
            if destination == here:
                return service.new_session(agent)[0]
            return service.new_session(agent, destination=destination)[0]

        if not resume:
            return start_new()
        rows = [dict(row, location=here) for row in service.inventory()['sessions'] if row['agent'] == agent]
        if not service.is_remote and paired:
            try:
                space = service.space()
                if space and str(space.get('state', '')).lower() in ('available', 'running'):
                    rows.extend(dict(row, location='codespace') for row in service.inventory(remote=True)['sessions']
                                if row['agent'] == agent)
            except (ValueError, OSError, RuntimeError) as exc:
                if destination != here:
                    raise
                _say('Codespace unavailable: ' + str(exc))
        # Keep the current owner rather than showing a stale physical copy of
        # the same transcript as a second conversation.
        by_id = {}
        stale = ('unknown', 'transferred')
        for row in rows:
            old = by_id.get(row['session_id'])
            if old is None or ((old.get('ownership') or {}).get('status', 'unknown') in stale
                               and (row.get('ownership') or {}).get('status', 'unknown') not in stale):
                by_id[row['session_id']] = row
        rows = sorted(by_id.values(), key=lambda row: float(row.get('updated_at') or 0), reverse=True)
        labels = [f'{_label(row)} · {row["location"]} ({index+1})' for index, row in enumerate(rows)]
        options = [*labels, 'New conversation', 'Back']
        ui = ui or Gum()
        choice = ui.choose(f'Resume {agent} project conversation', options, selected=options[0])
        if choice == 'Back':
            return 0
        if choice == 'New conversation':
            return start_new()
        row = rows[labels.index(choice)]
        return _continue(service, ui, row, destination, row['location']) or 0
    except (Cancelled, KeyboardInterrupt):
        return 0


def _show_status(status):
    if not isinstance(status, dict):
        _say(status)
        return
    phase = status.get('phase', status.get('status', 'stopped'))
    labels = dict(stopped='Stopped', connecting='Connecting', syncing='Connecting; reconciliation pending',
                  waiting='Waiting for Codespace', conflicts='Conflicts', synchronized='Synchronized',
                  cleanup_required='Stopped; cleanup required')
    _say('Scratch sync: ' + labels.get(phase, display_text(phase)))
    for path in status.get('conflicts', []):
        _say('Conflict: ' + str(path))
    if status.get('error'):
        _say(status['error'])


def _settings(service, ui):
    while True:
        options = ['Show status', 'Start scratch sync', 'Stop scratch sync', 'Back'] if not service.is_remote else ['Show status', 'Back']
        choice = ui.choose('Settings and troubleshooting', options)
        if choice == 'Back':
            return
        if choice == 'Show status':
            _show_status(service.sync_status())
            _say('Resolve conflict paths on both hosts before switching. Advanced status: task devcontainer:scratch:status. Use the project-sync.sh ownership and handoff commands for recovery.')
        elif choice == 'Start scratch sync':
            if _remote_ready(service, ui):
                _show_status(service.ensure_sync())
        else:
            _show_status(service.stop_sync() or dict(phase='stopped'))


def _attempt(ui, action):
    while True:
        try:
            return action()
        except (ValueError, OSError, RuntimeError) as exc:
            _say(exc)
            if ui.choose('Could not complete this action', ['Retry', 'Back']) == 'Back':
                return


def run(project, state, *, ui=None, service=None) -> int:
    if service is None:
        from .workflows import Workflows
        service = Workflows(project, state)
    ui = ui or Gum()
    if service.is_remote:
        _say('Continue locally in this Codespace. To return a conversation to your Mac, run task work on your Mac.')
    try:
        if not service.is_remote:
            try:
                if service.binding():
                    service.ensure_sync()
            except (ValueError, OSError, RuntimeError) as exc:
                _say('Scratch sync unavailable: ' + str(exc))
        while True:
            try:
                local = 'codespace' if service.is_remote else 'mac'
                rows = [dict(r, location=local) for r in service.inventory()['sessions']]
                try:
                    preferences = service.preferences()
                except (ValueError, OSError, RuntimeError) as exc:
                    _say(exc)
                    preferences = {}
                remote_status = 'Codespace not paired'
                sync_status = 'Scratch sync unavailable'
                try:
                    status = service.sync_status()
                    phase = status.get('phase', status.get('status', 'stopped'))
                    sync_status = 'Scratch sync: ' + dict(synchronized='Synchronized', syncing='Connecting; reconciliation pending', waiting='Waiting for Codespace', conflicts='Conflicts', stopped='Stopped', connecting='Connecting').get(phase, display_text(phase))
                except (ValueError, OSError, RuntimeError) as exc:
                    _say(exc)
                # Remote catalog discovery is read-only and never starts a space.
                if not service.is_remote:
                    try:
                        if service.binding():
                            space = service.space()
                            remote_status = 'Codespace: ' + display_text((space or {}).get('state', 'unavailable'))
                            if space and str(space.get('state', '')).lower() in ('available', 'running'):
                                remote_rows = service.inventory(remote=True)['sessions']
                                rows.extend(dict(row, location='codespace') for row in remote_rows)
                    except (ValueError, OSError, RuntimeError) as exc:
                        remote_status = 'Codespace unavailable'
                        _say('Codespace unavailable: ' + str(exc))
                rows.sort(key=lambda r: float(r.get('updated_at') or 0), reverse=True)
                matches = [r for r in rows if r['agent'] == preferences.get('agent') and r['session_id'] == preferences.get('session_id')]
                remembered = next((r for r in matches if r['location'] == preferences.get('location')), matches[0] if matches else None)
                options = []
                if remembered:
                    options += [('Continue in Codespace · ' if service.is_remote else 'Continue on this Mac · ') + _label(remembered)]
                    if not service.is_remote:
                        options += ['Continue in Codespace · ' + _label(remembered)]
                if rows:
                    options.append('Other conversation')
                options += ['New conversation', 'Settings and troubleshooting', 'Exit']
                root = getattr(getattr(service, 'project', project), 'root', None)
                project_name = display_text(getattr(root, 'name', 'Project'))
                host = 'Codespace' if service.is_remote else 'this Mac'
                title = f'{project_name} · {host} · {sync_status}'
                if not service.is_remote:
                    title += ' · ' + remote_status
                choice = ui.choose(title, options, selected=options[0])
                if choice == 'Exit':
                    return 0
                if choice.startswith('Continue '):
                    destination = local if choice.startswith('Continue on this Mac') or service.is_remote else 'codespace'
                    _attempt(ui, lambda: _continue(service, ui, remembered, destination, remembered['location']))
                elif choice == 'Other conversation':
                    if not rows:
                        _say('No project conversations found. Start a new conversation.')
                        continue
                    labels = [f'{_label(r)} · {r["location"]} ({i+1})' for i, r in enumerate(rows)]
                    selected = ui.choose('Choose conversation', [*labels, 'Back'])
                    if selected == 'Back':
                        continue
                    row = rows[labels.index(selected)]
                    actions = (['Continue in Codespace'] if service.is_remote else ['Continue on this Mac', 'Continue in Codespace']) + ['Back']
                    action = ui.choose('Continue conversation', actions)
                    if action != 'Back':
                        _attempt(ui, lambda: _continue(service, ui, row, local if action == 'Continue on this Mac' or service.is_remote else 'codespace', row['location']))
                elif choice == 'New conversation':
                    agent = ui.choose('New conversation on this machine', ['Codex', 'Claude', 'Back'])
                    if agent != 'Back':
                        code, sid = service.new_session(agent.lower())
                        if code:
                            raise ValueError(f'Agent exited with status {code}; your conversation is saved.')
                else:
                    _attempt(ui, lambda: _settings(service, ui))
            except (ValueError, OSError, RuntimeError) as exc:
                _say(exc)
                if ui.choose('Could not complete this action', ['Retry', 'Back']) == 'Retry':
                    # Return to the saved selection without silently repeating a
                    # transfer, adoption, or Codespace start.
                    continue
    except (Cancelled, KeyboardInterrupt):
        return 0
