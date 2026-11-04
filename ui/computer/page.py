"""Computer sources share the existing task, preview, export and history flow."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel,
    QMessageBox, QProgressBar, QScrollArea, QVBoxLayout, QWidget, QFileDialog)

from storage.models.source import DataSource


METADATA_FILESYSTEMS = {'NTFS', 'FAT32', 'EXFAT'}


def list_computer_devices():
    from core.computer.devices import list_devices
    return list_devices()


def inspect_computer_source(path, kind, data_dir):
    if kind == 'device':
        from core.computer.devices import ensure_different_device
        ensure_different_device(path, data_dir)
    from core.computer.service import inspect_source
    return inspect_source(path, kind=kind)


def guard_computer_source(path, kind, data_dir):
    if kind == 'device':
        from core.computer.devices import ensure_different_device
        ensure_different_device(path, data_dir)


def scan_computer_source(path, token, *, kind, mode, partition_offset, data_dir):
    from core.computer.service import ComputerScanner
    return ComputerScanner(data_dir=data_dir).scan(path, token, kind=kind, mode=mode,
                                  partition_offset=partition_offset)


def check_computer_access(repository, candidate_ids, data_dir, destination=None):
    from core.computer.service import check_access
    return check_access(repository, candidate_ids, data_dir, destination=destination)


def computer_error_types():
    from core.computer.io import DiskError
    try:
        from core.computer.service import ComputerError
    except ImportError:
        return (DiskError,)
    return (ComputerError, DiskError)


def readable_size(value):
    value = int(value)
    for suffix in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or suffix == 'TiB':
            return f'{value:.1f} {suffix}' if suffix != 'B' else f'{value} B'
        value /= 1024


class ComputerPage(QWidget):
    """Purely read-only probes run before a task can write the local index."""
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.source_path = None
        self.source_kind = None
        self.devices = []
        self.partitions = []
        self.last_task_id = None
        self.active_task_id = None
        self._future = None
        self._readonly_callback = None
        self._readonly_error = None
        self._cancel_requested = False
        self.setObjectName('page')
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setObjectName('pageScroll')
        scroll.setFrameShape(QFrame.Shape.NoFrame); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.page_scroll = scroll
        outer.addWidget(scroll)
        content = QWidget(); content.setObjectName('page'); scroll.setWidget(content)
        box = QVBoxLayout(content); box.setContentsMargins(24, 22, 24, 18); box.setSpacing(10)
        eyebrow = QLabel('DEEPRECOVER  /  COMPUTER'); eyebrow.setObjectName('pageEyebrow'); box.addWidget(eyebrow)
        title = QLabel('电脑磁盘与镜像恢复'); title.setObjectName('title'); box.addWidget(title)
        subtitle = QLabel('选择 Windows / macOS 磁盘、U 盘或镜像，以只读方式检查删除记录或连续图片签名。')
        subtitle.setObjectName('subtitle'); subtitle.setWordWrap(True); box.addWidget(subtitle)

        source = window.card(box, '01   选择只读来源')
        row = QHBoxLayout(); source.addLayout(row)
        self.image_button = window.button(row, '选择磁盘镜像', self.choose_image, 'primary', 'folder')
        self.devices_button = window.button(row, '检测本机磁盘 / U 盘', self.detect_devices, 'secondary', 'refresh')
        row.addStretch()
        self.device_table = window.table(['磁盘路径', '容量', '设备信息'])
        self.device_table.setMinimumHeight(120); self.device_table.setMaximumHeight(180)
        self.device_table.setToolTip('检测不会自动扫描。选中磁盘后再检测分区；不会提权、卸载或写入磁盘。')
        source.addWidget(self.device_table)
        row = QHBoxLayout(); source.addLayout(row)
        self.use_device_button = window.button(row, '使用选中磁盘并检测分区', self.choose_device, 'secondary', 'disk')
        self.reprobe_button = window.button(row, '重新检测当前来源', self.inspect_selected, 'secondary', 'refresh')
        row.addStretch()
        self.source_label = QLabel('尚未选择来源。磁盘检测与分区预检不会建立恢复任务。')
        self.source_label.setObjectName('sourceInfo'); self.source_label.setWordWrap(True)
        self.source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        source.addWidget(self.source_label)

        work = window.card(box, '02   选择分区与扫描方式')
        self.partition_combo = QComboBox(); self.partition_combo.setMinimumContentsLength(14)
        self.partition_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        work.addWidget(self.partition_combo)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem('解析删除记录（NTFS / FAT32 / exFAT）', 'deleted')
        self.mode_combo.addItem('JPG / PNG 签名候选（不判断是否已删除）', 'signature')
        work.addWidget(self.mode_combo)
        self.capability_label = QLabel('先选择来源并完成分区检测。')
        self.capability_label.setObjectName('sectionNote'); self.capability_label.setWordWrap(True)
        work.addWidget(self.capability_label)
        row = QHBoxLayout(); work.addLayout(row)
        self.start_button = window.button(row, '开始电脑扫描', self.start_scan, 'primary', 'play')
        self.stop_button = window.button(row, '停止', self.cancel, 'danger', 'stop')
        row.addStretch()
        self.results_button = window.button(row, '打开上次电脑结果', self.open_results, 'secondary', 'results')
        self.history_button = window.button(row, '任务记录', lambda: window.nav.setCurrentRow(5), 'secondary', 'history')
        self.progress = QProgressBar(); self.progress.setRange(0, 1000); self.progress.setValue(0)
        work.addWidget(self.progress)
        self.status = QLabel('等待选择来源。候选保存在本地，可分页查看、按类型筛选并校验导出。')
        self.status.setObjectName('sectionNote'); self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText); work.addWidget(self.status)
        self.directory_help_button = window.button(work, '查看工作目录设置', self.show_work_directory,
                                                   'secondary', 'folder')

        paths = window.card(box, '工作目录必须与源磁盘分开')
        directory = QLabel(str(window.config.data_dir)); directory.setObjectName('sourceInfo')
        self.work_directory_label = directory
        directory.setTextFormat(Qt.TextFormat.PlainText); directory.setWordWrap(True)
        directory.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); paths.addWidget(directory)
        note = QLabel('扫描原始磁盘前会检查工作目录是否位于其他物理磁盘。请先将 DEEPRECOVER_DATA_DIR 指向其他磁盘并重启；导出目标也必须与原始来源分开。')
        note.setObjectName('sectionNote'); note.setWordWrap(True); paths.addWidget(note)
        limits = window.card(box, '恢复依据与能力范围')
        for text in (
            '删除记录是文件系统元数据证据；数据可能被覆盖。只有布局和来源检查通过的候选可预览或导出，导出另行核对 SHA-256。',
            'NTFS / FAT32 / exFAT 的支持取决于剩余元数据与分配状态。受限、压缩、加密、布局未知的记录保留限制状态，不标记恢复成功。',
            'APFS / HFS+ 等来源可做连续 JPG / PNG 签名扫描；本版不解析其删除记录。签名候选可能来自现存文件，不能证明曾被删除。',
            '本程序只读访问来源，不提权、不卸载、不修复文件系统。正在使用的系统盘仍可能被其他程序写入；稳定镜像更适合验证。',
        ):
            label = QLabel(text); label.setObjectName('sectionNote'); label.setWordWrap(True); limits.addWidget(label)
        box.addStretch(1)
        self.partition_combo.currentIndexChanged.connect(self.partition_changed)
        self.mode_combo.currentIndexChanged.connect(self.refresh_controls)
        self.refresh_controls()

    def show_work_directory(self):
        self.page_scroll.ensureWidgetVisible(self.work_directory_label)

    @property
    def busy(self):
        return self._future is not None and not self._future.done()

    @property
    def readonly_pending(self):
        return self._future is not None

    def occupied(self):
        return self._future is not None or self.window.pending is not None

    def refresh_controls(self, *_):
        occupied = self.occupied()
        for button in (self.image_button, self.devices_button, self.use_device_button, self.reprobe_button):
            button.setEnabled(not occupied)
        self.partition_combo.setEnabled(not occupied and bool(self.partitions))
        self.mode_combo.setEnabled(not occupied and bool(self.partitions))
        partition = self.current_partition()
        supported = bool(partition) and (self.mode_combo.currentData() == 'signature' or
                     str(partition.file_system).upper() in METADATA_FILESYSTEMS)
        self.start_button.setEnabled(not occupied and bool(self.source_path) and supported)
        pending = self.window.pending
        self.stop_button.setEnabled(self._future is not None or bool(pending and
            self.window.repository.task(pending[0].id)['kind'] == 'computer_scan'))
        self.results_button.setEnabled(self.last_task_id is not None)

    def current_partition(self):
        index = self.partition_combo.currentIndex()
        return self.partitions[index] if 0 <= index < len(self.partitions) else None

    def partition_changed(self, *_):
        partition = self.current_partition()
        if partition is not None:
            fs = str(partition.file_system)
            if fs.upper() not in METADATA_FILESYSTEMS:
                self.mode_combo.setCurrentIndex(self.mode_combo.findData('signature'))
                self.capability_label.setText(f'{fs}：本版仅提供连续图片签名候选，不解析已删除记录；加密、覆盖或 TRIM 后的数据可能不可读。')
            else:
                self.capability_label.setText(f'{fs}：可检查删除记录。布局不明或簇已再分配的记录不会被当成可校验导出的完整文件。')
        self.refresh_controls()

    def _set_source(self, path, kind):
        self.source_path, self.source_kind = str(path), kind
        self.partitions = []
        self.partition_combo.clear()
        self.source_label.setText(('磁盘' if kind == 'device' else '镜像') + ' · ' + str(path))
        self.source_label.setToolTip(str(path)); self.progress.setValue(0)
        self.refresh_controls()

    def _readonly(self, operation, callback, message, on_error=None):
        if self.occupied():
            self.window.statusBar().showMessage('已有任务或只读预检执行中，请等待或停止。'); return
        self._cancel_requested = False
        self._readonly_callback = callback
        self._readonly_error = on_error
        self._future = self.window.manager.executor.submit(operation)
        self.status.setText(message); self.window.statusBar().showMessage(message); self.refresh_controls()

    def choose_image(self):
        if self.occupied(): return
        name, _ = QFileDialog.getOpenFileName(self.window, '选择电脑磁盘镜像', '',
            '原始磁盘镜像 (*.img *.dd *.raw *.bin);;所有文件 (*)')
        if not name: return
        path = Path(name).resolve()
        config = self.window.config
        if (not path.is_file() or path == config.database.resolve() or
                any(path.is_relative_to((config.data_dir / name).resolve()) for name in ('Logs', 'Acquisitions'))):
            self.status.setText('请选择普通磁盘镜像；不能选择程序数据库、日志或采集副本。'); return
        self._set_source(path, 'image'); self.inspect_selected()

    def detect_devices(self):
        def complete(devices):
            self.devices = devices
            self.window.fill(self.device_table, [(device.path, readable_size(device.size),
                device.label + (' · 系统盘 / 可能仍在写入' if device.system_disk else '')) for device in devices])
            self.device_table.clearSelection(); self.device_table.setCurrentCell(-1, -1)
            self.status.setText(f'检测到 {len(devices)} 个来源。不会自动扫描，请明确选择磁盘。')
        self._readonly(list_computer_devices, complete, '正在只读检测磁盘与 U 盘…')

    def choose_device(self):
        if self.occupied(): return
        row = self.device_table.currentRow()
        if not 0 <= row < len(self.devices):
            self.status.setText('请先检测并选中一个磁盘。'); return
        device = self.devices[row]
        self._set_source(device.path, 'device'); self.inspect_selected()

    def inspect_selected(self):
        if not self.source_path or self.occupied(): return
        path, kind = self.source_path, self.source_kind
        def complete(partitions):
            self.partitions = list(partitions)
            self.partition_combo.clear()
            for partition in self.partitions:
                self.partition_combo.addItem(f'{partition.label or partition.file_system} · {partition.file_system} · '
                    f'{readable_size(partition.size)} · 起始 {partition.offset}')
            supported = next((index for index, partition in enumerate(self.partitions)
                              if str(partition.file_system).upper() in METADATA_FILESYSTEMS), None)
            if supported is not None:
                self.partition_combo.setCurrentIndex(supported)
                self.mode_combo.setCurrentIndex(self.mode_combo.findData('deleted'))
            self.partition_changed()
            self.status.setText(f'只读预检完成，发现 {len(self.partitions)} 个可选范围。请选择扫描方式。' if self.partitions else '未找到可选范围，请检查镜像格式与设备状态。')
        self._readonly(lambda: inspect_computer_source(path, kind, self.window.config.data_dir), complete,
                       '正在检查源盘与工作目录隔离，并检测分区…')

    def start_scan(self):
        partition = self.current_partition()
        if self.occupied() or not self.source_path or partition is None: return
        path, kind, mode = self.source_path, self.source_kind, self.mode_combo.currentData()
        if mode == 'deleted' and str(partition.file_system).upper() not in METADATA_FILESYSTEMS:
            self.status.setText('此文件系统暂不支持删除记录解析，请选择签名候选扫描。'); return
        text = (f'来源：{path}\n范围：{partition.label or partition.file_system}，起始 {partition.offset}\n'
                '仅只读访问来源。扫描只产生候选，数据可能已被覆盖；导出另行校验并保存到其他位置。\n是否开始？')
        if QMessageBox.question(self.window, '确认只读电脑扫描', text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        def begin(_):
            self.progress.setValue(0)
            self.status.setText('正在只读扫描，结果将保存在本地索引…')
            def complete(_count, task_id):
                self.last_task_id = task_id
                self.progress.setValue(1000)
                self.window.open_results(task_id)
                self.refresh_controls()
            self.window.run_task('computer_scan', lambda token: scan_computer_source(path, token,
                kind=kind, mode=mode, partition_offset=partition.offset, data_dir=self.window.config.data_dir), complete,
                source=DataSource('computer', path, str(partition.file_system)),
                persist_candidates=True, batch_size=1)
            if self.window.pending: self.active_task_id = self.window.pending[0].id
            self.refresh_controls()
        self._readonly(lambda: guard_computer_source(path, kind, self.window.config.data_dir), begin,
                       '正在重新确认工作目录不在源磁盘上…')

    def cancel(self):
        if self._future is not None:
            self._cancel_requested = True
            self._future.cancel()
            self.status.setText('已请求停止只读预检，正在等待当前系统调用返回。')
        elif self.window.pending and self.window.repository.task(self.window.pending[0].id)['kind'] == 'computer_scan':
            self.window.cancel_task()

    def prepare_close(self):
        if self._future is not None:
            self.cancel()
            if self.busy: return False
        return True

    def open_results(self):
        if self.last_task_id: self.window.open_results(self.last_task_id)

    def poll(self):
        if self._future is not None and self._future.done():
            future, callback = self._future, self._readonly_callback
            on_error = self._readonly_error
            cancelled = self._cancel_requested
            self._future = self._readonly_callback = self._readonly_error = None
            try:
                result = future.result()
                if not cancelled: callback(result)
                else:
                    self.status.setText('只读预检已停止，未建立恢复任务。')
                    if on_error: on_error(self.status.text())
            except Exception as error:
                if cancelled:
                    self.status.setText('只读预检已停止，未建立恢复任务。')
                    if on_error: on_error(self.status.text())
                else:
                    message = f'只读预检未完成：{error}'
                    if self.source_kind == 'device':
                        message += ('\n当前工作目录：' + str(self.window.config.data_dir) +
                            '\n扫描原始盘时，工作目录必须位于另一块物理磁盘；同盘其他分区也不满足要求。'
                            '请查看工作目录设置，修改后重启，或改选已有的稳定镜像。')
                    self.status.setText(message)
                    self.window.statusBar().showMessage(self.status.text())
                    if on_error: on_error(self.status.text())
            self.refresh_controls()
        if self.active_task_id:
            task = self.window.repository.task(self.active_task_id)
            if task['status'] not in ('running', 'queued'):
                self.last_task_id = self.active_task_id
                count = self.window.repository.candidate_count(self.active_task_id)
                self.status.setText(f"任务状态：{task['status']} · 已入库 {count} 条候选。完整性与删除依据请查看结果。")
                if task['status'] == 'completed': self.progress.setValue(1000)
                self.active_task_id = None
                self.refresh_controls()
        pending = self.window.pending
        if pending and self.window.repository.task(pending[0].id)['kind'] == 'computer_scan':
            progress = pending[0].token.progress
            if progress is not None:
                self.progress.setValue(progress.processed * 1000 // progress.total if progress.total else 0)
                self.status.setText(f'已处理 {progress.processed} / {progress.total} · {progress.candidates} 条候选（不等于恢复成功）')
