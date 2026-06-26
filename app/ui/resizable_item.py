"""A movable + 8-handle-resizable graphics item used for the video box and the
title/description text boxes in the preview."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from ..core.layout_model import TextStyle

_CORNERS = ("tl", "tr", "bl", "br")
_EDGES = ("t", "b", "l", "r")
_HANDLES = _CORNERS + _EDGES

_CURSORS = {
    "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
    "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
    "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
    "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
}


class ResizableItem(QGraphicsObject):
    geometryChanged = Signal()

    def __init__(self, kind: str, rect: QRectF, label: str = "", parent=None):
        super().__init__(parent)
        self.kind = kind
        self.label = label or kind.upper()
        self._rect = QRectF(0, 0, rect.width(), rect.height())
        self.setPos(rect.topLeft())

        self._pixmap: Optional[QPixmap] = None
        self._fit = "fit"
        self._text = ""
        self._style = TextStyle()

        self._mode = None          # None | "move" | "resize"
        self._handle = None
        self._press_scene = QPointF()
        self._start_rect = QRectF()

        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)

    # ---- content setters ----------------------------------------------
    def set_pixmap(self, pm: Optional[QPixmap], fit: str = "fit") -> None:
        self._pixmap = pm
        self._fit = fit
        self.update()

    def set_fit(self, fit: str) -> None:
        self._fit = fit
        self.update()

    def set_text(self, text: str, style: TextStyle) -> None:
        self._text = text or ""
        self._style = style
        self.update()

    # ---- geometry ------------------------------------------------------
    def scene_rect(self) -> QRectF:
        return QRectF(self.pos(), self._rect.size())

    def set_scene_rect(self, r: QRectF) -> None:
        self.prepareGeometryChange()
        self.setPos(r.topLeft())
        self._rect = QRectF(0, 0, max(2.0, r.width()), max(2.0, r.height()))
        self.update()

    def _view_scale(self) -> float:
        sc = self.scene()
        if sc is not None and sc.views():
            m = sc.views()[0].transform().m11()
            if m:
                return abs(m)
        return 1.0

    def _hs(self) -> float:
        return 9.0 / max(1e-4, self._view_scale())

    def boundingRect(self) -> QRectF:
        m = self._hs()
        return self._rect.adjusted(-m, -m, m, m)

    def _handle_centers(self) -> dict:
        w, h = self._rect.width(), self._rect.height()
        return {
            "tl": QPointF(0, 0), "tr": QPointF(w, 0),
            "bl": QPointF(0, h), "br": QPointF(w, h),
            "t": QPointF(w / 2, 0), "b": QPointF(w / 2, h),
            "l": QPointF(0, h / 2), "r": QPointF(w, h / 2),
        }

    def _handle_at(self, pos: QPointF) -> Optional[str]:
        hs = self._hs()
        for name, c in self._handle_centers().items():
            if QRectF(c.x() - hs / 2, c.y() - hs / 2, hs, hs).contains(pos):
                return name
        return None

    # ---- painting ------------------------------------------------------
    def paint(self, painter, option, widget=None):
        r = self._rect
        scale = self._view_scale()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)

        if self.kind == "video":
            if self._pixmap and not self._pixmap.isNull():
                painter.save()
                painter.setClipRect(r)
                self._draw_pixmap(painter, r)
                painter.restore()
            else:
                painter.fillRect(r, QColor(40, 40, 48))
                painter.setPen(QColor(180, 180, 190))
                painter.drawText(r, Qt.AlignCenter, self.label)
        else:  # title / desc text box
            if self._style.bg_enabled:
                painter.fillRect(r, QColor(self._style.bg_color))
            if self._text.strip():
                f = QFont()
                f.setPixelSize(max(6, int(self._style.size_pt)))
                painter.setFont(f)
                painter.setPen(QColor(self._style.color))
                pad = min(r.width(), r.height()) * 0.06
                painter.drawText(r.adjusted(pad, pad, -pad, -pad),
                                 self._text_flags(), self._text)
            else:
                painter.setPen(QColor(150, 150, 160))
                painter.drawText(r, Qt.AlignCenter, self.label)

        # border
        pen = QPen(QColor(0, 200, 255) if self.isSelected() else QColor(120, 200, 255))
        pen.setCosmetic(True)
        pen.setWidth(2 if self.isSelected() else 1)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(r)

        if self.isSelected():
            hs = self._hs()
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.setPen(QPen(QColor(0, 120, 200)))
            for c in self._handle_centers().values():
                painter.drawRect(QRectF(c.x() - hs / 2, c.y() - hs / 2, hs, hs))

    def _text_flags(self):
        h = {"left": Qt.AlignLeft, "right": Qt.AlignRight}.get(
            self._style.align, Qt.AlignHCenter)
        return h | Qt.AlignVCenter | Qt.TextWordWrap

    def _draw_pixmap(self, painter, r: QRectF) -> None:
        pm = self._pixmap
        if self._fit == "free":
            painter.drawPixmap(r, pm, QRectF(pm.rect()))
            return
        mode = (Qt.KeepAspectRatioByExpanding if self._fit == "fill"
                else Qt.KeepAspectRatio)
        scaled = pm.scaled(r.size().toSize(), mode, Qt.SmoothTransformation)
        x = r.x() + (r.width() - scaled.width()) / 2
        y = r.y() + (r.height() - scaled.height()) / 2
        painter.drawPixmap(QPointF(x, y), scaled)

    # ---- mouse ---------------------------------------------------------
    def hoverMoveEvent(self, event):
        h = self._handle_at(event.pos()) if self.isSelected() else None
        self.setCursor(_CURSORS.get(h, Qt.SizeAllCursor))
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        if not self.isSelected():
            if self.scene():
                self.scene().clearSelection()
            self.setSelected(True)
        self._handle = self._handle_at(event.pos())
        self._mode = "resize" if self._handle else "move"
        self._press_scene = event.scenePos()
        self._start_rect = self.scene_rect()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._mode is None:
            return
        if self._mode == "move":
            self._do_move(event.scenePos())
        else:
            self._do_resize(event.scenePos())
        self.geometryChanged.emit()

    def mouseReleaseEvent(self, event):
        self._mode = None
        self._handle = None
        self.geometryChanged.emit()
        event.accept()

    def _scene_bounds(self) -> QRectF:
        return self.scene().sceneRect() if self.scene() else QRectF(0, 0, 1e6, 1e6)

    def _do_move(self, scene_pos: QPointF) -> None:
        delta = scene_pos - self._press_scene
        sr = self._start_rect
        b = self._scene_bounds()
        nx = min(max(sr.x() + delta.x(), b.left()), b.right() - sr.width())
        ny = min(max(sr.y() + delta.y(), b.top()), b.bottom() - sr.height())
        self.setPos(nx, ny)

    def _do_resize(self, scene_pos: QPointF) -> None:
        sr = self._start_rect
        b = self._scene_bounds()
        left, top, right, bottom = sr.left(), sr.top(), sr.right(), sr.bottom()
        h = self._handle
        mx = min(max(scene_pos.x(), b.left()), b.right())
        my = min(max(scene_pos.y(), b.top()), b.bottom())
        min_sz = 24.0

        if h in ("l", "tl", "bl"):
            left = min(mx, right - min_sz)
        if h in ("r", "tr", "br"):
            right = max(mx, left + min_sz)
        if h in ("t", "tl", "tr"):
            top = min(my, bottom - min_sz)
        if h in ("b", "bl", "br"):
            bottom = max(my, top + min_sz)

        self.prepareGeometryChange()
        self.setPos(left, top)
        self._rect = QRectF(0, 0, right - left, bottom - top)
        self.update()
