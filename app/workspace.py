"""Explicit workspace selection and idle-only handoff; source data is never moved."""
from dataclasses import dataclass
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from app.app_config import AppConfig
from app.logging_config import configure_logging, check_log_targets
from storage.database.repository import Repository, SCHEMA_VERSION


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceSelection:
    path: Path
    source_path: str | None = None
    source_kind: str | None = None
    filesystem_device: int | None = None
    selected_device_path: str | None = None


def _existing_directory(target):
    existing = target
    while not existing.exists():
        if existing.parent == existing:
            raise WorkspaceError('找不到工作目录所在的文件系统，请先连接目标磁盘。')
        existing = existing.parent
    if not existing.is_dir(): raise WorkspaceError('工作目录的上级路径必须是文件夹。')
    return existing


def _check_layout(target):
    directories = ('Devices', 'Backups', 'Acquisitions', 'ScanCache', 'Recovered', 'Reports', 'Logs')
    files = ('index.db', 'index.db-wal', 'index.db-shm', 'index.db-journal', '.deeprecover-session.lock')
    for name in (*directories, *files):
        child = target / name
        if child.is_symlink() or child.resolve() != child:
            raise WorkspaceError('工作目录包含指向其他位置的管理项链接，请选择独立目录。')
        if name in directories and child.exists() and not child.is_dir():
            raise WorkspaceError('工作目录中有同名文件占用了管理目录，请另选目录。')
        if name in files and child.exists() and (not child.is_file() or child.stat().st_nlink > 1):
            raise WorkspaceError('工作目录中的管理文件必须是独立普通文件。')
    try:
        check_log_targets(target / 'Logs')
    except OSError as error:
        raise WorkspaceError('日志文件包含链接、共享文件或无效路径，请选择独立工作目录。') from error


def _check_existing_database(path):
    if not path.exists(): return
    if not path.is_file(): raise WorkspaceError('工作目录中的 index.db 不是普通文件。')
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise WorkspaceError('该工作目录仍有未完成的数据库事务，请关闭其他 DeepRecover 窗口后重试。')
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'tasks', 'candidates'} | ({'sources'} if version >= 2 else set()) | ({'exports'} if version >= 3 else set())
        if version > SCHEMA_VERSION: raise WorkspaceError('工作目录来自更新版本，请升级 DeepRecover 后打开。')
        if version < 1 or not required <= tables:
            raise WorkspaceError('此目录已有其他数据库，请另选独立工作目录。')
    except sqlite3.Error as error:
        raise WorkspaceError('此目录中的 index.db 无法识别，请另选工作目录。') from error


def validate_workspace(path, *, repository=None, current_data_dir=None, source_path=None, source_kind=None,
                       selected_device_path=None):
    """Only inspect paths, known source boundaries and physical mappings."""
    target = Path(path).expanduser().resolve()
    if target.exists() and not target.is_dir(): raise WorkspaceError('工作目录必须是文件夹。')
    for ancestor in (target, *target.parents):
        if ancestor.suffix.casefold() == '.app': raise WorkspaceError('不能把工作目录放在应用程序包内。')
        if (ancestor / 'Manifest.db').exists() or (ancestor / 'Manifest.plist').exists():
            raise WorkspaceError('工作目录不能位于原始 iPhone 备份中。')
    existing = _existing_directory(target)
    _check_layout(target)
    protected = []
    if current_data_dir is not None: protected.append((Path(current_data_dir) / 'Acquisitions').resolve())
    if repository is not None: protected.extend(Path(p).resolve() for p in repository.source_locations('ios_backup'))
    if any(target.is_relative_to(p) for p in protected):
        raise WorkspaceError('工作目录不能位于源备份或采集副本目录中。')
    protected_device = source_path if source_kind == 'device' else selected_device_path
    if protected_device is not None:
        from core.computer.devices import ensure_different_device
        ensure_different_device(protected_device, target)
    _check_existing_database(target / 'index.db')
    return WorkspaceSelection(target, source_path, source_kind, existing.stat().st_dev, selected_device_path)


def startup_workspace(suggested=None):
    default = AppConfig.default().data_dir
    if os.environ.get('DEEPRECOVER_DATA_DIR'):
        return validate_workspace(default)
    from ui.settings.workspace import WorkspaceDialog
    from PySide6.QtWidgets import QMessageBox
    suggested = suggested or default
    while True:
        chosen = WorkspaceDialog.select(suggested)
        if chosen is None: return None
        try:
            return validate_workspace(chosen, current_data_dir=default)
        except (WorkspaceError, OSError, sqlite3.Error) as error:
            QMessageBox.critical(None, '请选择其他工作目录', str(error) if isinstance(error, WorkspaceError)
                else f'无法检查此目录（{type(error).__name__}），请检查权限或选择其他磁盘。')
            suggested = chosen


def request_workspace_change(window):
    from PySide6.QtWidgets import QMessageBox
    from ui.settings.workspace import WorkspaceDialog
    page = window.computer_page
    if window.pending or page.readonly_pending:
        window.statusBar().showMessage('请先结束当前任务或只读预检，再切换工作目录。'); return
    chosen = WorkspaceDialog.select(window.config.data_dir, window)
    if chosen is None: return
    if chosen == window.config.data_dir.resolve():
        window.statusBar().showMessage('已在使用这个工作目录。'); return
    source_path, source_kind = page.source_path, page.source_kind
    selected_device = page.selected_device() if source_kind is None else None
    selected_device_path = selected_device.path if selected_device is not None else None
    def activate(selection):
        if window.pending or page.readonly_pending or (page.source_path, page.source_kind) != (source_path, source_kind):
            window.statusBar().showMessage('来源或任务状态已改变，请重新选择工作目录。'); return
        callback = getattr(window, 'workspace_change_handler', None)
        if not callable(callback):
            window.statusBar().showMessage('此窗口未启用工作目录切换，请重新从应用入口启动。'); return
        if QMessageBox.question(window, '切换工作目录',
            '打开所选目录的已有历史，或建立独立的新工作区。原目录和历史保留，不会自动迁移数据。是否切换？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        try:
            callback(selection)
        except (WorkspaceError, OSError, RuntimeError, sqlite3.Error) as error:
            QMessageBox.critical(window, '工作目录未切换', str(error) if isinstance(error, WorkspaceError)
                else f'无法打开所选工作目录（{type(error).__name__}），原工作目录保持可用。')
    page._readonly(lambda: validate_workspace(chosen, repository=window.repository,
        current_data_dir=window.config.data_dir, source_path=source_path, source_kind=source_kind,
        selected_device_path=selected_device_path),
        activate, '正在只读检查新工作目录与来源隔离，检查通过前不创建目录或数据库…')


class WorkspaceSession:
    def __init__(self, app):
        self.app = app
        self.window = None

    def activate(self, selection):
        from PySide6.QtCore import QLockFile
        from app.main_window import MainWindow
        old = self.window
        if old is not None:
            if old.pending or old.computer_page.readonly_pending:
                raise WorkspaceError('任务或只读预检尚未结束，不能切换工作目录。')
            if (old.computer_page.source_path, old.computer_page.source_kind) != (selection.source_path, selection.source_kind):
                raise WorkspaceError('当前来源与已检查的来源不一致，请重新预检。')
            if selection.selected_device_path is not None:
                selected = old.computer_page.selected_device()
                if selected is None or selected.path != selection.selected_device_path:
                    raise WorkspaceError('当前选中的磁盘已改变，请重新预检。')
            if old.config.data_dir.resolve() == selection.path:
                old.show(); old.raise_(); return old
        if selection.path.resolve() != selection.path:
            raise WorkspaceError('所选目录路径已改变，请重新选择。')
        if (selection.filesystem_device is not None and
                _existing_directory(selection.path).stat().st_dev != selection.filesystem_device):
            raise WorkspaceError('所选目录所在卷已改变，请重新预检。')
        _check_layout(selection.path)
        _check_existing_database(selection.path / 'index.db')
        selection.path.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(selection.path / '.deeprecover-session.lock'))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            if lock.error() == QLockFile.LockError.LockFailedError:
                raise WorkspaceError('此工作目录已由其他 DeepRecover 窗口使用，请关闭该窗口或选择其他目录。')
            raise WorkspaceError('无法锁定此工作目录，请检查目标磁盘的写入权限或选择其他目录。')
        new = None
        try:
            _check_layout(selection.path)
            config = AppConfig(selection.path)
            config.initialize()
            repository = Repository(config.database)
            repository.recover_interrupted()
            new = MainWindow(config, repository)
            new._workspace_lock = lock
            new.workspace_change_handler = self.activate
            logger = configure_logging(config.data_dir / 'Logs')
            new.show()
            if old is not None and not old.close():
                raise WorkspaceError('原窗口未结束当前操作，工作目录未切换。')
        except Exception:
            if new is not None: new.close()
            lock.unlock()
            raise
        self.window = new
        if old is not None: old.deleteLater()
        logger.info('workspace_opened')
        return new
