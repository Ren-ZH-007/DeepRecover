"""Local environment observations and explicit, guarded report saving."""
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, qVersion
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget)
from shiboken6 import isValid

from app.theme import STYLE
from app.workspace import validate_workspace
from core.diagnostics import (DiagnosticsError, MANUAL_CASES,
    collect_environment, disk_snapshot, write_environment_report)


CHECK_ERROR = '本地环境检查未完成，请检查工作目录权限后重试。'
SAVE_ERROR = '诊断报告未保存，无法确认工作目录安全，请重新检查或选择其他工作目录。'
BUSY_MESSAGE = '请先结束当前任务或只读预检，再进行本地环境检查或保存报告。'


def _label(text, name=None):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    return label


def _identity(path):
    path = Path(path)
    if path.resolve() != path or path.is_symlink() or not path.is_dir():
        raise DiagnosticsError(SAVE_ERROR)
    info = path.stat()
    return info.st_dev, info.st_ino


def _safe(operation, message):
    """The shared preflight presenter must never receive raw diagnostic errors."""
    try:
        return operation()
    except Exception:
        raise DiagnosticsError(message) from None


def _disk_text(disks):
    if disks['status'] != 'detected':
        return '尚未检测；可先在“电脑恢复”页检测磁盘后再检查'
    volumes = disks['volumes']
    unmapped = sum(v['physical_mapping'] in ('unmapped', 'unknown') for v in volumes)
    count = disks['physical_disk_count']
    text = f"{count if count is not None else '未知数量的'} 块物理盘 · {len(volumes)} 个磁盘 / 卷"
    return text + (f' · {unmapped} 个未能确认所在物理盘' if unmapped else ' · 均已确认所在物理盘')


def _error_text(errors):
    if errors['status'] != 'collected':
        return '未能读取本地任务记录'
    tasks = sum(row['count'] for row in errors['tasks'])
    exports = sum(row['count'] for row in errors['exports'])
    if not tasks and not exports:
        return '没有失败、中断或取消的任务记录'
    rows = [row for row in errors['tasks'] + errors['exports'] if row['error'] not in ('none', 'unknown')]
    common = max(rows, key=lambda row: row['count'])['error'] if rows else None
    return f'{tasks} 次任务未成功 · {exports} 项导出未成功' + (f' · 最常见：{common}' if common else '')


def _size(value):
    if type(value) is not int or value < 0:
        return '未知'
    return f'{value / (1024 ** 3):.1f} GiB'


class DiagnosticsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.report = None
        self.saved_path = None
        self._report_workspace = None
        self._report_identity = None
        self._generation = 0
        self._closed = False
        self._owned_future = None
        self.setWindowTitle('本地环境检查与实机验收准备')
        self.setStyleSheet(STYLE)
        self.resize(760, 760)
        self.setMinimumSize(520, 420)
        box = QVBoxLayout(self)
        box.setContentsMargins(20, 18, 20, 18)
        box.addWidget(_label('检查本机环境，准备实机验收', 'title'))
        box.addWidget(_label('仅查询系统与工作目录的基本信息，不检测设备、不扫描备份，也不打开来源内容。磁盘映射取自“电脑恢复”页最近一次检测，错误类型取自本地任务记录，均不含名称、路径或序列号。', 'subtitle'))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        sections = QVBoxLayout(content)
        sections.setContentsMargins(0, 0, 0, 0)
        sections.setSpacing(12)
        card = QFrame()
        card.setObjectName('card')
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setColumnStretch(1, 1)
        self.values = {}
        for row, (key, text) in enumerate((
            ('version', '应用版本'), ('system', '系统与架构'),
            ('runtime', '启动形式'), ('qt', '界面运行环境'),
            ('screens', '屏幕观测'), ('workspace', '工作目录容量'),
            ('adb', 'Android 工具'), ('disks', '磁盘映射'),
            ('errors', '错误类型'),
        )):
            grid.addWidget(_label(text, 'sectionNote'), row, 0)
            self.values[key] = _label('尚未检查')
            self.values[key].setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(self.values[key], row, 1)
        sections.addWidget(card)
        pending = QFrame()
        pending.setObjectName('card')
        pending_box = QVBoxLayout(pending)
        pending_box.setContentsMargins(16, 14, 16, 14)
        pending_box.addWidget(_label('M5 实机验收 · 尚未执行', 'sectionTitle'))
        for _, title in MANUAL_CASES:
            pending_box.addWidget(_label(f'○  {title}：未执行', 'sectionNote'))
        pending_box.addWidget(_label('环境信息可读、工具已定位或容量充足，都不代表真实设备、安装体验或数据恢复验收通过。', 'sectionNote'))
        sections.addWidget(pending)
        sections.addWidget(_label('报告仅保存在当前工作目录的 Reports 中，不含完整路径、设备序列号、卷名、屏幕名称或日志内容；应用不会上传报告。', 'sectionNote'))
        sections.addStretch(1)
        scroll.setWidget(content)
        box.addWidget(scroll, 1)
        self.status_label = _label('点击检查后读取本机基本信息。', 'sourceInfo')
        box.addWidget(self.status_label)
        row = QHBoxLayout()
        self.check_button = QPushButton('重新检查')
        self.check_button.setProperty('kind', 'secondary')
        self.check_button.clicked.connect(self.start_check)
        row.addWidget(self.check_button)
        row.addStretch(1)
        self.save_button = QPushButton('保存脱敏报告')
        self.save_button.setProperty('kind', 'primary')
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_report)
        row.addWidget(self.save_button)
        close = QPushButton('关闭')
        close.clicked.connect(self.close)
        row.addWidget(close)
        box.addLayout(row)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_buttons)
        self._timer.start(100)

    def _alive(self, generation=None):
        return isValid(self) and not self._closed and (generation is None or generation == self._generation)

    def _busy(self):
        return self.window.pending is not None or self.window.computer_page.readonly_pending

    def _refresh_buttons(self):
        if not self._alive():
            return
        idle = not self._busy()
        self.check_button.setEnabled(idle)
        self.save_button.setEnabled(idle and self.report is not None)

    def _source_binding(self):
        page = self.window.computer_page
        selected = page.selected_device()
        return page.source_path, page.source_kind, selected.path if selected is not None else None

    def start_check(self):
        if not self._alive():
            return
        if self._busy():
            self.status_label.setText(BUSY_MESSAGE)
            self._refresh_buttons()
            return
        self._generation += 1
        generation = self._generation
        self.report = None
        self.saved_path = None
        self._report_workspace = Path(self.window.config.data_dir)
        try:
            self._report_identity = _identity(self._report_workspace)
        except (OSError, RuntimeError, ValueError):
            self._report_identity = None
        for label in self.values.values():
            label.setText('正在检查…')
        app = QApplication.instance()
        screens = app.screens()
        display = {'version': qVersion(), 'platform': app.platformName(),
            'screen_count': len(screens),
            'high_dpi_screens': sum(screen.devicePixelRatio() > 1 for screen in screens)}
        workspace = self._report_workspace
        page = self.window.computer_page
        # Copy on the GUI thread; an empty list means no detection has run yet.
        disks = disk_snapshot(page.devices) if page.devices else None
        repository = self.window.repository

        def errors():
            try:
                return repository.error_summary()
            except Exception:
                return None

        def complete(report):
            if not self._alive(generation):
                return
            self._owned_future = None
            self.report = report
            self._show_report(report)
            self.status_label.setText('环境信息已读取；M5 实机验收尚未执行。可保存脱敏报告。')
            self._refresh_buttons()

        def failed(_):
            if not self._alive(generation):
                return
            self._owned_future = None
            self.report = None
            for label in self.values.values():
                label.setText('未完成检查')
            self.status_label.setText(CHECK_ERROR)
            self._refresh_buttons()

        self.status_label.setText('正在只读查询本机环境；不会建立扫描或采集任务。')
        page._readonly(lambda: _safe(lambda: collect_environment(workspace, display=display, disks=disks, errors=errors()), CHECK_ERROR),
            complete, '正在检查本机基本环境，不检测设备或扫描备份…', failed)
        self._owned_future = page._future
        self._refresh_buttons()

    def _show_report(self, report):
        system = report['system']
        qt = report['qt']
        workspace = report['workspace']
        names = {'win32': 'Windows', 'darwin': 'macOS', 'linux': 'Linux', 'unknown': '未知系统'}
        self.values['version'].setText(report['app_version'])
        self.values['system'].setText(f"{names.get(system['platform'], '未知系统')} {system['os_version']} · {system['architecture']}")
        self.values['runtime'].setText(('独立应用包' if system['packaged'] else 'Python 源码启动') + f" · Python {system['python_version']}")
        self.values['qt'].setText(f"Qt {qt['version']} · {qt['platform']}")
        count, high_dpi = qt['screen_count'], qt['high_dpi_screens']
        self.values['screens'].setText(f'{count} 个屏幕 · {high_dpi} 个缩放屏幕（仅当前观测）' if count is not None and high_dpi is not None else '屏幕信息未知')
        self.values['workspace'].setText(f"可用 {_size(workspace['free_bytes'])} / 总容量 {_size(workspace['total_bytes'])}；未测试写入" if workspace['status'] == 'available' else '容量信息无法读取；未测试写入')
        self.values['adb'].setText('已定位 ADB 工具；未执行、未检测连接设备' if report['adb']['located'] else '未定位 ADB 工具；手机连接与授权未检测')
        self.values['disks'].setText(_disk_text(report['disks']))
        self.values['errors'].setText(_error_text(report['errors']))

    def save_report(self):
        if not self._alive():
            return
        if self._busy():
            self.status_label.setText(BUSY_MESSAGE)
            self._refresh_buttons()
            return
        if self.report is None:
            self.status_label.setText('请先完成本地环境检查。')
            return
        generation = self._generation
        workspace = self._report_workspace
        report_identity = self._report_identity
        binding = self._source_binding()
        report = deepcopy(self.report)
        page = self.window.computer_page

        def preflight():
            if workspace != Path(self.window.config.data_dir) or _identity(workspace) != report_identity:
                raise DiagnosticsError(SAVE_ERROR)
            return validate_workspace(workspace, repository=self.window.repository,
                current_data_dir=workspace, source_path=binding[0], source_kind=binding[1],
                selected_device_path=binding[2] if binding[1] is None else None)

        def complete(selection):
            if not self._alive(generation):
                return
            self._owned_future = None
            try:
                if self._busy() or binding != self._source_binding() or workspace != Path(self.window.config.data_dir):
                    raise DiagnosticsError(SAVE_ERROR)
                identity = _identity(workspace)
                if identity != report_identity or identity[0] != selection.filesystem_device:
                    raise DiagnosticsError(SAVE_ERROR)
                self.saved_path = write_environment_report(workspace, report)
            except Exception:
                self.saved_path = None
                self.status_label.setText(SAVE_ERROR)
            else:
                self.status_label.setText(f'已保存到 Reports/{self.saved_path.name}。报告未上传，实机验收仍未执行。')
            self._refresh_buttons()

        def failed(_):
            if not self._alive(generation):
                return
            self._owned_future = None
            self.saved_path = None
            self.status_label.setText(SAVE_ERROR)
            self._refresh_buttons()

        self.status_label.setText('正在检查报告保存位置与当前来源隔离…')
        page._readonly(lambda: _safe(preflight, SAVE_ERROR), complete,
            '正在只读检查诊断报告保存位置，检查通过前不写入报告…', failed)
        self._owned_future = page._future
        self._refresh_buttons()

    def done(self, result):
        self._closed = True
        self._generation += 1
        self._timer.stop()
        page = self.window.computer_page
        if self._owned_future is not None and page._future is self._owned_future:
            page.cancel()
        super().done(result)
