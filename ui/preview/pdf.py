"""PDF raster pages with cancellable rendering; no browser, links or forms."""
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout

from app.theme import STYLE
from core.candidate_io import CandidateAccessError
from core.preview.pdf import render_pdf, ZOOMS
from core.tasks import CancellationToken, TaskCancelled


class PdfPreviewDialog(QDialog):
    def __init__(self, preview, parent=None):
        super().__init__(parent)
        self.setWindowTitle('本地 PDF 静态预览'); self.setStyleSheet(STYLE)
        self.resize(900,800); self.setMinimumSize(520,400)
        self._closed=False;self._pending=None;self._image=QImage(preview.image)
        self.data=preview.data;self.pages=preview.pages;self.page=0;self.zoom=100
        self.rendered_pages=1;self._executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='pdf-page')
        box=QVBoxLayout(self)
        title=QLabel('已校验来源的 PDF · 静态页面');title.setObjectName('sectionTitle');box.addWidget(title)
        row=QHBoxLayout();box.addLayout(row)
        self.previous_button=QPushButton('上一页');self.previous_button.clicked.connect(lambda:self.change_page(-1));row.addWidget(self.previous_button)
        self.page_label=QLabel();row.addWidget(self.page_label)
        self.next_button=QPushButton('下一页');self.next_button.clicked.connect(lambda:self.change_page(1));row.addWidget(self.next_button)
        row.addStretch()
        self.zoom_combo=QComboBox()
        self.zoom_combo.setAccessibleName('PDF 缩放比例')
        for value in ZOOMS:self.zoom_combo.addItem(f'{value}%',value)
        self.zoom_combo.setCurrentIndex(self.zoom_combo.findData(100));self.zoom_combo.currentIndexChanged.connect(self.change_zoom);row.addWidget(self.zoom_combo)
        self.scroll=QScrollArea();self.scroll.setWidgetResizable(False);box.addWidget(self.scroll,1)
        self.canvas=QLabel();self.canvas.setObjectName('previewCanvas');self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.canvas)
        self.status=QLabel('只在内存中展示页面；不执行脚本、表单或外部引用。');self.status.setObjectName('sectionNote');self.status.setWordWrap(True);box.addWidget(self.status)
        row=QHBoxLayout();box.addLayout(row)
        self.stop_button=QPushButton('停止渲染');self.stop_button.clicked.connect(self.cancel_render);row.addWidget(self.stop_button)
        row.addStretch();close=QPushButton('关闭');close.clicked.connect(self.close);row.addWidget(close)
        note=QLabel('输入最多 16 MiB、200 页；单页最多 400 万像素、8 秒。预览不修改来源或候选，不证明文件曾被删除或全部内容完整。')
        note.setObjectName('sectionNote');note.setWordWrap(True);box.addWidget(note)
        self.timer=QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(30)
        self._display();self._controls()

    def _display(self):
        self.canvas.setPixmap(QPixmap.fromImage(self._image));self.canvas.resize(self._image.size())
        self.page_label.setText(f'第 {self.page+1} / {self.pages} 页')
        self.scroll.verticalScrollBar().setValue(0);self.scroll.horizontalScrollBar().setValue(0)

    def _controls(self):
        busy=self._pending is not None
        self.previous_button.setEnabled(not busy and self.page>0)
        self.next_button.setEnabled(not busy and self.page+1<self.pages)
        self.zoom_combo.setEnabled(not busy);self.stop_button.setEnabled(busy)

    def change_page(self,delta):
        target=self.page+delta
        if not self._pending and 0<=target<self.pages:self.start_render(target,self.zoom)

    def change_zoom(self,*_):
        target=self.zoom_combo.currentData()
        if not self._closed and not self._pending and target!=self.zoom:self.start_render(self.page,target)

    def start_render(self,page,zoom):
        if self._closed or self._pending:return
        token=CancellationToken();data=self.data
        future=self._executor.submit(render_pdf,data,page,zoom,token)
        self._pending=(future,token,page,zoom)
        self.status.setText('正在渲染页面…可随时停止。')
        self.canvas.clear();self._image=QImage();self._controls()

    def poll(self):
        if not self._pending or not self._pending[0].done():return
        future,token,page,zoom=self._pending;self._pending=None
        try:
            image=future.result();token.checkpoint()
        except TaskCancelled:
            self.canvas.clear();self._image=QImage();self.status.setText('渲染已停止，页面已清除。可重新选择页码或缩放。')
        except Exception as error:
            self.canvas.clear();self._image=QImage()
            self.status.setText(str(error) if isinstance(error,CandidateAccessError) else 'PDF 页面渲染失败，未改变候选状态。')
        else:
            self._image=image;self.page=page;self.zoom=zoom;self.rendered_pages+=1
            self._display();self.status.setText('页面已渲染；不代表全篇完整或已删除数据恢复成功。')
        self.zoom_combo.blockSignals(True);self.zoom_combo.setCurrentIndex(self.zoom_combo.findData(self.zoom));self.zoom_combo.blockSignals(False)
        self._controls()

    def cancel_render(self):
        if self._pending:self._pending[1].cancel();self.status.setText('正在结束渲染进程…')

    def done(self,result):
        if not self._closed:
            self._closed=True;self.timer.stop();self.cancel_render();self._pending=None
            self._executor.shutdown(wait=False,cancel_futures=True)
            self.data=b'';self._image=QImage();self.canvas.clear()
        super().done(result)

    def closeEvent(self,event):
        self.done(0);event.accept()
