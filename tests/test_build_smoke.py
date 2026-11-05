import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 not installed')
class PackagedEntryTests(unittest.TestCase):
    def test_smoke_entry_checks_pipeline_and_preserves_user_data_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); user_data = root / 'user-data'; user_data.mkdir()
            sentinel = user_data / 'original.txt'; sentinel.write_bytes(b'keep original data')
            report = root / 'smoke.json'
            screenshot = root / 'overview.png'
            environment = dict(os.environ, QT_QPA_PLATFORM='offscreen', DEEPRECOVER_DATA_DIR=str(user_data))
            result = subprocess.run([sys.executable, 'main.py', '--smoke-test', '--smoke-report', str(report),
                '--smoke-screenshot', str(screenshot)],
                cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(report.read_text())
            self.assertTrue(data['success'])
            self.assertEqual(data['navigation_pages'], 8)
            self.assertEqual(data['verified_exports'], 2)
            self.assertEqual(data['computer_candidates'], 2)
            self.assertEqual(data['computer_verified_exports'], 2)
            self.assertTrue(data['workspace_dialog_verified'])
            self.assertTrue(data['workspace_roundtrip_verified'])
            self.assertTrue(data['environment_diagnostics_verified'])
            self.assertTrue(data['video_preview_verified'])
            self.assertTrue(data['video_muted'])
            self.assertTrue(data['video_buffer_released'])
            self.assertEqual(data['video_frame_dimensions'], [96, 64])
            self.assertEqual(data['qt_platform'], 'offscreen')
            self.assertEqual(set(data['decoded_formats']), {'PNG','JPEG'})
            self.assertEqual(screenshot.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(list(user_data.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_bytes(), b'keep original data')
