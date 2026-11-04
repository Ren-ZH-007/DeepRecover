"""Workspace chooser and handoff through real Qt, SQLite and local image services."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import sys
import tempfile
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest
from app.workspace import WorkspaceSession, validate_workspace
from ui.settings.workspace import WorkspaceDialog
from core.scanner.signature.scanner import SignatureScanner
from storage.models.source import DataSource
from tests.test_scanner import png_image
from scripts.computer_qa_capture import verify_text_glyphs


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    session = WorkspaceSession(app); cases = []; shots = []; release = Event()
    with tempfile.TemporaryDirectory(prefix='DeepRecover-Guided-QA-') as directory:
        root = Path(directory).resolve()
        def capture(name, widget):
            QTest.qWait(40); app.processEvents()
            assert widget.grab().save(str(output / name), 'PNG'); shots.append(name)
        def record(name, **details): cases.append({'id': name, 'result': 'passed', **details})
        def switch(target):
            previous = session.window
            with patch.object(WorkspaceDialog, 'select', return_value=target), \
                    patch('PySide6.QtWidgets.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                previous.workspace_button.click()
                future = previous.computer_page._future; assert future is not None
                future.result(30); previous.poll()
            assert session.window is not previous
            return session.window
        try:
            target = root / 'workspace-A'
            chooser = WorkspaceDialog(target); chooser.show(); app.processEvents()
            font_check = verify_text_glyphs(chooser.font())
            assert not target.exists(); capture('01-workspace-before-initialization.png', chooser)
            chooser.accept(); assert not target.exists()
            window = session.activate(validate_workspace(chooser.selected_path))
            assert window.config.database.exists() and window.config.data_dir == target
            record('GQA-01', no_database_or_logs_before_selection=True, opened_selected_workspace=True)
            image = root / '合成图片来源.img'; image.write_bytes(b'prefix' + png_image())
            before = hashlib.sha256(image.read_bytes()).hexdigest()
            handle = window.manager.submit('signature_scan', lambda token: SignatureScanner().scan(image, token),
                source=DataSource('image', str(image)), persist_candidates=True, batch_size=1)
            assert handle.future.result(30) == 1
            window.open_results(handle.id); window.result_table.selectRow(0)
            assert window.result_table.item(0,3).text() == '图片结构检查通过'
            capture('02-original-workspace-history.png', window)
            record('GQA-02', actual_signature_candidates=1, readable_content_state=True)
            original_db = window.config.database.read_bytes()
            original_log = (target / 'Logs/app.log').read_bytes()
            window.nav.setCurrentRow(6)
            window = switch(root / 'workspace-B'); window.nav.setCurrentRow(6)
            assert window.repository.tasks() == []
            assert (target / 'index.db').read_bytes() == original_db
            assert (target / 'Logs/app.log').read_bytes() == original_log
            assert list((window.config.data_dir / 'Acquisitions').iterdir()) == []
            capture('03-fresh-workspace-no-history-copy.png', window)
            record('GQA-03', old_database_unchanged=True, old_log_unchanged=True, history_not_copied=True)
            window = switch(target); window.open_results(handle.id)
            assert window.repository.candidate_count(handle.id) == 1 and window.result_table.rowCount() == 1
            capture('04-existing-workspace-history-reopened.png', window)
            record('GQA-04', existing_workspace_history_reopened=True)
            window.nav.setCurrentRow(6)
            window.run_task('guided_busy_fixture', lambda token: release.wait(10), lambda *_: None)
            assert window.pending is not None and not window.workspace_button.isEnabled()
            with patch.object(WorkspaceDialog, 'select') as select:
                window.choose_work_directory(); select.assert_not_called()
            capture('05-busy-workspace-switch-blocked.png', window)
            release.set(); window.pending[0].future.result(10); window.poll()
            assert window.config.data_dir == target
            record('GQA-05', busy_picker_not_opened=True, workspace_unchanged=window.config.data_dir == target)
            assert hashlib.sha256(image.read_bytes()).hexdigest() == before
        finally:
            release.set()
            if session.window is not None:
                window = session.window
                if window.pending:
                    window.pending[0].future.result(15); window.poll()
                window.close()
            app.processEvents()
            logger = logging.getLogger('deeprecover')
            for handler in list(logger.handlers):
                if isinstance(handler, RotatingFileHandler): logger.removeHandler(handler); handler.close()
    report = {'date': datetime.now(timezone.utc).date().isoformat(), 'platform': sys.platform,
        'architecture': platform.machine(), 'qt_platform': app.platformName(), 'cases': cases,
        'screenshot_font_check': font_check, 'synthetic_source_sha256_unchanged': True,
        'raw_devices_opened': False,
        'screenshots': [{'file': name, 'sha256': hashlib.sha256((output / name).read_bytes()).hexdigest()} for name in shots]}
    (output / 'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'workflow_cases': len(cases), 'passed': len(cases), 'screenshots': len(shots)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output.resolve())
