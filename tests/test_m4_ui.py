import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from threading import Event

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
try:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app.main_window import MainWindow
    from core.preview.images import preview_image
    QT_AVAILABLE=True
except ImportError:
    QT_AVAILABLE=False
from app.app_config import AppConfig
from core.candidate_io import snapshot, CandidateAccessError
from core.tasks import CancellationToken, TaskCancelled
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource
from tests.test_scanner import png_image, JPEG


@unittest.skipUnless(QT_AVAILABLE,'PySide6 not installed')
class M4UiTests(unittest.TestCase):
    def setUp(self):
        self.app=QApplication.instance() or QApplication([])
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=AppConfig(self.root/'data'); self.config.initialize()
        self.repo=Repository(self.config.database)
        self.window=MainWindow(self.config,self.repo)
        self.addCleanup(self.window.close)

    def android(self,data=JPEG,file_type='jpg'):
        path=self.config.data_dir/'Acquisitions'/uuid4().hex; path.write_bytes(data)
        source=DataSource('android','SERIAL','Phone')
        task=self.repo.create_task('android_acquisition',source)
        candidate=Candidate('android_file','photo.'+file_type,file_type,len(data),local_path=str(path),source_id=source.id,
            sha256=hashlib.sha256(data).hexdigest(),source_stat=snapshot(path.stat()),integrity='not_checked')
        self.repo.add_candidate(task,candidate); self.repo.update_task(task,'completed')
        return task,candidate,path

    def finish(self):
        self.assertIsNotNone(self.window.pending)
        task=self.window.pending[0].id
        self.window.pending[0].future.result(10)
        self.window.poll()
        return task

    def test_scan_preview_export_history_end_to_end_without_source_writes(self):
        source=self.root/'source.img'; data=b'prefix'+png_image()+b'gap'+JPEG; source.write_bytes(data)
        with patch('app.main_window.QFileDialog.getOpenFileName',return_value=(str(source),'')):
            self.window.choose_scan_source()
        self.window.start_scan(); source_task=self.finish()
        self.window.result_table.selectRow(0)
        self.window.preview_selected(); self.finish()
        self.assertFalse(self.window.preview_label.pixmap().isNull())
        with patch('app.main_window.QFileDialog.getExistingDirectory',return_value=str(self.root/'output')),patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
            self.window.export_results(True)
        exported=self.finish()
        self.assertEqual(self.window.export_progress.value(),1000)
        self.assertEqual(self.window.export_dialog.table.rowCount(),2)
        self.assertEqual([Path(r['output_path']).read_bytes() for r in self.repo.exports(exported)],[png_image(),JPEG])
        self.assertTrue(all(c['status']=='candidate' for c in self.repo.candidate_page(source_task)))
        self.assertEqual(source.read_bytes(),data)
        self.window.close()
        self.window=MainWindow(self.config,Repository(self.config.database)); self.addCleanup(self.window.close)
        self.window.open_results(exported)
        self.assertEqual(self.window.export_dialog.table.rowCount(),2)
        for row in range(2):
            self.assertEqual(self.window.export_dialog.table.item(row,3).text(),self.window.export_dialog.table.item(row,4).text())

    def test_filter_resets_pagination_and_export_uses_all_filtered_ids(self):
        task,candidate,_=self.android()
        self.repo.add_candidates(task,[Candidate('test',str(i),'png',1,status='partial' if i<5 else 'candidate') for i in range(205)])
        self.repo.add_candidate(task,Candidate('test','notes.pdf','pdf',5))
        self.window.open_results(task)
        self.window.next_page.click()
        self.assertEqual(self.window.result_offset,100)
        self.window.category_filter.setCurrentIndex(self.window.category_filter.findData('image'))
        self.assertEqual(self.window.result_offset,0)
        self.assertIn('/ 206',self.window.result_page_label.text())
        self.window.status_filter.setCurrentIndex(self.window.status_filter.findData('partial'))
        self.assertEqual(self.window.result_table.rowCount(),5)
        self.assertFalse(self.window.next_page.isEnabled())
        self.window.category_filter.setCurrentIndex(self.window.category_filter.findData('document'))
        self.assertEqual(self.window.result_table.rowCount(),0)

    def test_preview_failure_clears_previous_image_and_does_not_change_candidate(self):
        task,candidate,_=self.android()
        self.window.open_results(task); self.window.result_table.selectRow(0)
        self.window.preview_selected(); self.finish()
        self.assertFalse(self.window.preview_label.pixmap().isNull())
        bad_task,bad,_=self.android(b'not a picture',file_type='png')
        self.window.open_results(bad_task); self.window.result_table.selectRow(0)
        self.window.preview_selected()
        with self.assertRaises(CandidateAccessError): self.window.pending[0].future.result(5)
        self.window.poll()
        self.assertTrue(self.window.preview_label.pixmap() is None or self.window.preview_label.pixmap().isNull())
        self.assertIn('JPG/PNG',self.window.preview_label.text())
        self.assertEqual(self.repo.candidate(bad.id)['status'],'candidate')

    def test_preview_uses_content_format_and_enforces_pixel_limit(self):
        _,candidate,_=self.android(JPEG,file_type='txt')
        image=preview_image(self.repo,candidate.id,self.config.data_dir,CancellationToken())
        self.assertEqual((image.width(),image.height()),(2,2))
        _,large,_=self.android(png_image(width=10000,height=10000),file_type='png')
        with self.assertRaises(CandidateAccessError): preview_image(self.repo,large.id,self.config.data_dir,CancellationToken())

    def test_export_requires_selection_and_confirmation(self):
        task,candidate,_=self.android()
        self.window.open_results(task)
        self.window.export_results(False); self.assertIsNone(self.window.pending)
        self.window.result_table.selectRow(0)
        with patch('app.main_window.QFileDialog.getExistingDirectory',return_value=str(self.root/'out')),patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.No):
            self.window.export_results(False)
        self.assertIsNone(self.window.pending)
        self.assertFalse((self.root/'out').exists())

    def test_export_history_pages_and_interrupted_rows(self):
        _,candidate,_=self.android()
        task=self.repo.create_task('export')
        for i in range(105):
            record=self.repo.start_export(task,candidate.id,self.root/f'{i}.jpg',{'name':candidate.name,'size':candidate.size})
            self.repo.finish_export(record,'exported',source_sha256=candidate.sha256,target_sha256=candidate.sha256)
        self.repo.update_task(task,'completed')
        self.window.show_export_history(task)
        dialog=self.window.export_dialog
        self.assertEqual(dialog.table.rowCount(),100)
        dialog.next.click(); self.app.processEvents()
        self.assertEqual(dialog.table.rowCount(),5)
        self.assertFalse(dialog.next.isEnabled())
        dialog.previous.click(); self.assertEqual(dialog.table.rowCount(),100)

    def test_export_all_filtered_spans_pages_and_excludes_other_types(self):
        task,first,_=self.android(JPEG)
        _,second,_=self.android(png_image(),file_type='png')
        self.repo.add_candidate(task,replace(second,id=str(uuid4()),source_id=first.source_id))
        self.repo.add_candidate(task,replace(first,id=str(uuid4()),file_type='pdf'))
        self.window.page_size=1
        self.window.open_results(task)
        self.window.category_filter.setCurrentIndex(self.window.category_filter.findData('image'))
        self.assertEqual(self.window.result_table.rowCount(),1)
        with patch('app.main_window.QFileDialog.getExistingDirectory',return_value=str(self.root/'out')),patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
            self.window.export_results(True)
        export_task=self.finish()
        self.assertEqual([Path(r['output_path']).read_bytes() for r in self.repo.exports(export_task)],[JPEG,png_image()])

    def test_stop_export_marks_audit_cancelled_and_keeps_source_unchanged(self):
        task,candidate,path=self.android(JPEG)
        self.window.open_results(task); self.window.result_table.selectRow(0)
        ready,release=Event(),Event()
        from core.export.service import copy_verified
        def waiting(item,target,token):
            ready.set()
            if not release.wait(5): raise RuntimeError('export not released')
            return copy_verified(item,target,token)
        try:
            with patch('core.export.service.copy_verified',side_effect=waiting),patch('app.main_window.QFileDialog.getExistingDirectory',return_value=str(self.root/'out')),patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
                self.window.export_results(False)
                export_task=self.window.pending[0].id
                self.assertTrue(ready.wait(5))
                self.window.cancel_export(); release.set()
                with self.assertRaises(TaskCancelled): self.window.pending[0].future.result(5)
                self.window.poll()
            self.assertEqual(self.repo.task(export_task)['status'],'cancelled')
            self.assertEqual(self.window.export_dialog.table.item(0,1).text(),'cancelled')
            self.assertEqual(path.read_bytes(),JPEG)
        finally:
            release.set()
