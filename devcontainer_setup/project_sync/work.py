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
        raise ValueError('No matching Codespace. Create one with task codespaces first.')
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
    if destination == 'codespace' and not service.is_remote and not _remote_ready(service, ui):
        return
    service.remember(row['agent'], row['session_id'], destination)
    code = service.continue_session(row['agent'], row['session_id'], destination)
    if code:
        raise ValueError(f'Agent exited with status {code}. Your selection is saved; retry when ready.')


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
        while True:
            try:
                local = 'codespace' if service.is_remote else 'mac'
                rows = [dict(r, location=local) for r in service.inventory()['sessions']]
                preferences = service.preferences()
                # Remote catalog discovery is read-only and never starts a space.
                if not service.is_remote and service.binding():
                    space = service.space()
                    if space and str(space.get('state', '')).lower() in ('available', 'running'):
                        remote_rows = service.inventory(remote=True)['sessions']
                        rows.extend(dict(row, location='codespace') for row in remote_rows)
                rows.sort(key=lambda r: float(r.get('updated_at') or 0), reverse=True)
                matches = [r for r in rows if r['agent'] == preferences.get('agent') and r['session_id'] == preferences.get('session_id')]
                remembered = next((r for r in matches if r['location'] == preferences.get('location')), matches[0] if matches else None)
                options = []
                if remembered:
                    options += ['Continue here · ' + _label(remembered)]
                    if not service.is_remote:
                        options += ['Continue there · ' + _label(remembered)]
                options += ['Other conversation', 'New conversation', 'Settings and troubleshooting', 'Exit']
                choice = ui.choose('Work on this project', options, selected=options[0])
                if choice == 'Exit':
                    return 0
                if choice.startswith('Continue '):
                    destination = local if choice.startswith('Continue here') else 'codespace'
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
                    actions = ['Continue here'] + ([] if service.is_remote else ['Continue there']) + ['Back']
                    action = ui.choose('Continue conversation', actions)
                    if action != 'Back':
                        _attempt(ui, lambda: _continue(service, ui, row, local if action == 'Continue here' else 'codespace', row['location']))
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
