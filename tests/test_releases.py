from pathlib import Path
from contextlib import chdir
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from devcontainer_setup.releases import fetch_release


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                   stderr=subprocess.PIPE).strip()


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.cache = self.root / 'cache'
        git(self.source, 'init', '-b', 'main')
        git(self.source, 'config', 'user.name', 'Release Test')
        git(self.source, 'config', 'user.email', 'release@example.com')
        (self.source / 'scripts').mkdir()
        (self.source / 'scripts/setup.sh').write_text('#!/bin/sh\nexit 0\n')
        (self.source / 'devcontainer_setup').mkdir()
        (self.source / 'devcontainer_setup/__main__.py').write_text('print("wizard")\n')
        self.commit()

    def commit(self):
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'Release')
        return git(self.source, 'rev-parse', 'HEAD')

    def test_branch_resolves_to_immutable_revision_and_advances_separately(self):
        original = git(self.source, 'rev-parse', 'HEAD')
        release, sha = fetch_release(str(self.source), cache=self.cache)
        self.assertEqual(sha, original)
        self.assertEqual(release, self.cache / sha)
        self.assertEqual(git(release, 'rev-parse', 'HEAD'), sha)
        (self.source / 'new.txt').write_text('next release')
        next_sha = self.commit()
        newer, actual = fetch_release(str(self.source), cache=self.cache)
        self.assertEqual(actual, next_sha)
        self.assertNotEqual(newer, release)
        self.assertFalse((release / 'new.txt').exists())

    def test_cache_reuse_preserves_existing_contents(self):
        release, sha = fetch_release(str(self.source), cache=self.cache)
        marker = release / 'owner-note'
        marker.write_text('keep this')
        original_stat = (release / 'scripts/setup.sh').stat()
        self.assertEqual(fetch_release(str(self.source), cache=self.cache), (release, sha))
        self.assertEqual(marker.read_text(), 'keep this')
        self.assertEqual((release / 'scripts/setup.sh').stat().st_mtime_ns,
                         original_stat.st_mtime_ns)
        self.assertEqual(list(self.cache.iterdir()), [release])

    def test_full_sha_ref_and_file_url_support_old_revision(self):
        original = git(self.source, 'rev-parse', 'HEAD')
        (self.source / 'new.txt').write_text('next')
        self.commit()
        release, sha = fetch_release(self.source.as_uri(), original, self.cache)
        self.assertEqual(sha, original)
        self.assertFalse((release / 'new.txt').exists())

    def test_tag_resolves_to_commit(self):
        git(self.source, 'tag', '-a', 'v1', '-m', 'Version one')
        release, sha = fetch_release(str(self.source), 'v1', self.cache)
        self.assertEqual(sha, git(self.source, 'rev-parse', 'v1^{commit}'))
        self.assertTrue((release / 'scripts/setup.sh').is_file())

    def test_missing_wizard_preserves_old_release_and_cleans_temporary_fetch(self):
        old, _ = fetch_release(str(self.source), cache=self.cache)
        (self.source / 'devcontainer_setup/__main__.py').unlink()
        self.commit()
        with self.assertRaisesRegex(ValueError, 'wizard|__main__'):
            fetch_release(str(self.source), cache=self.cache)
        self.assertEqual(list(self.cache.iterdir()), [old])
        self.assertTrue((old / 'devcontainer_setup/__main__.py').is_file())

    def test_failed_fetch_preserves_cache_and_cleans_only_own_temporary_path(self):
        old, _ = fetch_release(str(self.source), cache=self.cache)
        unrelated = self.cache / '.someone-elses-work'
        unrelated.mkdir()
        (unrelated / 'data').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'fetch|Fetch'):
            fetch_release(str(self.source), 'missing-branch', self.cache)
        self.assertEqual(set(self.cache.iterdir()), {old, unrelated})
        self.assertEqual((unrelated / 'data').read_text(), 'keep')

    def test_existing_invalid_cache_is_never_replaced(self):
        sha = git(self.source, 'rev-parse', 'HEAD')
        occupied = self.cache / sha
        occupied.mkdir(parents=True)
        (occupied / 'owner-data').write_text('untouched')
        with self.assertRaisesRegex(ValueError, 'cache|Cache'):
            fetch_release(str(self.source), cache=self.cache)
        self.assertEqual(list(occupied.iterdir()), [occupied / 'owner-data'])
        self.assertEqual((occupied / 'owner-data').read_text(), 'untouched')
        self.assertEqual(list(self.cache.iterdir()), [occupied])

    def test_default_cache_uses_xdg_cache_directory(self):
        with patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'xdg')}):
            release, sha = fetch_release(str(self.source))
        self.assertEqual(release, self.root / 'xdg/devcontainer-setup/releases' / sha)

    def test_modified_cached_wizard_is_preserved_and_refused(self):
        release, _ = fetch_release(str(self.source), cache=self.cache)
        script = release / 'scripts/setup.sh'
        script.write_text('owner modification\n')
        with self.assertRaisesRegex(ValueError, 'cache.*modified|modified.*cache'):
            fetch_release(str(self.source), cache=self.cache)
        self.assertEqual(script.read_text(), 'owner modification\n')

    def test_relative_local_repository_uses_callers_working_directory(self):
        with chdir(self.root):
            release, sha = fetch_release('source', cache=self.cache)
        self.assertEqual(sha, git(self.source, 'rev-parse', 'HEAD'))
        self.assertTrue((release / 'scripts/setup.sh').is_file())


if __name__ == '__main__':
    unittest.main()
