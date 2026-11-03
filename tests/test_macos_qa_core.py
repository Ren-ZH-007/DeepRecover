"""Synthetic core QA; platform and privacy-denial cases also run on Linux/Windows."""
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app.app_config import AppConfig
from core.device.android.adapter import AdbError, AndroidAdapter
from core.device.ios.discovery import discover_backups, inspect_backup


class MacosCoreQaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="deeprecover-core-qa-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def backup(self, name):
        path = self.root / name
        path.mkdir()
        (path / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
        (path / "Info.plist").write_bytes(plistlib.dumps({"Device Name": name}))
        return path

    def inventory(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_truncated_xml_metadata_stays_visible_without_hiding_other_backups(self):
        damaged = self.backup("damaged")
        valid = self.backup("valid")
        for name in ("Manifest.plist", "Info.plist"):
            with self.subTest(metadata=name):
                original = (damaged / name).read_bytes()
                (damaged / name).write_bytes(b'<?xml version="1.0"?><plist><dict><key>truncated')
                before = self.inventory()
                found = {backup.path: backup for backup in discover_backups([self.root])}
                self.assertTrue(found[damaged].error)
                self.assertIsNone(found[damaged].encrypted)
                self.assertFalse(found[valid].error)
                self.assertEqual(self.inventory(), before)
                (damaged / name).write_bytes(original)

    def test_direct_selection_with_only_manifest_database_reports_missing_metadata(self):
        path = self.root / "incomplete"
        path.mkdir()
        (path / "Manifest.db").write_bytes(b"synthetic incomplete backup")
        before = self.inventory()
        found = discover_backups([path])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, path)
        self.assertTrue(found[0].error)
        self.assertIsNone(found[0].encrypted)
        self.assertEqual(self.inventory(), before)

    def test_unreadable_search_root_reports_permissions_and_continues(self):
        denied = self.root / "protected-root"
        denied.mkdir()
        valid = self.backup("readable-backup")
        original_iterdir = Path.iterdir

        def iterdir(path):
            if path == denied:
                raise PermissionError("synthetic privacy denial")
            return original_iterdir(path)

        before = self.inventory()
        with patch.object(Path, "iterdir", iterdir):
            found = {backup.path: backup for backup in discover_backups([denied, valid])}
        self.assertIn("权限", found[denied].error)
        self.assertIsNone(found[denied].encrypted)
        self.assertFalse(found[valid].error)
        self.assertEqual(self.inventory(), before)

    def test_unreadable_child_does_not_discard_readable_sibling(self):
        denied = self.backup("protected-child")
        valid = self.backup("readable-child")
        original_is_dir = Path.is_dir

        def is_dir(path):
            if path == denied:
                raise PermissionError("synthetic child permission denial")
            return original_is_dir(path)

        with patch.object(Path, "is_dir", is_dir):
            found = {backup.path: backup for backup in discover_backups([self.root])}
        self.assertIn("权限", found[denied].error)
        self.assertFalse(found[valid].error)

    def test_metadata_permission_denial_is_distinguished_from_corruption(self):
        backup = self.backup("metadata-protected")
        protected = backup / "Manifest.plist"
        original_open = Path.open

        def open_file(path, *args, **kwargs):
            if path == protected:
                raise PermissionError("synthetic metadata denial")
            return original_open(path, *args, **kwargs)

        before = self.inventory()
        with patch.object(Path, "open", open_file):
            result = inspect_backup(backup)
        self.assertIn("权限", result.error)
        self.assertIsNone(result.encrypted)
        self.assertEqual(self.inventory(), before)

    def test_metadata_canonical_path_cannot_escape_selected_backup(self):
        # Model the OS's symlink resolution without needing Windows link privileges.
        for name in ("Manifest.plist", "Info.plist"):
            with self.subTest(metadata=name):
                backup = self.backup(name + "-backup")
                metadata = backup / name
                external = self.root / (name + "-external")
                external.write_bytes(plistlib.dumps({"IsEncrypted": False, "Device Name": "outside"}))
                original_resolve = Path.resolve
                original_open = Path.open
                opened = []

                def resolve(path, *args, **kwargs):
                    return external if path == metadata else original_resolve(path, *args, **kwargs)

                def open_file(path, *args, **kwargs):
                    opened.append(path)
                    return original_open(path, *args, **kwargs)

                before = self.inventory()
                with patch.object(Path, "resolve", resolve), patch.object(Path, "open", open_file):
                    result = inspect_backup(backup)
                self.assertIn("超出", result.error)
                self.assertIsNone(result.encrypted)
                self.assertNotIn(external, opened)
                self.assertEqual(self.inventory(), before)

    def test_metadata_alias_within_backup_remains_readable(self):
        backup = self.backup("internal-alias")
        metadata = backup / "Manifest.plist"
        internal = backup / "manifest-current.plist"
        internal.write_bytes(plistlib.dumps({"IsEncrypted": False}))
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            return internal if path == metadata else original_resolve(path, *args, **kwargs)

        before = self.inventory()
        with patch.object(Path, "resolve", resolve):
            result = inspect_backup(backup)
        self.assertFalse(result.error)
        self.assertFalse(result.encrypted)
        self.assertEqual(self.inventory(), before)

    def test_metadata_directory_and_symlink_loop_are_reported_as_corrupt(self):
        backup = self.backup("invalid-metadata")
        metadata = backup / "Manifest.plist"
        metadata.unlink()
        metadata.mkdir()
        self.assertTrue(inspect_backup(backup).error)
        with patch.object(Path, "resolve", side_effect=RuntimeError("synthetic symlink loop")):
            self.assertTrue(inspect_backup(backup).error)

    def test_missing_search_root_is_not_reported_as_a_backup(self):
        self.assertEqual(discover_backups([self.root / "absent"]), [])

    def test_unicode_tilde_work_directory_override_remains_local(self):
        home = self.root / "home"
        environment = {"HOME": str(home), "USERPROFILE": str(home),
                       "DEEPRECOVER_DATA_DIR": "~/数据 工作目录"}
        with patch.dict(os.environ, environment):
            config = AppConfig.default()
        self.assertEqual(config.data_dir, home / "数据 工作目录")
        config.initialize()
        self.assertTrue((config.data_dir / "Acquisitions").is_dir())

    def test_work_directory_file_collision_preserves_existing_bytes(self):
        config = AppConfig(self.root / "work")
        config.data_dir.mkdir()
        occupied = config.data_dir / "Acquisitions"
        occupied.write_bytes(b"keep existing user file")
        with self.assertRaises(FileExistsError):
            config.initialize()
        self.assertEqual(occupied.read_bytes(), b"keep existing user file")

    def test_authorized_other_device_never_authorizes_the_selected_device(self):
        for state in ("unauthorized", "offline", "no permissions", "recovery", "sideload", "bootloader"):
            with self.subTest(state=state):
                calls = []

                def runner(command, **kwargs):
                    calls.append(command)
                    return subprocess.CompletedProcess(command, 0, f"OTHER device\nSELECTED {state}\n")

                adapter = AndroidAdapter(self.root / "工具 目录" / "adb", runner)
                with self.assertRaises(AdbError):
                    adapter.list_files("SELECTED", "/sdcard/DCIM")
                destination = self.root / "not-created"
                with self.assertRaises(AdbError):
                    adapter.pull_file("SELECTED", "/sdcard/DCIM/photo.jpg", destination)
                self.assertEqual([command[1:] for command in calls], [["devices", "-l"]] * 2)
                self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
