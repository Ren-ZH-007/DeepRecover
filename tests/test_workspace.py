from contextlib import closing
from dataclasses import replace
import importlib.util
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
from threading import Event
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from app.app_config import AppConfig
from app.logging_config import configure_logging
from app.workspace import WorkspaceError, WorkspaceSession, startup_workspace, validate_workspace
from core.computer.io import DiskError
from storage.database.repository import Repository
from storage.models.source import DataSource


def close_logs():
    logger = logging.getLogger('deeprecover')
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler); handler.close()


class WorkspaceValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_new_directory_preflight_does_not_create_anything(self):
        target = self.root / 'new' / 'workspace'
        selected = validate_workspace(target)
        self.assertEqual(selected.path, target)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_same_raw_source_rejection_happens_before_any_directory_creation(self):
        target = self.root / 'new'
        with patch('core.computer.devices.ensure_different_device', side_effect=DiskError('same disk')) as guard:
            with self.assertRaises(DiskError):
                validate_workspace(target, source_path='/dev/disk99', source_kind='device')
        guard.assert_called_once_with('/dev/disk99', target)
        self.assertFalse(target.exists())

    def test_successful_raw_preflight_binds_source_but_does_not_initialize_database(self):
        target = self.root / 'new'
        with patch('core.computer.devices.ensure_different_device', return_value=True):
            selected = validate_workspace(target, source_path='/dev/disk99', source_kind='device')
        self.assertEqual((selected.source_path, selected.source_kind), ('/dev/disk99', 'device'))
        self.assertFalse(target.exists())

    def test_known_backup_and_collected_copies_cannot_be_workspaces(self):
        current = AppConfig(self.root / 'old'); current.initialize(); repo = Repository(current.database)
        backup = self.root / 'source'; backup.mkdir()
        repo.create_task('backup_index', DataSource('ios_backup', str(backup)))
        for target in [backup / 'new', current.data_dir / 'Acquisitions' / 'new']:
            with self.subTest(target=target), self.assertRaises(WorkspaceError):
                validate_workspace(target, repository=repo, current_data_dir=current.data_dir)
            self.assertFalse(target.exists())

    def test_backup_markers_are_rejected_without_a_registered_history(self):
        source = self.root / 'backup'; source.mkdir()
        sentinel = source / 'Manifest.db'; sentinel.write_bytes(b'original backup')
        with self.assertRaises(WorkspaceError): validate_workspace(source / 'new')
        self.assertEqual(sentinel.read_bytes(), b'original backup')
        self.assertEqual(list(source.iterdir()), [sentinel])

    def test_managed_directory_links_are_rejected_before_initialization(self):
        target = self.root / 'workspace'
        with patch.object(Path, 'is_symlink', autospec=True,
                side_effect=lambda value: value == target / 'Logs'):
            with self.assertRaises(WorkspaceError): validate_workspace(target)
        self.assertFalse(target.exists())

    def test_existing_workspace_database_is_read_without_sidecars_or_content_changes(self):
        config = AppConfig(self.root / 'existing'); config.initialize(); Repository(config.database)
        before = config.database.read_bytes()
        validate_workspace(config.data_dir)
        self.assertEqual(config.database.read_bytes(), before)
        self.assertFalse(Path(str(config.database) + '-wal').exists())
        self.assertFalse(Path(str(config.database) + '-shm').exists())

    def test_managed_path_redirection_is_rejected_even_without_symlink_flag(self):
        target = self.root / 'workspace'; original_resolve = Path.resolve
        def resolve(value, *args, **kwargs):
            return self.root / 'source' if value == target / 'Logs' else original_resolve(value, *args, **kwargs)
        with patch.object(Path, 'resolve', autospec=True, side_effect=resolve), \
                patch.object(Path, 'is_symlink', return_value=False):
            with self.assertRaises(WorkspaceError): validate_workspace(target)
        self.assertFalse(target.exists())

    def test_unknown_and_future_databases_are_rejected_without_modification(self):
        for version in [0, 999]:
            target = self.root / str(version); target.mkdir(); database = target / 'index.db'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE personal_notes(value TEXT)')
                db.execute(f'PRAGMA user_version={version}')
            before = database.read_bytes()
            with self.subTest(version=version), self.assertRaises(WorkspaceError): validate_workspace(target)
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(list(target.iterdir()), [database])

    def test_pending_database_transaction_is_not_checkpointed_or_removed(self):
        config = AppConfig(self.root / 'existing'); config.initialize(); Repository(config.database)
        wal = Path(str(config.database) + '-wal'); wal.write_bytes(b'pending transaction')
        with self.assertRaises(WorkspaceError): validate_workspace(config.data_dir)
        self.assertEqual(wal.read_bytes(), b'pending transaction')

    def test_file_ancestor_cannot_be_used_as_a_directory(self):
        parent = self.root / 'plain-file'; parent.write_bytes(b'keep')
        with self.assertRaises(WorkspaceError): validate_workspace(parent / 'workspace')
        self.assertEqual(parent.read_bytes(), b'keep')

    def test_log_file_links_are_rejected_by_preflight_and_logging(self):
        target = self.root / 'workspace'; logs = target / 'Logs'; logs.mkdir(parents=True)
        original = logs / 'app.log'; original.write_bytes(b'original bytes')
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda value: value == original):
            with self.assertRaises(WorkspaceError): validate_workspace(target)
            with self.assertRaises(OSError): configure_logging(logs)
        self.assertEqual(original.read_bytes(), b'original bytes')

    def test_shared_log_and_database_files_cannot_alias_other_workspaces(self):
        target = self.root / 'workspace'; logs = target / 'Logs'; logs.mkdir(parents=True)
        log = logs / 'app.log'; log.write_bytes(b'keep original')
        database = target / 'index.db'; database.write_bytes(b'keep database')
        original_stat = Path.stat
        for shared in [log, database]:
            def stat(value, *args, **kwargs):
                info = original_stat(value, *args, **kwargs)
                if value == shared:
                    return SimpleNamespace(st_mode=info.st_mode, st_nlink=2, st_size=info.st_size)
                return info
            with self.subTest(shared=shared), patch.object(Path, 'stat', autospec=True, side_effect=stat):
                with self.assertRaises(WorkspaceError): validate_workspace(target)
                if shared == log:
                    with self.assertRaises(OSError): configure_logging(logs)
        self.assertEqual(log.read_bytes(), b'keep original')
        self.assertEqual(database.read_bytes(), b'keep database')


QT_AVAILABLE = importlib.util.find_spec('PySide6') is not None


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')
class WorkspaceUiTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.session = WorkspaceSession(self.app)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.session.window is not None:
            page = self.session.window.computer_page
            if page._future:
                page.cancel()
                try: page._future.result(5)
                except Exception: pass
                self.session.window.poll()
            self.session.window.pending = None
            self.session.window.close()
        self.app.processEvents(); close_logs(); self.temp.cleanup()

    def open_initial(self):
        return self.session.activate(validate_workspace(self.root / 'old'))

    def test_startup_cancel_creates_no_working_data(self):
        from ui.settings.workspace import WorkspaceDialog
        target = self.root / 'cancelled'
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(AppConfig, 'default', return_value=AppConfig(target)), \
                patch.object(WorkspaceDialog, 'select', return_value=None):
            self.assertIsNone(startup_workspace())
        self.assertFalse(target.exists())

    def test_explicit_startup_directory_preserves_environment_workflow(self):
        from ui.settings.workspace import WorkspaceDialog
        target = self.root / 'explicit'
        with patch.dict(os.environ, {'DEEPRECOVER_DATA_DIR': str(target)}), patch.object(WorkspaceDialog, 'select') as choose:
            selected = startup_workspace()
        choose.assert_not_called(); self.assertEqual(selected.path, target); self.assertFalse(target.exists())

    def test_startup_invalid_directory_allows_retry_without_initialization(self):
        from ui.settings.workspace import WorkspaceDialog
        bad = self.root / 'bad'; bad.mkdir(); (bad / 'Manifest.db').write_bytes(b'backup')
        good = self.root / 'good'
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(AppConfig, 'default', return_value=AppConfig(self.root / 'default')), \
                patch.object(WorkspaceDialog, 'select', side_effect=[bad, good]), \
                patch('PySide6.QtWidgets.QMessageBox.critical') as explain:
            selected = startup_workspace()
        explain.assert_called_once(); self.assertEqual(selected.path, good)
        self.assertFalse(good.exists()); self.assertEqual((bad / 'Manifest.db').read_bytes(), b'backup')

    def test_dialog_accepts_new_path_without_creating_it(self):
        from ui.settings.workspace import WorkspaceDialog
        dialog = WorkspaceDialog(self.root / 'new')
        dialog.accept()
        self.assertEqual(dialog.selected_path, self.root / 'new')
        self.assertFalse(dialog.selected_path.exists())
        dialog.close()

    def test_busy_window_does_not_open_directory_picker(self):
        from ui.settings.workspace import WorkspaceDialog
        window = self.open_initial(); window.pending = object()
        with patch.object(WorkspaceDialog, 'select') as choose: window.choose_work_directory()
        choose.assert_not_called(); window.pending = None

    def test_refused_raw_directory_creates_no_task_or_target(self):
        from ui.settings.workspace import WorkspaceDialog
        window = self.open_initial(); page = window.computer_page
        page._set_source('/dev/disk99', 'device'); target = self.root / 'refused'
        with patch.object(WorkspaceDialog, 'select', return_value=target), \
                patch('core.computer.devices.ensure_different_device', side_effect=DiskError('same disk')):
            window.choose_work_directory()
            with self.assertRaises(DiskError): page._future.result(5)
            window.poll()
        self.assertFalse(target.exists()); self.assertEqual(window.repository.tasks(), [])
        self.assertIs(self.session.window, window)

    def test_switch_opens_new_workspace_without_copying_history_or_logging_to_old_directory(self):
        from PySide6.QtWidgets import QMessageBox
        from ui.settings.workspace import WorkspaceDialog
        window = self.open_initial(); old = window.config
        task = window.repository.create_task('fixture'); window.repository.update_task(task, 'completed')
        database_before = old.database.read_bytes()
        log_before = (old.data_dir / 'Logs/app.log').read_bytes()
        target = self.root / 'new'
        with patch.object(WorkspaceDialog, 'select', return_value=target), \
                patch('PySide6.QtWidgets.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            window.choose_work_directory(); window.computer_page._future.result(5); window.poll()
        new = self.session.window
        self.assertEqual(new.config.data_dir, target); self.assertTrue(new.isVisible())
        self.assertEqual(new.repository.tasks(), [])
        self.assertEqual(old.database.read_bytes(), database_before)
        self.assertEqual((old.data_dir / 'Logs/app.log').read_bytes(), log_before)
        self.assertIn(b'workspace_opened', (target / 'Logs/app.log').read_bytes())

    def test_existing_workspace_reopens_its_own_history(self):
        self.open_initial(); target = AppConfig(self.root / 'existing'); target.initialize()
        repo = Repository(target.database); task = repo.create_task('existing'); repo.update_task(task, 'completed')
        window = self.session.activate(validate_workspace(target.data_dir))
        self.assertEqual(window.repository.tasks()[0]['id'], task)

    def test_source_change_after_preflight_blocks_workspace_initialization(self):
        window = self.open_initial(); selected = validate_workspace(self.root / 'new')
        window.computer_page._set_source('/dev/disk99', 'device')
        with self.assertRaises(WorkspaceError): self.session.activate(selected)
        self.assertFalse(selected.path.exists())

    def test_same_path_with_changed_managed_layout_is_rechecked_before_writes(self):
        self.open_initial(); selected = validate_workspace(self.root / 'new')
        with patch.object(Path, 'is_symlink', autospec=True,
                side_effect=lambda value: value == selected.path / 'Logs'):
            with self.assertRaises(WorkspaceError): self.session.activate(selected)
        self.assertFalse(selected.path.exists())

    def test_changed_filesystem_is_rejected_before_workspace_creation(self):
        self.open_initial(); selected = validate_workspace(self.root / 'new')
        selected = replace(selected, filesystem_device=selected.filesystem_device + 1)
        with self.assertRaises(WorkspaceError): self.session.activate(selected)
        self.assertFalse(selected.path.exists())

    def test_declining_switch_keeps_old_workspace_and_creates_no_target(self):
        from PySide6.QtWidgets import QMessageBox
        from ui.settings.workspace import WorkspaceDialog
        window = self.open_initial(); target = self.root / 'declined'
        with patch.object(WorkspaceDialog, 'select', return_value=target), \
                patch('PySide6.QtWidgets.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
            window.choose_work_directory(); window.computer_page._future.result(5); window.poll()
        self.assertIs(self.session.window, window); self.assertFalse(target.exists())

    def test_second_session_cannot_recover_another_sessions_running_task(self):
        window = self.open_initial(); release = Event()
        handle = window.manager.submit('fixture', lambda token: release.wait(10))
        try:
            # No SQL transaction is held while this worker is waiting.
            with self.assertRaises(WorkspaceError):
                WorkspaceSession(self.app).activate(validate_workspace(window.config.data_dir))
            self.assertFalse(handle.future.done())
            self.assertIn(window.repository.task(handle.id)['status'], ('queued', 'running'))
        finally:
            release.set(); handle.future.result(10)

    def test_normal_close_releases_workspace_for_a_new_session(self):
        window = self.open_initial(); target = window.config.data_dir
        self.assertTrue((target / '.deeprecover-session.lock').exists())
        window.close()
        self.assertFalse((target / '.deeprecover-session.lock').exists())
        second = WorkspaceSession(self.app)
        try:
            reopened = second.activate(validate_workspace(target))
            self.assertTrue(reopened.isVisible())
        finally:
            if second.window is not None: second.window.close()

    def test_failed_activation_releases_its_new_lock(self):
        self.open_initial(); target = self.root / 'failed'
        with patch('app.workspace.configure_logging', side_effect=OSError('denied')):
            with self.assertRaises(OSError): self.session.activate(validate_workspace(target))
        self.assertFalse((target / '.deeprecover-session.lock').exists())
        self.assertTrue(self.session.window.isVisible())
        self.session.activate(validate_workspace(target))

    def test_selected_raw_disk_is_protected_before_partition_probe(self):
        from core.computer.devices import DiskDevice
        from ui.settings.workspace import WorkspaceDialog
        window = self.open_initial(); page = window.computer_page
        page.set_devices([DiskDevice('/dev/disk99', 'test disk', 1000)])
        page.device_tree.setCurrentItem(page.device_item('/dev/disk99'))
        self.assertIsNone(page.source_path)
        target = self.root / 'refused'
        with patch.object(WorkspaceDialog, 'select', return_value=target), \
                patch('core.computer.devices.ensure_different_device', side_effect=DiskError('same disk')) as guard:
            window.choose_work_directory()
            with self.assertRaises(DiskError): page._future.result(5)
            window.poll()
        guard.assert_called_once_with('/dev/disk99', target)
        self.assertFalse(target.exists()); self.assertEqual(window.repository.tasks(), [])
