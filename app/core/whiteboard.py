"""Small adapter around the vendored stream whiteboard renderer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDERER = ROOT / "vendor" / "whiteboard" / "render_stream_whiteboard.py"
HAND = ROOT / "vendor" / "whiteboard" / "drawing-hand.png"

def render(image: str, output: str, duration: float, stop_event=None) -> None:
    from PIL import Image
    with Image.open(image) as im:
        w, h = im.size
    annotation = Path(output).with_suffix(".annotation.json")
    data = {"sceneId": Path(image).stem, "canvas": {"width": w, "height": h},
            "storyBasis": "Tự động tạo từ ảnh và voice script.",
            "sceneDurationMs": max(1000, round(duration * 1000)),
            "elements": [{"id": "full_scene", "label": "Toàn cảnh", "sequence": 1,
              "narrativeRole": "Nội dung cảnh", "subtitle": "",
              "type": "scene", "region": {"x": 0, "y": 0, "width": w, "height": h},
              "reveal": {"direction": "top_to_bottom", "startMs": 0,
                          "durationMs": max(500, round(duration * 1000 - 500)),
                          "maskPaddingPx": 22, "protectedRegions": []},
              "handPath": {"start": [w // 2, 0], "end": [w // 2, h],
                           "easing": "easeInOut"}}]}
    annotation.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    cmd = [sys.executable, str(RENDERER), image, str(annotation), output, str(HAND),
           "--ink-path", "grid", "--color-fill", "contour-wipe"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    while proc.poll() is None:
        if stop_event is not None and stop_event.is_set():
            proc.terminate(); proc.wait(timeout=5); raise RuntimeError("Đã dừng.")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr.read() if proc.stderr else "")[-2000:])
