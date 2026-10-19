"""Small, conservative editor for Taskfile block mappings, not a YAML parser.

Only block mapping keys and literal include paths are interpreted. Task remains
responsible for validating values; ambiguous structures are refused before edits.
"""
from dataclasses import dataclass
import json
import re

NAMES = ('Taskfile.yml', 'Taskfile.yaml', 'taskfile.yml', 'taskfile.yaml',
         'Taskfile.dist.yml', 'Taskfile.dist.yaml', 'taskfile.dist.yml', 'taskfile.dist.yaml')
KEY = re.compile(r'''^( *)("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s'"{}\[\]#][^\n]*?):(?:[ \t]+(.*)|[ \t]*)$''')


def scalar(value):
    value = re.sub(r'\s+#.*$', '', value).strip()
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except ValueError as error:
            raise ValueError('Unsupported quoted Taskfile scalar') from error
    elif value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError('Unsupported quoted Taskfile scalar')
        decoded = value[1:-1].replace("''", "'")
    elif not value or any(char in value for char in '{}[]&*!|>'):
        raise ValueError('Expand unsupported Taskfile scalar before setup')
    else:
        decoded = value
    if '{{' in decoded:
        raise ValueError('Expand dynamic Taskfile scalar before setup')
    return decoded


def key_line(line):
    match = KEY.fullmatch(line.rstrip('\r\n'))
    if not match:
        raise ValueError('Expand unsupported Taskfile block mapping before setup')
    key = scalar(match[2])
    if key == '<<':
        raise ValueError('Expand Taskfile merge mappings before setup')
    return len(match[1]), key, (match[3] or '').strip()


def significant(line):
    return bool(line.strip()) and not line.lstrip().startswith('#')


@dataclass
class Mapping:
    lines: list
    start: int
    end: int
    indent: int
    entries: dict


def mapping(text, name, required=False):
    lines = text.splitlines(keepends=True)
    roots = []
    for index, line in enumerate(lines):
        if significant(line) and not line.startswith((' ', '\t')):
            _, key, value = key_line(line)
            roots.append((index, key, value))
    found = [(n, entry) for n, entry in enumerate(roots) if entry[1] == name]
    if len(found) > 1:
        raise ValueError('Duplicate Taskfile ' + name + ' mappings; review required')
    if not found:
        if required:
            raise ValueError('Expand the root Taskfile ' + name + ' mapping before setup')
        return None
    number, (start, _, value) = found[0]
    if value and not value.startswith('#'):
        raise ValueError('Expand the inline Taskfile ' + name + ' mapping before setup')
    end = roots[number + 1][0] if number + 1 < len(roots) else len(lines)
    meaningful = [i for i in range(start + 1, end) if significant(lines[i])]
    indent = len(lines[meaningful[0]]) - len(lines[meaningful[0]].lstrip(' ')) if meaningful else 2
    entries = {}
    for i in meaningful:
        line = lines[i]
        width = len(line) - len(line.lstrip(' '))
        if '\t' in line[:len(line) - len(line.lstrip())] or width < indent:
            raise ValueError('Inconsistent Taskfile ' + name + ' mapping indentation')
        if width == indent:
            _, key, value = key_line(line)
            if key in entries:
                raise ValueError('Duplicate Taskfile mapping key: ' + key)
            entries[key] = (i, value)
    return Mapping(lines, start, end, indent, entries)


def include_paths(text):
    """Return literal include locations; refuse dynamic/flow include definitions."""
    block = mapping(text, 'includes')
    if block is None:
        return {}
    result = {}
    items = list(block.entries.items())
    for n, (key, (start, value)) in enumerate(items):
        end = items[n + 1][1][0] if n + 1 < len(items) else block.end
        if value and not value.startswith('#'):
            result[key] = scalar(value)
            continue
        children = [line for line in block.lines[start + 1:end] if significant(line)]
        width = len(children[0]) - len(children[0].lstrip(' ')) if children else 0
        fields = [key_line(line) for line in children
                  if len(line) - len(line.lstrip(' ')) == width]
        paths = [scalar(value) for _, field, value in fields if field == 'taskfile']
        if len(paths) != 1:
            raise ValueError('Taskfile include requires one literal taskfile: ' + key)
        result[key] = paths[0]
    return result


def integrate_include(text):
    paths = include_paths(text)
    if 'devcontainer' in paths:
        if paths['devcontainer'] != '.devcontainer/Taskfile.yml':
            raise ValueError('The Taskfile already uses the devcontainer include namespace.')
        return text
    block = mapping(text, 'includes')
    indent = ' ' * (block.indent if block else 2)
    entry = (indent + 'devcontainer:\n' + indent + '  taskfile: .devcontainer/Taskfile.yml\n'
             + indent + '  optional: true\n')
    if block is None:
        return text.rstrip() + '\n\nincludes:\n' + entry
    lines = block.lines
    if not lines[block.start].endswith('\n'):
        lines[block.start] += '\n'
    lines.insert(block.start + 1, entry)
    return ''.join(lines)


def _flow_items(value, opening, closing):
    """Split only literal Task DSL flow lists/maps, respecting nested quotes."""
    value = value.strip()
    if not value.startswith(opening) or not value.endswith(closing):
        raise ValueError('unsupported Task reference flow layout')
    body, items, start, stack, quote, index = value[1:-1], [], 0, [], '', 0
    while index < len(body):
        char = body[index]
        if quote:
            if char == '\\' and quote == '"':
                index += 2
                continue
            if char == quote:
                if quote == "'" and body[index:index+2] == "''":
                    index += 2
                    continue
                quote = ''
        elif char in "\"'":
            quote = char
        elif char in '[{':
            stack.append(']' if char == '[' else '}')
        elif char in ']}':
            if not stack or stack.pop() != char:
                raise ValueError('unsupported Task reference flow nesting')
        elif char == '#':
            raise ValueError('expand flow comments before Task reference retirement')
        elif char == ',' and not stack:
            items.append(body[start:index].strip())
            start = index + 1
        index += 1
    if quote or stack:
        raise ValueError('unsupported Task reference quoting or nesting')
    if body[start:].strip():
        items.append(body[start:].strip())
    return items


def _flow_fields(value):
    fields = {}
    for item in _flow_items(value, '{', '}'):
        match = re.fullmatch(r'''("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^:\s]+)\s*:\s*(.*)''', item, re.S)
        if not match:
            raise ValueError('unsupported Task reference mapping')
        key = scalar(match[1])
        if key == '<<' or key in fields:
            raise ValueError('expand merged/duplicate Task reference fields')
        fields[key] = (match[2], [])
    return fields


def _block_fields(lines):
    meaningful = [i for i, line in enumerate(lines) if significant(line)]
    if not meaningful:
        return {}
    width = min(len(lines[i]) - len(lines[i].lstrip(' ')) for i in meaningful)
    starts = [i for i in meaningful if len(lines[i]) - len(lines[i].lstrip(' ')) == width
              and not lines[i].lstrip().startswith('-')]
    if not starts:
        raise ValueError('unsupported Task reference block fields')
    fields = {}
    for number, start in enumerate(starts):
        _, key, value = key_line(lines[start])
        if key in fields:
            raise ValueError('duplicate Task reference fields')
        end = starts[number + 1] if number + 1 < len(starts) else len(lines)
        fields[key] = (value, lines[start+1:end])
    return fields


def _list_items(value, lines):
    value = value.strip()
    if value:
        return [(item, []) for item in _flow_items(value, '[', ']')]
    meaningful = [i for i, line in enumerate(lines) if significant(line)]
    if not meaningful:
        return []
    width = min(len(lines[i]) - len(lines[i].lstrip(' ')) for i in meaningful)
    starts = [i for i in meaningful if len(lines[i]) - len(lines[i].lstrip(' ')) == width]
    result = []
    for number, start in enumerate(starts):
        match = re.fullmatch(r'\s*-\s+(.*)', lines[start].rstrip('\r\n'))
        if not match:
            raise ValueError('unsupported Task reference list item')
        end = starts[number + 1] if number + 1 < len(starts) else len(lines)
        # A list mapping's continuation keys align two columns after '-'.
        # Keep their relative depth so vars.task cannot masquerade as a call.
        # Discard YAML trivia before dedenting: slicing a list-level comment
        # could otherwise remove its '#' and manufacture scalar content.
        result.append((match[1], [line[width+2:] for line in lines[start+1:end]
                                  if significant(line)]))
    return result


def task_stanzas(text):
    """Expose scoped Task properties without interpreting shell command bodies."""
    block = mapping(text, 'tasks', required=True)
    items = list(block.entries.items())
    for number, (name, (start, value)) in enumerate(items):
        end = items[number + 1][1][0] if number + 1 < len(items) else block.end
        if value.startswith('{'):
            fields = _flow_fields(value)
        elif value.startswith('['):
            fields = {'cmds': (value, [])}
        elif value and not value.startswith('#'):
            # Task's string shorthand is a shell command, not a Task reference.
            if value.startswith(('*', '&', '!')):
                raise ValueError('expand anchored Task references before retirement: ' + name)
            fields = {}
        else:
            fields = _block_fields(block.lines[start+1:end])
        yield name, fields


def task_aliases(fields):
    aliases = []
    for value, children in _list_items(*fields.get('aliases', ('', []))):
        if children:
            raise ValueError('unsupported Task reference aliases; expand multiline aliases before retirement')
        aliases.append(scalar(value))
    return aliases


def task_references(fields):
    """Read literal cmds/task and deps targets; refuse unprovable references."""
    for field in ('cmds', 'deps'):
        for value, children in _list_items(*fields.get(field, ('', []))):
            value = value.strip()
            if value.startswith(('*', '&', '!')):
                raise ValueError('expand anchored Task reference before retirement')
            if value.startswith('{'):
                item = _flow_fields(value)
            elif KEY.fullmatch(value):
                item = _block_fields([value + '\n'] + children)
            elif field == 'deps':
                yield scalar(value)
                continue
            else:
                continue  # Shell commands, including literal blocks, are opaque.
            while 'defer' in item:
                deferred, nested = item['defer']
                if deferred.startswith('{'):
                    item = _flow_fields(deferred)
                elif not deferred:
                    item = _block_fields(nested)
                elif deferred.startswith(('*', '&', '!')):
                    raise ValueError('expand anchored deferred Task reference before retirement')
                else:
                    item = {}  # A deferred shell command is also opaque.
            if 'task' in item:
                target, nested = item['task']
                if nested or target.startswith(('|', '>')):
                    raise ValueError('expand multiline Task reference before retirement')
                yield scalar(target)


def include_namespaces(text):
    """Literal include namespaces, including aliases and flattening."""
    paths = include_paths(text)
    block = mapping(text, 'includes')
    if block is None:
        return
    items = list(block.entries.items())
    for number, (key, (start, value)) in enumerate(items):
        namespaces = [key]
        if not value or value.startswith('#'):
            end = items[number + 1][1][0] if number + 1 < len(items) else block.end
            fields = _block_fields(block.lines[start+1:end])
            namespaces += task_aliases(fields)
            flatten = fields.get('flatten', ('false', []))[0]
            if flatten not in ('true', 'false'):
                raise ValueError('expand dynamic Task include flattening before retirement')
            if flatten == 'true':
                namespaces = ['']
        yield paths[key], namespaces
