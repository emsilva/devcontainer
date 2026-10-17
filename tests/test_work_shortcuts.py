import unittest
from unittest.mock import Mock, patch

from devcontainer_setup.project_sync import work
from test_work import Service, UI, row


class Shortcuts(unittest.TestCase):
    def run_shortcut(self, service, ui=None, **options):
        return work.shortcut(None, None, ui=ui or UI(), service=service, **options)

    def test_local_starts_fresh_without_agent_picker_or_remote_discovery(self):
        for agent in ('codex', 'claude'):
            service, ui = Service(), UI()
            service.space = Mock(side_effect=AssertionError('Local new must stay offline'))
            self.assertEqual(self.run_shortcut(service, ui, agent=agent), 0)
            self.assertEqual(service.calls, [('new', agent)])
            self.assertEqual(ui.menus, [])

    def test_fresh_local_command_does_not_initialize_a_menu(self):
        with patch.object(work, 'Gum', side_effect=AssertionError('No menu is required')):
            self.assertEqual(work.shortcut(None, None, agent='codex', service=Service()), 0)

    def test_remote_new_uses_explicit_destination(self):
        service = Service()
        service.new_session = Mock(return_value=(0, 'new-id'))
        self.assertEqual(self.run_shortcut(service, agent='claude', destination='codespace'), 0)
        service.new_session.assert_called_once_with('claude', destination='codespace')

    def test_codespace_local_shortcut_starts_on_that_host(self):
        service = Service()
        service.is_remote = True
        self.assertEqual(self.run_shortcut(service, agent='codex', destination='codespace'), 0)
        self.assertEqual(service.calls, [('new', 'codex')])

    def test_resume_is_project_catalog_filtered_by_agent_newest_first(self):
        first = dict(row(), session_id='older', title='Old Codex', updated_at=1)
        last = dict(row(), session_id='newest', title='Latest Codex', updated_at=3)
        other = dict(row(), agent='claude', title='Claude conversation', updated_at=5)
        service, ui = Service([first, other, last]), UI('Latest Codex')
        self.assertEqual(self.run_shortcut(service, ui, agent='codex', resume=True), 0)
        self.assertTrue(ui.menus[0][1][0].startswith('Latest Codex'))
        self.assertNotIn('Claude conversation', str(ui.menus))
        self.assertEqual(service.calls[-1], ('continue', 'codex', 'newest', 'mac'))

    def test_resume_chooses_owned_remote_copy_over_stale_local_copy(self):
        service = Service([row('transferred')])
        service.inventory = lambda remote=False: dict(sessions=[row('idle' if remote else 'transferred')])
        ui = UI('Repair menu')
        self.run_shortcut(service, ui, agent='codex', resume=True)
        self.assertEqual(sum('Repair menu' in option for option in ui.menus[0][1]), 1)
        self.assertIn('codespace', ui.menus[0][1][0])
        self.assertEqual(service.calls[-1], ('continue', 'codex', 'private-id', 'mac'))

    def test_resume_retains_adoption_and_active_writer_guards(self):
        service = Service([row('unknown')])
        self.run_shortcut(service, UI('Repair', 'Yes'), agent='codex', resume=True)
        self.assertEqual(service.calls[0], ('adopt', 'codex', 'private-id'))
        service = Service([row('running')])
        with self.assertRaisesRegex(ValueError, 'active or uncertain'):
            self.run_shortcut(service, UI('Repair'), agent='codex', resume=True)
        self.assertEqual(service.calls, [])

    def test_empty_resume_offers_new_or_back_and_cancel_never_launches(self):
        for choice in ('Back', 'CANCEL'):
            service = Service()
            self.assertEqual(self.run_shortcut(service, UI(choice), agent='claude', resume=True), 0)
            self.assertEqual(service.calls, [])
        service = Service()
        self.run_shortcut(service, UI('New conversation'), agent='claude', resume=True)
        self.assertEqual(service.calls, [('new', 'claude')])

    def test_explicit_remote_start_can_be_cancelled_before_new_conversation(self):
        service = Service()
        service.space = lambda: dict(state='Shutdown')
        service.start_codespace = Mock()
        self.assertEqual(self.run_shortcut(service, UI('Back'), agent='codex', destination='codespace'), 0)
        service.start_codespace.assert_not_called()
        self.assertEqual(service.calls, [])

    def test_local_resume_survives_remote_discovery_failure(self):
        service = Service([row()])
        service.space = Mock(side_effect=ValueError('Remote unavailable'))
        self.run_shortcut(service, UI('Repair'), agent='codex', resume=True)
        self.assertEqual(service.calls[-1], ('continue', 'codex', 'private-id', 'mac'))

    def test_new_exit_code_is_returned_without_retry_or_dashboard(self):
        service, ui = Service(), UI()
        service.new_session = Mock(return_value=(7, 'saved-id'))
        self.assertEqual(self.run_shortcut(service, ui, agent='claude'), 7)
        service.new_session.assert_called_once_with('claude')
        self.assertEqual(ui.menus, [])
