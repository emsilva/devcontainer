"""Owned root Task aliases, with exact legacy producer-stanza migration."""
import hashlib
import re

ALIASES = ('work', 'work:cs', 'work:disconnect', 'work:status', 'work:stop', 'work:codex', 'work:claude',
           'scratch:configure', 'scratch:start', 'scratch:stop', 'scratch:status', 'scratch:logs', 'scratch:backup')
LEGACY = ('work', 'work:codex', 'work:claude', 'work:codex:cs', 'work:claude:cs',
          'agent:codex', 'agent:claude', 'agent:sessions', 'agent:handoff', 'agent:recover',
          'scratch:configure', 'scratch:start', 'scratch:stop', 'scratch:status', 'scratch:logs', 'scratch:backup')
LABEL = 'devcontainer-setup:work-tasks'


def integrate(text):
    pattern = re.compile(r'^  # '+LABEL+r' begin sha256=([0-9a-f]{64})\n(.*?)^  # '+LABEL+r' end\n', re.M | re.S)
    if LABEL in text:
        matches = list(pattern.finditer(text))
        if len(matches) != 1 or text.count(LABEL) != 2:
            raise ValueError('Malformed managed work task block; review required')
        match = matches[0]
        if hashlib.sha256(match[2].encode()).hexdigest() != match[1]:
            raise ValueError('Edited managed work task block; review required')
        text = text[:match.start()] + text[match.end():]
    tasks = re.search(r'^tasks:[ \t]*(?:#.*)?$', text, re.M)
    if tasks is None:
        raise ValueError('Expand the root Taskfile tasks mapping before adding continuity aliases')
    boundary = re.search(r'^[^\s#]', text[tasks.end()+1:], re.M)
    end = tasks.end()+1+boundary.start() if boundary else len(text)
    body = text[tasks.end():end]
    # Only exact stanzas emitted by the earlier producer are ours to retire.
    for alias in LEGACY:
        for interactive in ('', '    interactive: true\n'):
            stanza = (f'  {alias}:\n' + interactive + '    cmds:\n'
                      f'      - task: devcontainer:{alias}\n'
                      '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n')
            exact = re.escape('\n' + stanza) + r'(?=(?:\n)*(?:  [^ \t#\n]|\Z))'
            body = re.sub(exact, '\n', body)
    additions = ''
    for alias in ALIASES:
        key = re.escape(alias)
        if re.search(rf'''^  (?:{key}:(?=\s|$)|'{key}':|"{key}":)''', body, re.M):
            continue
        additions += (f'  {alias}:\n' + ('    interactive: true\n' if alias in ('work', 'work:cs', 'work:codex', 'work:claude') else '')
                      + '    cmds:\n' + f'      - task: devcontainer:{alias}\n'
                      + '        vars: {CLI_ARGS: "{{.CLI_ARGS}}"}\n')
    block = ('  # '+LABEL+' begin sha256='+hashlib.sha256(additions.encode()).hexdigest()+'\n'
             + additions + '  # '+LABEL+' end\n') if additions else ''
    return text[:tasks.end()] + '\n' + block + body.lstrip('\n') + text[end:]
