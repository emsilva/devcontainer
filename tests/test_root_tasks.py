import hashlib
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
            integrate(text.replace('devcontainer:work:cs\n', 'devcontainer:other\n'))

    def test_native_agent_tasks_retired_from_owned_block_only(self):
        old = ''.join('  ' + alias + ':\n    interactive: true\n    cmds:\n'
                      '      - task: devcontainer:' + alias + '\n'
                      '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n'
                      for alias in ('work', 'work:codex', 'work:claude', 'work:cs'))
        owned = ('  # devcontainer-setup:work-tasks begin sha256='
                 + hashlib.sha256(old.encode()).hexdigest() + '\n' + old
                 + '  # devcontainer-setup:work-tasks end\n')
        custom = '  "work:codex":\n    cmds: [echo owner-agent]\n'
        original = 'version: "3"\ntasks:\n' + owned + custom
        changed = integrate(original)
        self.assertIn(custom, changed)
        self.assertNotIn('task: devcontainer:work\n', changed)
        self.assertNotIn('task: devcontainer:work:codex\n', changed)
        self.assertNotIn('task: devcontainer:work:claude\n', changed)
        self.assertIn('task: devcontainer:work:cs\n', changed)
        self.assertIn('task: devcontainer:work:disconnect\n', changed)
        self.assertEqual(integrate(changed), changed)

    def test_quoted_custom_work_preserved(self):
        text = 'tasks:\n  "work":\n    cmds: [echo mine]\n'
        result = integrate(text)
        self.assertIn('  "work":\n    cmds: [echo mine]', result)
        self.assertNotIn('  work:\n', result)

    def test_extended_legacy_delegate_is_owner_content(self):
        custom = ('  agent:handoff:\n    cmds:\n      - task: devcontainer:agent:handoff\n'
                  '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n    env: {OWNER: yes}\n')
        self.assertIn(custom, integrate('tasks:\n' + custom))
