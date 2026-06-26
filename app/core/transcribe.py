"""Optional speech-to-text via faster-whisper.

The import is lazy so the app runs fine without the package installed; the auto
description feature is simply disabled until it is present.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import ffmpeg_runner

MODEL_SIZES = ["tiny", "base", "small", "medium"]


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


class Transcriber:
    """Holds a loaded model and transcribes videos to plain text."""

    def __init__(self, model_size: str = "base", device: str = "cpu",
                 compute_type: str = "int8"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel
        self._model = WhisperModel(
            self.model_size, device=self.device, compute_type=self.compute_type
        )

    def _extract_audio(self, video_path: str) -> Optional[str]:
        tmp = Path(tempfile.gettempdir()) / (
            f"cv_audio_{abs(hash(video_path)) & 0xffffff}.wav")
        proc = subprocess.run(
            [ffmpeg_runner.get_ffmpeg(), "-y", "-hide_banner", "-i", video_path,
             "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", str(tmp)],
            capture_output=True, creationflags=ffmpeg_runner.NO_WINDOW,
        )
        return str(tmp) if tmp.exists() and proc.returncode == 0 else None

    def transcribe(self, video_path: str, language: Optional[str] = None,
                   max_chars: int = 600) -> str:
        """Return transcript text for ``video_path`` ("" on failure)."""
        self.load()
        wav = self._extract_audio(video_path)
        if not wav:
            return ""
        try:
            segments, _info = self._model.transcribe(  # type: ignore[union-attr]
                wav, language=(language or None), beam_size=1,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        finally:
            try:
                Path(wav).unlink(missing_ok=True)
            except OSError:
                pass
        if max_chars and len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + "…"
        return text
