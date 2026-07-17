"""Movie recap/review generator: cut a source movie into short clips, join
them, narrate a humorous Vietnamese summary (Gemini + edge-tts) and burn
voice-synced one-line subtitles.

Cut planning, AI scoring and the ffmpeg command builders are pure Python so
they can be unit-tested offline; only ``ReviewWorker`` at the bottom touches
Qt. AI is strictly optional — with no Gemini key the pipeline falls back to
periodic cutting and a generic narration script.
"""

from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QThread, Signal

from . import ffmpeg_runner, tts
from .image_gen import (ImageGenError, KeyPool, TEXT_API_URL, _extract_error)
from .slideshow import (Segment, SubSpec, audio_duration, fit_wav_to_slot,
                        render_sub_pngs, restore_punctuation,
                        transcode_to_wav)
from . import video_probe

MIN_SEG = 0.5           # segments shorter than this are dropped
MAX_AI_CANDIDATES = 48  # cap on frames sent for AI scoring
WORDS_PER_SEC = 2.8     # ~Vietnamese speaking rate, sizes the script

# Uniform per-segment encode so the concat demuxer can stream-copy.
_SEG_V = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-pix_fmt", "yuv420p"]
_SEG_A = ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]


@dataclass
class ReviewOptions:
    src: str
    out_dir: str
    keep: float = 3.0        # seconds kept per cut
    skip: float = 10.0       # seconds skipped between cuts
    mode: str = "even"       # "even" | "ai"
    voice: str = "vi-VN-HoaiMyNeural"
    language: str = "vi"     # narration language (script + subs)
    subtitles: bool = True
    sub_effect: str = "none"  # "none" | "fade"
    sub_size: int = 40
    sub_pos: str = "bottom"   # "bottom" | "middle" | "top"
    provider: str = "gemini"  # "gemini" | "openai" — script/scoring AI
    api_keys: List[str] = field(default_factory=list)
    use_whisper: bool = True
    audio_mode: str = "duck"  # "mute" | "duck" | "keep" — original audio
    duck_db: float = -15.0    # duck mode: original audio gain in dB


# --------------------------------------------------------------------------
# Cut planning (pure)
# --------------------------------------------------------------------------
def periodic_segments(duration: float, keep: float,
                      skip: float) -> List[tuple[float, float]]:
    """Even mode: keep K seconds, skip S seconds, repeat. ``[(start, dur)]``."""
    keep = max(MIN_SEG, float(keep))
    skip = max(0.0, float(skip))
    if duration <= 0:
        return []
    if duration <= keep:
        return [(0.0, duration)]
    segs: List[tuple[float, float]] = []
    t = 0.0
    while t < duration:
        d = min(keep, duration - t)
        if d >= MIN_SEG:
            segs.append((round(t, 3), round(d, 3)))
        t += keep + skip
    return segs


def candidate_segments(duration: float, keep: float,
                       max_candidates: int = MAX_AI_CANDIDATES,
                       ) -> List[tuple[float, float]]:
    """AI mode: K-second windows across the movie, at most ``max_candidates``."""
    keep = max(MIN_SEG, float(keep))
    if duration <= 0:
        return []
    if duration <= keep:
        return [(0.0, duration)]
    stride = max(keep, (duration - keep) / max(1, max_candidates - 1))
    segs: List[tuple[float, float]] = []
    t = 0.0
    while t < duration - MIN_SEG and len(segs) < max_candidates:
        segs.append((round(t, 3), round(min(keep, duration - t), 3)))
        t += stride
    return segs


def select_ai_segments(candidates: List[tuple[float, float]],
                       scores: List[float],
                       n_target: int) -> List[tuple[float, float]]:
    """Top-``n_target`` by score, non-overlapping, back in time order."""
    ranked = sorted(zip(candidates, scores), key=lambda x: -x[1])
    chosen: List[tuple[float, float]] = []
    for (start, dur), _score in ranked:
        if len(chosen) >= max(1, n_target):
            break
        if any(start < s + d and s < start + dur for s, d in chosen):
            continue
        chosen.append((start, dur))
    return sorted(chosen)


# --------------------------------------------------------------------------
# AI text helpers (urllib, no SDK — same pattern as image_gen).
# ``parts`` always use the Gemini shape ({"text": …} / {"inlineData": …});
# the OpenAI backend converts them on the fly.
# --------------------------------------------------------------------------
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_TEXT_MODEL = "gpt-4o-mini"


def _openai_text(parts: List[dict], api_key: str,
                 timeout: float = 90.0) -> str:
    """POST Gemini-shaped ``parts`` to the OpenAI chat API."""
    import urllib.error
    import urllib.request

    content: List[dict] = []
    for p in parts:
        if "text" in p:
            content.append({"type": "text", "text": p["text"]})
        elif "inlineData" in p:
            d = p["inlineData"]
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{d['mimeType']};base64,{d['data']}"}})
    body = json.dumps({
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": content}],
    }).encode("utf-8")
    req = urllib.request.Request(OPENAI_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            msg = json.loads(raw).get("error", {}).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            msg = ""
        raise ImageGenError(msg or f"HTTP {e.code}", e.code) from e
    except urllib.error.URLError as e:
        raise ImageGenError(f"Lỗi mạng: {e.reason}") from e
    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise ImageGenError("không có nội dung trong phản hồi") from None
    if not text:
        raise ImageGenError("không có nội dung trong phản hồi")
    return text


def _gemini_text(parts: List[dict], api_key: str,
                 timeout: float = 90.0) -> str:
    """POST ``parts`` to the Gemini text model, return the reply text."""
    import urllib.error
    import urllib.request

    body = json.dumps({"contents": [{"parts": parts}]}).encode("utf-8")
    req = urllib.request.Request(TEXT_API_URL, data=body, headers={
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        msg, status = _extract_error(raw)
        raise ImageGenError(msg or f"HTTP {e.code}", e.code, status) from e
    except urllib.error.URLError as e:
        raise ImageGenError(f"Lỗi mạng: {e.reason}") from e
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        raise ImageGenError("không có nội dung trong phản hồi") from None
    if not text:
        raise ImageGenError("không có nội dung trong phản hồi")
    return text


def _parse_json_reply(text: str):
    """Parse a JSON reply that may be wrapped in ``` fences or prose."""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None


def _call_with_pool(parts: List[dict], pool: KeyPool,
                    log: Callable[[str], None],
                    provider: str = "gemini") -> Optional[str]:
    """Run one AI text call, rotating keys on quota/auth errors."""
    call = _openai_text if provider == "openai" else _gemini_text
    name = "OpenAI" if provider == "openai" else "Gemini"
    while True:
        if pool.total == 0:
            return None
        try:
            return call(parts, pool.current())
        except ImageGenError as e:
            if e.is_key_error and pool.rotate():
                log(f"⚠ Key #{pool.index}: {e.short_reason} — đổi key…")
                continue
            log(f"⚠ {name} lỗi: {e}")
            return None
        except Exception as e:
            log(f"⚠ {name} lỗi: {e}")
            return None


def sample_frames(src: str, times: List[float],
                  max_w: int = 320) -> List[Optional[bytes]]:
    """One JPEG thumbnail per timestamp (single capture, sequential seeks)."""
    import cv2

    out: List[Optional[bytes]] = []
    cap = cv2.VideoCapture(src)
    try:
        for t in times:
            frame = None
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
                ok, frame = cap.read()
                if not ok:
                    frame = None
            if frame is None:
                frame = video_probe.extract_frame(src, t)
            if frame is None:
                out.append(None)
                continue
            h, w = frame.shape[:2]
            if w > max_w:
                frame = cv2.resize(frame, (max_w, max(1, int(h * max_w / w))))
            ok, buf = cv2.imencode(".jpg", frame,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            out.append(buf.tobytes() if ok else None)
    finally:
        cap.release()
    return out


_SCORE_PROMPT = (
    "Bạn là biên tập viên video recap phim. Dưới đây là {n} khung hình đánh "
    "số từ 1 đến {n} theo thứ tự thời gian trong phim. Chấm điểm 0-10 mức độ "
    "hấp dẫn của từng khung hình để đưa vào video recap (ưu tiên hành động, "
    "cảm xúc, cao trào, nhân vật rõ nét; trừ điểm cảnh tối, mờ, trống, màn "
    "hình đen, credit). Chỉ trả về JSON array thuần, ví dụ: "
    '[{{"i":1,"score":7}},{{"i":2,"score":3}}] — không giải thích gì thêm.')


def score_frames(thumbs: List[Optional[bytes]], pool: KeyPool,
                 log: Callable[[str], None],
                 provider: str = "gemini") -> Optional[List[float]]:
    """Score each thumbnail 0..10 via AI vision; None = total failure."""
    import base64

    scores: List[float] = [0.0] * len(thumbs)
    got_any = False
    batch = 16
    for lo in range(0, len(thumbs), batch):
        chunk = thumbs[lo:lo + batch]
        idx_map = [lo + j for j, b in enumerate(chunk) if b]
        imgs = [b for b in chunk if b]
        if not imgs:
            continue
        parts: List[dict] = [{"inlineData": {
            "mimeType": "image/jpeg",
            "data": base64.b64encode(b).decode("ascii"),
        }} for b in imgs]
        parts.append({"text": _SCORE_PROMPT.format(n=len(imgs))})
        reply = _call_with_pool(parts, pool, log, provider)
        data = _parse_json_reply(reply) if reply else None
        if data is None and reply is not None:
            parts[-1] = {"text": _SCORE_PROMPT.format(n=len(imgs))
                         + " Chỉ trả JSON."}
            reply = _call_with_pool(parts, pool, log, provider)
            data = _parse_json_reply(reply) if reply else None
        if not isinstance(data, list):
            continue
        for item in data:
            try:
                i = int(item.get("i", 0)) - 1
                if 0 <= i < len(idx_map):
                    scores[idx_map[i]] = float(item.get("score", 0))
                    got_any = True
            except (AttributeError, TypeError, ValueError):
                continue
    return scores if got_any else None


# --------------------------------------------------------------------------
# Narration script
# --------------------------------------------------------------------------
# Narration languages offered in the UI: (label, code). The code must match
# the first part of the edge-tts voice ids so the voice list can be filtered.
LANGUAGES = [
    ("Tiếng Việt", "vi"), ("English (Anh)", "en"), ("日本語 (Nhật)", "ja"),
    ("한국어 (Hàn)", "ko"), ("中文 (Trung)", "zh"), ("Français (Pháp)", "fr"),
    ("Español (Tây Ban Nha)", "es"), ("Deutsch (Đức)", "de"),
    ("Русский (Nga)", "ru"), ("ไทย (Thái)", "th"),
    ("Bahasa Indonesia", "id"), ("हिन्दी (Hindi)", "hi"),
]

_LANG_NAMES = {
    "vi": "tiếng Việt", "en": "tiếng Anh (English)",
    "ja": "tiếng Nhật (Japanese)", "ko": "tiếng Hàn (Korean)",
    "zh": "tiếng Trung (Chinese)", "fr": "tiếng Pháp (French)",
    "es": "tiếng Tây Ban Nha (Spanish)", "de": "tiếng Đức (German)",
    "ru": "tiếng Nga (Russian)", "th": "tiếng Thái (Thai)",
    "id": "tiếng Indonesia (Indonesian)", "hi": "tiếng Hindi",
}

_SCRIPT_PROMPT = (
    "Viết lời bình HÀI HƯỚC hoàn toàn bằng {lang} cho video recap phim "
    "'{title}', dài khoảng {secs} giây (~{words} từ, văn nói, dí dỏm, châm "
    "biếm nhẹ nhàng, gần gũi kiểu reviewer phim trên mạng). {source} "
    "Kể tóm tắt mạch phim, giữ bất ngờ đến gần cuối. KHÔNG dùng markdown, "
    "KHÔNG timestamps, KHÔNG tiêu đề, KHÔNG emoji — chỉ trả về đoạn văn "
    "thuần bằng {lang} để đọc liền mạch.")


def clean_movie_title(src: str) -> str:
    """A readable movie title from the file name."""
    name = Path(src).stem
    name = re.sub(r"[._\-\[\]()]+", " ", name)
    name = re.sub(r"\b(1080p|720p|2160p|4k|bluray|webrip|web|hdrip|x264|x265"
                  r"|h264|h265|hevc|aac|vietsub|thuyet minh|full ?hd)\b",
                  " ", name, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", name).strip() or "bộ phim này"


def gen_review_script(transcript: str, title: str, recap_secs: float,
                      pool: KeyPool, log: Callable[[str], None],
                      provider: str = "gemini",
                      language: str = "vi") -> Optional[str]:
    """Humorous narration in ``language`` via Gemini/OpenAI; None on failure."""
    secs = max(10, int(recap_secs))
    words = max(30, int(recap_secs * WORDS_PER_SEC))
    if transcript.strip():
        source = ("Dựa trên nội dung thoại của phim sau: "
                  f"«{transcript.strip()[:8000]}».")
    else:
        source = ("Không có thoại tham khảo — hãy bình luận chung chung, "
                  "hài hước về những cảnh phim hấp dẫn của bộ phim này.")
    lang = _LANG_NAMES.get(language, _LANG_NAMES["vi"])
    prompt = _SCRIPT_PROMPT.format(lang=lang, title=title, secs=secs,
                                   words=words, source=source)
    reply = _call_with_pool([{"text": prompt}], pool, log, provider)
    if not reply:
        return None
    text = re.sub(r"[*#`]+", "", reply).strip()
    return text or None


_FALLBACK_VI = (
    "Chào mừng cả nhà đến với màn review siêu tốc bộ phim {title}. "
    "Mình đã cắt sẵn những khoảnh khắc đáng chú ý nhất, nên cả nhà "
    "cứ ngồi yên, khỏi tua. Phim có đủ cả: tình tiết lúc thì căng "
    "như dây đàn, lúc thì lầy không đỡ nổi. Nhân vật chính thì khỏi "
    "phải bàn, đúng kiểu số hưởng nhưng đường đời lại lắm ổ gà. "
    "Xem xong mấy đoạn này mà thấy cuốn thì tìm bản đầy đủ xem ngay "
    "nhé, đảm bảo không phí thời gian đâu.")

_FALLBACK_EN = (
    "Welcome to the super fast review of {title}. I have already cut "
    "the most interesting moments, so sit back and skip nothing. This "
    "movie has it all: scenes so tense you forget to breathe, and "
    "moments so silly you cannot help laughing. The main character is "
    "something else, blessed by fate yet tripping over every bump on "
    "the road. If these highlights hook you, go watch the full movie, "
    "it is worth every minute.")


def fallback_script(title: str, recap_secs: float,
                    language: str = "vi") -> str:
    """Generic humorous narration used when no AI is available."""
    tmpl = _FALLBACK_VI if language == "vi" else _FALLBACK_EN
    base = tmpl.format(title=title)
    reps = max(1, int(recap_secs * WORDS_PER_SEC / 90))
    return " ".join([base] * reps) if reps > 1 else base


# --------------------------------------------------------------------------
# ffmpeg cut / concat / final render
# --------------------------------------------------------------------------
def _run_ffmpeg(args: List[str]) -> None:
    proc = subprocess.run(
        [ffmpeg_runner.get_ffmpeg(), "-y", "-hide_banner", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=ffmpeg_runner.NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg lỗi: {(proc.stderr or '')[-400:]}")


def cut_segment(src: str, start: float, dur: float, dst: str,
                w: int, h: int, fps: int, has_audio: bool) -> None:
    """Re-encode one keep-segment to uniform params (always with audio)."""
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    if not has_audio:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}",
                 "-i", "anullsrc=r=44100:cl=stereo"]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}")
    # apad=whole_dur + output -t pin both streams to exactly ``dur`` —
    # plain apad + -shortest overshoots (the padded audio runs ahead of
    # the muxer's shortest check).
    args += ["-vf", vf, "-map", "0:v:0",
             "-map", "0:a:0" if has_audio else "1:a:0",
             "-af", f"apad=whole_dur={dur:.3f}", "-t", f"{dur:.3f}",
             *_SEG_V, *_SEG_A, dst]
    _run_ffmpeg(args)
    if not Path(dst).exists():
        raise RuntimeError(f"Không cắt được đoạn tại {start:.1f}s")


def concat_segments(seg_paths: List[str], tmp_dir: str, out_path: str) -> None:
    """Concat demuxer with stream copy (segments share identical params)."""
    list_path = Path(tmp_dir) / "concat.txt"
    lines = []
    for p in seg_paths:
        safe = Path(p).as_posix().replace("'", r"'\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_path),
                 "-c", "copy", "-movflags", "+faststart", out_path])
    if not Path(out_path).exists():
        raise RuntimeError("Không ghép được các đoạn video.")


def build_review_graph(subs: List[SubSpec], audio_mode: str = "duck",
                       duck_db: float = -15.0) -> str:
    """Final graph: input 0 = recap video, 1 = narration WAV, 2.. = sub PNGs.
    Subtitle overlay chain mirrors ``slideshow.build_slideshow_graph``.

    ``audio_mode``: "mute" = narration only, "duck" = original audio lowered
    by ``duck_db`` dB under the narration, "keep" = original audio untouched
    with the narration mixed on top.
    """
    parts: List[str] = []
    cur = "0:v"
    for k, sub in enumerate(subs):
        idx = 2 + k
        chain = "format=rgba"
        if sub.effect == "fade":
            win = sub.end - sub.start
            fd = min(0.25, win / 4)
            chain += (f",fade=t=in:st=0:d={fd:.3f}:alpha=1"
                      f",fade=t=out:st={max(0.0, win - fd):.3f}"
                      f":d={fd:.3f}:alpha=1")
        chain += f",setpts=PTS-STARTPTS+{sub.start:.3f}/TB"
        parts.append(f"[{idx}:v]{chain}[s{k}]")
        parts.append(f"[{cur}][s{k}]overlay=0:{sub.y}:"
                     f"enable='between(t,{sub.start:.3f},{sub.end:.3f})'[o{k}]")
        cur = f"o{k}"
    parts.append(f"[{cur}]format=yuv420p[vout]")
    # apad every branch so no audio input EOFs before the muxer's -t cap —
    # ffmpeg 7.1 asserts (best_input >= 0) on early filter-input EOF.
    if audio_mode == "mute":
        parts.append("[1:a]apad[aout]")
    else:
        if audio_mode == "keep":
            parts.append("[0:a]apad[bg]")
        else:  # duck
            db = min(0.0, float(duck_db))
            parts.append(f"[0:a]volume={db:.1f}dB,apad[bg]")
        parts.append("[1:a]apad[na]")
        parts.append("[na][bg]amix=inputs=2:duration=first:normalize=0[aout]")
    return ";\n".join(parts)


def build_review_command(recap: str, narration_wav: str, subs: List[SubSpec],
                         graph_path: str, out_path: str, fps: int,
                         total_dur: float) -> List[str]:
    cmd: List[str] = [ffmpeg_runner.get_ffmpeg(), "-y", "-hide_banner",
                      "-i", recap, "-i", narration_wav]
    for sub in subs:
        cmd += ["-loop", "1", "-t", f"{sub.end - sub.start:.3f}",
                "-framerate", str(fps), "-i", sub.png_path]
    cmd += ["-filter_complex_script", graph_path,
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{total_dur:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", out_path]
    return cmd


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class ReviewWorker(QThread):
    """Runs the whole review pipeline off the UI thread."""

    status = Signal(str)
    log = Signal(str)
    progress = Signal(float)          # 0..1 overall
    finished_job = Signal(bool, str)  # ok, output path (or error text)

    def __init__(self, opts: ReviewOptions, parent=None):
        super().__init__(parent)
        self.opts = opts
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cv_review_")
        try:
            out = self._run(tmp)
            self.finished_job.emit(True, out)
        except _Stopped:
            self.finished_job.emit(False, "Đã dừng.")
        except Exception as e:
            self.finished_job.emit(False, str(e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- pipeline ----------------------------------------------------------
    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise _Stopped()

    def _run(self, tmp: str) -> str:
        o = self.opts

        self.status.emit("Đang đọc thông tin video…")
        info = video_probe.probe(o.src)
        if info.duration <= 0:
            raise RuntimeError("Không đọc được video nguồn.")
        w = max(2, info.width - info.width % 2)
        h = max(2, info.height - info.height % 2)
        fps = max(1, min(60, int(round(info.fps or 30))))
        self.progress.emit(0.02)
        self._check_stop()

        segs = self._plan_segments(info.duration)
        self.log.emit(f"Cắt {len(segs)} đoạn, tổng "
                      f"{sum(d for _, d in segs):.0f}s.")
        self.progress.emit(0.15)

        seg_paths: List[str] = []
        for i, (start, dur) in enumerate(segs):
            self._check_stop()
            self.status.emit(f"Đang cắt đoạn {i + 1}/{len(segs)}…")
            dst = str(Path(tmp) / f"seg_{i:03d}.mp4")
            cut_segment(o.src, start, dur, dst, w, h, fps, info.has_audio)
            seg_paths.append(dst)
            self.progress.emit(0.15 + 0.30 * (i + 1) / len(segs))

        self._check_stop()
        self.status.emit("Đang ghép các đoạn…")
        recap = str(Path(tmp) / "recap.mp4")
        concat_segments(seg_paths, tmp, recap)
        recap_dur = audio_duration(recap)
        self.progress.emit(0.50)

        transcript = self._transcribe(info)
        self.progress.emit(0.60)

        title = clean_movie_title(o.src)
        script = self._make_script(transcript, title, recap_dur)
        self.progress.emit(0.65)

        narration_wav = str(Path(tmp) / "narration.wav")
        words, speed = self._synth_narration(script, narration_wav, tmp,
                                             recap_dur)
        self.progress.emit(0.75)

        subs: List[SubSpec] = []
        if o.subtitles:
            self._check_stop()
            self.status.emit("Đang tạo phụ đề…")
            seg = Segment(index=0, ts_raw="", ts_seconds=0.0, text=script,
                          words=words, speed=speed, duration=recap_dur)
            subs = render_sub_pngs([seg], w, h, tmp, o.sub_effect,
                                   o.sub_size, o.sub_pos)
        self.progress.emit(0.78)

        self._check_stop()
        graph = build_review_graph(subs, o.audio_mode, o.duck_db)
        graph_path = str(Path(tmp) / "graph.txt")
        Path(graph_path).write_text(graph, encoding="utf-8")

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        Path(o.out_dir).mkdir(parents=True, exist_ok=True)
        out = str(Path(o.out_dir) / f"review_{stamp}.mp4")
        cmd = build_review_command(recap, narration_wav, subs, graph_path,
                                   out, fps, recap_dur)
        self.status.emit("Đang dựng video cuối…")
        code = ffmpeg_runner.run(
            cmd, recap_dur,
            progress_cb=lambda p: self.progress.emit(0.78 + 0.22 * p),
            stop_event=self._stop, log_cb=self.log.emit)
        if code == -1:
            raise _Stopped()
        if code != 0 or not Path(out).exists():
            raise RuntimeError("ffmpeg lỗi khi dựng video (xem log).")
        self.progress.emit(1.0)
        return out

    def _plan_segments(self, duration: float) -> List[tuple[float, float]]:
        o = self.opts
        even = periodic_segments(duration, o.keep, o.skip)
        if o.mode != "ai":
            return even
        pool = KeyPool(o.api_keys)
        if pool.total == 0:
            self.log.emit("⚠ Không có key AI (Gemini/OpenAI) — "
                          "chuyển sang cắt đều.")
            return even
        self.status.emit("AI đang chọn cảnh hay…")
        cands = candidate_segments(duration, o.keep)
        times = [s + d / 2 for s, d in cands]
        thumbs = sample_frames(o.src, times)
        self._check_stop()
        scores = score_frames(thumbs, pool, self.log.emit, o.provider)
        if scores is None:
            self.log.emit("⚠ AI không chấm điểm được — chuyển sang cắt đều.")
            return even
        chosen = select_ai_segments(cands, scores, len(even))
        if not chosen:
            return even
        self.log.emit(f"AI chọn {len(chosen)}/{len(cands)} cảnh.")
        return chosen

    def _transcribe(self, info: video_probe.VideoInfo) -> str:
        o = self.opts
        if not (o.use_whisper and info.has_audio):
            return ""
        try:
            from .transcribe import Transcriber, whisper_available
            if not whisper_available():
                return ""
            self.status.emit("Đang nghe thoại phim (Whisper)…")
            text = Transcriber("base").transcribe(o.src, language=None,
                                                  max_chars=9000)
            if text:
                self.log.emit(f"Transcript: {len(text)} ký tự.")
            return text
        except Exception as e:
            self.log.emit(f"⚠ Whisper lỗi ({e}) — bỏ qua transcript.")
            return ""

    def _make_script(self, transcript: str, title: str,
                     recap_dur: float) -> str:
        o = self.opts
        pool = KeyPool(o.api_keys)
        if pool.total:
            self.status.emit("Đang viết lời bình hài hước…")
            script = gen_review_script(transcript, title, recap_dur, pool,
                                       self.log.emit, o.provider, o.language)
            if script:
                return script
            self.log.emit("⚠ Không tạo được kịch bản AI — dùng lời bình mẫu.")
        else:
            self.log.emit("⚠ Không có key AI (Gemini/OpenAI) — "
                          "dùng lời bình mẫu.")
        return fallback_script(title, recap_dur, o.language)

    def _synth_narration(self, script: str, dst_wav: str, tmp: str,
                         video_dur: float) -> tuple[list, float]:
        """TTS the script and fit it to exactly ``video_dur`` seconds."""
        self._check_stop()
        self.status.emit("Đang đọc lời bình…")
        mp3 = str(Path(tmp) / "narration.mp3")
        raw = str(Path(tmp) / "narration_raw.wav")
        words = tts.synthesize(script, self.opts.voice, mp3,
                               stop_event=self._stop)
        transcode_to_wav(mp3, raw, 0.0)
        need = audio_duration(raw) / max(0.1, video_dur)
        if need > 1.15:
            # Re-read natively faster first — edge-tts keeps intonation
            # much better than atempo.
            rate = min(100, int(round((need - 1) * 100)))
            self.log.emit(f"Lời bình dài ×{need:.2f} — đọc nhanh +{rate}%.")
            words = tts.synthesize(script, self.opts.voice, mp3,
                                   rate=f"+{rate}%", stop_event=self._stop)
            transcode_to_wav(mp3, raw, 0.0)
        speed = fit_wav_to_slot(raw, dst_wav, video_dur)
        return restore_punctuation(words, script), speed


class _Stopped(Exception):
    pass
