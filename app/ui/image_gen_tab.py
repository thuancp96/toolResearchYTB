"""The 'Tạo ảnh AI' tab: batch image generation from prompts via Gemini."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..core.image_gen import CF_MODELS, DescribeWorker, ImageGenWorker
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
        self.cf_account_label = QLabel("Account ID:")
        self.cf_account = QLineEdit()
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
        g = QGroupBox("Prompt (mỗi dòng 1 prompt, vd: [00:15] Cô gái đi trong mưa)")
        lay = QVBoxLayout(g)
        self.prompts_edit = QPlainTextEdit()
        self.prompts_edit.setPlaceholderText(
            "[00:15] Cô gái đi trong mưa\n[00:30] Bầu trời đêm đầy sao")
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

    # ------------------------------------------------------------------
    # prompts / keys
    # ------------------------------------------------------------------
    def _parse_prompts(self) -> list[str]:
        return [ln.strip() for ln in self.prompts_edit.toPlainText().splitlines()
                if ln.strip()]

    def _parse_keys(self) -> list[str]:
        return [ln.strip() for ln in self.keys_edit.toPlainText().splitlines()
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
        prompts = self._parse_prompts()
        out = self.out_dir.path()
        if self._provider() in ("gemini", "cloudflare") and not keys:
            QMessageBox.warning(self, "Thiếu API key",
                                "Nhập ít nhất 1 key/token (mỗi dòng 1 cái).")
            return
        if self._provider() == "cloudflare" and not self.cf_account.text().strip():
            QMessageBox.warning(self, "Thiếu Account ID",
                                "Nhập Cloudflare Account ID (xem ở trang chủ "
                                "dash.cloudflare.com, cột bên phải).")
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
                                     account_id=self.cf_account.text().strip(),
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
            "cf_account_id": self.cf_account.text().strip(),
            "cf_model": self.cf_model_combo.currentData(),
            "char_name": self.char_name.text().strip(),
            "char_desc": self.char_desc.toPlainText().strip(),
            "char_image": self._char_image_path,
            "describe_key": self.describe_key.text().strip(),
            "out_dir": self.out_dir.path(),
            "prompts": self.prompts_edit.toPlainText(),
        }

    def apply_config(self, d: dict) -> None:
        if not d:
            return
        idx = self.provider_combo.findData(d.get("provider", "pollinations"))
        self.provider_combo.setCurrentIndex(max(0, idx))
        self.size_combo.setCurrentIndex(
            min(max(0, d.get("size_index", 0)), self.size_combo.count() - 1))
        self.keys_edit.setPlainText("\n".join(d.get("api_keys", [])))
        self.cf_account.setText(d.get("cf_account_id", ""))
        m = self.cf_model_combo.findData(d.get("cf_model", ""))
        if m >= 0:
            self.cf_model_combo.setCurrentIndex(m)
        self.char_name.setText(d.get("char_name", ""))
        self.char_desc.setPlainText(d.get("char_desc", ""))
        self._set_char_image(d.get("char_image", ""))
        self.describe_key.setText(d.get("describe_key", ""))
        self.out_dir.setPath(d.get("out_dir", ""))
        self.prompts_edit.setPlainText(d.get("prompts", ""))
