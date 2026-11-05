"""Read-only observations and guarded local report saving through real Qt."""
import hashlib
import json
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch

from tests import test_computer_ui as base
from core.computer.io import DiskError

if base.QT_AVAILABLE:
    from ui.settings.diagnostics import (BUSY_MESSAGE, CHECK_ERROR, SAVE_ERROR,
        DiagnosticsDialog)
    from core.diagnostics import DiagnosticsError, collect_environment


@unittest.skipUnless(base.QT_AVAILABLE, 'PySide6 not installed')
class DiagnosticsUiTests(unittest.TestCase):
    setUp = base.ComputerUiTests.setUp
    close_window = base.ComputerUiTests.close_window

    def dialog(self):
        dialog = DiagnosticsDialog(self.window)
        dialog.show()
        self.addCleanup(dialog.close)
        return dialog

    def finish(self, error=None):
        future = self.page._future
        self.assertIsNotNone(future)
        if error:
            with self.assertRaises(error):
                future.result(5)
        else:
            future.result(5)
        self.window.poll()
        self.app.processEvents()

    def checked(self):
        dialog = self.dialog()
        dialog.start_check()
        self.finish()
        self.assertIsNotNone(dialog.report)
        return dialog

    def test_real_collection_does_not_create_tasks_change_database_or_touch_source(self):
        dialog = self.dialog()
        before = self.config.database.read_bytes()
        image_hash = hashlib.sha256(self.image.read_bytes()).hexdigest()
        report_files = list((self.config.data_dir / 'Reports').iterdir())
        dialog.start_check()
        self.assertFalse(dialog.save_button.isEnabled())
        self.finish()
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertEqual(self.repo.tasks(), [])
        self.assertEqual(self.config.database.read_bytes(), before)
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), report_files)
        self.assertEqual(hashlib.sha256(self.image.read_bytes()).hexdigest(), image_hash)
        self.assertEqual(dialog.report['manual_acceptance']['status'], 'not_run')
        self.assertTrue(all(case['status'] == 'not_run' for case in dialog.report['manual_acceptance']['cases']))
        self.assertIn('未执行', dialog.status_label.text())
        self.assertIn('未测试写入', dialog.values['workspace'].text())

    def test_result_selection_changes_preserve_open_diagnostics_and_main_close_closes_it(self):
        self.window.show_environment_diagnostics()
        self.finish()
        dialog = self.window.diagnostics_dialog
        self.assertTrue(dialog.isVisible())
        self.window.clear_preview()
        self.assertTrue(dialog.isVisible())
        self.assertIsNone(self.window.evidence_dialog)
        self.window.close()
        self.app.processEvents()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(dialog.isVisible())
        self.assertTrue(dialog._closed)

    def test_normal_save_is_local_redacted_new_file_and_preserves_database(self):
        dialog = self.checked()
        before = self.config.database.read_bytes()
        dialog.save_button.click()
        self.finish()
        first = dialog.saved_path
        self.assertIsNotNone(first)
        self.assertEqual(first.parent, self.config.data_dir / 'Reports')
        payload = json.loads(first.read_text())
        self.assertEqual(payload['manual_acceptance']['status'], 'not_run')
        self.assertNotIn(str(self.root), first.read_text())
        self.assertEqual(payload['kind'], 'environment_readiness')
        self.assertEqual(self.config.database.read_bytes(), before)
        self.assertEqual(self.repo.tasks(), [])
        original = first.read_bytes()
        dialog.save_report()
        self.finish()
        self.assertNotEqual(first, dialog.saved_path)
        self.assertEqual(first.read_bytes(), original)
        self.assertIn('报告未上传', dialog.status_label.text())

    def test_failures_do_not_leak_exception_message_through_dialog_computer_or_statusbar(self):
        dialog = self.dialog()
        secret = 'private-user /Users/private-person device-serial-secret'
        with patch('ui.settings.diagnostics.collect_environment', side_effect=RuntimeError(secret)):
            dialog.start_check()
            self.finish(DiagnosticsError)
        self.assertIsNone(dialog.report)
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertEqual(dialog.status_label.text(), CHECK_ERROR)
        for text in (dialog.status_label.text(), self.page.status.text(), self.window.statusBar().currentMessage()):
            self.assertNotIn('private', text)
            self.assertNotIn('device-serial-secret', text)
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), [])
        self.assertEqual(self.repo.tasks(), [])

    def test_busy_task_blocks_check_and_report_save(self):
        dialog = self.checked()
        self.window.run_task('diagnostic_busy_fixture', lambda token: self.release.wait(10), lambda *_: None)
        self.assertIsNotNone(self.window.pending)
        with patch('ui.settings.diagnostics.collect_environment') as collect, \
                patch('ui.settings.diagnostics.write_environment_report') as write:
            dialog.start_check()
            dialog.save_report()
        collect.assert_not_called()
        write.assert_not_called()
        self.assertEqual(dialog.status_label.text(), BUSY_MESSAGE)
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), [])
        self.release.set()
        self.window.pending[0].future.result(5)
        self.window.poll()

    def test_same_or_unknown_raw_disk_guard_refuses_save_before_report_write(self):
        dialog = self.checked()
        self.page._set_source('/dev/disk99', 'device')
        before = self.config.database.read_bytes()
        with patch('core.computer.devices.ensure_different_device', side_effect=DiskError('secret raw source name')) as guard:
            dialog.save_report()
            self.finish(DiagnosticsError)
        guard.assert_called_once_with('/dev/disk99', self.config.data_dir)
        self.assertIsNone(dialog.saved_path)
        self.assertEqual(dialog.status_label.text(), SAVE_ERROR)
        self.assertNotIn('secret', self.page.status.text())
        self.assertNotIn('secret', self.window.statusBar().currentMessage())
        self.assertEqual(self.config.database.read_bytes(), before)
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), [])

    def test_highlighted_raw_device_is_guarded_before_partition_selection(self):
        from core.computer.devices import DiskDevice
        dialog = self.checked()
        self.page.set_devices([DiskDevice('/dev/disk99', 'test raw source', 1000)])
        self.page.device_tree.setCurrentItem(self.page.device_item('/dev/disk99'))
        self.assertIsNone(self.page.source_path)
        with patch('core.computer.devices.ensure_different_device', side_effect=DiskError('private source')) as guard:
            dialog.save_report()
            self.finish(DiagnosticsError)
        guard.assert_called_once_with('/dev/disk99', self.config.data_dir)
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), [])

    def test_source_change_between_preflight_and_callback_blocks_write(self):
        dialog = self.checked()
        self.page._set_source(self.image, 'image')
        dialog.save_report()
        self.page._future.result(5)
        self.page._set_source(self.root / 'other-image.img', 'image')
        self.window.poll()
        self.assertIsNone(dialog.saved_path)
        self.assertEqual(dialog.status_label.text(), SAVE_ERROR)
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), [])

    def test_workspace_identity_change_after_preflight_blocks_write(self):
        dialog = self.checked()
        dialog.save_report()
        self.page._future.result(5)
        identity = dialog._report_identity
        with patch('ui.settings.diagnostics._identity', return_value=(identity[0], identity[1] + 1)):
            self.window.poll()
        self.assertIsNone(dialog.saved_path)
        self.assertEqual(dialog.status_label.text(), SAVE_ERROR)
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), [])

    def test_close_before_save_callback_never_creates_report(self):
        dialog = self.checked()
        dialog.save_report()
        self.page._future.result(5)
        dialog.close()
        self.window.poll()
        self.assertTrue(dialog._closed)
        self.assertIsNone(dialog.saved_path)
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), [])

    def test_close_during_collection_discards_report_and_other_dialog_cannot_borrow_job(self):
        first = self.dialog()
        second = self.dialog()
        release = Event()
        self.addCleanup(release.set)
        def collect(*args, **kwargs):
            release.wait(5)
            return collect_environment(*args, **kwargs)
        with patch('ui.settings.diagnostics.collect_environment', side_effect=collect):
            first.start_check()
            owned = self.page._future
            second.start_check()
            self.assertIsNone(second.report)
            self.assertEqual(second.status_label.text(), BUSY_MESSAGE)
            second.close()
            self.assertIs(self.page._future, owned)
            self.assertFalse(self.page._cancel_requested)
            first.close()
            release.set()
            owned.result(5)
            self.window.poll()
        self.assertIsNone(first.report)
        self.assertIsNone(second.report)
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), [])

    def test_recheck_clears_old_report_and_failed_save_does_not_show_raw_error(self):
        dialog = self.checked()
        with patch('ui.settings.diagnostics.collect_environment', side_effect=RuntimeError('private collection failure')):
            dialog.start_check()
            self.assertIsNone(dialog.report)
            self.assertFalse(dialog.save_button.isEnabled())
            self.finish(DiagnosticsError)
        dialog.start_check()
        self.finish()
        with patch('ui.settings.diagnostics.write_environment_report', side_effect=OSError('private report destination')):
            dialog.save_report()
            self.finish()
        self.assertIsNone(dialog.saved_path)
        self.assertEqual(dialog.status_label.text(), SAVE_ERROR)
        self.assertEqual(list((self.config.data_dir / 'Reports').iterdir()), [])
