"""Lifecycle and immutable recovery use only private fixture state."""
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch


class ScratchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'repo'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'remote', 'add', 'origin', 'git@github.com:owner/repo.git'], check=True)
        self.state = Path(self.temp.name).resolve() / 'state'
        self.scratch = self.root / '.scratch'
        self.scratch.mkdir()
        self.module = importlib.import_module('devcontainer_setup.project_sync.scratch')
        # A failing worker reads the Codespace state once; never real gh, the network or osascript.
        self.reads, self.replies, self.notes = [], [], []
        def upstream(url, token):
            self.reads.append(url)
            return self.replies.pop(0) if self.replies else (500, b'{}')
        for fake in (patch('devcontainer_setup.project_sync.transport._upstream', side_effect=upstream),
                     patch('devcontainer_setup.project_sync.transport.github_token', return_value='fixture-token'),
                     patch('devcontainer_setup.project_sync.notify.notify',
                           side_effect=lambda title, text: self.notes.append((title, text)) or True)):
            fake.start()
            self.addCleanup(fake.stop)

    def _cache_link_fixture(self, scratch):
        (scratch / 'memory').mkdir()
        (scratch / 'memory/keep.md').write_text('retained note')
        for directory, name in (('node_modules/.bin', 'tool'), ('.venv/bin', 'python')):
            cache = scratch / directory
            cache.mkdir(parents=True)
            (cache / name).symlink_to('/excluded-owner-target')
        (scratch / '.gitignore').write_text('*\n')
        (scratch / 'README.md').write_text('tracked marker')
        (scratch / 'memory/README.md').write_text('nested payload')

    def _native_remote(self):
        class LocalPython:
            def python(self, code, payload=b'', timeout=60):
                return subprocess.run([os.sys.executable, '-I', '-B', '-c', code], input=payload,
                                      capture_output=True, check=True, timeout=timeout).stdout
        return LocalPython()

    def test_cache_links_are_pruned_locally_and_backups_retain_markers(self):
        self._cache_link_fixture(self.scratch)
        self.assertEqual(self.module._scratch(self.root), self.scratch)
        with tarfile.open(self.module.backup(self.root, self.state)) as archive:
            names = set(archive.getnames())
            self.assertTrue({'.scratch/memory/keep.md', '.scratch/.gitignore',
                             '.scratch/README.md', '.scratch/memory/README.md'} <= names)
            self.assertFalse(any('node_modules' in name or '.venv' in name for name in names))

    def test_actual_canonical_remote_prunes_cache_links_and_snapshots_markers(self):
        self._cache_link_fixture(self.scratch)
        remote = self._native_remote()
        identity = json.loads(self.module._remote(remote, str(self.root)))
        self.assertEqual(identity['identity'], 'github.com/owner/repo')
        data = self.module._remote(remote, str(self.root), snapshot=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            self.assertEqual(archive.extractfile('.scratch/memory/keep.md').read(), b'retained note')
            self.assertEqual(archive.extractfile('.scratch/.gitignore').read(), b'*\n')
            self.assertEqual(archive.extractfile('.scratch/README.md').read(), b'tracked marker')
            self.assertFalse(any('node_modules' in name or '.venv' in name for name in archive.getnames()))

    def test_excluded_directory_links_are_not_followed_but_included_links_are_refused(self):
        (self.scratch / '.cache').symlink_to(self.root / 'missing-cache', target_is_directory=True)
        self.assertEqual(self.module._scratch(self.root), self.scratch)
        self.module._remote(self._native_remote(), str(self.root))
        (self.scratch / 'owner-link').symlink_to(self.root / 'missing-owner')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.module._scratch(self.root)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.module._remote(self._native_remote(), str(self.root))
        self.assertIn(b'symlink', caught.exception.stderr)

    def test_handoff_feedback_link_outside_scratch_does_not_trip_the_link_check(self):
        from devcontainer_setup.project_sync import scratch_tree
        target = self.scratch / 'handoff-feedback'
        target.mkdir()
        (target / 'gaps.jsonl').write_text('row\n')
        home = self.root.parent / 'claude-home' / '.claude'
        home.mkdir(parents=True)
        link = home / 'handoff-feedback'
        link.symlink_to(target)
        self.assertTrue(scratch_tree.eligible(Path('handoff-feedback/gaps.jsonl')))
        self.assertFalse(scratch_tree.eligible(Path('.handoff/gaps.jsonl')))
        self.assertEqual(self.module._scratch(self.root), self.scratch)
        names = [relative.as_posix() for _, relative in scratch_tree.entries(self.scratch)]
        self.assertIn('handoff-feedback/gaps.jsonl', names)
        identity = json.loads(self.module._remote(self._native_remote(), str(self.root)))
        self.assertEqual(identity['root'], str(self.root))
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(target))
        self.assertEqual((target / 'gaps.jsonl').read_text(), 'row\n')

    def test_included_special_files_are_refused_locally_and_remotely(self):
        os.mkfifo(self.scratch / 'owner-pipe')
        with self.assertRaisesRegex(ValueError, 'special file'):
            self.module._scratch(self.root)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.module._remote(self._native_remote(), str(self.root))
        self.assertIn(b'special file', caught.exception.stderr)

    def test_unreadable_included_directory_never_silently_disappears(self):
        included = self.scratch / 'memory'
        included.mkdir()
        scan = os.scandir
        def unreadable(path):
            if Path(path) == included:
                raise PermissionError('included directory unreadable')
            return scan(path)
        with patch('os.scandir', side_effect=unreadable):
            with self.assertRaises(PermissionError):
                self.module.backup(self.root, self.state)
        self.assertFalse((self.state / 'backups').exists())

    def test_pair_older_checkout_without_scratch_before_seeding_it(self):
        remote = self.root.parent / 'legacy-codespace'
        shutil.copytree(self.root, remote, ignore=shutil.ignore_patterns('.scratch'))
        class LocalPython:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def python(self, code, payload=b'', timeout=60):
                return subprocess.run(['python3', '-I', '-B', '-c', code], input=payload,
                                      capture_output=True, check=True, timeout=timeout).stdout
        before = subprocess.check_output(['git', '-C', str(remote), 'status', '--porcelain'])
        with patch.object(self.module, 'CodespaceConnection', LocalPython):
            binding = self.module.configure(self.root, self.state, 'bound-name', str(remote))
        self.assertEqual(binding['identity'], 'github.com/owner/repo')
        self.assertEqual(binding['remote_root'], str(remote))
        self.assertFalse((remote / '.scratch').exists(), 'Identity discovery must leave the legacy checkout unchanged')
        self.assertEqual(subprocess.check_output(['git', '-C', str(remote), 'status', '--porcelain']), before)
        with self.assertRaises(subprocess.CalledProcessError):
            self.module._remote(LocalPython(), str(remote))
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.module._remote(LocalPython(), str(remote), snapshot=True)
        self.assertIn(b'Remote .scratch missing', caught.exception.stderr)

    def test_remote_preparation_verifies_identity_root_and_refuses_unsafe_scratch(self):
        from devcontainer_setup.project_sync.state import discover_project
        identity = discover_project(self.root).identity
        remote = self.root.parent / 'prepare-remote'
        shutil.copytree(self.root, remote, ignore=shutil.ignore_patterns('.scratch'))
        connection = self._native_remote()
        subprocess.run(['git', '-C', str(remote), 'remote', 'set-url', 'origin', 'https://github.com/foreign/repo'], check=True)
        with self.assertRaises(subprocess.CalledProcessError):
            self.module._remote(connection, str(remote), prepare_identity=identity)
        self.assertFalse((remote / '.scratch').exists())
        subprocess.run(['git', '-C', str(remote), 'remote', 'set-url', 'origin', 'git@github.com:owner/repo.git'], check=True)
        child = remote / 'child'; child.mkdir()
        alias = self.root.parent / 'root-alias'; alias.symlink_to(remote, target_is_directory=True)
        for root in (child, alias):
            with self.subTest(root=root), self.assertRaises(subprocess.CalledProcessError):
                self.module._remote(connection, str(root), prepare_identity=identity)
            self.assertFalse((remote / '.scratch').exists())
        scratch = remote / '.scratch'
        scratch.write_text('owner file')
        with self.assertRaises(subprocess.CalledProcessError):
            self.module._remote(connection, str(remote), prepare_identity=identity)
        self.assertEqual(scratch.read_text(), 'owner file'); scratch.unlink()
        target = self.root.parent / 'owner-directory'; target.mkdir()
        scratch.symlink_to(target, target_is_directory=True)
        with self.assertRaises(subprocess.CalledProcessError):
            self.module._remote(connection, str(remote), prepare_identity=identity)
        self.assertEqual(list(target.iterdir()), []); self.assertTrue(scratch.is_symlink()); scratch.unlink()
        for _ in range(2):
            result = json.loads(self.module._remote(connection, str(remote), prepare_identity=identity))
            self.assertEqual(result, dict(identity=identity, root=str(remote)))
            self.assertEqual(list(scratch.iterdir()), [])
        (scratch / 'included-link').symlink_to(target)
        with self.assertRaises(subprocess.CalledProcessError):
            self.module._remote(connection, str(remote), prepare_identity=identity)
        self.assertTrue((scratch / 'included-link').is_symlink())

    @unittest.skipUnless(os.environ.get('CI') or os.environ.get('MUTAGEN_TEST_BINARY'), 'set MUTAGEN_TEST_BINARY for older-checkout connection gate')
    def test_actual_older_checkout_prepares_scratch_and_enters_herdr_despite_publish_refusal(self):
        import threading
        from types import SimpleNamespace
        from test_replication import ReplicationTests
        from devcontainer_setup.project_sync import workspace, replication
        from devcontainer_setup.project_sync.state import read_json
        case = ReplicationTests(); case.setUp()
        self.addCleanup(case.doCleanups)
        root = case.root
        remote = case.base / 'older-checkout'
        shutil.copytree(root, remote)
        self.assertFalse((remote / '.scratch').exists())
        (root / '.scratch').mkdir(); (root / '.scratch/note.md').write_text('scratch payload')
        case.conversation(case.local_env, text='LOCAL')
        remote_sid, _ = case.conversation(case.connection.env, text='REMOTE', root=remote)
        for checkout in (root, remote):
            (checkout / 'source').write_text('staged')
            subprocess.run(['git', '-C', str(checkout), 'add', 'source'], check=True)
            (checkout / 'source').write_text('unstaged')
        before = case.receipt(), case.checkout_receipt(remote)
        binary = Path(os.environ['MUTAGEN_TEST_BINARY']).resolve()
        temporary_root = '/private/tmp' if Path('/private/tmp').is_dir() else '/tmp'
        with tempfile.TemporaryDirectory(prefix='t3-old-', dir=temporary_root) as directory:
            state = Path(directory).resolve() / 's'
            m = self.module
            exiting = threading.Event()
            failures = []
            nonce = '9' * 48
            class Connection:
                def __init__(self, *args, **kwargs): self.child = SimpleNamespace(poll=lambda: None)
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def ssh_config(self, path): pass  # Native SSH is replaced by private local Mutagen endpoints.
                def python(self, code, payload=b'', timeout=60):
                    if code == workspace._HERDR_PROBE:
                        return b'herdr fixture\n'
                    if payload and json.loads(payload)['request']['operation'] == 'publish':
                        return b'{"ok":false,"error":"publication refused"}'
                    try:
                        return case.connection.python(code, payload, timeout)
                    except AssertionError:
                        raise ValueError('Remote Python command failed') from None
            actual_mutagen, actual_exchange, actual_ready = m._mutagen, replication.sync_project_data, workspace._ready
            mutagen_used = []
            def mutagen(tool, owner, *args, **kwargs):
                translated = tuple(str(remote / '.scratch') if arg == 'project-sync:' + str(remote / '.scratch') else arg for arg in args)
                result = actual_mutagen(tool, owner, *translated, **kwargs)
                if args[:1] == ('sync',):
                    mutagen_used.append(True)
                if args[:2] == ('sync', 'resume'):
                    actual_mutagen(tool, owner, 'sync', 'flush', args[2])
                return result
            def exchange(*args, **kwargs):
                self.assertIs(threading.current_thread(), worker, 'No foreground conversation exchange')
                return actual_exchange(*args, env=case.local_env, **kwargs)
            def supervise():
                try:
                    m.supervise(root, state, nonce)
                except BaseException as error:
                    failures.append(error)
            worker = threading.Thread(target=supervise)
            def start(*args):
                worker.start()
                deadline = time.monotonic() + 5
                while not (state / m.RUNTIME).exists() and worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(.02)
                return m.status(state)
            observations = []
            def open_herdr(project, owner, selected_root, **options):
                self.assertEqual(options, {'remote_herdr': 'herdr fixture'})
                self.assertEqual(selected_root, str(remote))
                self.assertEqual(m.inspect_sync(root, state)['phase'], 'synchronized')
                self.assertEqual((remote / '.scratch/note.md').read_text(), 'scratch payload')
                observations.append(True)
                return 0
            try:
                with patch.object(m, 'CodespaceConnection', Connection), \
                        patch('devcontainer_setup.project_sync.transport.CodespaceConnection', Connection), \
                        patch.object(m, 'install_mutagen', return_value=binary), \
                        patch.object(m, '_mutagen', side_effect=mutagen), \
                        patch('devcontainer_setup.project_sync.state.process_identity', side_effect=lambda pid: 'project_sync.scratch --supervise ' + str(state) + ' ' + nonce if worker.is_alive() else None), \
                        patch.object(m.signal, 'signal'), patch.object(m.threading, 'Event', return_value=exiting), \
                        patch.object(m, 'start', side_effect=start), \
                        patch.object(workspace, '_prerequisites'), \
                        patch.object(workspace, '_choose_pairing', return_value=('fixture', str(remote))), \
                        patch.object(workspace, '_ready', side_effect=lambda project, owner: actual_ready(project, owner, timeout=5)), \
                        patch('devcontainer_setup.project_sync.ssh_hosts.configure_ssh'), \
                        patch('devcontainer_setup.project_sync.ssh_hosts.refresh_ssh'), \
                        patch('devcontainer_setup.project_sync.herdr.open_remote', side_effect=open_herdr), \
                        patch('devcontainer_setup.project_sync.restore.end_proxies_elsewhere', return_value=[]), \
                        patch.object(replication, 'sync_project_data', side_effect=exchange):
                    # Discovery/configure and retarget remain read-only for the remote checkout.
                    m.configure(root, state, 'fixture', str(remote))
                    self.assertFalse((remote / '.scratch').exists())
                    m.retarget(root, state, 'fixture', str(remote))
                    self.assertFalse((remote / '.scratch').exists())
                    self.assertEqual(workspace.remote(case.project, state), 0)
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        runtime = read_json(state / m.RUNTIME)
                        if runtime.get('conversations', {}).get('completed_at') is not None:
                            break
                        time.sleep(.02)
                    history = runtime['conversations']
                    self.assertEqual(history['outcome'], 'degraded')
                    self.assertEqual(history['directions']['desktop_to_codespace']['stage'], 'publication')
                    self.assertEqual(history['directions']['desktop_to_codespace']['outcome'], 'failed')
                    inbound = history['directions']['codespace_to_desktop']
                    self.assertEqual(inbound['outcome'], 'complete')
                    self.assertIn(remote_sid, [row['source_session_id'] for row in inbound['publication']['imported']])
                    self.assertEqual(before, (case.receipt(), case.checkout_receipt(remote)))
                    self.assertEqual(observations, [True])
                    exiting.set(); worker.join(timeout=10)
                    self.assertFalse(worker.is_alive()); self.assertEqual(failures, [])
            finally:
                exiting.set()
                if worker.ident is not None:
                    worker.join(timeout=10)
                try:
                    self.assertFalse(worker.is_alive(), 'Fixture worker did not stop')
                    if (state / m.SESSION).exists():
                        actual_mutagen(binary, state, 'sync', 'terminate', read_json(state / m.SESSION)['name'])
                finally:
                    if mutagen_used:
                        actual_mutagen(binary, state, 'daemon', 'stop')

    def test_remote_identity_still_refuses_existing_unsafe_scratch(self):
        class LocalPython:
            def python(self, code, payload=b'', timeout=60):
                return subprocess.run(['python3', '-I', '-B', '-c', code], input=payload,
                                      capture_output=True, check=True, timeout=timeout).stdout
        self.scratch.rmdir()
        self.scratch.write_text('owner file')
        with self.assertRaises(subprocess.CalledProcessError):
            self.module._remote(LocalPython(), str(self.root))
        self.assertEqual(self.scratch.read_text(), 'owner file')
        self.scratch.unlink()
        self.scratch.symlink_to(self.root / 'missing-owner-directory')
        with self.assertRaises(subprocess.CalledProcessError):
            self.module._remote(LocalPython(), str(self.root))
        self.assertTrue(self.scratch.is_symlink())

    def test_backup_preserves_ignored_binary_and_is_immutable(self):
        (self.scratch / '.gitignore').write_text('*\n')
        payload = bytes(range(256)) * 100
        (self.scratch / 'ignored.bin').write_bytes(payload)
        first = self.module.backup(self.root, self.state)
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        (self.scratch / 'ignored.bin').write_bytes(b'changed')
        second = self.module.backup(self.root, self.state)
        self.assertNotEqual(first, second)
        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest)
        self.assertEqual(first.stat().st_mode & 0o777, 0o400)
        with tarfile.open(first) as archive:
            self.assertEqual(archive.extractfile('.scratch/ignored.bin').read(), payload)

    def test_retarget_replays_owned_pause_after_interrupted_archive(self):
        m = self.module
        project = m.discover_project(self.root)
        previous = dict(version=1, identity=project.identity, root=str(self.root),
                        codespace='fixture', remote_root='/workspaces/original')
        receipt = dict(name='scratch-' + 'a'*48, nonce='a'*48, binding=previous,
                       established=True, identifier='sync_owned')
        m._write(self.state / m.CONFIG, previous)
        m._write(self.state / m.SESSION, receipt)
        write = m._write
        failed = []
        def interrupted(path, value):
            if Path(path).name == m.SESSION and Path(path).parent != self.state and not failed:
                failed.append(True)
                raise OSError('interrupted after owned pause')
            return write(path, value)
        with patch.object(m, '_verify_destination', create=True), patch.object(m, 'stop'), patch.object(m, 'install_mutagen', return_value=Path('/fixture/mutagen')), \
                patch.object(m, '_session_info', return_value={'identifier':'sync_owned'}), \
                patch.object(m, '_mutagen') as command, patch.object(m, '_write', side_effect=interrupted):
            with self.assertRaises(OSError):
                m.retarget(self.root, self.state, 'fixture', '/workspaces/recovered')
            self.assertTrue((self.state / 'scratch-retarget.json').exists())
            m.complete_retarget(self.state)
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args.args[2:], ('sync', 'pause', 'sync_owned'))
        self.assertEqual(m.read_json(self.state / m.CONFIG)['remote_root'], '/workspaces/recovered')
        self.assertFalse((self.state / m.SESSION).exists())
        self.assertFalse((self.state / 'scratch-retarget.json').exists())

    def test_retarget_checks_new_remote_identity_before_stopping_or_archiving(self):
        m = self.module
        project = m.discover_project(self.root)
        previous = dict(version=1, identity=project.identity, root=str(self.root), codespace='space', remote_root='/workspaces/original')
        m._write(self.state / m.CONFIG, previous)
        before = (self.state / m.CONFIG).read_bytes()
        with patch.object(m, '_verify_destination', side_effect=ValueError('remote identity differs'), create=True), patch.object(m, 'stop') as stop:
            with self.assertRaisesRegex(ValueError, 'identity'):
                m.retarget(self.root, self.state, 'space', '/workspaces/new')
        stop.assert_not_called()
        self.assertEqual((self.state / m.CONFIG).read_bytes(), before)
        self.assertFalse((self.state / 'retired-bindings').exists())

    def test_pending_generated_retarget_is_reconciled_to_verified_ordinary_checkout(self):
        m = self.module; project = m.discover_project(self.root)
        previous = dict(version=1, identity=project.identity, root=str(self.root), codespace='space', remote_root='/workspaces/original')
        obsolete = dict(previous, remote_root='/workspaces/.devcontainer-work/project/snapshot')
        receipt = dict(name='scratch-'+'b'*48, nonce='b'*48, binding=previous, established=True, identifier='sync_kept')
        archive = self.state / 'retired-bindings/interrupted'
        m._write(self.state / m.CONFIG, previous); m._write(self.state / m.SESSION, receipt)
        m._write(self.state / 'scratch-retarget.json', dict(schema=1, previous=previous, candidate=obsolete, receipt=receipt, archive=str(archive)))
        with patch.object(m, '_verify_destination', create=True), patch.object(m, 'stop'), patch.object(m, 'install_mutagen', return_value=Path('/fixture/tool')), patch.object(m, '_session_info', return_value={'identifier':'sync_kept'}), patch.object(m, '_mutagen') as command:
            m.retarget(self.root, self.state, 'space', '/workspaces/ordinary')
        self.assertEqual(command.call_args.args[2:], ('sync', 'pause', 'sync_kept'))
        self.assertEqual(m.read_json(self.state / m.CONFIG)['remote_root'], '/workspaces/ordinary')
        self.assertEqual(m.read_json(archive / m.SESSION)['identifier'], 'sync_kept')
        self.assertEqual(m.read_json(archive / 'retarget-intent.json')['candidate'], obsolete)
        self.assertFalse((self.state / m.SESSION).exists())

    def test_pending_generated_retarget_retries_crash_after_final_config_write(self):
        m = self.module; project = m.discover_project(self.root)
        previous = dict(version=1, identity=project.identity, root=str(self.root), codespace='space', remote_root='/workspaces/original')
        obsolete = dict(previous, remote_root='/workspaces/.devcontainer-work/project/snapshot')
        receipt = dict(name='scratch-'+'b'*48, nonce='b'*48, binding=previous, established=True, identifier='sync_kept')
        archive = self.state / 'retired-bindings/interrupted'
        m._write(self.state / m.CONFIG, previous); m._write(self.state / m.SESSION, receipt)
        original = dict(schema=1, previous=previous, candidate=obsolete, receipt=receipt, archive=str(archive))
        m._write(self.state / 'scratch-retarget.json', original)
        unlink = Path.unlink; failed = []
        def interrupted(path, *args, **kwargs):
            if path == self.state / 'scratch-retarget.json' and not failed:
                failed.append(True); raise OSError('crash before final intent unlink')
            return unlink(path, *args, **kwargs)
        with patch.object(m, '_verify_destination', create=True), patch.object(m, 'stop'), patch.object(m, 'install_mutagen', return_value=Path('/fixture/tool')), patch.object(m, '_session_info', return_value={'identifier':'sync_kept'}), patch.object(m, '_mutagen'), patch.object(Path, 'unlink', interrupted):
            with self.assertRaises(OSError): m.retarget(self.root, self.state, 'space', '/workspaces/ordinary')
            m.retarget(self.root, self.state, 'space', '/workspaces/ordinary')
        self.assertEqual(m.read_json(archive / 'retarget-intent.json'), original)
        self.assertFalse((self.state / 'scratch-retarget.json').exists())
        self.assertEqual(m.read_json(self.state / m.CONFIG)['remote_root'], '/workspaces/ordinary')

    def test_pending_generated_live_config_retries_crash_after_candidate_journal_write(self):
        m = self.module; project = m.discover_project(self.root)
        previous = dict(version=1, identity=project.identity, root=str(self.root), codespace='space', remote_root='/workspaces/original')
        obsolete = dict(previous, remote_root='/workspaces/.devcontainer-work/project/snapshot')
        receipt = dict(name='scratch-'+'b'*48, nonce='b'*48, binding=previous, established=True, identifier='sync_kept')
        archive = self.state / 'retired-bindings/interrupted'
        original = dict(schema=1, previous=previous, candidate=obsolete, receipt=receipt, archive=str(archive))
        intent_path = self.state / 'scratch-retarget.json'
        m._write(self.state / m.CONFIG, obsolete); m._write(self.state / m.SESSION, receipt)
        m._write(intent_path, original)
        write = m._write; failed = []
        def interrupted(path, value):
            write(path, value)
            if Path(path) == intent_path and value['candidate']['remote_root'] == '/workspaces/ordinary' and not failed:
                failed.append(True); raise OSError('crash immediately after candidate journal write')
        with patch.object(m, '_verify_destination'), patch.object(m, 'stop'), patch.object(m, 'install_mutagen', return_value=Path('/fixture/tool')), patch.object(m, '_session_info', return_value={'identifier':'sync_kept'}), patch.object(m, '_mutagen') as command, patch.object(m, '_write', side_effect=interrupted):
            with self.assertRaises(OSError): m.retarget(self.root, self.state, 'space', '/workspaces/ordinary')
            self.assertEqual(m.read_json(self.state / m.CONFIG), obsolete)
            self.assertEqual(m.read_json(intent_path)['candidate']['remote_root'], '/workspaces/ordinary')
            self.assertEqual(m.read_json(intent_path)['observed_binding'], obsolete)
            command.assert_not_called()
            m._write(self.state / m.CONFIG, dict(previous, remote_root='/workspaces/unrecorded'))
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                m.retarget(self.root, self.state, 'space', '/workspaces/ordinary')
            command.assert_not_called()
            m._write(self.state / m.CONFIG, obsolete)
            m.retarget(self.root, self.state, 'space', '/workspaces/ordinary')
        self.assertEqual(m.read_json(archive / 'retarget-intent.json'), original)
        self.assertEqual(m.read_json(self.state / m.CONFIG)['remote_root'], '/workspaces/ordinary')
        self.assertFalse(intent_path.exists())
        self.assertEqual(command.call_args.args[2:], ('sync', 'pause', 'sync_kept'))

    def test_pending_start_error_names_existing_work_cs_task(self):
        self.module._write(self.state / 'scratch-retarget.json', {'pending': True})
        with self.assertRaisesRegex(ValueError, 'run task work:cs '):
            self.module.start(self.root, self.state)

    def test_same_binding_retarget_keeps_the_existing_forward_and_worker(self):
        m = self.module
        project = m.discover_project(self.root)
        candidate = dict(version=1, identity=project.identity, root=str(self.root),
                         codespace='fixture', remote_root='/workspaces/existing')
        m._write(self.state / m.CONFIG, candidate)
        before = (self.state / m.CONFIG).read_bytes()
        with patch.object(m, 'stop') as stop:
            self.assertEqual(m.retarget(self.root, self.state, 'fixture', '/workspaces/existing'), candidate)
        stop.assert_not_called()
        self.assertEqual((self.state / m.CONFIG).read_bytes(), before)

    def test_advanced_start_cannot_bypass_pending_retarget(self):
        self.module._write(self.state / 'scratch-retarget.json', {'pending': True})
        with patch.object(self.module, 'install_mutagen') as install:
            with self.assertRaisesRegex(ValueError, 'retarget.*pending'):
                self.module.start(self.root, self.state)
        install.assert_not_called()

    def test_symlink_scratch_and_payload_refused(self):
        (self.scratch / 'link').symlink_to(self.root / 'outside')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.module.backup(self.root, self.state)
        (self.scratch / 'link').unlink()
        self.scratch.rmdir()
        outside = self.root / 'outside'
        outside.mkdir()
        self.scratch.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.module._scratch(self.root)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.module.backup(self.root, self.state)
        self.assertTrue(self.scratch.is_symlink())

    def test_missing_configuration_and_unrelated_pid_do_not_signal(self):
        self.assertFalse(self.module.status(self.state)['running'])
        with self.assertRaisesRegex(ValueError, 'configur'):
            self.module.start(self.root, self.state)
        self.state.mkdir(exist_ok=True)
        (self.state / 'scratch-runtime.json').write_text(json.dumps({'pid': os.getpid(), 'nonce': 'foreign'}))
        with patch('os.kill') as kill:
            self.assertFalse(self.module.stop(self.state)['running'])
            kill.assert_not_called()

    def test_configure_validates_remote_before_binding_and_refuses_rebind(self):
        class Remote:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def python(self, code, payload=b'', timeout=60):
                self.code = code
                return b'{"identity":"github.com/other/repo","root":"/workspaces/repo"}'
        with patch.object(self.module, 'CodespaceConnection', Remote):
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.module.configure(self.root, self.state, 'bound-name', '/workspaces/repo')
        self.assertFalse((self.state / 'scratch.json').exists())

    def test_status_does_not_download_missing_mutagen_tool(self):
        tools = importlib.import_module('devcontainer_setup.project_sync.tools')
        receipt = dict(name='scratch-'+'a'*48, nonce='a'*48, binding={}, established=True)
        with patch.object(self.module, 'status', return_value=dict(running=True, phase='syncing')), patch.object(self.module, '_config', return_value={}), patch.object(self.module, '_saved_session', return_value=receipt), patch.object(tools, '_download', side_effect=AssertionError('status attempted network')) as download:
            with self.assertRaisesRegex(ValueError, 'installed|cached'):
                self.module.inspect_sync(self.root, self.state)
        download.assert_not_called()

    def test_checksum_failure_does_not_publish_tool(self):
        tools = importlib.import_module('devcontainer_setup.project_sync.tools')
        asset = {'url': 'https://example.invalid/tool', 'sha256': '0' * 64}
        with patch.object(tools, '_download', return_value=b'bad archive'):
            with self.assertRaisesRegex(ValueError, 'checksum'):
                tools.install_mutagen(self.state, asset=asset)
        self.assertFalse(list(self.state.rglob('mutagen')))

    def test_valid_tool_archive_published_once_and_tamper_refused(self):
        tools = importlib.import_module('devcontainer_setup.project_sync.tools')
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as archive:
            entry = tarfile.TarInfo('mutagen'); entry.size = 10; entry.mode = 0o755
            archive.addfile(entry, io.BytesIO(b'#!/bin/sh\n'))
        data = buf.getvalue()
        asset = {'url': 'https://example.invalid/tool', 'sha256': hashlib.sha256(data).hexdigest()}
        with patch.object(tools, '_download', return_value=data) as download:
            binary = tools.install_mutagen(self.state, asset=asset)
            self.assertTrue(os.access(binary, os.X_OK))
            self.assertEqual(tools.install_mutagen(self.state, asset=asset), binary)
            self.assertEqual(download.call_count, 1)
            binary.write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'modified'):
                tools.install_mutagen(self.state, asset=asset)

    def test_detached_start_survives_launcher_exit_idempotent_stop_restart(self):
        import time
        from devcontainer_setup.project_sync.state import discover_project, atomic_write
        from devcontainer_setup.project_sync import tools
        project = discover_project(self.root)
        config = {'version': 1, 'identity': project.identity, 'root': str(self.root),
                  'codespace': 'bound-name', 'remote_root': '/workspaces/repo'}
        atomic_write(self.state / 'scratch.json', json.dumps(config).encode())
        # A deterministic fake executable is confined to fixture state. It lets
        # the real detached controller wait for missing SSH prerequisites offline.
        import platform
        arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'amd64'}[platform.machine()]
        system = platform.system().lower()
        directory = self.state / 'tools' / ('mutagen-0.18.1-' + system + '-' + arch)
        binary = b'#!/bin/sh\nexit 0\n'
        pin = json.loads(tools.MANIFEST.read_text())['mutagen']['platforms'][system + '_' + arch]['sha256']
        atomic_write(directory / 'mutagen', binary, 0o700)
        atomic_write(directory / 'receipt.json', json.dumps({'archive_sha256': pin, 'files': {'mutagen': hashlib.sha256(binary).hexdigest()}}).encode())
        import shutil
        consumer = Path(self.temp.name) / 'consumer'
        package = consumer / 'project_sync'
        shutil.copytree(Path(self.module.__file__).parent, package,
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(tools.MANIFEST, package / 'sync-tools.json')
        poison = Path(self.temp.name) / 'ambient'
        poison.mkdir()
        marker = Path(self.temp.name) / 'poison-ran'
        payload = 'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()'
        (poison / 'sitecustomize.py').write_text(payload)
        (self.root / 'sitecustomize.py').write_text(payload)
        (self.root / 'json.py').write_text(payload + '; raise RuntimeError("checkout import poison")')
        code = ('import sys; sys.path.insert(0,' + repr(str(consumer)) + '); '
                'from pathlib import Path; from project_sync.scratch import start; import json; '
                'print(json.dumps(start(Path(' + repr(str(self.root)) + '),Path(' + repr(str(self.state)) + '))))')
        env = dict(os.environ)
        env['PYTHONPATH'] = str(poison)
        env['HOME'] = str(Path(self.temp.name).resolve())
        # The waiting worker reads the Codespace state once: fake gh has no token, so it is unknown.
        fakes, calls = Path(self.temp.name).resolve() / 'fake-bin', Path(self.temp.name).resolve() / 'fake-calls'
        for tool in ('gh', 'osascript'):
            atomic_write(fakes / tool, ('#!/bin/sh\necho ' + tool + ' "$@" >> ' + shlex.quote(str(calls))
                                        + '\nexit 1\n').encode(), 0o700)
        env.pop('GH_TOKEN', None)
        env['PATH'] = str(fakes) + os.pathsep + env['PATH']
        launcher = subprocess.run([os.sys.executable, '-I', '-B', '-c', code], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(launcher.returncode, 0, launcher.stderr)
        started = json.loads(launcher.stdout)
        try:
            self.assertFalse(marker.exists(), 'Supervisor executed ambient Python startup code')
            self.assertEqual(list(package.rglob('__pycache__')), [])
            self.assertTrue(self.module.status(self.state)['running'])
            # Identical bytes from a different runtime generate a different
            # proxy bootstrap. Move ownership to that origin once, then reuse it.
            with patch.dict(os.environ, env, clear=True):
                relocated = self.module.start(self.root, self.state)
                self.assertNotEqual(relocated['nonce'], started['nonce'])
                self.assertEqual(self.module.start(self.root, self.state)['pid'], relocated['pid'])
            copied_again = subprocess.run([os.sys.executable, '-I', '-B', '-c', code], env=env,
                                          capture_output=True, text=True, timeout=15)
            self.assertEqual(copied_again.returncode, 0, copied_again.stderr)
            baseline = json.loads(copied_again.stdout)
            self.assertNotEqual(baseline['nonce'], relocated['nonce'])
            # Updating installed helpers must replace an already-loaded producer,
            # otherwise its next reconnect can rewrite a newer SSH alias backwards.
            with (package / 'ssh_hosts.py').open('a') as changed:
                changed.write('\n# Simulated installed helper upgrade\n')
            upgraded = subprocess.run([os.sys.executable, '-I', '-B', '-c', code], env=env,
                                      capture_output=True, text=True, timeout=15)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            replacement = json.loads(upgraded.stdout)
            self.assertNotEqual(replacement['nonce'], baseline['nonce'])
            reused = subprocess.run([os.sys.executable, '-I', '-B', '-c', code], env=env,
                                   capture_output=True, text=True, timeout=15)
            self.assertEqual(reused.returncode, 0, reused.stderr)
            self.assertEqual(json.loads(reused.stdout)['pid'], replacement['pid'])
            self.assertFalse(self.module.stop(self.state)['running'])
            self.assertFalse(self.module.stop(self.state)['running'])
            # Restart from another shell creates a distinct nonce and process.
            again = subprocess.run([os.sys.executable, '-I', '-B', '-c', code], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertNotEqual(json.loads(again.stdout)['nonce'], replacement['nonce'])
            deadline = time.monotonic() + 5
            while self.module.status(self.state)['phase'] != 'waiting' and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIn('SSH key', self.module.status(self.state)['error'])
            self.assertIn('gh auth token', calls.read_text())
            self.assertNotIn('osascript', calls.read_text())
        finally:
            self.module.stop(self.state)
        self.assertLess((self.state / 'scratch.log').stat().st_size, 256 * 1024)
        lines = (self.state / 'scratch.log').read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            # UTC time and pid on every line, so work:logs can merge it with keeper.log.
            self.assertRegex(line, r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}Z \d+ \S')

    def test_case_collision_is_visible(self):
        (self.scratch / 'case').write_bytes(b'a')
        other = self.scratch / 'CASE'
        other.write_bytes(b'b')
        if len(list(self.scratch.iterdir())) != 2:
            self.skipTest('case-insensitive filesystem')
        with self.assertRaisesRegex(ValueError, 'case collision'):
            self.module.backup(self.root, self.state)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.module._remote(self._native_remote(), str(self.root))
        self.assertIn(b'case collision', caught.exception.stderr)

    def test_state_inside_scratch_is_refused_before_backup(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.module.backup(self.root, self.scratch / 'private-state')

    def test_stale_owned_session_recovered_but_foreign_session_never_terminated(self):
        from devcontainer_setup.project_sync.state import atomic_write
        self.state.mkdir()
        own = {'pid': 99999999, 'nonce': 'a' * 48, 'session': 'scratch-' + 'a' * 48,
               'phase': 'syncing', 'process_identity': 'gone'}
        atomic_write(self.state / 'scratch-runtime.json', json.dumps(own).encode())
        receipt = {'name': own['session'], 'nonce': own['nonce'], 'binding': {}, 'established': True, 'identifier': 'sync_owned'}
        atomic_write(self.state / 'scratch-session.json', json.dumps(receipt).encode())
        inventory = json.dumps([{'name': own['session'], 'identifier': 'sync_owned'}])
        with patch.object(self.module, 'install_mutagen', return_value=Path('/fake/mutagen')), patch.object(self.module, '_mutagen', return_value=inventory) as command:
            result = self.module.stop(self.state)
            self.assertFalse(result['running'])
            self.assertEqual(result['phase'], 'stopped')
            self.assertIn(('sync', 'pause', 'sync_owned'), [call.args[2:] for call in command.call_args_list])
            self.assertFalse(any(call.args[2:4] == ('sync', 'terminate') for call in command.call_args_list))
        own.update(session='unrelated-user-session', phase='syncing')
        atomic_write(self.state / 'scratch-runtime.json', json.dumps(own).encode())
        with patch.object(self.module, '_mutagen') as command:
            with self.assertRaisesRegex(ValueError, 'disagree'):
                self.module.stop(self.state)
            command.assert_not_called()

    def test_remote_identity_uses_canonical_discovery_and_snapshots_ignored_files(self):
        (self.scratch / 'remote.bin').write_bytes(b'\x00remote fixture')
        class LocalPython:
            def python(self, code, payload=b'', timeout=60):
                return subprocess.run([os.sys.executable, '-I', '-B', '-c', code], input=payload,
                                      capture_output=True, timeout=timeout, check=True).stdout
        identity = json.loads(self.module._remote(LocalPython(), str(self.root)))
        self.assertEqual(identity['identity'], 'github.com/owner/repo')
        data = self.module._remote(LocalPython(), str(self.root), snapshot=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            self.assertEqual(archive.extractfile('.scratch/remote.bin').read(), b'\x00remote fixture')

    def test_disconnection_pauses_only_owned_session_before_reconnect(self):
        from types import SimpleNamespace
        config = {'identity': 'github.com/owner/repo', 'codespace': 'bound-name', 'root': str(self.root),
                  'remote_root': '/workspaces/repo'}
        class Event:
            def __init__(self): self.waits = iter([False, True])
            def is_set(self): return getattr(self, 'stopped', False)
            def set(self): pass
            def wait(self, timeout): return next(self.waits)
        class Connection:
            def __init__(self, *args, **kwargs): self.child = SimpleNamespace(poll=lambda: 1)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def ssh_config(self, path): pass
        remote = json.dumps({'identity': config['identity'], 'root': config['remote_root']}).encode()
        created = []
        def invoke(binary, state, *args, **kwargs):
            if args[:2] == ('sync', 'create'): created.append(True)
            if args[:2] == ('sync', 'list'):
                return json.dumps([{'name': 'scratch-' + 'b' * 48, 'identifier': 'sync_owned'}]) if created else '[]'
            return ''
        with patch.object(self.module, '_config', return_value=config), patch.object(self.module, 'CodespaceConnection', Connection), patch.object(self.module, 'install_mutagen', return_value=Path('/fake/tool')), patch.object(self.module, '_remote', side_effect=[remote, b'archive']), patch.object(self.module, 'backup'), patch.object(self.module.threading, 'Event', Event), patch.object(self.module.signal, 'signal'), patch.object(self.module, '_mutagen', side_effect=invoke) as command:
            self.module.supervise(self.root, self.state, 'b' * 48)
        calls = [call.args[2:] for call in command.call_args_list]
        pause = ('sync', 'pause', 'sync_owned')
        self.assertIn(pause, calls)
        self.assertLess(calls.index(pause), calls.index(('daemon', 'stop')))
        self.assertFalse(any(call[:2] == ('sync', 'terminate') for call in calls))

    def test_worker_publishes_current_cycle_progress_and_keeps_receipts_separate(self):
        from types import SimpleNamespace
        from devcontainer_setup.project_sync import sync_cycle
        from devcontainer_setup.project_sync.state import discover_project
        config = dict(identity='github.com/owner/repo', codespace='bound-name', root=str(self.root),
                      remote_root='/workspaces/repo')
        class Event:
            def __init__(self): self.waits = iter([False, True])
            def is_set(self): return getattr(self, 'stopped', False)
            def set(self): self.stopped = True
            def wait(self, timeout):
                self.stopped = next(self.waits)
                return self.stopped
        class Connection:
            def __init__(self, *args, **kwargs): self.child = SimpleNamespace(poll=lambda: None)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def ssh_config(self, path): pass
        name = 'scratch-' + 'e' * 48
        receipt = dict(name=name, nonce='e' * 48, binding=config, established=True, identifier='sync_owned')
        self.module._write(self.state / self.module.SESSION, receipt)
        remote = json.dumps(dict(identity=config['identity'], root=config['remote_root'])).encode()
        seen = []
        saved_receipt = dict(snapshot_id='a' * 64, received_at='2026-10-18T00:00:00+00:00')
        self.module._write(self.state / self.module.RUNTIME,
                           dict(binding=config, phase='starting', retained_receipts={'desktop_to_codespace': saved_receipt}))
        def exchange(connection, project, state, remote_root, **kwargs):
            progress = kwargs.get('progress')
            if progress is None:
                return dict(schema=2, issues=[])  # old worker has no progress contract
            cycle = sync_cycle.begin(project, remote_root)
            progress(cycle)
            seen.append(json.loads((self.state / self.module.RUNTIME).read_text()))
            row = cycle['directions']['codespace_to_desktop']
            row.update(snapshot_id='a' * 64, receipt=saved_receipt, outcome='complete')
            cycle.update(outcome='degraded' if len(seen) == 1 else 'complete', completed_at=time.time())
            cycle['issues'] = [dict(reason='first cycle publication failed')] if len(seen) == 1 else []
            progress(cycle)
            return cycle
        def command(binary, state, *args, **kwargs):
            if args[:2] == ('sync', 'list'):
                return json.dumps([dict(name=name, identifier='sync_owned')])
            return ''
        with patch.object(self.module, '_config', return_value=config), \
                patch.object(self.module, 'CodespaceConnection', Connection), \
                patch.object(self.module, 'install_mutagen', return_value=Path('/fake/tool')), \
                patch.object(self.module, '_remote', return_value=remote), \
                patch.object(self.module.threading, 'Event', Event), patch.object(self.module.signal, 'signal'), \
                patch.object(self.module, '_mutagen', side_effect=command), \
                patch('devcontainer_setup.project_sync.replication.sync_project_data', side_effect=exchange):
            self.module.supervise(self.root, self.state, 'e' * 48)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]['retained_receipts']['desktop_to_codespace'], saved_receipt)
        self.assertNotEqual(seen[0]['conversations']['cycle_id'], seen[1]['conversations']['cycle_id'])
        self.assertTrue(all(item['conversations']['outcome'] == 'pending' for item in seen))
        self.assertTrue(all(item['conversations']['completed_at'] is None for item in seen))
        self.assertTrue(all(item['heartbeat_at'] > 0 and item['checkpoint_version'] == 3 for item in seen))
        self.assertTrue(all(item['prepared_binding'] == config and item['connection_prepared'] for item in seen))
        self.assertEqual(seen[1]['retained_receipts']['codespace_to_desktop'], saved_receipt)
        self.assertIsNone(seen[1]['conversations']['directions']['codespace_to_desktop']['receipt'])
        runtime = json.loads((self.state / self.module.RUNTIME).read_text())
        self.assertIsNone(runtime['conversation_error'])
        self.assertFalse(runtime['connection_prepared'])

    def test_stop_interrupts_owned_forward_startup(self):
        import time
        from devcontainer_setup.project_sync.state import discover_project, atomic_write
        from devcontainer_setup.project_sync import tools
        import platform
        project = discover_project(self.root)
        atomic_write(self.state / 'scratch.json', json.dumps({'version': 1, 'identity': project.identity,
            'root': str(self.root), 'codespace': 'bound-name', 'remote_root': '/workspaces/repo'}).encode())
        arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'amd64'}[platform.machine()]
        system = platform.system().lower()
        directory = self.state / 'tools' / ('mutagen-0.18.1-' + system + '-' + arch)
        binary = b'#!/bin/sh\nexit 0\n'
        pin = json.loads(tools.MANIFEST.read_text())['mutagen']['platforms'][system + '_' + arch]['sha256']
        atomic_write(directory / 'mutagen', binary, 0o700)
        atomic_write(directory / 'receipt.json', json.dumps({'archive_sha256': pin, 'files': {'mutagen': hashlib.sha256(binary).hexdigest()}}).encode())
        home = Path(self.temp.name).resolve()
        (home / '.ssh').mkdir()
        (home / '.ssh/codespaces.auto').write_bytes(b'fixture-key')
        bindir = home / 'bin'; bindir.mkdir()
        gh = '#!' + os.sys.executable + '\nimport sys,time,pathlib\nif sys.argv[1]=="--version":print("gh version 2.102.0 (fixture)")\nelse:\n pathlib.Path(' + repr(str(home / 'forward-entered')) + ').write_text("entered")\n time.sleep(60)\n'
        atomic_write(bindir / 'gh', gh.encode(), 0o700)
        env = dict(os.environ, HOME=str(home), GH_TOKEN='fixture-adapter-upstream-token', PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        # A worker that fails reads the Codespace state; a dead local proxy keeps that read off the network.
        dead = 'http://127.0.0.1:9'
        for name in ('no_proxy', 'NO_PROXY'):
            env.pop(name, None)
        env.update(https_proxy=dead, HTTPS_PROXY=dead)
        routed = subprocess.run([os.sys.executable, '-I', '-c', 'import urllib.request as u; '
                                 'print(u.getproxies().get("https"), u.proxy_bypass("api.github.com"))'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(routed.stdout.split(), [dead, 'False'])
        try:
            with patch.dict(os.environ, env, clear=True):
                started = self.module.start(self.root, self.state)
            deadline = time.monotonic() + 5
            while not (home / 'forward-entered').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((home / 'forward-entered').exists())
            began = time.monotonic()
            self.assertFalse(self.module.stop(self.state)['running'])
            self.assertLess(time.monotonic() - began, 8)
        finally:
            # Fixture cleanup only: terminate exact controller PID after a failed gate.
            try:
                if self.module.status(self.state)['running']:
                    os.kill(started['pid'], 9)
            except ProcessLookupError:
                pass

    def test_mutagen_socket_limit_fails_clearly_and_version_state_is_isolated(self):
        from devcontainer_setup.project_sync.tools import mutagen_env
        with self.assertRaisesRegex(ValueError, 'socket path'):
            mutagen_env(Path('/private/' + 'x' * 120))
        env = mutagen_env(Path('/private/tmp/fixture-state'))
        self.assertEqual(env['MUTAGEN_DATA_DIRECTORY'], '/private/tmp/fixture-state/m181')

    def test_tool_download_retries_transient_reset_and_reports_failure(self):
        from devcontainer_setup.project_sync import tools
        with patch.object(tools, 'urlopen', side_effect=[ConnectionResetError('reset'), io.BytesIO(b'archive')]) as request:
            self.assertEqual(tools._download('https://example.invalid/archive'), b'archive')
            self.assertEqual(request.call_count, 2)
        with patch.object(tools, 'urlopen', side_effect=ConnectionResetError('reset')), \
                patch('subprocess.run', return_value=subprocess.CompletedProcess(['curl'], 22, b'', b'private detail')):
            with self.assertRaisesRegex(ValueError, 'download') as caught:
                tools._download('https://example.invalid/archive')
            self.assertNotIn('private', str(caught.exception))

    def test_tool_download_uses_curl_when_python_body_keeps_resetting(self):
        from devcontainer_setup.project_sync import tools
        def curl(args, **kwargs):
            self.assertEqual(args[0], 'curl')
            Path(args[args.index('--output') + 1]).write_bytes(b'complete archive')
            return subprocess.CompletedProcess(args, 0, b'', b'')
        with patch.object(tools, 'urlopen', side_effect=ConnectionResetError('reset')), \
                patch('subprocess.run', side_effect=curl) as request:
            self.assertEqual(tools._download('https://example.invalid/archive'), b'complete archive')
        self.assertEqual(request.call_count, 1)

    def test_failed_create_without_session_is_retryable_and_never_cleanup_required(self):
        from types import SimpleNamespace
        config = {'identity': 'github.com/owner/repo', 'codespace': 'bound-name', 'root': str(self.root),
                  'remote_root': '/workspaces/repo'}
        class Event:
            def __init__(self): self.waits = iter([False, True])
            def is_set(self): return getattr(self, 'stopped', False)
            def set(self): pass
            def wait(self, timeout): return next(self.waits)
        class Connection:
            def __init__(self, *args, **kwargs): self.child = SimpleNamespace(poll=lambda: 1)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def ssh_config(self, path): pass
        remote = json.dumps({'identity': config['identity'], 'root': config['remote_root']}).encode()
        def command(binary, state, *args, **kwargs):
            if args[:2] == ('sync', 'create'): raise ValueError('fixture create failed before session existed')
            if args[:2] == ('sync', 'list'): return '[]'
            if args[:2] == ('sync', 'pause') or args[:2] == ('sync', 'terminate'):
                raise ValueError('missing session')
            return ''
        with patch.object(self.module, '_config', return_value=config), patch.object(self.module, 'CodespaceConnection', Connection), patch.object(self.module, 'install_mutagen', return_value=Path('/fake/tool')), patch.object(self.module, '_remote', side_effect=[remote, b'archive', remote, b'archive']), patch.object(self.module, 'backup'), patch.object(self.module.threading, 'Event', Event), patch.object(self.module.signal, 'signal'), patch.object(self.module, '_mutagen', side_effect=command) as commands:
            self.module.supervise(self.root, self.state, 'c' * 48)
        runtime = json.loads((self.state / 'scratch-runtime.json').read_text())
        self.assertEqual(runtime['phase'], 'stopped')
        self.assertEqual(sum(call.args[2:4] == ('sync', 'create') for call in commands.call_args_list), 2)
        self.assertFalse(any(call.args[2:4] == ('sync', 'terminate') for call in commands.call_args_list))

    def test_normal_stop_preserves_owned_session_baseline_and_receipt(self):
        from types import SimpleNamespace
        config = {'identity': 'github.com/owner/repo', 'codespace': 'bound-name', 'root': str(self.root),
                  'remote_root': '/workspaces/repo'}
        class Event:
            def is_set(self): return getattr(self, 'stopped', False)
            def set(self): pass
            def wait(self, timeout):
                self.stopped = True
                return True
        class Connection:
            def __init__(self, *args, **kwargs): self.child = SimpleNamespace(poll=lambda: None)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def ssh_config(self, path): pass
        remote = json.dumps({'identity': config['identity'], 'root': config['remote_root']}).encode()
        created = []
        def command(binary, state, *args, **kwargs):
            if args[:2] == ('sync', 'create'): created.append(True)
            if args[:2] == ('sync', 'list'):
                return json.dumps([{'name': 'scratch-' + 'd' * 48, 'identifier': 'sync_' + 'D' * 43}]) if created else '[]'
            return ''
        with patch.object(self.module, '_config', return_value=config), patch.object(self.module, 'CodespaceConnection', Connection), patch.object(self.module, 'install_mutagen', return_value=Path('/fake/tool')), patch.object(self.module, '_remote', side_effect=[remote, b'archive']), patch.object(self.module, 'backup'), patch.object(self.module.threading, 'Event', Event), patch.object(self.module.signal, 'signal'), patch.object(self.module, '_mutagen', side_effect=command) as commands:
            self.module.supervise(self.root, self.state, 'd' * 48)
        self.assertFalse(any(call.args[2:4] == ('sync', 'terminate') for call in commands.call_args_list))
        self.assertTrue((self.state / 'scratch-session.json').is_file())
        saved = json.loads((self.state / 'scratch-session.json').read_text())
        self.assertEqual(saved['identifier'], 'sync_' + 'D' * 43)
        self.assertTrue(saved['established'])

    @unittest.skipUnless(os.environ.get('CI') or os.environ.get('MUTAGEN_TEST_BINARY'), 'set MUTAGEN_TEST_BINARY for real cache-link boundary gate')
    def test_actual_mutagen_cache_links_allow_note_lifecycle_and_same_baseline_restart(self):
        binary = Path(os.environ['MUTAGEN_TEST_BINARY']).resolve()
        temporary_root = '/private/tmp' if Path('/private/tmp').is_dir() else '/tmp'
        with tempfile.TemporaryDirectory(prefix='t2-cache-', dir=temporary_root) as directory:
            base = Path(directory).resolve(); state = base / 's'
            alpha, beta = base / 'alpha/.scratch', base / 'beta/.scratch'
            for endpoint in (alpha, beta):
                endpoint.mkdir(parents=True)
                self._cache_link_fixture(endpoint)
                self.module._scratch(endpoint.parent)
            name, nonce = 'scratch-' + 'f' * 48, 'f' * 48
            created = False
            try:
                args = ['sync', 'create', str(alpha), str(beta), '--name=' + name,
                        '--mode=two-way-safe', '--hash=sha256', '--symlink-mode=ignore',
                        '--no-global-configuration']
                args += ['--ignore=' + item for item in self.module.scratch_tree.MUTAGEN_IGNORES]
                self.module._mutagen(binary, state, *args, timeout=120)
                created = True
                def flush():
                    self.module._mutagen(binary, state, 'sync', 'flush', name)
                flush()
                (alpha / 'memory/new.md').write_text('created')
                flush()
                self.assertEqual((beta / 'memory/new.md').read_text(), 'created')
                (beta / 'memory/new.md').write_text('changed remotely')
                flush()
                self.assertEqual((alpha / 'memory/new.md').read_text(), 'changed remotely')
                info = json.loads(self.module._mutagen(binary, state, 'sync', 'list', '--template={{json .}}'))[0]
                self.assertEqual(self.module._reconciliation(info)['phase'], 'synchronized')
                receipt = dict(name=name, nonce=nonce, identifier=info['identifier'], established=True, binding={})
                runtime = dict(pid=99999999, nonce=nonce, session=name, phase='syncing')
                self.module._write(state / self.module.SESSION, receipt)
                self.module._write(state / self.module.RUNTIME, runtime)
                with patch.object(self.module, 'install_mutagen', return_value=binary):
                    self.module._recover_session(state, runtime)
                (alpha / 'memory/new.md').unlink()
                after = json.loads(self.module._mutagen(binary, state, 'sync', 'list', '--template={{json .}}'))
                self.assertEqual([row['identifier'] for row in after], [info['identifier']])
                self.module._mutagen(binary, state, 'sync', 'resume', info['identifier'])
                flush()
                self.assertFalse((beta / 'memory/new.md').exists())
                self.assertFalse((alpha / 'memory/new.md').exists())
                self.assertEqual((beta / 'memory/keep.md').read_text(), 'retained note')
                for endpoint in (alpha, beta):
                    self.assertTrue((endpoint / 'node_modules/.bin/tool').is_symlink())
                    self.assertTrue((endpoint / '.venv/bin/python').is_symlink())
                    link = endpoint / 'memory/included-link'
                    link.symlink_to('/included-owner-target')
                    with self.assertRaisesRegex(ValueError, 'symlink'):
                        self.module._scratch(endpoint.parent)
                    link.unlink()
            finally:
                try:
                    if created:
                        self.module._mutagen(binary, state, 'sync', 'terminate', name)
                finally:
                    self.module._mutagen(binary, state, 'daemon', 'stop')

    @unittest.skipUnless(os.environ.get('CI') or os.environ.get('MUTAGEN_TEST_BINARY'), 'set MUTAGEN_TEST_BINARY to the pinned official binary for real restart gate')
    def test_actual_mutagen_offline_deletion_survives_controller_stop_restart(self):
        from devcontainer_setup.project_sync.state import atomic_write
        binary = Path(os.environ['MUTAGEN_TEST_BINARY']).resolve()
        temporary_root = '/private/tmp' if Path('/private/tmp').is_dir() else '/tmp'
        with tempfile.TemporaryDirectory(prefix='t2-restart-', dir=temporary_root) as temporary:
            state = Path(temporary).resolve()
            alpha, beta = state / 'alpha/.scratch', state / 'beta/.scratch'
            alpha.mkdir(parents=True); beta.mkdir(parents=True)
            (alpha / 'delete-me').write_bytes(b'baseline content')
            nonce = 'e' * 48
            name = 'scratch-' + nonce
            runtime = {'pid': 99999999, 'nonce': nonce, 'session': name, 'phase': 'syncing'}
            created = False
            try:
                self.module._mutagen(binary, state, 'sync', 'create', str(alpha), str(beta), '--name=' + name,
                                     '--mode=two-way-safe', '--hash=sha256', '--no-global-configuration')
                created = True
                self.module._mutagen(binary, state, 'sync', 'flush', name)
                info = json.loads(self.module._mutagen(binary, state, 'sync', 'list', '--template={{json .}}'))[0]
                receipt = {'name': name, 'nonce': nonce, 'identifier': info['identifier'], 'established': True,
                           'binding': {'root': str(alpha.parent), 'remote_root': str(beta.parent),
                                       'identity': 'github.com/fixture/restart', 'codespace': 'fixture-name'}}
                atomic_write(state / 'scratch-session.json', json.dumps(receipt).encode())
                atomic_write(state / 'scratch-runtime.json', json.dumps(runtime).encode())
                with patch.object(self.module, 'install_mutagen', return_value=binary):
                    self.module._recover_session(state, runtime)
                (alpha / 'delete-me').unlink()
                # Listing restarts the same isolated daemon from durable data.
                after = json.loads(self.module._mutagen(binary, state, 'sync', 'list', '--template={{json .}}'))
                self.assertEqual([entry['identifier'] for entry in after], [info['identifier']])
                self.module._mutagen(binary, state, 'sync', 'resume', info['identifier'])
                self.module._mutagen(binary, state, 'sync', 'flush', info['identifier'])
                self.assertFalse((alpha / 'delete-me').exists(), 'offline deletion was resurrected on the source')
                self.assertFalse((beta / 'delete-me').exists(), 'offline deletion failed to propagate')
            finally:
                if created:
                    current = json.loads(self.module._mutagen(binary, state, 'sync', 'list', '--template={{json .}}'))
                    if any(entry['name'] == name for entry in current):
                        self.module._mutagen(binary, state, 'sync', 'terminate', name)
                self.module._mutagen(binary, state, 'daemon', 'stop')

    @unittest.skipUnless(os.environ.get('CI') or os.environ.get('MUTAGEN_TEST_BINARY'), 'set MUTAGEN_TEST_BINARY for real retained-baseline migration gate')
    def test_actual_retarget_retains_paused_old_baseline_and_new_session_has_no_old_deletions(self):
        m = self.module; binary = Path(os.environ['MUTAGEN_TEST_BINARY']).resolve()
        temporary_root = '/private/tmp' if Path('/private/tmp').is_dir() else '/tmp'
        with tempfile.TemporaryDirectory(prefix='t2-migrate-', dir=temporary_root) as directory:
            base = Path(directory).resolve(); state = base / 's'
            roots = [base / name for name in ('local', 'old', 'normal')]
            for root in roots:
                root.mkdir(); (root / '.scratch').mkdir()
                subprocess.run(['git', 'init', '-q', str(root)], check=True)
                subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'https://github.com/fixture/migrate'], check=True)
            local, old, normal = roots
            alpha, beta, new_beta = [root / '.scratch' for root in roots]
            (alpha / 'same-name').write_text('old baseline')
            nonce = 'c'*48; old_name = 'scratch-'+nonce; new_name = 'scratch-'+'d'*48
            project = m.discover_project(local)
            previous = dict(version=1, identity=project.identity, root=str(local), codespace='fixture', remote_root=str(old))
            created = []
            try:
                m._mutagen(binary, state, 'sync', 'create', str(alpha), str(beta), '--name='+old_name, '--mode=two-way-safe', '--hash=sha256', '--no-global-configuration')
                created.append(old_name); m._mutagen(binary, state, 'sync', 'flush', old_name)
                info = json.loads(m._mutagen(binary, state, 'sync', 'list', '--template={{json .}}'))[0]
                m._write(state / m.CONFIG, previous)
                m._write(state / m.SESSION, dict(name=old_name, nonce=nonce, identifier=info['identifier'], established=True, binding=previous))
                def verify(project, state, codespace, root):
                    self.assertEqual(m.discover_project(Path(root)).identity, project.identity)
                with patch.object(m, '_verify_destination', side_effect=verify), patch.object(m, 'install_mutagen', return_value=binary):
                    m.retarget(local, state, 'fixture', str(normal))
                retained = json.loads(m._mutagen(binary, state, 'sync', 'list', '--template={{json .}}'))
                self.assertEqual(len(retained), 1)
                self.assertEqual(retained[0]['identifier'], info['identifier'])
                self.assertTrue(retained[0]['paused'])
                archived = list((state / 'retired-bindings').glob('*/scratch-session.json'))
                self.assertEqual(len(archived), 1)
                self.assertEqual(m.read_json(archived[0])['identifier'], info['identifier'])
                (alpha / 'same-name').unlink()
                (new_beta / 'same-name').write_text('new endpoint owner content')
                m._mutagen(binary, state, 'sync', 'create', str(alpha), str(new_beta), '--name='+new_name, '--mode=two-way-safe', '--hash=sha256', '--no-global-configuration')
                created.append(new_name); m._mutagen(binary, state, 'sync', 'flush', new_name)
                sessions = json.loads(m._mutagen(binary, state, 'sync', 'list', '--template={{json .}}'))
                self.assertEqual(len({row['identifier'] for row in sessions}), 2)
                self.assertEqual((new_beta / 'same-name').read_text(), 'new endpoint owner content')
                self.assertEqual((alpha / 'same-name').read_text(), 'new endpoint owner content')
                self.assertEqual((beta / 'same-name').read_text(), 'old baseline')
                self.assertTrue(next(row for row in sessions if row['name']==old_name)['paused'])
            finally:
                # Only disposable fixture sessions are removed by test cleanup.
                for name in created:
                    m._mutagen(binary, state, 'sync', 'terminate', name)
                m._mutagen(binary, state, 'daemon', 'stop')

    def test_supervisor_reuses_exact_saved_baseline_after_stop_or_create_interruption(self):
        from types import SimpleNamespace
        from devcontainer_setup.project_sync.state import atomic_write
        config = {'identity': 'github.com/owner/repo', 'codespace': 'bound-name', 'root': str(self.root),
                  'remote_root': '/workspaces/repo'}
        class Event:
            def is_set(self): return getattr(self, 'stopped', False)
            def set(self): pass
            def wait(self, timeout):
                self.stopped = True
                return True
        class Connection:
            def __init__(self, *args, **kwargs): self.child = SimpleNamespace(poll=lambda: None)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def ssh_config(self, path): pass
        remote = json.dumps({'identity': config['identity'], 'root': config['remote_root']}).encode()
        name, identifier = 'scratch-' + 'a' * 48, 'sync_preserved'
        for established in (True, False):
            with self.subTest(established=established):
                receipt = {'name': name, 'nonce': 'a' * 48, 'binding': config, 'established': established}
                if established: receipt['identifier'] = identifier
                atomic_write(self.state / 'scratch-session.json', json.dumps(receipt).encode())
                def command(binary, state, *args, **kwargs):
                    if args[:2] == ('sync', 'create'): self.fail('existing baseline must never be recreated')
                    if args[:2] == ('sync', 'list'):
                        return json.dumps([{'name': name, 'identifier': identifier}])
                    return ''
                with patch.object(self.module, '_config', return_value=config), patch.object(self.module, 'CodespaceConnection', Connection), patch.object(self.module, 'install_mutagen', return_value=Path('/fake/tool')), patch.object(self.module, '_remote', return_value=remote), patch.object(self.module, 'backup') as backup, patch.object(self.module.threading, 'Event', Event), patch.object(self.module.signal, 'signal'), patch.object(self.module, '_mutagen', side_effect=command) as commands:
                    self.module.supervise(self.root, self.state, 'f' * 48)
                self.assertIn(('sync', 'resume', identifier), [call.args[2:] for call in commands.call_args_list])
                backup.assert_not_called()
                saved = json.loads((self.state / 'scratch-session.json').read_text())
                self.assertEqual(saved['identifier'], identifier)
                runtime = json.loads((self.state / 'scratch-runtime.json').read_text())
                self.assertEqual(runtime['session'], name)
                self.assertEqual(runtime['nonce'], 'f' * 48)

    def test_supervisor_never_recreates_missing_remote_scratch_for_any_saved_receipt(self):
        m = self.module
        (self.scratch / 'note.md').write_text('owner note')
        class Event:
            def is_set(self): return getattr(self, 'stopped', False)
            def wait(self, timeout):
                self.stopped = True
                return True
        native = self._native_remote()
        class Connection:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def python(self, *args, **kwargs): return native.python(*args, **kwargs)
            def ssh_config(self, path): pass
        for established in (True, False):
            with self.subTest(established=established):
                remote = self.root.parent / ('missing-scratch-' + str(established))
                shutil.copytree(self.root, remote, ignore=shutil.ignore_patterns('.scratch'))
                config = dict(identity='github.com/owner/repo', codespace='fixture',
                              root=str(self.root), remote_root=str(remote))
                receipt = dict(name='scratch-' + 'a' * 48, nonce='a' * 48,
                               binding=config, established=established)
                if established:
                    receipt['identifier'] = 'sync_preserved'
                m._write(self.state / m.SESSION, receipt)
                before = (self.state / m.SESSION).read_bytes()
                def command(binary, state, *args, **kwargs):
                    if args[:2] == ('sync', 'list'):
                        return json.dumps([dict(name=receipt['name'], identifier='sync_preserved')])
                    return ''
                with patch.object(m, '_config', return_value=config), \
                        patch.object(m, 'CodespaceConnection', Connection), \
                        patch.object(m, 'install_mutagen', return_value=Path('/fake/tool')), \
                        patch.object(m.threading, 'Event', Event), patch.object(m.signal, 'signal'), \
                        patch('devcontainer_setup.project_sync.ssh_hosts.refresh_ssh'), \
                        patch('devcontainer_setup.project_sync.replication.sync_project_data', side_effect=ValueError('fixture refusal')), \
                        patch.object(m, 'backup') as backup, patch.object(m, '_mutagen', side_effect=command) as commands:
                    m.supervise(self.root, self.state, 'f' * 48)
                    # Concurrent offline status inspection cannot promote a
                    # planned receipt before endpoint validation either.
                    with patch.object(m, 'status', return_value=dict(running=True, phase='waiting')):
                        m.inspect_sync(self.root, self.state)
                self.assertFalse((remote / '.scratch').exists(), 'A saved baseline must never seed an empty endpoint')
                self.assertEqual((self.state / m.SESSION).read_bytes(), before)
                self.assertEqual((self.scratch / 'note.md').read_text(), 'owner note')
                operations = [call.args[2:4] for call in commands.call_args_list]
                self.assertNotIn(('sync', 'resume'), operations)
                self.assertNotIn(('sync', 'create'), operations)
                self.assertIn(('sync', 'pause'), operations)
                backup.assert_not_called()

    FORWARD_DOWN = ValueError('Codespace forward unavailable; it may be stopped or authentication expired. '
                              'No start request was allowed.')
    STOPPED = 'Codespace is Shutdown; no start request was allowed.'
    STOP_NOTE = ('Codespace recovery', "repo's Codespace stopped (Shutdown); run task work:cs to bring it back")
    KEEPER_NOTE = ('Codespace recovery', 'Recovery for repo is not running; run task work:cs:armed')

    def _stopped_cycle(self):
        from devcontainer_setup.project_sync.replication import ConnectionVerificationError
        return ConnectionVerificationError(self.STOPPED, 'Shutdown')

    def _arm(self, armed=True):
        from devcontainer_setup.project_sync import arming
        arming.write(self.state, {'schema': 1, 'armed': armed, 'codespace': 'bound-name', 'gh_user': 'fixture'})

    def _work(self, attempts, cycles=()):
        """Run the worker in this process against scripted connections and sync cycles.

        Each attempt is 'ok' or the error opening the connection raises; each cycle is 'ok',
        the error that sync cycle raises, or a callable run in its place. The worker stops once
        both lists are used up. It returns the last runtime and the worker log; self.waits
        holds each wait's timeout.
        """
        from types import SimpleNamespace
        from devcontainer_setup.project_sync import sync_cycle
        m, attempts, cycles, self.waits = self.module, list(attempts), list(cycles), []
        config = dict(identity='github.com/owner/repo', codespace='bound-name', root=str(self.root),
                      remote_root='/workspaces/repo')
        name = 'scratch-' + 'e' * 48
        m._write(self.state / m.SESSION, dict(name=name, nonce='e' * 48, binding=config, established=True,
                                              identifier='sync_owned'))
        class Event:
            def is_set(self): return getattr(self, 'stopped', False)
            def set(self): self.stopped = True
            def wait(inner, timeout):
                self.waits.append(timeout)
                inner.stopped = not (attempts or cycles)
                return inner.stopped
        class Connection:
            codespace, env = config['codespace'], {}
            def __init__(self, *args, **kwargs): self.child = SimpleNamespace(poll=lambda: None)
            def __enter__(self):
                outcome = attempts.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return self
            def __exit__(self, *args): pass
            def ssh_config(self, path): pass
        def exchange(connection, project, state, remote_root, **kwargs):
            outcome = cycles.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if callable(outcome):
                return outcome(connection, project, state, remote_root, **kwargs)
            cycle = sync_cycle.begin(project, remote_root)
            cycle.update(outcome='complete', stage='complete', completed_at=time.time())
            return cycle
        def command(binary, state, *args, **kwargs):
            return json.dumps([dict(name=name, identifier='sync_owned')]) if args[:2] == ('sync', 'list') else ''
        remote = json.dumps(dict(identity=config['identity'], root=config['remote_root'])).encode()
        with patch.object(m, '_config', return_value=config), patch.object(m, 'CodespaceConnection', Connection), \
                patch.object(m, 'install_mutagen', return_value=Path('/fake/tool')), \
                patch.object(m, '_remote', return_value=remote), patch.object(m.threading, 'Event', Event), \
                patch.object(m.signal, 'signal'), patch.object(m, '_mutagen', side_effect=command), \
                patch('devcontainer_setup.project_sync.replication.sync_project_data', side_effect=exchange):
            m.supervise(self.root, self.state, 'e' * 48)
        return json.loads((self.state / m.RUNTIME).read_text()), (self.state / 'scratch.log').read_text()

    def test_the_worker_records_stopped_at_when_it_learns_a_down_state(self):
        # #61: an unarmed project's evidence expires at the next seen stop, stamped with that read's time.
        from devcontainer_setup.project_sync import evidence
        from devcontainer_setup.project_sync.replication import ConnectionVerificationError
        started = 1_700_000_000.0
        evidence.record_start(self.state, 'bound-name', started, 'started')
        notices = self._notices([0.0])
        times = iter([started + 3600, started + 3601])
        with patch('devcontainer_setup.project_sync.scratch.time.time', side_effect=lambda: next(times)):
            notices.explain(ConnectionVerificationError('Codespace is Shutdown', 'Shutdown'), 'symptom')
        saved = json.loads((self.state / evidence.FILE).read_text())['bound-name']
        self.assertEqual(saved['stopped_at'], started + 3600)
        evidence.record_start(self.state, 'bound-name', started + 7200, 'started')
        notices.explain(ConnectionVerificationError('up', 'Available'), 'symptom')
        self.assertIsNone(json.loads((self.state / evidence.FILE).read_text())['bound-name']['stopped_at'])

    def test_a_cached_down_state_records_no_stop(self):
        # #61: a cached down reading must not stamp a stop onto a start recorded since.
        from devcontainer_setup.project_sync import evidence
        from devcontainer_setup.project_sync.replication import ConnectionVerificationError
        started = 1_700_000_000.0
        evidence.record_start(self.state, 'bound-name', started, 'started')
        now = [0.0]
        notices = self._notices(now)
        with patch('devcontainer_setup.project_sync.scratch.time.time', return_value=started + 100):
            notices.explain(ConnectionVerificationError('Codespace is Shutdown', 'Shutdown'), 'symptom')
        evidence.record_start(self.state, 'bound-name', started + 200, 'started')
        now[0] = 30.0
        with patch('devcontainer_setup.project_sync.scratch.time.time', return_value=started + 999) as clock:
            notices.explain(ValueError('forward down'), 'symptom')
        clock.assert_not_called()
        self.assertIsNone(json.loads((self.state / evidence.FILE).read_text())['bound-name']['stopped_at'])

    def test_unarmed_stop_notifies_once_until_syncing(self):
        # A stop seen in a cycle, the forward refusing while it lasts, a cycle passing verify(), then a second stop.
        runtime, log = self._work(['ok', self.FORWARD_DOWN, 'ok'], [self._stopped_cycle(), 'ok', self._stopped_cycle()])
        self.assertEqual(self.notes, [self.STOP_NOTE, self.STOP_NOTE])
        self.assertTrue(runtime['notified_stop'])
        self.assertEqual(runtime['error'], self.STOPPED)
        # The forward's refusal is reported by the state verify() read under a minute ago.
        self.assertEqual(log.count('Scratch waiting: ' + self.STOPPED), 3)
        self.assertNotIn('forward unavailable', log)
        # verify() carried its own reading, and the healthy path reads nothing.
        self.assertEqual(self.reads, [])

    def test_stop_first_seen_at_the_forward_names_the_state_and_notifies_once(self):
        self.replies.append((200, json.dumps({'name': 'bound-name', 'state': 'Shutdown'}).encode()))
        runtime, log = self._work([self.FORWARD_DOWN, self.FORWARD_DOWN])
        self.assertEqual(runtime['error'], self.STOPPED)
        self.assertEqual(self.notes, [self.STOP_NOTE])
        self.assertTrue(runtime['notified_stop'])
        # One read: the second refusal, under a minute later, reuses it.
        self.assertEqual(self.reads, ['https://api.github.com/user/codespaces/bound-name'])
        self.assertIn('Notify: ' + self.STOP_NOTE[1], log)
        self.assertNotIn('fixture-token', log + json.dumps(runtime))

    def test_forward_refusal_keeps_its_text_when_the_state_is_unknown_or_available(self):
        for reply in ((500, b'{}'), (200, json.dumps({'name': 'bound-name', 'state': 'Available'}).encode())):
            with self.subTest(reply=reply):
                self.replies[:] = [reply]
                self.notes.clear()
                runtime, _ = self._work([self.FORWARD_DOWN])
                self.assertEqual(runtime['error'], str(self.FORWARD_DOWN))
                self.assertEqual(self.notes, [])
                self.assertFalse(runtime['notified_stop'])

    def test_armed_stop_leaves_notifications_to_the_keeper(self):
        self._arm()
        healthy = {'running': True, 'heartbeat_age': 12}
        with patch('devcontainer_setup.project_sync.keeper.status', return_value=healthy) as status:
            runtime, _ = self._work(['ok', self.FORWARD_DOWN], ['ok', 'ok', self._stopped_cycle()])
        self.assertEqual(self.notes, [])
        self.assertFalse(runtime['notified_stop'])
        self.assertEqual(runtime['error'], self.STOPPED)
        # Two ticks and two waits within one minute: the keeper is checked once.
        self.assertEqual(status.call_count, 1)
        self.assertFalse(runtime['notified_keeper'])

    def test_dead_keeper_notifies_once(self):
        import functools
        self._arm()
        dead, hung, healthy = ({'running': False, 'heartbeat_age': None}, {'running': True, 'heartbeat_age': 301},
                               {'running': True, 'heartbeat_age': 5})
        # One clock reading per check here, 80 s apart: unhealthy from 160 s, notified at 400 s,
        # healthy at 560 s, unhealthy again from 640 s, notified again at 880 s.
        readings = [dead, hung, dead, dead, dead, healthy, dead, dead, dead, dead]
        def status(state):
            reading = readings.pop(0)
            if not readings:
                self._arm(False)  # disarmed: the next check ends the watch without a notice
            return reading
        ticks = iter(range(80, 10000, 80))
        stepped = functools.partial(self.module._Notices, clock=lambda: next(ticks))
        sent = []  # how many readings were left when each notice went out
        def notify(title, text):
            sent.append(len(readings))
            self.notes.append((title, text))
            return True
        with patch.object(self.module, '_Notices', stepped), \
                patch('devcontainer_setup.project_sync.notify.notify', side_effect=notify), \
                patch('devcontainer_setup.project_sync.keeper.status', side_effect=status) as watched:
            # The keeper is checked while waiting, then on each syncing tick.
            runtime, _ = self._work([self.FORWARD_DOWN, 'ok'], ['ok'] * 11)
        self.assertEqual(watched.call_count, 10)
        self.assertEqual(self.notes, [self.KEEPER_NOTE, self.KEEPER_NOTE])
        self.assertEqual(sent, [6, 0])  # after the 4th reading (400 s) and the 10th (880 s)
        self.assertFalse(runtime['notified_keeper'])  # the disarm cleared it

    def test_a_stop_through_a_reconnect_that_fails_verify_notifies_once(self):
        # The stop is first seen at the forward; a brief reconnect then fails verify(): one stop, one notice.
        self.replies.append((200, json.dumps({'name': 'bound-name', 'state': 'ShuttingDown'}).encode()))
        runtime, _ = self._work([self.FORWARD_DOWN, 'ok'], [self._stopped_cycle()])
        self.assertEqual(self.notes, [('Codespace recovery', "repo's Codespace stopped (ShuttingDown); "
                                       'run task work:cs to bring it back')])
        self.assertTrue(runtime['notified_stop'])

    def test_connects_that_each_fail_verify_notify_once(self):
        # Through the real verify(), whose own progress emits must not count as a passing cycle.
        from devcontainer_setup.project_sync import replication
        self.replies.extend([(200, json.dumps({'name': 'bound-name', 'state': 'Shutdown'}).encode())] * 2)
        with patch('devcontainer_setup.project_sync.workspace._verify_roots', side_effect=ValueError('fixture')):
            runtime, _ = self._work(['ok', 'ok'], [replication.sync_project_data] * 2)
        self.assertEqual(self.notes, [self.STOP_NOTE])
        self.assertEqual(len(self.reads), 2)
        self.assertEqual(runtime['error'], self.STOPPED)

    def test_a_verify_that_keeps_failing_backs_off_with_one_read_per_attempt(self):
        # A reachable Codespace whose identity check fails: only a passing verify() resets the backoff.
        from devcontainer_setup.project_sync import replication
        self.replies.extend([(200, json.dumps({'name': 'bound-name', 'state': 'Available'}).encode())] * 7)
        with patch('devcontainer_setup.project_sync.workspace._verify_roots', side_effect=ValueError('fixture')):
            runtime, _ = self._work(['ok'] * 7, [replication.sync_project_data] * 7)
        self.assertEqual(self.waits, [2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(len(self.reads), 7)
        self.assertEqual(runtime['error'], 'Project connection verification failed; identity/authentication must be valid')
        self.assertEqual(self.notes, [])

    def test_only_a_down_state_sends_the_stop_notice(self):
        for state, notified in (('Starting', False), ('Queued', False), ('Unknown', False),
                                ('ShuttingDown', True), ('Shutdown', True), ('Failed', True)):
            with self.subTest(state=state):
                (self.state / self.module.RUNTIME).unlink(missing_ok=True)
                self.notes.clear()
                self.replies[:] = [(200, json.dumps({'name': 'bound-name', 'state': state}).encode())]
                runtime, _ = self._work([self.FORWARD_DOWN])
                # The error names whatever state was read; only a down state notifies.
                self.assertEqual(runtime['error'], 'Codespace is ' + state + '; no start request was allowed.')
                self.assertEqual(bool(self.notes), notified)
                self.assertEqual(runtime['notified_stop'], notified)

    def test_waiting_is_written_before_the_state_read(self):
        seen = []
        def upstream(url, token):
            runtime = json.loads((self.state / self.module.RUNTIME).read_text())
            seen.append((runtime['phase'], runtime.get('error')))
            return 200, json.dumps({'name': 'bound-name', 'state': 'Shutdown'}).encode()
        with patch('devcontainer_setup.project_sync.transport._upstream', side_effect=upstream):
            runtime, log = self._work([self.FORWARD_DOWN])
        self.assertEqual(seen, [('waiting', str(self.FORWARD_DOWN))])
        self.assertEqual(runtime['error'], self.STOPPED)
        self.assertEqual(log.count('Scratch waiting: '), 1)

    def _notices(self, now):
        import logging
        config = dict(codespace='bound-name', root=str(self.root))
        return self.module._Notices(self.state, config, {}, logging.getLogger('fixture-notices'),
                                    clock=lambda: now[0])

    def test_keeper_watch_notifies_once_the_keeper_is_down_for_four_minutes(self):
        # R25, R26: an arm can start its keeper minutes after the worker (D1), so a notice waits out
        # 240 s of unhealthy readings; a healthy reading starts it over.
        self._arm()
        dead, healthy = {'running': False, 'heartbeat_age': None}, {'running': True, 'heartbeat_age': 5}
        for name, sequence, expected in (
                ('unhealthy for 239 s', [(0, dead), (80, dead), (160, dead), (239, dead)], 0),
                ('unhealthy for 240 s', [(0, dead), (80, dead), (160, dead), (240, dead)], 1),
                ('still unhealthy later', [(0, dead), (80, dead), (160, dead), (240, dead), (320, dead), (400, dead)], 1),
                ('a healthy reading starts it over', [(0, dead), (160, dead), (220, healthy), (280, dead), (500, dead)], 0),
                ('a re-arm starting its keeper 180 s later (D1)',
                 [(0, dead), (60, dead), (120, dead), (180, dead), (240, healthy), (300, healthy)], 0)):
            with self.subTest(name):
                self.notes.clear()
                now, readings = [0.0], [reading for _, reading in sequence]
                notices = self._notices(now)
                with patch('devcontainer_setup.project_sync.keeper.status', side_effect=lambda state: readings.pop(0)):
                    for at, _ in sequence:
                        now[0] = 1000.0 + at
                        notices.watch_keeper()
                self.assertEqual(readings, [])  # every reading was taken, none skipped by the check gate
                self.assertEqual(self.notes, [self.KEEPER_NOTE] * expected)

    def test_a_killed_keeper_is_reported_within_six_minutes(self):
        # Spec slice D: `kill -9` of the keeper gives the worker's notice within 6 min (R27), for every
        # steady syncing tick up to 30 s (the loop's 5 s wait plus its cycle) and wherever the death
        # falls between two checks.
        self._arm()
        dead, healthy = {'running': False, 'heartbeat_age': None}, {'running': True, 'heartbeat_age': 5}
        worst = {}
        for tick in range(5, 31):
            for phase in range(0, 80, 4):
                death, now, told = 120.5 + phase, [0.0], []
                notices = self._notices(now)
                with patch('devcontainer_setup.project_sync.keeper.status',
                           side_effect=lambda state: healthy if now[0] < death else dead), \
                        patch('devcontainer_setup.project_sync.notify.notify',
                              side_effect=lambda title, text: told.append(now[0]) or True):
                    while not told and now[0] < 2000:
                        notices.watch_keeper()
                        now[0] += tick
                self.assertTrue(told, (tick, phase))
                worst[tick] = max(worst.get(tick, 0), told[0] - death)
        self.assertEqual({tick: late for tick, late in worst.items() if late > 360}, {})

    def test_a_disarm_ends_the_keeper_watch_and_a_re_arm_starts_it_over(self):
        self._arm()
        dead = {'running': False, 'heartbeat_age': None}
        now = [1000.0]
        notices = self._notices(now)
        with patch('devcontainer_setup.project_sync.keeper.status', return_value=dead) as status:
            notices.watch_keeper()  # unhealthy from 0 s
            self._arm(False)
            now[0] = 1100.0
            notices.watch_keeper()  # disarmed: no reading, no notice
            self._arm()
            for at in (400, 600):  # re-armed and unhealthy for 200 s
                now[0] = 1000.0 + at
                notices.watch_keeper()
        self.assertEqual(status.call_count, 3)
        self.assertEqual(self.notes, [])

    def test_keeper_watch_reads_once_a_check_while_healthy_and_every_tick_while_not(self):
        self._arm()
        dead, healthy = {'running': False, 'heartbeat_age': None}, {'running': True, 'heartbeat_age': 5}
        now, current, readings = [1000.0], [healthy], []
        notices = self._notices(now)
        def status(state):
            readings.append(now[0] - 1000.0)
            return current[0]
        def watch(*times):
            for at in times:
                now[0] = 1000.0 + at
                notices.watch_keeper()
        with patch('devcontainer_setup.project_sync.keeper.status', side_effect=status):
            watch(0, 30, 59, 60)  # healthy: a reading a minute
            self.assertEqual(readings, [0, 60])
            current[0] = dead
            watch(65, 119, 120, 125, 130)  # the next check finds it dead; from then on every tick reads
            self.assertEqual(readings, [0, 60, 120, 125, 130])
            watch(*range(135, 360, 5))
            self.assertEqual(self.notes, [])
            watch(360)  # 240 s after the first unhealthy reading
            self.assertEqual(self.notes, [self.KEEPER_NOTE])
            del readings[:]
            watch(365, 370, 420)  # told: back to a reading a minute
            self.assertEqual(readings, [420])
        self.assertEqual(self.notes, [self.KEEPER_NOTE])

    def test_a_disarm_clears_the_keeper_notice_so_a_re_armed_keeper_is_watched(self):
        from devcontainer_setup.project_sync.state import atomic_write
        self._arm()
        now = [1000.0]
        notices = self._notices(now)
        with patch('devcontainer_setup.project_sync.keeper.status',
                   return_value={'running': False, 'heartbeat_age': None}):
            for at in (0, 240):  # dead for 240 s: told
                now[0] = 1000.0 + at
                notices.watch_keeper()
            self.assertEqual(self.notes, [self.KEEPER_NOTE])
            atomic_write(self.state / 'arming.json', b'{"schema": 2}\n')
            now[0] = 1300.0
            notices.watch_keeper()  # unreadable: not a disarm, so the notice stands
            self.assertTrue(notices.runtime['notified_keeper'])
            self._arm(False)
            now[0] = 1360.0
            notices.watch_keeper()  # disarmed: the notice is cleared
            self.assertFalse(notices.runtime['notified_keeper'])
            self._arm()
            for at in (420, 660):  # re-armed, and that keeper dead for 240 s: told again
                now[0] = 1000.0 + at
                notices.watch_keeper()
        self.assertEqual(self.notes, [self.KEEPER_NOTE] * 2)

    def test_unreadable_arming_sends_no_notice_and_logs_once(self):
        from devcontainer_setup.project_sync.state import atomic_write
        atomic_write(self.state / 'arming.json', b'{"schema": 2, "armed": false}\n')
        self.replies.append((200, json.dumps({'name': 'bound-name', 'state': 'Shutdown'}).encode()))
        with patch.object(self.module, 'KEEPER_CHECK', 0), \
                patch('devcontainer_setup.project_sync.keeper.status') as status:
            runtime, log = self._work([self.FORWARD_DOWN, 'ok'], ['ok', 'ok', self._stopped_cycle()])
        self.assertEqual(self.notes, [])
        status.assert_not_called()
        self.assertEqual(runtime['error'], self.STOPPED)
        lines = [line for line in log.splitlines() if 'arming.json' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('task work:status', lines[0])

    def test_a_good_connection_drops_an_old_stop_reading(self):
        # The stop stays open until a cycle passes verify() (R23), but once a connection works the old
        # reading no longer names the next failure: a local fault keeps its own text.
        self.replies.append((200, json.dumps({'name': 'bound-name', 'state': 'Shutdown'}).encode()))
        with patch.object(self.module, 'discover_project', side_effect=ValueError('fixture local fault after connecting')):
            runtime, log = self._work([self.FORWARD_DOWN, 'ok'])
        self.assertEqual(runtime['error'], 'fixture local fault after connecting')
        self.assertIn('Scratch waiting: fixture local fault after connecting', log)
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(self.notes, [self.STOP_NOTE])
        self.assertTrue(runtime['notified_stop'])

    def test_restarted_worker_keeps_the_notices_it_sent(self):
        import functools
        config = dict(identity='github.com/owner/repo', codespace='bound-name', root=str(self.root),
                      remote_root='/workspaces/repo')
        def restart_after(**sent):
            self.module._write(self.state / self.module.RUNTIME, dict(binding=config, phase='waiting', **sent))
        # Unarmed, the same stop still read: no second stop notice.
        restart_after(notified_stop=True)
        self.replies.append((200, json.dumps({'name': 'bound-name', 'state': 'Shutdown'}).encode()))
        runtime, _ = self._work([self.FORWARD_DOWN])
        self.assertEqual(self.notes, [])
        self.assertTrue(runtime['notified_stop'])
        # Armed, the same keeper still dead well past KEEPER_DOWN: no second keeper notice.
        self._arm()
        restart_after(notified_keeper=True)
        ticks = iter(range(80, 10000, 80))
        stepped = functools.partial(self.module._Notices, clock=lambda: next(ticks))
        with patch.object(self.module, '_Notices', stepped), \
                patch('devcontainer_setup.project_sync.keeper.status',
                      return_value={'running': False, 'heartbeat_age': None}) as status:
            runtime, _ = self._work([self.FORWARD_DOWN] * 4)  # watched at 160, 320, 480 and 640 s
        self.assertEqual(status.call_count, 4)
        self.assertEqual(self.notes, [])
        self.assertTrue(runtime['notified_keeper'])

    def test_only_planned_session_absence_is_retryable_and_inventory_failure_is_not_absence(self):
        receipt = {'name': 'scratch-' + 'a' * 48, 'nonce': 'a' * 48, 'binding': {}, 'established': False}
        with patch.object(self.module, '_mutagen', return_value='[]'):
            self.assertIsNone(self.module._session_info(Path('/fake/tool'), self.state, receipt))
            receipt['established'] = True
            with self.assertRaisesRegex(ValueError, 'baseline is missing'):
                self.module._session_info(Path('/fake/tool'), self.state, receipt)
        receipt['established'] = False
        with patch.object(self.module, '_mutagen', side_effect=ValueError('fixture daemon unavailable')):
            with self.assertRaisesRegex(ValueError, 'daemon unavailable'):
                self.module._session_info(Path('/fake/tool'), self.state, receipt)

    def test_mutagen_timeout_is_actionable_private_and_never_repeated(self):
        for operation in ('create', 'list', 'flush'):
            with self.subTest(operation=operation):
                timeout = subprocess.TimeoutExpired(['mutagen', 'private-detail'], 30, stderr='private-stderr')
                with patch.object(self.module.subprocess, 'run', side_effect=timeout) as run:
                    with self.assertRaisesRegex(ValueError, 'Mutagen command timed out; inspect session status before retrying') as caught:
                        self.module._mutagen('/fake/tool', self.state, 'sync', operation)
                self.assertNotIn('private-', str(caught.exception))
                self.assertEqual(run.call_count, 1)
