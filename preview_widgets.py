# SPDX-License-Identifier: GPL-3.0-or-later
"""
Preview Widgets for GDS to KiCad GUI

Shared Qt widgets for rendering symbol and pad layout previews.
Both support mouse wheel zoom, click-drag pan, and slider sync
via the zoomChanged signal.
"""

from typing import Optional, List

from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QBrush, QPolygonF

from theme import COLORS
from kicad_sym_writer import SymbolDefinition, SymbolPin, PinSide, PinType, PIN_SPACING, TB_PIN_SPACING, PIN_LENGTH


class SymbolPreviewWidget(QWidget):
    """Renders a live preview of a KiCad schematic symbol."""

    zoomChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.symbol: Optional[SymbolDefinition] = None
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._dragging = False
        self._drag_start = None
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_symbol(self, symbol: SymbolDefinition):
        self.symbol = symbol
        self.update()

    def set_zoom(self, zoom: float):
        self._zoom = max(0.2, min(5.0, zoom))
        self.zoomChanged.emit(self._zoom)
        self.update()

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.set_zoom(self._zoom * factor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._dragging and self._drag_start is not None:
            delta = event.position() - self._drag_start
            self._pan_x += delta.x()
            self._pan_y += delta.y()
            self._drag_start = event.position()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def paintEvent(self, event):
        if not self.symbol:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS['background']))

        w = self.width()
        h = self.height()
        painter.translate(w / 2 + self._pan_x, h / 2 + self._pan_y)

        sym = self.symbol

        # Auto-fit: scale so the symbol fills the widget at zoom=1.0
        extent_w = sym.body_width + 2 * (PIN_LENGTH + 8)  # pins + text labels
        extent_h = sym.body_height + 2 * (PIN_LENGTH + 2)
        margin = 20
        avail_w = max(w - 2 * margin, 1)
        avail_h = max(h - 2 * margin, 1)
        fit_scale = min(avail_w / extent_w, avail_h / extent_h)
        scale = fit_scale * self._zoom

        painter.scale(scale, -scale)
        half_w = sym.body_width / 2.0
        half_h = sym.body_height / 2.0

        # Body rectangle
        pen = QPen(QColor(COLORS['accent']), 0.15)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor('#3b4252')))
        painter.drawRect(QRectF(-half_w, -half_h, sym.body_width, sym.body_height))

        # Pins
        pin_pen = QPen(QColor(COLORS['text_primary']), 0.1)
        power_pen = QPen(QColor(COLORS['warning']), 0.1)
        text_color = QColor(COLORS['text_primary'])

        for pin in sym.pins:
            x, y = pin.get_coordinates(sym.body_width, sym.body_height)
            is_power = pin.pin_type == PinType.POWER_IN

            painter.setPen(power_pen if is_power else pin_pen)

            if pin.side == PinSide.LEFT:
                painter.drawLine(QRectF(x, y, PIN_LENGTH, 0).topLeft(),
                                 QRectF(x, y, PIN_LENGTH, 0).topRight())
            elif pin.side == PinSide.RIGHT:
                painter.drawLine(QRectF(x - PIN_LENGTH, y, PIN_LENGTH, 0).topLeft(),
                                 QRectF(x - PIN_LENGTH, y, PIN_LENGTH, 0).topRight())
            elif pin.side == PinSide.TOP:
                painter.drawLine(QRectF(x, y - PIN_LENGTH, 0, PIN_LENGTH).topLeft(),
                                 QRectF(x, y - PIN_LENGTH, 0, PIN_LENGTH).bottomLeft())
            elif pin.side == PinSide.BOTTOM:
                painter.drawLine(QRectF(x, y, 0, PIN_LENGTH).topLeft(),
                                 QRectF(x, y, 0, PIN_LENGTH).bottomLeft())

            painter.setBrush(QBrush(QColor(COLORS['success']) if is_power
                                    else QColor(COLORS['text_primary'])))
            painter.drawEllipse(QRectF(x - 0.2, y - 0.2, 0.4, 0.4))

            # Pin name text
            painter.save()
            painter.setPen(QPen(text_color, 0.05))
            font = painter.font()
            font.setPointSizeF(0.8)
            painter.setFont(font)
            painter.scale(1, -1)

            if pin.side == PinSide.LEFT:
                painter.drawText(QRectF(x + PIN_LENGTH + 0.3, -y - 0.5, 8, 1),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 pin.name)
            elif pin.side == PinSide.RIGHT:
                painter.drawText(QRectF(x - PIN_LENGTH - 8.3, -y - 0.5, 8, 1),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                 pin.name)
            elif pin.side == PinSide.TOP:
                painter.save()
                painter.translate(x, -y)
                painter.rotate(-90)
                painter.drawText(QRectF(PIN_LENGTH + 0.3, -0.5, 8, 1),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 pin.name)
                painter.restore()
            elif pin.side == PinSide.BOTTOM:
                painter.save()
                painter.translate(x, -y)
                painter.rotate(-90)
                painter.drawText(QRectF(-PIN_LENGTH - 8.3, -0.5, 8, 1),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                 pin.name)
                painter.restore()

            painter.restore()

        # Symbol name at center
        painter.save()
        painter.setPen(QPen(QColor(COLORS['text_secondary']), 0.05))
        font = painter.font()
        font.setPointSizeF(1.0)
        font.setBold(True)
        painter.setFont(font)
        painter.scale(1, -1)
        painter.drawText(QRectF(-half_w, -half_h, sym.body_width, sym.body_height),
                         Qt.AlignmentFlag.AlignCenter, sym.name)
        painter.restore()

        painter.end()


class LayoutPreviewWidget(QWidget):
    """Renders pad rectangles from pad review GDS data."""

    zoomChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pads: List[dict] = []
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._mirror_x = False
        self._dragging = False
        self._drag_start = None
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_pads(self, pads: List[dict]):
        self.pads = pads
        self.update()

    def set_mirror_x(self, mirror: bool):
        """Toggle X-axis mirroring for flip-chip interposer view."""
        self._mirror_x = mirror
        self.update()

    def set_zoom(self, zoom: float):
        self._zoom = max(0.1, min(10.0, zoom))
        self.zoomChanged.emit(self._zoom)
        self.update()

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.set_zoom(self._zoom * factor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._dragging and self._drag_start is not None:
            delta = event.position() - self._drag_start
            self._pan_x += delta.x()
            self._pan_y += delta.y()
            self._drag_start = event.position()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def paintEvent(self, event):
        if not self.pads:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS['background']))

        # Calculate bounding box
        all_bboxes = [p["bbox"] for p in self.pads]
        min_x = min(b[0] for b in all_bboxes)
        min_y = min(b[1] for b in all_bboxes)
        max_x = max(b[2] for b in all_bboxes)
        max_y = max(b[3] for b in all_bboxes)

        span_x = max_x - min_x or 1
        span_y = max_y - min_y or 1

        # Scale to fit widget with margin
        margin = 40
        avail_w = self.width() - 2 * margin
        avail_h = self.height() - 2 * margin
        scale = min(avail_w / span_x, avail_h / span_y) * self._zoom

        cx = (min_x + max_x) / 2.0
        cy = (min_y + max_y) / 2.0

        painter.translate(self.width() / 2 + self._pan_x, self.height() / 2 + self._pan_y)
        mx = -1 if self._mirror_x else 1
        painter.scale(mx * scale, -scale)  # Y-up, optional X-mirror
        painter.translate(-cx, -cy)

        # Draw pads
        pad_pen = QPen(QColor(COLORS['accent']), 0)
        pad_brush = QBrush(QColor(COLORS['accent']))
        text_color = QColor(COLORS['text_primary'])

        for pad in self.pads:
            left, bottom, right, top = pad["bbox"]
            w = right - left
            h = top - bottom

            painter.setPen(pad_pen)
            painter.setBrush(pad_brush)
            painter.setOpacity(0.5)

            poly_pts = pad.get("polygon_points")
            if poly_pts and pad.get("is_polygon", False):
                qpoly = QPolygonF([QPointF(float(px), float(py))
                                   for px, py in poly_pts])
                painter.drawPolygon(qpoly)
            else:
                painter.drawRect(QRectF(left, bottom, w, h))

            painter.setOpacity(1.0)

            # Draw name -- scale font by name length so long names fit
            name = pad.get("name") or f"#{pad['index']}"
            painter.save()
            painter.setPen(QPen(text_color, 0))
            font = painter.font()
            char_width_factor = max(len(name) * 0.6, 1)
            font_size = min(w / char_width_factor, h * 0.5)
            if font_size > 0:
                font.setPointSizeF(max(font_size, 100))
                painter.setFont(font)
            painter.scale(1, -1)  # flip text
            painter.drawText(
                QRectF(left, -top, w, h),
                Qt.AlignmentFlag.AlignCenter,
                name,
            )
            painter.restore()

        painter.end()
