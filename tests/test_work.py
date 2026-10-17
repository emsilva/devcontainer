import unittest
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
from devcontainer_setup.project_sync import work


class UI:
    def __init__(self, *choices):
        self.choices = iter(choices)
        self.menus = []
    def choose(self, title, options, selected=None):
        self.menus.append((title, options, selected))
        choice = next(self.choices, 'Exit')
        if choice == 'CANCEL':
            raise work.Cancelled()
        for option in options:
            if option.startswith(choice):
                return option
        raise AssertionError((choice, options))
    def confirm(self, title, default=True):
        return self.choose(title, ['Yes', 'No']) == 'Yes'


class Service:
    is_remote = False
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.prefs = {}
    def preferences(self): return self.prefs
    def inventory(self, remote=False): return dict(sessions=self.rows if not remote else [], host='local')
    def binding(self): return dict(codespace='test')
    def space(self): return dict(name='test', state='Available')
    def remember(self, *args): self.calls.append(('remember', *args))
    def continue_session(self, *args): self.calls.append(('continue', *args)); return 0
    def new_session(self, agent): self.calls.append(('new', agent)); return 0, 'new-id'
    def adopt(self, *args): self.calls.append(('adopt', *args))


def row(status='idle'):
    return dict(agent='codex', session_id='private-id', title='Repair menu', updated_at=100, ownership=dict(status=status))


class WorkTests(unittest.TestCase):
    def test_empty_new_local_tool(self):
        service = Service()
        self.assertEqual(work.run(None, None, ui=UI('New conversation', 'Claude', 'Exit'), service=service), 0)
        self.assertIn(('new', 'claude'), service.calls)

    def test_remember_before_launch_and_reenter(self):
        service = Service([row()])
        service.prefs = dict(agent='codex', session_id='private-id', location='mac')
        ui = UI('Continue here', 'Exit')
        work.run(None, None, ui=ui, service=service)
        self.assertEqual(service.calls, [('remember', 'codex', 'private-id', 'mac'), ('continue', 'codex', 'private-id', 'mac')])
        self.assertTrue(ui.menus[0][2].startswith('Continue here'))
        self.assertNotIn('private-id', str(ui.menus))

    def test_unknown_requires_explicit_adoption(self):
        service = Service([row('unknown')])
        work.run(None, None, ui=UI('Other conversation', 'Repair', 'Continue here', 'No', 'Exit'), service=service)
        self.assertEqual(service.calls, [])

    def test_running_guidance_and_cancel(self):
        service = Service([row('running')])
        work.run(None, None, ui=UI('Other conversation', 'Repair', 'Continue here', 'Back', 'CANCEL'), service=service)
        self.assertEqual(service.calls, [])

    def test_remote_has_no_cross_host_action(self):
        service = Service([row()]); service.is_remote = True
        service.prefs = dict(agent='codex', session_id='private-id')
        ui = UI('Exit')
        work.run(None, None, ui=ui, service=service)
        self.assertNotIn('Continue there', str(ui.menus))

    def test_first_pair_stopped_start_before_roots_and_ambiguous_choices(self):
        service = Service([row()])
        service.prefs = dict(agent='codex', session_id='private-id')
        service.binding = lambda: None
        service.spaces = lambda: [dict(name='one', state='Available'), dict(name='two', state='Shutdown')]
        service.start_codespace = lambda name=None: service.calls.append(('start', name))
        service.roots = lambda name: (service.calls.append(('roots', name)) or ['/workspaces/a', '/workspaces/b'])
        service.pair = lambda name, root: service.calls.append(('pair', name, root))
        work.run(None, None, ui=UI('Continue there', 'two', 'Start Codespace', '/workspaces/b', 'Exit'), service=service)
        self.assertEqual(service.calls[:3], [('start', 'two'), ('roots', 'two'), ('pair', 'two', '/workspaces/b')])
        self.assertEqual(service.calls[-1], ('continue', 'codex', 'private-id', 'codespace'))

    def test_start_cancel_does_not_discover_roots_or_start(self):
        service = Service([row()])
        service.prefs = dict(agent='codex', session_id='private-id')
        service.binding = lambda: None
        service.spaces = lambda: [dict(name='one', state='Shutdown')]
        service.roots = lambda name: self.fail('roots must not run before explicit start')
        work.run(None, None, ui=UI('Continue there', 'Back', 'Exit'), service=service)
        self.assertEqual(service.calls, [])

    def test_other_host_catalog_and_explicit_adoption(self):
        service = Service([row('unknown')])
        work.run(None, None, ui=UI('Other conversation', 'Repair', 'Continue here', 'Yes', 'Exit'), service=service)
        self.assertEqual(service.calls[0], ('adopt', 'codex', 'private-id'))
        service = Service()
        service.inventory = lambda remote=False: dict(sessions=[row()] if remote else [])
        work.run(None, None, ui=UI('Other conversation', 'Repair', 'Continue here', 'Exit'), service=service)
        self.assertEqual(service.calls[-1], ('continue', 'codex', 'private-id', 'mac'))

    def test_settings_status_stop_and_service_error_retry(self):
        service = Service()
        service.sync_status = lambda: dict(status='conflicts', conflicts=['scratch/conflict.txt'])
        service.stop_sync = lambda: service.calls.append(('stop',))
        work.run(None, None, ui=UI('Settings', 'Show status', 'Stop scratch', 'Back', 'Exit'), service=service)
        self.assertIn(('stop',), service.calls)
        service = Service([row()]); service.prefs = dict(agent='codex', session_id='private-id')
        original = service.continue_session
        def launch(*args):
            if not any(c[0] == 'failed' for c in service.calls):
                service.calls.append(('failed',)); raise ValueError('network interrupted')
            return original(*args)
        service.continue_session = launch
        work.run(None, None, ui=UI('Continue here', 'Retry', 'Exit'), service=service)
        self.assertIn(('continue', 'codex', 'private-id', 'mac'), service.calls)

    def test_status_is_readable_and_lists_conflicts(self):
        service = Service()
        service.sync_status = lambda: dict(phase='conflicts', conflicts=['scratch/file.txt'], process_identity='private-process-token')
        output = io.StringIO()
        with redirect_stdout(output):
            work.run(None, None, ui=UI('Settings', 'Show status', 'Back', 'Exit'), service=service)
        self.assertIn('Scratch sync: Conflicts', output.getvalue())
        self.assertIn('scratch/file.txt', output.getvalue())
        self.assertNotIn('private-process-token', output.getvalue())

    def test_remote_remembered_copy_does_not_prompt_local_adoption(self):
        service = Service([row('unknown')])
        service.prefs = dict(agent='codex', session_id='private-id', location='codespace')
        service.inventory = lambda remote=False: dict(sessions=[row('idle' if remote else 'unknown')])
        work.run(None, None, ui=UI('Continue here', 'Exit'), service=service)
        self.assertEqual(service.calls[-1], ('continue', 'codex', 'private-id', 'mac'))
        self.assertFalse(any(c[0] == 'adopt' for c in service.calls))
