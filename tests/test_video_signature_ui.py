"""Real scan UI controls and read-only, persistent video evidence views."""
from importlib.resources import files
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel
from app.app_config import AppConfig
from app.main_window import MainWindow
from storage.database.repository import Repository


class VideoSignatureUiTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = AppConfig(self.root / 'work'); self.config.initialize()
        self.repo = Repository(self.config.database)
        self.window = MainWindow(self.config, self.repo); self.window.timer.stop()
        self.addCleanup(self.window.close)
        self.fixture = files('app').joinpath('assets/preview-test.mp4').read_bytes()
        self.path = self.root / 'synthetic.mov'; self.path.write_bytes(b'padding' + self.fixture + b'trailer')

    def scan(self):
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(self.path), '')) as picker:
            self.window.choose_scan_source()
        self.assertIn('*.mp4 *.mov', picker.call_args.args[-1])
        self.window.scan_start_button.click()
        job = self.window.pending[0]; self.assertEqual(job.future.result(10), 1)
        self.window.poll(); self.app.processEvents()
        return job.id

    def test_scan_controls_filter_and_bound_source_range(self):
        task = self.scan()
        self.window.category_filter.setCurrentIndex(self.window.category_filter.findData('video'))
        self.assertEqual(self.window.result_table.rowCount(), 1)
        item = self.window.current_candidates[0]
        self.assertEqual((item['offset'], item['size']), (7, len(self.fixture)))
        self.assertEqual(self.repo.task(task)['source_id'], item['source_id'])
        self.assertIn('采样范围', self.window.result_table.item(0, 3).text())
        self.assertIn('未知', self.window.result_table.item(0, 11).text())

    def test_video_evidence_displays_bounded_counts_without_touching_database(self):
        self.scan(); before = self.config.database.read_bytes()
        self.window.result_table.selectRow(0); self.window.show_candidate_evidence()
        dialog = self.window.evidence_dialog
        label = dialog.findChild(QLabel, 'evidence-video-details')
        self.assertIsNotNone(label); self.assertIn('111 个采样', label.text())
        self.assertIn('未逐帧解码', label.text())
        self.assertEqual(self.config.database.read_bytes(), before)

    def test_history_reopens_after_source_disappears(self):
        task = self.scan(); original = self.repo.candidates(task)
        self.path.unlink(); self.window.open_results(task)
        self.assertEqual(self.window.current_candidates, original)
        self.window.result_table.selectRow(0); self.window.show_candidate_evidence()
        self.assertIsNotNone(self.window.evidence_dialog.findChild(QLabel, 'evidence-video-details'))

    def test_computer_mode_describes_supported_video_signatures(self):
        combo = self.window.computer_page.mode_combo
        text = combo.itemText(combo.findData('signature'))
        self.assertIn('MP4 / MOV', text); self.assertIn('不判断是否已删除', text)
