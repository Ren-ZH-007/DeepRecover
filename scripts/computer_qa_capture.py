"""Synthetic computer-recovery Qt workflows; no real disk is enumerated or read.

Run with QT_QPA_PLATFORM=cocoa on macOS, or offscreen elsewhere. Dialog answers
are injected; source inspection, scanners, preview, export and SQLite are real.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtTest import QTest
from PySide6.QtGui import QTextLayout
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea
from app.app_config import AppConfig
from app.main_window import MainWindow
from core.candidate_io import CandidateAccessError
from core.computer.io import DiskError
from core.computer import devices as disk_devices
from core.export.service import ExportError
from storage.database.repository import Repository
from tests.fixtures_ntfs import NTFSFixture, file_name, resident_attr, nonresident_attr
from tests.test_scanner import png_image


def verify_text_glyphs(font):
    text = 'DeepRecover 0123456789 电脑恢复 已删除图片 碎片文档 来源只读 本地处理 预览导出 历史结果'
    layout = QTextLayout(text, font)
    layout.beginLayout()
    line = layout.createLine()
    assert line.isValid(), 'Screenshot font cannot lay out text'
    line.setLineWidth(10000)
    layout.endLayout()
    runs = layout.glyphRuns()
    assert runs and all(run.glyphIndexes() and all(run.glyphIndexes()) for run in runs), \
        'Screenshot font is missing Latin or Chinese glyphs; configure QT_QPA_FONTDIR'
    return {'text': text, 'families': sorted({run.rawFont().familyName() for run in runs}),
        'missing_glyphs': 0}


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    cases, shots = [], []
    with tempfile.TemporaryDirectory(prefix='DeepRecover-Computer-QA-') as directory:
        root = Path(directory).resolve(); config = AppConfig(root / 'work'); config.initialize()
        repo = Repository(config.database); window = MainWindow(config, repo); window.show()
        app.processEvents()
        font_check = verify_text_glyphs(window.font())
        page = window.computer_page
        def capture(name, widget=None):
            QTest.qWait(30); app.processEvents()
            assert (widget or window).grab().save(str(output / name), 'PNG')
            shots.append(name)
        def record(identifier, **observed):
            cases.append({'id': identifier, 'result': 'passed', **observed})
        def probe(error=None):
            future = page._future; assert future is not None
            if error:
                try: future.result(30)
                except error: pass
                else: raise AssertionError('Expected preflight ' + error.__name__)
            else: future.result(30)
            window.poll(); app.processEvents()
        def finish(error=None):
            if window.pending is None:
                assert error is None and page.last_task_id is not None
                return page.last_task_id
            handle = window.pending[0]
            if error:
                try: handle.future.result(30)
                except error: pass
                else: raise AssertionError('Expected task ' + error.__name__)
            else: handle.future.result(30)
            window.poll(); app.processEvents(); return handle.id
        def choose_image(path):
            with patch('ui.computer.page.QFileDialog.getOpenFileName', return_value=(str(path), '')):
                page.choose_image(); probe()
        def scan():
            with patch('ui.computer.page.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                page.start_scan(); probe()
            return finish()
        def select(name):
            row = next(i for i, candidate in enumerate(window.current_candidates) if candidate['name'] == name)
            window.result_table.clearSelection(); window.result_table.selectRow(row)
        def export(destination, error=None):
            with patch('app.main_window.QFileDialog.getExistingDirectory', return_value=str(destination)), \
                    patch('app.main_window.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                window.export_results(False)
            return finish(error)
        def open_history(task_id):
            window.nav.setCurrentRow(5); window.refresh_history()
            window.history_table.selectRow(next(i for i, task in enumerate(window.history) if task['id'] == task_id))
            window.open_history_result(); app.processEvents()
        try:
            row = bytes(component for x in range(32) for component in (x * 8, 160, 255 - x * 8))
            png = png_image(width=32, height=24, raw=(b'\0' + row) * 24)
            text = b'Fragmented deleted NTFS text: correct logical order.\n' * 110
            text = text[:4096 + 777]
            image = NTFSFixture()
            image.write_record(16, [resident_attr(0x30, file_name('已删除 图片.png')), resident_attr(0x80, png)])
            image.write_cluster(120, text[:4096]); image.write_cluster(100, text[4096:])
            image.write_record(17, [resident_attr(0x30, file_name('碎片 文档.txt')),
                nonresident_attr([(120, 1), (100, 1)], len(text))])
            image.write_record(18, [resident_attr(0x30, file_name('加密 受限.png')),
                nonresident_attr([(110, 1)], 100, flags=0x4000)])
            ntfs = root / '合成 NTFS 来源.img'; ntfs.write_bytes(image.image)
            apfs = root / '合成 APFS 签名来源.img'; apfs.write_bytes(bytes(32) + b'NXSB' + bytes(4096) + png)
            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (ntfs, apfs)}
            window.nav.setCurrentRow(7); choose_image(ntfs)
            assert repo.tasks() == [] and len(page.partitions) == 1
            assert page.current_partition().file_system == 'NTFS' and page.mode_combo.currentData() == 'deleted'
            capture('01-ntfs-readonly-preflight.png')
            record('CQA-01', filesystem='NTFS', task_created_during_probe=False)
            ntfs_task = scan(); candidates = repo.candidate_page(ntfs_task)
            assert len(candidates) == 3 and sum(c['status'] == 'candidate' for c in candidates) == 2
            assert all(c['deletion_state'] == 'deleted_record' for c in candidates)
            fragmented = next(c for c in candidates if c['name'] == '碎片 文档.txt')
            assert len(fragmented['extents']) == 2
            capture('02-ntfs-deleted-records.png')
            record('CQA-02', deleted_records=3, ready=2, restricted=1, fragmented_extents=2)
            select('已删除 图片.png'); window.preview_selected(); finish()
            assert not window.preview_label.pixmap().isNull()
            capture('03-resident-png-preview.png'); record('CQA-03', resident_png_decoded=True)
            window.status_filter.setCurrentIndex(window.status_filter.findData('candidate'))
            window.result_table.selectAll(); exported = export(root / '已校验 导出')
            records = repo.exports(exported); expected = {'已删除 图片.png': png, '碎片 文档.txt': text}
            assert len(records) == 2 and all(r['status'] == 'exported' for r in records)
            for item in records:
                payload = Path(item['output_path']).read_bytes()
                assert payload == expected[item['metadata']['name']]
                assert hashlib.sha256(payload).hexdigest() == item['source_sha256'] == item['target_sha256']
            capture('04-verified-deleted-exports.png', window.export_dialog)
            record('CQA-04', verified_exports=2, fragmented_logical_order=True)
            window.export_dialog.close(); window.open_results(ntfs_task); select('加密 受限.png')
            window.preview_selected(); finish(CandidateAccessError)
            assert window.preview_label.pixmap() is None or window.preview_label.pixmap().isNull()
            capture('05-restricted-preview-rejected.png')
            restricted = export(root / '受限项 导出', ExportError); failed, = repo.exports(restricted)
            assert failed['status'] == 'failed' and not Path(failed['output_path']).exists()
            capture('06-restricted-export-rejected.png', window.export_dialog)
            record('CQA-05', restricted_preview_rejected=True, restricted_export_rejected=True)
            window.close(); repo = Repository(config.database); window = MainWindow(config, repo)
            page = window.computer_page; window.show()
            with patch('core.computer.io.DiskReader', side_effect=AssertionError('History read source disk')) as reader:
                open_history(ntfs_task); assert window.result_table.rowCount() == 3
                open_history(exported); assert window.export_dialog.table.rowCount() == 2
                reader.assert_not_called()
            capture('07-history-reopened.png', window.export_dialog)
            record('CQA-06', candidates_after_reopen=3, exports_after_reopen=2, source_read_for_history=False)
            window.export_dialog.close(); window.nav.setCurrentRow(7); choose_image(apfs)
            assert page.current_partition().file_system == 'APFS' and page.mode_combo.currentData() == 'signature'
            assert '不解析已删除记录' in page.capability_label.text()
            capture('08-apfs-signature-capability.png')
            apfs_task = scan(); apfs_rows = repo.candidate_page(apfs_task)
            assert len(apfs_rows) == 1 and apfs_rows[0]['deletion_state'] == 'unknown'
            capture('09-apfs-unknown-deletion-state.png')
            record('CQA-07', filesystem='APFS', mode='signature', deletion_state='unknown')
            count = len(repo.tasks()); window.nav.setCurrentRow(7)
            page.devices = [SimpleNamespace(path='/dev/disk99', size=1024, label='QA mock disk', system_disk=False)]
            window.fill(page.device_table, [('/dev/disk99', 1024, 'QA mock disk')]); page.device_table.selectRow(0)
            def apfs_info(path):
                return {'DeviceIdentifier': path.removeprefix('/dev/'),
                    'APFSPhysicalStores': [{'APFSPhysicalStore': 'disk0s2'}]}
            with patch.object(disk_devices.sys, 'platform', 'darwin'), \
                    patch.object(disk_devices, 'normalize_device', side_effect=lambda path: path) as normalize, \
                    patch.object(disk_devices, '_mac_apfs', return_value={}), \
                    patch.object(disk_devices, '_mac_info', side_effect=apfs_info), \
                    patch.object(disk_devices, '_mac_volume_path', return_value='/dev/disk3s5'), \
                    patch('core.computer.service.DiskReader', side_effect=AssertionError('Raw disk opened')) as raw:
                page.choose_device(); probe(DiskError)
                raw.assert_not_called()
                normalize.assert_called_once_with('/dev/disk99')
            assert len(repo.tasks()) == count and page.partitions == []
            assert '工作目录和导出目标必须位于另一块物理磁盘，不能写回来源磁盘。' in page.status.text()
            assert str(config.data_dir) in page.status.text()
            capture('10-raw-workspace-isolation-refused.png')
            page.directory_help_button.click(); app.processEvents()
            assert not page.work_directory_label.visibleRegion().isEmpty()
            capture('10a-work-directory-guidance.png')
            record('CQA-08', same_device_guard_rejected=True, task_count_unchanged=True,
                real_disk_opened=False, mocked_apfs_store_schema='APFSPhysicalStore', work_directory_help_visible=True)
            window.resize(880, 620); QTest.qWait(50); app.processEvents()
            scroll = page.findChildren(QScrollArea)[0]; scroll.verticalScrollBar().setValue(0)
            for button in (page.image_button, page.devices_button):
                assert button.height() >= button.minimumSizeHint().height() and not button.visibleRegion().isEmpty()
            assert (window.width(), window.height()) == (880, 620)
            capture('11-minimum-computer-source-controls.png')
            scroll.ensureWidgetVisible(page.start_button); capture('12-minimum-computer-scan-controls.png')
            assert page.start_button.height() >= page.start_button.minimumSizeHint().height()
            record('CQA-09', minimum_window=[880, 620], source_controls_readable=True, scan_control_readable=True)
            after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (ntfs, apfs)}
            assert after == before
        finally:
            if page._future:
                page.cancel()
                try: page._future.result(30)
                except Exception: pass
                window.poll()
            if window.pending:
                window.pending[0].cancel()
                try: window.pending[0].future.result(30)
                except Exception: pass
                window.poll()
            window.close(); app.processEvents()
    evidence = {'date': datetime.now(timezone.utc).date().isoformat(), 'platform': sys.platform,
        'architecture': platform.machine(), 'qt_platform': app.platformName(), 'cases': cases,
        'source_hashes_unchanged': True, 'source_sha256_before': before, 'source_sha256_after': after,
        'real_disks_enumerated_or_opened': False,
        'screenshot_font_check': font_check,
        'screenshots': [{'file': name, 'sha256': hashlib.sha256((output / name).read_bytes()).hexdigest()} for name in shots]}
    (output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'workflow_cases': len(cases), 'screenshots': len(shots), 'passed': len(cases)}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True, type=Path)
    run(parser.parse_args().output.resolve())
