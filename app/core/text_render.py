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

# Malgun Gothic ships with Korean Windows and has full Hangul coverage.  It
# must be preferred over Arial/Segoe UI when Pillow renders Korean subtitles,
# otherwise unsupported glyphs appear as square boxes.
_KOREAN_FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/gulim.ttc",
    *_FONT_CANDIDATES,
]


def _has_hangul(text: str) -> bool:
    return any(
        "\u1100" <= ch <= "\u11ff" or "\u3130" <= ch <= "\u318f"
        or "\uac00" <= ch <= "\ud7a3"
        for ch in text
    )


def default_font_path(text: str = "") -> str:
    candidates = _KOREAN_FONT_CANDIDATES if _has_hangul(text) else _FONT_CANDIDATES
    for c in candidates:
        if os.path.exists(c):
            return c
    return ""


def _load_font(style: TextStyle, size: int = 0,
               text: str = "") -> ImageFont.FreeTypeFont:
    size = max(8, int(size or style.size_pt))
    path = style.font_path or default_font_path(text)
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


def _measure(draw, text: str, font, max_w: int,
             spacing_ratio: float = 0.18) -> tuple:
    """Return (lines, line_h, spacing, total_h) for ``text`` at ``font``."""
    lines = _wrap(draw, text, font, max_w)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    spacing = int(line_h * spacing_ratio)
    total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing
    return lines, line_h, spacing, total_h


def _truncate(draw, lines: List[str], font, max_w: int,
              max_lines: int) -> List[str]:
    """Clamp ``lines`` to ``max_lines`` lines, appending … to the last one."""
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    last = kept[-1]
    ell = "…"
    while last and draw.textlength(last + ell, font=font) > max_w:
        last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
    kept[-1] = (last.rstrip() + ell) if last else ell
    return kept


def _fit(draw, text: str, style: TextStyle, max_w: int, max_h: int,
         max_lines: int) -> tuple:
    """Shrink the font until ``text`` fits the box (width, height and, if set,
    ``max_lines``). Returns (font, lines, line_h, spacing). Falls back to the
    smallest size and truncates when it still cannot fit."""
    base = max(8, int(style.size_pt))
    min_size = max(8, int(base * 0.35))
    for size in range(base, min_size - 1, -1):
        font = _load_font(style, size, text)
        lines, line_h, spacing, total_h = _measure(draw, text, font, max_w)
        line_ok = max_lines <= 0 or len(lines) <= max_lines
        if line_ok and total_h <= max_h:
            return font, lines, line_h, spacing
    font = _load_font(style, min_size, text)
    lines, line_h, spacing, _ = _measure(draw, text, font, max_w)
    if max_lines > 0:
        lines = _truncate(draw, lines, font, max_w, max_lines)
    return font, lines, line_h, spacing


def render_text_png(text: str, w: int, h: int, style: TextStyle, out_path: str,
                    pad_ratio: float = 0.06, force: bool = False,
                    auto_fit: bool = False, max_lines: int = 0) -> bool:
    """Render ``text`` into a w×h transparent PNG at ``out_path``.

    Returns False (and writes nothing) when there is no text to draw, unless
    ``force`` is set — then an empty frame (just the background box, if enabled)
    is still written so the description box appears without any text.

    ``auto_fit`` shrinks the font so the text fits the box; ``max_lines`` (when
    > 0) clamps the result to that many lines, truncating with … if needed.
    """
    has_text = bool(text and text.strip())
    if not has_text and not force:
        return False
    w = max(2, int(w))
    h = max(2, int(h))
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    tight = style.bg_enabled and getattr(style, "bg_style", "box") == "tight"
    if style.bg_enabled and not tight:
        draw.rectangle([0, 0, w - 1, h - 1], fill=_hex_to_rgba(style.bg_color, 255))

    if not has_text:
        # Frame-only: write the (possibly transparent) box and stop.
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)
        return True

    pad = int(min(w, h) * pad_ratio)
    max_w = max(1, w - 2 * pad)
    max_h = max(1, h - 2 * pad)

    if auto_fit:
        font, lines, line_h, spacing = _fit(draw, text.strip(), style,
                                            max_w, max_h, max_lines)
    else:
        font = _load_font(style, text=text.strip())
        lines, line_h, spacing, _ = _measure(draw, text.strip(), font, max_w)
        if max_lines > 0:
            lines = _truncate(draw, lines, font, max_w, max_lines)

    total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing
    y = max(pad, (h - total_h) // 2)

    placed = []
    for line in lines:
        lw = draw.textlength(line, font=font)
        if style.align == "left":
            x = pad
        elif style.align == "right":
            x = w - pad - lw
        else:
            x = (w - lw) / 2
        placed.append((line, x, y, lw))
        y += line_h + spacing

    if tight:
        # Rounded pill hugging each line (mirrors the preview).
        pad_x = line_h * 0.35
        pad_y = line_h * 0.10
        radius = max(1, int(line_h * 0.30))
        bg = _hex_to_rgba(style.bg_color, 255)
        for line, x, ly, lw in placed:
            if not line.strip():
                continue
            draw.rounded_rectangle(
                [x - pad_x, ly - pad_y, x + lw + pad_x, ly + line_h + pad_y],
                radius=radius, fill=bg)

    fill = _hex_to_rgba(style.color, 255)
    for line, x, ly, lw in placed:
        draw.text((x, ly), line, font=font, fill=fill)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return True
