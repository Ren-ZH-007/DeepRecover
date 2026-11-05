import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QColor, QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QMainWindow, QPushButton, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QInputDialog, QMessageBox, QProgressBar, QComboBox, QSplitter, QFrame, QListWidgetItem, QScrollArea, QDialog)
from app.navigation import PAGES
from core.tasks import TaskManager, TaskCancelled
from core.device.android.adapter import AndroidAdapter, AdbError
from core.device.ios.discovery import discover_backups
from core.parser.ios_backup.manifest import iter_manifest, BackupError
from core.acquisition.android import acquire_files
from storage.models.source import DataSource
from core.scanner.signature.scanner import SignatureScanner, ScanError
from core.candidate_io import CandidateAccessError
from core.preview.images import preview_image
from core.preview.video import prepare_video
from storage.models.classification import category
from core.export.service import export_batch, ExportError
from ui.recovery.export_history import ExportHistory
from ui.recovery.presentation import (candidate_status, content_check, deletion_evidence, video_check_details,
    export_guidance, task_status, task_kind, source_kind, error_text)
from ui.computer.page import ComputerPage, check_computer_access, computer_error_types
from app.theme import STYLE, icon, brand_icon
from app.version import VERSION


def selection_modifier(platform=None):
    return 'Command / Shift' if (platform or sys.platform) == 'darwin' else 'Ctrl / Shift'


class MainWindow(QMainWindow):
    def choose_work_directory(self):
        from app.workspace import request_workspace_change
        return request_workspace_change(self)

    def show_environment_diagnostics(self):
        if self.pending or self.computer_page.readonly_pending:
            self.statusBar().showMessage('请先结束当前任务或只读预检，再检查本机环境。'); return
        if self.diagnostics_dialog and self.diagnostics_dialog.isVisible():
            self.diagnostics_dialog.raise_(); self.diagnostics_dialog.activateWindow(); return
        if self.diagnostics_dialog:
            self.diagnostics_dialog.deleteLater()
        from ui.settings.diagnostics import DiagnosticsDialog
        self.diagnostics_dialog = DiagnosticsDialog(self)
        self.diagnostics_dialog.show()
        self.diagnostics_dialog.start_check()

    def __init__(self, config, repository):
        super().__init__()
        self.config, self.repository = config, repository
        self.manager = TaskManager(repository)
        self.pending = None
        self.backups = []
        self.devices = []
        self.android_files = []
        self.listed_device = None
        self.result_task_id = None
        self.result_offset = 0
        self.page_size = 100
        self.history = []
        self.scan_source = None
        self.current_candidates = []
        self.preview_candidate_id = None
        self.export_dialog = None
        self.evidence_dialog = None
        self.diagnostics_dialog = None
        self.video_dialog = None
        self.setWindowTitle(f'DeepRecover · 本地数据工作台 · {VERSION}')
        self.setWindowIcon(brand_icon())
        self.resize(1280, 820)
        self.setMinimumSize(880, 620)
        self.setStyleSheet(STYLE)
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame(); sidebar.setObjectName('sideBar'); sidebar.setFixedWidth(208)
        sidebox = QVBoxLayout(sidebar); sidebox.setContentsMargins(18,28,18,20); sidebox.setSpacing(14)
        brand = QHBoxLayout(); sidebox.addLayout(brand)
        mark = QLabel(); mark.setPixmap(brand_icon().pixmap(32,32)); brand.addWidget(mark)
        words = QVBoxLayout(); brand.addLayout(words)
        name = QLabel('DeepRecover'); name.setObjectName('brand'); words.addWidget(name)
        tagline = QLabel('本地数据工作台'); tagline.setObjectName('brandSubtitle'); words.addWidget(tagline)
        section = QLabel('工作空间'); section.setObjectName('sidebarSection'); sidebox.addWidget(section)
        self.nav = QListWidget()
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for label, name in zip(PAGES,('home','android','iphone','scan','results','history','settings','disk')):
            self.nav.addItem(QListWidgetItem(icon(name),label))
        sidebox.addWidget(self.nav,1)
        footer = QLabel(f'●  本地处理 · 源数据只读\nv{VERSION}  /  Preview'); footer.setObjectName('sidebarFooter'); sidebox.addWidget(footer)
        self.stack = QStackedWidget()
        layout.addWidget(sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(container)
        self.build_pages()
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentRowChanged.connect(self.refresh_history)
        self.nav.currentRowChanged.connect(lambda index: self.stop_video_preview() if index != 4 else None)
        self.nav.setCurrentRow(0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().showMessage('就绪  ·  数据仅在本机处理')
        self.setup_application_menu()

    def setup_application_menu(self):
        self.quit_action = QAction('退出 DeepRecover', self)
        self.quit_action.setObjectName('quitApplication')
        self.quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        self.quit_action.setShortcuts(QKeySequence.StandardKey.Quit)
        self.quit_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.quit_action.triggered.connect(self.request_quit)
        self.addAction(self.quit_action)
        self.application_menu = None
        if sys.platform == 'darwin':
            # Cocoa relocates QuitRole into the native application menu. The
            # action must remain ours so Command+Q respects closeEvent below.
            self.menuBar().setNativeMenuBar(True)
            self.application_menu = self.menuBar().addMenu('文件')
            self.application_menu.addAction(self.quit_action)

    def request_quit(self):
        # Never bypass cancellation/wait protection with QApplication.quit().
        # Accepted closure exits via Qt's default quitOnLastWindowClosed.
        self.close()

    def page(self, title, subtitle, *, scrollable=False):
        page = QWidget()
        page.setObjectName('page')
        box = QVBoxLayout(page)
        box.setContentsMargins(24, 22, 24, 18)
        box.setSpacing(10)
        eyebrow = QLabel('DEEPRECOVER  /  WORKSPACE'); eyebrow.setObjectName('pageEyebrow'); box.addWidget(eyebrow)
        label = QLabel(title); label.setObjectName('title'); box.addWidget(label)
        label = QLabel(subtitle); label.setWordWrap(True); label.setObjectName('subtitle'); box.addWidget(label)
        if scrollable:
            # Text-heavy pages must retain readable controls at 880×620 and
            # when source paths wrap onto several lines.
            scroll = QScrollArea()
            scroll.setObjectName('pageScroll')
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)
        else:
            self.stack.addWidget(page)
        return box

    def table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setDefaultSectionSize(40)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        return table

    def fill(self, table, rows):
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                item = QTableWidgetItem(str(value)); item.setToolTip(str(value))
                if value in ('completed','exported','structure_checked','metadata_checked','device'): item.setForeground(QColor('#087c68'))
                elif value in ('failed','missing','invalid_structure','unauthorized'): item.setForeground(QColor('#c53f53'))
                elif value in ('partial','truncated','validation_limit','cancelled','interrupted'): item.setForeground(QColor('#a86608'))
                table.setItem(i, j, item)

    @staticmethod
    def explain_item(item, display):
        item.setText(display.label)
        item.setToolTip(display.tooltip)
        item.setForeground(QColor({'info':'#356fbd','warning':'#a86608',
            'danger':'#c53f53','neutral':'#536d8d'}.get(display.tone, '#536d8d')))

    def button(self, box, label, callback, kind='secondary', symbol=None):
        button = QPushButton(label); button.setProperty('kind',kind)
        if symbol: button.setIcon(icon(symbol,color='#ffffff' if kind=='primary' else '#52708e'))
        button.clicked.connect(callback); box.addWidget(button)
        return button

    def card(self, box, title=None):
        frame = QFrame(); frame.setObjectName('card'); box.addWidget(frame)
        inner = QVBoxLayout(frame); inner.setContentsMargins(18,16,18,16); inner.setSpacing(10)
        if title:
            label = QLabel(title); label.setObjectName('sectionTitle'); inner.addWidget(label)
        return inner

    def build_pages(self):
        box = self.page('让数据处理，更有条理', '连接授权设备或打开本地备份，从来源记录到校验导出，全程在本机完成。', scrollable=True)
        hero = QFrame(); hero.setObjectName('heroCard'); box.addWidget(hero)
        hero_box = QVBoxLayout(hero); hero_box.setContentsMargins(26,24,26,24); hero_box.setSpacing(12)
        badge = QLabel('●  LOCAL FIRST   /   本地优先'); badge.setObjectName('badge'); badge.setMaximumWidth(240); hero_box.addWidget(badge)
        headline = QLabel('从一个可靠的数据源开始'); headline.setObjectName('heroTitle'); hero_box.addWidget(headline)
        note = QLabel('只读索引与图片候选扫描，保留来源线索。\n预览确认后，按需导出并逐项核对 SHA-256。'); note.setObjectName('heroText'); note.setWordWrap(True); hero_box.addWidget(note)
        actions = QHBoxLayout(); hero_box.addLayout(actions)
        self.button(actions,'打开本地备份',lambda: self.nav.setCurrentRow(2),'primary','folder')
        self.button(actions,'扫描本地镜像',lambda: self.nav.setCurrentRow(3),'secondary','scan')
        self.computer_home_button = self.button(actions,'电脑磁盘恢复',lambda: self.nav.setCurrentRow(7),'secondary','disk'); actions.addStretch()
        cards = QHBoxLayout(); cards.setSpacing(12); box.addLayout(cards)
        for number,title,description,symbol in [('01','连接与采集','选择已授权设备或本地备份','android'),('02','定位与预览','按类型筛选，查看 JPG / PNG','image'),('03','校验与导出','独立批次，源 / 目标摘要对照','check')]:
            frame = QFrame(); frame.setObjectName('card'); cards.addWidget(frame,1)
            inner = QVBoxLayout(frame); inner.setContentsMargins(18,18,18,18); inner.setSpacing(10)
            mark = QLabel(); mark.setPixmap(icon(symbol,26,'#2563eb').pixmap(26,26)); inner.addWidget(mark)
            label = QLabel(number); label.setObjectName('metricValue'); inner.addWidget(label)
            label = QLabel(title); label.setObjectName('sectionTitle'); inner.addWidget(label)
            label = QLabel(description); label.setObjectName('sectionNote'); label.setWordWrap(True); inner.addWidget(label)
        help_box = self.card(box,'把每一步，都留在可追溯的记录中')
        label = QLabel('来源、路径、扫描偏移与导出校验记录会保存在本地工作目录。任务结束后，可在「任务记录」重新打开结果。'); label.setWordWrap(True); label.setObjectName('sectionNote'); help_box.addWidget(label)
        box.addStretch(1)
        notice = QLabel('能力边界  ·  普通文件复制与候选定位，不等于已删除数据恢复。'); notice.setObjectName('sectionNote'); notice.setWordWrap(True); box.addWidget(notice)

        box = self.page('连接 Android 设备', '先在手机上允许 USB 调试，再选择共享目录和需要采集的普通文件。')
        row = QHBoxLayout(); box.addLayout(row)
        self.button(row,'检测设备',self.detect_android,'primary','refresh'); self.button(row,'选择共享目录',self.list_android_files,'secondary','folder'); row.addStretch()
        self.android_table = self.table(['设备序列号','设备型号','连接状态']); box.addWidget(self.android_table,1)
        self.android_list_source = QLabel('尚未列出目录。选择已授权设备后，点击「选择共享目录」。'); self.android_list_source.setObjectName('sourceInfo')
        self.android_list_source.setTextFormat(Qt.TextFormat.PlainText); self.android_list_source.setWordWrap(True); box.addWidget(self.android_list_source)
        self.android_file_table = self.table([f'普通文件路径 · {selection_modifier()} 可多选']); self.android_file_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); box.addWidget(self.android_file_table,2)
        row = QHBoxLayout(); box.addLayout(row)
        self.button(row,'采集选中文件',self.collect_android,'primary','export'); row.addStretch()
        label = QLabel('仅使用只读 ADB pull，不执行 root、写入或删除设备文件。'); label.setObjectName('sectionNote'); label.setWordWrap(True); row.addWidget(label,1)

        box = self.page('打开 iPhone 本地备份', '发现 Finder / Apple Devices / iTunes 备份，只读解析未加密且已完成的备份索引。')
        row = QHBoxLayout(); box.addLayout(row)
        self.button(row,'自动发现',lambda: self.detect_backups(None),'secondary','refresh')
        self.button(row,'选择备份目录',self.choose_backup,'secondary','folder')
        self.button(row,'读取选中索引',self.parse_backup,'primary','play'); row.addStretch()
        self.backup_table = self.table(['设备名称','iOS 版本','加密状态','备份时间','来源目录']); box.addWidget(self.backup_table,1)
        info = self.card(box,'保留原始备份，不改变源数据')
        label = QLabel('支持稳定的未加密 Manifest.db；有未合并事务日志时会明确拒绝读取。来源目录和索引结果保存在本地，可在历史中回读。'); label.setObjectName('sectionNote'); label.setWordWrap(True); info.addWidget(label)

        box = self.page('扫描本地镜像', '定位连续 JPG / PNG / MP4 / MOV 签名，记录来源、偏移与检查结果。', scrollable=True)
        source_box = self.card(box,'01   选择扫描源')
        row = QHBoxLayout(); source_box.addLayout(row)
        self.button(row,'选择镜像或文件',self.choose_scan_source,'secondary','folder'); row.addStretch()
        self.scan_source_label = QLabel('尚未选择源文件'); self.scan_source_label.setObjectName('sourceInfo'); self.scan_source_label.setTextFormat(Qt.TextFormat.PlainText); self.scan_source_label.setWordWrap(True); self.scan_source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); source_box.addWidget(self.scan_source_label)
        work = self.card(box,'02   开始只读扫描')
        row = QHBoxLayout(); work.addLayout(row)
        self.scan_start_button = self.button(row,'开始图片 / 视频扫描',self.start_scan,'primary','play'); self.scan_start_button.setEnabled(False)
        self.button(row,'停止扫描',self.cancel_scan,'danger','stop'); row.addStretch()
        self.scan_progress = QProgressBar(); self.scan_progress.setRange(0,1000); self.scan_progress.setValue(0); work.addWidget(self.scan_progress)
        self.scan_status = QLabel('等待选择数据源。扫描结果将自动打开到候选索引。'); self.scan_status.setObjectName('sectionNote'); self.scan_status.setWordWrap(True); work.addWidget(self.scan_status)
        limits = self.card(box,'检查范围')
        label = QLabel('连续图片 / 视频候选  ·  单项检查上限 64 MiB  ·  PNG 解压上限 256 MiB  ·  最多 10,000 项'); label.setObjectName('sectionNote'); label.setWordWrap(True); limits.addWidget(label)
        label = QLabel('视频限自包含的单 H.264 视频轨及可选 AAC 音轨；不重组碎片。通过检查不代表完整解码，也不能判断文件是否已删除。'); label.setObjectName('sectionNote'); label.setWordWrap(True); limits.addWidget(label)
        box.addStretch(1)

        box = self.page('查看候选结果', '筛选与预览后，按需导出；来源记录、候选状态和导出记录分别保留。')
        self.result_source = QLabel('从任务记录打开结果，或先完成一次采集 / 索引 / 扫描。'); self.result_source.setObjectName('sourceInfo')
        self.result_source.setWordWrap(True); self.result_source.setMaximumHeight(64); self.result_source.setTextFormat(Qt.TextFormat.PlainText); self.result_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); box.addWidget(self.result_source)
        row = QHBoxLayout(); box.addLayout(row)
        self.category_filter = QComboBox()
        for label,value in [('全部类型','all'),('图片','image'),('视频','video'),('音频','audio'),('文档','document'),('其他','other')]: self.category_filter.addItem(label,value)
        row.addWidget(self.category_filter)
        self.status_filter = QComboBox()
        for label,value in [('全部状态','all'),('候选','candidate'),('部分 / 受限','partial'),('失败','failed')]: self.status_filter.addItem(label,value)
        row.addWidget(self.status_filter)
        self.result_selection_hint = QLabel(f'{selection_modifier()} 多选'); self.result_selection_hint.setObjectName('sectionNote'); row.addWidget(self.result_selection_hint)
        row.addStretch()
        self.result_page_label = QLabel('0 条'); self.result_page_label.setObjectName('sectionNote'); row.addWidget(self.result_page_label)
        self.category_filter.currentIndexChanged.connect(self.filter_results); self.status_filter.currentIndexChanged.connect(self.filter_results)
        splitter = QSplitter(); box.addWidget(splitter,1)
        self.result_table = self.table(['文件名','类型','大小 / 字节','内容检查','原始路径','本地文件','来源范围 SHA-256','采集时间 / UTC','源内偏移 / 字节','候选状态','文件系统','删除依据','来源范围数'])
        self.result_table.setToolTip(f'{selection_modifier()} 可多选本页记录；分页或筛选会重置当前选择。')
        self.result_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); splitter.addWidget(self.result_table)
        preview = QFrame(); preview.setObjectName('previewPanel'); preview_box = QVBoxLayout(preview); preview_box.setContentsMargins(14,14,14,14); splitter.addWidget(preview)
        label = QLabel('图片 / 视频预览'); label.setObjectName('sectionTitle'); preview_box.addWidget(label)
        self.preview_label = QLabel('选择 JPG/PNG 图片\n或 MP4/MOV 视频'); self.preview_label.setObjectName('previewCanvas')
        self.preview_label.setWordWrap(True); self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview_label.setTextFormat(Qt.TextFormat.PlainText); self.preview_label.setMinimumWidth(210)
        self.result_table.itemSelectionChanged.connect(self.clear_preview); preview_box.addWidget(self.preview_label,1)
        self.preview_button = self.button(preview_box,'预览选中图片 / 视频',self.preview_selected,'secondary','image')
        self.evidence_button = self.button(preview_box,'查看删除与检查依据',self.show_candidate_evidence,'secondary','shield')
        self.evidence_button.setEnabled(False)
        self.evidence_button.setToolTip('选中一项，分别查看删除依据、内容检查和独立导出记录说明。')
        splitter.setSizes([720,260])
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column in range(13): self.result_table.setColumnWidth(column,140)
        self.result_table.setColumnWidth(0,220); self.result_table.setColumnWidth(1,70); self.result_table.setColumnWidth(2,100)
        row = QHBoxLayout(); box.addLayout(row)
        self.previous_page = self.button(row,'上一页',lambda: self.change_result_page(-1),'secondary','arrow-left')
        self.next_page = self.button(row,'下一页',lambda: self.change_result_page(1),'secondary','arrow-right')
        self.previous_page.setEnabled(False); self.next_page.setEnabled(False); row.addStretch()
        self.export_selected_button = self.button(row,'导出选中项',lambda: self.export_results(False),'secondary','export')
        self.export_all_button = self.button(row,'导出全部筛选',lambda: self.export_results(True),'primary','check')
        self.button(row,'停止导出',self.cancel_export,'danger','stop')
        self.export_progress = QProgressBar(); self.export_progress.setRange(0,1000); self.export_progress.setValue(0); box.addWidget(self.export_progress)
        self.export_status = QLabel('生成独立批次目录，不覆盖现有文件。导出后逐项核对源 / 目标 SHA-256。'); self.export_status.setObjectName('sectionNote'); self.export_status.setWordWrap(True); box.addWidget(self.export_status)

        box = self.page('任务与处理记录', '最近 200 个非预览任务。双击一条记录，重新打开候选结果或导出校验记录。')
        row = QHBoxLayout(); box.addLayout(row)
        self.button(row,'刷新记录',self.refresh_history,'secondary','refresh'); self.button(row,'打开选中结果',self.open_history_result,'primary','folder'); row.addStretch(); self.button(row,'停止当前任务',self.cancel_task,'danger','stop')
        self.history_table = self.table(['任务类型','状态','更新时间 / UTC','错误类型']); box.addWidget(self.history_table,1)
        self.history_table.cellDoubleClicked.connect(lambda *_: self.open_history_result())
        label = QLabel('中断或取消仅保留已提交的部分记录，不支持断点续扫。'); label.setObjectName('sectionNote'); box.addWidget(label)

        box = self.page('工作空间设置', '选择本地工作目录，每个目录独立保留历史；正在处理任务时不能切换。', scrollable=True)
        data = self.card(box,'本地工作目录')
        label = QLabel(str(self.config.data_dir)); label.setObjectName('sourceInfo'); label.setTextFormat(Qt.TextFormat.PlainText); label.setWordWrap(True); label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); data.addWidget(label)
        self.workspace_button = self.button(data,'选择工作目录',self.choose_work_directory,'primary','folder')
        label = QLabel('选择另一个工作目录不会迁移或删除原数据。工作目录须与原始备份、待恢复介质分开。'); label.setObjectName('sectionNote'); label.setWordWrap(True); data.addWidget(label)
        devices = self.card(box,'设备与处理')
        for text in ['DEEPRECOVER_ADB  ·  Android Platform Tools 中 adb 的完整路径','后台并发  ·  2 个任务线程','日志策略  ·  按大小轮转，不记录文件内容或完整设备序列号','数据策略  ·  本地处理，不上传备份或候选内容']:
            label = QLabel(text); label.setObjectName('sectionNote'); label.setWordWrap(True); devices.addWidget(label)
        diagnostics = self.card(box,'本机环境与实机验收')
        label = QLabel('查看系统、显示缩放、可用空间和 ADB 工具定位。诊断报告不含来源路径、设备序列号或日志，仅保存到本地；实机验收项目仍需实际测试。')
        label.setObjectName('sectionNote'); label.setWordWrap(True); diagnostics.addWidget(label)
        self.diagnostics_button = self.button(diagnostics,'检查本机环境',self.show_environment_diagnostics,'secondary','shield')
        about = self.card(box,'关于 DeepRecover')
        label = QLabel(f'版本 {VERSION}  ·  Preview\n普通文件复制与候选导出，不等于已删除数据恢复成功。'); label.setObjectName('sectionNote'); label.setWordWrap(True); about.addWidget(label); box.addStretch(1)

        self.computer_page = ComputerPage(self)
        self.stack.addWidget(self.computer_page)

    def run_task(self, kind, operation, on_success, *, persist_candidates=False, source=None, batch_size=100, with_task_id=False):
        if self.pending or self.computer_page.readonly_pending:
            self.statusBar().showMessage('已有任务执行中，请等待或在任务记录页停止。'); return
        self.stop_video_preview()
        self.pending = (self.manager.submit(kind, operation, persist_candidates=persist_candidates,
                                            source=source, batch_size=batch_size, with_task_id=with_task_id), on_success)
        self.computer_page.refresh_controls()
        self.workspace_button.setEnabled(False)
        self.diagnostics_button.setEnabled(False)
        self.statusBar().showMessage('任务执行中…')

    def poll(self):
        self.computer_page.poll()
        self.workspace_button.setEnabled(not self.pending and not self.computer_page.readonly_pending)
        self.diagnostics_button.setEnabled(not self.pending and not self.computer_page.readonly_pending)
        if self.pending is None:
            return
        progress = self.pending[0].token.progress
        if progress is not None and progress.phase == 'scan':
            self.scan_progress.setValue(progress.processed * 1000 // progress.total if progress.total else 0)
            self.scan_status.setText(f'已扫描 {progress.processed} / {progress.total} 字节 · {progress.candidates} 个签名候选（不等于恢复成功）')
        elif progress is not None and progress.phase == 'export':
            self.export_progress.setValue(progress.processed * 1000 // progress.total if progress.total else 0)
            self.export_status.setText(f'已处理 {progress.processed} / {progress.total} 项 · 已校验导出 {progress.candidates} 项')
        if not self.pending[0].future.done():
            return
        handle, callback = self.pending
        self.pending = None
        try:
            result = handle.future.result()
            callback(result, handle.id)
            self.statusBar().showMessage('任务完成。')
        except TaskCancelled:
            self.statusBar().showMessage('任务已停止。')
        except (AdbError, BackupError, ScanError, CandidateAccessError, ExportError, *computer_error_types()) as error:
            self.statusBar().showMessage(str(error))
        except Exception as error:
            self.statusBar().showMessage(f'操作未完成（{type(error).__name__}），请检查数据源和目录权限。')
        if progress is not None and progress.phase == 'scan':
            task = self.repository.task(handle.id)
            display = task_status(task['status'])
            self.scan_status.setText(self.scan_status.text() + f" · 任务状态：{display.label}，已入库 {self.repository.candidate_count(handle.id)} 条")
            self.scan_status.setToolTip(display.tooltip)
            if task['status'] == 'completed':
                self.scan_progress.setValue(1000)
        task = self.repository.task(handle.id)
        if task['kind'] in ('image_preview', 'video_preview') and task['status'] != 'completed' and self.preview_candidate_id is not None:
            self.preview_label.setText(self.statusBar().currentMessage())
        if task['kind'] == 'export':
            outcome = {'completed':'导出完成','failed':'导出失败','cancelled':'导出已停止','interrupted':'导出中断'}.get(task['status'],task['status'])
            counts = self.repository.export_counts(handle.id)
            details = (f"已校验 {counts.get('exported',0)} 项，失败 {counts.get('failed',0)} 项，取消 {counts.get('cancelled',0)} 项。"
                       if task['result_path'] else '准备阶段已停止，尚未生成导出文件；原因见状态栏。')
            self.export_status.setText(f'{outcome} · {details}详见导出记录。')
            self.show_export_history(handle.id)
        self.refresh_history()
        self.computer_page.refresh_controls()
        self.workspace_button.setEnabled(not self.pending and not self.computer_page.readonly_pending)
        self.diagnostics_button.setEnabled(not self.pending and not self.computer_page.readonly_pending)

    def filter_results(self, *_):
        self.result_offset = 0
        if self.result_task_id:
            self.load_result_page()

    def selected_candidate_ids(self):
        return [self.current_candidates[index.row()]['id'] for index in self.result_table.selectionModel().selectedRows()
                if 0 <= index.row() < len(self.current_candidates)]

    def clear_preview(self):
        self.stop_video_preview()
        if self.evidence_dialog:
            self.evidence_dialog.close()
            self.evidence_dialog.deleteLater()
            self.evidence_dialog = None
        self.preview_candidate_id = None
        self.preview_label.clear()
        self.preview_label.setText('选择 JPG/PNG 图片或 MP4/MOV 视频，再点击预览。')
        self.evidence_button.setEnabled(len(self.selected_candidate_ids()) == 1)

    def stop_video_preview(self):
        if self.video_dialog:
            dialog = self.video_dialog
            dialog.close()
            self.video_dialog = None
            dialog.deleteLater()

    def show_candidate_evidence(self):
        rows = self.result_table.selectionModel().selectedRows()
        if len(rows) != 1 or not 0 <= rows[0].row() < len(self.current_candidates):
            self.statusBar().showMessage('请选择一项候选以查看依据。'); return
        candidate = self.current_candidates[rows[0].row()]
        if self.evidence_dialog:
            self.evidence_dialog.close(); self.evidence_dialog.deleteLater()
        dialog = QDialog(self); dialog.setWindowTitle('候选依据与检查'); dialog.setStyleSheet(STYLE); dialog.resize(640, 500)
        dialog.setMinimumSize(420, 300)
        box = QVBoxLayout(dialog); box.setContentsMargins(18,18,18,18)
        heading = QLabel(candidate['name']); heading.setObjectName('sectionTitle')
        heading.setTextFormat(Qt.TextFormat.PlainText); heading.setWordWrap(True); box.addWidget(heading)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(); layout = QVBoxLayout(content); layout.setSpacing(14)
        for key, title, display in (
            ('deletion', '删除依据', deletion_evidence(candidate)),
            ('content', '内容检查', content_check(candidate.get('integrity'))),
            ('export', '导出校验', export_guidance()),
        ):
            section = self.card(layout, title)
            label = QLabel(display.label); label.setObjectName(f'evidence-{key}-label')
            label.setTextFormat(Qt.TextFormat.PlainText); label.setWordWrap(True)
            label.setToolTip(display.tooltip); section.addWidget(label)
            note = QLabel(display.explanation); note.setObjectName(f'evidence-{key}-explanation')
            note.setTextFormat(Qt.TextFormat.PlainText); note.setWordWrap(True)
            note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); section.addWidget(note)
            if key == 'content' and video_check_details(candidate):
                details = QLabel(video_check_details(candidate)); details.setObjectName('evidence-video-details')
                details.setTextFormat(Qt.TextFormat.PlainText); details.setWordWrap(True); section.addWidget(details)
        layout.addStretch(1); scroll.setWidget(content); box.addWidget(scroll,1)
        close = QPushButton('关闭'); close.clicked.connect(dialog.close); box.addWidget(close)
        self.evidence_dialog = dialog; dialog.show()

    def preview_selected(self):
        ids = self.selected_candidate_ids()
        if len(ids) != 1 or self.pending or self.computer_page.readonly_pending:
            self.statusBar().showMessage('请选择一项图片或视频，并等待当前任务结束。'); return
        selected, task_id = ids[0], self.result_task_id
        candidate = self.repository.candidate(selected)
        video = candidate is not None and category(candidate.get('file_type')) == 'video'
        self.stop_video_preview()
        def complete(result, _):
            if self.result_task_id == task_id and self.preview_candidate_id == selected and self.selected_candidate_ids() == [selected]:
                if video:
                    from ui.preview.video import VideoPreviewDialog
                    self.video_dialog = VideoPreviewDialog(result, self)
                    dialog = self.video_dialog
                    dialog.finished.connect(lambda *_: self.preview_label.setText('视频预览已关闭；可重新选择并预览。')
                        if self.video_dialog is dialog and self.preview_candidate_id == selected else None)
                    self.video_dialog.show()
                    self.preview_label.setText('视频窗口已打开 · 静音\n播放已校验的内存内容，不修改来源。')
                else:
                    self.preview_label.setPixmap(QPixmap.fromImage(result).scaled(280,240,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                self.preview_label.setToolTip('本地解码预览；不更改候选状态，不表示已删除数据恢复成功。')
        def begin(_):
            if self.result_task_id != task_id or self.selected_candidate_ids() != [selected]:
                self.statusBar().showMessage('选中项已变化，请重新预览。'); return
            self.preview_candidate_id = selected
            self.preview_label.clear(); self.preview_label.setText('正在校验视频来源与预览范围…' if video else '正在校验来源并解码图片…')
            self.run_task('video_preview' if video else 'image_preview',
                lambda token: (prepare_video if video else preview_image)(self.repository,selected,self.config.data_dir,token),complete)
        if self.requires_computer_preflight(ids):
            self.preview_label.clear(); self.preview_label.setText('正在检查源盘与工作目录隔离…')
            self.computer_page._readonly(lambda: check_computer_access(self.repository,tuple(ids),self.config.data_dir), begin,
                '正在检查原始磁盘与工作目录隔离，检查通过前不会建立预览任务。',
                lambda message: self.preview_label.setText('未开始预览：' + message) if self.result_task_id == task_id else None)
        else:
            begin(None)

    def requires_computer_preflight(self, candidate_ids):
        for candidate_id in candidate_ids:
            candidate = self.repository.candidate(candidate_id)
            if candidate and candidate['source'] in ('computer_deleted','computer_signature'):
                identity = candidate.get('source_stat')
                # Unknown computer identities must also be checked before a
                # task can write to a possibly relocated working database.
                if not isinstance(identity,dict) or identity.get('kind') != 'image':
                    return True
        return False

    def export_results(self, all_filtered):
        if self.pending or self.computer_page.readonly_pending or not self.result_task_id:
            self.statusBar().showMessage('请先打开结果并等待当前任务结束。'); return
        try:
            ids = self.repository.candidate_ids(self.result_task_id,category=self.category_filter.currentData(),status=self.status_filter.currentData()) if all_filtered else self.selected_candidate_ids()
        except ValueError as error:
            self.statusBar().showMessage(str(error)); return
        if not ids:
            self.statusBar().showMessage('没有选中或符合筛选条件的候选。'); return
        directory = QFileDialog.getExistingDirectory(self,'选择本地导出目录',str(self.config.data_dir / 'Recovered'))
        if not directory:
            return
        if QMessageBox.question(self,'确认本地批量导出',f'导出 {len(ids)} 项到新批次目录。\n源文件保持只读；复制不等于已删除数据恢复。\n失败或取消时保留此前已校验文件和本地清单，是否继续？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        def begin(_):
            self.export_progress.setValue(0); self.export_status.setText('正在导出并核对来源与目标摘要…')
            self.run_task('export',lambda token,task_id: export_batch(self.repository,task_id,tuple(ids),directory,self.config.data_dir,token),
                          lambda *_: None,with_task_id=True)
        if self.requires_computer_preflight(ids):
            self.export_status.setText('正在检查工作目录和导出目标与原始磁盘隔离…')
            self.computer_page._readonly(lambda: check_computer_access(self.repository,tuple(ids),self.config.data_dir,directory), begin,
                '正在检查原始磁盘、工作目录与导出目录隔离，检查通过前不会建立导出任务。',
                lambda message: self.export_status.setText('未开始导出：' + message))
        else:
            begin(None)

    def cancel_export(self):
        if self.pending and self.repository.task(self.pending[0].id)['kind'] == 'export':
            self.cancel_task()
        else:
            self.statusBar().showMessage('当前没有正在执行的导出任务。')

    def show_export_history(self, task_id):
        if self.export_dialog:
            self.export_dialog.close()
            self.export_dialog.deleteLater()
        self.export_dialog = ExportHistory(self.repository,task_id,self)
        self.export_dialog.show()

    def choose_scan_source(self):
        if self.pending:
            self.statusBar().showMessage('请等待或停止当前任务后再选择扫描源。'); return
        filename, _ = QFileDialog.getOpenFileName(self, '选择只读扫描源', '', '镜像或文件 (*.img *.dd *.bin *.jpg *.jpeg *.png *.mp4 *.mov);;所有文件 (*)')
        if filename:
            from pathlib import Path
            source = Path(filename).resolve()
            if not source.is_file() or source == self.config.database.resolve() or source.is_relative_to((self.config.data_dir / 'Logs').resolve()):
                self.statusBar().showMessage('请选择稳定的普通文件；不能扫描程序工作数据库或日志。'); return
            self.scan_source = source
            self.scan_source_label.setText(str(source))
            self.scan_source_label.setToolTip(str(source))
            self.scan_start_button.setEnabled(True)

    def start_scan(self):
        if self.scan_source is None or self.pending:
            self.statusBar().showMessage('请先选择扫描源，并等待当前任务结束。'); return
        source = self.scan_source
        self.scan_progress.setValue(0)
        self.scan_status.setText('正在只读扫描…')
        self.run_task('signature_scan', lambda token: SignatureScanner().scan(source, token),
                      lambda _, task_id: self.open_results(task_id), persist_candidates=True,
                      source=DataSource('image', str(source), source.name), batch_size=1)

    def cancel_scan(self):
        if self.pending and self.repository.task(self.pending[0].id)['kind'] == 'signature_scan':
            self.cancel_task()
        else:
            self.statusBar().showMessage('当前没有正在执行的扫描任务。')

    def detect_android(self):
        def complete(devices, _):
            self.devices = devices
            self.android_files = []
            self.listed_device = None
            self.android_list_source.setText('尚未列出目录。')
            self.fill(self.android_file_table, [])
            self.fill(self.android_table, [(d.serial, d.model, d.state) for d in devices])
        self.run_task('android_discovery', lambda token: AndroidAdapter().devices(), complete)

    def list_android_files(self):
        row = self.android_table.currentRow()
        if row < 0 or not self.devices[row].authorized:
            self.statusBar().showMessage('请先选择已授权设备。'); return
        device = self.devices[row]
        root, accepted = QInputDialog.getText(self, '选择共享存储目录', '仅列出普通文件；最多 10,000 个，请选择具体子目录：', text='/sdcard/DCIM')
        if not accepted:
            return
        def complete(paths, _):
            self.listed_device = device
            self.android_files = paths
            self.android_list_source.setText(f'列表来源：{device.serial} · {device.model} · {root} · {len(paths)} 个普通文件')
            self.fill(self.android_file_table, [(path,) for path in paths])
            self.android_file_table.clearSelection()
        self.run_task('android_file_listing', lambda token: AndroidAdapter().list_files(device.serial, root, token), complete)

    def collect_android(self):
        selected = sorted({index.row() for index in self.android_file_table.selectionModel().selectedRows()})
        if not selected or self.listed_device is None:
            self.statusBar().showMessage('请先列出文件并明确选择要采集的项。'); return
        device = self.listed_device
        paths = tuple(self.android_files[row] for row in selected)
        if QMessageBox.question(self, '授权普通文件采集',
                f'设备：{device.serial} · {device.model}\n已选择 {len(paths)} 个普通文件。\n'
                f'仅通过 ADB pull 复制到 {self.config.data_dir / "Acquisitions"}。\n'
                '不写入或删除设备文件，不等同于已删除数据恢复。是否开始？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.run_task('android_acquisition',
            lambda token: acquire_files(AndroidAdapter(), device.serial, paths, self.config.data_dir / 'Acquisitions', token),
            lambda _, task_id: self.open_results(task_id), persist_candidates=True,
            source=DataSource('android', device.serial, device.model), batch_size=1)

    def choose_backup(self):
        directory = QFileDialog.getExistingDirectory(self, '选择备份目录或 Backup 根目录')
        if directory:
            self.detect_backups([directory])

    def detect_backups(self, roots):
        def complete(backups, _):
            self.backups = backups
            self.fill(self.backup_table, [(b.device_name, b.product_version,
                b.error or ('已加密' if b.encrypted else '未加密'), b.date, b.path) for b in backups])
        self.run_task('ios_discovery', lambda token: discover_backups(roots), complete)

    def parse_backup(self):
        row = self.backup_table.currentRow()
        if row < 0:
            self.statusBar().showMessage('请先选择一份备份。'); return
        path = self.backups[row].path
        if self.config.data_dir.resolve().is_relative_to(path.resolve()):
            self.statusBar().showMessage('工作目录位于源备份中，请更改 DEEPRECOVER_DATA_DIR 后重启。'); return
        self.run_task('ios_manifest', lambda token: iter_manifest(path, token),
                      lambda _, task_id: self.open_results(task_id), persist_candidates=True,
                      source=DataSource('ios_backup', str(path), self.backups[row].device_name))

    def open_results(self, task_id):
        task = self.repository.task(task_id)
        if task and task['kind'] == 'export':
            self.show_export_history(task_id); return
        self.result_task_id = task_id
        self.result_offset = 0
        self.category_filter.blockSignals(True); self.status_filter.blockSignals(True)
        self.category_filter.setCurrentIndex(0); self.status_filter.setCurrentIndex(0)
        self.category_filter.blockSignals(False); self.status_filter.blockSignals(False)
        self.load_result_page()
        self.nav.setCurrentRow(4)

    def load_result_page(self):
        task = self.repository.task(self.result_task_id)
        if task is None:
            self.current_candidates = []
            self.result_table.setRowCount(0)
            self.clear_preview()
            self.result_source.setText('任务记录已不存在，请刷新任务记录。')
            self.result_page_label.setText('0 条')
            self.previous_page.setEnabled(False); self.next_page.setEnabled(False)
            return
        source = self.repository.source(task['source_id']) if task['source_id'] else None
        filters = {'category':self.category_filter.currentData(),'status':self.status_filter.currentData()}
        total = self.repository.candidate_count(self.result_task_id,**filters)
        self.result_offset = min(self.result_offset, max(0, (total - 1) // self.page_size * self.page_size))
        candidates = self.repository.candidate_page(self.result_task_id, offset=self.result_offset, limit=self.page_size,**filters)
        self.result_table.clearSelection()
        self.current_candidates = candidates
        self.fill(self.result_table, [(c['name'], c['file_type'], c['size'], content_check(c.get('integrity')).label, c['original_path'],
                                      c.get('local_path', ''), c.get('sha256') or '', c.get('acquired_at') or '',
                                      c.get('offset') if c.get('offset') is not None else '', candidate_status(c.get('status')).label,
                                      c.get('file_system') or '',
                                      deletion_evidence(c).label,
                                      len(c.get('extents') or ()) if c.get('file_system') else '') for c in candidates])
        for row, candidate in enumerate(candidates):
            for column, display in ((3, content_check(candidate.get('integrity'))),
                                    (9, candidate_status(candidate.get('status'))),
                                    (11, deletion_evidence(candidate))):
                self.explain_item(self.result_table.item(row,column), display)
        self.clear_preview()
        description = f"{source_kind(source['kind']).label} · {source['label']} · {source['locator']}" if source else '旧记录：未保存独立数据源，见原始路径'
        display = task_status(task['status'])
        self.result_source.setText(f"来源：{description}\n任务状态：{display.label} · 仅展示已入库记录，历史路径可能已失效。")
        raw_source = f"\n来源类型记录值：{source['kind']}" if source else ''
        self.result_source.setToolTip(f"任务 {task['id']}\n来源：{description}{raw_source}\n{display.tooltip}\n取消、失败或中断任务仅展示部分结果，不自动重新采集。")
        self.result_page_label.setText(f'{self.result_offset + 1 if total else 0}–{self.result_offset + len(candidates)} / {total} 条')
        self.previous_page.setEnabled(self.result_offset > 0)
        self.next_page.setEnabled(self.result_offset + len(candidates) < total)

    def change_result_page(self, direction):
        self.result_offset = max(0, self.result_offset + direction * self.page_size)
        self.load_result_page()

    def open_history_result(self):
        row = self.history_table.currentRow()
        if row < 0:
            self.statusBar().showMessage('请先选择一条历史任务。'); return
        self.open_results(self.history[row]['id'])

    def refresh_history(self, *_):
        selected = self.history_table.currentRow()
        selected_id = self.history[selected]['id'] if 0 <= selected < len(self.history) else None
        self.history = self.repository.tasks()
        self.fill(self.history_table, [(task_kind(t['kind']).label, task_status(t['status']).label,
                                       t['updated_at'], error_text(t['error_code']).label) for t in self.history])
        for row, task in enumerate(self.history):
            for column, display in ((0, task_kind(task['kind'])), (1, task_status(task['status'])),
                                    (3, error_text(task['error_code']))):
                self.explain_item(self.history_table.item(row,column), display)
            if task['id'] == selected_id:
                self.history_table.selectRow(row)

    def cancel_task(self):
        self.stop_video_preview()
        if self.pending:
            self.pending[0].cancel()
            if self.repository.task(self.pending[0].id)['kind'] == 'signature_scan':
                self.statusBar().showMessage('已请求停止扫描；已入库的候选记录会保留。')
            elif self.repository.task(self.pending[0].id)['kind'] in ('export','image_preview','video_preview'):
                self.statusBar().showMessage('已请求停止；此前已校验的导出文件和记录会保留。')
            elif self.repository.task(self.pending[0].id)['kind'] == 'computer_scan':
                self.statusBar().showMessage('已请求停止电脑扫描；已提交的部分候选保留，当前读取结束后停止。')
            else:
                self.statusBar().showMessage('已请求停止；正在进行的 ADB pull 最长等待 60 秒，其他 ADB 调用最长 15 秒。')

    def closeEvent(self, event):
        if self.video_dialog and self.video_dialog._releasing:
            self.statusBar().showMessage('正在释放视频解码资源，请稍后再次关闭。')
            event.ignore(); return
        if not self.computer_page.prepare_close():
            self.statusBar().showMessage('正在结束只读磁盘预检，请等待当前系统调用返回后再次关闭。')
            event.ignore(); return
        if self.pending and not self.pending[0].future.done():
            self.cancel_task()
            self.statusBar().showMessage('正在停止任务，请在任务结束后再次关闭窗口。')
            event.ignore(); return
        # A parented QDialog is still a top-level window and can outlive a
        # hidden main window. Close it only after the task guard accepts exit.
        if self.export_dialog:
            self.export_dialog.close()
        if self.evidence_dialog:
            self.evidence_dialog.close()
        if self.diagnostics_dialog:
            self.diagnostics_dialog.close()
        self.stop_video_preview()
        self.timer.stop()
        self.manager.shutdown()
        workspace_lock = getattr(self, '_workspace_lock', None)
        if workspace_lock is not None:
            workspace_lock.unlock()
            self._workspace_lock = None
        event.accept()
