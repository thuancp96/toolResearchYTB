"""Render a text region to a transparent RGBA PNG using Pillow.

Avoids ffmpeg ``drawtext`` escaping issues and gives proper Unicode (Vietnamese)
support with word-wrapping and alignment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from PIL import Image, ImageDraw, ImageFont

from .layout_model import TextStyle

# Common Windows fonts with good Vietnamese coverage.
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "C:/Windows/Fonts/calibri.ttf",
]


def default_font_path() -> str:
    for c in _FONT_CANDIDATES:
        if os.path.exists(c):
            return c
    return ""


def _load_font(style: TextStyle) -> ImageFont.FreeTypeFont:
    size = max(8, int(style.size_pt))
    path = style.font_path or default_font_path()
    if path and os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    s = (hex_color or "#000000").lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        r, g, b = 0, 0, 0
    return (r, g, b, alpha)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> List[str]:
    lines: List[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        cur = ""
        for w in words:
            trial = w if not cur else cur + " " + w
            if draw.textlength(trial, font=font) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def render_text_png(text: str, w: int, h: int, style: TextStyle, out_path: str,
                    pad_ratio: float = 0.06) -> bool:
    """Render ``text`` into a w×h transparent PNG at ``out_path``.

    Returns False (and writes nothing) when there is no text to draw.
    """
    if not text or not text.strip():
        return False
    w = max(2, int(w))
    h = max(2, int(h))
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if style.bg_enabled:
        draw.rectangle([0, 0, w - 1, h - 1], fill=_hex_to_rgba(style.bg_color, 255))

    font = _load_font(style)
    pad = int(min(w, h) * pad_ratio)
    max_w = max(1, w - 2 * pad)
    lines = _wrap(draw, text.strip(), font, max_w)

    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    spacing = int(line_h * 0.18)
    total_h = len(lines) * line_h + (len(lines) - 1) * spacing
    y = max(pad, (h - total_h) // 2)

    fill = _hex_to_rgba(style.color, 255)
    for line in lines:
        lw = draw.textlength(line, font=font)
        if style.align == "left":
            x = pad
        elif style.align == "right":
            x = w - pad - lw
        else:
            x = (w - lw) / 2
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h + spacing

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return True
