"""Actual PDF preview controls, lifecycle, source guards and evidence UI."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QFileDialog,QLabel
from app.app_config import AppConfig
from app.main_window import MainWindow
from core.scanner.signature.scanner import SignatureScanner
from core.tasks import CancellationToken,TaskCancelled
from storage.database.repository import Repository
from storage.models.source import DataSource
from tests.pdf_fixture import make_pdf


class PdfUiTests(unittest.TestCase):
    def setUp(self):
        self.app=QApplication.instance() or QApplication([])
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.config=AppConfig(self.root/'work');self.config.initialize()
        self.repo=Repository(self.config.database);self.window=MainWindow(self.config,self.repo);self.window.timer.stop()
        self.addCleanup(self.window.close)
        self.pdf=make_pdf();self.path=self.root/'synthetic.img';self.path.write_bytes(b'prefix'+self.pdf+b'gap')

    def scan(self):
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(self.path),'')) as picker:self.window.choose_scan_source()
        self.assertIn('*.pdf',picker.call_args.args[-1]);self.window.scan_start_button.click()
        job=self.window.pending[0];self.assertEqual(job.future.result(10),1);self.window.poll()
        return job.id

    def preview(self):
        self.window.result_table.selectRow(0);self.window.preview_selected()
        self.assertIsNotNone(self.window.pending)
        self.window.pending[0].future.result(15);self.window.poll()
        dialog=self.window.pdf_dialog;self.assertIsNotNone(dialog)
        return dialog

    def page_done(self,dialog):
        self.assertIsNotNone(dialog._pending)
        dialog._pending[0].result(15);dialog.poll();self.app.processEvents()

    def test_actual_scan_filter_pdf_paging_and_zoom_preserve_candidate_and_source(self):
        task=self.scan();self.window.category_filter.setCurrentIndex(self.window.category_filter.findData('document'))
        self.assertEqual(self.window.result_table.rowCount(),1)
        before=self.repo.candidates(task);original=self.path.read_bytes()
        dialog=self.preview();self.assertEqual(dialog.page_label.text(),'第 1 / 2 页')
        self.assertFalse(dialog.previous_button.isEnabled());self.assertTrue(dialog.next_button.isEnabled())
        first=dialog._image.pixelColor(100,500)
        dialog.next_button.click();self.page_done(dialog)
        self.assertEqual(dialog.page_label.text(),'第 2 / 2 页');self.assertNotEqual(dialog._image.pixelColor(100,500),first)
        dialog.zoom_combo.setCurrentIndex(dialog.zoom_combo.findData(50));self.page_done(dialog)
        self.assertEqual((dialog._image.width(),dialog._image.height()),(384,512))
        self.assertEqual(self.repo.candidates(task),before);self.assertEqual(self.path.read_bytes(),original)
        self.assertFalse(any(t['kind']=='pdf_preview' for t in self.repo.tasks()))

    def test_selection_navigation_and_new_task_close_pdf_and_release_owned_data(self):
        self.scan()
        for operation in ('selection','navigation','task'):
            with self.subTest(operation=operation):
                self.window.open_results(self.window.result_task_id);dialog=self.preview()
                if operation=='selection':self.window.result_table.clearSelection()
                elif operation=='navigation':self.window.nav.setCurrentRow(6)
                else:
                    self.window.run_task('synthetic-no-content',lambda token:None,lambda *_:None)
                    self.window.pending[0].future.result(10);self.window.poll()
                self.assertTrue(dialog._closed);self.assertEqual(dialog.data,b'');self.assertTrue(dialog._image.isNull())
                self.assertIsNone(self.window.pdf_dialog)

    def test_close_during_page_render_cancels_job_without_stale_callback(self):
        self.scan();dialog=self.preview();started=threading.Event()
        def slow(data,page,zoom,token):
            started.set()
            while not token._event.wait(0.01):pass
            token.checkpoint()
        with patch('ui.preview.pdf.render_pdf',side_effect=slow):
            dialog.next_button.click();future=dialog._pending[0];self.assertTrue(started.wait(2))
            dialog.close()
            self.assertIn('PDF 预览已关闭',self.window.preview_label.text())
            with self.assertRaises(TaskCancelled):future.result(2)
        self.assertTrue(dialog._closed);self.assertEqual(dialog.data,b'');self.assertIsNone(dialog._pending)
        self.assertTrue(dialog._image.isNull())

    def test_stop_clears_page_and_allows_retry_after_render_finishes(self):
        self.scan();dialog=self.preview();started=threading.Event()
        def slow(data,page,zoom,token):
            started.set()
            while not token._event.wait(0.01):pass
            token.checkpoint()
        with patch('ui.preview.pdf.render_pdf',side_effect=slow):
            dialog.next_button.click();self.assertTrue(started.wait(2));future=dialog._pending[0]
            dialog.stop_button.click()
            with self.assertRaises(TaskCancelled):future.result(2)
            dialog.poll()
        self.assertTrue(dialog._image.isNull());self.assertIn('已停止',dialog.status.text())
        self.assertTrue(dialog.next_button.isEnabled());dialog.next_button.click();self.page_done(dialog)
        self.assertEqual(dialog.page,1)

    def test_changing_selection_while_initial_preparation_runs_does_not_open_stale_pdf(self):
        self.scan();self.window.result_table.selectRow(0);self.window.preview_selected()
        job=self.window.pending[0];self.window.result_table.clearSelection()
        job.future.result(15);self.window.poll()
        self.assertIsNone(self.window.pdf_dialog)

    def test_evidence_and_history_remain_readonly_when_source_is_missing(self):
        task=self.scan();before=self.config.database.read_bytes();self.path.unlink()
        self.window.open_results(task);self.window.result_table.selectRow(0);self.window.show_candidate_evidence()
        detail=self.window.evidence_dialog.findChild(QLabel,'evidence-pdf-details')
        self.assertIsNotNone(detail);self.assertIn('2 页',detail.text());self.assertIn('未渲染全部页面',detail.text())
        self.assertEqual(self.config.database.read_bytes(),before)

    def test_pdf_failure_clears_canvas_without_showing_native_error_strings(self):
        self.scan();dialog=self.preview()
        with patch('ui.preview.pdf.render_pdf',side_effect=RuntimeError('private/native/path.pdf')):
            dialog.next_button.click()
            with self.assertRaises(RuntimeError):dialog._pending[0].result(10)
            dialog.poll()
        self.assertTrue(dialog._image.isNull());self.assertNotIn('private',dialog.status.text())

    def test_qt_authored_pdf_with_embedded_font_passes_real_rendering(self):
        from PySide6.QtCore import QByteArray,QBuffer,QIODevice
        from PySide6.QtGui import QPainter,QPdfWriter,QPagedPaintDevice,QFont
        from core.preview.pdf import inspect_pdf,render_pdf
        buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        writer=QPdfWriter(buffer);writer.setPdfVersion(QPagedPaintDevice.PdfVersion.PdfVersion_1_4)
        painter=QPainter(writer)
        # The platform UI font may be a virtual/protected system face on macOS.
        # Use an explicit portable family for a deterministic embedded-font PDF.
        painter.setFont(QFont('Arial',12));painter.drawText(100,400,'Original Qt-authored PDF')
        writer.newPage();painter.drawText(100,400,'A second static page');painter.end()
        data=bytes(buffer.data());buffer.close()
        details=inspect_pdf(data,CancellationToken());self.assertEqual(details['pages'],2)
        try:
            self.assertFalse(render_pdf(data,1,50,CancellationToken()).isNull())
        except Exception:
            import platform
            import sys
            folder=Path('build')/(f'macos-{platform.machine()}' if sys.platform=='darwin' else '')
            folder.mkdir(parents=True,exist_ok=True)
            (folder/'pdf-render-failure.pdf').write_bytes(data)
            raise

    def test_raw_pdf_preflight_failure_creates_no_preview_task_or_renderer(self):
        from core.computer.service import ComputerError
        from storage.models.candidate import Candidate
        source=DataSource('computer','synthetic-raw-device');task=self.repo.create_task('computer_scan',source)
        candidate=Candidate('computer_signature','candidate.pdf','pdf',len(self.pdf),source_id=source.id,source_stat={'kind':'device','size':4096},extents=((0,len(self.pdf)),),integrity='pdf_structure_checked',sha256='a'*64)
        self.repo.add_candidate(task,candidate);self.repo.update_task(task,'completed');self.window.open_results(task)
        before=self.repo.tasks();self.window.result_table.selectRow(0)
        with patch('app.main_window.check_computer_access',side_effect=ComputerError('源盘与工作目录未隔离')),patch('app.main_window.prepare_pdf') as renderer:
            self.window.preview_selected()
            with self.assertRaises(ComputerError):self.window.computer_page._future.result(5)
            self.window.poll();renderer.assert_not_called()
        self.assertIsNone(self.window.pending);self.assertEqual(self.repo.tasks(),before)

    def test_main_window_exit_closes_pdf(self):
        self.scan();dialog=self.preview();self.window.close()
        self.assertTrue(dialog._closed);self.assertEqual(dialog.data,b'')
