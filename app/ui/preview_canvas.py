"""The interactive preview: a QGraphicsView whose scene is sized to the output
canvas (in pixels) so item positions map 1:1 to normalized layout coords."""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView

from ..core.layout_model import Layout, Region
from .resizable_item import ResizableItem


def bgr_to_pixmap(img: np.ndarray) -> QPixmap:
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(qimg)


class PreviewCanvas(QGraphicsView):
    layoutChanged = Signal(object)  # emits the Layout after a drag/resize

    def __init__(self, layout: Layout, parent=None):
        super().__init__(parent)
        self._layout = layout
        self._frame: Optional[np.ndarray] = None

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(self.renderHints())
        self.setBackgroundBrush(QColor(20, 20, 26))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        cw, ch = layout.canvas_size()
        self._bg_item = QGraphicsPixmapItem()
        self._bg_item.setZValue(-10)
        self._scene.addItem(self._bg_item)

        self._items = {
            "video": ResizableItem("video", self._region_rect(layout.video), "VIDEO"),
            "title": ResizableItem("title", self._region_rect(layout.title), "TITLE"),
            "desc": ResizableItem("desc", self._region_rect(layout.desc), "DESCRIPTION"),
            "desc_img": ResizableItem("image", self._region_rect(layout.desc_img), "ẢNH"),
        }
        for name, item in self._items.items():
            item.setZValue(1 if name == "video" else 2)
            self._scene.addItem(item)
            item.geometryChanged.connect(self._on_item_changed)
        self._items["video"].cropChanged.connect(self._on_crop_changed)
        self._items["desc_img"].cropChanged.connect(self._on_img_crop_changed)
        # The description box shows its frame (not a placeholder) when empty.
        self._items["desc"].set_empty_label(False)
        # The title auto-shrinks to fit and is clamped to 2 lines (matches render).
        self._items["title"].set_text_fit(True, 2)

        self.set_layout(layout)

    # ---- helpers -------------------------------------------------------
    def _region_rect(self, region: Region) -> QRectF:
        cw, ch = self._layout.canvas_size()
        return QRectF(region.nx * cw, region.ny * ch, region.nw * cw, region.nh * ch)

    def layout(self) -> Layout:
        return self._layout

    # ---- public API ----------------------------------------------------
    def set_layout(self, layout: Layout) -> None:
        self._layout = layout
        cw, ch = layout.canvas_size()
        self._scene.setSceneRect(0, 0, cw, ch)
        self._items["video"].set_scene_rect(self._region_rect(layout.video))
        self._items["video"].set_fit(layout.video_fit)
        self._items["video"].set_crop(layout.video_crop_x, layout.video_crop_y)
        self._items["title"].set_scene_rect(self._region_rect(layout.title))
        self._items["desc"].set_scene_rect(self._region_rect(layout.desc))
        self._items["desc"].setVisible(layout.show_desc)
        self._items["desc_img"].set_scene_rect(self._region_rect(layout.desc_img))
        self._items["desc_img"].set_crop(layout.desc_img_crop_x,
                                         layout.desc_img_crop_y)
        self._load_desc_image()
        self.refresh_text()
        self._rebuild_bg()
        self._fit()

    def set_aspect(self, aspect: str) -> None:
        self._layout.aspect = aspect
        self.set_layout(self._layout)

    def set_video_frame(self, frame: Optional[np.ndarray]) -> None:
        self._frame = frame
        if frame is not None:
            self._items["video"].set_pixmap(bgr_to_pixmap(frame), self._layout.video_fit)
        else:
            self._items["video"].set_pixmap(None, self._layout.video_fit)
        self._rebuild_bg()

    def refresh_text(self) -> None:
        self._items["title"].set_text(self._layout.title_text, self._layout.title_style)
        self._items["desc"].set_text(self._layout.desc_text, self._layout.desc_style)
        self._items["video"].set_fit(self._layout.video_fit)

    def set_region(self, name: str, region: Region) -> None:
        """Reposition an item from normalized coords (slider -> canvas)."""
        if name in self._items:
            self._items[name].set_scene_rect(self._region_rect(region))

    def select_region(self, name: str) -> None:
        self._scene.clearSelection()
        if name in self._items:
            self._items[name].setSelected(True)

    def set_desc_visible(self, on: bool) -> None:
        self._layout.show_desc = on
        self._items["desc"].setVisible(on)
        self._items["desc_img"].setVisible(on and bool(self._layout.desc_image_path))

    def set_desc_image(self, path: str) -> None:
        """Set (or clear, with an empty path) the description image."""
        self._layout.desc_image_path = path or ""
        self._load_desc_image()

    def set_desc_img_fit(self, fit: str) -> None:
        self._layout.desc_img_fit = fit
        self._items["desc_img"].set_fit(fit)

    def _load_desc_image(self) -> None:
        item = self._items["desc_img"]
        path = self._layout.desc_image_path
        pm = QPixmap(path) if path else QPixmap()
        item.set_pixmap(None if pm.isNull() else pm, self._layout.desc_img_fit)
        item.setVisible(self._layout.show_desc and bool(path) and not pm.isNull())

    # ---- background ----------------------------------------------------
    def refresh_bg(self) -> None:
        self._rebuild_bg()

    def _rebuild_bg(self) -> None:
        cw, ch = self._layout.canvas_size()
        if self._layout.bg_mode == "color" or self._frame is None:
            pm = QPixmap(cw, ch)
            pm.fill(QColor(self._layout.bg_color) if self._layout.bg_mode == "color"
                    else QColor(18, 18, 24))
            self._bg_item.setPixmap(pm)
            return
        frame = self._frame
        fh, fw = frame.shape[:2]
        scale = max(cw / fw, ch / fh)
        nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
        resized = cv2.resize(frame, (nw, nh))
        x0, y0 = (nw - cw) // 2, (nh - ch) // 2
        crop = resized[y0:y0 + ch, x0:x0 + cw]
        k = max(1, int(self._layout.bg_blur)) * 2 + 1
        blurred = cv2.GaussianBlur(crop, (k, k), 0)
        self._bg_item.setPixmap(bgr_to_pixmap(blurred))

    # ---- sync ----------------------------------------------------------
    def _on_crop_changed(self, cx: float, cy: float) -> None:
        self._layout.video_crop_x = cx
        self._layout.video_crop_y = cy
        self.layoutChanged.emit(self._layout)

    def _on_img_crop_changed(self, cx: float, cy: float) -> None:
        self._layout.desc_img_crop_x = cx
        self._layout.desc_img_crop_y = cy
        self.layoutChanged.emit(self._layout)

    def _on_item_changed(self) -> None:
        cw, ch = self._layout.canvas_size()
        for name in ("title", "video", "desc", "desc_img"):
            r = self._items[name].scene_rect()
            region = getattr(self._layout, name)
            region.nx, region.ny = r.x() / cw, r.y() / ch
            region.nw, region.nh = r.width() / cw, r.height() / ch
            region.clamp()
        self.layoutChanged.emit(self._layout)

    # ---- fit -----------------------------------------------------------
    def _fit(self) -> None:
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit()
