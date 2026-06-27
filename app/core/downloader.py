"""Download channel videos via yt-dlp in a background thread.

The YouTube Data API only returns metadata, so actual video files are fetched
with yt-dlp. Import is lazy so the rest of the app works without it installed.

Resolution: we prefer 1080p, falling back to the best stream <=1080p (so >=720p
whenever a 720p+ stream exists). 1080p on YouTube is DASH-only, so it must be
merged from separate video+audio with ffmpeg — we point yt-dlp at the bundled
imageio-ffmpeg binary when no system ffmpeg is on PATH.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

# Best video up to 1080p + best audio (needs merge); else best single file
# up to 1080p; else whatever is available. yt-dlp auto-skips merge formats if
# ffmpeg is unavailable, so this degrades gracefully.
DOWNLOAD_FORMAT = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"


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


def ffmpeg_dir() -> Optional[str]:
    """Directory containing an ffmpeg yt-dlp can use for merging, or None to
    let yt-dlp find a system ffmpeg on PATH. Falls back to copying the
    imageio-ffmpeg binary to a cache dir under the name ``ffmpeg(.exe)``."""
    if shutil.which("ffmpeg"):
        return None
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
    cache = Path(tempfile.gettempdir()) / "cv_ffmpeg"
    cache.mkdir(parents=True, exist_ok=True)
    dst = cache / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    try:
        if not dst.exists() or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copy2(src, dst)
    except OSError:
        return None
    return str(cache)


def build_ydl_opts(dest_dir: str, count: int, download_all: bool,
                   hook=None) -> dict:
    opts = {
        "outtmpl": os.path.join(dest_dir, "%(uploader)s",
                                "%(title)s [%(id)s].%(ext)s"),
        "format": DOWNLOAD_FORMAT,
        "merge_output_format": "mp4",
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    ff = ffmpeg_dir()
    if ff:
        opts["ffmpeg_location"] = ff
    if hook:
        opts["progress_hooks"] = [hook]
    if not download_all:
        opts["playlistend"] = max(1, int(count))
    return opts


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
        self._cur = ""
        self._seen: set[str] = set()

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
            status = d.get("status")
            info = d.get("info_dict") or {}
            if status == "finished":
                # A 1080p download merges video + audio, so the hook fires
                # 'finished' once per stream. Count each video only once by id.
                vid = info.get("id") or d.get("filename", "")
                if vid in self._seen:
                    return
                self._seen.add(vid)
                self._downloaded += 1
                name = info.get("title") or os.path.basename(d.get("filename", ""))
                self.log.emit(f"  ✓ {name}")
            elif status == "downloading":
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

            opts = build_ydl_opts(self.dest_dir, self.count, self.download_all, hook)
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
