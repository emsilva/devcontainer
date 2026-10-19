"""Owned root Task aliases, with exact legacy producer-stanza migration."""
import hashlib
import re
from .taskfile_mapping import mapping

ALIASES = ('work:cs', 'work:disconnect', 'work:status', 'work:stop',
           'scratch:configure', 'scratch:start', 'scratch:stop', 'scratch:status', 'scratch:logs', 'scratch:backup')
LEGACY = ('work', 'work:codex', 'work:claude', 'work:codex:cs', 'work:claude:cs',
          'agent:codex', 'agent:claude', 'agent:sessions', 'agent:handoff', 'agent:recover',
          'scratch:configure', 'scratch:start', 'scratch:stop', 'scratch:status', 'scratch:logs', 'scratch:backup')
LABEL = 'devcontainer-setup:work-tasks'


def integrate(text, enabled=True):
    pattern = re.compile(r'^( +)# '+LABEL+r' begin sha256=([0-9a-f]{64})\n(.*?)^\1# '+LABEL+r' end\n', re.M | re.S)
    if LABEL in text:
        matches = list(pattern.finditer(text))
        if len(matches) != 1 or text.count(LABEL) != 2:
            raise ValueError('Malformed managed work task block; review required')
        match = matches[0]
        if hashlib.sha256(match[3].encode()).hexdigest() != match[2]:
            raise ValueError('Edited managed work task block; review required')
        text = text[:match.start()] + text[match.end():]
        owner = mapping(text, 'tasks', required=True)
        start = sum(map(len, owner.lines[:owner.start+1]))
        end = sum(map(len, owner.lines[:owner.end]))
        if not start <= match.start() <= end or len(match[1]) != owner.indent:
            raise ValueError('Malformed managed work task block placement; review required')
    block = mapping(text, 'tasks', required=True)
    indent = ' ' * block.indent
    body = ''.join(block.lines[block.start+1:block.end])
    # Only exact stanzas emitted by the earlier producer are ours to retire.
    for alias in LEGACY:
        for interactive in ('', '    interactive: true\n'):
            stanza = (f'  {alias}:\n' + interactive + '    cmds:\n'
                      f'      - task: devcontainer:{alias}\n'
                      '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n')
            exact = re.escape('\n' + stanza) + r'(?=(?:\n)*(?:  [^ \t#\n]|\Z))'
            body = re.sub(exact, '\n', '\n' + body).lstrip('\n')
    additions = ''
    existing = mapping('tasks:\n' + body, 'tasks', required=True).entries
    for alias in ALIASES if enabled else ():
        if alias in existing:
            continue
        additions += (indent + alias + ':\n'
                      + (indent + '  interactive: true\n' if alias == 'work:cs' else '')
                      + indent + '  cmds:\n' + indent + f'    - task: devcontainer:{alias}\n'
                      + indent + '      vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n')
    owned = (indent + '# '+LABEL+' begin sha256='+hashlib.sha256(additions.encode()).hexdigest()+'\n'
             + additions + indent + '# '+LABEL+' end\n') if additions else ''
    prefix = ''.join(block.lines[:block.start+1])
    if owned and not prefix.endswith('\n'):
        prefix += '\n'
    return prefix + owned + body + ''.join(block.lines[block.end:])
