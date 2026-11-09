"""Opt-in managed AppImage file lifecycle; only temporary, headless user homes."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


APPIMAGE = None


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class ApplicationInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='vidgrab-application-test-')
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / 'Home with বাংলা % dollars'
        self.home.mkdir(mode=0o700)
        self.environment = {**os.environ, 'HOME': str(self.home),
            'XDG_DATA_HOME': str(self.home / 'data'), 'XDG_CONFIG_HOME': str(self.home / 'config'),
            'XDG_CACHE_HOME': str(self.home / 'cache'), 'PATH': '/usr/bin:/bin'}
        self.environment.pop('APPIMAGE_EXTRACT_AND_RUN', None)

    def command(self, flag, *, app=None, success=True):
        result = subprocess.run([str(app or APPIMAGE), flag], env=self.environment,
            capture_output=True, text=True, timeout=25)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, 'The unsafe action must be refused')
        return result.stderr

    def install(self):
        result = self.command('--install-appimage')
        self.assertTrue(result['installed'])
        self.assertEqual(result['kind'], 'appimage')
        return Path(result['app']), Path(result['shortcut'])

    def test_status_does_not_register_or_create_user_storage(self):
        status = self.command('--installation-status')
        self.assertEqual(status['kind'], 'appimage')
        self.assertFalse(status['installed'])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_install_remove_reinstall_preserves_paths_state_and_disconnection(self):
        source_hash = digest(APPIMAGE)
        registered = self.command('--register-browser-host')
        native_dir = self.home / 'data/vidgrab-p02/native'
        descriptor = native_dir / 'host-config.json'
        selected_state = self.home / 'selected state'
        selected_media = self.home / 'selected media বাংলা'
        selected_state.mkdir(); selected_media.mkdir()
        database = selected_state / 'vidgrab.db'
        media = selected_media / 'finished.mp3'
        database.write_bytes(b'preserved fixture state')
        media.write_bytes(b'preserved fixture media')
        registered.update(data_dir=str(selected_state), download_dir=str(selected_media))
        descriptor.write_text(json.dumps(registered))
        self.command('--unregister-browser-host')
        self.assertTrue((native_dir / 'browser-connection.json').is_file(),
            'Use the preview with persistent browser disconnection before testing installation')
        preference = (native_dir / 'browser-connection.json').read_bytes()
        app, shortcut = self.install()
        self.assertNotEqual(app, APPIMAGE)
        self.assertTrue(app.is_relative_to(self.home))
        self.assertEqual(digest(app), source_hash)
        self.assertTrue(shortcut.is_file())
        self.assertIn('Terminal=false', shortcut.read_text())
        self.assertIn('%%', shortcut.read_text(), 'Percent signs must survive desktop Exec expansion')
        self.assertEqual(json.loads(descriptor.read_text())['app'], str(app))
        self.assertEqual((native_dir / 'browser-connection.json').read_bytes(), preference)
        self.assertFalse(self.command('--browser-connection-status', app=app)['enabled'])
        before = {path: digest(path) for path in (database, media, descriptor)}
        removed = self.command('--remove-appimage', app=app)
        self.assertFalse(removed['installed'])
        self.assertFalse(app.exists()); self.assertFalse(shortcut.exists())
        self.assertEqual({path: digest(path) for path in before}, before)
        self.assertTrue((native_dir / 'vidgrab-native-host').is_file())
        self.assertEqual((native_dir / 'browser-connection.json').read_bytes(), preference)
        reinstalled, _ = self.install()
        self.assertEqual(reinstalled, app)
        self.assertEqual({path: digest(path) for path in before}, before)
        self.assertFalse(self.command('--browser-connection-status', app=app)['enabled'])
        self.assertEqual(digest(APPIMAGE), source_hash)

    def test_foreign_installation_file_is_preserved(self):
        app = self.home / 'data/vidgrab-p02/application/VidGrab.AppImage'
        app.parent.mkdir(mode=0o700, parents=True)
        app.write_bytes(b'another application')
        self.assertIn('another installation', self.command('--install-appimage', success=False))
        self.assertEqual(app.read_bytes(), b'another application')
        self.assertFalse((self.home / 'data/vidgrab-p02/native').exists())

    def test_symlink_installation_directory_is_refused(self):
        target = self.home / 'foreign'
        target.mkdir()
        directory = self.home / 'data/vidgrab-p02/application'
        directory.parent.mkdir(parents=True)
        directory.symlink_to(target, target_is_directory=True)
        self.assertIn('symlink', self.command('--install-appimage', success=False))
        self.assertEqual(list(target.iterdir()), [])

    def test_modified_managed_image_is_not_removed(self):
        app, shortcut = self.install()
        with app.open('r+b') as stream:
            stream.seek(-1, os.SEEK_END)
            old = stream.read(1)
            stream.seek(-1, os.SEEK_END)
            stream.write(bytes([old[0] ^ 1]))
        self.assertIn('changed', self.command('--remove-appimage', success=False))
        self.assertTrue(app.exists()); self.assertTrue(shortcut.exists())

    def test_foreign_shortcut_is_not_overwritten_or_removed(self):
        app, shortcut = self.install()
        shortcut.write_text('[Desktop Entry]\nType=Application\nName=Foreign\nExec=/another/application\n')
        foreign = shortcut.read_bytes()
        self.assertIn('changed', self.command('--install-appimage', success=False))
        self.assertIn('changed', self.command('--remove-appimage', success=False))
        self.assertEqual(shortcut.read_bytes(), foreign)
        self.assertTrue(app.is_file())

    def test_running_owner_blocks_headless_removal(self):
        app, shortcut = self.install()
        config = json.loads((self.home / 'data/vidgrab-p02/native/host-config.json').read_text())
        ipc = Path(config['data_dir']) / 'ipc'
        ipc.mkdir(mode=0o700, parents=True)
        with (ipc / 'owner.lock').open('w+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn('Quit VidGrab', self.command('--remove-appimage', success=False))
            self.assertTrue(app.exists()); self.assertTrue(shortcut.exists())
        self.command('--remove-appimage')
        self.assertFalse(app.exists())


def main():
    global APPIMAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--appimage', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Select a fresh report path')
    APPIMAGE = args.appimage.resolve(strict=True)
    source_hash = digest(APPIMAGE)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ApplicationInstallationTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert digest(APPIMAGE) == source_hash, 'The selected source image must remain unchanged'
    args.report.write_text(json.dumps({'result': 'passed' if result.wasSuccessful() else 'failed',
        'appimage': str(APPIMAGE), 'sha256': source_hash, 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors),
        'limits': ['headless file lifecycle; no GUI, package manager or engine startup']}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
