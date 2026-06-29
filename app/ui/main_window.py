"""Main application window: folder pickers, aspect toggle, settings panels,
the interactive preview, position sliders, progress + log."""

from __future__ import annotations

import copy
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QScrollArea, QSplitter, QTabWidget,
    QVBoxLayout, QWidget,
)

from ..core import config as cfg
from ..core.batch_processor import BatchWorker
from ..core.layout_model import VIDEO_EXTS, Layout, Region, clean_title
from ..core.transcribe import whisper_available
from ..core.video_probe import extract_frame
from .preview_canvas import PreviewCanvas
from .widgets import ColorButton, FloatSlider, FolderPicker
from .youtube_tab import YouTubeTab

DARK_QSS = """
QWidget { background:#1f2430; color:#dfe5ef; font-size:12px; }
QGroupBox { border:1px solid #36405a; border-radius:6px; margin-top:8px; padding-top:6px; }
QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 4px; color:#7fb6ff; }
QLineEdit, QComboBox, QPlainTextEdit, QDoubleSpinBox { background:#2a3142; border:1px solid #3a435c; border-radius:4px; padding:2px 4px; }
QPushButton { background:#2f6bd6; border:none; border-radius:4px; padding:6px 12px; }
QPushButton:hover { background:#3f7be6; }
QPushButton:disabled { background:#3a435c; color:#8a93a6; }
QProgressBar { background:#2a3142; border:1px solid #3a435c; border-radius:4px; text-align:center; }
QProgressBar::chunk { background:#2f9bd6; border-radius:4px; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Custom Video & YouTube Tool")
        self.resize(1320, 840)
        self.setStyleSheet(DARK_QSS)

        self.layout_data = Layout()
        self.worker: BatchWorker | None = None
        self._current_stem = ""
        self.pos_sliders: dict[str, dict[str, FloatSlider]] = {
            "title": {}, "video": {}, "desc": {}}

        self.canvas = PreviewCanvas(self.layout_data)
        self.canvas.layoutChanged.connect(self._sync_pos_sliders)

        self._build_ui()
        self._auto_load()

    # ===================================================================
    # UI construction
    # ===================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.addLayout(self._build_top_bar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._wrap_preview())
        splitter.addWidget(self._build_settings_scroll())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        root.addWidget(self._build_progress_log())

        self.tabs = QTabWidget()
        self.tabs.addTab(central, "Ghép Video")
        self.youtube_tab = YouTubeTab()
        self.tabs.addTab(self.youtube_tab, "YouTube Finder")
        self.setCentralWidget(self.tabs)

    def _build_top_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self.input_picker = FolderPicker("Thư mục video đầu vào")
        self.output_picker = FolderPicker("Thư mục xuất (mặc định = đầu vào)")
        self.input_picker.pathChanged.connect(self._on_input_changed)

        bar.addWidget(QLabel("Input:"))
        bar.addWidget(self.input_picker, 2)
        bar.addWidget(QLabel("Output:"))
        bar.addWidget(self.output_picker, 2)

        # aspect toggle
        self.rb_916 = QRadioButton("9:16")
        self.rb_169 = QRadioButton("16:9")
        self.rb_916.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_916)
        grp.addButton(self.rb_169)
        self.rb_916.toggled.connect(self._on_aspect_changed)
        bar.addWidget(self.rb_916)
        bar.addWidget(self.rb_169)

        self.btn_start = QPushButton("▶ Bắt đầu")
        self.btn_stop = QPushButton("■ Dừng")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        bar.addWidget(self.btn_start)
        bar.addWidget(self.btn_stop)

        btn_save = QPushButton("Lưu cấu hình")
        btn_load = QPushButton("Tải cấu hình")
        btn_save.clicked.connect(self._save_config)
        btn_load.clicked.connect(self._load_config)
        bar.addWidget(btn_save)
        bar.addWidget(btn_load)
        return bar

    def _wrap_preview(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Preview (kéo & resize các khung)"))
        v.addWidget(self.canvas, 1)
        return w

    def _build_settings_scroll(self) -> QScrollArea:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.addWidget(self._grp_auto())
        v.addWidget(self._grp_video())
        v.addWidget(self._grp_bg())
        v.addWidget(self._grp_text("title", "Tiêu đề (Title)"))
        v.addWidget(self._grp_text("desc", "Mô tả (Description)"))
        v.addWidget(self._grp_audio())
        v.addWidget(self._grp_output())
        v.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        return scroll

    def _src_combo(self) -> QComboBox:
        c = QComboBox()
        c.addItem("Tên file", "filename")
        c.addItem("Giọng nói (Whisper)", "whisper")
        c.addItem("Thủ công", "manual")
        return c

    def _grp_auto(self) -> QGroupBox:
        g = QGroupBox("Tự động mô tả (Whisper)")
        f = QFormLayout(g)
        avail = whisper_available()
        self.whisper_enable = QCheckBox("Bật nhận dạng giọng nói")
        self.whisper_enable.setEnabled(avail)
        if not avail:
            self.whisper_enable.setToolTip("Cài 'faster-whisper' để dùng tính năng này")
        self.model_combo = QComboBox()
        for m in ("tiny", "base", "small", "medium"):
            self.model_combo.addItem(m, m)
        self.model_combo.setCurrentText("base")
        self.lang_edit = QLineEdit()
        self.lang_edit.setPlaceholderText("auto (vd: vi, en, ja)")

        self.title_src = self._src_combo()
        self.title_src.setCurrentIndex(0)   # filename
        self.desc_src = self._src_combo()
        self.desc_src.setCurrentIndex(1)    # whisper
        self.title_src.currentIndexChanged.connect(
            lambda: self._on_source_changed("title"))
        self.desc_src.currentIndexChanged.connect(
            lambda: self._on_source_changed("desc"))

        f.addRow(self.whisper_enable)
        f.addRow("Model:", self.model_combo)
        f.addRow("Ngôn ngữ:", self.lang_edit)
        f.addRow("Nguồn Title:", self.title_src)
        f.addRow("Nguồn Desc:", self.desc_src)
        if not avail:
            note = QLabel("⚠ Chưa cài faster-whisper")
            note.setStyleSheet("color:#e0a030;")
            f.addRow(note)
        return g

    def _grp_video(self) -> QGroupBox:
        g = QGroupBox("Khung Video")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        row.addWidget(QLabel("Chế độ:"))
        self.fit_combo = QComboBox()
        self.fit_combo.addItem("Vừa khung (fit)", "fit")
        self.fit_combo.addItem("Lấp đầy (fill)", "fill")
        self.fit_combo.addItem("Cắt & kéo (crop)", "crop")
        self.fit_combo.addItem("Kéo giãn (free)", "free")
        self.fit_combo.currentIndexChanged.connect(self._on_fit_changed)
        row.addWidget(self.fit_combo, 1)
        v.addLayout(row)
        hint = QLabel("Chế độ 'Cắt & kéo': kéo video trong khung để chọn phần hiển thị.")
        hint.setStyleSheet("color:#8a93a6;")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self._add_pos_sliders(v, "video")
        return g

    def _grp_bg(self) -> QGroupBox:
        g = QGroupBox("Nền")
        f = QFormLayout(g)
        self.bg_mode = QComboBox()
        self.bg_mode.addItem("Làm mờ video", "blur")
        self.bg_mode.addItem("Màu đặc", "color")
        self.bg_mode.currentIndexChanged.connect(self._on_bg_changed)
        self.bg_blur = FloatSlider(0, 50, self.layout_data.bg_blur, 1, 0, "Độ mờ:")
        self.bg_blur.valueChanged.connect(self._on_bg_changed)
        self.bg_color = ColorButton(self.layout_data.bg_color)
        self.bg_color.colorChanged.connect(self._on_bg_changed)
        f.addRow("Kiểu nền:", self.bg_mode)
        f.addRow(self.bg_blur)
        f.addRow("Màu nền:", self.bg_color)
        return g

    def _grp_text(self, name: str, title: str) -> QGroupBox:
        style = getattr(self.layout_data, f"{name}_style")
        g = QGroupBox(title)
        v = QVBoxLayout(g)

        if name == "desc":
            self.show_desc_check = QCheckBox("Hiện khung mô tả")
            self.show_desc_check.setChecked(self.layout_data.show_desc)
            self.show_desc_check.setToolTip(
                "Nếu bật mà không nhập mô tả, khung mô tả vẫn được hiển thị.")
            self.show_desc_check.toggled.connect(self._on_show_desc_changed)
            v.addWidget(self.show_desc_check)

        f = QFormLayout()

        edit = QLineEdit(getattr(self.layout_data, f"{name}_text"))
        edit.textChanged.connect(lambda t, n=name: self._on_text_changed(n, t))
        f.addRow("Văn bản:", edit)

        size = FloatSlider(10, 120, style.size_pt, 1, 0, "Cỡ chữ:")
        size.valueChanged.connect(lambda val, n=name: self._on_style_changed(n))
        f.addRow(size)

        color = ColorButton(style.color)
        bg_color = ColorButton(style.bg_color)
        bg_on = QCheckBox("Nền chữ")
        bg_on.setChecked(style.bg_enabled)
        color.colorChanged.connect(lambda c, n=name: self._on_style_changed(n))
        bg_color.colorChanged.connect(lambda c, n=name: self._on_style_changed(n))
        bg_on.toggled.connect(lambda c, n=name: self._on_style_changed(n))
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Màu chữ:"))
        crow.addWidget(color)
        crow.addWidget(bg_on)
        crow.addWidget(bg_color)
        crow.addStretch(1)

        align = QComboBox()
        align.addItem("Trái", "left")
        align.addItem("Giữa", "center")
        align.addItem("Phải", "right")
        align.setCurrentIndex(1)
        align.currentIndexChanged.connect(lambda i, n=name: self._on_style_changed(n))

        font_btn = QPushButton("Chọn font…")
        font_lbl = QLabel("(mặc định)")
        font_btn.clicked.connect(lambda _, n=name: self._pick_font(n))
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Canh lề:"))
        frow.addWidget(align)
        frow.addWidget(font_btn)
        frow.addWidget(font_lbl, 1)

        v.addLayout(f)
        v.addLayout(crow)
        v.addLayout(frow)
        self._add_pos_sliders(v, name)

        # keep references
        setattr(self, f"{name}_edit", edit)
        setattr(self, f"{name}_size", size)
        setattr(self, f"{name}_color", color)
        setattr(self, f"{name}_bgcolor", bg_color)
        setattr(self, f"{name}_bgon", bg_on)
        setattr(self, f"{name}_align", align)
        setattr(self, f"{name}_fontlbl", font_lbl)
        return g

    def _grp_audio(self) -> QGroupBox:
        g = QGroupBox("Audio")
        f = QFormLayout(g)
        self.audio_speed = FloatSlider(0.25, 3.0, 1.0, 0.05, 2, "Tốc độ:")
        self.audio_volume = FloatSlider(0.0, 4.0, 1.0, 0.05, 2, "Âm lượng:")
        self.audio_speed.valueChanged.connect(self._on_audio_changed)
        self.audio_volume.valueChanged.connect(self._on_audio_changed)
        f.addRow(self.audio_speed)
        f.addRow(self.audio_volume)
        return g

    def _grp_output(self) -> QGroupBox:
        g = QGroupBox("Xuất video")
        f = QFormLayout(g)
        self.codec_combo = QComboBox()
        self.codec_combo.addItem("H.264 (libx264)", "libx264")
        self.codec_combo.addItem("H.265 (libx265)", "libx265")
        self.preset_combo = QComboBox()
        for p in ("ultrafast", "veryfast", "fast", "medium", "slow"):
            self.preset_combo.addItem(p, p)
        self.preset_combo.setCurrentText("veryfast")
        self.crf_check = QCheckBox("Dùng CRF (chất lượng cố định)")
        self.crf_check.setChecked(True)
        self.crf_slider = FloatSlider(14, 32, 20, 1, 0, "CRF:")
        self.bitrate_slider = FloatSlider(1, 30, 10, 1, 0, "Bitrate(M):")
        self.suffix_edit = QLineEdit("_out")
        f.addRow("Codec:", self.codec_combo)
        f.addRow("Preset:", self.preset_combo)
        f.addRow(self.crf_check)
        f.addRow(self.crf_slider)
        f.addRow(self.bitrate_slider)
        f.addRow("Hậu tố tên:", self.suffix_edit)
        return g

    def _add_pos_sliders(self, parent_layout, name: str) -> None:
        region = getattr(self.layout_data, name)
        f = QFormLayout()
        specs = [("x", "X", region.nx, 0.0), ("y", "Y", region.ny, 0.0),
                 ("w", "W", region.nw, 0.02), ("h", "H", region.nh, 0.02)]
        for key, lbl, val, lo in specs:
            s = FloatSlider(lo, 1.0, val, 0.005, 3, lbl, label_width=20)
            s.valueChanged.connect(lambda _v, n=name: self._on_pos_changed(n))
            self.pos_sliders[name][key] = s
            f.addRow(s)
        parent_layout.addLayout(f)

    def _build_progress_log(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.status_lbl = QLabel("Sẵn sàng.")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(150)
        v.addWidget(self.status_lbl)
        v.addWidget(self.progress)
        v.addWidget(self.log_view)
        return w

    # ===================================================================
    # Event handlers
    # ===================================================================
    def _on_input_changed(self, path: str) -> None:
        if path and not self.output_picker.path():
            self.output_picker.setPath(path)
        self._refresh_preview_video(path)

    def _refresh_preview_video(self, folder: str) -> None:
        if not folder or not os.path.isdir(folder):
            return
        vids = [p for p in sorted(Path(folder).iterdir())
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        if not vids:
            self.status_lbl.setText("Không tìm thấy video trong thư mục.")
            return
        first = vids[0]
        self._current_stem = first.stem
        frame = extract_frame(str(first))
        self.canvas.set_video_frame(frame)
        # show filename in any field whose source is 'filename'
        for name, combo in (("title", self.title_src), ("desc", self.desc_src)):
            if combo.currentData() == "filename":
                self._set_text_field(name, clean_title(first.stem))
        self.status_lbl.setText(f"Đã nạp {len(vids)} video. Xem trước: {first.name}")

    def _set_text_field(self, name: str, text: str) -> None:
        edit: QLineEdit = getattr(self, f"{name}_edit")
        edit.blockSignals(True)
        edit.setText(text)
        edit.blockSignals(False)
        setattr(self.layout_data, f"{name}_text", text)
        self.canvas.refresh_text()

    def _on_aspect_changed(self) -> None:
        aspect = "9:16" if self.rb_916.isChecked() else "16:9"
        self.canvas.set_aspect(aspect)
        self._sync_pos_sliders(self.layout_data)

    def _on_fit_changed(self) -> None:
        self.layout_data.video_fit = self.fit_combo.currentData()
        self.canvas.refresh_text()

    def _on_show_desc_changed(self, on: bool) -> None:
        self.layout_data.show_desc = on
        self.canvas.set_desc_visible(on)

    def _on_bg_changed(self, *_) -> None:
        self.layout_data.bg_mode = self.bg_mode.currentData()
        self.layout_data.bg_blur = int(self.bg_blur.value())
        self.layout_data.bg_color = self.bg_color.color()
        self.canvas.refresh_bg()

    def _on_text_changed(self, name: str, text: str) -> None:
        setattr(self.layout_data, f"{name}_text", text)
        # editing manually implies manual source
        combo: QComboBox = getattr(self, f"{name}_src")
        if combo.currentData() != "manual":
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData("manual"))
            combo.blockSignals(False)
            setattr(self.layout_data, f"{name}_source", "manual")
        self.canvas.refresh_text()

    def _on_style_changed(self, name: str) -> None:
        style = getattr(self.layout_data, f"{name}_style")
        style.size_pt = int(getattr(self, f"{name}_size").value())
        style.color = getattr(self, f"{name}_color").color()
        style.bg_color = getattr(self, f"{name}_bgcolor").color()
        style.bg_enabled = getattr(self, f"{name}_bgon").isChecked()
        style.align = getattr(self, f"{name}_align").currentData()
        self.canvas.refresh_text()

    def _on_source_changed(self, name: str) -> None:
        combo: QComboBox = getattr(self, f"{name}_src")
        src = combo.currentData()
        setattr(self.layout_data, f"{name}_source", src)
        if src == "filename" and self._current_stem:
            self._set_text_field(name, clean_title(self._current_stem))

    def _on_audio_changed(self, *_) -> None:
        self.layout_data.audio_speed = self.audio_speed.value()
        self.layout_data.audio_volume = self.audio_volume.value()

    def _pick_font(self, name: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn font", "C:/Windows/Fonts", "Fonts (*.ttf *.otf *.ttc)")
        if path:
            getattr(self.layout_data, f"{name}_style").font_path = path
            getattr(self, f"{name}_fontlbl").setText(Path(path).name)
            self.canvas.refresh_text()

    def _on_pos_changed(self, name: str) -> None:
        region: Region = getattr(self.layout_data, name)
        s = self.pos_sliders[name]
        region.nx, region.ny = s["x"].value(), s["y"].value()
        region.nw, region.nh = s["w"].value(), s["h"].value()
        region.clamp()
        self.canvas.set_region(name, region)

    def _sync_pos_sliders(self, layout: Layout) -> None:
        for name in ("title", "video", "desc"):
            region: Region = getattr(layout, name)
            s = self.pos_sliders[name]
            for key, val in (("x", region.nx), ("y", region.ny),
                             ("w", region.nw), ("h", region.nh)):
                s[key].blockSignals(True)
                s[key].setValue(val)
                s[key].blockSignals(False)

    # ===================================================================
    # Processing
    # ===================================================================
    def _start(self) -> None:
        inp = self.input_picker.path()
        if not inp or not os.path.isdir(inp):
            QMessageBox.warning(self, "Thiếu input", "Chọn thư mục video đầu vào.")
            return
        files = [str(p) for p in sorted(Path(inp).iterdir())
                 if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        if not files:
            QMessageBox.warning(self, "Không có video",
                                "Thư mục không chứa video hợp lệ.")
            return
        out = self.output_picker.path() or inp

        options = {
            "whisper_enabled": self.whisper_enable.isChecked(),
            "model_size": self.model_combo.currentData(),
            "language": self.lang_edit.text().strip(),
            "suffix": self.suffix_edit.text().strip() or "_out",
            "out_opts": {
                "codec": self.codec_combo.currentData(),
                "preset": self.preset_combo.currentData(),
                "use_crf": self.crf_check.isChecked(),
                "crf": int(self.crf_slider.value()),
                "bitrate": int(self.bitrate_slider.value()),
            },
        }

        self.worker = BatchWorker(files, out, copy.deepcopy(self.layout_data), options)
        self.worker.file_started.connect(self._on_file_started)
        self.worker.file_progress.connect(self._on_file_progress)
        self.worker.log.connect(self.log_view.appendPlainText)
        self.worker.finished_all.connect(self._on_finished_all)

        self.log_view.clear()
        self.progress.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.worker.start()

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.status_lbl.setText("Đang dừng…")

    def _on_file_started(self, idx: int, total: int, name: str) -> None:
        self.status_lbl.setText(f"Đang xử lý {idx}/{total}: {name}")

    def _on_file_progress(self, idx: int, total: int, pct: float) -> None:
        overall = ((idx - 1) + pct) / max(1, total)
        self.progress.setValue(int(overall * 100))

    def _on_finished_all(self, success: int, total: int) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100 if success == total else self.progress.value())
        self.status_lbl.setText(f"Hoàn tất: {success}/{total} video.")
        QMessageBox.information(self, "Xong", f"Đã xử lý {success}/{total} video.")

    # ===================================================================
    # Config
    # ===================================================================
    def _collect(self) -> dict:
        return {
            "layout": self.layout_data.to_dict(),
            "input": self.input_picker.path(),
            "output": self.output_picker.path(),
            "whisper": {
                "enabled": self.whisper_enable.isChecked(),
                "model": self.model_combo.currentData(),
                "language": self.lang_edit.text(),
            },
            "out_opts": {
                "codec": self.codec_combo.currentData(),
                "preset": self.preset_combo.currentData(),
                "use_crf": self.crf_check.isChecked(),
                "crf": int(self.crf_slider.value()),
                "bitrate": int(self.bitrate_slider.value()),
                "suffix": self.suffix_edit.text(),
            },
            "youtube": self.youtube_tab.collect_config(),
        }

    def _apply(self, d: dict) -> None:
        if not d:
            return
        self.layout_data = Layout.from_dict(d.get("layout", {}))
        self.input_picker.setPath(d.get("input", ""))
        self.output_picker.setPath(d.get("output", ""))

        w = d.get("whisper", {})
        if whisper_available():
            self.whisper_enable.setChecked(bool(w.get("enabled", False)))
        self.model_combo.setCurrentText(w.get("model", "base"))
        self.lang_edit.setText(w.get("language", ""))

        o = d.get("out_opts", {})
        self.codec_combo.setCurrentIndex(
            max(0, self.codec_combo.findData(o.get("codec", "libx264"))))
        self.preset_combo.setCurrentText(o.get("preset", "veryfast"))
        self.crf_check.setChecked(o.get("use_crf", True))
        self.crf_slider.setValue(o.get("crf", 20))
        self.bitrate_slider.setValue(o.get("bitrate", 10))
        self.suffix_edit.setText(o.get("suffix", "_out"))

        self.youtube_tab.apply_config(d.get("youtube", {}))

        # reflect layout into all widgets + canvas
        self._reflect_layout()

    def _reflect_layout(self) -> None:
        lay = self.layout_data
        (self.rb_916 if lay.aspect == "9:16" else self.rb_169).setChecked(True)
        self.fit_combo.setCurrentIndex(max(0, self.fit_combo.findData(lay.video_fit)))
        self.show_desc_check.setChecked(lay.show_desc)
        self.bg_mode.setCurrentIndex(max(0, self.bg_mode.findData(lay.bg_mode)))
        self.bg_blur.setValue(lay.bg_blur)
        self.bg_color.setColor(lay.bg_color)
        self.audio_speed.setValue(lay.audio_speed)
        self.audio_volume.setValue(lay.audio_volume)
        for name in ("title", "desc"):
            style = getattr(lay, f"{name}_style")
            getattr(self, f"{name}_edit").setText(getattr(lay, f"{name}_text"))
            getattr(self, f"{name}_size").setValue(style.size_pt)
            getattr(self, f"{name}_color").setColor(style.color)
            getattr(self, f"{name}_bgcolor").setColor(style.bg_color)
            getattr(self, f"{name}_bgon").setChecked(style.bg_enabled)
            getattr(self, f"{name}_align").setCurrentIndex(
                max(0, getattr(self, f"{name}_align").findData(style.align)))
            getattr(self, f"{name}_src").setCurrentIndex(
                max(0, getattr(self, f"{name}_src").findData(
                    getattr(lay, f"{name}_source"))))
        self.canvas.set_layout(lay)
        self._sync_pos_sliders(lay)

    def _save_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Lưu cấu hình", str(cfg.DEFAULT_CONFIG_PATH), "JSON (*.json)")
        if path:
            cfg.save_config(path, self._collect())
            self.status_lbl.setText(f"Đã lưu cấu hình: {path}")

    def _load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Tải cấu hình", str(cfg.PROJECT_ROOT), "JSON (*.json)")
        if path:
            self._apply(cfg.load_config(path))
            self.status_lbl.setText(f"Đã tải cấu hình: {path}")
            if self.input_picker.path():
                self._refresh_preview_video(self.input_picker.path())

    def _auto_load(self) -> None:
        if cfg.DEFAULT_CONFIG_PATH.exists():
            self._apply(cfg.load_config(cfg.DEFAULT_CONFIG_PATH))
            if self.input_picker.path():
                self._refresh_preview_video(self.input_picker.path())

    def closeEvent(self, event):
        try:
            cfg.save_config(cfg.DEFAULT_CONFIG_PATH, self._collect())
        except Exception:
            pass
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        self.youtube_tab.stop_worker()
        super().closeEvent(event)
