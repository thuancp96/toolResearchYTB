from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.text_render import default_font_path


def test_default_font_path_prefers_japanese_font(monkeypatch):
    existing = {
        "C:/Windows/Fonts/msgothic.ttc": True,
        "C:/Windows/Fonts/meiryo.ttc": True,
        "C:/Windows/Fonts/arial.ttf": False,
        "C:/Windows/Fonts/segoeui.ttf": False,
    }

    monkeypatch.setattr("app.core.text_render.os.path.exists", lambda p: existing.get(p, False))

    assert default_font_path("日本語字幕") == "C:/Windows/Fonts/msgothic.ttc"
