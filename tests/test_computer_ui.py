"""Computer UI orchestration with synthetic sources; no real disk is opened."""
import hashlib
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import tempfile
from threading import Event, current_thread, main_thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
QT_AVAILABLE = importlib.util.find_spec('PySide6') is not None
if QT_AVAILABLE:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app.main_window import MainWindow

from app.app_config import AppConfig
from core.tasks import TaskCancelled
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')
class ComputerUiTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        temporary = tempfile.TemporaryDirectory(prefix='computer-ui-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = AppConfig(self.root / 'work'); self.config.initialize()
        self.repo = Repository(self.config.database)
        self.window = MainWindow(self.config, self.repo)
        self.window.show(); self.app.processEvents()
        self.page = self.window.computer_page
        self.release = Event()
        self.addCleanup(self.close_window)
        self.image = self.root / '中文 来源.img'; self.image.write_bytes(b'original image bytes')

    def close_window(self):
        self.release.set()
        self.page.cancel()
        if self.page._future:
            try: self.page._future.result(5)
            except Exception: pass
            self.window.poll()
        if self.window.pending:
            self.window.pending[0].cancel()
            try: self.window.pending[0].future.result(5)
            except TaskCancelled: pass
            self.window.poll()
        self.window.close(); self.app.processEvents()

    def partition(self, file_system='NTFS'):
        return SimpleNamespace(offset=0, size=self.image.stat().st_size,
                               file_system=file_system, label='合成分区')

    def prepare_source(self, kind='image', file_system='NTFS'):
        self.page._set_source(str(self.image) if kind == 'image' else '/dev/disk99', kind)
        self.page.partitions = [self.partition(file_system)]
        self.page.partition_combo.addItem('合成分区')
        self.page.partition_changed()

    def finish_probe(self):
        self.assertIsNotNone(self.page._future)
        self.page._future.result(5)
        self.window.poll(); self.app.processEvents()

    def finish_task(self, error=None):
        if self.window.pending is None and error is None:
            # A fast worker can finish in the same poll as its preflight.
            task_id = self.page.last_task_id
            self.assertIsNotNone(task_id)
            self.assertEqual(self.repo.task(task_id)['status'], 'completed')
            return task_id
        self.assertIsNotNone(self.window.pending)
        task = self.window.pending[0]
        if error:
            with self.assertRaises(error): task.future.result(5)
        else: task.future.result(5)
        self.window.poll(); self.app.processEvents()
        return task.id

    def candidate(self):
        return Candidate('computer_deleted', '测试文件.txt', 'txt', self.image.stat().st_size,
                         offset=0, integrity='metadata_checked', file_system='NTFS',
                         extents=((0, self.image.stat().st_size),),
                         deletion_state='deleted_record', record_id=12,
                         sha256=hashlib.sha256(self.image.read_bytes()).hexdigest())

    def raw_history(self):
        source = DataSource('computer', '/dev/disk99', 'NTFS')
        task = self.repo.create_task('computer_scan', source)
        candidate = replace(self.candidate(), source_id=source.id,
            source_stat={'kind':'device'}, file_type='png', name='测试图片.png')
        self.repo.add_candidate(task, candidate); self.repo.update_task(task, 'completed')
        self.window.open_results(task); self.window.result_table.selectRow(0)
        return task, candidate.id

    def test_eighth_page_and_home_entry_preserve_original_routes(self):
        self.assertEqual(self.window.stack.count(), 8)
        self.assertEqual(self.window.nav.count(), 8)
        self.assertEqual(self.window.nav.item(1).text(), 'Android 设备')
        self.assertEqual(self.window.nav.item(4).text(), '恢复结果')
        self.window.computer_home_button.click()
        self.assertEqual(self.window.stack.currentIndex(), 7)
        self.assertIs(self.window.stack.currentWidget(), self.page)

    def test_image_inspection_is_background_readonly_and_unknown_fs_uses_signatures(self):
        threads = []
        def inspect(path, kind, data_dir):
            threads.append(current_thread())
            self.assertEqual((path, kind, data_dir), (str(self.image), 'image', self.config.data_dir))
            return [self.partition('APFS')]
        with patch('ui.computer.page.QFileDialog.getOpenFileName', return_value=(str(self.image), '')), \
                patch('ui.computer.page.inspect_computer_source', side_effect=inspect):
            self.page.choose_image(); self.finish_probe()
        self.assertTrue(threads and all(thread is not main_thread() for thread in threads))
        self.assertEqual(self.repo.tasks(), [])
        self.assertEqual(self.page.mode_combo.currentData(), 'signature')
        self.assertIn('不解析已删除记录', self.page.capability_label.text())
        self.assertTrue(self.page.start_button.isEnabled())

    def test_program_database_logs_and_acquisitions_cannot_be_image_sources(self):
        log = self.config.data_dir / 'Logs' / 'source.img'; log.write_bytes(b'log')
        acquired = self.config.data_dir / 'Acquisitions' / 'source.img'; acquired.write_bytes(b'acquired')
        for source in (self.config.database, log, acquired):
            with self.subTest(source=source), \
                    patch('ui.computer.page.QFileDialog.getOpenFileName', return_value=(str(source), '')), \
                    patch('ui.computer.page.inspect_computer_source') as inspect:
                self.page.choose_image()
                self.assertIsNone(self.page.source_path)
                inspect.assert_not_called()
        self.assertEqual(self.repo.tasks(), [])

    def test_device_preflight_failure_never_creates_database_task(self):
        device = SimpleNamespace(path='/dev/disk99', size=4096, label='合成设备', system_disk=False)
        self.page.devices = [device]
        self.window.fill(self.page.device_table, [(device.path, device.size, device.label)])
        self.page.device_table.selectRow(0)
        with patch('ui.computer.page.inspect_computer_source', side_effect=RuntimeError('工作目录位于源盘')):
            self.page.choose_device()
            with self.assertRaises(RuntimeError): self.page._future.result(5)
            self.window.poll()
        self.assertIn('工作目录位于源盘', self.page.status.text())
        self.assertEqual(self.repo.tasks(), [])
        self.assertFalse(self.page.start_button.isEnabled())

    def test_scan_confirmation_defaults_to_no_and_does_not_open_source(self):
        self.prepare_source()
        with patch('ui.computer.page.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as question, \
                patch('ui.computer.page.guard_computer_source') as guard, \
                patch('ui.computer.page.scan_computer_source') as scan:
            self.page.start_scan()
        self.assertEqual(question.call_args.args[-1], QMessageBox.StandardButton.No)
        guard.assert_not_called(); scan.assert_not_called()
        self.assertEqual(self.repo.tasks(), [])

    def test_scan_rechecks_disk_guard_before_task_and_binds_results(self):
        self.prepare_source(kind='device')
        order = []
        def guard(*args):
            self.assertEqual(args, ('/dev/disk99', 'device', self.config.data_dir))
            self.assertEqual(self.repo.tasks(), [])
            order.append('guard')
        def scan(path, token, **kwargs):
            order.append('scan')
            self.assertEqual(len(self.repo.tasks()), 1)
            self.assertEqual(path, '/dev/disk99')
            self.assertEqual(kwargs, {'kind':'device','mode':'deleted','partition_offset':0,'data_dir':self.config.data_dir})
            token.report_progress(1, 1, 1, phase='computer')
            yield self.candidate()
        with patch('ui.computer.page.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
                patch('ui.computer.page.guard_computer_source', side_effect=guard), \
                patch('ui.computer.page.scan_computer_source', side_effect=scan):
            self.page.start_scan(); self.finish_probe(); task_id = self.finish_task()
        self.assertEqual(order, ['guard', 'scan'])
        source = self.repo.source(self.repo.task(task_id)['source_id'])
        self.assertEqual((source['kind'], source['locator']), ('computer', '/dev/disk99'))
        candidate = self.repo.candidate_page(task_id)[0]
        self.assertEqual(candidate['source_id'], source['id'])
        self.assertEqual(self.window.stack.currentIndex(), 4)
        self.assertEqual(self.window.result_table.item(0,10).text(), 'NTFS')
        self.assertEqual(self.window.result_table.item(0,11).text(), '文件系统删除记录')
        self.assertEqual(self.window.result_table.item(0,12).text(), '1')
        self.assertEqual(self.page.progress.value(), 1000)

    def test_cancelled_preflight_blocks_other_tasks_and_close_until_system_call_returns(self):
        started = Event()
        def inspect(*args):
            started.set()
            if not self.release.wait(5): raise RuntimeError('preflight gate not released')
            return [self.partition()]
        with patch('ui.computer.page.QFileDialog.getOpenFileName', return_value=(str(self.image), '')), \
                patch('ui.computer.page.inspect_computer_source', side_effect=inspect):
            self.page.choose_image(); self.assertTrue(started.wait(5))
            self.window.run_task('forbidden-during-preflight', lambda token: None, lambda *_: None)
            self.assertIsNone(self.window.pending)
            self.assertFalse(self.window.close())
            self.assertTrue(self.window.isVisible())
            self.assertEqual(self.repo.tasks(), [])
            self.release.set(); self.finish_probe()
        self.assertEqual(self.repo.tasks(), [])
        self.assertIn('已停止', self.page.status.text())
        self.assertEqual(self.page.partitions, [])

    def test_cancelled_computer_scan_preserves_committed_history_and_source(self):
        self.prepare_source()
        started = Event(); before = self.image.read_bytes()
        def scan(path, token, **kwargs):
            yield self.candidate()
            started.set()
            if not self.release.wait(5): raise RuntimeError('scan gate not released')
            token.checkpoint()
        with patch('ui.computer.page.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
                patch('ui.computer.page.guard_computer_source'), \
                patch('ui.computer.page.scan_computer_source', side_effect=scan):
            self.page.start_scan(); self.finish_probe()
            self.assertTrue(started.wait(5))
            self.page.cancel(); self.release.set(); task_id = self.finish_task(TaskCancelled)
        self.assertEqual(self.repo.task(task_id)['status'], 'cancelled')
        self.assertEqual(self.repo.candidate_count(task_id), 1)
        self.assertTrue(self.page.results_button.isEnabled())
        self.page.results_button.click()
        self.assertEqual(self.window.result_table.rowCount(), 1)
        self.assertIn('cancelled', self.window.result_source.text())
        self.assertEqual(self.image.read_bytes(), before)

    def test_raw_history_preview_guard_failure_does_not_create_task(self):
        _task, candidate_id = self.raw_history(); before = len(self.repo.tasks())
        with patch('app.main_window.check_computer_access', side_effect=RuntimeError('工作目录已位于源盘')) as access, \
                patch('app.main_window.preview_image') as preview:
            self.window.preview_selected()
            self.assertIsNone(self.window.pending)
            with self.assertRaises(RuntimeError): self.page._future.result(5)
            self.window.poll()
        self.assertEqual(len(self.repo.tasks()), before)
        self.assertIn('未开始预览', self.window.preview_label.text())
        preview.assert_not_called()
        access.assert_called_once_with(self.repo, (candidate_id,), self.config.data_dir)

    def test_raw_history_export_guard_failure_does_not_create_task_or_directory(self):
        _task, candidate_id = self.raw_history(); before = len(self.repo.tasks())
        destination = self.root / 'blocked-export'
        with patch('app.main_window.QFileDialog.getExistingDirectory', return_value=str(destination)), \
                patch('app.main_window.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
                patch('app.main_window.check_computer_access', side_effect=RuntimeError('导出目标位于源盘')) as access, \
                patch('app.main_window.export_batch') as export:
            self.window.export_results(False)
            self.assertIsNone(self.window.pending)
            with self.assertRaises(RuntimeError): self.page._future.result(5)
            self.window.poll()
        self.assertEqual(len(self.repo.tasks()), before)
        self.assertFalse(destination.exists())
        self.assertIn('未开始导出', self.window.export_status.text())
        export.assert_not_called()
        access.assert_called_once_with(self.repo, (candidate_id,), self.config.data_dir, str(destination))

    def test_raw_history_preview_starts_only_after_successful_guard(self):
        from PySide6.QtGui import QImage
        from contextlib import closing
        def task_count():
            with closing(self.repo.connect()) as db:
                return db.execute('SELECT count(*) FROM tasks').fetchone()[0]
        _task, candidate_id = self.raw_history(); before = task_count(); order = []
        def access(*args):
            self.assertEqual(task_count(), before)
            order.append('guard')
        def preview(*args):
            self.assertEqual(task_count(), before + 1)
            order.append('preview')
            image = QImage(2, 2, QImage.Format.Format_RGB32); image.fill(0xff123456)
            return image
        with patch('app.main_window.check_computer_access', side_effect=access), \
                patch('app.main_window.preview_image', side_effect=preview):
            self.window.preview_selected(); self.finish_probe()
            if self.window.pending:
                self.window.pending[0].future.result(5); self.window.poll()
        self.assertEqual(order, ['guard', 'preview'])
        self.assertEqual(task_count(), before + 1)
        self.assertFalse(self.window.preview_label.pixmap().isNull())


if __name__ == '__main__':
    unittest.main()
