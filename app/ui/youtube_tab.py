"""The 'YouTube Finder' tab: search config, results table, context menu, CSV."""

from __future__ import annotations

import csv
import os
from dataclasses import fields as dataclass_fields

from PySide6.QtCore import Qt, QSize, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core.channel_finder import INT_MAX, ChannelFinderWorker, FinderConfig
from ..core.downloader import VideoDownloadWorker, channel_videos_url, ytdlp_available
from ..core.youtube_api import ChannelInfo
from .widgets import FolderPicker

COLS = ["Icon", "Tên Kênh", "Mã kênh", "URL", "Quốc gia", "Ngày tạo",
        "Tuổi kênh (ngày)", "Người đăng ký", "Tổng lượt xem", "Tổng video",
        "Video gần đây", "lượt xem/ngày", "xem/ngày (Cao)", "Video đỉnh ID"]
SORT_COL_HIGH = 12

DEFAULT_API_KEY = "AIzaSyBeOdyYDN5ZzHsXFylajcbO73G1ez4R4mQ"

CSV_FIELDS = ["channel_id", "title", "handle", "url", "country", "published_at",
              "age_days", "subs", "total_views", "total_videos", "recent_count",
              "views_per_day", "views_per_day_high", "top_video_id"]

_INT_FIELDS = {"age_days", "subs", "total_views", "total_videos", "recent_count"}
_FLOAT_FIELDS = {"views_per_day", "views_per_day_high"}

REGIONS = ["", "US", "GB", "JP", "VN", "KR", "IN", "DE", "FR", "BR", "CA",
           "AU", "RU", "ID", "PH", "TH", "ES", "IT", "MX"]

COPY_FORMATS = [
    ("Copy: Channel ID", ["channel_id"]),
    ("Copy: Channel ID | Title", ["channel_id", "title"]),
    ("Copy: Channel ID | Title | URL", ["channel_id", "title", "url"]),
    ("Copy: Channel ID | Title | URL | Country",
     ["channel_id", "title", "url", "country"]),
    ("Copy: Channel ID | Subs | Total Views",
     ["channel_id", "subs", "total_views"]),
]


class NumItem(QTableWidgetItem):
    """Table item that sorts by a stored numeric value, not its text."""

    def __init__(self, value, text):
        super().__init__(text)
        self._v = value

    def __lt__(self, other):
        if isinstance(other, NumItem):
            return self._v < other._v
        return super().__lt__(other)


class HoverDelegate(QStyledItemDelegate):
    """Paints a full-row highlight under the mouse (unless the row is selected)."""

    def paint(self, painter, option, index):
        table = self.parent()
        if (getattr(table, "hover_row", -1) == index.row()
                and not (option.state & QStyle.State_Selected)):
            painter.fillRect(option.rect, QColor("#33445f"))
        super().paint(painter, option, index)


class ChannelTable(QTableWidget):
    """QTableWidget that tracks the hovered row for full-row hover highlight."""

    def __init__(self, rows, cols, parent=None):
        super().__init__(rows, cols, parent)
        self.hover_row = -1
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setItemDelegate(HoverDelegate(self))

    def mouseMoveEvent(self, e):
        idx = self.indexAt(e.pos())
        row = idx.row() if idx.isValid() else -1
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        if self.hover_row != -1:
            self.hover_row = -1
            self.viewport().update()
        super().leaveEvent(e)


class YouTubeTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: ChannelFinderWorker | None = None
        self.dl_worker: VideoDownloadWorker | None = None
        self._build()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _spin(self, default: int, maxv: int = INT_MAX, minv: int = 0) -> QSpinBox:
        s = QSpinBox()
        s.setRange(minv, maxv)
        s.setValue(default)
        s.setGroupSeparatorShown(True)
        return s

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(self._build_config())
        root.addLayout(self._build_download_bar())
        root.addLayout(self._build_url_bar())

        row = QHBoxLayout()
        self.btn_search = QPushButton("🔎 Bắt đầu tìm")
        self.btn_stop = QPushButton("■ Dừng")
        self.btn_stop.setEnabled(False)
        self.btn_search.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.progress = QProgressBar()
        self.status = QLabel("Sẵn sàng.")
        row.addWidget(self.btn_search)
        row.addWidget(self.btn_stop)
        row.addWidget(self.progress, 1)
        root.addLayout(row)
        root.addWidget(self.status)

        frow = QHBoxLayout()
        frow.addWidget(QLabel("Lọc:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Lọc nhanh (theo bất kỳ cột nào)…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        frow.addWidget(self.filter_edit, 1)
        root.addLayout(frow)

        root.addWidget(self._build_table(), 1)

        brow = QHBoxLayout()
        btn_import = QPushButton("Import CSV")
        btn_export = QPushButton("Export CSV")
        btn_import.clicked.connect(self._import_csv)
        btn_export.clicked.connect(self._export_csv)
        brow.addWidget(btn_import)
        brow.addStretch(1)
        brow.addWidget(btn_export)
        root.addLayout(brow)

    def _build_config(self) -> QGroupBox:
        g = QGroupBox("Cấu hình tìm kiếm")
        grid = QGridLayout(g)

        self.api_key = QPlainTextEdit()
        self.api_key.setPlaceholderText(
            "Mỗi dòng 1 YouTube Data API key — hết quota key 1 sẽ tự chuyển "
            "sang key 2… (hoặc đặt biến môi trường YOUTUBE_API_KEY)")
        self.api_key.setPlainText(DEFAULT_API_KEY)
        self.api_key.setFixedHeight(
            self.api_key.fontMetrics().lineSpacing() * 3 + 14)
        self.api_key.setTabChangesFocus(True)
        grid.addWidget(QLabel("API keys:"), 0, 0)
        grid.addWidget(self.api_key, 0, 1, 1, 5)

        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("để trống nếu lấy TOP TRENDING")
        self.region = QComboBox()
        self.region.setEditable(True)
        for code in REGIONS:
            self.region.addItem(code if code else "(global)", code)
        self.region.setCurrentIndex(REGIONS.index("US"))
        self.posted_days = self._spin(30)
        self.recent = self._spin(8)
        self.max_results = self._spin(100)
        self.min_subs = self._spin(0)
        self.max_subs = self._spin(INT_MAX)
        self.min_views = self._spin(0)
        self.max_views = self._spin(INT_MAX)
        self.min_age = self._spin(0)
        self.max_age = self._spin(18250)
        self.min_total = self._spin(0)
        self.threads = self._spin(5, maxv=32, minv=1)
        self.top_trending = QCheckBox("TOP TRENDING (theo quốc gia)")
        self.top_trending.setToolTip(
            "Bỏ qua từ khóa, lấy video thịnh hành (mostPopular) theo khu vực.")
        self.strict_region = QCheckBox("Chỉ kênh khai báo đúng quốc gia")
        self.strict_region.setToolTip(
            "Khi chọn khu vực, kênh khai báo quốc gia khác luôn bị loại.\n"
            "Bật ô này để loại cả kênh KHÔNG khai báo quốc gia\n"
            "(kết quả chuẩn hơn nhưng sẽ ít hơn đáng kể).")

        cells = [
            ("Từ khóa:", self.keyword), ("Khu vực:", self.region),
            ("Video đăng trong (ngày):", self.posted_days),
            ("Video gần đây / kênh:", self.recent),
            ("Kết quả tối đa / từ khóa:", self.max_results),
            ("Tối thiểu sub:", self.min_subs),
            ("Tối đa sub:", self.max_subs),
            ("Tối thiểu lượt xem:", self.min_views),
            ("Tối đa lượt xem:", self.max_views),
            ("Tuổi kênh tối thiểu (ngày):", self.min_age),
            ("Tuổi kênh tối đa (ngày):", self.max_age),
            ("Tối thiểu tổng video:", self.min_total),
            ("Số luồng xử lý:", self.threads),
        ]
        for i, (label, widget) in enumerate(cells):
            r = 1 + i // 3
            c = (i % 3) * 2
            grid.addWidget(QLabel(label), r, c)
            grid.addWidget(widget, r, c + 1)
        last_row = 1 + len(cells) // 3
        grid.addWidget(self.top_trending, last_row, 0, 1, 2)
        grid.addWidget(self.strict_region, last_row, 2, 1, 2)
        return g

    def _build_download_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Tải về:"))
        self.dl_dir = FolderPicker("Thư mục lưu video tải về")
        bar.addWidget(self.dl_dir, 1)
        bar.addWidget(QLabel("Số video / kênh:"))
        self.dl_count = self._spin(5, maxv=100000, minv=1)
        bar.addWidget(self.dl_count)
        self.dl_all = QCheckBox("Tải tất cả")
        self.dl_all.toggled.connect(self.dl_count.setDisabled)
        bar.addWidget(self.dl_all)
        return bar

    def _build_url_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addWidget(QLabel("URL kênh:"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "Dán URL kênh YouTube (vd https://www.youtube.com/@kenh) rồi bấm Tải video")
        self.url_edit.returnPressed.connect(self._download_from_url)
        bar.addWidget(self.url_edit, 1)
        self.btn_url_dl = QPushButton("⬇ Tải video")
        self.btn_url_dl.clicked.connect(self._download_from_url)
        bar.addWidget(self.btn_url_dl)
        return bar

    def _build_table(self) -> QTableWidget:
        t = ChannelTable(0, len(COLS))
        t.setHorizontalHeaderLabels(COLS)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setIconSize(QSize(28, 28))
        t.setContextMenuPolicy(Qt.CustomContextMenu)
        t.customContextMenuRequested.connect(self._context_menu)
        t.verticalHeader().setVisible(False)
        hdr = t.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        widths = [44, 160, 180, 200, 64, 92, 110, 100, 110, 80, 92, 100, 100, 130]
        for i, w in enumerate(widths):
            t.setColumnWidth(i, w)
        self.table = t
        return t

    # ------------------------------------------------------------------
    # search lifecycle
    # ------------------------------------------------------------------
    def _region_value(self) -> str:
        t = self.region.currentText().strip()
        return "" if t in ("", "(global)") else t.upper()

    def _build_finder_config(self) -> FinderConfig:
        return FinderConfig(
            keyword=self.keyword.text().strip(),
            region=self._region_value(),
            posted_days=self.posted_days.value(),
            recent_per_channel=self.recent.value(),
            max_results=self.max_results.value(),
            min_subs=self.min_subs.value(), max_subs=self.max_subs.value(),
            min_views=self.min_views.value(), max_views=self.max_views.value(),
            min_age_days=self.min_age.value(), max_age_days=self.max_age.value(),
            min_total_videos=self.min_total.value(),
            threads=self.threads.value(),
            top_trending=self.top_trending.isChecked(),
            strict_region=self.strict_region.isChecked(),
        )

    def _api_keys(self) -> list[str]:
        """One key per line in the text area; env var as fallback."""
        keys = [ln.strip() for ln in self.api_key.toPlainText().splitlines()
                if ln.strip()]
        if not keys and os.environ.get("YOUTUBE_API_KEY"):
            keys = [os.environ["YOUTUBE_API_KEY"]]
        return keys

    def _start(self) -> None:
        keys = self._api_keys()
        if not keys:
            QMessageBox.warning(self, "Thiếu API key",
                                "Nhập YouTube Data API key (mỗi dòng 1 key) "
                                "hoặc đặt biến môi trường YOUTUBE_API_KEY.")
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.progress.setRange(0, 0)  # busy until first progress tick
        self.worker = ChannelFinderWorker(self._build_finder_config(), keys)
        self.worker.channel_found.connect(self._add_row)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self.status.setText)
        self.worker.status.connect(self.status.setText)
        self.worker.finished_all.connect(self._on_done)
        self.btn_search.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.worker.start()

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
        if self.dl_worker:
            self.dl_worker.stop()
        self.status.setText("Đang dừng…")

    def stop_worker(self) -> None:
        for w in (self.worker, self.dl_worker):
            if w and w.isRunning():
                w.stop()
                w.wait(3000)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)

    def _on_done(self, count: int) -> None:
        self.btn_search.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.table.setSortingEnabled(True)
        self.table.sortItems(SORT_COL_HIGH, Qt.DescendingOrder)
        self.status.setText(f"Hoàn tất: {self.table.rowCount()} kênh.")

    # ------------------------------------------------------------------
    # table population
    # ------------------------------------------------------------------
    def _num(self, value, is_float: bool = False) -> NumItem:
        text = f"{value:,.2f}" if is_float else f"{int(value):,}"
        return NumItem(value, text)

    def _add_row(self, info: ChannelInfo) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        icon_item = QTableWidgetItem()
        if info.thumb_bytes:
            pm = QPixmap()
            if pm.loadFromData(info.thumb_bytes):
                icon_item.setIcon(QIcon(pm))
        icon_item.setData(Qt.UserRole, info)
        self.table.setItem(r, 0, icon_item)
        self.table.setItem(r, 1, QTableWidgetItem(info.title))
        self.table.setItem(r, 2, QTableWidgetItem(info.channel_id))
        self.table.setItem(r, 3, QTableWidgetItem(info.url))
        self.table.setItem(r, 4, QTableWidgetItem(info.country))
        self.table.setItem(r, 5, QTableWidgetItem((info.published_at or "")[:10]))
        self.table.setItem(r, 6, self._num(info.age_days))
        self.table.setItem(r, 7, self._num(info.subs))
        self.table.setItem(r, 8, self._num(info.total_views))
        self.table.setItem(r, 9, self._num(info.total_videos))
        self.table.setItem(r, 10, self._num(info.recent_count))
        self.table.setItem(r, 11, self._num(info.views_per_day, True))
        self.table.setItem(r, 12, self._num(info.views_per_day_high, True))
        self.table.setItem(r, 13, QTableWidgetItem(info.top_video_id))

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        for r in range(self.table.rowCount()):
            if not text:
                self.table.setRowHidden(r, False)
                continue
            match = any(
                (self.table.item(r, c) and text in self.table.item(r, c).text().lower())
                for c in range(1, self.table.columnCount()))
            self.table.setRowHidden(r, not match)

    # ------------------------------------------------------------------
    # context menu / copy / open
    # ------------------------------------------------------------------
    def _selected_infos(self) -> list[ChannelInfo]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        out = []
        for r in rows:
            item = self.table.item(r, 0)
            if item and isinstance(item.data(Qt.UserRole), ChannelInfo):
                out.append(item.data(Qt.UserRole))
        return out

    def _context_menu(self, pos) -> None:
        if self.table.itemAt(pos) is None:
            return
        infos = self._selected_infos()
        if not infos:
            return
        menu = QMenu(self)
        act_open = menu.addAction("Mở kênh")
        act_download = menu.addAction("⬇ Tải video của kênh này")
        menu.addSeparator()
        copy_acts = [(menu.addAction(label), attrs) for label, attrs in COPY_FORMATS]
        act_custom = menu.addAction("Copy: Custom fields (key ngăn cách dấu phẩy)…")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_open:
            for info in infos:
                QDesktopServices.openUrl(QUrl(info.url))
        elif chosen is act_download:
            self._download_selected(infos)
        elif chosen is act_custom:
            self._copy_custom(infos)
        else:
            for act, attrs in copy_acts:
                if chosen is act:
                    self._copy(infos, attrs)
                    break

    def _copy(self, infos: list[ChannelInfo], attrs: list[str]) -> None:
        lines = [" | ".join(str(getattr(info, a, "")) for a in attrs)
                 for info in infos]
        QApplication.clipboard().setText("\n".join(lines))
        self.status.setText(f"Đã copy {len(lines)} dòng.")

    def _copy_custom(self, infos: list[ChannelInfo]) -> None:
        valid = {f.name for f in dataclass_fields(ChannelInfo)}
        text, ok = QInputDialog.getText(
            self, "Custom fields",
            f"Nhập key, ngăn cách dấu phẩy.\nHợp lệ: {', '.join(sorted(valid))}")
        if not ok:
            return
        attrs = [k.strip() for k in text.split(",") if k.strip() in valid]
        if attrs:
            self._copy(infos, attrs)

    # ------------------------------------------------------------------
    # Download (yt-dlp)
    # ------------------------------------------------------------------
    def _start_download(self, jobs: list[tuple[str, str]]) -> None:
        if not ytdlp_available():
            QMessageBox.warning(self, "Thiếu yt-dlp",
                                "Cần cài yt-dlp để tải video:\n\n"
                                "    python -m pip install yt-dlp")
            return
        dest = self.dl_dir.path()
        if not dest:
            QMessageBox.warning(self, "Thiếu thư mục",
                                "Chọn 'Tải về' (thư mục lưu video) trước.")
            return
        if self.dl_worker and self.dl_worker.isRunning():
            QMessageBox.information(self, "Đang tải",
                                    "Đang có tác vụ tải chạy, vui lòng đợi/Dừng.")
            return
        if not jobs:
            return
        n = "tất cả" if self.dl_all.isChecked() else str(self.dl_count.value())
        self.dl_worker = VideoDownloadWorker(
            jobs, dest, self.dl_count.value(), self.dl_all.isChecked())
        self.dl_worker.log.connect(self.status.setText)
        self.dl_worker.status.connect(self.status.setText)
        self.dl_worker.progress.connect(self._on_progress)
        self.dl_worker.finished_all.connect(self._on_download_done)
        self.btn_stop.setEnabled(True)
        self.status.setText(f"Bắt đầu tải {n} video cho {len(jobs)} mục…")
        self.dl_worker.start()

    def _download_selected(self, infos: list[ChannelInfo]) -> None:
        self._start_download([
            (info.title or info.channel_id,
             channel_videos_url(info.channel_id, info.uploads_playlist))
            for info in infos])

    @staticmethod
    def _normalize_channel_url(url: str) -> str:
        """Point a bare channel URL at its Videos tab so 'first N' = newest N."""
        url = url.strip()
        low = url.lower()
        if any(k in low for k in ("watch?v=", "list=", "/videos", "/shorts",
                                  "/streams")):
            return url
        if any(k in low for k in ("/channel/", "/@", "/c/", "/user/")):
            return url.rstrip("/") + "/videos"
        return url

    def _download_from_url(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Thiếu URL", "Nhập URL kênh YouTube.")
            return
        self._start_download([(url, self._normalize_channel_url(url))])

    def _on_download_done(self, n: int) -> None:
        if not (self.worker and self.worker.isRunning()):
            self.btn_stop.setEnabled(False)
        QMessageBox.information(self, "Tải xong", f"Đã tải {n} video.")

    # ------------------------------------------------------------------
    # CSV
    # ------------------------------------------------------------------
    def _all_infos(self) -> list[ChannelInfo]:
        out = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and isinstance(item.data(Qt.UserRole), ChannelInfo):
                out.append(item.data(Qt.UserRole))
        return out

    def _export_csv(self) -> None:
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "Trống", "Chưa có dữ liệu để xuất.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "channels.csv",
                                              "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for info in self._all_infos():
                w.writerow({k: getattr(info, k, "") for k in CSV_FIELDS})
        self.status.setText(f"Đã xuất {self.table.rowCount()} kênh → {path}")

    def _coerce(self, key: str, value):
        try:
            if key in _INT_FIELDS:
                return int(float(value))
            if key in _FLOAT_FIELDS:
                return float(value)
        except (TypeError, ValueError):
            return 0
        return value or ""

    def _import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import CSV", "", "CSV (*.csv)")
        if not path:
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                info = ChannelInfo(channel_id=row.get("channel_id", ""))
                for k in CSV_FIELDS:
                    if k in row:
                        setattr(info, k, self._coerce(k, row.get(k)))
                self._add_row(info)
        self.table.setSortingEnabled(True)
        self.status.setText(f"Đã nhập {self.table.rowCount()} kênh từ {path}")

    # ------------------------------------------------------------------
    # config persistence (used by MainWindow)
    # ------------------------------------------------------------------
    def collect_config(self) -> dict:
        return {
            "api_key": self.api_key.toPlainText(),
            "keyword": self.keyword.text(),
            "region": self._region_value(),
            "posted_days": self.posted_days.value(),
            "recent_per_channel": self.recent.value(),
            "max_results": self.max_results.value(),
            "min_subs": self.min_subs.value(), "max_subs": self.max_subs.value(),
            "min_views": self.min_views.value(), "max_views": self.max_views.value(),
            "min_age_days": self.min_age.value(), "max_age_days": self.max_age.value(),
            "min_total_videos": self.min_total.value(),
            "threads": self.threads.value(),
            "top_trending": self.top_trending.isChecked(),
            "strict_region": self.strict_region.isChecked(),
            "dl_dir": self.dl_dir.path(),
            "dl_count": self.dl_count.value(),
            "dl_all": self.dl_all.isChecked(),
        }

    def apply_config(self, d: dict) -> None:
        if not d:
            return
        self.api_key.setPlainText(d.get("api_key") or DEFAULT_API_KEY)
        self.keyword.setText(d.get("keyword", ""))
        code = d.get("region", "US")
        idx = self.region.findData(code)
        if idx >= 0:
            self.region.setCurrentIndex(idx)
        else:
            self.region.setEditText(code or "")
        self.posted_days.setValue(d.get("posted_days", 30))
        self.recent.setValue(d.get("recent_per_channel", 8))
        self.max_results.setValue(d.get("max_results", 100))
        self.min_subs.setValue(d.get("min_subs", 0))
        self.max_subs.setValue(d.get("max_subs", INT_MAX))
        self.min_views.setValue(d.get("min_views", 0))
        self.max_views.setValue(d.get("max_views", INT_MAX))
        self.min_age.setValue(d.get("min_age_days", 0))
        self.max_age.setValue(d.get("max_age_days", 18250))
        self.min_total.setValue(d.get("min_total_videos", 0))
        self.threads.setValue(d.get("threads", 5))
        self.top_trending.setChecked(d.get("top_trending", False))
        self.strict_region.setChecked(d.get("strict_region", False))
        self.dl_dir.setPath(d.get("dl_dir", ""))
        self.dl_count.setValue(d.get("dl_count", 5))
        self.dl_all.setChecked(d.get("dl_all", False))
