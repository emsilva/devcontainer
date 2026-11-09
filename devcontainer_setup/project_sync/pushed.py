"""Prove commit custody on the Mac without moving the owner's Git refs."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os
import re
import subprocess

from .state import Project


CHECK_NAMESPACE = 'refs/project-sync-check/heads/'
_UNREADABLE = 'GitHub could not be read from this Mac'
_INCOMPLETE = "this Mac's checkout is shallow or partial"
_OID = re.compile(r'[0-9a-f]{40}')
_ENV_DENYLIST = frozenset((
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_INDEX_VERSION',
    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_QUARANTINE_PATH',
    'GIT_NAMESPACE', 'GIT_SHALLOW_FILE', 'GIT_REPLACE_REF_BASE', 'GIT_GRAFT_FILE', 'GIT_CONFIG',
    'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM', 'GIT_CURL_VERBOSE',
))


@dataclass(frozen=True)
class Claim:
    path: str
    identity: str | None
    refs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Proof:
    path: str
    pushed: int
    unpushed: tuple[tuple[str, str], ...]
    unknown: tuple[tuple[str, str], ...]
    no_remote: bool


class _Git:
    def __init__(self, timeout):
        self.timeout = timeout
        self.env = {key: value for key, value in os.environ.items()
                    if key not in _ENV_DENYLIST and not key.startswith('GIT_TRACE')}
        self.env.update(GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1',
                        GIT_GRAFT_FILE='/dev/null', GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', LC_ALL='C')

    def run(self, cwd, *args, input=None):
        try:
            return subprocess.run(['git', '-c', 'credential.interactive=never', *args],
                                  cwd=cwd, env=self.env, input=input, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=self.timeout)
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            return None


def _github_url(identity: str) -> str:
    """Build only the canonical GitHub HTTPS URL; never accept URL credentials."""
    if not re.fullmatch(r'github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', identity):
        raise ValueError('Invalid GitHub repository identity')
    return 'https://' + identity + '.git'


def _cleanup(git, root):
    refs = git.run(root, 'for-each-ref', '--format=%(refname)', CHECK_NAMESPACE)
    if refs is None or refs.returncode:
        return False
    names = refs.stdout.splitlines()
    if not names:
        return True
    result = git.run(root, 'update-ref', '--no-deref', '--stdin',
                     input=''.join(f'delete {name}\n' for name in names))
    return result is not None and result.returncode == 0


def _read_project(git, root, ids):
    unknown = dict.fromkeys(ids, _UNREADABLE)
    shallow = git.run(root, 'rev-parse', '--is-shallow-repository')
    if shallow is None or shallow.returncode or shallow.stdout.strip() not in ('true', 'false'):
        return unknown
    if shallow.stdout.strip() == 'true':
        return dict.fromkeys(ids, _INCOMPLETE)
    for key in ('remote.origin.promisor', 'extensions.partialClone'):
        config = git.run(root, 'config', '--get', key)
        if config is None or config.returncode not in (0, 1):
            return unknown
        if config.returncode == 0:
            return dict.fromkeys(ids, _INCOMPLETE)

    # Retire stale destinations without following a symbolic ref to owner state.
    if not _cleanup(git, root):
        return unknown
    fetched = git.run(root, '-c', 'fetch.pruneTags=false', '-c', 'remote.origin.pruneTags=false',
                      '-c', 'fetch.writeCommitGraph=false', '-c', 'maintenance.auto=false',
                      '-c', 'gc.auto=0', 'fetch', '--quiet', '--prune', '--no-tags', '--refmap=',
                      '--no-write-fetch-head', '--no-recurse-submodules', 'origin',
                      '+refs/heads/*:' + CHECK_NAMESPACE + '*')
    if fetched is None or fetched.returncode:
        return unknown
    valid = [oid for oid in ids if _OID.fullmatch(oid)]
    if not valid:
        return unknown
    batch = git.run(root, 'cat-file', '--batch-check', input=''.join(oid + '\n' for oid in valid))
    if batch is None or batch.returncode:
        return unknown
    lines = batch.stdout.splitlines()
    if len(lines) != len(valid):
        return unknown
    results = unknown.copy()
    for oid, line in zip(valid, lines):
        if line == oid + ' missing':
            results[oid] = 'unpushed'
        elif re.fullmatch(re.escape(oid) + r' commit [0-9]+', line):
            contained = git.run(root, 'for-each-ref', '--count=1', '--contains', oid, CHECK_NAMESPACE)
            if contained is not None and contained.returncode == 0:
                results[oid] = 'pushed' if contained.stdout.strip() else 'unpushed'
    return results


def _project_statuses(git, root, ids):
    results = dict.fromkeys(ids, _UNREADABLE)
    try:
        results = _read_project(git, root, ids)
    finally:
        if not _cleanup(git, root):
            # A failed cleanup must never yield permission to delete a Codespace.
            results = dict.fromkeys(ids, _UNREADABLE)
    return results


def _other_statuses(git, identity, ids):
    reason = 'this Mac has no checkout of ' + identity.removeprefix('github.com/') + ' to compare'
    try:
        url = _github_url(identity)
    except ValueError:
        return dict.fromkeys(ids, 'not a GitHub repository')
    advertised = git.run('/', 'ls-remote', '--heads', url)
    if advertised is None or advertised.returncode:
        return dict.fromkeys(ids, _UNREADABLE)
    heads = set()
    for line in advertised.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) != 2 or not _OID.fullmatch(fields[0]) or not fields[1].startswith('refs/heads/'):
            return dict.fromkeys(ids, _UNREADABLE)
        heads.add(fields[0])
    return {oid: 'pushed' if oid in heads else reason for oid in ids}


def prove(project: Project, claims: Sequence[Claim], *, timeout: float = 60) -> tuple[Proof, ...]:
    """Return one ordered proof per claim; uncertainty never implies an unpushed commit."""
    git = _Git(timeout)
    ids = dict.fromkeys(oid for claim in claims if claim.identity == project.identity for _, oid in claim.refs)
    local = _project_statuses(git, project.root, ids) if ids else {}
    proofs = []
    for claim in claims:
        if not claim.refs or claim.identity is None:
            proofs.append(Proof(claim.path, 0, (), (), bool(claim.refs)))
            continue
        if claim.identity == project.identity:
            results = local
        elif claim.identity.startswith('github.com/'):
            results = _other_statuses(git, claim.identity, (oid for _, oid in claim.refs))
        else:
            results = dict.fromkeys((oid for _, oid in claim.refs), 'not a GitHub repository')
        pushed = 0
        unpushed, unknown = [], []
        for ref, oid in claim.refs:
            status = results[oid]
            if status == 'pushed':
                pushed += 1
            elif status == 'unpushed':
                unpushed.append((ref, oid[:12]))
            else:
                unknown.append((ref, status))
        proofs.append(Proof(claim.path, pushed, tuple(unpushed), tuple(unknown), False))
    return tuple(proofs)
