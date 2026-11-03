import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.app_config import AppConfig
from core.device.android.adapter import AdbError, AndroidAdapter
from core.device.ios.discovery import default_roots, discover_backups


class PlatformSupportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.profile = self.root / "windows-profile"
        self.appdata = self.root / "roaming"
        self.local = self.root / "local"
        self.windows_environment = {
            "USERPROFILE": str(self.profile),
            "APPDATA": str(self.appdata),
            "LOCALAPPDATA": str(self.local),
        }

    def test_macos_data_directory_ignores_windows_environment(self):
        with patch.object(sys, "platform", "darwin"), patch.object(Path, "home", return_value=self.home), \
                patch.dict(os.environ, self.windows_environment, clear=True):
            config = AppConfig.default()
            self.assertEqual(config.data_dir, self.home / "Library/Application Support/DeepRecover")
            config.initialize()
        self.assertTrue((config.data_dir / "Acquisitions").is_dir())
        self.assertFalse(self.local.exists())

    def test_data_directory_override_takes_precedence_on_every_platform(self):
        override = self.root / "custom workspace" / ".." / "selected"
        for platform in ("darwin", "win32", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                    patch.dict(os.environ, {**self.windows_environment, "DEEPRECOVER_DATA_DIR": str(override)}, clear=True):
                self.assertEqual(AppConfig.default().data_dir, override.resolve())

    def test_windows_and_other_existing_data_directories_are_preserved(self):
        for platform in ("win32", "linux"):
            for environment, expected in (({"LOCALAPPDATA": str(self.local)}, self.local), ({}, self.home)):
                with self.subTest(platform=platform, environment=environment), patch.object(sys, "platform", platform), \
                        patch.object(Path, "home", return_value=self.home), patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(AppConfig.default().data_dir, expected / "DeepRecover_Data")

    def test_finder_backup_directory_is_first_on_macos(self):
        with patch.object(sys, "platform", "darwin"), patch.object(Path, "home", return_value=self.home), \
                patch.dict(os.environ, self.windows_environment, clear=True):
            roots = default_roots()
        self.assertEqual(roots[0], self.home / "Library/Application Support/MobileSync/Backup")
        self.assertIn(self.profile / "Apple/MobileSync/Backup", roots)
        self.assertIn(self.appdata / "Apple Computer/MobileSync/Backup", roots)

    def test_windows_and_other_backup_search_paths_keep_existing_order(self):
        expected = [self.profile / "Apple/MobileSync/Backup",
                    self.appdata / "Apple Computer/MobileSync/Backup",
                    self.home / "Library/Application Support/MobileSync/Backup"]
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                    patch.object(Path, "home", return_value=self.home), \
                    patch.dict(os.environ, self.windows_environment, clear=True):
                self.assertEqual(default_roots(), expected)

    def test_finder_backup_discovery_reads_synthetic_sources_without_writes(self):
        finder = self.home / "Library/Application Support/MobileSync/Backup" / "finder-device"
        legacy = self.profile / "Apple/MobileSync/Backup" / "legacy-device"
        for path in (finder, legacy):
            path.mkdir(parents=True)
            (path / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
            (path / "Info.plist").write_bytes(plistlib.dumps({"Device Name": path.name}))
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.root.rglob("*") if path.is_file()}
        with patch.object(sys, "platform", "darwin"), patch.object(Path, "home", return_value=self.home), \
                patch.dict(os.environ, self.windows_environment, clear=True):
            backups = discover_backups()
        self.assertEqual([backup.path for backup in backups], [finder.resolve(), legacy.resolve()])
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_explicit_adb_and_environment_override_precede_path_and_homebrew(self):
        for executable, expected in ((self.root / "custom adb", self.root / "custom adb"),
                                     (None, str(self.root / "configured adb"))):
            with self.subTest(executable=executable), patch.object(sys, "platform", "darwin"), \
                    patch.dict(os.environ, {"DEEPRECOVER_ADB": str(self.root / "configured adb")}, clear=True), \
                    patch("core.device.android.adapter.shutil.which") as which, \
                    patch("core.device.android.adapter.os.path.isfile") as is_file:
                self.assertEqual(AndroidAdapter(executable).executable, expected)
                which.assert_not_called()
                is_file.assert_not_called()

    def test_path_adb_precedes_homebrew(self):
        with patch.object(sys, "platform", "darwin"), patch.dict(os.environ, {}, clear=True), \
                patch("core.device.android.adapter.shutil.which", return_value="/custom/bin/adb") as which, \
                patch("core.device.android.adapter.os.path.isfile") as is_file:
            self.assertEqual(AndroidAdapter().executable, "/custom/bin/adb")
            which.assert_called_once_with("adb")
            is_file.assert_not_called()

    def test_finder_launch_discovers_both_homebrew_prefixes(self):
        for candidate in ("/opt/homebrew/bin/adb", "/usr/local/bin/adb"):
            with self.subTest(candidate=candidate), patch.object(sys, "platform", "darwin"), \
                    patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True), \
                    patch("core.device.android.adapter.shutil.which", return_value=None), \
                    patch("core.device.android.adapter.os.path.isfile", side_effect=lambda path: path == candidate), \
                    patch("core.device.android.adapter.os.access", return_value=True) as access:
                self.assertEqual(AndroidAdapter().executable, candidate)
                access.assert_called_once_with(candidate, os.X_OK)

    def test_homebrew_directory_or_non_executable_file_is_rejected(self):
        for regular_file, executable in ((False, True), (True, False)):
            with self.subTest(regular_file=regular_file, executable=executable), \
                    patch.object(sys, "platform", "darwin"), patch.dict(os.environ, {}, clear=True), \
                    patch("core.device.android.adapter.shutil.which", return_value=None), \
                    patch("core.device.android.adapter.os.path.isfile", return_value=regular_file), \
                    patch("core.device.android.adapter.os.access", return_value=executable):
                adapter = AndroidAdapter()
                self.assertIsNone(adapter.executable)
                with self.assertRaises(AdbError):
                    adapter.devices()

    def test_homebrew_fallback_is_not_used_outside_macos(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch("core.device.android.adapter.shutil.which", return_value=None), \
                    patch("core.device.android.adapter.os.path.isfile") as is_file:
                self.assertIsNone(AndroidAdapter().executable)
                is_file.assert_not_called()

    def test_invalid_configured_adb_reports_error_without_fallback(self):
        def runner(*args, **kwargs):
            raise FileNotFoundError("synthetic missing configured executable")

        with patch.object(sys, "platform", "darwin"), \
                patch.dict(os.environ, {"DEEPRECOVER_ADB": "/missing/configured-adb"}, clear=True), \
                patch("core.device.android.adapter.shutil.which") as which, \
                patch("core.device.android.adapter.os.path.isfile") as is_file:
            with self.assertRaisesRegex(AdbError, "ADB 无法启动"):
                AndroidAdapter(runner=runner).devices()
            which.assert_not_called()
            is_file.assert_not_called()

    def test_homebrew_discovery_does_not_authorize_a_device(self):
        calls = []
        state = "unauthorized"

        def runner(command, **kwargs):
            calls.append(command)
            self.assertNotIn("shell", kwargs)
            return subprocess.CompletedProcess(command, 0, f"List of devices attached\nSERIAL {state}\n")

        with patch.object(sys, "platform", "darwin"), patch.dict(os.environ, {}, clear=True), \
                patch("core.device.android.adapter.shutil.which", return_value=None), \
                patch("core.device.android.adapter.os.path.isfile", return_value=True), \
                patch("core.device.android.adapter.os.access", return_value=True):
            adapter = AndroidAdapter(runner=runner)
            destination = self.root / "never-copied"
            for state in ("unauthorized", "offline", "no permissions"):
                with self.subTest(state=state):
                    with self.assertRaises(AdbError):
                        adapter.list_files("SERIAL", "/sdcard/DCIM")
                    with self.assertRaises(AdbError):
                        adapter.pull_file("SERIAL", "/sdcard/DCIM/photo.jpg", destination)
        self.assertFalse(destination.exists())
        self.assertEqual(calls, [["/opt/homebrew/bin/adb", "devices", "-l"]] * 6)


if __name__ == "__main__":
    unittest.main()
