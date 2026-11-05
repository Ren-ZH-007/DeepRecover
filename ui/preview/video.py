"""Silent playback from a verified, owned memory buffer, never a file URL."""
from PySide6.QtCore import (QByteArray, QBuffer, QEventLoop, QIODevice,
    QLoggingCategory, QMetaObject, QUrl, Q_ARG, Qt, QTimer)
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout

from app.theme import STYLE
from core.preview.video import MAX_DURATION_MS, dimensions_allowed


def time_label(milliseconds):
    seconds = max(0, milliseconds) // 1000
    return f'{seconds // 60}:{seconds % 60:02d}'


class VideoPreviewDialog(QDialog):
    def __init__(self, preview, parent=None):
        super().__init__(parent)
        # Qt/FFmpeg diagnostics can include media metadata. Keep those categories
        # out of normal application output; user-facing errors are fixed text.
        QLoggingCategory.setFilterRules('qt.multimedia.*=false')
        self.setWindowTitle('本地视频预览 · 静音')
        self.setStyleSheet(STYLE)
        self.resize(800, 560)
        self.setMinimumSize(520, 390)
        self._closed = False
        self._failed = False
        self._releasing = False
        self._first_frame = True
        self._image = QImage()
        self.frame_count = 0
        self.duration_ms = preview.duration_ms
        box = QVBoxLayout(self)
        title = QLabel('已校验来源的本地视频 · 静音预览'); title.setObjectName('sectionTitle'); box.addWidget(title)
        self.canvas = QLabel('正在加载视频首帧…')
        self.canvas.setObjectName('previewCanvas')
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setMinimumSize(320, 180)
        box.addWidget(self.canvas, 1)
        self.status = QLabel('正在从内存加载；未生成预览文件。')
        self.status.setObjectName('sectionNote'); self.status.setWordWrap(True); box.addWidget(self.status)
        row = QHBoxLayout(); box.addLayout(row)
        self.play_button = QPushButton('播放'); self.play_button.setProperty('kind', 'primary')
        self.play_button.setEnabled(False); self.play_button.clicked.connect(self.toggle_play); row.addWidget(self.play_button)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setAccessibleName('视频播放进度')
        self.seek_slider.setRange(0, self.duration_ms); self.seek_slider.setEnabled(False)
        self.seek_slider.sliderReleased.connect(self.seek); row.addWidget(self.seek_slider, 1)
        self.position_label = QLabel(f'0:00 / {time_label(self.duration_ms)}'); row.addWidget(self.position_label)
        close = QPushButton('停止并关闭'); close.clicked.connect(self.close); row.addWidget(close)
        note = QLabel('只播放校验时读入的内存内容，不修改来源或候选状态。能播放不代表文件完整，也不证明数据曾被删除。')
        note.setObjectName('sectionNote'); note.setWordWrap(True); box.addWidget(note)
        self.buffer = QBuffer(self)
        self.buffer.setData(QByteArray(preview.data)); self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.player = QMediaPlayer(self)
        # No QAudioOutput is created: preview never plays sound.
        self.sink = QVideoSink(self); self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._frame)
        self.player.errorOccurred.connect(lambda *_: self.fail())
        self.player.durationChanged.connect(self._duration)
        self.player.positionChanged.connect(self._position)
        self.player.playbackStateChanged.connect(self._state)
        self.player.seekableChanged.connect(self._seekable)
        self.player.mediaStatusChanged.connect(self._media_status)
        self.load_timer = QTimer(self); self.load_timer.setSingleShot(True)
        self.load_timer.timeout.connect(lambda: self.fail('视频加载超时，已停止预览。可导出后使用本机播放器检查。'))
        self.load_timer.start(10000)
        self.player.setSourceDevice(self.buffer)
        self.player.play()

    def _frame(self, frame):
        if self._closed or self._failed or not frame.isValid():
            return
        size = frame.size()
        if not dimensions_allowed(size.width(), size.height()):
            self.fail('解码后的尺寸超出预览上限，已停止。'); return
        image = frame.toImage()
        if image.isNull():
            self.fail(); return
        if not dimensions_allowed(image.width(), image.height()):
            self.fail('旋转后的画面尺寸超出 1080p 预览上限，已停止。'); return
        self._image = image
        self.frame_count += 1
        self._render()
        self.load_timer.stop()
        self.play_button.setEnabled(True)
        self.seek_slider.setEnabled(self.player.isSeekable())
        if self._first_frame:
            self._first_frame = False
            self.player.pause()
            self.status.setText('首帧已解码。点击播放或拖动进度；始终静音。')

    def _render(self):
        if not self._image.isNull():
            self.canvas.setPixmap(QPixmap.fromImage(self._image).scaled(
                self.canvas.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    def _duration(self, duration):
        if self._closed or self._failed or not duration:
            return
        if not 0 < duration <= MAX_DURATION_MS:
            self.fail('视频实际时长超出 10 分钟预览上限，已停止。'); return
        self.duration_ms = duration
        self.seek_slider.setRange(0, duration)
        self._position(self.player.position())

    def _position(self, position):
        if self._closed or self._failed:
            return
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position)
        self.position_label.setText(f'{time_label(position)} / {time_label(self.duration_ms)}')

    def _seekable(self, allowed):
        self.seek_slider.setEnabled(bool(allowed) and not self._first_frame and not self._closed and not self._failed)

    def _state(self, state):
        if not self._closed and not self._failed:
            self.play_button.setText('暂停' if state == QMediaPlayer.PlaybackState.PlayingState else '播放')

    def _media_status(self, status):
        if self._closed or self._failed:
            return
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.fail()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.play_button.setText('重新播放')
            self.status.setText('播放已结束；预览没有改变候选状态。')

    def toggle_play(self):
        if self._closed or self._failed or self._first_frame:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause(); self.status.setText('已暂停 · 静音预览。')
        else:
            if self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                self.player.setPosition(0)
            self.player.play(); self.status.setText('正在播放 · 静音预览；来源保持只读。')

    def seek(self):
        if self._closed or self._failed or self._first_frame or not self.player.isSeekable():
            return
        self.player.setPosition(self.seek_slider.value())

    def fail(self, message=None):
        if self._closed or self._failed:
            return
        self._failed = True
        self.load_timer.stop()
        self.play_button.setEnabled(False); self.seek_slider.setEnabled(False)
        self._release()
        self.canvas.setText('未能预览此视频')
        self.status.setText(message or '视频无法解码或本机缺少支持的解码器，已停止。可导出后使用本机播放器检查。')

    def _release(self):
        if self._releasing:
            return
        self._releasing = True
        # A direct PySide setSourceDevice(None) can keep the GIL while joining
        # FFmpeg's reader, which may itself need the GIL for QIODevice virtuals.
        # Invoke the native Qt slot from its event queue, with the GIL released
        # by exec(). No user input is processed during this short teardown.
        if self.player.sourceDevice() is not None:
            loop = QEventLoop(self)
            poll = QTimer(loop); poll.setInterval(10)
            poll.timeout.connect(lambda: loop.quit() if self.player.sourceDevice() is None else None)
            limit = QTimer(loop); limit.setSingleShot(True); limit.timeout.connect(loop.quit)
            QMetaObject.invokeMethod(self.player, 'setSource', Qt.ConnectionType.QueuedConnection, Q_ARG(QUrl, QUrl()))
            poll.start(); limit.start(10000)
            loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            poll.stop(); limit.stop(); loop.deleteLater()
            if self.player.sourceDevice() is not None:
                self._releasing = False
                raise RuntimeError('视频后台尚未停止，请稍后再关闭窗口。')
        self.player.setVideoSink(None)
        self.sink.setVideoFrame(QVideoFrame())
        self.buffer.close(); self.buffer.setData(QByteArray())
        self._image = QImage(); self.canvas.clear()
        self._releasing = False

    def done(self, result):
        if not self._closed:
            self._closed = True
            self.load_timer.stop()
            self._release()
        super().done(result)
