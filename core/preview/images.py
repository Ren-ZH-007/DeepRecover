from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImageReader

from core.candidate_io import CandidateAccessError, read_candidate


def preview_image(repository, candidate_id, data_dir, token, max_pixels=16_000_000):
    """Decode a bounded still image off the UI thread; do not alter the source."""
    data = read_candidate(repository, candidate_id, data_dir, token)
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    # Qt's documented process-wide allocation guard also covers decoder work.
    QImageReader.setAllocationLimit(64)
    if bytes(reader.format()).lower() not in (b'jpeg', b'jpg', b'png'):
        raise CandidateAccessError('当前只提供 JPG/PNG 静态图片预览。')
    size = reader.size()
    if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > max_pixels:
        raise CandidateAccessError('图片尺寸无效或超过 1,600 万像素预览上限。')
    if size.width() > 1024 or size.height() > 768:
        reader.setScaledSize(size.scaled(QSize(1024, 768), Qt.AspectRatioMode.KeepAspectRatio))
    token.checkpoint()
    image = reader.read()
    token.checkpoint()
    if image.isNull():
        raise CandidateAccessError('图片无法完整解码；未标记为有效或恢复成功。')
    if image.width() > 1024 or image.height() > 768:
        image = image.scaled(1024,768,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
    token.checkpoint()
    return image
