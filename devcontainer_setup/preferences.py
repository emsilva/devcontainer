"""User-scoped preferences, validated before atomic private-file replacement."""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import tempfile

DEFAULTS = {'version': 1, 'apply_dotfiles': True, 'dotfiles_repo': '', 'agentic_tools': True}


def config_path(env: Mapping | None = None) -> Path:
    environment = os.environ if env is None else env
    configured = environment.get('XDG_CONFIG_HOME', '')
    if configured and Path(configured).is_absolute():
        root = Path(configured)
    else:
        home = environment.get('HOME')
        root = (Path(home) if home else Path.home()) / '.config'
    return root / 'devcontainer-setup' / 'config.json'


def _validate(values: dict, path: Path) -> None:
    def invalid(reason):
        raise ValueError(f'Invalid preferences at {path}: {reason}. Repair this file before saving preferences.')
    if not isinstance(values, dict):
        invalid('expected a JSON object')
    if set(values) - set(DEFAULTS):
        invalid('unrecognized fields: ' + ', '.join(sorted(set(values) - set(DEFAULTS))))
    if 'version' in values and (type(values['version']) is not int or values['version'] != 1):
        invalid('version must be 1')
    for field in ('apply_dotfiles', 'agentic_tools'):
        if field in values and type(values[field]) is not bool:
            invalid(f'{field} must be true or false')
    if 'dotfiles_repo' in values:
        repo = values['dotfiles_repo']
        if not isinstance(repo, str) or any(ord(character) < 32 or ord(character) == 127 for character in repo):
            invalid('dotfiles_repo must be a string without control characters')


def load_preferences(env: Mapping | None = None) -> dict:
    path = config_path(env)
    try:
        content = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return DEFAULTS.copy()
    try:
        values = json.loads(content)
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'Invalid preferences at {path}: {error}. Repair this file before saving preferences.') from error
    _validate(values, path)
    return DEFAULTS | values


def save_preferences(values: dict, env: Mapping | None = None) -> Path:
    path = config_path(env)
    _validate(values, path)
    merged = load_preferences(env) | values
    _validate(merged, path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(merged, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
