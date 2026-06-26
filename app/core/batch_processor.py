"""Background worker that processes every video in a folder."""

from __future__ import annotations

import copy
import tempfile
import threading
from pathlib import Path
from typing import List

from PySide6.QtCore import QThread, Signal

from . import ffmpeg_runner
from .layout_model import Layout
from .text_render import render_text_png
from .transcribe import Transcriber
from .video_probe import probe


def _clean_stem(stem: str) -> str:
    return stem.replace("_", " ").replace("-", " ").strip()


def _first_sentence(text: str, limit: int = 70) -> str:
    text = text.strip()
    for sep in (". ", "! ", "? ", "\n"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    return text[:limit].strip()


class BatchWorker(QThread):
    file_started = Signal(int, int, str)     # idx(1-based), total, name
    file_progress = Signal(int, int, float)  # idx, total, pct(0..1)
    file_finished = Signal(str, bool)        # output path, ok
    log = Signal(str)
    finished_all = Signal(int, int)          # success_count, total

    def __init__(self, files: List[str], out_dir: str, layout: Layout,
                 options: dict, parent=None):
        super().__init__(parent)
        self.files = files
        self.out_dir = Path(out_dir)
        self.layout = layout
        self.options = options
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _resolve(self, source: str, manual: str, stem: str,
                 transcript: str, is_title: bool) -> str:
        if source == "manual":
            return manual
        if source == "filename":
            return _clean_stem(stem)
        if source == "whisper":
            if not transcript:
                return _clean_stem(stem)
            return _first_sentence(transcript) if is_title else transcript
        return manual

    def run(self) -> None:
        total = len(self.files)
        success = 0
        transcriber = None
        opts = self.options
        out_opts = opts.get("out_opts", {})
        suffix = opts.get("suffix", "_out")
        container = opts.get("container", ".mp4")
        whisper_enabled = opts.get("whisper_enabled", False)

        self.out_dir.mkdir(parents=True, exist_ok=True)

        for idx, f in enumerate(self.files, start=1):
            if self._stop.is_set():
                break
            name = Path(f).name
            self.file_started.emit(idx, total, name)
            self.log.emit(f"[{idx}/{total}] {name}")

            try:
                info = probe(f)
            except Exception as e:
                self.log.emit(f"  ✗ Không đọc được video: {e}")
                self.file_finished.emit(f, False)
                continue

            lay = copy.deepcopy(self.layout)
            transcript = ""
            need_whisper = whisper_enabled and (
                lay.title_source == "whisper" or lay.desc_source == "whisper")
            if need_whisper:
                try:
                    if transcriber is None:
                        self.log.emit("  Đang nạp model Whisper…")
                        transcriber = Transcriber(opts.get("model_size", "base"))
                        transcriber.load()
                    self.log.emit("  Đang nhận dạng giọng nói…")
                    transcript = transcriber.transcribe(f, opts.get("language") or None)
                except Exception as e:
                    self.log.emit(f"  ⚠ Whisper lỗi, dùng tên file: {e}")

            stem = Path(f).stem
            lay.title_text = self._resolve(lay.title_source, lay.title_text,
                                           stem, transcript, True)
            lay.desc_text = self._resolve(lay.desc_source, lay.desc_text,
                                          stem, transcript, False)

            cw, ch = lay.canvas_size()
            tmp = Path(tempfile.gettempdir())
            title_png = desc_png = None
            if lay.title_text.strip():
                _, _, tw, th = lay.title.to_pixels(cw, ch)
                p = str(tmp / f"cv_title_{idx}.png")
                if render_text_png(lay.title_text, tw, th, lay.title_style, p):
                    title_png = p
            if lay.desc_text.strip():
                _, _, dw, dh = lay.desc.to_pixels(cw, ch)
                p = str(tmp / f"cv_desc_{idx}.png")
                if render_text_png(lay.desc_text, dw, dh, lay.desc_style, p):
                    desc_png = p

            out_path = self.out_dir / f"{stem}{suffix}{container}"
            speed = max(0.1, lay.audio_speed)
            out_seconds = (info.duration / speed) if info.duration else 0.0

            cmd = ffmpeg_runner.build_command(
                f, str(out_path), lay, info.fps, info.has_audio,
                title_png, desc_png, out_opts)
            self.log.emit("  Đang render…")
            rc = ffmpeg_runner.run(
                cmd, out_seconds,
                progress_cb=lambda p, i=idx: self.file_progress.emit(i, total, p),
                stop_event=self._stop,
                log_cb=lambda m: self.log.emit("  " + m.replace("\n", "\n  ")),
            )

            for png in (title_png, desc_png):
                if png:
                    try:
                        Path(png).unlink(missing_ok=True)
                    except OSError:
                        pass

            if rc == 0:
                success += 1
                self.file_progress.emit(idx, total, 1.0)
                self.file_finished.emit(str(out_path), True)
                self.log.emit(f"  ✓ {out_path.name}")
            elif rc == -1:
                self.log.emit("  ■ Đã dừng.")
                break
            else:
                self.file_finished.emit(str(out_path), False)
                self.log.emit(f"  ✗ Render lỗi (mã {rc})")

        self.finished_all.emit(success, total)
