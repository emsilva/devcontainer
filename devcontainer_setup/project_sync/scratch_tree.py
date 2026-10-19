"""One eligibility policy for local/remote scratch trees and Mutagen."""
from pathlib import Path
import os
import stat

# Leading slashes anchor tracked markers at the scratch root. Other names are
# transient at every depth; prune them before inspecting any excluded links.
MUTAGEN_IGNORES = ('/.gitignore', '/README.md', '.DS_Store', '__pycache__',
                   '.venv', 'node_modules', '.cache', '.project-sync', '.handoff', '.mutagen')


def eligible(relative, *, backup=False):
    """Backups also retain root markers; Git ignore rules never hide payloads."""
    relative = Path(relative)
    for pattern in MUTAGEN_IGNORES:
        if pattern.startswith('/'):
            if not backup and relative.as_posix() == pattern[1:]:
                return False
        elif pattern in relative.parts:
            return False
    return True


def entries(root, *, backup=False):
    """Yield validated entries without following links or hiding scan failures."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Scratch root must be an existing directory without a symlink.')
    def walk(directory):
        # scandir failures propagate, unlike os.walk's default silent omission.
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda child: child.name)
        folded = set()
        for child in children:
            item = directory / child.name
            relative = item.relative_to(root)
            if not eligible(relative, backup=backup):
                continue
            if child.name.casefold() in folded:
                raise ValueError('Scratch case collisions are unsupported; preserve and resolve them explicitly.')
            folded.add(child.name.casefold())
            mode = child.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                raise ValueError('Scratch symlinks are unsupported; preserve and resolve them explicitly.')
            if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
                raise ValueError('Scratch contains an unsupported special file.')
            yield item, relative
            if stat.S_ISDIR(mode):
                yield from walk(item)
    yield from walk(root)


def add_backup(archive, root):
    """Archive eligible payloads and markers, excluding transient cache trees."""
    # Validate before archive.add can inspect the root, including linked roots.
    payloads = list(entries(root, backup=True))
    archive.add(root, arcname='.scratch', recursive=False)
    for item, relative in payloads:
        archive.add(item, arcname='.scratch/' + relative.as_posix(), recursive=False)
