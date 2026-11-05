"""Real Qt diagnostics workflows on local fixtures; never enumerate raw devices.

Collect and save use the actual services. Only deliberate failure cases replace
the UI dependency, to prove that private exception text cannot reach the screen.
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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel
from app.app_config import AppConfig
from app.main_window import MainWindow
from storage.database.repository import Repository
from storage.models.source import DataSource
from scripts.computer_qa_capture import verify_text_glyphs
from ui.settings.diagnostics import CHECK_ERROR, SAVE_ERROR


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    cases, shots = [], []
    private_marker = 'private-source-and-serial-DO-NOT-SHOW'
    with tempfile.TemporaryDirectory(prefix='DeepRecover-Diagnostics-QA-') as directory:
        root = Path(directory).resolve()
        config = AppConfig(root / 'work'); config.initialize()
        source = root / 'source-backup'; source.mkdir()
        sentinel = source / 'original-test-data.bin'
        sentinel.write_bytes(b'Only synthetic local source bytes; preserve unchanged.')
        source_hash = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        repo = Repository(config.database)
        fixture = repo.create_task('m5_source_fixture', DataSource('ios_backup', str(source)))
        repo.update_task(fixture, 'completed')
        before_db = config.database.read_bytes()
        before_tasks = repo.tasks()
        before_sources = repo.source_locations('ios_backup')
        before_files = {path.relative_to(config.data_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in config.data_dir.rglob('*') if path.is_file()}
        window = MainWindow(config, repo); window.timer.stop()
        window.nav.setCurrentRow(6); window.show(); app.processEvents()
        font_check = verify_text_glyphs(window.font())

        def finish():
            future = window.computer_page._future
            assert future is not None, 'Expected an asynchronous read-only operation'
            try:
                future.result(30)
            except Exception:
                # Failure presentation is checked after the normal UI poll.
                pass
            window.poll(); app.processEvents()
            assert not window.computer_page.readonly_pending and window.pending is None

        def unchanged():
            assert config.database.read_bytes() == before_db
            assert repo.tasks() == before_tasks
            assert repo.source_locations('ios_backup') == before_sources
            assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == source_hash
            assert list(source.iterdir()) == [sentinel]
            after_files = {path.relative_to(config.data_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in config.data_dir.rglob('*') if path.is_file()
                           and not path.is_relative_to(config.data_dir / 'Reports')}
            assert after_files == before_files

        def visible_text(dialog):
            return '\n'.join([label.text() for label in dialog.findChildren(QLabel)] + [
                window.statusBar().currentMessage(), window.computer_page.status.text()])

        def capture(name, dialog):
            QTest.qWait(40); app.processEvents()
            assert dialog.grab().save(str(output / name), 'PNG')
            shots.append(name)

        def record(identifier, **details):
            cases.append({'id': identifier, 'result': 'passed', **details})

        try:
            with patch('core.computer.devices.list_devices', side_effect=AssertionError('Unexpected device enumeration')) as inventory, \
                    patch('core.computer.io.DiskReader', side_effect=AssertionError('Unexpected raw device access')) as reader, \
                    patch('core.device.android.adapter.AndroidAdapter.devices', side_effect=AssertionError('Unexpected ADB execution')) as adb:
                window.diagnostics_button.click(); finish()
                dialog = window.diagnostics_dialog
                assert dialog.isVisible() and dialog.report is not None and dialog.save_button.isEnabled()
                assert dialog.report['manual_acceptance']['status'] == 'not_run'
                assert all(case['status'] == 'not_run' for case in dialog.report['manual_acceptance']['cases'])
                assert list((config.data_dir / 'Reports').iterdir()) == []
                serialized = json.dumps(dialog.report, ensure_ascii=False)
                assert str(root) not in serialized and str(source) not in serialized
                unchanged(); capture('01-local-environment-check.png', dialog)
                record('DQA-01', real_collect_service=True, no_report_before_explicit_save=True,
                    database_and_tasks_unchanged=True, manual_acceptance_not_run=True)

                dialog.save_button.click(); finish()
                saved = dialog.saved_path
                assert saved is not None and saved.parent == config.data_dir / 'Reports'
                assert json.loads(saved.read_text()) == dialog.report
                saved_bytes = saved.read_bytes()
                assert str(root).encode() not in saved_bytes and private_marker.encode() not in saved_bytes
                assert len(list(saved.parent.iterdir())) == 1
                assert '已保存到 Reports/' in dialog.status_label.text()
                unchanged(); capture('02-sanitized-report-saved.png', dialog)
                record('DQA-02', real_write_service=True, one_new_local_report=True,
                    database_and_tasks_unchanged=True, report_contains_no_source_path=True)

                with patch('ui.settings.diagnostics.write_environment_report',
                        side_effect=PermissionError(f'{root}/{private_marker}')) as write:
                    dialog.save_button.click(); finish(); write.assert_called_once()
                assert dialog.saved_path is None and dialog.status_label.text() == SAVE_ERROR
                assert private_marker not in visible_text(dialog)
                assert str(root) not in visible_text(dialog)
                assert saved.read_bytes() == saved_bytes and len(list(saved.parent.iterdir())) == 1
                unchanged(); capture('03-report-error-redacted.png', dialog)
                record('DQA-03', save_exception_redacted=True, existing_report_not_overwritten=True,
                    database_and_tasks_unchanged=True)

                with patch('ui.settings.diagnostics.collect_environment',
                        side_effect=RuntimeError(f'{root}/{private_marker}')) as collect:
                    dialog.check_button.click(); finish(); collect.assert_called_once()
                assert dialog.report is None and not dialog.save_button.isEnabled()
                assert dialog.status_label.text() == CHECK_ERROR
                assert private_marker not in visible_text(dialog) and str(root) not in visible_text(dialog)
                unchanged(); capture('04-check-error-redacted.png', dialog)
                record('DQA-04', collect_exception_redacted=True, stale_report_cleared=True,
                    database_and_tasks_unchanged=True)
                inventory.assert_not_called(); reader.assert_not_called(); adb.assert_not_called()
        finally:
            if window.computer_page._future is not None:
                try:
                    window.computer_page._future.result(30)
                except Exception:
                    pass
                window.poll()
            if window.diagnostics_dialog is not None:
                window.diagnostics_dialog.close()
            window.close(); app.processEvents()

    report = {'date': datetime.now(timezone.utc).date().isoformat(), 'platform': sys.platform,
        'architecture': platform.machine(), 'qt_platform': app.platformName(), 'cases': cases,
        'screenshot_font_check': font_check, 'source_sentinel_sha256_unchanged': True,
        'database_and_tasks_unchanged': True, 'raw_devices_opened': False,
        'devices_enumerated': False, 'adb_executed': False,
        'screenshots': [{'file': name, 'sha256': hashlib.sha256((output / name).read_bytes()).hexdigest()}
                        for name in shots]}
    (output / 'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'workflow_cases': len(cases), 'passed': len(cases), 'screenshots': len(shots)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output.resolve())
