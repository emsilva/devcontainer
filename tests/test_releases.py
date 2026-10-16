from pathlib import Path
from contextlib import chdir
import io
import os
import py_compile
import shutil
import subprocess
import tarfile
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

    def archive_cache(self):
        sha = git(self.source, 'rev-parse', 'HEAD')
        release = self.cache / sha
        release.mkdir(parents=True)
        archive = subprocess.check_output(['git', '-C', str(self.source), 'archive', sha])
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            # This archive contains only the local, test-authored fixture.
            stream.extractall(release)
        return release, sha

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

    def test_archive_cache_reused_without_git_metadata_and_preserves_owner_notes(self):
        release, sha = self.archive_cache()
        self.assertFalse((release / '.git').exists())
        marker = release / 'owner-note'
        marker.write_text('keep me')
        original_mtime = (release / 'scripts/setup.sh').stat().st_mtime_ns
        self.assertEqual(fetch_release(str(self.source), cache=self.cache), (release, sha))
        self.assertEqual(marker.read_text(), 'keep me')
        self.assertEqual((release / 'scripts/setup.sh').stat().st_mtime_ns, original_mtime)
        self.assertFalse((release / '.git').exists())

    def test_archive_cache_rejects_modified_nonentrypoint_source_and_mode(self):
        helper = self.source / 'devcontainer_setup/helper.py'
        helper.write_text('VALUE = 1\n')
        self.commit()
        release, _ = self.archive_cache()
        cached = release / 'devcontainer_setup/helper.py'
        cached.write_text('VALUE = 2\n')
        with self.assertRaisesRegex(ValueError, 'modified'):
            fetch_release(str(self.source), cache=self.cache)
        self.assertEqual(cached.read_text(), 'VALUE = 2\n')
        cached.write_bytes(helper.read_bytes())
        cached.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'modified'):
            fetch_release(str(self.source), cache=self.cache)
        self.assertEqual(cached.stat().st_mode & 0o111, 0o111)

    def test_relative_xdg_cache_home_falls_back_to_home_cache(self):
        home = self.root / 'home'
        with chdir(self.root), patch.dict(os.environ, {'XDG_CACHE_HOME': 'relative-cache'}), \
                patch('pathlib.Path.home', return_value=home):
            release, sha = fetch_release(str(self.source))
        self.assertEqual(release, home / '.cache/devcontainer-setup/releases' / sha)

    def test_archive_cache_checks_tracked_symlink_target(self):
        (self.source / 'target.txt').write_text('source bytes')
        (self.source / 'link.txt').symlink_to('target.txt')
        self.commit()
        release, sha = self.archive_cache()
        self.assertEqual(fetch_release(str(self.source), cache=self.cache), (release, sha))
        cached_link = release / 'link.txt'
        cached_link.unlink()
        cached_link.symlink_to('owner-note')
        with self.assertRaisesRegex(ValueError, 'modified'):
            fetch_release(str(self.source), cache=self.cache)
        self.assertEqual(os.readlink(cached_link), 'owner-note')


    def execution_source(self):
        implementation = Path(__file__).resolve().parents[1]
        for name in ('__init__.py', 'releases.py', 'ui.py'):
            shutil.copyfile(implementation / 'devcontainer_setup' / name,
                            self.source / 'devcontainer_setup' / name)
        (self.source / 'scripts/setup.sh').write_text('printf "trusted-runner\\n"\n')
        (self.source / 'devcontainer_setup/helper.py').write_text('VALUE = 1\n')
        return self.commit()

    def test_execution_entrypoints_refuse_modified_cached_source(self):
        from devcontainer_setup.__main__ import _ensure_runner
        implementation = Path(__file__).resolve().parents[1]
        sha = self.execution_source()
        for entry in ('installer', 'runner', 'ensure'):
            for name in ('scripts/setup.sh', 'devcontainer_setup/helper.py'):
                with self.subTest(entry=entry, name=name):
                    cache_base = self.root / f'{entry}-{Path(name).name}'
                    self.cache = cache_base / 'devcontainer-setup/releases'
                    release, _ = self.archive_cache()
                    target = release / name
                    content = 'printf "modified-cache-runner\\n"\n' if name.endswith('.sh') else 'VALUE = 99\n'
                    target.write_text(content)
                    env = dict(os.environ, XDG_CACHE_HOME=str(cache_base),
                               SETUP_DEVCONTAINER_REPO=str(self.source), SETUP_DEVCONTAINER_REF=sha)
                    if entry == 'ensure':
                        with patch.dict(os.environ, env), patch('devcontainer_setup.__main__.SOURCE', self.source):
                            with self.assertRaisesRegex(ValueError, 'modified'):
                                _ensure_runner(sha)
                    else:
                        if entry == 'installer':
                            command = ['sh', '-s', '--', '--scan']
                            script = (implementation / 'install.sh').read_text()
                        else:
                            command = ['bash', '-s', '--', '--check']
                            script = (implementation / 'templates/scripts/setup-wizard.sh').read_text().replace('__SOURCE_REF__', sha)
                        result = subprocess.run(command, input=script, text=True, capture_output=True,
                                                cwd=self.source, env=env)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn('modified', result.stderr)
                        self.assertNotIn('modified-cache-runner', result.stdout)
                    self.assertEqual(target.read_text(), content)

    def test_execution_entrypoints_accept_valid_archive_cache(self):
        from devcontainer_setup.__main__ import _ensure_runner
        implementation = Path(__file__).resolve().parents[1]
        sha = self.execution_source()
        cache_base = self.root / 'valid'
        self.cache = cache_base / 'devcontainer-setup/releases'
        release, _ = self.archive_cache()
        (release / 'owner-note').write_text('keep')
        env = dict(os.environ, XDG_CACHE_HOME=str(cache_base), SETUP_DEVCONTAINER_REPO=str(self.source),
                   SETUP_DEVCONTAINER_REF=sha)
        for script_path, shell, argument in (('install.sh', 'sh', '--scan'),
                ('templates/scripts/setup-wizard.sh', 'bash', '--check')):
            script = (implementation / script_path).read_text().replace('__SOURCE_REF__', sha)
            result = subprocess.run([shell, '-s', '--', argument], input=script, text=True,
                                    capture_output=True, cwd=self.source, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'trusted-runner\n')
        with patch.dict(os.environ, env), patch('devcontainer_setup.__main__.SOURCE', self.source):
            _ensure_runner(sha)
        self.assertFalse((release / '.git').exists())
        self.assertEqual((release / 'owner-note').read_text(), 'keep')


    def test_execution_entrypoints_ignore_forged_cached_bytecode(self):
        implementation = Path(__file__).resolve().parents[1]
        self.execution_source()
        shutil.copyfile(implementation / 'scripts/setup.sh', self.source / 'scripts/setup.sh')
        main = self.source / 'devcontainer_setup/__main__.py'
        authentic = b'print("TRUSTED_SOURCE")\n'
        forged = b'print("UNVERIFIED_PYC")\n'
        self.assertEqual(len(authentic), len(forged))
        main.write_bytes(authentic)
        sha = self.commit()
        for entry, shell, argument in (('install.sh', 'sh', '--scan'),
                ('templates/scripts/setup-wizard.sh', 'bash', '--check')):
            with self.subTest(entry=entry):
                cache_base = self.root / shell
                self.cache = cache_base / 'devcontainer-setup/releases'
                release, _ = self.archive_cache()
                cached_main = release / 'devcontainer_setup/__main__.py'
                original = cached_main.stat()
                cached_main.write_bytes(forged)
                os.utime(cached_main, ns=(original.st_atime_ns, original.st_mtime_ns))
                bytecode = Path(py_compile.compile(str(cached_main), doraise=True))
                forged_bytes = bytecode.read_bytes()
                cached_main.write_bytes(authentic)
                os.utime(cached_main, ns=(original.st_atime_ns, original.st_mtime_ns))
                script = (implementation / entry).read_text().replace('__SOURCE_REF__', sha)
                result = subprocess.run([shell, '-s', '--', argument], input=script, text=True,
                    capture_output=True, cwd=self.source,
                    env=dict(os.environ, XDG_CACHE_HOME=str(cache_base),
                             SETUP_DEVCONTAINER_REPO=str(self.source), SETUP_DEVCONTAINER_REF=sha))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'TRUSTED_SOURCE\n')
                self.assertEqual(bytecode.read_bytes(), forged_bytes)
                self.assertEqual(cached_main.read_bytes(), authentic)


    def test_bootstraps_support_prior_release_api_and_preserve_modified_archives(self):
        implementation = Path(__file__).resolve().parents[1]
        self.execution_source()
        # Expose the historical fetch_release/_validate public API without
        # depending on old Git objects being available in a shallow CI clone.
        old_api = (implementation / 'devcontainer_setup/releases.py').read_bytes().replace(
            b'publish_release', b'_private_cache_publication')
        self.assertNotIn(b'def publish_release', old_api)
        (self.source / 'devcontainer_setup/releases.py').write_bytes(old_api)
        sha = self.commit()
        for entry, shell, argument in (('install.sh', 'sh', '--scan'),
                ('templates/scripts/setup-wizard.sh', 'bash', '--check')):
            for modified in (False, True):
                with self.subTest(entry=entry, modified=modified):
                    cache_base = self.root / f'{shell}-{modified}'
                    self.cache = cache_base / 'devcontainer-setup/releases'
                    release, _ = self.archive_cache()
                    helper = release / 'devcontainer_setup/helper.py'
                    if modified:
                        helper.write_text('VALUE = 99\n')
                    original = helper.read_bytes()
                    script = (implementation / entry).read_text().replace('__SOURCE_REF__', sha)
                    result = subprocess.run([shell, '-s', '--', argument], input=script, text=True,
                        capture_output=True, cwd=self.source,
                        env=dict(os.environ, XDG_CACHE_HOME=str(cache_base),
                                 SETUP_DEVCONTAINER_REPO=str(self.source), SETUP_DEVCONTAINER_REF=sha))
                    if modified:
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn('modified tracked source', result.stderr)
                        self.assertNotIn('trusted-runner', result.stdout)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, 'trusted-runner\n')
                    self.assertEqual(helper.read_bytes(), original)
                    self.assertFalse((release / '.git').exists())


if __name__ == '__main__':
    unittest.main()
