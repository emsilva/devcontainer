"""Whether GitHub's default branch holds the project this checkout tracks.

A Codespace starts from GitHub's default branch. When that branch holds fewer than half of the files
tracked here, the Codespace would hold almost none of the project, so the setup stops before it asks,
writes or pushes anything. Read-only apart from refreshing refs/remotes/origin/<default>; with nothing
to compare (no commit here, no fetched default) it passes and today's flow decides.
"""
from dataclasses import dataclass
from pathlib import Path
import subprocess

from .git_ops import _fetch_default, default_branch, git, is_ancestor

# What the setup itself writes: the same on both sides once a setup has landed, so it proves nothing.
SETUP_FILES = '.devcontainer/'


@dataclass(frozen=True)
class Holding:
    default: str
    branch: str | None
    held: int
    tracked: int

    @property
    def refused(self) -> bool:
        return self.tracked != 0 and self.held * 2 < self.tracked


def _commit(project: Path, ref: str) -> str:
    return git(project, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}', check=False)


def _project_files(project: Path, commit: str) -> set[bytes]:
    # Raw bytes split on NUL: a stripped or decoded listing would change real file names.
    listed = subprocess.run(['git', '-C', str(project), 'ls-tree', '-r', '--name-only', '-z', commit],
                            capture_output=True, check=True).stdout
    return {path for path in listed.split(b'\0') if path and not path.startswith(SETUP_FILES.encode())}


def measure(project: Path) -> Holding | None:
    """How many of the files tracked at HEAD GitHub's default branch also has; None when it holds them all."""
    default = default_branch(project)
    # When GitHub does not answer, the last fetch serves.
    _fetch_default(project, default)
    tip, head = _commit(project, f'refs/remotes/origin/{default}'), _commit(project, 'HEAD')
    if not tip or not head or is_ancestor(project, head, tip):
        return None
    here, there = _project_files(project, head), _project_files(project, tip)
    branch = git(project, 'branch', '--show-current', check=False) or None
    return Holding(default, branch, len(here & there), len(here))


def refusal(project: Path) -> str | None:
    """The three lines that stop the setup, or None when the default branch holds enough of the project."""
    holding = measure(project)
    if holding is None or not holding.refused:
        return None
    default, files = holding.default, 'file' if holding.tracked == 1 else 'files'
    if holding.branch == default:
        return '\n'.join((
            f"GitHub's {default} holds {holding.held} of the {holding.tracked} {files} tracked on this Mac's {default}.",
            'A Codespace starts from GitHub, so it would hold almost none of this project.',
            f"Push this Mac's {default} to GitHub, or merge it there, first."))
    where = holding.branch or 'this detached checkout'
    return '\n'.join((
        f"GitHub's {default} holds {holding.held} of the {holding.tracked} {files} tracked on {where}.",
        f'A Codespace starts from {default}, so it would hold almost none of this project.',
        f'Merge the project into {default} first, then run the setup from a checkout on {default}.'))
