"""Orchestrates channel discovery in a background thread.

Follows the same QThread + stop-Event pattern as
``app/core/batch_processor.py``.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

from . import youtube_api as yt
from .youtube_api import ChannelInfo, YouTubeApiError

INT_MAX = 2147483647


@dataclass
class FinderConfig:
    keyword: str = ""
    region: str = "US"
    posted_days: int = 30
    recent_per_channel: int = 8
    max_results: int = 100
    min_subs: int = 0
    max_subs: int = INT_MAX
    min_views: int = 0
    max_views: int = INT_MAX
    min_age_days: int = 0
    max_age_days: int = 18250
    min_total_videos: int = 0
    threads: int = 5
    top_trending: bool = False
    strict_region: bool = False   # also drop channels with no declared country


class ChannelFinderWorker(QThread):
    channel_found = Signal(object)   # ChannelInfo
    progress = Signal(int, int)      # done, total
    log = Signal(str)
    status = Signal(str)
    finished_all = Signal(int)       # count emitted

    def __init__(self, cfg: FinderConfig, keys, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        if isinstance(keys, str):
            keys = [keys]
        self.key = yt.KeyPool(keys, on_rotate=self._on_key_rotate)
        self._stop = threading.Event()

    def _on_key_rotate(self, pos: int, total: int) -> None:
        self.log.emit(f"⚠ API key hết quota → chuyển sang key {pos}/{total}")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            self._run()
        except YouTubeApiError as e:
            self.log.emit(f"✗ Lỗi API: {e}")
            self.finished_all.emit(0)
        except Exception as e:  # noqa: BLE001 - surface anything to the UI log
            self.log.emit(f"✗ Lỗi: {e}")
            self.finished_all.emit(0)

    def _run(self) -> None:
        cfg, key = self.cfg, self.key
        trending = cfg.top_trending or not cfg.keyword.strip()

        self.status.emit("Đang lấy danh sách kênh…")
        if trending:
            self.log.emit(f"Chế độ TOP TRENDING (region={cfg.region or 'US'})")
            ids = yt.trending_channel_ids(key, cfg.region, cfg.max_results)
        else:
            pub = yt.iso_days_ago(cfg.posted_days)
            self.log.emit(f"Chế độ từ khóa: '{cfg.keyword}' (đăng sau {pub[:10]})")
            ids = yt.search_video_channel_ids(
                key, cfg.keyword, cfg.region, pub, cfg.max_results)

        if self._stop.is_set():
            self.finished_all.emit(0)
            return
        self.log.emit(f"Tìm thấy {len(ids)} kênh. Đang lấy thống kê…")

        raw = yt.list_channels(key, ids)
        base = []
        for cid in ids:               # preserve discovery order
            r = raw.get(cid)
            if r and self._passes(info := yt.parse_channel(r)):
                base.append(info)
        self.log.emit(f"{len(base)}/{len(ids)} kênh qua bộ lọc. "
                      f"Đang lấy chỉ số video gần đây…")

        total = len(base)
        done = 0
        if total == 0:
            self.status.emit("Không có kênh phù hợp.")
            self.finished_all.emit(0)
            return

        with ThreadPoolExecutor(max_workers=max(1, cfg.threads)) as ex:
            futures = {ex.submit(self._enrich, info): info for info in base}
            for fut in as_completed(futures):
                if self._stop.is_set():
                    ex.shutdown(cancel_futures=True)
                    break
                info = fut.result()
                done += 1
                self.channel_found.emit(info)
                self.progress.emit(done, total)

        self.status.emit(f"Hoàn tất: {done} kênh." if not self._stop.is_set()
                         else f"Đã dừng ({done} kênh).")
        self.finished_all.emit(done)

    # ------------------------------------------------------------------
    def _passes(self, info: ChannelInfo) -> bool:
        c = self.cfg
        # regionCode on the API only biases the search; enforce the country
        # declared on the channel here so US really means US.
        if c.region:
            country = (info.country or "").upper()
            if country and country != c.region.upper():
                return False
            if not country and c.strict_region:
                return False
        if not (c.min_subs <= info.subs <= c.max_subs):
            return False
        if not (c.min_views <= info.total_views <= c.max_views):
            return False
        if not (c.min_age_days <= info.age_days <= c.max_age_days):
            return False
        if info.total_videos < c.min_total_videos:
            return False
        return True

    def _enrich(self, info: ChannelInfo) -> ChannelInfo:
        if self._stop.is_set():
            return info
        try:
            if info.uploads_playlist:
                vids = yt.recent_video_ids(
                    self.key, info.uploads_playlist, self.cfg.recent_per_channel)
                stats = yt.list_video_stats(self.key, vids) if vids else []
                info.recent_count = len(stats)
                info.views_per_day_high, info.top_video_id = yt.recent_metrics(stats)
        except YouTubeApiError as e:
            self.log.emit(f"  ⚠ {info.title}: {e}")
        if info.thumb_url:
            try:
                info.thumb_bytes = yt.download_bytes(info.thumb_url)
            except Exception:  # noqa: BLE001 - thumbnails are best-effort
                pass
        return info
