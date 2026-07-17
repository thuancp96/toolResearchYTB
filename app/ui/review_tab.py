"""Tab "Review Phim": cut a movie into highlight clips, join them and add a
humorous AI narration with voice-synced subtitles.

Gemini keys are borrowed live from the image-gen tab via ``keys_provider``
(injected by MainWindow) so users enter them in one place only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from ..core import tts
from ..core.layout_model import VIDEO_EXTS
from ..core.review import LANGUAGES, ReviewOptions, ReviewWorker
from .widgets import FolderPicker

_VIDEO_FILTER = ("Video (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
                 + ");;Tất cả (*)")


class PlayPauseButton(QPushButton):
    """▶/⏸ toggle around a lazily created QMediaPlayer + QAudioOutput."""

    def __init__(self, parent=None):
        super().__init__("▶ Phát", parent)
        self.setEnabled(False)
        self._player = None
        self._audio_out = None
        self._path = ""
        self.clicked.connect(self._toggle)

    def _ensure_player(self) -> bool:
        if self._player is not None:
            return True
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            self._audio_out = QAudioOutput()
            self._audio_out.setVolume(1.0)
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._audio_out)
            self._player.playbackStateChanged.connect(self._on_state)
            return True
        except Exception:
            self._player = None
            return False

    def set_media(self, path: str) -> None:
        """Point at a new file ("" just releases the current one — needed on
        Windows before the temp mp3 can be rewritten)."""
        self._path = path
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
            if path:
                self._player.setSource(QUrl.fromLocalFile(path))
        self.setEnabled(bool(path))
        self.setText("▶ Phát")

    def _toggle(self) -> None:
        if not self._path:
            return
        if not self._ensure_player():
            try:
                os.startfile(self._path)  # default mp3 app
            except OSError as e:
                QMessageBox.warning(self, "Không phát được audio", str(e))
            return
        from PySide6.QtMultimedia import QMediaPlayer
        if self._player.source().isEmpty():
            self._player.setSource(QUrl.fromLocalFile(self._path))
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_state(self, state) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        self.setText("⏸ Tạm dừng" if state == QMediaPlayer.PlayingState
                     else "▶ Phát")


class ReviewTab(QWidget):
    def __init__(self, keys_provider: Optional[Callable[[], list]] = None,
                 parent=None):
        super().__init__(parent)
        self.keys_provider = keys_provider
        self.worker: ReviewWorker | None = None
        self.voice_test_worker: tts.VoiceTestWorker | None = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- source / output ---
        g_src = QGroupBox("Nguồn")
        src_lay = QVBoxLayout(g_src)
        row = QHBoxLayout()
        row.addWidget(QLabel("Video gốc:"))
        self.src_edit = QLineEdit()
        self.src_edit.setPlaceholderText("Chọn file phim…")
        row.addWidget(self.src_edit, 1)
        btn_browse = QPushButton("…")
        btn_browse.setFixedWidth(32)
        btn_browse.clicked.connect(self._browse_src)
        row.addWidget(btn_browse)
        src_lay.addLayout(row)
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Nơi lưu:"))
        self.out_dir = FolderPicker("Thư mục lưu video review")
        out_row.addWidget(self.out_dir, 1)
        src_lay.addLayout(out_row)
        root.addWidget(g_src)

        # --- cutting ---
        g_cut = QGroupBox("Cắt video")
        cut_lay = QVBoxLayout(g_cut)
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Giữ mỗi đoạn:"))
        self.keep_spin = QDoubleSpinBox()
        self.keep_spin.setRange(0.5, 60.0)
        self.keep_spin.setSingleStep(0.5)
        self.keep_spin.setValue(3.0)
        self.keep_spin.setSuffix(" s")
        crow.addWidget(self.keep_spin)
        crow.addWidget(QLabel("Bỏ qua:"))
        self.skip_spin = QDoubleSpinBox()
        self.skip_spin.setRange(0.0, 600.0)
        self.skip_spin.setSingleStep(1.0)
        self.skip_spin.setValue(10.0)
        self.skip_spin.setSuffix(" s")
        crow.addWidget(self.skip_spin)
        crow.addWidget(QLabel("Chế độ:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Cắt đều (giữ/bỏ)", "even")
        self.mode_combo.addItem("Cắt theo AI (chọn cảnh hay)", "ai")
        crow.addWidget(self.mode_combo, 1)
        cut_lay.addLayout(crow)
        hint = QLabel("Chế độ AI dùng key nhập ở mục “AI viết lời bình” "
                      "bên dưới; không có key sẽ tự chuyển về cắt đều.")
        hint.setStyleSheet("color:#8a93a6;")
        cut_lay.addWidget(hint)
        root.addWidget(g_cut)

        # --- AI provider / key ---
        g_ai = QGroupBox("AI viết lời bình && chọn cảnh")
        ai_lay = QVBoxLayout(g_ai)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Nhà cung cấp:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("Gemini", "gemini")
        self.provider_combo.addItem("OpenAI", "openai")
        prow.addWidget(self.provider_combo)
        prow.addWidget(QLabel("API key:"))
        self.ai_key_edit = QLineEdit()
        self.ai_key_edit.setPlaceholderText(
            "AIza… (Gemini) hoặc sk-… (OpenAI); nhiều key cách nhau dấu ;")
        prow.addWidget(self.ai_key_edit, 1)
        ai_lay.addLayout(prow)
        ai_hint = QLabel("Bỏ trống với Gemini sẽ tự dùng key AIza… "
                         "đã nhập ở tab “Tạo ảnh AI”.")
        ai_hint.setStyleSheet("color:#8a93a6;")
        ai_lay.addWidget(ai_hint)
        root.addWidget(g_ai)

        # --- voice ---
        g_voice = QGroupBox("Giọng đọc thuyết minh")
        v_lay = QVBoxLayout(g_voice)
        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("Ngôn ngữ thuyết minh:"))
        self.lang_combo = QComboBox()
        for label, code in LANGUAGES:
            self.lang_combo.addItem(label, code)
        self.lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        lrow.addWidget(self.lang_combo, 1)
        v_lay.addLayout(lrow)
        vrow = QHBoxLayout()
        vrow.addWidget(QLabel("Giọng đọc:"))
        self.voice_combo = QComboBox()
        self._fill_voices("vi")
        self.voice_combo.currentIndexChanged.connect(self._on_voice_changed)
        vrow.addWidget(self.voice_combo, 1)
        self.btn_voice_test = QPushButton("🔊 Nghe thử")
        self.btn_voice_test.clicked.connect(self._test_voice)
        vrow.addWidget(self.btn_voice_test)
        self.play_btn = PlayPauseButton()
        vrow.addWidget(self.play_btn)
        v_lay.addLayout(vrow)
        if not tts.tts_available():
            note = QLabel("⚠ Chưa cài edge-tts — chạy: pip install edge-tts")
            note.setStyleSheet("color:#e0b060;")
            v_lay.addWidget(note)
            self.btn_voice_test.setEnabled(False)
        root.addWidget(g_voice)

        # --- subtitles ---
        g_sub = QGroupBox("Phụ đề")
        sub_row = QHBoxLayout(g_sub)
        self.sub_check = QCheckBox("Hiện phụ đề")
        self.sub_check.setChecked(True)
        sub_row.addWidget(self.sub_check)
        sub_row.addWidget(QLabel("Hiệu ứng chữ:"))
        self.sub_effect_combo = QComboBox()
        self.sub_effect_combo.addItem("Không", "none")
        self.sub_effect_combo.addItem("Fade in/out", "fade")
        sub_row.addWidget(self.sub_effect_combo)
        sub_row.addWidget(QLabel("Cỡ chữ:"))
        self.sub_size = QSpinBox()
        self.sub_size.setRange(16, 96)
        self.sub_size.setValue(40)
        sub_row.addWidget(self.sub_size)
        sub_row.addWidget(QLabel("Vị trí:"))
        self.sub_pos_combo = QComboBox()
        for label, value in [("Dưới", "bottom"), ("Giữa", "middle"),
                             ("Trên", "top")]:
            self.sub_pos_combo.addItem(label, value)
        sub_row.addWidget(self.sub_pos_combo)
        sub_row.addStretch(1)
        root.addWidget(g_sub)

        # --- original audio ---
        g_audio = QGroupBox("Âm thanh gốc của phim")
        arow = QHBoxLayout(g_audio)
        arow.addWidget(QLabel("Xử lý:"))
        self.audio_mode_combo = QComboBox()
        self.audio_mode_combo.addItem("Bỏ hẳn (chỉ còn voice)", "mute")
        self.audio_mode_combo.addItem("Giữ nhưng giảm nhỏ", "duck")
        self.audio_mode_combo.addItem("Giữ nguyên (chỉ thêm voice)", "keep")
        self.audio_mode_combo.setCurrentIndex(1)
        self.audio_mode_combo.currentIndexChanged.connect(
            self._on_audio_mode_changed)
        arow.addWidget(self.audio_mode_combo, 1)
        arow.addWidget(QLabel("Mức giảm:"))
        self.duck_spin = QDoubleSpinBox()
        self.duck_spin.setRange(-40.0, 0.0)
        self.duck_spin.setSingleStep(1.0)
        self.duck_spin.setValue(-15.0)
        self.duck_spin.setSuffix(" dB")
        arow.addWidget(self.duck_spin)
        arow.addStretch(1)
        root.addWidget(g_audio)

        # --- run ---
        run_row = QHBoxLayout()
        self.btn_start = QPushButton("🎬 Tạo video")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("■ Dừng")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        run_row.addWidget(self.btn_start)
        run_row.addWidget(self.btn_stop)
        run_row.addWidget(self.progress, 1)
        root.addLayout(run_row)
        if not tts.tts_available():
            self.btn_start.setEnabled(False)

        self.status = QLabel("")
        root.addWidget(self.status)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(140)
        root.addWidget(self.log_view)
        root.addStretch(1)

    # ------------------------------------------------------------------
    # source picking
    # ------------------------------------------------------------------
    def _browse_src(self) -> None:
        start = str(Path(self.src_edit.text()).parent) \
            if self.src_edit.text() else ""
        path, _ = QFileDialog.getOpenFileName(self, "Chọn video gốc", start,
                                              _VIDEO_FILTER)
        if path:
            self.src_edit.setText(path)
            if not self.out_dir.path():
                self.out_dir.setPath(str(Path(path).parent))

    # ------------------------------------------------------------------
    # voice preview / language
    # ------------------------------------------------------------------
    def _fill_voices(self, lang: str) -> None:
        """Voices of the chosen language plus the multilingual ones."""
        voices = [(lb, v) for lb, v in tts.VOICES
                  if v.split("-", 1)[0].lower() == lang
                  or "Multilingual" in v]
        if not voices:
            voices = list(tts.VOICES)
        current = self.voice_combo.currentData()
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        for label, voice_id in voices:
            self.voice_combo.addItem(label, voice_id)
        idx = self.voice_combo.findData(current)
        if idx >= 0:
            self.voice_combo.setCurrentIndex(idx)
        self.voice_combo.blockSignals(False)

    def _on_lang_changed(self) -> None:
        self._fill_voices(self.lang_combo.currentData())
        self.play_btn.set_media("")

    def _on_audio_mode_changed(self) -> None:
        self.duck_spin.setEnabled(
            self.audio_mode_combo.currentData() == "duck")

    def _on_voice_changed(self) -> None:
        self.play_btn.set_media("")

    def _test_voice(self) -> None:
        if self.voice_test_worker and self.voice_test_worker.isRunning():
            return
        # Release the temp mp3 before edge-tts rewrites it (Windows lock).
        self.play_btn.set_media("")
        self.btn_voice_test.setEnabled(False)
        self.status.setText("Đang tạo câu nghe thử…")
        self.voice_test_worker = tts.VoiceTestWorker(
            self.voice_combo.currentData())
        self.voice_test_worker.done.connect(self._on_voice_test_done)
        self.voice_test_worker.failed.connect(self._on_voice_test_failed)
        self.voice_test_worker.start()

    def _on_voice_test_done(self, path: str) -> None:
        self.btn_voice_test.setEnabled(True)
        self.status.setText("Bấm ▶ để nghe thử.")
        self.play_btn.set_media(path)
        self.play_btn._toggle()  # start playing right away

    def _on_voice_test_failed(self, msg: str) -> None:
        self.btn_voice_test.setEnabled(True)
        self.status.setText("")
        QMessageBox.warning(self, "Không nghe thử được", msg)

    # ------------------------------------------------------------------
    # run lifecycle
    # ------------------------------------------------------------------
    def _ai_keys(self) -> list:
        """Keys typed in this tab win; with Gemini an empty box falls back
        to the AIza… keys from the image tab (Cloudflare tokens filtered)."""
        own = [k.strip() for k in
               re.split(r"[;,\s]+", self.ai_key_edit.text()) if k.strip()]
        if own:
            return own
        if self.provider_combo.currentData() == "gemini":
            keys = [k for k in (self.keys_provider() if self.keys_provider
                                else []) if k]
            return [k for k in keys if k.startswith("AIza")]
        return []

    def _start(self) -> None:
        src = self.src_edit.text().strip()
        if not src or not Path(src).is_file():
            QMessageBox.warning(self, "Thiếu video gốc",
                                "Chọn file video phim trước.")
            return
        out = self.out_dir.path()
        if not out:
            QMessageBox.warning(self, "Thiếu nơi lưu",
                                "Chọn thư mục lưu video review.")
            return

        opts = ReviewOptions(
            src=src, out_dir=out,
            keep=self.keep_spin.value(), skip=self.skip_spin.value(),
            mode=self.mode_combo.currentData(),
            voice=self.voice_combo.currentData(),
            language=self.lang_combo.currentData(),
            subtitles=self.sub_check.isChecked(),
            sub_effect=self.sub_effect_combo.currentData(),
            sub_size=self.sub_size.value(),
            sub_pos=self.sub_pos_combo.currentData(),
            provider=self.provider_combo.currentData(),
            api_keys=self._ai_keys(),
            audio_mode=self.audio_mode_combo.currentData(),
            duck_db=self.duck_spin.value(),
        )
        self.worker = ReviewWorker(opts)
        self.worker.status.connect(self.status.setText)
        self.worker.log.connect(self.log_view.appendPlainText)
        self.worker.progress.connect(
            lambda p: self.progress.setValue(int(p * 100)))
        self.worker.finished_job.connect(self._on_done)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.log_view.clear()
        self.status.setText("Bắt đầu…")
        self.worker.start()

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
        self.status.setText("Đang dừng…")

    def _on_done(self, ok: bool, detail: str) -> None:
        self.btn_start.setEnabled(tts.tts_available())
        self.btn_stop.setEnabled(False)
        if ok:
            self.status.setText(f"✓ Xong: {detail}")
            QMessageBox.information(self, "Hoàn tất", f"Đã xuất:\n{detail}")
        else:
            self.status.setText(f"✗ {detail}")

    def stop_worker(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)
        if self.voice_test_worker and self.voice_test_worker.isRunning():
            self.voice_test_worker.wait(3000)

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------
    def collect_config(self) -> dict:
        return {
            "src_video": self.src_edit.text(),
            "out_dir": self.out_dir.path(),
            "keep": self.keep_spin.value(),
            "skip": self.skip_spin.value(),
            "mode": self.mode_combo.currentData(),
            "voice": self.voice_combo.currentData(),
            "language": self.lang_combo.currentData(),
            "subtitles": self.sub_check.isChecked(),
            "sub_effect": self.sub_effect_combo.currentData(),
            "sub_size": self.sub_size.value(),
            "sub_pos": self.sub_pos_combo.currentData(),
            "provider": self.provider_combo.currentData(),
            "ai_keys": self.ai_key_edit.text(),
            "audio_mode": self.audio_mode_combo.currentData(),
            "duck_db": self.duck_spin.value(),
        }

    def apply_config(self, d: dict) -> None:
        if not d:
            return
        self.src_edit.setText(d.get("src_video", ""))
        self.out_dir.setPath(d.get("out_dir", ""))
        self.keep_spin.setValue(float(d.get("keep", 3.0)))
        self.skip_spin.setValue(float(d.get("skip", 10.0)))
        self.mode_combo.setCurrentIndex(
            max(0, self.mode_combo.findData(d.get("mode", "even"))))
        # Language first — it repopulates the voice list.
        lidx = self.lang_combo.findData(d.get("language", "vi"))
        if lidx >= 0:
            self.lang_combo.setCurrentIndex(lidx)
        self._fill_voices(self.lang_combo.currentData())
        idx = self.voice_combo.findData(d.get("voice", ""))
        if idx >= 0:
            self.voice_combo.setCurrentIndex(idx)
        self.sub_check.setChecked(bool(d.get("subtitles", True)))
        self.sub_effect_combo.setCurrentIndex(max(
            0, self.sub_effect_combo.findData(d.get("sub_effect", "none"))))
        self.sub_size.setValue(int(d.get("sub_size", 40)))
        self.sub_pos_combo.setCurrentIndex(max(
            0, self.sub_pos_combo.findData(d.get("sub_pos", "bottom"))))
        self.provider_combo.setCurrentIndex(max(
            0, self.provider_combo.findData(d.get("provider", "gemini"))))
        self.ai_key_edit.setText(d.get("ai_keys", ""))
        self.audio_mode_combo.setCurrentIndex(max(
            0, self.audio_mode_combo.findData(d.get("audio_mode", "duck"))))
        self.duck_spin.setValue(float(d.get("duck_db", -15.0)))
        self._on_audio_mode_changed()
