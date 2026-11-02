from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QMainWindow, QPushButton, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QInputDialog, QMessageBox, QProgressBar, QComboBox, QSplitter)
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

STYLE = '''
QWidget { background: #101823; color: #dbe5f2; font-family: "Segoe UI"; font-size: 14px; }
QListWidget { background: #152131; border: none; padding: 12px; }
QListWidget::item { padding: 14px 8px; margin: 3px 0; }
QListWidget::item:selected { background: #244d67; color: #6ee7df; border-radius: 6px; }
QLabel#title { font-size: 27px; font-weight: 600; }
QLabel#subtitle { color: #9aaec3; }
QPushButton { background: #236d82; border: none; border-radius: 5px; padding: 10px 18px; }
QPushButton:hover { background: #2b8195; }
QPushButton:disabled { background: #263343; color: #8a99ab; }
QTableWidget { background: #152131; border: 1px solid #2b3b4e; gridline-color: #29394c; }
QHeaderView::section { background: #213247; color: #bdccdd; padding: 9px; border: none; }
'''

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
        self.setWindowTitle('DeepRecover · 第一阶段 M4 · 0.1.0')
        self.resize(1180, 780)
        self.setMinimumSize(880, 620)
        self.setStyleSheet(STYLE)
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        self.nav = QListWidget()
        self.nav.setFixedWidth(190)
        self.nav.addItems(PAGES)
        self.stack = QStackedWidget()
        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(container)
        self.build_pages()
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentRowChanged.connect(self.refresh_history)
        self.nav.setCurrentRow(0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)
        self.statusBar().showMessage('本地处理 · M4 图片预览与校验导出 · 未执行设备操作')

    def page(self, title, subtitle):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(30, 28, 30, 24)
        box.setSpacing(18)
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
        table.verticalHeader().hide()
        return table

    def fill(self, table, rows):
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                item = QTableWidgetItem(str(value)); item.setToolTip(str(value)); table.setItem(i, j, item)

    def button(self, box, label, callback):
        button = QPushButton(label); button.clicked.connect(callback); box.addWidget(button)
        return button

    def build_pages(self):
        box = self.page('DeepRecover', '手机数据恢复工作台 / 第一阶段 M4 · 图片预览与校验导出')
        label = QLabel('已就绪\n\n• 本地数据库与 v3 迁移\n• Android 授权普通文件采集\n• iPhone 本地备份发现与只读索引\n• JPG/PNG 连续签名扫描与偏移\n• 图片预览、分类筛选和历史回读\n• 批量导出、防覆盖、SHA-256 对照清单\n\n下一里程碑\n\n真实手机/备份 → Windows 实机 → 安装包验收')
        label.setWordWrap(True); box.addWidget(label)
        box.addStretch()
        box.addWidget(QLabel('索引中的文件仅为候选记录，不代表已恢复或已验证完整。'))
        box = self.page('Android 设备', '在手机上允许 USB 调试，选择设备和共享目录，再明确选择文件采集。仅复制现存普通文件，不是已删除数据恢复；不执行 root、删除或写入设备。')
        self.button(box, '检测 ADB 与设备', self.detect_android)
        self.android_table = self.table(['设备序列号', '型号', '状态']); box.addWidget(self.android_table)
        self.button(box, '列出所选设备的共享目录文件', self.list_android_files)
        self.android_list_source = QLabel('尚未列出目录。')
        self.android_list_source.setTextFormat(Qt.TextFormat.PlainText)
        self.android_list_source.setWordWrap(True)
        box.addWidget(self.android_list_source)
        self.android_file_table = self.table(['普通文件路径（可多选）'])
        self.android_file_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        box.addWidget(self.android_file_table)
        self.button(box, '采集选中文件到本地', self.collect_android)
        box = self.page('iPhone 本地备份', '发现 Apple Devices / iTunes 备份。仅支持未加密备份；只读遍历全部普通文件索引，分批入库、分页展示，不提取文件。')
        row = QHBoxLayout(); box.addLayout(row)
        self.button(row, '自动发现备份', lambda: self.detect_backups(None))
        self.button(row, '选择备份目录', self.choose_backup)
        self.button(row, '读取选中备份索引', self.parse_backup)
        self.backup_table = self.table(['设备', 'iOS 版本', '加密状态', '备份时间', '目录']); box.addWidget(self.backup_table)
        box = self.page('文件扫描', '只读扫描本地普通文件或镜像中的连续 JPG/PNG 签名。记录偏移和结构检查结果，不提取文件、不分析文件系统或重组碎片，无法判断数据是否已删除。')
        self.button(box, '选择本地镜像或文件', self.choose_scan_source)
        self.scan_source_label = QLabel('尚未选择源文件。')
        self.scan_source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.scan_source_label.setWordWrap(True)
        box.addWidget(self.scan_source_label)
        self.scan_start_button = self.button(box, '开始 JPG/PNG 签名扫描', self.start_scan)
        self.scan_start_button.setEnabled(False)
        self.button(box, '停止扫描', self.cancel_scan)
        self.scan_progress = QProgressBar(); self.scan_progress.setRange(0, 1000); box.addWidget(self.scan_progress)
        self.scan_status = QLabel('单个候选最多检查 64 MiB；PNG 解压检查最多 256 MiB；最多 10,000 个候选。\n结构检查不等于完整像素解码、可预览或恢复成功。')
        self.scan_status.setWordWrap(True); box.addWidget(self.scan_status); box.addStretch()
        box = self.page('恢复结果 / 候选索引', '分类依据类型元数据；普通文件复制不等于已删除数据恢复。预览仅支持 JPG/PNG；导出前检查来源，导出后核对 SHA-256，原始候选状态保持不变。')
        self.result_source = QLabel('从任务记录选择任务回读结果。')
        self.result_source.setWordWrap(True)
        self.result_source.setTextFormat(Qt.TextFormat.PlainText)
        self.result_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.result_source)
        row = QHBoxLayout(); box.addLayout(row)
        self.category_filter = QComboBox()
        for label, value in [('全部类型','all'),('图片','image'),('视频','video'),('音频','audio'),('文档','document'),('其他','other')]:
            self.category_filter.addItem(label,value)
        row.addWidget(self.category_filter)
        self.status_filter = QComboBox()
        for label, value in [('全部状态','all'),('候选','candidate'),('部分/受限','partial'),('失败','failed')]:
            self.status_filter.addItem(label,value)
        row.addWidget(self.status_filter)
        self.category_filter.currentIndexChanged.connect(self.filter_results)
        self.status_filter.currentIndexChanged.connect(self.filter_results)
        splitter = QSplitter(); box.addWidget(splitter,1)
        self.result_table = self.table(['文件名', '类型', '大小 / 字节', '完整性', '原始路径', '本地文件', '来源范围 SHA-256', '采集时间 / UTC', '源内偏移 / 字节', '候选状态'])
        self.result_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        splitter.addWidget(self.result_table)
        preview = QWidget(); preview_box = QVBoxLayout(preview); splitter.addWidget(preview)
        self.preview_label = QLabel('选择一张 JPG/PNG，再点击预览。')
        self.preview_label.setWordWrap(True); self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setTextFormat(Qt.TextFormat.PlainText); self.preview_label.setMinimumWidth(240)
        self.result_table.itemSelectionChanged.connect(self.clear_preview)
        preview_box.addWidget(self.preview_label)
        self.preview_button = self.button(preview_box, '预览选中图片', self.preview_selected)
        splitter.setSizes([650,280])
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column in range(self.result_table.columnCount()):
            self.result_table.setColumnWidth(column, 150)
        row = QHBoxLayout(); box.addLayout(row)
        self.previous_page = self.button(row, '上一页', lambda: self.change_result_page(-1))
        self.result_page_label = QLabel('0 条'); row.addWidget(self.result_page_label)
        self.next_page = self.button(row, '下一页', lambda: self.change_result_page(1))
        self.previous_page.setEnabled(False); self.next_page.setEnabled(False)
        row = QHBoxLayout(); box.addLayout(row)
        self.export_selected_button = self.button(row,'导出选中项（本页）',lambda: self.export_results(False))
        self.export_all_button = self.button(row,'导出全部筛选结果',lambda: self.export_results(True))
        self.button(row,'停止当前导出',self.cancel_export)
        self.export_progress = QProgressBar(); self.export_progress.setRange(0,1000); box.addWidget(self.export_progress)
        self.export_status = QLabel('导出使用新批次目录和生成文件名，不覆盖现有文件。'); self.export_status.setWordWrap(True); box.addWidget(self.export_status)
        box = self.page('任务记录', '显示最近 200 条任务。异常退出后的未完成任务标记为 interrupted；暂不支持断点续扫。')
        self.button(box, '刷新任务记录', self.refresh_history)
        self.button(box, '停止当前任务', self.cancel_task)
        self.button(box, '回读选中任务的结果', self.open_history_result)
        self.history_table = self.table(['任务类型', '状态', '更新时间 / UTC', '错误类型']); box.addWidget(self.history_table)
        self.history_table.cellDoubleClicked.connect(lambda *_: self.open_history_result())
        box = self.page('设置', '当前版本使用环境变量配置，下次启动生效。')
        label = QLabel(f'工作目录：{self.config.data_dir}\n\nDEEPRECOVER_DATA_DIR：工作数据目录\nDEEPRECOVER_ADB：ADB 可执行文件完整路径\n\n后台线程：2\n日志：按大小轮转，不记录文件内容或设备序列号\n数据：仅保存在本机')
        label.setWordWrap(True); label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(label); box.addStretch()

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
            self.export_status.setText(self.export_status.text() + f" · 任务状态：{task['status']}，详见导出记录")
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
        self.result_source.setText(f"任务 {task['id']} · {task['status']}\n来源：{description}\n取消、失败或中断任务仅展示已入库的部分结果。历史路径可能已失效，不自动重新采集。")
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
        self.timer.stop()
        self.manager.shutdown()
        event.accept()
