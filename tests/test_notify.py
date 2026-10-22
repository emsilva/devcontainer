import subprocess
import unittest

from devcontainer_setup.project_sync.notify import notify


class NotifyTests(unittest.TestCase):
    def test_notify_never_raises(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError('osascript')

        def failing(args, **kwargs):
            return subprocess.CompletedProcess(args, 1)

        def slow(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 1)

        for run in (missing, failing, slow):
            self.assertIs(notify('t', 'm', run=run), False)

    def test_notify_passes_text_as_arguments(self):
        seen = []
        ok = notify('Title "q"', 'Body\\', run=lambda args, **k: seen.append(args) or subprocess.CompletedProcess(args, 0))
        self.assertTrue(ok)
        self.assertEqual(seen[0][0], 'osascript')
        self.assertEqual(seen[0][-2:], ['Title "q"', 'Body\\'])


if __name__ == '__main__':
    unittest.main()
