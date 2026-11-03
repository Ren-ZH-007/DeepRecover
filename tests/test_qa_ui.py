import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage
    from app.main_window import MainWindow
    QT_AVAILABLE=True
except ImportError:
    QT_AVAILABLE=False
from app.app_config import AppConfig
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from core.export.service import ExportError


@unittest.skipUnless(QT_AVAILABLE,'PySide6 not installed')
class DetailedQaUiTests(unittest.TestCase):
    def test_rejected_export_has_terminal_status_instead_of_running_message(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize(); repo=Repository(config.database)
            window=MainWindow(config,repo)
            def reject(token): raise ExportError('QA 准备阶段拒绝')
            try:
                window.export_status.setText('正在导出并核对来源与目标摘要…')
                with self.assertLogs('deeprecover',level='WARNING'):
                    window.run_task('export',reject,lambda *_: None)
                    with self.assertRaises(ExportError): window.pending[0].future.result(3)
                window.poll(); app.processEvents()
                self.assertIsNone(window.pending)
                self.assertNotIn('正在',window.export_status.text())
                self.assertIn('导出失败',window.export_status.text())
                self.assertIn('尚未生成导出文件',window.export_status.text())
            finally: window.close()

    def test_late_preview_cannot_overwrite_new_selection(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize(); repo=Repository(config.database)
            task=repo.create_task('test'); repo.add_candidates(task,[Candidate('test',str(i),'jpg',1) for i in range(2)]); repo.update_task(task,'completed')
            window=MainWindow(config,repo); window.open_results(task); window.result_table.selectRow(0)
            ready,release=Event(),Event()
            def preview(*args):
                ready.set()
                if not release.wait(3): raise RuntimeError('preview gate not released')
                image=QImage(2,2,QImage.Format.Format_RGB32); image.fill(0xff336699); return image
            try:
                with patch('app.main_window.preview_image',side_effect=preview):
                    window.preview_selected(); self.assertTrue(ready.wait(3))
                    window.result_table.selectRow(1); release.set()
                    window.pending[0].future.result(3); window.poll(); app.processEvents()
                self.assertTrue(window.preview_label.pixmap() is None or window.preview_label.pixmap().isNull())
                self.assertIn('选择',window.preview_label.text())
            finally:
                release.set()
                if window.pending: window.pending[0].future.result(3); window.poll()
                window.close()
