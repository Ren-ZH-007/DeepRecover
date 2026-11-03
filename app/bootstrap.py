import sys
import argparse
import os
import tempfile
import logging
from app.app_config import AppConfig
from app.logging_config import configure_logging
from storage.database.repository import Repository
from app.version import VERSION

def main():
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
        from app.main_window import MainWindow
    except ImportError:
        print("请先运行 python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    app = QApplication([sys.argv[0]] if smoke_report else sys.argv)
    app.setApplicationName("DeepRecover")
    app.setOrganizationName("DeepRecover")
    app.setApplicationVersion(VERSION)
    try:
        config = AppConfig.default()
        config.initialize()
        logger = configure_logging(config.data_dir / "Logs")
        repository = Repository(config.database)
        repository.recover_interrupted()
        window = MainWindow(config, repository)
    except Exception as error:
        QMessageBox.critical(None, "DeepRecover 启动失败", f"无法初始化工作目录或数据库（{type(error).__name__}）。请检查目录权限及版本。")
        return 1
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
        assert window.isVisible() and window.stack.count() == 7
        for index in range(7):
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
