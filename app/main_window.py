import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QColor, QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QMainWindow, QPushButton, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QInputDialog, QMessageBox, QProgressBar, QComboBox, QSplitter, QFrame, QListWidgetItem)
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
from core.export.service import export_batch, ExportError
from ui.recovery.export_history import ExportHistory
from app.theme import STYLE, icon, brand_icon
from app.version import VERSION


def selection_modifier(platform=None):
    return 'Command / Shift' if (platform or sys.platform) == 'darwin' else 'Ctrl / Shift'


class MainWindow(QMainWindow):
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
        for label, name in zip(PAGES,('home','android','iphone','scan','results','history','settings')):
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

    def page(self, title, subtitle):
        page = QWidget()
        page.setObjectName('page')
        box = QVBoxLayout(page)
        box.setContentsMargins(24, 22, 24, 18)
        box.setSpacing(10)
        eyebrow = QLabel('DEEPRECOVER  /  WORKSPACE'); eyebrow.setObjectName('pageEyebrow'); box.addWidget(eyebrow)
        label = QLabel(title); label.setObjectName('title'); box.addWidget(label)
        label = QLabel(subtitle); label.setWordWrap(True); label.setObjectName('subtitle'); box.addWidget(label)
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
                if value in ('completed','exported','structure_checked','device'): item.setForeground(QColor('#087c68'))
                elif value in ('failed','missing','invalid_structure','unauthorized'): item.setForeground(QColor('#c53f53'))
                elif value in ('partial','truncated','validation_limit','cancelled','interrupted'): item.setForeground(QColor('#a86608'))
                table.setItem(i, j, item)

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
        box = self.page('让数据处理，更有条理', '连接授权设备或打开本地备份，从来源记录到校验导出，全程在本机完成。')
        hero = QFrame(); hero.setObjectName('heroCard'); box.addWidget(hero)
        hero_box = QVBoxLayout(hero); hero_box.setContentsMargins(26,24,26,24); hero_box.setSpacing(12)
        badge = QLabel('●  LOCAL FIRST   /   本地优先'); badge.setObjectName('badge'); badge.setMaximumWidth(240); hero_box.addWidget(badge)
        headline = QLabel('从一个可靠的数据源开始'); headline.setObjectName('heroTitle'); hero_box.addWidget(headline)
        note = QLabel('只读索引与图片候选扫描，保留来源线索。\n预览确认后，按需导出并逐项核对 SHA-256。'); note.setObjectName('heroText'); note.setWordWrap(True); hero_box.addWidget(note)
        actions = QHBoxLayout(); hero_box.addLayout(actions)
        self.button(actions,'打开本地备份',lambda: self.nav.setCurrentRow(2),'primary','folder')
        self.button(actions,'扫描本地镜像',lambda: self.nav.setCurrentRow(3),'secondary','scan'); actions.addStretch()
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

        box = self.page('扫描本地镜像', '定位文件或镜像中的连续 JPG / PNG 签名，记录偏移与结构检查结果。')
        source_box = self.card(box,'01   选择扫描源')
        row = QHBoxLayout(); source_box.addLayout(row)
        self.button(row,'选择镜像或文件',self.choose_scan_source,'secondary','folder'); row.addStretch()
        self.scan_source_label = QLabel('尚未选择源文件'); self.scan_source_label.setObjectName('sourceInfo'); self.scan_source_label.setTextFormat(Qt.TextFormat.PlainText); self.scan_source_label.setWordWrap(True); source_box.addWidget(self.scan_source_label)
        work = self.card(box,'02   开始只读扫描')
        row = QHBoxLayout(); work.addLayout(row)
        self.scan_start_button = self.button(row,'开始 JPG / PNG 扫描',self.start_scan,'primary','play'); self.scan_start_button.setEnabled(False)
        self.button(row,'停止扫描',self.cancel_scan,'danger','stop'); row.addStretch()
        self.scan_progress = QProgressBar(); self.scan_progress.setRange(0,1000); self.scan_progress.setValue(0); work.addWidget(self.scan_progress)
        self.scan_status = QLabel('等待选择数据源。扫描结果将自动打开到候选索引。'); self.scan_status.setObjectName('sectionNote'); self.scan_status.setWordWrap(True); work.addWidget(self.scan_status)
        limits = self.card(box,'检查范围')
        label = QLabel('连续图片候选  ·  单项检查上限 64 MiB  ·  PNG 解压上限 256 MiB  ·  最多 10,000 项'); label.setObjectName('sectionNote'); label.setWordWrap(True); limits.addWidget(label)
        label = QLabel('不分析文件系统或重组碎片；结构检查通过不代表完整解码，也不能判断文件是否已删除。'); label.setObjectName('sectionNote'); label.setWordWrap(True); limits.addWidget(label)
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
        self.result_table = self.table(['文件名','类型','大小 / 字节','完整性','原始路径','本地文件','来源范围 SHA-256','采集时间 / UTC','源内偏移 / 字节','候选状态'])
        self.result_table.setToolTip(f'{selection_modifier()} 可多选本页记录；分页或筛选会重置当前选择。')
        self.result_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); splitter.addWidget(self.result_table)
        preview = QFrame(); preview.setObjectName('previewPanel'); preview_box = QVBoxLayout(preview); preview_box.setContentsMargins(14,14,14,14); splitter.addWidget(preview)
        label = QLabel('图片预览'); label.setObjectName('sectionTitle'); preview_box.addWidget(label)
        self.preview_label = QLabel('选择一张 JPG / PNG\n查看本地图片预览'); self.preview_label.setObjectName('previewCanvas')
        self.preview_label.setWordWrap(True); self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview_label.setTextFormat(Qt.TextFormat.PlainText); self.preview_label.setMinimumWidth(210)
        self.result_table.itemSelectionChanged.connect(self.clear_preview); preview_box.addWidget(self.preview_label,1)
        self.preview_button = self.button(preview_box,'预览选中图片',self.preview_selected,'secondary','image')
        splitter.setSizes([720,260])
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column in range(10): self.result_table.setColumnWidth(column,140)
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

        box = self.page('工作空间设置', '配置保存在本机。环境变量修改后，下次启动生效。')
        data = self.card(box,'本地工作目录')
        label = QLabel(str(self.config.data_dir)); label.setObjectName('sourceInfo'); label.setTextFormat(Qt.TextFormat.PlainText); label.setWordWrap(True); label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); data.addWidget(label)
        label = QLabel('DEEPRECOVER_DATA_DIR  ·  将工作目录与原始备份、待恢复介质分开。'); label.setObjectName('sectionNote'); label.setWordWrap(True); data.addWidget(label)
        devices = self.card(box,'设备与处理')
        for text in ['DEEPRECOVER_ADB  ·  Android Platform Tools 中 adb 的完整路径','后台并发  ·  2 个任务线程','日志策略  ·  按大小轮转，不记录文件内容或完整设备序列号','数据策略  ·  本地处理，不上传备份或候选内容']:
            label = QLabel(text); label.setObjectName('sectionNote'); label.setWordWrap(True); devices.addWidget(label)
        about = self.card(box,'关于 DeepRecover')
        label = QLabel(f'版本 {VERSION}  ·  Preview\n普通文件复制与候选导出，不等于已删除数据恢复成功。'); label.setObjectName('sectionNote'); label.setWordWrap(True); about.addWidget(label); box.addStretch(1)

    def run_task(self, kind, operation, on_success, *, persist_candidates=False, source=None, batch_size=100, with_task_id=False):
        if self.pending:
            self.statusBar().showMessage('已有任务执行中，请等待或在任务记录页停止。'); return
        self.pending = (self.manager.submit(kind, operation, persist_candidates=persist_candidates,
                                            source=source, batch_size=batch_size, with_task_id=with_task_id), on_success)
        self.statusBar().showMessage('任务执行中…')

    def poll(self):
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
        except (AdbError, BackupError, ScanError, CandidateAccessError, ExportError) as error:
            self.statusBar().showMessage(str(error))
        except Exception as error:
            self.statusBar().showMessage(f'操作未完成（{type(error).__name__}），请检查数据源和目录权限。')
        if progress is not None and progress.phase == 'scan':
            task = self.repository.task(handle.id)
            self.scan_status.setText(self.scan_status.text() + f" · 任务状态：{task['status']}，已入库 {self.repository.candidate_count(handle.id)} 条")
            if task['status'] == 'completed':
                self.scan_progress.setValue(1000)
        task = self.repository.task(handle.id)
        if task['kind'] == 'image_preview' and task['status'] != 'completed' and self.preview_candidate_id is not None:
            self.preview_label.setText(self.statusBar().currentMessage())
        if task['kind'] == 'export':
            outcome = {'completed':'导出完成','failed':'导出失败','cancelled':'导出已停止','interrupted':'导出中断'}.get(task['status'],task['status'])
            counts = self.repository.export_counts(handle.id)
            details = (f"已校验 {counts.get('exported',0)} 项，失败 {counts.get('failed',0)} 项，取消 {counts.get('cancelled',0)} 项。"
                       if task['result_path'] else '准备阶段已停止，尚未生成导出文件；原因见状态栏。')
            self.export_status.setText(f'{outcome} · {details}详见导出记录。')
            self.show_export_history(handle.id)
        self.refresh_history()

    def filter_results(self, *_):
        self.result_offset = 0
        if self.result_task_id:
            self.load_result_page()

    def selected_candidate_ids(self):
        return [self.current_candidates[index.row()]['id'] for index in self.result_table.selectionModel().selectedRows()]

    def clear_preview(self):
        self.preview_candidate_id = None
        self.preview_label.clear()
        self.preview_label.setText('选择一张 JPG/PNG，再点击预览。')

    def preview_selected(self):
        ids = self.selected_candidate_ids()
        if len(ids) != 1 or self.pending:
            self.statusBar().showMessage('请选择一张图片，并等待当前任务结束。'); return
        selected, task_id = ids[0], self.result_task_id
        self.preview_candidate_id = selected
        self.preview_label.clear(); self.preview_label.setText('正在校验来源并解码图片…')
        def complete(image, _):
            if self.result_task_id == task_id and self.preview_candidate_id == selected:
                self.preview_label.setPixmap(QPixmap.fromImage(image).scaled(280,240,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                self.preview_label.setToolTip('本地解码预览；不更改候选状态，不表示已删除数据恢复成功。')
        self.run_task('image_preview',lambda token: preview_image(self.repository,selected,self.config.data_dir,token),complete)

    def export_results(self, all_filtered):
        if self.pending or not self.result_task_id:
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
        self.export_progress.setValue(0); self.export_status.setText('正在导出并核对来源与目标摘要…')
        self.run_task('export',lambda token,task_id: export_batch(self.repository,task_id,tuple(ids),directory,self.config.data_dir,token),
                      lambda *_: None,with_task_id=True)

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
        filename, _ = QFileDialog.getOpenFileName(self, '选择只读扫描源', '', '镜像或文件 (*.img *.dd *.bin *.jpg *.jpeg *.png);;所有文件 (*)')
        if filename:
            from pathlib import Path
            source = Path(filename).resolve()
            if not source.is_file() or source == self.config.database.resolve() or source.is_relative_to((self.config.data_dir / 'Logs').resolve()):
                self.statusBar().showMessage('请选择稳定的普通文件；不能扫描程序工作数据库或日志。'); return
            self.scan_source = source
            self.scan_source_label.setText(str(source))
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
            return
        source = self.repository.source(task['source_id']) if task['source_id'] else None
        filters = {'category':self.category_filter.currentData(),'status':self.status_filter.currentData()}
        total = self.repository.candidate_count(self.result_task_id,**filters)
        self.result_offset = min(self.result_offset, max(0, (total - 1) // self.page_size * self.page_size))
        candidates = self.repository.candidate_page(self.result_task_id, offset=self.result_offset, limit=self.page_size,**filters)
        self.current_candidates = candidates
        self.preview_candidate_id = None
        self.preview_label.clear(); self.preview_label.setText('选择一张 JPG/PNG，再点击预览。')
        self.fill(self.result_table, [(c['name'], c['file_type'], c['size'], c['integrity'], c['original_path'],
                                      c.get('local_path', ''), c.get('sha256') or '', c.get('acquired_at') or '',
                                      c.get('offset') if c.get('offset') is not None else '', c['status']) for c in candidates])
        description = f"{source['kind']} · {source['label']} · {source['locator']}" if source else '旧记录：未保存独立数据源，见原始路径'
        self.result_source.setText(f"来源：{description}\n任务状态：{task['status']} · 仅展示已入库记录，历史路径可能已失效。")
        self.result_source.setToolTip(f"任务 {task['id']}\n来源：{description}\n取消、失败或中断任务仅展示部分结果，不自动重新采集。")
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
        self.fill(self.history_table, [(t['kind'], t['status'], t['updated_at'], t['error_code'] or '') for t in self.history])
        for row, task in enumerate(self.history):
            if task['id'] == selected_id:
                self.history_table.selectRow(row)

    def cancel_task(self):
        if self.pending:
            self.pending[0].cancel()
            if self.repository.task(self.pending[0].id)['kind'] == 'signature_scan':
                self.statusBar().showMessage('已请求停止扫描；已入库的候选记录会保留。')
            elif self.repository.task(self.pending[0].id)['kind'] in ('export','image_preview'):
                self.statusBar().showMessage('已请求停止；此前已校验的导出文件和记录会保留。')
            else:
                self.statusBar().showMessage('已请求停止；正在进行的 ADB pull 最长等待 60 秒，其他 ADB 调用最长 15 秒。')

    def closeEvent(self, event):
        if self.pending and not self.pending[0].future.done():
            self.cancel_task()
            self.statusBar().showMessage('正在停止任务，请在任务结束后再次关闭窗口。')
            event.ignore(); return
        # A parented QDialog is still a top-level window and can outlive a
        # hidden main window. Close it only after the task guard accepts exit.
        if self.export_dialog:
            self.export_dialog.close()
        self.timer.stop()
        self.manager.shutdown()
        event.accept()
