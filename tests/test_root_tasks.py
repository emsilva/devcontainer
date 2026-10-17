import unittest
from devcontainer_setup.root_tasks import integrate


class RootTasksTests(unittest.TestCase):
    def test_exact_legacy_delegates_retired_custom_preserved_idempotent(self):
        custom = '  work:codex:cs:\n    cmds: [echo custom]\n'
        text = ("version: '3'\ntasks:\n" + custom + '  agent:handoff:\n    cmds:\n'
                '      - task: devcontainer:agent:handoff\n'
                '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n'
                '  up:\n    cmds: [echo owner]\n')
        changed = integrate(text)
        self.assertIn(custom, changed)
        self.assertNotIn('devcontainer:agent:handoff', changed)
        self.assertIn('  work:cs:', changed)
        self.assertIn('cmds: [echo owner]', changed)
        self.assertEqual(integrate(changed), changed)

    def test_edited_owned_block_refused(self):
        text = integrate("version: '3'\ntasks:\n")
        with self.assertRaisesRegex(ValueError, 'Edited'):
            integrate(text.replace('devcontainer:work\n', 'devcontainer:other\n'))

    def test_quoted_custom_work_preserved(self):
        text = 'tasks:\n  "work":\n    cmds: [echo mine]\n'
        result = integrate(text)
        self.assertIn('  "work":\n    cmds: [echo mine]', result)
        self.assertNotIn('  work:\n', result)
