"""Download channel videos via yt-dlp in a background thread.

The YouTube Data API only returns metadata, so actual video files are fetched
with yt-dlp. Import is lazy so the rest of the app works without it installed.
"""

from __future__ import annotations

import os
import threading
from typing import List, Tuple

from PySide6.QtCore import QThread, Signal


def ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False


def channel_videos_url(channel_id: str, uploads_playlist: str = "") -> str:
    """Best URL to enumerate a channel's videos (newest first)."""
    if uploads_playlist:
        return f"https://www.youtube.com/playlist?list={uploads_playlist}"
    return f"https://www.youtube.com/channel/{channel_id}/videos"


class _Cancelled(Exception):
    pass


class VideoDownloadWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int)   # channels done, total channels
    status = Signal(str)
    finished_all = Signal(int)    # videos downloaded

    def __init__(self, jobs: List[Tuple[str, str]], dest_dir: str, count: int,
                 download_all: bool, parent=None):
        super().__init__(parent)
        self.jobs = jobs              # [(channel_title, videos_url), ...]
        self.dest_dir = dest_dir
        self.count = count
        self.download_all = download_all
        self._stop = threading.Event()
        self._downloaded = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            import yt_dlp
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"✗ Chưa cài yt-dlp: {e}")
            self.finished_all.emit(0)
            return

        def hook(d):
            if self._stop.is_set():
                raise _Cancelled()
            if d.get("status") == "finished":
                self._downloaded += 1
                self.log.emit(f"  ✓ {os.path.basename(d.get('filename', ''))}")
            elif d.get("status") == "downloading":
                pct = (d.get("_percent_str") or "").strip()
                if pct:
                    self.status.emit(f"{self._cur} — {pct}")

        total = len(self.jobs)
        for i, (title, url) in enumerate(self.jobs, 1):
            if self._stop.is_set():
                break
            self._cur = f"[{i}/{total}] {title}"
            self.status.emit(f"Đang tải {self._cur}…")
            self.progress.emit(i - 1, total)

            opts = {
                "outtmpl": os.path.join(self.dest_dir, "%(uploader)s",
                                        "%(title)s [%(id)s].%(ext)s"),
                "format": "best[ext=mp4]/best",   # single file: no ffmpeg merge
                "ignoreerrors": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "progress_hooks": [hook],
            }
            if not self.download_all:
                opts["playlistend"] = max(1, self.count)

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except _Cancelled:
                self.log.emit("  ■ Đã dừng tải.")
                break
            except Exception as e:  # noqa: BLE001
                self.log.emit(f"  ⚠ Lỗi tải '{title}': {e}")

        self.progress.emit(total, total)
        self.status.emit(f"Tải xong: {self._downloaded} video → {self.dest_dir}")
        self.finished_all.emit(self._downloaded)
