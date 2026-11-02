import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
import hashlib
import subprocess
import shlex
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
                self.assertIn('cancelled',window.result_source.text())
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
