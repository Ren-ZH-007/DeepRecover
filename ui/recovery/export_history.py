from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView)
from app.theme import STYLE, icon, brand_icon


STATUS = {
    'completed': ('导出完成', '#15816c', '#e8f6f0'),
    'exported': ('摘要校验通过', '#15816c', '#e8f6f0'),
    'failed': ('导出失败', '#b45353', '#fceeee'),
    'cancelled': ('已停止', '#a87529', '#fff5e5'),
    'interrupted': ('已中断', '#7b8ba1', '#edf1f6'),
    'running': ('正在导出', '#356fbd', '#eaf2ff'),
    'queued': ('等待开始', '#7b8ba1', '#edf1f6'),
}


class ExportHistory(QDialog):
    def __init__(self, repository, task_id, parent=None):
        super().__init__(parent)
        self.repository, self.task_id, self.offset = repository, task_id, 0
        self.setWindowTitle('本地导出记录 · SHA-256 对照')
        self.setWindowIcon(brand_icon())
        self.resize(1100, 600)
        self.setStyleSheet(STYLE)
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 22, 24, 20)
        box.setSpacing(14)
        task = repository.task(task_id)
        header = QHBoxLayout(); header.setSpacing(12); box.addLayout(header)
        mark = QLabel(); mark.setPixmap(icon('export', 30, '#2563eb').pixmap(30, 30)); header.addWidget(mark)
        headings = QVBoxLayout(); headings.setSpacing(3); header.addLayout(headings, 1)
        title = QLabel('导出记录'); title.setObjectName('title'); headings.addWidget(title)
        note = QLabel('核对来源与目标摘要，追踪每个文件的实际导出结果。')
        note.setObjectName('subtitle'); headings.addWidget(note)
        status, foreground, background = STATUS.get(task['status'], (task['status'], '#7b8ba1', '#edf1f6'))
        badge = QLabel(status); badge.setObjectName('badge')
        badge.setStyleSheet(f'color: {foreground}; background: {background};')
        header.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        counts = repository.export_counts(task_id)
        summary = QLabel(f"已校验 {counts.get('exported', 0)} 项    ·    失败 {counts.get('failed', 0)} 项    ·    已停止 {counts.get('cancelled', 0)} 项")
        summary.setObjectName('sectionTitle'); box.addWidget(summary)
        label = QLabel(f"本地清单  {task['result_path'] or '尚未生成清单'}")
        label.setObjectName('sourceInfo'); label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True); label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(label)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['原始名称', '结果', '输出文件', '来源 SHA-256', '目标 SHA-256', '大小', '错误类型'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True); self.table.setShowGrid(False)
        self.table.verticalHeader().hide(); self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((165, 100, 240, 195, 195, 85, 125)):
            self.table.setColumnWidth(column, width)
        box.addWidget(self.table, 1)
        row = QHBoxLayout(); box.addLayout(row)
        self.previous = QPushButton('上一页'); self.previous.setIcon(icon('arrow-left', 15, '#536d8d')); row.addWidget(self.previous)
        self.previous.setProperty('kind', 'secondary')
        self.page = QLabel(); self.page.setObjectName('sectionNote'); self.page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.page, 1)
        self.next = QPushButton('下一页'); self.next.setIcon(icon('arrow-right', 15, '#536d8d')); row.addWidget(self.next)
        self.next.setProperty('kind', 'secondary')
        self.previous.clicked.connect(lambda: self.move(-100))
        self.next.clicked.connect(lambda: self.move(100))
        footnote = QLabel('摘要一致仅证明本次复制的字节相同，不表示已删除数据恢复成功。历史记录不会自动重新校验目标。')
        footnote.setObjectName('sectionNote'); footnote.setWordWrap(True); box.addWidget(footnote)
        self.load()

    def move(self, amount):
        self.offset = max(0, self.offset + amount)
        self.load()

    def load(self):
        total = self.repository.export_count(self.task_id)
        records = self.repository.exports(self.task_id, offset=self.offset, limit=100)
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = (record['metadata']['name'], record['status'], record['output_path'],
                      record['source_sha256'] or '', record['target_sha256'] or '', record['size'], record['error_code'] or '')
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setToolTip(str(value))
                if column == 1:
                    explanation, foreground, background = STATUS.get(record['status'], (record['status'], '#7b8ba1', '#edf1f6'))
                    item.setToolTip(explanation); item.setForeground(QColor(foreground)); item.setBackground(QColor(background))
                    font = item.font(); font.setBold(True); item.setFont(font)
                self.table.setItem(row, column, item)
        self.page.setText(f'{self.offset+1 if total else 0}–{self.offset+len(records)} / {total} 条')
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + len(records) < total)
