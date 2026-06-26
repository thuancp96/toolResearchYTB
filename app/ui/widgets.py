"""Small reusable Qt widgets: a float slider+spinbox, a folder picker, a color
button."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSlider, QWidget,
)


class FloatSlider(QWidget):
    """A labelled slider kept in sync with a double spin-box."""

    valueChanged = Signal(float)

    def __init__(self, minimum: float, maximum: float, value: float,
                 step: float = 0.01, decimals: int = 2, label: str = "",
                 label_width: int = 70, parent=None):
        super().__init__(parent)
        self._min = float(minimum)
        self._max = float(maximum)
        self._step = float(step)
        self._steps = max(1, int(round((self._max - self._min) / self._step)))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lbl = QLabel(label)
            lbl.setFixedWidth(label_width)
            lay.addWidget(lbl)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, self._steps)
        self._spin = QDoubleSpinBox()
        self._spin.setRange(self._min, self._max)
        self._spin.setSingleStep(self._step)
        self._spin.setDecimals(decimals)
        self._spin.setFixedWidth(72)
        lay.addWidget(self._slider, 1)
        lay.addWidget(self._spin)

        self._slider.valueChanged.connect(self._on_slider)
        self._spin.valueChanged.connect(self._on_spin)
        self.setValue(value)

    def _to_slider(self, v: float) -> int:
        return int(round((v - self._min) / self._step))

    def _from_slider(self, i: int) -> float:
        return self._min + i * self._step

    def value(self) -> float:
        return self._spin.value()

    def setValue(self, v: float) -> None:
        v = max(self._min, min(self._max, float(v)))
        self._slider.blockSignals(True)
        self._spin.blockSignals(True)
        self._slider.setValue(self._to_slider(v))
        self._spin.setValue(v)
        self._slider.blockSignals(False)
        self._spin.blockSignals(False)

    def _on_slider(self, i: int) -> None:
        v = self._from_slider(i)
        self._spin.blockSignals(True)
        self._spin.setValue(v)
        self._spin.blockSignals(False)
        self.valueChanged.emit(v)

    def _on_spin(self, v: float) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(self._to_slider(v))
        self._slider.blockSignals(False)
        self.valueChanged.emit(v)


class FolderPicker(QWidget):
    """Line edit + Browse button selecting a directory."""

    pathChanged = Signal(str)

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText(placeholder)
        btn = QPushButton("…")
        btn.setFixedWidth(32)
        lay.addWidget(self._edit, 1)
        lay.addWidget(btn)
        btn.clicked.connect(self._browse)
        self._edit.editingFinished.connect(
            lambda: self.pathChanged.emit(self._edit.text()))

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục", self._edit.text())
        if d:
            self._edit.setText(d)
            self.pathChanged.emit(d)

    def path(self) -> str:
        return self._edit.text().strip()

    def setPath(self, p: str) -> None:
        self._edit.setText(p or "")


class ColorButton(QPushButton):
    """A button whose background shows the current colour; click to change."""

    colorChanged = Signal(str)

    def __init__(self, color: str = "#ffffff", parent=None):
        super().__init__(parent)
        self.setFixedWidth(48)
        self._color = color
        self._apply()
        self.clicked.connect(self._pick)

    def _apply(self) -> None:
        self.setStyleSheet(
            f"background-color: {self._color}; border: 1px solid #888;")
        self.setText("")

    def color(self) -> str:
        return self._color

    def setColor(self, c: str) -> None:
        self._color = c
        self._apply()

    def _pick(self) -> None:
        col = QColorDialog.getColor(QColor(self._color), self, "Chọn màu")
        if col.isValid():
            self._color = col.name()
            self._apply()
            self.colorChanged.emit(self._color)
