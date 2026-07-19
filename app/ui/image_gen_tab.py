"""The 'Tạo ảnh AI' tab: batch image generation from prompts via Gemini."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import slideshow, tts
from ..core.image_gen import (
    CF_MODELS, DescribeWorker, ImageGenWorker, normalize_prompts,
)
from ..core.slideshow import VoiceVideoWorker
from .widgets import FolderPicker

COLS = ["#", "Prompt", "Trạng thái", "File"]
COL_PROMPT, COL_STATUS, COL_FILE = 1, 2, 3

PROVIDERS = [
    ("Pollinations (free, không cần key)", "pollinations"),
    ("Cloudflare Workers AI (free tier, cần token)", "cloudflare"),
    ("Gemini API (cần key + billing)", "gemini"),
]
# (label, (width, height, gemini aspectRatio string))
SIZES = [
    ("16:9 — ngang, video YouTube (1280×720)", (1280, 720, "16:9")),
    ("9:16 — dọc, Shorts/TikTok (720×1280)", (720, 1280, "9:16")),
    ("1:1 — vuông (1024×1024)", (1024, 1024, "1:1")),
    ("4:3 — ngang cổ điển (1152×864)", (1152, 864, "4:3")),
    ("3:4 — dọc (864×1152)", (864, 1152, "3:4")),
    ("21:9 — siêu rộng (1344×576)", (1344, 576, "21:9")),
    ("3:2 — ảnh chụp ngang (1216×832)", (1216, 832, "3:2")),
    ("2:3 — ảnh chụp dọc (832×1216)", (832, 1216, "2:3")),
]

OK_COLOR = QColor("#7fd67f")
ERR_COLOR = QColor("#e07a7a")


class ImageGenTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: ImageGenWorker | None = None
        self.describe_worker: DescribeWorker | None = None
        self.vv_worker: VoiceVideoWorker | None = None
        self.voice_test_worker: tts.VoiceTestWorker | None = None
        self._test_player = None
        self._test_audio_out = None
        self._char_image_path = ""
        self._build()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(self._build_config())
        root.addWidget(self._build_character())
        root.addWidget(self._build_prompts(), 1)
        root.addWidget(self._build_voice_video())

        row = QHBoxLayout()
        self.btn_start = QPushButton("▶ Bắt đầu tạo ảnh")
        self.btn_stop = QPushButton("■ Dừng")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.progress = QProgressBar()
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addWidget(self.progress, 1)
        root.addLayout(row)

        self.status = QLabel("Sẵn sàng.")
        root.addWidget(self.status)
        root.addWidget(self._build_table(), 2)

    def _build_config(self) -> QGroupBox:
        g = QGroupBox("Cấu hình tạo ảnh")
        lay = QVBoxLayout(g)

        prov_row = QHBoxLayout()
        prov_row.addWidget(QLabel("Nguồn:"))
        self.provider_combo = QComboBox()
        for label, value in PROVIDERS:
            self.provider_combo.addItem(label, value)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        prov_row.addWidget(self.provider_combo, 1)
        prov_row.addWidget(QLabel("Tỉ lệ:"))
        self.size_combo = QComboBox()
        for label, wha in SIZES:
            self.size_combo.addItem(label, wha)
        self.size_combo.setToolTip(
            "Ảnh đầu ra luôn đúng tỉ lệ này — nguồn nào trả sai tỉ lệ "
            "sẽ được tự động crop giữa ảnh.")
        prov_row.addWidget(self.size_combo)
        lay.addLayout(prov_row)

        cf_row = QHBoxLayout()
        self.cf_account_label = QLabel("Cloudflare Account IDs:")
        self.cf_account = QPlainTextEdit()
        self.cf_account.setFixedHeight(56)
        self.cf_account.setPlaceholderText("Cloudflare Account ID (32 ký tự hex)")
        self.cf_model_label = QLabel("Model:")
        self.cf_model_combo = QComboBox()
        for label, model_id, _ in CF_MODELS:
            self.cf_model_combo.addItem(label, model_id)
        cf_row.addWidget(self.cf_account_label)
        cf_row.addWidget(self.cf_account, 1)
        cf_row.addWidget(self.cf_model_label)
        cf_row.addWidget(self.cf_model_combo, 1)
        lay.addLayout(cf_row)

        self.keys_label = QLabel(
            "API keys Gemini (mỗi dòng 1 key — hết quota tự đổi key kế tiếp):")
        lay.addWidget(self.keys_label)
        self.keys_edit = QPlainTextEdit()
        self.keys_edit.setPlaceholderText("AIza…")
        self.keys_edit.setFixedHeight(70)
        lay.addWidget(self.keys_edit)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Lưu vào:"))
        self.out_dir = FolderPicker("Thư mục lưu ảnh")
        out_row.addWidget(self.out_dir, 1)
        lay.addLayout(out_row)
        self._on_provider_changed()
        return g

    def _provider(self) -> str:
        return self.provider_combo.currentData()

    def _on_provider_changed(self) -> None:
        p = self._provider()
        needs_keys = p in ("gemini", "cloudflare")
        self.keys_edit.setVisible(needs_keys)
        self.keys_label.setVisible(needs_keys)
        if p == "gemini":
            self.keys_label.setText(
                "API keys Gemini (mỗi dòng 1 key — hết quota tự đổi key kế tiếp):")
            self.keys_edit.setPlaceholderText("AIza…")
        elif p == "cloudflare":
            self.keys_label.setText(
                "API tokens Cloudflare (mỗi dòng 1 token — lỗi/hết quota tự đổi token kế tiếp):")
            self.keys_edit.setPlaceholderText("Token từ dash.cloudflare.com → My Profile → API Tokens")
        for w in (self.cf_account_label, self.cf_account,
                  self.cf_model_label, self.cf_model_combo):
            w.setVisible(p == "cloudflare")

    def _build_character(self) -> QGroupBox:
        g = QGroupBox("Nhân vật (tùy chọn — khóa nhân vật cho mọi ảnh)")
        lay = QVBoxLayout(g)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Tên:"))
        self.char_name = QLineEdit()
        self.char_name.setPlaceholderText("vd: Lan")
        row1.addWidget(self.char_name, 1)
        row1.addWidget(QLabel("Mô tả:"))
        self.char_desc = QPlainTextEdit()
        self.char_desc.setPlaceholderText(
            "vd: cô gái 20 tuổi, tóc đen dài, áo dài trắng — càng chi tiết "
            "các ảnh càng đồng nhất; hoặc bấm '🔍 Tạo mô tả từ ảnh' bên dưới")
        self.char_desc.setFixedHeight(58)
        row1.addWidget(self.char_desc, 3)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        btn_img = QPushButton("🖼 Tải ảnh nhân vật…")
        btn_img.clicked.connect(self._pick_char_image)
        btn_clear = QPushButton("✕")
        btn_clear.setFixedWidth(28)
        btn_clear.setToolTip("Bỏ ảnh nhân vật")
        btn_clear.clicked.connect(lambda: self._set_char_image(""))
        self.char_image_label = QLabel("(chưa có ảnh)")
        row2.addWidget(btn_img)
        row2.addWidget(btn_clear)
        row2.addWidget(self.char_image_label, 1)
        row2.addWidget(QLabel("Key Gemini (free):"))
        self.describe_key = QLineEdit()
        self.describe_key.setEchoMode(QLineEdit.Password)
        self.describe_key.setPlaceholderText("key miễn phí từ AI Studio")
        row2.addWidget(self.describe_key, 1)
        self.btn_describe = QPushButton("🔍 Tạo mô tả từ ảnh")
        self.btn_describe.setToolTip(
            "Dùng Gemini free phân tích ảnh nhân vật thành mô tả chi tiết — "
            "cách khóa nhân vật cho Pollinations/Cloudflare (không nhận ảnh "
            "đầu vào trực tiếp).")
        self.btn_describe.clicked.connect(self._describe_char_image)
        row2.addWidget(self.btn_describe)
        lay.addLayout(row2)

        note = QLabel("Có ảnh + Key Gemini (free): tự phân tích ảnh thành mô tả "
                      "khi bắt đầu tạo (nếu ô Mô tả trống). Nguồn Gemini gửi "
                      "thẳng ảnh kèm từng prompt — khóa nhân vật mạnh nhất.")
        note.setStyleSheet("color: #888;")
        lay.addWidget(note)
        return g

    def _describe_char_image(self) -> None:
        if not self._char_image_path:
            QMessageBox.warning(self, "Chưa có ảnh",
                                "Bấm '🖼 Tải ảnh nhân vật…' chọn ảnh trước.")
            return
        key = self.describe_key.text().strip()
        if not key:
            QMessageBox.warning(
                self, "Thiếu key Gemini",
                "Nhập 1 Gemini API key (miễn phí, lấy tại aistudio.google.com "
                "→ Get API key). Key này chỉ dùng model text để phân tích ảnh, "
                "không cần billing.")
            return
        self.btn_describe.setEnabled(False)
        self.status.setText("Đang phân tích ảnh nhân vật…")
        self.describe_worker = DescribeWorker(self._char_image_path, key)
        self.describe_worker.done.connect(self._on_describe_done)
        self.describe_worker.failed.connect(self._on_describe_failed)
        self.describe_worker.start()

    def _on_describe_done(self, desc: str) -> None:
        self.char_desc.setPlainText(desc)
        self.btn_describe.setEnabled(True)
        self.status.setText("Đã tạo mô tả nhân vật từ ảnh — có thể sửa lại "
                            "trước khi tạo ảnh.")

    def _on_describe_failed(self, msg: str) -> None:
        self.btn_describe.setEnabled(True)
        self.status.setText("Sẵn sàng.")
        QMessageBox.warning(self, "Không phân tích được ảnh", msg)

    def _pick_char_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn ảnh nhân vật", "",
            "Ảnh (*.png *.jpg *.jpeg *.webp *.bmp);;Tất cả (*)")
        if path:
            self._set_char_image(path)

    def _set_char_image(self, path: str) -> None:
        self._char_image_path = path
        self.char_image_label.setText(
            path.replace("\\", "/").rsplit("/", 1)[-1] if path
            else "(chưa có ảnh)")

    def _build_prompts(self) -> QGroupBox:
        g = QGroupBox("Prompt (timestamp cùng dòng hoặc riêng dòng đều được)")
        lay = QVBoxLayout(g)
        self.prompts_edit = QPlainTextEdit()
        self.prompts_edit.setPlaceholderText(
            "[00:15] Cô gái đi trong mưa\n\n[00:30]\nBầu trời đêm đầy sao")
        self.prompts_edit.textChanged.connect(self._update_count)
        lay.addWidget(self.prompts_edit, 1)

        brow = QHBoxLayout()
        btn_load = QPushButton("📂 Tải prompt từ file .txt")
        btn_load.clicked.connect(self._load_prompts_file)
        self.count_label = QLabel("0 prompt")
        brow.addWidget(btn_load)
        brow.addStretch(1)
        brow.addWidget(self.count_label)
        lay.addLayout(brow)
        return g

    def _build_table(self) -> QTableWidget:
        t = QTableWidget(0, len(COLS))
        t.setHorizontalHeaderLabels(COLS)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.verticalHeader().setVisible(False)
        hdr = t.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setSectionResizeMode(COL_PROMPT, QHeaderView.Stretch)
        t.setColumnWidth(0, 44)
        t.setColumnWidth(COL_STATUS, 190)
        t.setColumnWidth(COL_FILE, 260)
        self.table = t
        return t

    def _build_voice_video(self) -> QGroupBox:
        g = QGroupBox("Voice / Video từ script")
        lay = QVBoxLayout(g)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Chế độ:"))
        self.vv_mode_combo = QComboBox()
        for label, value in [("Không (chỉ tạo ảnh)", "none"),
                             ("Tạo Voice (mp3)", "voice"),
                             ("Tạo Video (mp4)", "video")]:
            self.vv_mode_combo.addItem(label, value)
        self.vv_mode_combo.currentIndexChanged.connect(self._on_vv_mode_changed)
        mode_row.addWidget(self.vv_mode_combo, 1)
        lay.addLayout(mode_row)

        # Everything below collapses in "none" mode.
        self.vv_body = QWidget()
        body = QVBoxLayout(self.vv_body)
        body.setContentsMargins(0, 0, 0, 0)

        body.addWidget(QLabel(
            "Script thoại (mỗi đoạn: [mm:ss] + lời thoại — \"Voice:\" "
            "có hay không đều được):"))
        self.vv_script_edit = QPlainTextEdit()
        self.vv_script_edit.setPlaceholderText(
            '[00:00]\nVoice: "Have you ever wondered why we yawn?"\n\n'
            '[00:05] voice "Yawning is something every human does."\n\n'
            '[00:10] Or just write the line right after the timestamp.')
        self.vv_script_edit.setFixedHeight(110)
        body.addWidget(self.vv_script_edit)

        srow = QHBoxLayout()
        btn_script = QPushButton("📂 Tải script .txt")
        btn_script.clicked.connect(self._load_script_file)
        srow.addWidget(btn_script)
        srow.addStretch(1)
        body.addLayout(srow)

        vrow = QHBoxLayout()
        vrow.addWidget(QLabel("Giọng đọc:"))
        self.vv_voice_combo = QComboBox()
        for label, voice_id in tts.VOICES:
            self.vv_voice_combo.addItem(label, voice_id)
        vrow.addWidget(self.vv_voice_combo, 1)
        self.btn_voice_test = QPushButton("🔊 Nghe thử")
        self.btn_voice_test.clicked.connect(self._vv_test_voice)
        vrow.addWidget(self.btn_voice_test)
        body.addLayout(vrow)

        timing_row = QHBoxLayout()
        timing_row.addWidget(QLabel("Thời lượng đoạn:"))
        self.vv_timing_combo = QComboBox()
        self.vv_timing_combo.addItem(
            "Khớp mốc [mm:ss] trong script (tăng tốc giọng nếu cần)",
            "timestamps")
        self.vv_timing_combo.addItem("Tự nhiên theo giọng đọc", "auto")
        self.vv_timing_combo.setToolTip(
            "Khớp mốc: mỗi đoạn dài đúng bằng khoảng cách 2 mốc thời gian — "
            "thoại dài hơn sẽ được đọc nhanh lên cho vừa, ngắn hơn thì thêm "
            "khoảng lặng.\nTự nhiên: mỗi đoạn dài theo giọng đọc thật, "
            "mốc chỉ dùng để khớp ảnh.")
        timing_row.addWidget(self.vv_timing_combo, 1)
        body.addLayout(timing_row)

        # Video-only options.
        self.vv_video_opts = QWidget()
        vopts = QVBoxLayout(self.vv_video_opts)
        vopts.setContentsMargins(0, 0, 0, 0)

        trow = QHBoxLayout()
        trow.addWidget(QLabel("Chuyển cảnh:"))
        self.vv_transition_combo = QComboBox()
        self.vv_transition_combo.addItem("Ngẫu nhiên", "random")
        for name in slideshow.TRANSITIONS:
            self.vv_transition_combo.addItem(name, name)
        trow.addWidget(self.vv_transition_combo, 1)
        self.vv_trans_dur = QDoubleSpinBox()
        self.vv_trans_dur.setRange(0.2, 2.0)
        self.vv_trans_dur.setSingleStep(0.1)
        self.vv_trans_dur.setValue(0.5)
        self.vv_trans_dur.setSuffix(" s")
        trow.addWidget(QLabel("Thời lượng:"))
        trow.addWidget(self.vv_trans_dur)
        self.vv_pause = QDoubleSpinBox()
        self.vv_pause.setRange(0.0, 2.0)
        self.vv_pause.setSingleStep(0.1)
        self.vv_pause.setValue(0.3)
        self.vv_pause.setSuffix(" s")
        trow.addWidget(QLabel("Nghỉ giữa câu:"))
        trow.addWidget(self.vv_pause)
        vopts.addLayout(trow)

        sub_row = QHBoxLayout()
        self.vv_sub_check = QCheckBox("Hiện phụ đề")
        sub_row.addWidget(self.vv_sub_check)
        sub_row.addWidget(QLabel("Hiệu ứng chữ:"))
        self.vv_sub_effect_combo = QComboBox()
        self.vv_sub_effect_combo.addItem("Không", "none")
        self.vv_sub_effect_combo.addItem("Fade in/out", "fade")
        sub_row.addWidget(self.vv_sub_effect_combo)
        sub_row.addWidget(QLabel("Cỡ chữ:"))
        self.vv_sub_size = QSpinBox()
        self.vv_sub_size.setRange(16, 96)
        self.vv_sub_size.setValue(40)
        sub_row.addWidget(self.vv_sub_size)
        sub_row.addWidget(QLabel("Vị trí:"))
        self.vv_sub_pos_combo = QComboBox()
        for label, value in [("Dưới", "bottom"), ("Giữa", "middle"),
                             ("Trên", "top")]:
            self.vv_sub_pos_combo.addItem(label, value)
        sub_row.addWidget(self.vv_sub_pos_combo)
        sub_row.addStretch(1)
        vopts.addLayout(sub_row)
        body.addWidget(self.vv_video_opts)

        run_row = QHBoxLayout()
        self.btn_vv_start = QPushButton("🎬 Tạo Voice/Video")
        self.btn_vv_start.clicked.connect(self._vv_start)
        self.btn_vv_stop = QPushButton("■ Dừng")
        self.btn_vv_stop.setEnabled(False)
        self.btn_vv_stop.clicked.connect(self._vv_stop)
        self.vv_progress = QProgressBar()
        self.vv_progress.setRange(0, 100)
        run_row.addWidget(self.btn_vv_start)
        run_row.addWidget(self.btn_vv_stop)
        run_row.addWidget(self.vv_progress, 1)
        body.addLayout(run_row)

        self.vv_status = QLabel("")
        body.addWidget(self.vv_status)

        if not tts.tts_available():
            note = QLabel("⚠ Chưa cài edge-tts — chạy: pip install edge-tts")
            note.setStyleSheet("color:#e0b060;")
            body.addWidget(note)
            self.btn_vv_start.setEnabled(False)
            self.btn_voice_test.setEnabled(False)

        lay.addWidget(self.vv_body)
        self._on_vv_mode_changed()
        return g

    def _vv_mode(self) -> str:
        return self.vv_mode_combo.currentData()

    def _on_vv_mode_changed(self) -> None:
        mode = self._vv_mode()
        self.vv_body.setVisible(mode != "none")
        self.vv_video_opts.setVisible(mode == "video")

    # ------------------------------------------------------------------
    # prompts / keys
    # ------------------------------------------------------------------
    def _parse_prompts(self) -> list[str]:
        return normalize_prompts(self.prompts_edit.toPlainText())

    def _parse_keys(self) -> list[str]:
        return [ln.strip() for ln in self.keys_edit.toPlainText().splitlines()
                if ln.strip()]

    def _parse_cf_account_ids(self) -> list[str]:
        """Account IDs paired line-for-line with Cloudflare API tokens."""
        return [ln.strip() for ln in self.cf_account.toPlainText().splitlines()
                if ln.strip()]

    def _update_count(self) -> None:
        self.count_label.setText(f"{len(self._parse_prompts())} prompt")

    def _load_prompts_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file prompt", "", "Text (*.txt);;Tất cả (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                self.prompts_edit.setPlainText(f.read())
        except OSError as e:
            QMessageBox.warning(self, "Không đọc được file", str(e))

    # ------------------------------------------------------------------
    # run lifecycle
    # ------------------------------------------------------------------
    def _start(self) -> None:
        keys = self._parse_keys()
        account_ids = self._parse_cf_account_ids()
        prompts = self._parse_prompts()
        out = self.out_dir.path()
        if self._provider() in ("gemini", "cloudflare") and not keys:
            QMessageBox.warning(self, "Thiếu API key",
                                "Nhập ít nhất 1 key/token (mỗi dòng 1 cái).")
            return
        if self._provider() == "cloudflare" and not account_ids:
            QMessageBox.warning(self, "Thiếu Account ID",
                                "Nhập Cloudflare Account ID (xem ở trang chủ "
                                "dash.cloudflare.com, cột bên phải).")
            return
        if self._provider() == "cloudflare" and len(account_ids) != len(keys):
            QMessageBox.warning(
                self, "Thiếu cặp Cloudflare",
                "Số Account ID phải bằng số API token; mỗi dòng được ghép thành một cặp.")
            return
        if not prompts:
            QMessageBox.warning(self, "Thiếu prompt",
                                "Nhập prompt (mỗi dòng 1 prompt) hoặc tải từ file .txt.")
            return
        if not out:
            QMessageBox.warning(self, "Thiếu thư mục lưu",
                                "Chọn thư mục lưu ảnh trước khi bắt đầu.")
            return

        self._populate_table(prompts)
        self.progress.setRange(0, len(prompts))
        self.progress.setValue(0)
        w, h, aspect = self.size_combo.currentData()
        self.worker = ImageGenWorker(prompts, out, keys,
                                     provider=self._provider(),
                                     width=w, height=h, aspect=aspect,
                                     account_ids=account_ids,
                                     model=self.cf_model_combo.currentData(),
                                     char_name=self.char_name.text().strip(),
                                     char_desc=self.char_desc.toPlainText().strip(),
                                     char_image_path=self._char_image_path,
                                     describe_key=self.describe_key.text().strip())
        self.worker.item_started.connect(self._on_item_started)
        self.worker.item_status.connect(self._on_item_status)
        self.worker.item_finished.connect(self._on_item_finished)
        self.worker.progress.connect(self._on_progress)
        self.worker.status.connect(self.status.setText)
        self.worker.log.connect(self.status.setText)
        self.worker.finished_all.connect(self._on_done)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status.setText("Đang tạo ảnh…")
        self.worker.start()

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
        self.status.setText("Đang dừng…")

    def stop_worker(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        if self.describe_worker and self.describe_worker.isRunning():
            self.describe_worker.wait(3000)
        if self.vv_worker and self.vv_worker.isRunning():
            self.vv_worker.stop()
            self.vv_worker.wait(5000)
        if self.voice_test_worker and self.voice_test_worker.isRunning():
            self.voice_test_worker.wait(3000)

    # ------------------------------------------------------------------
    # voice / video from script
    # ------------------------------------------------------------------
    def _load_script_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file script", "", "Text (*.txt);;Tất cả (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                self.vv_script_edit.setPlainText(f.read())
        except OSError as e:
            QMessageBox.warning(self, "Không đọc được file", str(e))

    def _vv_start(self) -> None:
        mode = self._vv_mode()
        if mode == "none":
            QMessageBox.information(
                self, "Chưa chọn chế độ",
                "Chọn 'Tạo Voice' hoặc 'Tạo Video' trước.")
            return
        out = self.out_dir.path()
        if not out:
            QMessageBox.warning(self, "Thiếu thư mục lưu",
                                "Chọn thư mục lưu (chung với ảnh) trước.")
            return
        script = self.vv_script_edit.toPlainText()
        segments = slideshow.parse_script(script)
        if not segments:
            QMessageBox.warning(
                self, "Script trống",
                "Dán script dạng:\n[00:00]\nVoice: \"…\"")
            return
        if mode == "video" and not slideshow.list_images(out):
            QMessageBox.warning(
                self, "Chưa có ảnh",
                "Thư mục lưu chưa có ảnh nào — tạo ảnh trước rồi mới tạo video.")
            return

        w, h, _aspect = self.size_combo.currentData()
        self.vv_worker = VoiceVideoWorker(
            mode, script, out, self.vv_voice_combo.currentData(),
            width=w, height=h,
            transition=self.vv_transition_combo.currentData(),
            trans_dur=self.vv_trans_dur.value(),
            subtitles=self.vv_sub_check.isChecked(),
            sub_effect=self.vv_sub_effect_combo.currentData(),
            sub_size=self.vv_sub_size.value(),
            sub_pos=self.vv_sub_pos_combo.currentData(),
            pause=self.vv_pause.value(),
            timing=self.vv_timing_combo.currentData())
        self.vv_worker.status.connect(self.vv_status.setText)
        self.vv_worker.log.connect(self.vv_status.setText)
        self.vv_worker.progress.connect(
            lambda p: self.vv_progress.setValue(int(p * 100)))
        self.vv_worker.finished_job.connect(self._on_vv_done)
        self.btn_vv_start.setEnabled(False)
        self.btn_vv_stop.setEnabled(True)
        self.vv_progress.setValue(0)
        self.vv_status.setText("Bắt đầu…")
        self.vv_worker.start()

    def _vv_stop(self) -> None:
        if self.vv_worker:
            self.vv_worker.stop()
        self.vv_status.setText("Đang dừng…")

    def _on_vv_done(self, ok: bool, detail: str) -> None:
        self.btn_vv_start.setEnabled(tts.tts_available())
        self.btn_vv_stop.setEnabled(False)
        if ok:
            self.vv_status.setText(f"✓ Xong: {detail}")
            QMessageBox.information(self, "Hoàn tất", f"Đã xuất:\n{detail}")
        else:
            self.vv_status.setText(f"✗ {detail}")

    def _vv_test_voice(self) -> None:
        if self.voice_test_worker and self.voice_test_worker.isRunning():
            return
        self.btn_voice_test.setEnabled(False)
        self.vv_status.setText("Đang tạo câu nghe thử…")
        self.voice_test_worker = tts.VoiceTestWorker(
            self.vv_voice_combo.currentData())
        self.voice_test_worker.done.connect(self._on_voice_test_done)
        self.voice_test_worker.failed.connect(self._on_voice_test_failed)
        self.voice_test_worker.start()

    def _on_voice_test_done(self, path: str) -> None:
        self.btn_voice_test.setEnabled(True)
        self.vv_status.setText("Đang phát câu nghe thử…")
        self._play_test_mp3(path)

    def _on_voice_test_failed(self, msg: str) -> None:
        self.btn_voice_test.setEnabled(True)
        self.vv_status.setText("")
        QMessageBox.warning(self, "Không nghe thử được", msg)

    def _play_test_mp3(self, path: str) -> None:
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            # Keep both objects alive as attributes or playback stops.
            self._test_audio_out = QAudioOutput()
            self._test_audio_out.setVolume(1.0)
            self._test_player = QMediaPlayer()
            self._test_player.setAudioOutput(self._test_audio_out)
            self._test_player.setSource(QUrl.fromLocalFile(path))
            self._test_player.play()
        except Exception:
            try:
                os.startfile(path)  # default mp3 app
            except OSError as e:
                QMessageBox.warning(self, "Không phát được audio", str(e))

    # ------------------------------------------------------------------
    # table
    # ------------------------------------------------------------------
    def _populate_table(self, prompts: list[str]) -> None:
        self.table.setRowCount(0)
        self.table.setRowCount(len(prompts))
        for r, line in enumerate(prompts):
            num = QTableWidgetItem(str(r + 1))
            num.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 0, num)
            self.table.setItem(r, COL_PROMPT, QTableWidgetItem(line))
            self.table.setItem(r, COL_STATUS, QTableWidgetItem("Chờ"))
            self.table.setItem(r, COL_FILE, QTableWidgetItem(""))

    def _set_status(self, row: int, text: str, color: QColor | None = None) -> None:
        item = self.table.item(row, COL_STATUS)
        if item is None:
            return
        item.setText(text)
        if color:
            item.setForeground(color)

    def _on_item_started(self, row: int) -> None:
        self._set_status(row, "Đang tạo…")
        self.table.scrollToItem(self.table.item(row, 0))

    def _on_item_status(self, row: int, text: str) -> None:
        self._set_status(row, text)

    def _on_item_finished(self, row: int, ok: bool, detail: str) -> None:
        if ok:
            self._set_status(row, "✓ Xong", OK_COLOR)
            self.table.item(row, COL_FILE).setText(detail)
        else:
            self._set_status(row, detail, ERR_COLOR)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)

    def _on_done(self, ok: int, fail: int) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    # ------------------------------------------------------------------
    # config persistence
    # ------------------------------------------------------------------
    def collect_config(self) -> dict:
        return {
            "provider": self._provider(),
            "size_index": self.size_combo.currentIndex(),
            "api_keys": self._parse_keys(),
            "cf_account_ids": self._parse_cf_account_ids(),
            "cf_model": self.cf_model_combo.currentData(),
            "char_name": self.char_name.text().strip(),
            "char_desc": self.char_desc.toPlainText().strip(),
            "char_image": self._char_image_path,
            "describe_key": self.describe_key.text().strip(),
            "out_dir": self.out_dir.path(),
            "prompts": self.prompts_edit.toPlainText(),
            "vv_mode": self._vv_mode(),
            "vv_script": self.vv_script_edit.toPlainText(),
            "vv_voice": self.vv_voice_combo.currentData(),
            "vv_timing": self.vv_timing_combo.currentData(),
            "vv_transition": self.vv_transition_combo.currentData(),
            "vv_trans_dur": self.vv_trans_dur.value(),
            "vv_pause": self.vv_pause.value(),
            "vv_subtitles": self.vv_sub_check.isChecked(),
            "vv_sub_effect": self.vv_sub_effect_combo.currentData(),
            "vv_sub_size": self.vv_sub_size.value(),
            "vv_sub_pos": self.vv_sub_pos_combo.currentData(),
        }

    def apply_config(self, d: dict) -> None:
        if not d:
            return
        idx = self.provider_combo.findData(d.get("provider", "pollinations"))
        self.provider_combo.setCurrentIndex(max(0, idx))
        self.size_combo.setCurrentIndex(
            min(max(0, d.get("size_index", 0)), self.size_combo.count() - 1))
        self.keys_edit.setPlainText("\n".join(d.get("api_keys", [])))
        account_ids = d.get("cf_account_ids")
        if account_ids is None:  # migrate the old single-Account-ID setting
            account_ids = [d.get("cf_account_id", "")]
        self.cf_account.setPlainText("\n".join(x for x in account_ids if x))
        m = self.cf_model_combo.findData(d.get("cf_model", ""))
        if m >= 0:
            self.cf_model_combo.setCurrentIndex(m)
        self.char_name.setText(d.get("char_name", ""))
        self.char_desc.setPlainText(d.get("char_desc", ""))
        self._set_char_image(d.get("char_image", ""))
        self.describe_key.setText(d.get("describe_key", ""))
        self.out_dir.setPath(d.get("out_dir", ""))
        self.prompts_edit.setPlainText(d.get("prompts", ""))
        i = self.vv_mode_combo.findData(d.get("vv_mode", "none"))
        self.vv_mode_combo.setCurrentIndex(max(0, i))
        self.vv_script_edit.setPlainText(d.get("vv_script", ""))
        i = self.vv_voice_combo.findData(d.get("vv_voice", ""))
        if i >= 0:
            self.vv_voice_combo.setCurrentIndex(i)
        i = self.vv_timing_combo.findData(d.get("vv_timing", "timestamps"))
        if i >= 0:
            self.vv_timing_combo.setCurrentIndex(i)
        i = self.vv_transition_combo.findData(d.get("vv_transition", "random"))
        if i >= 0:
            self.vv_transition_combo.setCurrentIndex(i)
        self.vv_trans_dur.setValue(float(d.get("vv_trans_dur", 0.5)))
        self.vv_pause.setValue(float(d.get("vv_pause", 0.3)))
        self.vv_sub_check.setChecked(bool(d.get("vv_subtitles", False)))
        i = self.vv_sub_effect_combo.findData(d.get("vv_sub_effect", "none"))
        if i >= 0:
            self.vv_sub_effect_combo.setCurrentIndex(i)
        self.vv_sub_size.setValue(int(d.get("vv_sub_size", 40)))
        i = self.vv_sub_pos_combo.findData(d.get("vv_sub_pos", "bottom"))
        if i >= 0:
            self.vv_sub_pos_combo.setCurrentIndex(i)
