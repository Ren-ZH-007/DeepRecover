"""Read-only candidate explanations and distinct real export audit outcomes."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
QT_AVAILABLE = importlib.util.find_spec('PySide6') is not None
if QT_AVAILABLE:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel, QScrollArea
    from app.main_window import MainWindow

from app.app_config import AppConfig
from core.candidate_io import snapshot
from core.export.service import export_batch
from core.tasks import CancellationToken
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource
from tests.test_scanner import png_image


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')
class ResultEvidenceUiTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory(prefix='result-evidence-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = AppConfig(self.root / 'work'); self.config.initialize()
        self.repo = Repository(self.config.database)
        self.window = MainWindow(self.config, self.repo)
        self.addCleanup(self.window.close)
        self.window.show(); self.app.processEvents()

    def task(self, *candidates):
        source = DataSource('computer', str(self.root / 'missing.img'), '合成历史来源')
        task = self.repo.create_task('computer_scan', source)
        for candidate in candidates: self.repo.add_candidate(task, candidate)
        self.repo.update_task(task, 'completed')
        self.window.open_results(task)
        return task

    def explain(self, row=0):
        self.window.result_table.selectRow(row)
        self.assertTrue(self.window.evidence_button.isEnabled())
        self.window.evidence_button.click(); self.app.processEvents()
        self.assertTrue(self.window.evidence_dialog.isVisible())
        return self.window.evidence_dialog

    def text(self, dialog, key):
        return dialog.findChild(QLabel, key).text()

    def test_deleted_record_and_layout_are_separate_from_export_in_history_without_source_io(self):
        candidate = Candidate('computer_deleted', '历史文件.png', 'png', 10,
            integrity='metadata_checked', file_system='NTFS', deletion_state='deleted_record')
        self.task(candidate)
        with patch('core.computer.io.DiskReader', side_effect=AssertionError('Evidence opened source')), \
                patch('app.main_window.preview_image', side_effect=AssertionError('Evidence decoded source')):
            dialog = self.explain()
        self.assertEqual(self.window.result_table.item(0,3).text(), '布局检查通过')
        self.assertIn('metadata_checked', self.window.result_table.item(0,3).toolTip())
        self.assertEqual(self.window.result_table.item(0,9).text(), '候选')
        self.assertIn('candidate', self.window.result_table.item(0,9).toolTip())
        self.assertEqual(self.text(dialog,'evidence-deletion-label'), '文件系统删除记录')
        self.assertEqual(self.text(dialog,'evidence-content-label'), '布局检查通过')
        self.assertIn('不判断是否已导出', self.text(dialog,'evidence-export-explanation'))
        self.assertIn('已完成', self.window.result_source.text())
        self.assertIn('completed', self.window.result_source.toolTip())

    def test_signature_unknown_states_and_confidence_do_not_turn_into_recovery_rate(self):
        candidate = Candidate('computer_signature', 'unknown.png', 'png', 10,
            status='future_state', integrity='future_integrity', confidence=.99,
            deletion_state='deleted_record', file_system='APFS')
        self.task(candidate); dialog = self.explain()
        self.assertEqual(self.text(dialog,'evidence-deletion-label'), '未知（签名候选）')
        self.assertEqual(self.text(dialog,'evidence-content-label'), '未验证的检查结果')
        self.assertEqual(self.window.result_table.item(0,9).text(), '状态未知')
        self.assertIn('future_state', self.window.result_table.item(0,9).toolTip())
        text = '\n'.join(label.text() for label in dialog.findChildren(QLabel))
        self.assertNotIn('99%', text); self.assertNotIn('恢复率', text)

    def test_selection_page_and_filter_changes_clear_evidence_and_old_selection(self):
        self.window.page_size = 1
        self.task(Candidate('signature_scan','first.png','png',10,integrity='structure_checked'),
                  Candidate('computer_deleted','second.txt','txt',8,integrity='layout_unverified',
                            status='partial',deletion_state='deleted_record',file_system='FAT32'))
        self.explain()
        self.window.change_result_page(1)
        self.assertIsNone(self.window.evidence_dialog)
        self.assertFalse(self.window.evidence_button.isEnabled())
        self.assertEqual(self.window.selected_candidate_ids(), [])
        self.assertEqual(self.window.result_table.item(0,0).text(), 'second.txt')
        dialog = self.explain()
        self.assertEqual(self.text(dialog,'evidence-content-label'), '文件布局未确认')
        self.window.category_filter.setCurrentIndex(self.window.category_filter.findData('image'))
        self.assertIsNone(self.window.evidence_dialog)
        self.assertFalse(self.window.evidence_button.isEnabled())
        self.assertEqual(self.window.selected_candidate_ids(), [])

    def test_multi_selection_cannot_display_a_single_files_stale_evidence(self):
        self.task(Candidate('android_file','first.jpg','jpg',10,integrity='not_checked'),
                  Candidate('ios_backup','second.jpg','jpg',10,integrity='not_checked'))
        self.explain(); self.window.result_table.selectAll()
        self.assertIsNone(self.window.evidence_dialog)
        self.assertFalse(self.window.evidence_button.isEnabled())
        self.assertEqual(self.window.result_table.item(0,11).text(), '普通文件采集')
        self.assertEqual(self.window.result_table.item(1,11).text(), '备份文件索引')

    def test_actual_export_does_not_change_candidate_state_and_history_is_not_current_file_check(self):
        payload = png_image(); path = self.config.data_dir / 'Acquisitions/source.png'; path.write_bytes(payload)
        source = DataSource('android','SYNTHETIC','合成设备')
        task = self.repo.create_task('android_acquisition',source)
        candidate = Candidate('android_file','源.png','png',len(payload),local_path=str(path),
            sha256=hashlib.sha256(payload).hexdigest(),source_stat=snapshot(path.stat()),
            source_id=source.id,integrity='not_checked')
        self.repo.add_candidate(task,candidate); self.repo.update_task(task,'completed')
        before = self.repo.candidate(candidate.id)
        exported = self.repo.create_task('export')
        export_batch(self.repo,exported,[candidate.id],self.root/'out',self.config.data_dir,CancellationToken())
        self.repo.update_task(exported,'completed')
        self.assertEqual(self.repo.candidate(candidate.id), before)
        record, = self.repo.exports(exported)
        self.assertEqual(Path(record['output_path']).read_bytes(),payload)
        Path(record['output_path']).unlink()
        self.window.open_results(task); dialog = self.explain()
        self.assertIn('不判断是否已导出',self.text(dialog,'evidence-export-explanation'))
        self.window.show_export_history(exported)
        item = self.window.export_dialog.table.item(0,1)
        self.assertEqual(item.text(),'摘要校验通过')
        self.assertIn('exported',item.toolTip())
        self.assertIn('历史目标不会自动重新校验',item.toolTip())
        self.assertEqual(path.read_bytes(),payload)

    def test_exported_marker_with_mismatched_hashes_is_not_displayed_as_verified(self):
        task = self.task(Candidate('signature_scan','bad.png','png',10))
        candidate, = self.repo.candidate_page(task)
        exported = self.repo.create_task('export')
        record = self.repo.start_export(exported,candidate['id'],self.root/'not-created.png',
                                        {'name':'bad.png','size':10})
        self.repo.finish_export(record,'exported',source_sha256='a'*64,target_sha256='b'*64)
        self.repo.update_task(exported,'completed')
        self.window.show_export_history(exported)
        item = self.window.export_dialog.table.item(0,1)
        self.assertEqual(item.text(),'摘要记录不足')
        self.assertIn('exported',item.toolTip())
        self.assertEqual(self.repo.exports(exported)[0]['status'],'exported')

    def test_evidence_details_remain_scrollable_and_export_controls_visible_at_minimum_window(self):
        self.task(Candidate('computer_deleted','受限.png','png',10,integrity='unsupported_layout',
                            status='partial',deletion_state='deleted_record',file_system='NTFS'))
        self.window.resize(880,620); QTest.qWait(40)
        self.assertEqual((self.window.width(),self.window.height()),(880,620))
        self.assertFalse(self.window.export_selected_button.visibleRegion().isEmpty())
        dialog = self.explain(); dialog.resize(420,300); QTest.qWait(40)
        scroll = dialog.findChild(QScrollArea)
        self.assertGreater(scroll.verticalScrollBar().maximum(),0)
        self.assertEqual(self.text(dialog,'evidence-content-label'),'暂不支持此布局')

    def test_missing_task_clears_previously_opened_results_and_evidence(self):
        self.task(Candidate('signature_scan','gone.png','png',10,integrity='structure_checked'))
        self.explain()
        self.window.open_results('missing-task')
        self.assertEqual(self.window.result_table.rowCount(),0)
        self.assertIsNone(self.window.evidence_dialog)
        self.assertFalse(self.window.evidence_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
