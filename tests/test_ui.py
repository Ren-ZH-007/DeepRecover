import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
import hashlib
import subprocess
import shlex
from threading import Event
from unittest.mock import patch
try:
    from PySide6.QtWidgets import QApplication
    from app.main_window import MainWindow
    QT_AVAILABLE=True
except ImportError:
    QT_AVAILABLE=False
from app.app_config import AppConfig
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource
from core.device.android.adapter import AndroidAdapter
from core.scanner.signature.scanner import SignatureScanner
from core.tasks import TaskCancelled
from tests.test_scanner import JPEG, png_image

@unittest.skipUnless(QT_AVAILABLE,'PySide6 not installed')
class UiTests(unittest.TestCase):
    def finish_task(self, window):
        self.assertIsNotNone(window.pending)
        window.pending[0].future.result(5)
        window.poll()

    def test_start_navigation_and_close(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize()
            window=MainWindow(config,Repository(config.database)); window.show()
            app.processEvents()
            for index in range(7):
                window.nav.setCurrentRow(index); app.processEvents()
                self.assertEqual(window.stack.currentIndex(),index)
            window.close(); app.processEvents()
            self.assertFalse(window.isVisible())

    def test_history_pagination_after_window_and_repository_reopen(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize()
            repo=Repository(config.database)
            task=repo.create_task('android_acquisition', DataSource('android','SERIAL','Phone'))
            repo.add_candidates(task, [Candidate('android_file',f'{i}.jpg','jpg',i) for i in range(205)])
            repo.update_task(task,'cancelled')
            window=MainWindow(config,repo)
            window.close()
            window=MainWindow(config,Repository(config.database))
            try:
                window.nav.setCurrentRow(5)
                window.history_table.selectRow(0)
                window.open_history_result()
                self.assertEqual(window.stack.currentIndex(),4)
                self.assertEqual(window.result_table.rowCount(),100)
                self.assertIn('已停止',window.result_source.text())
                self.assertIn('cancelled',window.result_source.toolTip())
                self.assertIn('SERIAL',window.result_source.text())
                self.assertFalse(window.previous_page.isEnabled())
                window.next_page.click(); app.processEvents()
                self.assertEqual(window.result_table.item(0,0).text(),'100.jpg')
                window.next_page.click(); app.processEvents()
                self.assertEqual(window.result_table.rowCount(),5)
                self.assertFalse(window.next_page.isEnabled())
                self.assertIn('/ 205',window.result_page_label.text())
                window.previous_page.click(); app.processEvents()
                self.assertEqual(window.result_table.item(0,0).text(),'100.jpg')
            finally:
                window.close()

    def test_android_ui_requires_selection_and_confirmation_then_copies_selected_only(self):
        from PySide6.QtWidgets import QMessageBox
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)/'data'); config.initialize()
            repo=Repository(config.database)
            original=Path(directory)/'source.bin'; original.write_bytes(b'existing ordinary file')
            before=hashlib.sha256(original.read_bytes()).hexdigest()
            calls=[]
            def runner(command,**kwargs):
                calls.append(command)
                output=''
                if command[1:] == ['devices','-l']:
                    output='SERIAL device model:Pixel\n'
                elif command[3] == 'shell' and command[4].startswith('find '):
                    output='/sdcard/DCIM/a.jpg\x00/sdcard/DCIM/b.jpg\x00'
                elif command[3] == 'shell' and command[4].startswith('readlink '):
                    output=shlex.split(command[4])[-1]+'\n'
                elif command[3] == 'pull':
                    Path(command[-1]).write_bytes(original.read_bytes())
                return subprocess.CompletedProcess(command,0,output,'')
            adapter=AndroidAdapter('adb',runner)
            window=MainWindow(config,repo)
            try:
                with patch('app.main_window.AndroidAdapter',return_value=adapter):
                    window.detect_android(); self.finish_task(window)
                    window.android_table.selectRow(0)
                    with patch('app.main_window.QInputDialog.getText',return_value=('/sdcard/DCIM',True)):
                        window.list_android_files(); self.finish_task(window)
                    window.collect_android()
                    self.assertIsNone(window.pending)
                    window.android_file_table.selectRow(1)
                    with patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.No):
                        window.collect_android()
                    self.assertIsNone(window.pending)
                    with patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
                        window.collect_android()
                    self.finish_task(window)
                pulls=[c for c in calls if len(c)>3 and c[3]=='pull']
                self.assertEqual([c[4] for c in pulls],['/sdcard/DCIM/b.jpg'])
                self.assertEqual(window.result_table.rowCount(),1)
                self.assertEqual(window.result_table.item(0,0).text(),'b.jpg')
                self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)
                self.assertEqual(window.result_table.item(0,6).text(),before)
                task=repo.task(window.result_task_id)
                self.assertEqual(repo.source(task['source_id'])['locator'],'SERIAL')
            finally:
                window.close()

    def test_empty_and_legacy_history_readback(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize()
            repo=Repository(config.database)
            empty=repo.create_task('android_discovery')
            legacy=repo.create_task('ios_manifest')
            repo.add_candidate(legacy,Candidate('ios_backup','legacy.jpg','jpg',1))
            window=MainWindow(config,repo)
            try:
                window.open_results(empty)
                self.assertEqual(window.result_table.rowCount(),0)
                self.assertFalse(window.next_page.isEnabled())
                self.assertFalse(window.previous_page.isEnabled())
                window.open_results(legacy)
                self.assertEqual(window.result_table.item(0,0).text(),'legacy.jpg')
                self.assertIn('旧记录',window.result_source.text())
                self.assertEqual(window.result_table.item(0,5).text(),'')
            finally:
                window.close()

    def test_scan_ui_select_run_progress_offsets_and_reopen_history(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.img'
            image=b'prefix'+png_image()+b'gap'+JPEG
            source.write_bytes(image)
            config=AppConfig(Path(directory)/'data'); config.initialize()
            repo=Repository(config.database)
            window=MainWindow(config,repo)
            try:
                self.assertFalse(window.scan_start_button.isEnabled())
                with patch('app.main_window.QFileDialog.getOpenFileName',return_value=(str(source),'')):
                    window.choose_scan_source()
                self.assertTrue(window.scan_start_button.isEnabled())
                window.scan_start_button.click()
                self.finish_task(window)
                self.assertEqual(window.scan_progress.value(),1000)
                self.assertIn('已完成',window.scan_status.text())
                self.assertIn('completed',window.scan_status.toolTip())
                self.assertEqual(window.result_table.rowCount(),2)
                self.assertEqual(window.result_table.item(0,8).text(),'6')
                self.assertEqual(window.result_table.item(0,9).text(),'候选')
                self.assertIn('candidate',window.result_table.item(0,9).toolTip())
                self.assertEqual(window.result_table.item(0,5).text(),'')
                task=window.result_task_id
                self.assertEqual(repo.source(repo.task(task)['source_id'])['kind'],'image')
            finally:
                window.close()
            source.unlink()  # Historical readback must not access the source.
            window=MainWindow(config,Repository(config.database))
            try:
                window.nav.setCurrentRow(5)
                window.history_table.selectRow(0)
                window.open_history_result()
                self.assertEqual(window.result_task_id,task)
                self.assertEqual(window.result_table.rowCount(),2)
                self.assertEqual(window.result_table.item(1,8).text(),str(6+len(png_image())+3))
            finally:
                window.close()

    def test_scan_ui_cancel_preserves_partial_history_and_incomplete_progress(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.img'; source.write_bytes(png_image()+JPEG)
            config=AppConfig(Path(directory)/'data'); config.initialize()
            repo=Repository(config.database)
            window=MainWindow(config,repo)
            scanner=SignatureScanner()
            ready,release=Event(),Event()
            def cancelled_scan(path,token):
                iterator=scanner.scan(path,token)
                try:
                    yield next(iterator)
                    ready.set()
                    if not release.wait(5):
                        raise RuntimeError('UI did not release scanner')
                    token.checkpoint()
                finally:
                    iterator.close()
            try:
                window.scan_source=source
                with patch('app.main_window.SignatureScanner') as factory:
                    factory.return_value.scan.side_effect=cancelled_scan
                    window.start_scan()
                    task=window.pending[0].id
                    self.assertTrue(ready.wait(5))
                    window.cancel_scan()
                    release.set()
                    with self.assertRaises(TaskCancelled): window.pending[0].future.result(5)
                    window.poll()
                self.assertLess(window.scan_progress.value(),1000)
                self.assertIn('已停止',window.scan_status.text())
                self.assertIn('cancelled',window.scan_status.toolTip())
                self.assertEqual(repo.candidate_count(task),1)
                window.nav.setCurrentRow(5)
                window.history_table.selectRow(0)
                window.open_history_result()
                self.assertIn('已停止',window.result_source.text())
                self.assertIn('cancelled',window.result_source.toolTip())
                self.assertEqual(window.result_table.rowCount(),1)
            finally:
                release.set()
                window.close()

    def test_scan_ui_rejects_working_database(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize()
            window=MainWindow(config,Repository(config.database))
            try:
                with patch('app.main_window.QFileDialog.getOpenFileName',return_value=(str(config.database),'')):
                    window.choose_scan_source()
                self.assertIsNone(window.scan_source)
                self.assertFalse(window.scan_start_button.isEnabled())
            finally:
                window.close()
