"""Lifecycle and immutable recovery use only private fixture state."""
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
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
        finally:
            self.module.stop(self.state)
        self.assertLess((self.state / 'scratch.log').stat().st_size, 256 * 1024)

    def test_case_collision_is_visible(self):
        (self.scratch / 'case').write_bytes(b'a')
        other = self.scratch / 'CASE'
        other.write_bytes(b'b')
        if len(list(self.scratch.iterdir())) != 2:
            self.skipTest('case-insensitive filesystem')
        with self.assertRaisesRegex(ValueError, 'case collision'):
            self.module.backup(self.root, self.state)

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

    @unittest.skipUnless(os.environ.get('MUTAGEN_TEST_BINARY'), 'set MUTAGEN_TEST_BINARY to the pinned official binary for real restart gate')
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

    @unittest.skipUnless(os.environ.get('MUTAGEN_TEST_BINARY'), 'set MUTAGEN_TEST_BINARY for real retained-baseline migration gate')
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
