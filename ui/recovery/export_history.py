from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QAbstractItemView


class ExportHistory(QDialog):
    def __init__(self, repository, task_id, parent=None):
        super().__init__(parent)
        self.repository, self.task_id, self.offset = repository, task_id, 0
        self.setWindowTitle('本地导出记录 · SHA-256 对照')
        self.resize(1000, 540)
        box = QVBoxLayout(self)
        task = repository.task(task_id)
        label = QLabel(f"任务状态：{task['status']}\n本地清单：{task['result_path'] or '未生成'}\nexported 仅表示源范围与目标文件摘要相同；不证明已删除数据恢复成功，历史记录不自动重验目标。")
        label.setTextFormat(Qt.TextFormat.PlainText); label.setWordWrap(True); box.addWidget(label)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['原始名称', '结果', '输出文件', '来源 SHA-256', '目标 SHA-256', '大小', '错误类型'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for column in range(7): self.table.setColumnWidth(column, 150)
        box.addWidget(self.table)
        row = QHBoxLayout(); box.addLayout(row)
        self.previous = QPushButton('上一页'); row.addWidget(self.previous)
        self.page = QLabel(); row.addWidget(self.page)
        self.next = QPushButton('下一页'); row.addWidget(self.next)
        self.previous.clicked.connect(lambda: self.move(-100))
        self.next.clicked.connect(lambda: self.move(100))
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
                item = QTableWidgetItem(str(value)); item.setToolTip(str(value)); self.table.setItem(row, column, item)
        self.page.setText(f'{self.offset+1 if total else 0}–{self.offset+len(records)} / {total} 条')
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + len(records) < total)
