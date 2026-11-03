"""Shared visual language for the local desktop workbench.

Set STYLE on a window. Object names describe surfaces and text hierarchy; buttons
use ``setProperty('kind', 'primary' | 'secondary' | 'danger')``. ``icon`` creates
retina-ready painter icons without SVG plugins, font icons or external assets.
"""
import sys
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap


def font_family(platform=None):
    """Use macOS system/CJK fonts without changing other platforms' fallbacks."""
    if (platform or sys.platform) == 'darwin':
        return '"Helvetica Neue", "PingFang SC", "Noto Sans CJK SC"'
    return '"Segoe UI", "Microsoft YaHei", "Noto Sans CJK SC"'


STYLE = '''
QWidget {
    color: #24364f;
    font-family: __APP_FONT_FAMILY__;
    font-size: 14px;
}
QMainWindow, QDialog { background: #f4f7fb; }
QWidget#page { background: #f4f7fb; }
QScrollArea#pageScroll { background: #f4f7fb; border: none; }
QLabel { background: transparent; }
QWidget#sideBar { background: #10213b; border: none; }
QWidget#sideBar QLabel { color: #dce7f7; }
QLabel#brand { color: #ffffff; font-size: 18px; font-weight: 700; }
QLabel#brandSubtitle { color: #8fa6c5; font-size: 11px; }
QLabel#sidebarFooter { color: #8fa6c5; font-size: 11px; padding: 4px 0; }
QLabel#sidebarSection { color: #6f8bad; font-size: 11px; font-weight: 600; padding: 6px 10px; }
QListWidget {
    background: transparent;
    border: none;
    outline: none;
    color: #a7bad3;
    padding: 2px 6px;
}
QListWidget::item { padding: 8px 12px; margin: 3px 0; border-radius: 8px; }
QListWidget::item:hover { background: #192f50; color: #ffffff; }
QListWidget::item:selected { background: #244879; color: #ffffff; font-weight: 600; }
QLabel#pageEyebrow { color: #647d9c; font-size: 11px; font-weight: 600; }
QLabel#title { color: #152b49; font-size: 27px; font-weight: 700; }
QLabel#subtitle { color: #718299; font-size: 12px; }
QLabel#sectionTitle { color: #243b5a; font-size: 14px; font-weight: 600; }
QLabel#sectionNote { color: #7b8ba0; font-size: 12px; }
QLabel#heroTitle { color: #163456; font-size: 28px; font-weight: 700; }
QLabel#heroText { color: #647c99; font-size: 13px; }
QLabel#metricValue { color: #183b65; font-size: 25px; font-weight: 700; }
QLabel#metricLabel { color: #7a8aa1; font-size: 12px; }
QLabel#badge {
    background: #e8f6f0; color: #15816c; border-radius: 11px;
    padding: 4px 10px; font-size: 11px; font-weight: 600;
}
QWidget#card, QFrame#card {
    background: #ffffff; border: 1px solid #e3eaf2; border-radius: 12px;
}
QWidget#heroCard { background: #eaf2ff; border: 1px solid #dce8fb; border-radius: 14px; }
QWidget#previewPanel { background: #ffffff; border: 1px solid #e3eaf2; border-radius: 10px; }
QLabel#previewCanvas {
    background: #f2f6fb; color: #91a0b4; border: 1px dashed #d4dfed;
    border-radius: 9px; padding: 12px; font-size: 12px;
}
QLabel#sourceInfo {
    background: #ecf2f9; color: #617691; border: 1px solid #e1e9f3;
    border-radius: 8px; padding: 9px 12px; font-size: 11px;
}
QPushButton {
    background: #ffffff; color: #49637f; border: 1px solid #dce5f0;
    border-radius: 7px; padding: 8px 13px; font-size: 12px; font-weight: 500;
}
QPushButton:hover { background: #f0f5fc; border-color: #bacde4; color: #235690; }
QPushButton:pressed { background: #e6eef9; }
QPushButton:focus { border-color: #6098e9; }
QPushButton[kind="primary"] { background: #2563eb; color: #ffffff; border-color: #2563eb; font-weight: 600; }
QPushButton[kind="primary"]:hover { background: #1d55d3; border-color: #1d55d3; }
QPushButton[kind="primary"]:pressed { background: #1747b6; }
QPushButton[kind="secondary"] { background: #eef4fd; color: #346cae; border-color: #dce8f8; }
QPushButton[kind="secondary"]:hover { background: #e1edfc; border-color: #bdd3f0; }
QPushButton[kind="danger"] { background: #fff5f5; color: #b46464; border-color: #f0dddd; }
QPushButton[kind="danger"]:hover { background: #fdeaea; border-color: #e9c3c3; }
QPushButton:disabled, QPushButton[kind="primary"]:disabled, QPushButton[kind="secondary"]:disabled,
QPushButton[kind="danger"]:disabled { background: #edf1f6; color: #a7b3c3; border-color: #e3e9f1; }
QComboBox {
    background: #ffffff; border: 1px solid #dce5f0; border-radius: 7px;
    color: #536d8d; padding: 7px 12px; min-height: 16px; font-size: 12px;
}
QComboBox:hover, QComboBox:focus { border-color: #9ebee8; }
QComboBox::drop-down { width: 24px; border: none; }
QComboBox QAbstractItemView { background: #ffffff; border: 1px solid #dce5f0; selection-background-color: #e8f0fe; selection-color: #2256a4; padding: 4px; outline: none; }
QTableWidget {
    background: #ffffff; alternate-background-color: #f8fafd; color: #455e7b;
    border: 1px solid #e2e9f2; border-radius: 9px; gridline-color: #eef2f7;
    selection-background-color: #e7f0ff; selection-color: #24569c; font-size: 12px;
}
QTableWidget::item { padding: 6px 9px; border-bottom: 1px solid #f0f3f8; }
QTableWidget::item:selected { background: #e7f0ff; color: #24569c; }
QTableWidget::item:focus { border: none; }
QHeaderView { background: #f6f9fd; }
QHeaderView::section {
    background: #f6f9fd; color: #7c8da5; border: none;
    border-bottom: 1px solid #e3eaf3; padding: 10px 9px; font-size: 11px; font-weight: 600;
}
QTableCornerButton::section { background: #f6f9fd; border: none; }
QProgressBar {
    background: #e7edf5; border: none; border-radius: 4px;
    min-height: 8px; max-height: 8px; color: transparent;
}
QProgressBar::chunk { background: #3b82f6; border-radius: 4px; }
QSplitter::handle { background: #f4f7fb; width: 12px; }
QStatusBar { background: #ffffff; border-top: 1px solid #e3eaf2; color: #7b8fa9; font-size: 11px; padding: 4px 10px; }
QStatusBar::item { border: none; }
QToolTip { background: #173354; color: #ffffff; border: none; border-radius: 4px; padding: 6px 9px; font-size: 12px; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #cbd7e7; min-height: 28px; border-radius: 3px; }
QScrollBar::handle:vertical:hover { background: #b2c4dc; }
QScrollBar:horizontal { background: transparent; height: 9px; margin: 2px; }
QScrollBar::handle:horizontal { background: #cbd7e7; min-width: 28px; border-radius: 3px; }
QScrollBar::handle:horizontal:hover { background: #b2c4dc; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QLineEdit, QTextEdit, QPlainTextEdit { background: #ffffff; border: 1px solid #dce5f0; border-radius: 7px; padding: 7px 10px; selection-background-color: #dceafe; }
'''.replace('__APP_FONT_FAMILY__', font_family())


def icon(name, size=20, color='#96afcf'):
    """Return a 24-unit line icon. Unknown names use a simple file glyph.

    Navigation names: home, android, iphone, scan, results, history, settings.
    Action names: folder, refresh, play, stop, check, shield, image, export,
    arrow-left, arrow-right. ``logo`` returns the blue rounded brand mark.
    """
    if name == 'logo':
        return brand_icon(size)
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24, size / 24)
    painter.setPen(QPen(QColor(color), 1.7, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    def line(x1, y1, x2, y2): painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    def poly(points):
        path = QPainterPath(QPointF(*points[0]))
        for point in points[1:]: path.lineTo(QPointF(*point))
        painter.drawPath(path)
    if name in ('home', 'overview'):
        poly([(3,11),(12,3),(21,11)]); poly([(5,10),(5,21),(10,21),(10,15),(14,15),(14,21),(19,21),(19,10)])
    elif name in ('android', 'device'):
        painter.drawRoundedRect(QRectF(5,7,14,12),3,3)
        line(8,7,6,3); line(16,7,18,3); line(8,19,8,22); line(16,19,16,22)
        painter.drawPoint(QPointF(9,11)); painter.drawPoint(QPointF(15,11)); line(8,15,16,15)
    elif name in ('iphone', 'phone'):
        painter.drawRoundedRect(QRectF(6,2,12,20),3,3)
        line(10,5,14,5); line(11,19,13,19)
    elif name in ('scan', 'scanner'):
        for points in [[(3,8),(3,3),(8,3)],[(16,3),(21,3),(21,8)],[(21,16),(21,21),(16,21)],[(8,21),(3,21),(3,16)]]: poly(points)
        line(3,12,21,12); line(8,8,8,16); line(12,8,12,16); line(16,8,16,16)
    elif name in ('results', 'grid'):
        for x,y in ((3,3),(14,3),(3,14),(14,14)): painter.drawRoundedRect(QRectF(x,y,7,7),1.4,1.4)
    elif name in ('history', 'clock'):
        painter.drawEllipse(QRectF(4,4,16,16)); poly([(12,7),(12,12),(16,14)])
    elif name == 'settings':
        for y,x in ((5,8),(12,16),(19,10)):
            line(3,y,x-2,y); line(x+2,y,21,y); painter.drawEllipse(QRectF(x-2,y-2,4,4))
    elif name in ('folder', 'backup'):
        poly([(3,8),(3,5),(9,5),(11,8),(21,8),(21,20),(3,20),(3,8)])
        line(3,8,11,8)
    elif name == 'refresh':
        path=QPainterPath(); path.arcMoveTo(QRectF(5,5,14,14),40); path.arcTo(QRectF(5,5,14,14),40,285); painter.drawPath(path)
        poly([(16,3),(20,7),(15,8)])
    elif name == 'play':
        poly([(8,4),(20,12),(8,20),(8,4)])
    elif name == 'stop':
        painter.drawRoundedRect(QRectF(6,6,12,12),2,2)
    elif name == 'check':
        poly([(5,12),(10,17),(20,6)])
    elif name == 'shield':
        path=QPainterPath(QPointF(12,2)); path.lineTo(21,6); path.lineTo(20,14); path.quadTo(18,20,12,23); path.quadTo(6,20,4,14); path.lineTo(3,6); path.closeSubpath(); painter.drawPath(path)
        poly([(8,12),(11,15),(16,9)])
    elif name == 'image':
        painter.drawRoundedRect(QRectF(3,4,18,16),2,2); painter.drawEllipse(QRectF(6,7,3,3)); poly([(4,18),(10,12),(14,16),(17,12),(21,17)])
    elif name in ('export', 'download'):
        poly([(4,15),(4,21),(20,21),(20,15)]); line(12,3,12,16); poly([(7,11),(12,16),(17,11)])
    elif name == 'arrow-left':
        poly([(14,5),(7,12),(14,19)])
    elif name == 'arrow-right':
        poly([(10,5),(17,12),(10,19)])
    else:
        poly([(6,2),(15,2),(20,7),(20,22),(6,22),(6,2)]); poly([(15,2),(15,7),(20,7)]); line(9,12,17,12); line(9,16,15,16)
    painter.end()
    return QIcon(pixmap)


def brand_icon(size=40):
    """Blue local-data brand mark, usable as both window and sidebar icon."""
    pixmap=QPixmap(size*2,size*2); pixmap.setDevicePixelRatio(2); pixmap.fill(Qt.GlobalColor.transparent)
    painter=QPainter(pixmap); painter.setRenderHint(QPainter.RenderHint.Antialiasing); painter.scale(size/32,size/32)
    gradient=QLinearGradient(0,0,32,32); gradient.setColorAt(0,QColor('#3b82f6')); gradient.setColorAt(1,QColor('#1d4ed8'))
    painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(gradient); painter.drawRoundedRect(QRectF(0,0,32,32),9,9)
    path=QPainterPath(QPointF(10,8)); path.lineTo(15,8); path.cubicTo(27,8,27,24,15,24); path.lineTo(10,24); path.closeSubpath()
    painter.setBrush(Qt.BrushStyle.NoBrush); painter.setPen(QPen(QColor('#ffffff'),2.6,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin)); painter.drawPath(path)
    painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor('#64e2c5')); painter.drawEllipse(QRectF(7,13,6,6))
    painter.end(); return QIcon(pixmap)
