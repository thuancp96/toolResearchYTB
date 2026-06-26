"""Data model for the video layout.

All regions are stored in *normalized* coordinates (0..1) relative to the output
canvas, so the same layout maps correctly to any aspect / resolution and the
preview always matches the rendered output.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Tuple

# Standard output canvas sizes (width, height).
CANVAS_SIZES = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
}

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv"}


def _even(v: int) -> int:
    v = int(round(v))
    return v - (v % 2)


@dataclass
class Region:
    """A rectangle in normalized coords; (nx, ny) = top-left, (nw, nh) = size."""

    nx: float
    ny: float
    nw: float
    nh: float

    def clamp(self) -> "Region":
        self.nw = max(0.02, min(1.0, self.nw))
        self.nh = max(0.02, min(1.0, self.nh))
        self.nx = max(0.0, min(1.0 - self.nw, self.nx))
        self.ny = max(0.0, min(1.0 - self.nh, self.ny))
        return self

    def to_pixels(self, cw: int, ch: int, even: bool = True) -> Tuple[int, int, int, int]:
        """Return (x, y, w, h) in pixels. When ``even`` (needed for libx264
        scale targets) width/height are forced to even values >= 2."""
        x = int(round(self.nx * cw))
        y = int(round(self.ny * ch))
        w = int(round(self.nw * cw))
        h = int(round(self.nh * ch))
        if even:
            x, y, w, h = _even(x), _even(y), _even(w), _even(h)
            w = max(w, 2)
            h = max(h, 2)
        return x, y, w, h


@dataclass
class TextStyle:
    font_path: str = ""
    size_pt: int = 40          # font height in pixels at canvas resolution
    color: str = "#000000"
    bg_color: str = "#ffffff"
    bg_enabled: bool = True
    align: str = "center"      # left | center | right


@dataclass
class Layout:
    aspect: str = "9:16"

    title: Region = field(default_factory=lambda: Region(0.08, 0.04, 0.84, 0.09))
    title_style: TextStyle = field(default_factory=lambda: TextStyle(size_pt=46))
    title_text: str = "VIDEO TITLE"
    title_source: str = "filename"   # whisper | filename | manual

    video: Region = field(default_factory=lambda: Region(0.05, 0.28, 0.90, 0.40))
    video_fit: str = "fit"           # fit | fill | free

    desc: Region = field(default_factory=lambda: Region(0.08, 0.78, 0.84, 0.17))
    desc_style: TextStyle = field(default_factory=lambda: TextStyle(size_pt=34))
    desc_text: str = "Description..."
    desc_source: str = "whisper"

    bg_mode: str = "blur"            # blur | color
    bg_blur: int = 20
    bg_color: str = "#000000"

    audio_speed: float = 1.0         # affects both video & audio (keeps sync)
    audio_volume: float = 1.0

    def canvas_size(self) -> Tuple[int, int]:
        return CANVAS_SIZES.get(self.aspect, CANVAS_SIZES["9:16"])

    # ---- serialization -------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Layout":
        lay = Layout()
        if not d:
            return lay
        for key in ("aspect", "title_text", "title_source", "video_fit",
                    "desc_text", "desc_source", "bg_mode", "bg_blur",
                    "bg_color", "audio_speed", "audio_volume"):
            if key in d:
                setattr(lay, key, d[key])
        for rkey in ("title", "video", "desc"):
            if rkey in d and isinstance(d[rkey], dict):
                setattr(lay, rkey, Region(**d[rkey]))
        for skey in ("title_style", "desc_style"):
            if skey in d and isinstance(d[skey], dict):
                setattr(lay, skey, TextStyle(**{
                    k: v for k, v in d[skey].items()
                    if k in TextStyle.__dataclass_fields__
                }))
        return lay
