"""A movable + 8-handle-resizable graphics item used for the video box and the
title/description text boxes in the preview."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPen, QPixmap
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
    cropChanged = Signal(float, float)   # video crop focal point (cx, cy) in 0..1

    def __init__(self, kind: str, rect: QRectF, label: str = "", parent=None):
        super().__init__(parent)
        self.kind = kind
        self.label = label or kind.upper()
        self._rect = QRectF(0, 0, rect.width(), rect.height())
        self.setPos(rect.topLeft())

        self._pixmap: Optional[QPixmap] = None
        self._fit = "fit"
        self._crop_x = 0.5
        self._crop_y = 0.5
        self._text = ""
        self._style = TextStyle()
        self._empty_label = True   # draw the placeholder label when text is empty
        self._auto_fit = False     # shrink font to fit the box (title)
        self._max_lines = 0        # 0 = unlimited; clamp + ellipsis otherwise

        self._mode = None          # None | "move" | "resize" | "pan"
        self._handle = None
        self._press_scene = QPointF()
        self._start_rect = QRectF()
        self._start_crop = (0.5, 0.5)

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

    def set_crop(self, cx: float, cy: float) -> None:
        self._crop_x = min(1.0, max(0.0, cx))
        self._crop_y = min(1.0, max(0.0, cy))
        self.update()

    def set_empty_label(self, on: bool) -> None:
        """When False, an empty text box shows only its frame (no placeholder)."""
        self._empty_label = on
        self.update()

    def set_text_fit(self, auto_fit: bool, max_lines: int = 0) -> None:
        """Enable auto font shrink and a max-line clamp for this text box."""
        self._auto_fit = auto_fit
        self._max_lines = max_lines
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
            self._paint_text_box(painter, r)

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

    # ---- text rendering (mirrors core.text_render so preview == output) ----
    def _wrap_qt(self, fm: QFontMetricsF, text: str, max_w: float) -> list:
        lines: list = []
        for paragraph in text.split("\n"):
            cur = ""
            for w in paragraph.split(" "):
                trial = w if not cur else cur + " " + w
                if fm.horizontalAdvance(trial) <= max_w or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = w
            lines.append(cur)
        return lines

    def _truncate_qt(self, fm: QFontMetricsF, lines: list, max_w: float,
                     max_lines: int) -> list:
        if len(lines) <= max_lines:
            return lines
        kept = lines[:max_lines]
        last, ell = kept[-1], "…"
        while last and fm.horizontalAdvance(last + ell) > max_w:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        kept[-1] = (last.rstrip() + ell) if last else ell
        return kept

    def _fit_qt(self, max_w: float, max_h: float, text: str, base_px: int,
                max_lines: int) -> tuple:
        min_size = max(6, int(base_px * 0.35))
        for size in range(base_px, min_size - 1, -1):
            fm = QFontMetricsF(self._font_px(size))
            lines = self._wrap_qt(fm, text, max_w)
            spacing = fm.height() * 0.18
            total_h = len(lines) * fm.height() + max(0, len(lines) - 1) * spacing
            line_ok = max_lines <= 0 or len(lines) <= max_lines
            if line_ok and total_h <= max_h:
                return size, lines
        fm = QFontMetricsF(self._font_px(min_size))
        lines = self._wrap_qt(fm, text, max_w)
        if max_lines > 0:
            lines = self._truncate_qt(fm, lines, max_w, max_lines)
        return min_size, lines

    @staticmethod
    def _font_px(px: int) -> QFont:
        f = QFont()
        f.setPixelSize(max(6, int(px)))
        return f

    def _paint_text_box(self, painter, r: QRectF) -> None:
        tight = (self._style.bg_enabled
                 and getattr(self._style, "bg_style", "box") == "tight")
        if self._style.bg_enabled and not tight:
            painter.fillRect(r, QColor(self._style.bg_color))
        text = self._text.strip()
        if not text:
            if self._empty_label:
                painter.setPen(QColor(150, 150, 160))
                painter.drawText(r, Qt.AlignCenter, self.label)
            return

        pad = min(r.width(), r.height()) * 0.06
        inner = r.adjusted(pad, pad, -pad, -pad)
        base_px = max(6, int(self._style.size_pt))
        if self._auto_fit:
            px, lines = self._fit_qt(inner.width(), inner.height(), text,
                                     base_px, self._max_lines)
        else:
            px = base_px
            fm = QFontMetricsF(self._font_px(px))
            lines = self._wrap_qt(fm, text, inner.width())
            if self._max_lines > 0:
                lines = self._truncate_qt(fm, lines, inner.width(), self._max_lines)

        font = self._font_px(px)
        fm = QFontMetricsF(font)
        painter.setFont(font)
        line_h = fm.height()
        spacing = line_h * 0.18
        total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing
        y = inner.y() + max(0.0, (inner.height() - total_h) / 2)

        placed = []
        for ln in lines:
            lw = fm.horizontalAdvance(ln)
            if self._style.align == "left":
                x = inner.x()
            elif self._style.align == "right":
                x = inner.x() + inner.width() - lw
            else:
                x = inner.x() + (inner.width() - lw) / 2
            placed.append((ln, x, y, lw))
            y += line_h + spacing

        if tight:
            # Rounded pill hugging each line (mirrors core.text_render).
            pad_x = line_h * 0.35
            pad_y = line_h * 0.10
            radius = line_h * 0.30
            painter.save()
            painter.setClipRect(r)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(self._style.bg_color))
            for ln, x, ly, lw in placed:
                if not ln.strip():
                    continue
                painter.drawRoundedRect(
                    QRectF(x - pad_x, ly - pad_y,
                           lw + 2 * pad_x, line_h + 2 * pad_y),
                    radius, radius)
            painter.restore()

        painter.setPen(QColor(self._style.color))
        for ln, x, ly, lw in placed:
            painter.drawText(QPointF(x, ly + fm.ascent()), ln)

    def _draw_pixmap(self, painter, r: QRectF) -> None:
        pm = self._pixmap
        if self._fit == "free":
            painter.drawPixmap(r, pm, QRectF(pm.rect()))
            return
        if self._fit in ("fill", "crop"):
            # Fill the box (overflowing), then position by the crop focal point.
            scaled = pm.scaled(r.size().toSize(), Qt.KeepAspectRatioByExpanding,
                               Qt.SmoothTransformation)
            x = r.x() + (r.width() - scaled.width()) * self._crop_x
            y = r.y() + (r.height() - scaled.height()) * self._crop_y
            painter.drawPixmap(QPointF(x, y), scaled)
            return
        # fit: letterbox-free, centered inside the box
        scaled = pm.scaled(r.size().toSize(), Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        x = r.x() + (r.width() - scaled.width()) / 2
        y = r.y() + (r.height() - scaled.height()) / 2
        painter.drawPixmap(QPointF(x, y), scaled)

    # ---- mouse ---------------------------------------------------------
    def _can_pan(self) -> bool:
        return (self.kind == "video" and self._fit == "crop"
                and self._pixmap is not None and not self._pixmap.isNull())

    def hoverMoveEvent(self, event):
        h = self._handle_at(event.pos()) if self.isSelected() else None
        if h:
            self.setCursor(_CURSORS[h])
        elif self._can_pan():
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.setCursor(Qt.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        if not self.isSelected():
            if self.scene():
                self.scene().clearSelection()
            self.setSelected(True)
        self._handle = self._handle_at(event.pos())
        if self._handle:
            self._mode = "resize"
        elif self._can_pan():
            self._mode = "pan"
            self.setCursor(Qt.ClosedHandCursor)
        else:
            self._mode = "move"
        self._press_scene = event.scenePos()
        self._start_rect = self.scene_rect()
        self._start_crop = (self._crop_x, self._crop_y)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._mode is None:
            return
        if self._mode == "move":
            self._do_move(event.scenePos())
            self.geometryChanged.emit()
        elif self._mode == "pan":
            self._do_pan(event.scenePos())
            self.cropChanged.emit(self._crop_x, self._crop_y)
        else:
            self._do_resize(event.scenePos())
            self.geometryChanged.emit()

    def mouseReleaseEvent(self, event):
        mode = self._mode
        self._mode = None
        self._handle = None
        if mode == "pan":
            self.setCursor(Qt.OpenHandCursor)
            self.cropChanged.emit(self._crop_x, self._crop_y)
        else:
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

    def _do_pan(self, scene_pos: QPointF) -> None:
        """Drag the video content within its box to choose the visible crop."""
        if self._pixmap is None or self._pixmap.isNull():
            return
        scaled = self._pixmap.size().scaled(
            self._rect.size().toSize(), Qt.KeepAspectRatioByExpanding)
        ovx = max(1.0, float(scaled.width() - self._rect.width()))
        ovy = max(1.0, float(scaled.height() - self._rect.height()))
        delta = scene_pos - self._press_scene
        cx0, cy0 = self._start_crop
        # Dragging right reveals the left part, so the focal point moves left.
        self._crop_x = min(1.0, max(0.0, cx0 - delta.x() / ovx))
        self._crop_y = min(1.0, max(0.0, cy0 - delta.y() / ovy))
        self.update()

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
