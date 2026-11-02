from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QMainWindow, QPushButton, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QInputDialog, QMessageBox)
from app.navigation import PAGES
from core.tasks import TaskManager, TaskCancelled
from core.device.android.adapter import AndroidAdapter, AdbError
from core.device.ios.discovery import discover_backups
from core.parser.ios_backup.manifest import iter_manifest, BackupError
from core.acquisition.android import acquire_files
from storage.models.source import DataSource

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
        self.setWindowTitle('DeepRecover · 第一阶段 M2 · 0.1.0')
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
        self.statusBar().showMessage('本地处理 · M2 授权文件采集与索引 · 未执行设备操作')

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
        box = self.page('DeepRecover', '手机数据恢复工作台 / 第一阶段 M2 · 授权采集与来源追踪')
        label = QLabel('已就绪\n\n• 本地数据库与 v2 迁移\n• Android 授权设备与选中文件采集\n• iPhone 本地备份发现与只读索引\n• 数据来源、分页与历史结果回读\n• 后台任务、取消和任务记录\n\n下一里程碑\n\n连续文件签名扫描 → 预览 → 批量导出及校验')
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
        box = self.page('文件扫描', '扫描引擎接口已建立，文件签名扫描将在下一里程碑实现。')
        button = QPushButton('开始扫描 · 待实现'); button.setEnabled(False); box.addWidget(button); box.addStretch()
        box = self.page('恢复结果 / 候选索引', '普通文件采集和备份索引不等于已删除数据恢复。未检查文件结构；SHA-256 仅表示本地副本摘要，不证明采集时源文件稳定。')
        self.result_source = QLabel('从任务记录选择任务回读结果。')
        self.result_source.setWordWrap(True)
        self.result_source.setTextFormat(Qt.TextFormat.PlainText)
        self.result_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.result_source)
        self.result_table = self.table(['文件名', '类型', '大小 / 字节', '完整性', '原始路径', '本地文件', 'SHA-256', '采集时间 / UTC']); box.addWidget(self.result_table)
        row = QHBoxLayout(); box.addLayout(row)
        self.previous_page = self.button(row, '上一页', lambda: self.change_result_page(-1))
        self.result_page_label = QLabel('0 条'); row.addWidget(self.result_page_label)
        self.next_page = self.button(row, '下一页', lambda: self.change_result_page(1))
        self.previous_page.setEnabled(False); self.next_page.setEnabled(False)
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

    def run_task(self, kind, operation, on_success, *, persist_candidates=False, source=None, batch_size=100):
        if self.pending:
            self.statusBar().showMessage('已有任务执行中，请等待或在任务记录页停止。'); return
        self.pending = (self.manager.submit(kind, operation, persist_candidates=persist_candidates,
                                            source=source, batch_size=batch_size), on_success)
        self.statusBar().showMessage('任务执行中…')

    def poll(self):
        if self.pending is None or not self.pending[0].future.done():
            return
        handle, callback = self.pending
        self.pending = None
        try:
            result = handle.future.result()
            callback(result, handle.id)
            self.statusBar().showMessage('任务完成。')
        except TaskCancelled:
            self.statusBar().showMessage('任务已停止。')
        except (AdbError, BackupError) as error:
            self.statusBar().showMessage(str(error))
        except Exception as error:
            self.statusBar().showMessage(f'操作未完成（{type(error).__name__}），请检查数据源和目录权限。')
        self.refresh_history()

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
        self.result_task_id = task_id
        self.result_offset = 0
        self.load_result_page()
        self.nav.setCurrentRow(4)

    def load_result_page(self):
        task = self.repository.task(self.result_task_id)
        if task is None:
            return
        source = self.repository.source(task['source_id']) if task['source_id'] else None
        total = self.repository.candidate_count(self.result_task_id)
        self.result_offset = min(self.result_offset, max(0, (total - 1) // self.page_size * self.page_size))
        candidates = self.repository.candidate_page(self.result_task_id, offset=self.result_offset, limit=self.page_size)
        self.fill(self.result_table, [(c['name'], c['file_type'], c['size'], c['integrity'], c['original_path'],
                                      c.get('local_path', ''), c.get('sha256') or '', c.get('acquired_at') or '') for c in candidates])
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
            self.statusBar().showMessage('已请求停止；正在进行的 ADB pull 最长等待 60 秒，其他 ADB 调用最长 15 秒。')

    def closeEvent(self, event):
        if self.pending and not self.pending[0].future.done():
            self.cancel_task()
            self.statusBar().showMessage('正在停止任务，请在任务结束后再次关闭窗口。')
            event.ignore(); return
        self.timer.stop()
        self.manager.shutdown()
        event.accept()
