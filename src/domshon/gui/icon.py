"""Application icon, drawn in code so no binary asset is needed (used for the window, tray,
launcher entries and the built executables)."""
from __future__ import annotations

import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap, QPolygonF

from .. import theme

SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> QImage:
    """Draw the icon at `size` x `size` pixels (transparent background)."""
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    cx = cy = size / 2
    r = size * 0.43
    pts = [QPointF(cx + r * math.cos(math.radians(60 * i - 90)), cy + r * math.sin(math.radians(60 * i - 90)))
           for i in range(6)]
    pen = QPen(QColor(theme.TOKENS["cyan"]))
    pen.setWidthF(max(1.0, size * 0.09))
    p.setPen(pen)
    p.setBrush(QColor(6, 5, 15, 235))
    p.drawPolygon(QPolygonF(pts))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(theme.TOKENS["magenta"]))
    p.drawEllipse(QPointF(cx, cy), size * 0.11, size * 0.11)
    p.end()
    return img


def make_icon() -> QIcon:
    icon = QIcon()
    for s in SIZES:
        icon.addPixmap(QPixmap.fromImage(render(s)))
    return icon


def png_bytes(size: int) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    render(size).save(buf, "PNG")
    buf.close()
    return bytes(ba)


def ico_bytes(sizes=(16, 24, 32, 48, 64, 128, 256)) -> bytes:
    """A multi-size .ico (PNG-compressed entries, supported since Windows Vista)."""
    import struct
    pngs = [(s, png_bytes(s)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for s, data in pngs:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return head + entries + blobs
