"""Probe video metadata and extract a preview frame.

Primary path uses OpenCV (no ffprobe shipped with imageio-ffmpeg). Audio
presence and a reliable duration come from a lightweight ``ffmpeg -i`` parse.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from . import ffmpeg_runner

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*: Audio:")
_RES_RE = re.compile(r"Stream #\d+:\d+.*: Video:.* (\d{2,5})x(\d{2,5})")


@dataclass
class VideoInfo:
    path: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0      # seconds
    has_audio: bool = False


def _ffmpeg_info(path: str) -> tuple[Optional[float], bool, Optional[tuple[int, int]]]:
    """Parse ``ffmpeg -i`` stderr for duration, audio presence, resolution."""
    try:
        proc = subprocess.run(
            [ffmpeg_runner.get_ffmpeg(), "-hide_banner", "-i", path],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            creationflags=ffmpeg_runner.NO_WINDOW,
        )
    except Exception:
        return None, False, None
    err = proc.stderr or ""
    duration = None
    m = _DUR_RE.search(err)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duration = h * 3600 + mn * 60 + s
    has_audio = bool(_AUDIO_RE.search(err))
    res = None
    rm = _RES_RE.search(err)
    if rm:
        res = (int(rm.group(1)), int(rm.group(2)))
    return duration, has_audio, res


def probe(path: str) -> VideoInfo:
    info = VideoInfo(path=path)
    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        info.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        info.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        info.fps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        if info.fps > 0 and frames > 0:
            info.duration = frames / info.fps
    cap.release()

    dur, has_audio, res = _ffmpeg_info(path)
    info.has_audio = has_audio
    if dur:
        info.duration = dur
    if (info.width == 0 or info.height == 0) and res:
        info.width, info.height = res
    if info.fps <= 0:
        info.fps = 30.0
    return info


def extract_frame(path: str, t: float = 1.0) -> Optional[np.ndarray]:
    """Return a BGR frame at ~t seconds, or None. Falls back to ffmpeg."""
    cap = cv2.VideoCapture(path)
    frame = None
    if cap.isOpened():
        if t > 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
            if not ok:
                frame = None
    cap.release()
    if frame is not None:
        return frame

    # Fallback: let ffmpeg decode one frame to a temp PNG.
    try:
        tmp = Path(tempfile.gettempdir()) / "cv_preview_frame.png"
        subprocess.run(
            [ffmpeg_runner.get_ffmpeg(), "-y", "-ss", str(max(t, 0)),
             "-i", path, "-frames:v", "1", str(tmp)],
            capture_output=True, creationflags=ffmpeg_runner.NO_WINDOW,
        )
        if tmp.exists():
            return cv2.imread(str(tmp))
    except Exception:
        pass
    return None
