"""A directory chooser that performs no filesystem writes."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from app.theme import STYLE


class WorkspaceDialog(QDialog):
    def __init__(self, default, parent=None):
        super().__init__(parent)
        self.setWindowTitle('选择本地工作目录')
        self.setStyleSheet(STYLE)
        self.resize(660, 330)
        box = QVBoxLayout(self); box.setContentsMargins(22, 22, 22, 22)
        title = QLabel('选择本次使用的工作目录'); title.setObjectName('sectionTitle'); box.addWidget(title)
        note = QLabel('任务历史、日志和导出记录保存在此目录。扫描原始磁盘时，请选择另一块物理磁盘上的目录。')
        note.setWordWrap(True); box.addWidget(note)
        row = QHBoxLayout(); box.addLayout(row)
        self.path_edit = QLineEdit(str(default)); self.path_edit.setAccessibleName('工作目录路径'); row.addWidget(self.path_edit, 1)
        self.browse_button = QPushButton('浏览目录'); self.browse_button.clicked.connect(self.browse); row.addWidget(self.browse_button)
        self.browse_button.setProperty('kind', 'secondary')
        info = QLabel('可以输入新目录路径，打开前不会创建目录。已有工作目录会读取其历史；原目录的数据不会自动迁移。每次启动可重新选择，也支持已有的环境变量配置。')
        info.setWordWrap(True); info.setObjectName('sectionNote'); box.addWidget(info)
        self.error = QLabel(); self.error.setWordWrap(True); self.error.setTextFormat(Qt.TextFormat.PlainText); box.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Open).setText('打开工作目录')
        buttons.button(QDialogButtonBox.StandardButton.Open).setProperty('kind', 'primary')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); box.addWidget(buttons)

    def browse(self):
        directory = QFileDialog.getExistingDirectory(self, '选择已有目录', self.path_edit.text(),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.ReadOnly)
        if directory: self.path_edit.setText(directory)

    def accept(self):
        if not self.path_edit.text().strip():
            self.error.setText('请选择或输入工作目录。'); return
        try:
            path = Path(self.path_edit.text()).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            self.error.setText('目录路径无法解析，请检查权限和路径。'); return
        if path.exists() and not path.is_dir():
            self.error.setText('工作目录必须是文件夹。'); return
        self.selected_path = path
        super().accept()

    @classmethod
    def select(cls, default, parent=None):
        dialog = cls(default, parent)
        return dialog.selected_path if dialog.exec() == QDialog.DialogCode.Accepted else None
