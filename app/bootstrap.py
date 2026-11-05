import sys
import argparse
import os
import tempfile
import logging
from app.version import VERSION

def main():
    if '--pdf-render-worker' in sys.argv:
        from core.preview.pdf_worker import main as pdf_worker_main
        return pdf_worker_main()
    if '--smoke-test' in sys.argv:
        parser = argparse.ArgumentParser(description='DeepRecover packaged application verification')
        parser.add_argument('--smoke-test', action='store_true')
        parser.add_argument('--smoke-report', required=True)
        parser.add_argument('--smoke-screenshot')
        args = parser.parse_args()
        previous = os.environ.get('DEEPRECOVER_DATA_DIR')
        try:
            with tempfile.TemporaryDirectory(prefix='deeprecover-packaged-') as directory:
                os.environ['DEEPRECOVER_DATA_DIR'] = directory
                try:
                    return _run_gui(args.smoke_report, args.smoke_screenshot)
                finally:
                    # Windows cannot delete an open log file when cleaning the
                    # isolated smoke directory. Only the smoke process exits here.
                    logging.shutdown()
        finally:
            if previous is None: os.environ.pop('DEEPRECOVER_DATA_DIR', None)
            else: os.environ['DEEPRECOVER_DATA_DIR'] = previous
    return _run_gui()


def _run_gui(smoke_report=None, smoke_screenshot=None):
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("请先运行 python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    app = QApplication([sys.argv[0]] if smoke_report else sys.argv)
    app.setApplicationName("DeepRecover")
    app.setOrganizationName("DeepRecover")
    app.setApplicationVersion(VERSION)
    quit_on_last_window = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    try:
        import sqlite3
        from app.workspace import WorkspaceError, WorkspaceSession, startup_workspace
        session = WorkspaceSession(app)
        suggested = None
        while True:
            selection = startup_workspace(suggested)
            if selection is None: return 0
            try:
                window = session.activate(selection)
                break
            except (WorkspaceError, OSError, sqlite3.Error) as error:
                QMessageBox.critical(None, '工作目录未打开', str(error) if isinstance(error, WorkspaceError)
                    else f'无法打开此目录（{type(error).__name__}），请检查权限或选择其他目录。')
                if os.environ.get('DEEPRECOVER_DATA_DIR'): return 1
                suggested = selection.path
        app.workspace_session = session
        logger = logging.getLogger('deeprecover')
    except Exception as error:
        QMessageBox.critical(None, "DeepRecover 启动失败", f"无法初始化工作目录或数据库（{type(error).__name__}）。请检查目录权限及版本。")
        return 1
    finally:
        app.setQuitOnLastWindowClosed(quit_on_last_window)
    logger.info("application_started version=%s", VERSION)
    window.show()
    if smoke_report:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(250, lambda: _verify_packaged(app, window, smoke_report, smoke_screenshot))
    return app.exec()


def _verify_packaged(app, window, report_path, screenshot_path=None):
    """Exercise the real packaged Qt plugins and file pipeline, then exit."""
    import hashlib
    import json
    from pathlib import Path
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImage, QAction, QKeySequence
    from core.scanner.signature.scanner import SignatureScanner
    from core.preview.images import preview_image
    from core.export.service import export_batch
    from core.tasks import CancellationToken
    from storage.models.source import DataSource
    from storage.database.repository import SCHEMA_VERSION
    result = {'success': False, 'version': VERSION, 'schema_version': SCHEMA_VERSION,
              'navigation_pages': window.stack.count(), 'qt_platform': app.platformName()}
    exit_code = 1
    try:
        assert window.isVisible() and window.stack.count() == 8
        from ui.settings.workspace import WorkspaceDialog
        from app.workspace import validate_workspace
        untouched = window.config.data_dir / 'workspace-dialog-check'
        chooser = WorkspaceDialog(untouched, window); chooser.show(); app.processEvents()
        assert chooser.isVisible() and not untouched.exists()
        chooser.reject(); app.processEvents()
        assert not untouched.exists()
        result['workspace_dialog_verified'] = True
        for index in range(8):
            window.nav.setCurrentRow(index); app.processEvents()
            assert window.stack.currentIndex() == index
        payloads = []
        image = QImage(32, 24, QImage.Format.Format_RGB32); image.fill(0xff3167d6)
        for format_name in ('PNG', 'JPEG'):
            buffer = QBuffer(); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            assert image.save(buffer, format_name), format_name
            payloads.append(bytes(buffer.data()))
        source = window.config.data_dir / 'synthetic.img'
        original = b'padding' + b''.join(payloads); source.write_bytes(original)
        repo = window.repository
        scan = window.manager.submit('signature_scan', lambda token: SignatureScanner().scan(source, token),
            source=DataSource('image', str(source)), persist_candidates=True, batch_size=1)
        assert scan.future.result(15) == 2
        ids = repo.candidate_ids(scan.id)
        window.open_results(scan.id)
        for candidate_id in ids:
            decoded = preview_image(repo, candidate_id, window.config.data_dir, CancellationToken())
            assert not decoded.isNull()
        exported = window.manager.submit('export', lambda token, task_id: export_batch(repo, task_id, ids,
            window.config.data_dir / 'Recovered', window.config.data_dir, token), with_task_id=True)
        exported.future.result(15)
        records = repo.exports(exported.id)
        assert len(records) == 2
        for record, payload in zip(records, payloads):
            assert record['status'] == 'exported' and record['source_sha256'] == record['target_sha256']
            assert Path(record['output_path']).read_bytes() == payload
        assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(original).digest()
        from core.computer.service import ComputerScanner
        computer = window.manager.submit('computer_scan', lambda token: ComputerScanner(window.config.data_dir).scan(source, token, mode='signature'),
            source=DataSource('computer', str(source)), persist_candidates=True, batch_size=1)
        assert computer.future.result(15) == 2
        computer_ids = repo.candidate_ids(computer.id)
        for candidate_id in computer_ids:
            assert not preview_image(repo, candidate_id, window.config.data_dir, CancellationToken()).isNull()
        copied = window.manager.submit('export', lambda token, task_id: export_batch(repo, task_id, computer_ids,
            window.config.data_dir / 'Recovered', window.config.data_dir, token), with_task_id=True)
        copied.future.result(15)
        computer_records = repo.exports(copied.id)
        assert len(computer_records) == 2 and all(r['status'] == 'exported' and r['source_sha256'] == r['target_sha256'] for r in computer_records)
        assert [Path(r['output_path']).read_bytes() for r in computer_records] == payloads
        assert source.read_bytes() == original
        result.update(computer_candidates=2, computer_verified_exports=2)
        # Verify the packaged multimedia backend with original synthetic media.
        from importlib.resources import files
        from time import monotonic
        from PySide6.QtCore import QThread
        fixture = files('app').joinpath('assets/preview-test.mp4').read_bytes()
        clip = window.config.data_dir / 'synthetic-video.img'
        video_image = b'synthetic-prefix' + fixture + b'unrelated-trailing-bytes'
        clip.write_bytes(video_image)
        video_scan = window.manager.submit('signature_scan', lambda token: SignatureScanner().scan(clip, token),
            source=DataSource('image', str(clip)), persist_candidates=True, batch_size=1)
        assert video_scan.future.result(15) == 1
        video_task = video_scan.id
        video_candidate_id = repo.candidate_ids(video_task)[0]
        scanned_video = repo.candidate(video_candidate_id)
        assert scanned_video['integrity'] == 'video_structure_checked'
        assert scanned_video['offset'] == len(b'synthetic-prefix') and scanned_video['size'] == len(fixture)
        assert scanned_video['sha256'] == hashlib.sha256(fixture).hexdigest()
        video_export = window.manager.submit('export', lambda token, task_id: export_batch(repo, task_id,
            [video_candidate_id], window.config.data_dir / 'Recovered', window.config.data_dir, token), with_task_id=True)
        video_export.future.result(15)
        video_records = repo.exports(video_export.id)
        assert len(video_records) == 1 and video_records[0]['status'] == 'exported'
        assert video_records[0]['source_sha256'] == video_records[0]['target_sha256'] == scanned_video['sha256']
        assert Path(video_records[0]['output_path']).read_bytes() == fixture
        result.update(video_scan_verified=True, video_scan_exports=1)
        before_candidate = repo.candidate(video_candidate_id)
        window.open_results(video_task); window.result_table.selectRow(0); window.preview_selected()
        assert window.pending is not None
        window.pending[0].future.result(15); window.poll()
        video = window.video_dialog
        assert video is not None and video.isVisible()
        def wait_video(predicate):
            deadline = monotonic() + 10
            while not predicate():
                assert monotonic() < deadline, 'Video backend did not complete verification'
                app.processEvents(); QThread.msleep(5)
        wait_video(lambda: video.frame_count > 0 or video._failed)
        assert not video._failed and not video.canvas.pixmap().isNull()
        assert (video._image.width(), video._image.height()) == (96, 64)
        assert video.player.audioOutput() is None and video.player.hasAudio()
        assert video.player.source().isEmpty() and video.player.sourceDevice() is video.buffer
        video.play_button.click()
        wait_video(lambda: video.player.position() >= 125 and video.frame_count >= 2)
        video.play_button.click()
        video.seek_slider.setValue(1000); video.seek()
        wait_video(lambda: video.player.position() >= 900)
        video.close()
        assert video.buffer.size() == 0 and not video.buffer.isOpen()
        assert not video.sink.videoFrame().isValid()
        assert clip.read_bytes() == video_image and repo.candidate(video_candidate_id) == before_candidate
        result.update(video_preview_verified=True, video_muted=True, video_buffer_released=True,
                      video_frame_dimensions=[96, 64])
        # Exercise the actual packaged PDF worker (including windowed Windows
        # stdin/stdout handles), static pages, zoom and verified range export.
        pdf_bytes = files('app').joinpath('assets/preview-test.pdf').read_bytes()
        pdf_path = window.config.data_dir / 'synthetic-pdf.img'
        pdf_source_bytes = b'pdf-prefix' + pdf_bytes + b'unrelated-tail'
        pdf_path.write_bytes(pdf_source_bytes)
        pdf_scan = window.manager.submit('signature_scan', lambda token: SignatureScanner().scan(pdf_path, token),
            source=DataSource('image', str(pdf_path)), persist_candidates=True, batch_size=1)
        assert pdf_scan.future.result(15) == 1
        pdf_id = repo.candidate_ids(pdf_scan.id)[0]
        pdf_candidate = repo.candidate(pdf_id)
        assert pdf_candidate['integrity'] == 'pdf_structure_checked'
        assert pdf_candidate['offset'] == len(b'pdf-prefix') and pdf_candidate['size'] == len(pdf_bytes)
        window.open_results(pdf_scan.id); window.result_table.selectRow(0); window.preview_selected()
        assert window.pending is not None
        window.pending[0].future.result(15); window.poll()
        pdf = window.pdf_dialog
        assert pdf is not None and pdf.isVisible() and pdf.pages == 2
        assert (pdf._image.width(), pdf._image.height()) == (768, 1024)
        first_color = pdf._image.pixelColor(100, 500)
        pdf.next_button.click(); pdf._pending[0].result(15); pdf.poll()
        assert pdf.page == 1 and pdf._image.pixelColor(100, 500) != first_color
        pdf.zoom_combo.setCurrentIndex(pdf.zoom_combo.findData(50))
        pdf._pending[0].result(15); pdf.poll()
        assert (pdf._image.width(), pdf._image.height()) == (384, 512)
        pdf.close()
        assert pdf.data == b'' and pdf._image.isNull() and pdf._pending is None
        pdf_export = window.manager.submit('export', lambda token, task_id: export_batch(repo, task_id,
            [pdf_id], window.config.data_dir / 'Recovered', window.config.data_dir, token), with_task_id=True)
        pdf_export.future.result(15)
        pdf_records = repo.exports(pdf_export.id)
        assert len(pdf_records) == 1 and pdf_records[0]['status'] == 'exported'
        assert pdf_records[0]['source_sha256'] == pdf_records[0]['target_sha256'] == hashlib.sha256(pdf_bytes).hexdigest()
        assert Path(pdf_records[0]['output_path']).read_bytes() == pdf_bytes
        assert repo.candidate(pdf_id) == pdf_candidate and pdf_path.read_bytes() == pdf_source_bytes
        result.update(pdf_scan_verified=True, pdf_preview_verified=True, pdf_page_count=2,
                      pdf_verified_exports=1, pdf_worker_verified=True, pdf_buffer_released=True)
        previous_directory = window.config.data_dir
        session = app.workspace_session
        fresh = session.activate(validate_workspace(previous_directory / 'workspace-roundtrip'))
        assert fresh.repository.tasks() == []
        window = session.activate(validate_workspace(previous_directory))
        assert window.repository.candidate_count(scan.id) == 2
        assert len(window.repository.exports(exported.id)) == 2
        assert window.repository.candidate_count(computer.id) == 2
        assert window.repository.candidate_count(video_task) == 1
        assert window.repository.candidate(video_candidate_id) == before_candidate
        assert window.repository.exports(video_export.id) == video_records
        assert clip.read_bytes() == video_image
        assert window.repository.candidate(pdf_id) == pdf_candidate
        assert window.repository.exports(pdf_export.id) == pdf_records
        assert pdf_path.read_bytes() == pdf_source_bytes
        assert source.read_bytes() == original
        result['workspace_roundtrip_verified'] = True
        # Exercise the user-facing diagnostics in the actual packaged process.
        # No raw device, ADB process or source content is accessed by this check.
        from time import monotonic
        from PySide6.QtCore import QThread
        before_tasks = window.repository.tasks()
        before_database = window.config.database.read_bytes()
        report_directory = window.config.data_dir / 'Reports'
        before_reports = set(report_directory.iterdir())
        window.show_environment_diagnostics()
        diagnostics = window.diagnostics_dialog
        assert diagnostics.isVisible()
        def wait_diagnostics():
            deadline = monotonic() + 15
            while window.computer_page.readonly_pending:
                assert monotonic() < deadline, 'Environment diagnostics timed out'
                app.processEvents(); window.poll(); QThread.msleep(10)
        wait_diagnostics()
        assert diagnostics.report['kind'] == 'environment_readiness'
        assert diagnostics.report['manual_acceptance']['status'] == 'not_run'
        assert all(case['status'] == 'not_run' for case in diagnostics.report['manual_acceptance']['cases'])
        diagnostics.save_button.click(); wait_diagnostics()
        new_reports = set(report_directory.iterdir()) - before_reports
        assert len(new_reports) == 1
        saved = json.loads(new_reports.pop().read_text(encoding='utf-8'))
        assert saved == diagnostics.report
        assert str(window.config.data_dir) not in json.dumps(saved, ensure_ascii=False)
        assert window.repository.tasks() == before_tasks
        assert window.config.database.read_bytes() == before_database
        diagnostics.close(); app.processEvents()
        result['environment_diagnostics_verified'] = True
        if screenshot_path:
            window.nav.setCurrentRow(0)
            app.processEvents()
            screenshot = Path(screenshot_path).resolve()
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(screenshot), 'PNG'):
                raise RuntimeError('Cannot save application verification screenshot')
        if app.platformName() == 'cocoa':
            shortcuts = QKeySequence.keyBindings(QKeySequence.StandardKey.Quit)
            assert shortcuts and window.quit_action.shortcuts() == shortcuts
            # Qt's logical Control modifier maps to Command on macOS. Do not
            # infer functionality from the translated shortcut display glyph.
            assert any(key.count() == 1 and key[0].key() == Qt.Key.Key_Q
                       and key[0].keyboardModifiers() == Qt.KeyboardModifier.ControlModifier
                       for key in shortcuts)
            assert window.quit_action.menuRole() == QAction.MenuRole.QuitRole
            assert window.quit_action.shortcutContext() == Qt.ShortcutContext.ApplicationShortcut
            assert not app.testAttribute(Qt.ApplicationAttribute.AA_MacDontSwapCtrlAndMeta)
            assert window.menuBar().isNativeMenuBar()
            window.quit_action.trigger()
            app.processEvents()
            assert not window.isVisible()
            result['native_quit_verified'] = True
        result.update(success=True, verified_exports=2, decoded_formats=['PNG', 'JPEG'])
        exit_code = 0
    except Exception as error:
        result['error_type'] = type(error).__name__
    finally:
        try:
            target = Path(report_path).resolve(); target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, indent=2), encoding='utf-8')
        except OSError:
            exit_code = 1
        window.close()
        app.exit(exit_code)
