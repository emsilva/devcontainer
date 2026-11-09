import hashlib
import re
import unittest
from devcontainer_setup.root_tasks import integrate


class RootTasksTests(unittest.TestCase):
    def test_work_cs_delete_renders_description_interactive_and_root_alias(self):
        import tempfile
        from pathlib import Path
        from devcontainer_setup.render import render
        from devcontainer_setup.root_tasks import ALIASES, INTERACTIVE
        source = Path(__file__).resolve().parents[1]
        self.assertIn('work:cs:delete', ALIASES)
        self.assertEqual(ALIASES[ALIASES.index('work:cs:restart') + 1], 'work:cs:delete')
        self.assertIn('work:cs:delete', INTERACTIVE)
        with tempfile.TemporaryDirectory() as directory:
            files = render(Path(directory), source, {'continuity': True, 'ssh': True}, 'a' * 40)
        root = integrate("version: '3'\ntasks:\n")
        for text in (root, files['.devcontainer/Taskfile.yml'].decode(), (source / 'Taskfile.yml').read_text()):
            stanza = re.split(r'\n(?=  \S)', text.split('  work:cs:delete:\n', 1)[1], maxsplit=1)[0]
            self.assertIn('interactive: true', stanza)
            if text == root:
                self.assertIn('task: devcontainer:work:cs:delete', stanza)
            else:
                self.assertIn('desc: Check a Codespace, then delete it after you type delete '
                              '(-- --check --json only checks)', stanza)
                self.assertIn('project-sync.sh delete {{.CLI_ARGS}}', stanza)
        self.assertEqual(integrate(root), root)
        custom = '  work:cs:delete:\n    cmds: [echo owner-delete]\n'
        self.assertIn(custom, integrate("version: '3'\ntasks:\n" + custom))

    def test_work_cs_delete_dispatches_in_both_taskfiles(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from devcontainer_setup.render import render
        source = Path(__file__).resolve().parents[1]
        for generated in (True, False):
            with self.subTest(generated=generated), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if generated:
                    files = render(root, source, {'continuity': True, 'ssh': True}, 'a' * 40)
                    included = root / '.devcontainer/Taskfile.yml'
                    included.parent.mkdir()
                    included.write_bytes(files['.devcontainer/Taskfile.yml'])
                    text = integrate("version: '3'\nincludes:\n  devcontainer: .devcontainer/Taskfile.yml\ntasks:\n")
                    script = root / '.devcontainer/scripts/project-sync.sh'
                else:
                    text = (source / 'Taskfile.yml').read_text()
                    script = root / 'scripts/project-sync.sh'
                (root / 'Taskfile.yml').write_text(text)
                script.parent.mkdir(parents=True, exist_ok=True)
                # The real task runner reaches only this local stub, never real project state.
                script.write_text('printf "%s\\n" "$@" > dispatch-args\n')
                result = subprocess.run(['task', 'work:cs:delete', '--', '--check', '--json'],
                                        cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / 'dispatch-args').read_text().splitlines(), ['delete', '--check', '--json'])

    def test_work_alias_has_a_description(self):
        from pathlib import Path
        from devcontainer_setup.root_tasks import ALIASES, INTERACTIVE
        source = Path(__file__).resolve().parents[1]
        stanza = ('  work:\n    desc: Show where this project stands and what to do next\n'
                  '    interactive: true\n    cmds:\n      - task: devcontainer:work\n'
                  '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n')
        root = integrate("version: '3'\ntasks:\n  owner:\n    cmds: [echo preserved]\n")
        self.assertIn(stanza, root)
        self.assertIn('work', ALIASES)
        self.assertIn('work', INTERACTIVE)
        self.assertEqual(integrate(root), root)
        self.assertIn('  owner:\n    cmds: [echo preserved]\n', root)
        old = '  work:cs:\n    cmds:\n      - task: devcontainer:work:cs\n'
        old += '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n'
        previous = ('tasks:\n  # devcontainer-setup:work-tasks begin sha256='
                    + hashlib.sha256(old.encode()).hexdigest() + '\n' + old
                    + '  # devcontainer-setup:work-tasks end\n')
        self.assertIn(stanza, integrate(previous))
        for path, script in ((source / 'Taskfile.yml', 'scripts/project-sync.sh'),
                             (source / 'templates/continuity-tasks.yml', '.devcontainer/scripts/project-sync.sh')):
            text = path.read_text()
            self.assertIn('  work:\n    desc: Show where this project stands and what to do next\n'
                          '    interactive: true\n    cmds:\n      - bash ' + script + ' work {{.CLI_ARGS}}\n', text)

    def test_work_forget_renders_description_and_noninteractive_root_alias(self):
        import tempfile
        from pathlib import Path
        from devcontainer_setup.render import render
        from devcontainer_setup.root_tasks import ALIASES, INTERACTIVE
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            files = render(Path(directory), source, {'continuity': True, 'ssh': True}, 'a' * 40)
        self.assertIn('work:forget', ALIASES)
        self.assertNotIn('work:forget', INTERACTIVE)
        root = integrate("version: '3'\ntasks:\n")
        for text in (root, files['.devcontainer/Taskfile.yml'].decode(), (source / 'Taskfile.yml').read_text()):
            stanza = re.split(r'\n(?=  \S)', text.split('  work:forget:\n', 1)[1], maxsplit=1)[0]
            self.assertNotIn('interactive:', stanza)
            if text == root:
                self.assertIn('task: devcontainer:work:forget', stanza)
            else:
                self.assertIn('desc: Stop sending a conversation the Codespace refused', stanza)
                self.assertIn('work --forget {{.CLI_ARGS}}', stanza)
        self.assertIn('.devcontainer/scripts/project_sync/forget.py', files)
        self.assertEqual(integrate(root), root)

    def test_work_signin_all_renders_description_interactive_and_root_alias(self):
        import tempfile
        from pathlib import Path
        from devcontainer_setup.render import render
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            files = render(Path(directory), source, {'continuity': True, 'ssh': True}, 'a' * 40)
        included = files['.devcontainer/Taskfile.yml'].decode()
        stanza = included.split('  work:signin:all:\n', 1)[1].split('\n  work:', 1)[0]
        self.assertIn('    desc: Sign in to every tool on the paired Codespace', stanza)
        self.assertIn('    interactive: true', stanza)
        self.assertIn('work --signin-all {{.CLI_ARGS}}', stanza)
        root = integrate("version: '3'\ntasks:\n")
        self.assertIn('  work:signin:all:\n    interactive: true\n', root)
        self.assertIn('task: devcontainer:work:signin:all\n', root)
        self.assertEqual(integrate(root), root)
        self.assertIn('  work:signin:all:\n', (source / 'Taskfile.yml').read_text())

    def test_work_signin_renders_description_interactive_and_root_alias(self):
        import tempfile
        from pathlib import Path
        from devcontainer_setup.render import render
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            files = render(Path(directory), source, {'continuity': True, 'ssh': True}, 'a' * 40)
        included = files['.devcontainer/Taskfile.yml'].decode()
        stanza = included.split('  work:signin:\n', 1)[1].split('\n  work:', 1)[0]
        self.assertIn('    desc:', stanza)
        self.assertIn('    interactive: true', stanza)
        self.assertIn('work --signin {{.CLI_ARGS}}', stanza)
        root = integrate("version: '3'\ntasks:\n")
        self.assertIn('  work:signin:\n    interactive: true\n', root)
        self.assertIn('task: devcontainer:work:signin\n', root)
        self.assertEqual(integrate(root), root)

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

    def test_new_work_tasks_render_and_alias(self):
        from pathlib import Path
        from devcontainer_setup.root_tasks import ALIASES
        new = ('work:cs:armed', 'work:disarm', 'work:hold', 'work:cs:stop', 'work:cs:restart', 'work:logs')
        template = (Path(__file__).resolve().parents[1] / 'templates/continuity-tasks.yml').read_text()
        generator = (Path(__file__).resolve().parents[1] / 'Taskfile.yml').read_text()
        changed = integrate("version: '3'\ntasks:\n")
        for name in new:
            self.assertIn(name, ALIASES)
            self.assertIn('  ' + name + ':\n', template)
            self.assertIn('  ' + name + ':\n', generator)
            self.assertIn('task: devcontainer:' + name + '\n', changed)
        for name in ('work:cs:armed', 'work:cs:restart'):
            self.assertIn('  ' + name + ':\n    interactive: true\n', changed)
        for name in ('work:disarm', 'work:hold', 'work:cs:stop', 'work:logs'):
            for text in (changed, template, generator):
                stanza = re.split(r'\n(?=  \S)', text.split('  ' + name + ':\n', 1)[1], maxsplit=1)[0]
                self.assertNotIn('interactive:', stanza)
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
        self.assertIn('  work:\n    desc: Show where this project stands and what to do next\n'
                      '    interactive: true\n    cmds:\n      - task: devcontainer:work\n'
                      '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n', changed)
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

    def test_mapping_layouts_refused_before_rewrite(self):
        for text in ('tasks: {owner: {cmds: [echo owner]}}\n',
                     'tasks:\n  owner: {cmds: [echo owner]}\ntasks:\n',
                     'tasks:\n  owner: {}\n    hidden: {}\n bad: {}\n',
                     'tasks:\n  <<: *owner\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                integrate(text)

    def test_disable_validates_and_preserves_custom_alias(self):
        custom = '    "work:cs":\n        cmds: [echo CUSTOM]\n'
        text = 'version: "3"\ntasks:\n' + custom
        enabled = integrate(text)
        disabled = integrate(enabled, enabled=False)
        self.assertIn(custom, disabled)
        self.assertNotIn('devcontainer:work:status', disabled)
        self.assertEqual(disabled, integrate(disabled, enabled=False))
        with self.assertRaisesRegex(ValueError, 'Edited'):
            integrate(enabled.replace('devcontainer:work:status', 'devcontainer:other'), enabled=False)

    def test_misplaced_managed_block_refused(self):
        managed = integrate('tasks:\n')
        block = managed.split('tasks:\n', 1)[1]
        with self.assertRaisesRegex(ValueError, 'Malformed'):
            integrate('tasks:\n  owner: {}\nvars:\n' + block)

    def test_empty_mapping_without_final_newline_is_supported(self):
        self.assertIn('tasks:\n  #', integrate('tasks:'))
