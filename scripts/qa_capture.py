"""Reproducible synthetic Qt workflows and unedited widget screenshots.

Run from the repository root with QT_QPA_PLATFORM=offscreen or native cocoa. Android transport
and modal user choices are injected; acquisition, indexing, scanning, preview,
export, SQLite, cancellation, hashes and Qt widgets execute normally.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import plistlib
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from time import monotonic
from threading import Event
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
from PySide6.QtTest import QTest
from app.app_config import AppConfig
from app.main_window import MainWindow
from core.candidate_io import CandidateAccessError
from core.device.android.adapter import AndroidAdapter
from core.export.service import ExportError
from core.parser.ios_backup.manifest import BackupError
from core.tasks import TaskCancelled
from storage.database.repository import Repository
from tests.test_scanner import JPEG, png_image


def hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([])
    cases=[]; screenshots=[]
    with tempfile.TemporaryDirectory(prefix='DeepRecover-QA-') as directory:
        root=Path(directory).resolve(); config=AppConfig(root/'work'); config.initialize()
        repo=Repository(config.database); window=MainWindow(config,repo); window.show()
        def capture(name,widget=None):
            app.processEvents()
            target=output/name
            assert (widget or window).grab().save(str(target))
            screenshots.append(name)
        def wait_for_layout(condition):
            # Word-wrapped labels inside QScrollArea settle through posted
            # layout requests. Observe the final geometry, not its first frame.
            deadline=monotonic()+2
            while True:
                app.processEvents()
                if condition(): return
                if monotonic() >= deadline:
                    raise AssertionError('Qt layout did not become readable within 2 seconds')
                QTest.qWait(10)
        def finish(error=None):
            handle=window.pending[0]
            if error:
                try: handle.future.result(15)
                except error: pass
                else: raise AssertionError('Expected '+error.__name__)
            else: handle.future.result(15)
            window.poll(); app.processEvents()
            return handle.id
        def backup(name,wal=False):
            path=root/name; path.mkdir()
            (path/'Manifest.plist').write_bytes(plistlib.dumps({'IsEncrypted':False}))
            (path/'Info.plist').write_bytes(plistlib.dumps({'Device Name':'QA Synthetic iPhone','Product Version':'QA'}))
            db=sqlite3.connect(path/'Manifest.db')
            if wal: db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('CREATE TABLE Files(fileID TEXT,domain TEXT,relativePath TEXT,flags INTEGER)')
            for char,payload in [('a',png),('b',JPEG)]:
                fid=char*40; folder=path/fid[:2]; folder.mkdir(); (folder/fid).write_bytes(payload)
                db.execute('INSERT INTO Files VALUES(?,?,?,1)',(fid,'CameraRollDomain','Media/QA-'+char+'.'+('png' if char=='a' else 'jpg')))
            db.commit(); return path,db
        def start_export(destination,all_filtered=True):
            with patch('app.main_window.QFileDialog.getExistingDirectory',return_value=str(destination)),patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
                window.export_results(all_filtered)
        try:
            png=png_image(width=256,height=128,raw=b''.join(b'\x00'+bytes(component for x in range(256) for component in (x,y*2,(x+y)%256)) for y in range(128)))
            phone=root/'synthetic-phone'; phone.mkdir()
            (phone/'QA-gradient.png').write_bytes(png); (phone/'QA-photo.jpg').write_bytes(JPEG)
            phone_before=hashes(phone)
            normal,db=backup('registered-backup',wal=True); db.close(); normal_before=hashes(normal)
            image=root/'synthetic.img'; padding=b'\x00'*(1024*1024-5)
            valid_pngs=b''.join(png+b'gap' for _ in range(101))
            image.write_bytes(padding+valid_pngs+JPEG+b'gap'+png[:-12]); image_before=hashlib.sha256(image.read_bytes()).hexdigest()
            capture('02-overview.png')
            allowed=[False]; calls=[]
            remote={'/sdcard/DCIM/'+p.name:p for p in phone.iterdir()}
            def runner(command,**kwargs):
                calls.append(command); stdout=''
                if command[1:]==['devices','-l']:
                    stdout='List of devices attached\nQA-SYNTHETIC '+('device model:QA_Phone' if allowed[0] else 'unauthorized')+'\n'
                elif command[3]=='shell':
                    if command[4].startswith('readlink '): stdout=shlex.split(command[4])[-1].replace('/sdcard','/storage/emulated/0')+'\n'
                    elif command[4].startswith('find '): stdout='\x00'.join(sorted(remote))+'\x00'
                elif command[3]=='pull': Path(command[-1]).write_bytes(remote[command[-2]].read_bytes())
                return subprocess.CompletedProcess(command,0,stdout,'')
            adapter=AndroidAdapter('QA-adb',runner)
            with patch('app.main_window.AndroidAdapter',return_value=adapter):
                window.nav.setCurrentRow(1); window.detect_android(); finish(); window.android_table.selectRow(0)
                with patch('app.main_window.QInputDialog.getText',side_effect=AssertionError('unauthorized device opened directory prompt')):
                    window.list_android_files()
                assert window.pending is None and all(c[1:]==['devices','-l'] for c in calls)
                capture('03-android-unauthorized.png')
                cases.append({'id':'QA-01','result':'passed','observed':'Unauthorized device made no shell/pull request'})
                allowed[0]=True; window.detect_android(); finish(); window.android_table.selectRow(0)
                with patch('app.main_window.QInputDialog.getText',return_value=('/sdcard/DCIM',True)):
                    window.list_android_files(); finish()
                window.android_file_table.selectAll(); capture('04-android-selected-files.png')
                with patch('app.main_window.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes): window.collect_android()
                android_task=finish()
            acquired=repo.candidate_page(android_task)
            assert len(acquired)==2 and hashes(phone)==phone_before
            assert all(hashlib.sha256(Path(c['local_path']).read_bytes()).hexdigest()==c['sha256'] for c in acquired)
            cases.append({'id':'QA-02','result':'passed','copied':2,'source_phone_hashes_unchanged':True})
            window.detect_backups([str(normal)]); finish(); window.backup_table.selectRow(0); window.parse_backup(); ios_task=finish()
            assert repo.candidate_count(ios_task)==2 and hashes(normal)==normal_before
            capture('05-ios-index-readonly.png')
            cases.append({'id':'QA-03','result':'passed','indexed':2,'closed_wal_sidecars_created':False,'backup_hashes_unchanged':True})
            window.nav.setCurrentRow(3)
            with patch('app.main_window.QFileDialog.getOpenFileName',return_value=(str(image),'')): window.choose_scan_source()
            capture('05b-scan-source-selected.png')
            window.start_scan(); scan_task=finish(); rows=repo.candidate_page(scan_task,offset=100)
            assert repo.candidate_count(scan_task)==103
            assert [c['integrity'] for c in rows]==['structure_checked','structure_checked','truncated']
            assert rows[0]['offset']==len(padding)+100*(len(png)+3)
            assert rows[1]['offset']==len(padding)+len(valid_pngs)
            window.change_result_page(1); capture('06-scan-page-2.png')
            cases.append({'id':'QA-04','result':'passed','total_candidates':103,'cross_block_first_offset':len(padding),'last_page_integrity':[c['integrity'] for c in rows]})
            window.status_filter.setCurrentIndex(window.status_filter.findData('candidate')); window.result_table.selectRow(0)
            window.preview_selected(); finish(); assert not window.preview_label.pixmap().isNull()
            capture('07-preview-and-filter.png')
            cases.append({'id':'QA-05','result':'passed','filtered_candidates':102,'decoded_preview':True})
            start_export(root/'exports'); export_task=finish(); records=repo.exports(export_task)
            assert len(records)==102 and all(r['status']=='exported' and r['source_sha256']==r['target_sha256'] for r in records)
            assert all(hashlib.sha256(Path(r['output_path']).read_bytes()).hexdigest()==r['source_sha256'] for r in records)
            window.export_dialog.next.click(); capture('08-verified-export-page-2.png',window.export_dialog)
            manifest=json.loads(Path(repo.task(export_task)['result_path']).read_text())
            assert manifest['exported']==102 and manifest['failed']==0
            cases.append({'id':'QA-06','result':'passed','verified_exports':102,'manifest_exported':manifest['exported']})
            window.export_dialog.close()
            Path(acquired[0]['local_path']).write_bytes(b'QA deliberate modified-copy fault')
            window.open_results(android_task); window.result_table.selectRow(0); window.preview_selected(); finish(CandidateAccessError)
            assert window.preview_label.pixmap() is None or window.preview_label.pixmap().isNull()
            capture('09-source-change-blocked.png')
            cases.append({'id':'QA-07','result':'passed','changed_copy_preview_rejected':True,'source_phone_unchanged':hashes(phone)==phone_before})
            window.result_table.selectRow(1); start_export(normal/'blocked',False); blocked_task=finish(ExportError)
            assert not (normal/'blocked').exists() and repo.export_count(blocked_task)==0 and hashes(normal)==normal_before
            window.export_dialog.close(); capture('10-after-backup-write-blocked.png')
            cases.append({'id':'QA-08','result':'passed','unrelated_backup_destination_rejected':True,'source_backup_unchanged':True})
            live,writer=backup('live-wal-backup',wal=True)
            try:
                before=hashes(live); window.detect_backups([str(live)]); finish(); window.backup_table.selectRow(0)
                window.nav.setCurrentRow(2); window.parse_backup(); wal_task=finish(BackupError)
                assert repo.candidate_count(wal_task)==0 and hashes(live)==before
                capture('11-wal-index-rejected.png')
                cases.append({'id':'QA-09','result':'passed','live_wal_rejected':True,'source_and_shm_hashes_unchanged':True})
            finally: writer.close()
            window.open_results(scan_task); window.status_filter.setCurrentIndex(window.status_filter.findData('candidate'))
            ready,release=Event(),Event(); finish_export=repo.finish_export
            def gate(*args,**kwargs):
                finish_export(*args,**kwargs)
                if args[1]=='exported':
                    ready.set()
                    if not release.wait(10): raise RuntimeError('cancel capture gate not released')
            try:
                with patch.object(repo,'finish_export',side_effect=gate):
                    start_export(root/'cancelled-exports'); assert ready.wait(10)
                    window.cancel_export(); release.set(); cancelled_task=finish(TaskCancelled)
            finally: release.set()
            cancelled=json.loads(Path(repo.task(cancelled_task)['result_path']).read_text())
            assert cancelled['exported']==1 and cancelled['not_attempted']==101 and cancelled['cancelled']
            capture('12-cancelled-export-audit.png',window.export_dialog)
            cases.append({'id':'QA-10','result':'passed','exported_before_cancel':1,'not_attempted':101,'gate':'Worker paused after the first real audit commit; UI stop invoked'})
            interrupted=repo.create_task('QA-interrupted'); window.close()
            repo=Repository(config.database); repo.recover_interrupted()
            window=MainWindow(config,repo); window.show(); window.nav.setCurrentRow(5); app.processEvents()
            assert repo.task(interrupted)['status']=='interrupted'
            assert repo.export_count(export_task)==102
            capture('13-history-after-reopen.png')
            cases.append({'id':'QA-11','result':'passed','history_exports_after_reopen':102,'unfinished_task':'interrupted'})
            window.nav.setCurrentRow(6); capture('16-settings-local-paths.png')
            window.resize(880,620); window.nav.setCurrentRow(0); app.processEvents()
            assert (window.width(),window.height()) == (880,620)
            home_actions={button.text():button for button in window.stack.widget(0).findChildren(QPushButton)}
            for text in ('打开本地备份','扫描本地镜像'):
                button=home_actions[text]
                assert button.height() >= button.minimumSizeHint().height()
                assert not button.visibleRegion().isEmpty()
            capture('14a-minimum-overview.png')
            window.open_results(scan_task); app.processEvents()
            assert not window.export_all_button.visibleRegion().isEmpty()
            capture('14-minimum-window.png')
            cases.append({'id':'QA-12','result':'passed','requested_size':[880,620],'actual_size':[window.width(),window.height()],'export_control_visible':True,'home_actions_readable':True})

            unicode_root=root/'中文 空格 # 路径'; unicode_root.mkdir()
            unicode_source=unicode_root/('长文件名'*12+'.img')
            unicode_original=b'prefix'+png+JPEG
            unicode_source.write_bytes(unicode_original)
            window.nav.setCurrentRow(3)
            with patch('app.main_window.QFileDialog.getOpenFileName',return_value=(str(unicode_source),'')):
                window.choose_scan_source()
            wait_for_layout(lambda: window.scan_source_label.height() >= window.scan_source_label.heightForWidth(window.scan_source_label.width()))
            assert window.scan_source_label.text() == window.scan_source_label.toolTip() == str(unicode_source)
            assert window.scan_source_label.height() >= window.scan_source_label.heightForWidth(window.scan_source_label.width())
            capture('17a-unicode-source-layout.png')
            window.start_scan(); unicode_task=finish()
            assert repo.candidate_count(unicode_task)==2
            window.result_table.selectRow(0); window.preview_selected(); finish()
            assert not window.preview_label.pixmap().isNull()
            capture('17-unicode-source-preview.png')
            start_export(unicode_root/'校验 导出 #'); unicode_export=finish()
            unicode_records=repo.exports(unicode_export)
            assert len(unicode_records)==2 and all(r['status']=='exported' and r['source_sha256']==r['target_sha256'] for r in unicode_records)
            assert unicode_source.read_bytes()==unicode_original
            capture('18-unicode-export-history.png',window.export_dialog)
            cases.append({'id':'QA-13','result':'passed','unicode_and_spaces_in_source_and_destination':True,'verified_exports':2,'source_unchanged':True})

            ready,release=Event(),Event()
            def exit_probe(token):
                ready.set()
                if not release.wait(20): raise RuntimeError('exit probe was not released')
                token.checkpoint()
            try:
                window.run_task('qa_exit_probe',exit_probe,lambda *_:None)
                assert ready.wait(15)
                window.quit_action.trigger(); app.processEvents()
                assert window.isVisible() and window.export_dialog.isVisible()
                capture('19-exit-waits-for-task.png')
                release.set(); finish(TaskCancelled)
                history=window.export_dialog
                window.quit_action.trigger(); app.processEvents()
                assert not window.isVisible() and not history.isVisible()
                cases.append({'id':'QA-14','result':'passed','quit_cancels_then_waits':True,'history_closes_after_job_finishes':True})
            finally: release.set()
            assert hashes(phone)==phone_before and hashes(normal)==normal_before
            assert hashlib.sha256(image.read_bytes()).hexdigest()==image_before
        finally:
            if window.pending:
                window.pending[0].cancel()
                try: window.pending[0].future.result(15)
                except Exception: pass
                window.poll()
            window.close()
    result={'date':datetime.now(timezone.utc).date().isoformat(),'platform':sys.platform,'architecture':platform.machine(),'qt_platform':app.platformName(),
            'backend':f'Qt {app.platformName()}; synthetic files and mocked ADB transport; real core services/SQLite',
            'cases':cases,'source_phone_backup_image_hashes_unchanged':True,
            'fault_injection':'Only a working acquisition copy was deliberately changed; live WAL writer closed after the no-write assertion',
            'screenshots':[{'file':name,'sha256':hashlib.sha256((output/name).read_bytes()).hexdigest()} for name in screenshots]}
    (output/'evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'workflow_cases':len(cases),'screenshots':len(screenshots),'passed':len(cases),'output':str(output)},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path,default=Path('docs/qa/20261103'))
    run(parser.parse_args().output.resolve())
