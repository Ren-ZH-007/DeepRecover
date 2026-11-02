from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QMainWindow, QPushButton, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView)
from app.navigation import PAGES
from core.tasks import TaskManager, TaskCancelled
from core.device.android.adapter import AndroidAdapter, AdbError
from core.device.ios.discovery import discover_backups
from core.parser.ios_backup.manifest import read_manifest, BackupError

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
        self.setWindowTitle('DeepRecover · 第一阶段工程版 0.1.0')
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
        self.statusBar().showMessage('本地处理 · 工程初始化版本 · 未执行设备操作')

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
        box = self.page('DeepRecover', '手机数据恢复工作台 / 第一阶段 · 工程基础')
        label = QLabel('已就绪\n\n• 本地数据库与版本迁移\n• Android ADB 连接状态检测\n• iPhone 本地备份发现与只读索引\n• 后台任务、取消和任务记录\n\n下一里程碑\n\n授权文件采集 → 连续文件签名扫描 → 预览 → 导出及 SHA-256 校验')
        label.setWordWrap(True); box.addWidget(label)
        box.addStretch()
        box.addWidget(QLabel('索引中的文件仅为候选记录，不代表已恢复或已验证完整。'))
        box = self.page('Android 设备', '通过 ADB 检测 USB 调试设备。需由你在手机上允许调试授权；本版本仅检测连接状态。')
        self.button(box, '检测 ADB 与设备', self.detect_android)
        self.android_table = self.table(['设备序列号', '型号', '状态']); box.addWidget(self.android_table)
        box = self.page('iPhone 本地备份', '发现 Apple Devices / iTunes 备份。仅支持未加密备份；解析上限为 10,000 条文件记录，不提取文件。')
        row = QHBoxLayout(); box.addLayout(row)
        self.button(row, '自动发现备份', lambda: self.detect_backups(None))
        self.button(row, '选择备份目录', self.choose_backup)
        self.button(row, '读取选中备份索引', self.parse_backup)
        self.backup_table = self.table(['设备', 'iOS 版本', '加密状态', '备份时间', '目录']); box.addWidget(self.backup_table)
        box = self.page('文件扫描', '扫描引擎接口已建立，文件签名扫描将在下一里程碑实现。')
        button = QPushButton('开始扫描 · 待实现'); button.setEnabled(False); box.addWidget(button); box.addStretch()
        box = self.page('恢复结果 / 候选索引', '当前展示 iPhone 备份索引候选。未检查文件结构，不计为成功恢复。预览及导出尚未接入。')
        self.result_table = self.table(['文件名', '类型', '大小 / 字节', '完整性', '原始路径']); box.addWidget(self.result_table)
        box = self.page('任务记录', '显示最近 200 条任务。异常退出后的未完成任务标记为 interrupted；暂不支持断点续扫。')
        self.button(box, '刷新任务记录', self.refresh_history)
        self.button(box, '停止当前任务', self.cancel_task)
        self.history_table = self.table(['任务类型', '状态', '更新时间 / UTC', '错误类型']); box.addWidget(self.history_table)
        box = self.page('设置', '当前版本使用环境变量配置，下次启动生效。')
        label = QLabel(f'工作目录：{self.config.data_dir}\n\nDEEPRECOVER_DATA_DIR：工作数据目录\nDEEPRECOVER_ADB：ADB 可执行文件完整路径\n\n后台线程：2\n日志：按大小轮转，不记录文件内容或设备序列号\n数据：仅保存在本机')
        label.setWordWrap(True); label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(label); box.addStretch()

    def run_task(self, kind, operation, on_success, *, persist_candidates=False):
        if self.pending:
            self.statusBar().showMessage('已有任务执行中，请等待或在任务记录页停止。'); return
        self.pending = (self.manager.submit(kind, operation, persist_candidates=persist_candidates), on_success)
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
        self.run_task('android_discovery', lambda token: AndroidAdapter().devices(),
                      lambda devices, _: self.fill(self.android_table, [(d.serial, d.model, d.state) for d in devices]))

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
        def complete(candidates, task_id):
            self.fill(self.result_table, [(c.name, c.file_type, c.size, c.integrity, c.original_path) for c in candidates])
            self.nav.setCurrentRow(4)
        self.run_task('ios_manifest', lambda token: read_manifest(path, token), complete, persist_candidates=True)

    def refresh_history(self, *_):
        self.fill(self.history_table, [(t['kind'], t['status'], t['updated_at'], t['error_code'] or '') for t in self.repository.tasks()])

    def cancel_task(self):
        if self.pending:
            self.pending[0].cancel()
            self.statusBar().showMessage('已请求停止；ADB 调用最长等待 15 秒。')

    def closeEvent(self, event):
        if self.pending and not self.pending[0].future.done():
            self.cancel_task()
            self.statusBar().showMessage('正在停止任务，请在任务结束后再次关闭窗口。')
            event.ignore(); return
        self.timer.stop()
        self.manager.shutdown()
        event.accept()
