"""Real scan/range/Qt decode/export/history workflows using synthetic media only."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QLabel
from app.app_config import AppConfig
from app.main_window import MainWindow
from core.computer.service import ComputerScanner
from core.export.service import ExportError
from core.scanner.signature.scanner import SignatureScanner
from core.tasks import CancellationToken
from scripts.computer_qa_capture import verify_text_glyphs
from storage.database.repository import Repository
from storage.models.source import DataSource
from tests.test_scanner import png_image
from tests.video_signature_fixture import movie, edit_box, atom, mov_fixture


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([]); app.setQuitOnLastWindowClosed(False)
    cases, shots = [], []
    fixture = files('app').joinpath('assets/preview-test.mp4').read_bytes()
    # Same real samples, QuickTime brand, moov after mdat, rebased chunk offsets.
    mov = mov_fixture(fixture)
    with tempfile.TemporaryDirectory(prefix='DeepRecover-Video-Signature-QA-') as temporary:
        root = Path(temporary).resolve(); config = AppConfig(root / 'workspace'); config.initialize()
        repo = Repository(config.database); path = root / 'synthetic-disk.img'
        original = b'synthetic-prefix' + fixture + b'gap' + mov + b'trailer' + png_image()
        path.write_bytes(original); before_hash = hashlib.sha256(original).hexdigest()
        window = MainWindow(config, repo); window.timer.stop(); window.show(); app.processEvents()
        font_check = verify_text_glyphs(window.font())

        def record(identifier, **values): cases.append({'id': identifier, 'result': 'passed', **values})
        def capture(name, widget):
            QTest.qWait(40); app.processEvents()
            assert widget.grab().save(str(output / name), 'PNG')
            shots.append(name)
        def finish(expected='completed'):
            assert window.pending is not None
            job = window.pending[0]
            try:
                job.future.result(20)
            except ExportError:
                if expected != 'failed': raise
            window.poll(); app.processEvents()
            assert repo.task(job.id)['status'] == expected
            return job.id
        def decode(row):
            window.result_table.selectRow(row); window.preview_selected(); finish()
            dialog = window.video_dialog; assert dialog is not None
            for _ in range(500):
                if dialog.frame_count > 0 or dialog._failed: break
                QTest.qWait(20)
            assert dialog.frame_count > 0 and not dialog._failed
            assert (dialog._image.width(), dialog._image.height()) == (96, 64)
            assert dialog.player.source().isEmpty() and dialog.player.audioOutput() is None
            return dialog
        try:
            window.nav.setCurrentRow(3)
            with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), '')):
                window.choose_scan_source()
            capture('01-read-only-scan-scope.png', window)
            window.scan_start_button.click(); task = finish()
            items = repo.candidate_page(task); assert len(items) == 3
            videos = repo.candidate_page(task, category='video')
            assert [c['file_type'] for c in videos] == ['mp4', 'mov']
            assert [c['offset'] for c in videos] == [len(b'synthetic-prefix'), len(b'synthetic-prefix') + len(fixture) + 3]
            assert all(c['integrity'] == 'video_structure_checked' and c['size'] == len(fixture) and c['deletion_state'] == 'unknown' for c in videos)
            record('VSQA-01', ui_scan=True, exact_offsets_and_lengths=True, deletion_unknown=True)

            window.category_filter.setCurrentIndex(window.category_filter.findData('video'))
            assert window.result_table.rowCount() == 2
            capture('02-video-filter-and-source-ranges.png', window)
            window.result_table.selectRow(0); window.evidence_button.click(); app.processEvents()
            details = window.evidence_dialog.findChild(QLabel, 'evidence-video-details')
            assert details is not None and '未逐帧解码' in details.text()
            capture('03-container-and-sample-evidence.png', window.evidence_dialog)
            window.evidence_dialog.close()
            record('VSQA-02', video_filter=True, persisted_check_details=True, no_decode_claim=True)

            dialog = decode(0); capture('04-scanned-mp4-decoded-frame.png', dialog); dialog.close()
            assert dialog.buffer.size() == 0 and not dialog.sink.videoFrame().isValid()
            dialog = decode(1); capture('05-scanned-mov-decoded-frame.png', dialog); dialog.close()
            assert dialog.buffer.size() == 0
            record('VSQA-03', mp4_actual_frame=True, mov_actual_frame=True, source_url_empty=True, muted=True, buffers_released=True)

            window.result_table.selectAll()
            with patch.object(QFileDialog, 'getExistingDirectory', return_value=str(root / 'output')), patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                window.export_results(False)
            export = finish(); records = repo.exports(export)
            assert len(records) == 2 and all(r['status'] == 'exported' and r['source_sha256'] == r['target_sha256'] for r in records)
            assert [Path(r['output_path']).read_bytes() for r in records] == [fixture, mov]
            window.show_export_history(export); capture('06-verified-range-exports.png', window.export_dialog); window.export_dialog.close()
            record('VSQA-04', exact_export_bytes=True, source_target_sha256_equal=True, independent_export_records=True)

            computer = window.manager.submit('computer_scan', lambda token: ComputerScanner(config.data_dir).scan(path, token, mode='signature'),
                source=DataSource('computer', str(path)), persist_candidates=True, batch_size=1)
            assert computer.future.result(20) == 3
            cv = repo.candidate_page(computer.id, category='video')
            assert [(c['offset'], c['size'], c['sha256']) for c in cv] == [(c['offset'], c['size'], c['sha256']) for c in videos]
            record('VSQA-05', computer_pipeline=True, identical_range_hashes=True)

            bad_path = root / 'unsupported.img'
            zero = movie(); at = zero.index(b'mdat') - 4
            zero = zero[:at] + b'\0' * 4 + zero[at + 4:]
            bad_data = (edit_box(movie(), b'moov', lambda p: p + atom(b'mvex', b'')) + b'gap'
                + movie().replace(b'avc1', b'encv') + b'gap'
                + edit_box(movie(), b'dref', lambda p: p[:-1] + b'\0') + b'gap' + zero + b'gap' + fixture[:-1])
            bad_path.write_bytes(bad_data)
            bad_job = window.manager.submit('signature_scan', lambda token: SignatureScanner().scan(bad_path, token),
                source=DataSource('image', str(bad_path)), persist_candidates=True, batch_size=1)
            assert bad_job.future.result(20) == 5
            rejected = repo.candidate_page(bad_job.id)
            assert [c['integrity'] for c in rejected] == ['video_fragmented', 'video_encrypted', 'video_external_reference', 'video_boundary_unknown', 'video_truncated']
            assert all(c['status'] == 'partial' and not c['sha256'] for c in rejected)
            window.open_results(bad_job.id); window.result_table.selectRow(3); window.evidence_button.click()
            capture('07-unknown-boundary-rejected.png', window.evidence_dialog); window.evidence_dialog.close()
            record('VSQA-06', fragmented_encrypted_external_unknown_truncated_distinguished=True, no_unverified_hashes=True)

            window.result_table.selectAll()
            with patch.object(QFileDialog, 'getExistingDirectory', return_value=str(root / 'rejected-output')), patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                window.export_results(False)
            rejected_export = finish('failed'); failures = repo.exports(rejected_export)
            assert len(failures) == 5 and all(r['status'] == 'failed' for r in failures)
            assert not list((root / 'rejected-output').rglob('*.mp4'))
            record('VSQA-07', all_unverified_exports_failed=True, no_media_outputs=True)

            assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
            assert bad_path.read_bytes() == bad_data
            path.unlink()
            reopened = Repository(config.database)
            assert reopened.candidate_page(task, category='video') == videos
            assert reopened.exports(export) == records
            window.open_results(task); window.category_filter.setCurrentIndex(window.category_filter.findData('video'))
            capture('08-history-with-missing-source.png', window)
            record('VSQA-08', history_without_source=True, candidate_records_unchanged=True, source_hashes_unchanged=True)
        finally:
            window.close(); app.processEvents()
    evidence = {'date_utc': datetime.now(timezone.utc).isoformat(), 'platform': sys.platform, 'architecture': platform.machine(),
        'qt_platform': app.platformName(), 'cases': cases, 'screenshot_font_check': font_check,
        'synthetic_sources_only': True, 'raw_devices_opened': False, 'network_media_sources_opened': False,
        'screenshots': [{'file': name, 'sha256': hashlib.sha256((output / name).read_bytes()).hexdigest()} for name in shots]}
    (output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'workflow_cases': len(cases), 'passed': len(cases), 'screenshots': len(shots)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output.resolve())
