"""The render file for the owner (Done means 2): every task work frame fits, and every situation is drawn."""
import unittest

# The module only: a TestCase class imported by name here would run all of its tests again.
from tests import render_screens


class RenderScreensTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Two wizard runs through main(): render once for the whole class.
        cls.text = render_screens.render()

    def test_every_task_work_line_fits(self):
        from devcontainer_setup.project_sync.overview import cells
        for name, local, remote in render_screens.situations():
            with self.subTest(situation=name):
                frame = render_screens.frame(local, remote)
                self.assertLessEqual(len(frame.lines), 6, frame.lines)
                for line in frame.lines:
                    self.assertLessEqual(cells(line), 78, line)
                    # A cut ends in '…'. Only the long-name cases may cut, and 'checking GitHub…' is a word.
                    if line.endswith('…') and line != render_screens.CHECKING:
                        self.assertIn(name, render_screens.LONG_NAMES, line)
                if name in render_screens.ONE_ACTION:
                    self.assertEqual([action.key for action in frame.actions], ['stop'])
                else:
                    self.assertGreaterEqual(len(frame.actions), 2)
                    self.assertLessEqual(len(frame.actions), 6)
        names = [name for name, _, _ in render_screens.situations()]
        self.assertEqual(len(names), len(set(names)))
        for name in (*render_screens.LONG_NAMES, *render_screens.ONE_ACTION):
            self.assertIn(name, names)
        long = {name: render_screens.frame(local, remote).lines[0]
                for name, local, remote in render_screens.situations() if name in render_screens.LONG_NAMES}
        self.assertTrue(any(line.endswith('…') for line in long.values()), long)

    def test_render_names_every_situation(self):
        headings = [line for line in self.text.splitlines() if line.startswith('### ')]
        for name, _, _ in render_screens.situations():
            self.assertEqual(headings.count('### ' + name), 1, name)
        for section in ('First setup', 'Rerun with launch history', 'Refused on a branch', 'Refused on main',
                        'Replace: the question before anything is written', 'Replace: continue or abandon',
                        'Show details: after a Mac restart', 'Show details: armed and running',
                        'Show details: restarting during a hold', 'Show details: not armed'):
            self.assertEqual(headings.count('### ' + section), 1, section)
        for marker in ('Traceback', 'Error:', 'Unexpected choice'):
            self.assertNotIn(marker, self.text)


class RowActionTests(unittest.TestCase):
    """FR20: a row that asks for forget, connect, pair, create or update has that item on the menu."""

    # Row text -> the menu keys that answer it (any one; a key ending in ':' is a prefix).
    ASKS = ((r'\bforget (it|them)\b', ('forget:',)), (r'\bconnect\b', ('connect', 'start')),
            (r'\bpair\b', ('pair:',)), (r'\bcreate\b', ('create',)),
            (r'\bcommits behind\b', ('update-checkout',)), (r'^Generator ', ('upgrade',)))
    # Frames allowed to miss an item, each with its reason. None of the render's frames needs one. Fable's
    # row_sweep axes (186,624 frames, fix-3b/miss-causes.txt) find these conditions, which no frame here draws:
    # - "forget it/them" without Forget: only where an unfinished replace, a cleanup or the retired delete take the
    #   slots first (priority and reserved items, which outrank a refused conversation).
    # - "commits behind" without Update the checkout and "Generator" without its review: both rank after the Forget
    #   items, so a full menu cuts them; their rows state a fact and do not name the action.
    EXCEPTIONS = {
        'Another repository: three to pair, a cleanup and a stopped replace':
            'Finish the replace and the cleanup come first and three Pair items follow, so the cut drops Create a '
            'Codespace while the row says "pair or create one"; Pair answers it, and Create returns once either '
            'finishes.',
    }

    def test_a_row_that_asks_for_an_action_has_it_on_the_menu(self):
        import re
        missing = []
        for name, local, remote in render_screens.situations():
            frame = render_screens.frame(local, remote)
            keys = [action.key for action in frame.actions]
            for line in frame.lines[1:]:
                for pattern, answers in self.ASKS:
                    if re.search(pattern, line, re.I) and not any(
                            key == answer or (answer.endswith(':') and key.startswith(answer))
                            for key in keys for answer in answers):
                        missing.append((name, line, keys))
        self.assertEqual([found for found in missing if found[0] not in self.EXCEPTIONS], [])
        # An exception that no longer applies is removed.
        self.assertEqual(sorted(set(self.EXCEPTIONS) - {found[0] for found in missing}), [])


if __name__ == '__main__':
    unittest.main()
