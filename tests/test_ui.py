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

    def test_result_search_filters_pages_export_selection_and_resets_on_new_results(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize()
            repo=Repository(config.database)
            task=repo.create_task('android_acquisition', DataSource('android','SERIAL','Phone'))
            repo.add_candidates(task,[Candidate('android_file',f'IMG_{i}.jpg','jpg',1,original_path=f'/sdcard/DCIM/IMG_{i}.jpg') for i in range(3)]
                                +[Candidate('android_file','report.pdf','pdf',1,original_path='/sdcard/Download/report.pdf')])
            repo.update_task(task,'completed')
            other=repo.create_task('android_acquisition', DataSource('android','SERIAL','Phone'))
            repo.add_candidates(other,[Candidate('android_file','a.jpg','jpg',1)]); repo.update_task(other,'completed')
            window=MainWindow(config,repo)
            try:
                window.open_results(task)
                self.assertEqual(window.result_table.rowCount(),4)
                window.result_search.setText('img  2')
                self.assertTrue(window.search_timer.isActive())
                self.assertEqual(window.result_table.rowCount(),4)
                window.result_search.returnPressed.emit()
                self.assertFalse(window.search_timer.isActive())
                self.assertEqual([window.result_table.item(r,0).text() for r in range(window.result_table.rowCount())],['IMG_2.jpg'])
                window.result_search.setText('download'); window.search_timer.timeout.emit()
                self.assertEqual(window.result_table.item(0,0).text(),'report.pdf')
                with patch('app.main_window.QFileDialog.getExistingDirectory',return_value='') as chooser, \
                     patch.object(repo,'candidate_ids',wraps=repo.candidate_ids) as ids:
                    window.export_results(True)
                self.assertEqual(ids.call_args.kwargs['query'],'download'); chooser.assert_called_once()
                window.result_search.setText(' '.join('x'*20)); window.filter_results()
                self.assertEqual(window.search_text(),' '.join('x'*8)); self.assertEqual(window.result_table.rowCount(),0)
                window.open_results(other)
                self.assertEqual(window.result_search.text(),''); self.assertFalse(window.search_timer.isActive())
                self.assertEqual(window.result_table.rowCount(),1)
            finally:
                window.close()

    def test_duplicate_column_hide_filter_export_selection_and_reset(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize()
            repo=Repository(config.database)
            task=repo.create_task('signature_scan', DataSource('image',str(Path(directory)/'disk.img'),'disk.img'))
            same='d'*64
            repo.add_candidates(task,[Candidate('signature_scan','first.jpg','jpg',1,sha256=same),
                                      Candidate('signature_scan','unique.jpg','jpg',1,sha256='e'*64),
                                      Candidate('signature_scan','copy.jpg','jpg',1,sha256=same),
                                      Candidate('signature_scan','unverified.jpg','jpg',1)])
            repo.update_task(task,'completed')
            window=MainWindow(config,repo)
            try:
                window.open_results(task)
                column={window.result_table.item(r,0).text():window.result_table.item(r,13).text() for r in range(window.result_table.rowCount())}
                self.assertEqual(column,{'first.jpg':'共 2 份 · 最早一项','unique.jpg':'','copy.jpg':'共 2 份 · 重复','unverified.jpg':''})
                self.assertEqual(window.result_table.horizontalHeaderItem(13).text(),'相同内容')
                window.hide_duplicates.setChecked(True)
                self.assertEqual([window.result_table.item(r,0).text() for r in range(window.result_table.rowCount())],
                                 ['first.jpg','unique.jpg','unverified.jpg'])
                with patch('app.main_window.QFileDialog.getExistingDirectory',return_value='') as chooser, \
                     patch.object(repo,'candidate_ids',wraps=repo.candidate_ids) as ids:
                    window.export_results(True)
                self.assertTrue(ids.call_args.kwargs['hide_duplicates']); chooser.assert_called_once()
                window.open_results(task)
                self.assertFalse(window.hide_duplicates.isChecked()); self.assertEqual(window.result_table.rowCount(),4)
            finally:
                window.close()

    def test_modified_time_column_date_filter_export_selection_and_reset(self):
        from datetime import date
        from PySide6.QtCore import QDate
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize()
            repo=Repository(config.database)
            task=repo.create_task('computer_scan', DataSource('image',str(Path(directory)/'disk.img'),'disk.img'))
            repo.add_candidates(task,[Candidate('computer_deleted','may.jpg','jpg',1,file_system='FAT32',modified_at='2024-05-01T13:45:30',modified_basis='local'),
                                      Candidate('computer_deleted','june.jpg','jpg',1,file_system='FAT32',modified_at='2024-06-01T08:00:00',modified_basis='local'),
                                      Candidate('signature_scan','undated.jpg','jpg',1)])
            repo.update_task(task,'completed')
            window=MainWindow(config,repo)
            try:
                window.open_results(task)
                names=lambda: [window.result_table.item(r,0).text() for r in range(window.result_table.rowCount())]
                self.assertEqual(window.result_table.horizontalHeaderItem(14).text(),'修改时间')
                self.assertEqual(window.result_table.item(0,14).text(),'2024-05-01 13:45（本地，无时区）')
                self.assertIn('没有时区信息',window.result_table.item(0,14).toolTip())
                self.assertEqual(window.result_table.item(2,14).text(),'')
                self.assertFalse(window.date_from.isEnabled())
                window.date_from.setDate(QDate(2024,5,1)); window.date_to.setDate(QDate(2024,5,31))
                self.assertEqual(len(names()),3)  # not applied until enabled
                window.date_filter.setChecked(True)
                self.assertTrue(window.date_from.isEnabled()); self.assertEqual(names(),['may.jpg'])
                window.date_to.setDate(QDate(2024,6,30)); self.assertEqual(names(),['may.jpg','june.jpg'])
                window.date_from.setDate(QDate(2024,7,1))  # reversed range is swapped, not an error
                self.assertEqual(window.date_range(),(date(2024,6,30),date(2024,7,1))); self.assertEqual(names(),[])
                window.date_from.setDate(QDate(2024,5,1))
                with patch('app.main_window.QFileDialog.getExistingDirectory',return_value='') as chooser, \
                     patch.object(repo,'candidate_ids',wraps=repo.candidate_ids) as ids:
                    window.export_results(True)
                self.assertEqual(ids.call_args.kwargs['dates'],(date(2024,5,1),date(2024,6,30))); chooser.assert_called_once()
                window.open_results(task)
                self.assertFalse(window.date_filter.isChecked()); self.assertFalse(window.date_to.isEnabled())
                self.assertEqual(window.date_to.date(),QDate.currentDate()); self.assertEqual(len(names()),3)
            finally:
                window.close()

    def test_history_resume_button_continues_signature_scan_and_refuses_changed_source(self):
        from tests.test_resume import interrupt_after, source_bytes, summary
        from PySide6.QtWidgets import QMessageBox
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'disk.img'; path.write_bytes(source_bytes(600_000))
            config=AppConfig(Path(directory)/'data'); config.initialize()
            repo=Repository(config.database)
            window=MainWindow(config,repo)
            def run(limit=None):
                scan=lambda token: SignatureScanner(block_size=4096).scan(path,token,checkpoint_every=0)
                handle=window.manager.submit('signature_scan',(lambda token: interrupt_after(scan(token),token,limit)) if limit else scan,
                                             persist_candidates=True,source=DataSource('image',str(path),path.name),batch_size=1)
                try: handle.future.result(30)
                except TaskCancelled: pass
                return handle.id
            def select(task_id):
                window.nav.setCurrentRow(5); window.refresh_history()
                window.history_table.selectRow([t['id'] for t in window.history].index(task_id)); app.processEvents()
            def settle():
                if window.computer_page._future is not None:
                    window.computer_page._future.result(30); window.poll()
                if window.pending is not None:
                    try: window.pending[0].future.result(60)
                    except Exception: pass
                    window.poll()
            try:
                reference=summary(repo.candidates(run()))
                task=run(4)
                select(next(t['id'] for t in repo.tasks() if t['status']=='completed'))
                self.assertFalse(window.resume_button.isEnabled())
                select(task); self.assertTrue(window.resume_button.isEnabled())
                with patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.No):
                    window.resume_history_task()
                self.assertIsNone(window.computer_page._future); self.assertEqual(repo.task(task)['status'],'cancelled')
                with patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes) as asked:
                    window.resume_history_task(); settle()
                self.assertIn('已保存',asked.call_args.args[2])
                self.assertEqual(window.stack.currentIndex(),4)  # results opened after completion
                self.assertEqual(repo.task(task)['status'],'completed')
                self.assertEqual(summary(repo.candidates(task)),reference)
                self.assertEqual(window.result_task_id,task)
                changed=run(3)
                with path.open('ab') as stream: stream.write(b'!')
                select(changed)
                with patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
                    window.resume_history_task(); settle()
                self.assertIn('源文件已变化',window.statusBar().currentMessage())
                self.assertEqual(repo.task(changed)['status'],'cancelled')
                self.assertIsNone(window.pending)
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

    def test_scan_ui_shows_speed_and_remaining_time_then_clears_it(self):
        from core.progress_rate import RateEstimator
        app=QApplication.instance() or QApplication([])
        MB=1024*1024
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.img'; source.write_bytes(b'x')
            config=AppConfig(Path(directory)/'data'); config.initialize()
            window=MainWindow(config,Repository(config.database))
            steps=[Event() for _ in range(2)]; release=[Event() for _ in range(2)]
            def paced_scan(path,token):
                for index,done in enumerate((0,20*MB)):
                    token.report_progress(done,100*MB,0)
                    steps[index].set()
                    if not release[index].wait(5): raise RuntimeError('UI did not release scanner')
                return iter(())
            class Clock:
                now=100.0
                def __call__(self): return self.now
            clock=Clock()
            try:
                window.scan_source=source
                with patch('app.main_window.SignatureScanner') as factory:
                    factory.return_value.scan.side_effect=paced_scan
                    window.start_scan()
                    window.rate=RateEstimator(clock=clock)
                    self.assertTrue(steps[0].wait(5)); window.poll()
                    self.assertIn('0 B / 100 MB',window.scan_status.text())
                    self.assertIn('正在估算剩余时间',window.scan_status.text())
                    release[0].set(); self.assertTrue(steps[1].wait(5))
                    clock.now=102.0; window.poll()
                    self.assertIn('20.0 MB / 100 MB',window.scan_status.text())
                    self.assertIn('速度 10.0 MB/s · 预计剩余约 8 秒',window.scan_status.text())
                    release[1].set(); self.finish_task(window)
                self.assertIn('已完成',window.scan_status.text())
                self.assertNotIn('预计剩余',window.scan_status.text())
                self.assertNotIn('正在估算',window.scan_status.text())
            finally:
                for event in release: event.set()
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
