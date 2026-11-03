"""Portable Qt boundary checks; native Cocoa verification runs separately."""
import hashlib
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent, QKeySequence
    from PySide6.QtWidgets import QApplication, QPushButton, QMessageBox
    from app.main_window import MainWindow
    from core.preview.images import preview_image
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

from app.app_config import AppConfig
from core.candidate_io import snapshot
from core.device.android.adapter import AndroidAdapter, AndroidDevice
from core.export.service import copy_verified
from core.tasks import TaskCancelled
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource
from tests.test_scanner import png_image


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')
class MacosQaUiTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = AppConfig(self.root / 'data')
        self.config.initialize()
        self.repo = Repository(self.config.database)
        self.window = MainWindow(self.config, self.repo)
        self.releases = []
        self.addCleanup(self.close_window)
        self.window.show()
        self.app.processEvents()

    def close_window(self):
        for release in self.releases:
            release.set()
        if self.window.pending:
            try:
                self.window.pending[0].future.result(5)
            except TaskCancelled:
                pass
            self.window.poll()
        self.window.close()
        self.app.processEvents()

    def candidate(self):
        payload = png_image()
        path = self.config.data_dir / 'Acquisitions' / '测试图片.png'
        path.write_bytes(payload)
        source = DataSource('android', 'SYNTHETIC', '本地测试设备')
        task = self.repo.create_task('android_acquisition', source)
        candidate = Candidate('android_file', '测试图片.png', 'png', len(payload),
            local_path=str(path), original_path='/sdcard/DCIM/测试图片.png',
            sha256=hashlib.sha256(payload).hexdigest(), source_stat=snapshot(path.stat()), source_id=source.id)
        self.repo.add_candidate(task, candidate)
        self.repo.update_task(task, 'completed')
        self.window.open_results(task)
        self.window.result_table.selectRow(0)
        return path, payload

    def attempt_exit(self, shortcut):
        if not shortcut:
            self.window.close()
            return
        # Some offscreen backends supply no standard Quit binding. Inject a
        # portable Qt key only for dispatch testing, never as Cocoa evidence.
        action = self.window.quit_action
        if not action.shortcuts():
            action.setShortcut(QKeySequence('Ctrl+Q', QKeySequence.SequenceFormat.PortableText))
        key = action.shortcuts()[0][0]
        dialog = self.window.export_dialog
        target = dialog if dialog and dialog.isVisible() else self.window
        target.activateWindow()
        self.app.processEvents()
        self.app.sendEvent(target, QKeyEvent(QEvent.Type.KeyPress, key.key(), key.keyboardModifiers()))
        self.app.sendEvent(target, QKeyEvent(QEvent.Type.KeyRelease, key.key(), key.keyboardModifiers()))
        # Synthetic modified key events leave Qt's keyboard state at the last
        # KeyPress modifiers. A neutral event prevents leakage into later row
        # selection tests; key releases alone do not refresh that Qt state.
        for event_type in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            self.app.sendEvent(target, QKeyEvent(event_type, Qt.Key.Key_unknown, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.app.keyboardModifiers(), Qt.KeyboardModifier.NoModifier)

    def check_running_exit(self, operation, shortcut):
        path, payload = self.candidate()
        prior = self.repo.create_task('export')
        self.repo.update_task(prior, 'completed')
        self.window.show_export_history(prior)
        self.app.processEvents()
        original_dialog = self.window.export_dialog
        started, release = Event(), Event()
        self.releases.append(release)

        def wait_for_exit():
            started.set()
            if not release.wait(5):
                raise RuntimeError('QA worker was not released')

        with ExitStack() as patches:
            if operation == 'preview':
                def gated_preview(*args):
                    wait_for_exit()
                    return preview_image(*args)
                patches.enter_context(patch('app.main_window.preview_image', side_effect=gated_preview))
                self.window.preview_selected()
            elif operation == 'export':
                def gated_copy(*args):
                    wait_for_exit()
                    return copy_verified(*args)
                patches.enter_context(patch('core.export.service.copy_verified', side_effect=gated_copy))
                patches.enter_context(patch('app.main_window.QFileDialog.getExistingDirectory', return_value=str(self.root / 'exports')))
                patches.enter_context(patch('app.main_window.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes))
                self.window.export_results(False)
            else:
                class LocalAdapter:
                    shared_path = staticmethod(AndroidAdapter.shared_path)
                    def pull_file(adapter, serial, remote, destination, token):
                        destination.write_bytes(payload)
                        wait_for_exit()
                        token.checkpoint()
                self.window.listed_device = AndroidDevice('SYNTHETIC', 'device', '本地测试设备')
                self.window.android_files = ['/sdcard/DCIM/测试图片.png']
                self.window.fill(self.window.android_file_table, [(self.window.android_files[0],)])
                self.window.android_file_table.selectRow(0)
                patches.enter_context(patch('app.main_window.AndroidAdapter', LocalAdapter))
                patches.enter_context(patch('app.main_window.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes))
                self.window.collect_android()

            self.assertIsNotNone(self.window.pending)
            handle = self.window.pending[0]
            self.assertTrue(started.wait(5))
            with patch.object(self.window.manager, 'shutdown', wraps=self.window.manager.shutdown) as shutdown:
                self.attempt_exit(shortcut)
                self.assertTrue(self.window.isVisible())
                self.assertTrue(original_dialog.isVisible())
                self.assertTrue(self.window.timer.isActive())
                self.assertFalse(handle.future.done())
                with self.assertRaises(TaskCancelled):
                    handle.token.checkpoint()
                shutdown.assert_not_called()
                release.set()
                with self.assertRaises(TaskCancelled):
                    handle.future.result(5)
                self.window.poll()
                self.assertIsNone(self.window.pending)
                self.assertEqual(self.repo.task(handle.id)['status'], 'cancelled')
                self.assertEqual(path.read_bytes(), payload)
                dialog = self.window.export_dialog
                if operation == 'export':
                    records = self.repo.exports(handle.id)
                    self.assertEqual([row['status'] for row in records], ['cancelled'])
                    self.assertFalse(Path(records[0]['output_path']).exists())
                    self.assertTrue(Path(self.repo.task(handle.id)['result_path']).is_file())
                elif operation == 'acquisition':
                    self.assertEqual(self.repo.candidate_count(handle.id), 0)
                    self.assertEqual(list((self.config.data_dir / 'Acquisitions').iterdir()), [path])
                else:
                    self.assertTrue(self.window.preview_label.pixmap() is None or self.window.preview_label.pixmap().isNull())
                self.attempt_exit(shortcut)
                self.assertFalse(self.window.isVisible())
                self.assertFalse(dialog.isVisible())
                self.assertFalse(self.window.timer.isActive())
                shutdown.assert_called_once()

    def test_preview_window_close_waits_for_cancellation(self):
        self.check_running_exit('preview', False)

    def test_preview_quit_shortcut_waits_for_cancellation(self):
        self.check_running_exit('preview', True)

    def test_export_window_close_waits_and_preserves_audit(self):
        self.check_running_exit('export', False)

    def test_export_quit_shortcut_waits_and_preserves_audit(self):
        self.check_running_exit('export', True)

    def test_acquisition_window_close_cleans_staging(self):
        self.check_running_exit('acquisition', False)

    def test_acquisition_quit_shortcut_cleans_staging(self):
        self.check_running_exit('acquisition', True)

    def test_home_actions_keep_readable_height_at_minimum_window(self):
        self.window.resize(880, 620)
        self.window.nav.setCurrentRow(0)
        self.app.processEvents()
        actions = {button.text(): button for button in self.window.stack.widget(0).findChildren(QPushButton)}
        for text in ('打开本地备份', '扫描本地镜像'):
            button = actions[text]
            self.assertGreaterEqual(button.height(), button.minimumSizeHint().height(), text)
            self.assertTrue(self.window.rect().contains(button.mapTo(self.window, button.rect().center())))

    def test_long_chinese_scan_path_remains_readable_at_minimum_window(self):
        directory = self.root
        for number in range(10):
            directory /= f'中文资料与相片{number}'
        directory.mkdir(parents=True)
        source = directory / '家庭照片只读来源文件.img'
        source.write_bytes(png_image())
        with patch('app.main_window.QFileDialog.getOpenFileName', return_value=(str(source), '')):
            self.window.choose_scan_source()
        self.window.resize(880, 620)
        self.window.nav.setCurrentRow(3)
        # QScrollArea may schedule another layout after wrapping the source.
        for _ in range(3):
            self.app.processEvents()
        label = self.window.scan_source_label
        self.assertEqual(label.text(), str(source))
        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
        self.assertEqual(label.toolTip(), str(source))
        self.assertGreaterEqual(self.window.scan_start_button.height(), self.window.scan_start_button.minimumSizeHint().height())
        self.assertEqual(source.read_bytes(), png_image())
