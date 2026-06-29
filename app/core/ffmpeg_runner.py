"""Locate the ffmpeg binary, build the filter graph, run with progress.

The binary is provided by ``imageio-ffmpeg`` (downloaded on first use) unless an
explicit path override is set.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Callable, List, Optional

from .layout_model import Layout

# Avoid a flashing console window for each subprocess on Windows.
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_FFMPEG_OVERRIDE: Optional[str] = None


def set_ffmpeg_path(path: Optional[str]) -> None:
    global _FFMPEG_OVERRIDE
    _FFMPEG_OVERRIDE = path or None


def get_ffmpeg() -> str:
    if _FFMPEG_OVERRIDE:
        return _FFMPEG_OVERRIDE
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _atempo_chain(speed: float) -> str:
    factors: List[float] = []
    s = float(speed)
    if s <= 0:
        s = 1.0
    while s > 2.0:
        factors.append(2.0)
        s /= 2.0
    while s < 0.5:
        factors.append(0.5)
        s /= 0.5
    factors.append(s)
    return ",".join(f"atempo={f:.6f}" for f in factors)


def _fg_filter(vw: int, vh: int, fit: str,
               cx: float = 0.5, cy: float = 0.5) -> tuple[str, bool]:
    """Return (scale_filter, centered) for the foreground video box.

    ``fill`` crops centered; ``crop`` crops at the (cx, cy) focal point chosen by
    dragging the video in the preview."""
    if fit in ("fill", "crop"):
        cx = min(1.0, max(0.0, cx))
        cy = min(1.0, max(0.0, cy))
        return (f"scale={vw}:{vh}:force_original_aspect_ratio=increase,"
                f"crop={vw}:{vh}:(iw-{vw})*{cx:.4f}:(ih-{vh})*{cy:.4f},"
                f"setsar=1", False)
    if fit == "free":
        return (f"scale={vw}:{vh},setsar=1", False)
    # default: fit (letterbox-free, centered inside the box)
    return (f"scale={vw}:{vh}:force_original_aspect_ratio=decrease,setsar=1", True)


def build_filter_complex(layout: Layout, fps: float, has_audio: bool,
                         title_idx: Optional[int], desc_idx: Optional[int]) -> str:
    cw, ch = layout.canvas_size()
    speed = max(0.1, float(layout.audio_speed))

    vx, vy, vw, vh = layout.video.to_pixels(cw, ch)
    parts: List[str] = []

    # --- foreground source (with optional speed change) ---
    setpts = f"setpts=PTS/{speed:.6f}"
    fg_scale, centered = _fg_filter(vw, vh, layout.video_fit,
                                    layout.video_crop_x, layout.video_crop_y)

    if layout.bg_mode == "blur":
        blur = max(1, int(layout.bg_blur))
        parts.append(f"[0:v]{setpts},split=2[vsrc][vbgsrc]")
        parts.append(f"[vbgsrc]scale={cw}:{ch}:force_original_aspect_ratio=increase,"
                     f"crop={cw}:{ch},boxblur={blur}:1,setsar=1[bg]")
        parts.append(f"[vsrc]{fg_scale}[fg]")
        base_shortest = ""
    else:
        bg_hex = (layout.bg_color or "#000000").lstrip("#")
        parts.append(f"color=c=0x{bg_hex}:s={cw}x{ch}:r={fps:.4f}[bg]")
        parts.append(f"[0:v]{setpts},{fg_scale}[fg]")
        base_shortest = ":shortest=1"

    if centered:
        ox = f"{vx}+({vw}-w)/2"
        oy = f"{vy}+({vh}-h)/2"
    else:
        ox, oy = str(vx), str(vy)
    parts.append(f"[bg][fg]overlay={ox}:{oy}{base_shortest}[base]")

    last = "base"
    if title_idx is not None:
        tx, ty, _, _ = layout.title.to_pixels(cw, ch)
        parts.append(f"[{last}][{title_idx}:v]overlay={tx}:{ty}[t]")
        last = "t"
    if desc_idx is not None:
        dx, dy, _, _ = layout.desc.to_pixels(cw, ch)
        parts.append(f"[{last}][{desc_idx}:v]overlay={dx}:{dy}[d]")
        last = "d"

    parts.append(f"[{last}]format=yuv420p[vout]")

    if has_audio:
        vol = max(0.0, float(layout.audio_volume))
        parts.append(f"[0:a]{_atempo_chain(speed)},volume={vol:.4f}[aout]")

    return ";".join(parts)


def build_command(input_path: str, output_path: str, layout: Layout, fps: float,
                  has_audio: bool, title_png: Optional[str], desc_png: Optional[str],
                  out_opts: dict) -> List[str]:
    cmd: List[str] = [get_ffmpeg(), "-y", "-hide_banner", "-i", input_path]
    idx = 1
    title_idx = desc_idx = None
    if title_png:
        cmd += ["-i", title_png]
        title_idx = idx
        idx += 1
    if desc_png:
        cmd += ["-i", desc_png]
        desc_idx = idx
        idx += 1

    graph = build_filter_complex(layout, fps, has_audio, title_idx, desc_idx)
    cmd += ["-filter_complex", graph, "-map", "[vout]"]
    cmd += ["-map", "[aout]"] if has_audio else ["-an"]

    codec = out_opts.get("codec", "libx264")
    cmd += ["-c:v", codec, "-preset", out_opts.get("preset", "veryfast")]
    if out_opts.get("use_crf", True):
        cmd += ["-crf", str(out_opts.get("crf", 20))]
    else:
        cmd += ["-b:v", f"{out_opts.get('bitrate', 10)}M"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-progress", "pipe:1", "-nostats", output_path]
    return cmd


def run(cmd: List[str], total_seconds: float,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_event: Optional[threading.Event] = None,
        log_cb: Optional[Callable[[str], None]] = None) -> int:
    """Run an ffmpeg command, reporting progress in [0,1]. Returns the exit
    code (or -1 if stopped)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        bufsize=1, creationflags=NO_WINDOW,
    )

    # Drain stderr in a thread so the pipe never blocks; keep the tail for errors.
    err_tail: List[str] = []

    def _drain_err():
        for ln in proc.stderr:  # type: ignore[arg-type]
            err_tail.append(ln)
            if len(err_tail) > 60:
                del err_tail[0]
    t = threading.Thread(target=_drain_err, daemon=True)
    t.start()

    try:
        for line in proc.stdout:  # type: ignore[arg-type]
            if stop_event is not None and stop_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return -1
            line = line.strip()
            if total_seconds > 0 and progress_cb is not None:
                us = None
                if line.startswith("out_time_us="):
                    val = line.split("=", 1)[1]
                    us = float(val) if val.lstrip("-").isdigit() else None
                elif line.startswith("out_time_ms="):
                    val = line.split("=", 1)[1]
                    us = float(val) if val.lstrip("-").isdigit() else None
                if us is not None:
                    progress_cb(max(0.0, min(0.999, (us / 1_000_000.0) / total_seconds)))
    finally:
        proc.wait()
        t.join(timeout=1)

    if proc.returncode != 0 and log_cb is not None:
        log_cb("".join(err_tail).strip()[-2000:])
    return proc.returncode
