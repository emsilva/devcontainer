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
            return json.loads(value)
        except ValueError as error:
            raise ValueError('Unsupported quoted Taskfile scalar') from error
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if not value or any(char in value for char in '{}[]&*!|>') or '{{' in value:
        raise ValueError('Expand unsupported Taskfile scalar before setup')
    return value


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
    lines.insert(block.start + 1, entry)
    return ''.join(lines)
