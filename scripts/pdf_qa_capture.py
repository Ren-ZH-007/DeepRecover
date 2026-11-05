"""Synthetic PDF scan, isolated page rendering, export and history workflows."""
import argparse
from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QFileDialog,QMessageBox,QLabel
from app.app_config import AppConfig
from app.main_window import MainWindow
from core.candidate_io import snapshot
from core.computer.service import ComputerScanner
from core.export.service import ExportError
from core.scanner.signature.scanner import SignatureScanner
from scripts.computer_qa_capture import verify_text_glyphs
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource
from tests.pdf_fixture import make_pdf,objects,stream
from tests.test_scanner import png_image


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    cases,shots=[],[]
    pdf=files('app').joinpath('assets/preview-test.pdf').read_bytes()
    with tempfile.TemporaryDirectory(prefix='DeepRecover-PDF-QA-') as temporary:
        root=Path(temporary).resolve();config=AppConfig(root/'workspace');config.initialize();repo=Repository(config.database)
        path=root/'synthetic-document-image.img';original=b'pdf-prefix'+pdf+b'gap'+png_image();path.write_bytes(original)
        window=MainWindow(config,repo);window.timer.stop();window.show();app.processEvents()
        font_check=verify_text_glyphs(window.font())
        def record(identifier,**values):cases.append({'id':identifier,'result':'passed',**values})
        def capture(name,widget):
            QTest.qWait(40);app.processEvents();assert widget.grab().save(str(output/name),'PNG');shots.append(name)
        def finish(expected='completed'):
            assert window.pending is not None
            job=window.pending[0]
            try:job.future.result(20)
            except ExportError:
                if expected!='failed':raise
            window.poll();app.processEvents();assert repo.task(job.id)['status']==expected
            return job.id
        def open_pdf(task):
            window.open_results(task);window.category_filter.setCurrentIndex(window.category_filter.findData('document'))
            window.result_table.selectRow(0);window.preview_selected();finish()
            dialog=window.pdf_dialog;assert dialog is not None and not dialog._image.isNull()
            return dialog
        def page_done(dialog):
            assert dialog._pending is not None
            dialog._pending[0].result(15);dialog.poll();app.processEvents()
        try:
            window.nav.setCurrentRow(3)
            with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'')):window.choose_scan_source()
            capture('01-read-only-pdf-scan-scope.png',window)
            window.scan_start_button.click();task=finish();items=repo.candidate_page(task)
            assert len(items)==2
            documents=repo.candidate_page(task,category='document');assert len(documents)==1
            candidate=documents[0]
            assert candidate['offset']==len(b'pdf-prefix') and candidate['size']==len(pdf)
            assert candidate['integrity']=='pdf_structure_checked' and candidate['sha256']==hashlib.sha256(pdf).hexdigest()
            assert candidate['deletion_state']=='unknown'
            record('PQA-01',real_ui_scan=True,exact_range_and_hash=True,document_filter=True,deletion_unknown=True)

            window.category_filter.setCurrentIndex(window.category_filter.findData('document'));window.result_table.selectRow(0)
            window.evidence_button.click();app.processEvents()
            detail=window.evidence_dialog.findChild(QLabel,'evidence-pdf-details')
            assert detail is not None and '2 页' in detail.text() and '未渲染全部页面' in detail.text()
            capture('02-pdf-structure-evidence.png',window.evidence_dialog);window.evidence_dialog.close()
            record('PQA-02',persisted_page_object_evidence=True,no_whole_document_render_claim=True)

            dialog=open_pdf(task);assert dialog.pages==2 and dialog.page==0
            assert (dialog._image.width(),dialog._image.height())==(768,1024)
            first_color=dialog._image.pixelColor(100,500)
            capture('03-first-page-actual-render.png',dialog)
            dialog.next_button.click();page_done(dialog)
            assert dialog.page==1 and dialog._image.pixelColor(100,500)!=first_color
            capture('04-second-page-actual-render.png',dialog)
            record('PQA-03',actual_first_and_second_pages=True,isolated_renderer=True,distinct_page_pixels=True)

            dialog.zoom_combo.setCurrentIndex(dialog.zoom_combo.findData(50));page_done(dialog)
            assert (dialog._image.width(),dialog._image.height())==(384,512)
            capture('05-document-zoom.png',dialog)
            window.result_table.clearSelection()
            assert dialog._closed and dialog.data==b'' and dialog._image.isNull() and window.pdf_dialog is None
            capture('06-selection-releases-document.png',window)
            record('PQA-04',zoom_pixels_verified=True,selection_closes_pdf=True,owned_bytes_and_pixels_released=True)

            computer=window.manager.submit('computer_scan',lambda token:ComputerScanner(config.data_dir).scan(path,token,mode='signature'),source=DataSource('computer',str(path)),persist_candidates=True,batch_size=1)
            assert computer.future.result(20)==2
            comp=repo.candidate_page(computer.id,category='document')[0]
            assert (comp['offset'],comp['size'],comp['sha256'])==(candidate['offset'],candidate['size'],candidate['sha256'])
            computer_dialog=open_pdf(computer.id);computer_dialog.close()
            local=config.data_dir/'Acquisitions/synthetic-pdf';local.write_bytes(pdf)
            android_source=DataSource('android','synthetic-device');android_task=repo.create_task('android_acquisition',android_source)
            acquired=Candidate('android_file','合成授权文件.pdf','pdf',len(pdf),source_id=android_source.id,local_path=str(local),source_stat=snapshot(local.stat()),sha256=hashlib.sha256(pdf).hexdigest(),integrity='not_checked')
            repo.add_candidate(android_task,acquired);repo.update_task(android_task,'completed')
            android_dialog=open_pdf(android_task);android_dialog.close()
            assert local.read_bytes()==pdf and repo.candidate(acquired.id)['integrity']=='not_checked'
            record('PQA-05',computer_source_same_verified_range=True,computer_pdf_rendered=True,ordinary_acquisition_rendered=True,ordinary_copy_not_marked_recovered=True)

            window.open_results(task);window.category_filter.setCurrentIndex(window.category_filter.findData('document'));window.result_table.selectRow(0)
            with patch.object(QFileDialog,'getExistingDirectory',return_value=str(root/'output')),patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):window.export_results(False)
            export=finish();records=repo.exports(export);assert len(records)==1
            assert records[0]['status']=='exported' and records[0]['source_sha256']==records[0]['target_sha256']==candidate['sha256']
            assert Path(records[0]['output_path']).read_bytes()==pdf
            window.show_export_history(export);capture('07-verified-pdf-range-export.png',window.export_dialog);window.export_dialog.close()
            record('PQA-06',exact_export_bytes=True,source_target_sha256_equal=True,independent_export_record=True)

            active=objects();active[1]=active[1][:-2]+b' /AcroForm << >> >>'
            external=objects();external[8]=b'<< /Type /Filespec /F (outside.pdf) >>'
            object_stream=objects();object_stream[8]=stream(b'not admitted',extra=b' /Type /ObjStm')
            bad_path=root/'unsupported-pdfs.img'
            bad_data=make_pdf(trailer=b'/Encrypt 8 0 R')+b'gap'+make_pdf(active)+b'gap'+make_pdf(external)+b'gap'+make_pdf(object_stream)+b'gap'+pdf[:-8]
            bad_path.write_bytes(bad_data)
            bad=window.manager.submit('signature_scan',lambda token:SignatureScanner().scan(bad_path,token),source=DataSource('image',str(bad_path)),persist_candidates=True,batch_size=1)
            assert bad.future.result(20)==5
            rejected=repo.candidate_page(bad.id)
            assert [c['integrity'] for c in rejected]==['pdf_encrypted','pdf_active_content','pdf_external_reference','pdf_unsupported','pdf_truncated']
            assert all(not c['sha256'] and c['status']=='partial' for c in rejected)
            window.open_results(bad.id);window.result_table.selectRow(0);window.evidence_button.click()
            capture('08-encrypted-and-active-documents-rejected.png',window.evidence_dialog);window.evidence_dialog.close()
            window.result_table.selectAll()
            with patch.object(QFileDialog,'getExistingDirectory',return_value=str(root/'rejected-output')),patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):window.export_results(False)
            rejected_export=finish('failed');assert all(r['status']=='failed' for r in repo.exports(rejected_export))
            assert not list((root/'rejected-output').rglob('*.pdf'))
            assert bad_path.read_bytes()==bad_data
            record('PQA-07',encrypted_active_external_object_stream_truncated_distinguished=True,all_unverified_exports_failed=True,no_pdf_output=True)

            assert path.read_bytes()==original and repo.candidate(candidate['id'])['status']=='candidate'
            path.unlink();reopened=Repository(config.database)
            assert reopened.candidate_page(task,category='document')==documents and reopened.exports(export)==records
            with patch('app.main_window.prepare_pdf',side_effect=AssertionError('history must not read PDF')):
                window.open_results(task);window.category_filter.setCurrentIndex(window.category_filter.findData('document'))
            capture('09-history-with-missing-source.png',window)
            record('PQA-08',source_hashes_unchanged=True,candidate_status_unchanged=True,history_without_source=True,history_does_not_render=True)
        finally:
            window.close();app.processEvents()
    evidence={'date_utc':datetime.now(timezone.utc).isoformat(),'platform':sys.platform,'architecture':platform.machine(),'qt_platform':app.platformName(),
        'cases':cases,'screenshot_font_check':font_check,'synthetic_sources_only':True,'raw_devices_opened':False,'network_media_sources_opened':False,'preview_document_copies_created':False,
        'screenshots':[{'file':name,'sha256':hashlib.sha256((output/name).read_bytes()).hexdigest()} for name in shots]}
    (output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'workflow_cases':len(cases),'passed':len(cases),'screenshots':len(shots)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output.resolve())
