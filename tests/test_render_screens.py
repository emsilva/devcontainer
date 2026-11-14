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


if __name__ == '__main__':
    unittest.main()
