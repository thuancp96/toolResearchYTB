"""Turn a voice script into an mp3 or a slideshow video with the AI images.

Script format (one segment per ``[mm:ss]`` block)::

    [00:00]
    Voice: "Have you ever wondered why we yawn?"

    [00:05]
    Voice: "Yawning is something every human does."

The actual TTS audio duration of each segment drives both the image display
time and the subtitle window, so subs are always in sync with the voice; the
script timestamps only order the segments and match them to image files.

Everything here is Qt-free except the ``VoiceVideoWorker`` at the bottom.
"""

from __future__ import annotations

import datetime
import random
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QThread, Signal

from . import ffmpeg_runner, tts
from .layout_model import TextStyle
from .text_render import render_text_png

# "[mm:ss]" or "[hh:mm:ss]", optionally a range "[00:06 - 00:12]", optionally
# followed by a separator and the voice text on the same line.
TS_LINE_RE = re.compile(
    r"^\s*\[(\d{1,2}:\d{2}(?::\d{2})?)"
    r"(?:\s*[-–—>~]+\s*\d{1,2}:\d{2}(?::\d{2})?)?\]"
    r"\s*[-–—:.]*\s*(.*)$")
# Label before the spoken text. Tolerant: "Voice:", "voice", "Voice 2 -",
# bare "voice" right before a quote or the text itself.
VOICE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?voice(?:\s*\d+)?\s*"
    r"(?:[:\-–—]\s*|(?=[\"'“”‘’])|\s+(?=\S))(.*)$",
    re.IGNORECASE)
# Stray timestamps inside the text ("… [00:06] …") must never reach the TTS.
INLINE_TS_RE = re.compile(
    r"\[\d{1,2}:\d{2}(?::\d{2})?(?:\s*[-–—>~]+\s*\d{1,2}:\d{2}(?::\d{2})?)?\]")
# Timestamp embedded in generated image filenames: "001_[00-15] title.png"
FILE_TS_RE = re.compile(r"\[(\d{1,2})-(\d{2})(?:-(\d{2}))?\]")
_QUOTES = "\"'“”‘’"

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# ffmpeg xfade transition names offered in the UI ("random" picks per cut).
TRANSITIONS = [
    "fade", "wipeleft", "wiperight", "slideup", "slidedown", "dissolve",
    "circleopen", "circleclose", "radial", "smoothleft", "smoothright",
    "hlslice",
]

DEFAULT_SILENCE = 3.0  # seconds for a segment with no voice text and no gap


@dataclass
class Segment:
    index: int
    ts_raw: str            # "00:05" ("" when the block had no timestamp)
    ts_seconds: float
    text: str              # voice text ("" allowed -> silent hold)
    audio_path: str = ""   # per-segment WAV (filled by the worker)
    duration: float = 0.0  # exact audio duration incl. the pause pad
    image_path: str = ""   # matched image (video mode)
    words: list = None     # [(start_s, end_s, word)] from TTS, pre-speedup
    speed: float = 1.0     # time-compression applied to the audio (atempo)


def ts_to_seconds(ts: str) -> float:
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return float(parts[0]) if parts else 0.0


def _clean_voice_text(lines: List[str]) -> str:
    """Join a block's lines, dropping 'Voice:' prefixes and wrapping quotes."""
    cleaned = []
    for ln in lines:
        m = VOICE_RE.match(ln)
        if m:
            ln = m.group(1)
        ln = INLINE_TS_RE.sub(" ", ln).strip()
        if ln:
            cleaned.append(ln)
    text = re.sub(r"\s{2,}", " ", " ".join(cleaned)).strip()
    return text.strip(_QUOTES).strip()


def parse_script(text: str) -> List[Segment]:
    """Parse the '[mm:ss] / Voice: "…"' script into ordered segments."""
    segments: List[Segment] = []
    cur_ts = ""
    cur_lines: List[str] = []
    started = False

    def flush() -> None:
        nonlocal cur_lines
        if not started:
            return
        content = _clean_voice_text(cur_lines)
        if not content and not cur_ts:
            cur_lines = []
            return
        segments.append(Segment(
            index=len(segments), ts_raw=cur_ts,
            ts_seconds=ts_to_seconds(cur_ts) if cur_ts else -1.0,
            text=content))
        cur_lines = []

    for raw in (text or "").splitlines():
        m = TS_LINE_RE.match(raw)
        if m:
            flush()
            started = True
            cur_ts = m.group(1)
            cur_lines = [m.group(2)] if m.group(2).strip() else []
        elif raw.strip():
            started = True
            cur_lines.append(raw)
    flush()
    return segments


# --------------------------------------------------------------------------
# Image matching
# --------------------------------------------------------------------------
def list_images(out_dir: str) -> List[str]:
    try:
        files = sorted(p for p in Path(out_dir).iterdir()
                       if p.suffix.lower() in IMG_EXTS)
    except OSError:
        return []
    return [str(p) for p in files]


def _file_ts_seconds(path: str) -> Optional[float]:
    m = FILE_TS_RE.search(Path(path).name)
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is not None:
        return int(a) * 3600 + int(b) * 60 + int(c)
    return int(a) * 60 + int(b)


def match_images(segments: List[Segment], images: List[str],
                 log: Callable[[str], None]) -> None:
    """Assign one image per segment: by filename timestamp first, then by
    order. Missing images reuse the last one; extras are ignored."""
    by_ts: dict[float, str] = {}
    for img in images:
        sec = _file_ts_seconds(img)
        if sec is not None and sec not in by_ts:
            by_ts[sec] = img

    used: set[str] = set()
    for seg in segments:
        if seg.ts_seconds >= 0 and seg.ts_seconds in by_ts:
            img = by_ts[seg.ts_seconds]
            if img not in used:
                seg.image_path = img
                used.add(img)

    remaining = [img for img in images if img not in used]
    for seg in segments:
        if seg.image_path:
            continue
        if remaining:
            seg.image_path = remaining.pop(0)
            used.add(seg.image_path)
        elif used or images:
            last = seg.index and segments[seg.index - 1].image_path
            seg.image_path = last or images[-1]
            log(f"⚠ Thiếu ảnh cho đoạn {seg.index + 1} "
                f"[{seg.ts_raw or '?'}] — dùng lại ảnh trước đó.")
    extra = len(images) - len(used)
    if extra > 0:
        log(f"⚠ Có {extra} ảnh không khớp đoạn nào — bỏ qua.")


# --------------------------------------------------------------------------
# Audio helpers (all shell out to the bundled ffmpeg)
# --------------------------------------------------------------------------
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def _run_ffmpeg(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ffmpeg_runner.get_ffmpeg(), "-y", "-hide_banner", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=ffmpeg_runner.NO_WINDOW)


def transcode_to_wav(src: str, dst: str, pause: float) -> None:
    """Normalize to 44.1 kHz stereo PCM and append ``pause`` s of silence
    (so probed durations are exact and already include the gap)."""
    af = f"apad=pad_dur={max(0.0, pause):.3f}"
    proc = _run_ffmpeg(["-i", src, "-af", af, "-ar", "44100", "-ac", "2",
                        "-c:a", "pcm_s16le", dst])
    if proc.returncode != 0 or not Path(dst).exists():
        raise RuntimeError(f"Không chuyển được audio: {proc.stderr[-400:]}")


def make_silence_wav(duration: float, dst: str) -> None:
    proc = _run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                        "-t", f"{max(0.1, duration):.3f}",
                        "-c:a", "pcm_s16le", dst])
    if proc.returncode != 0 or not Path(dst).exists():
        raise RuntimeError(f"Không tạo được khoảng lặng: {proc.stderr[-400:]}")


def compute_slots(segments: List[Segment]) -> List[Optional[float]]:
    """Per-segment target duration from the script timestamps: the gap to the
    next mark. The last segment reuses the previous gap (no next mark).
    ``None`` where the timestamps are missing or not increasing."""
    n = len(segments)
    slots: List[Optional[float]] = [None] * n
    for i in range(n - 1):
        a, b = segments[i].ts_seconds, segments[i + 1].ts_seconds
        if a >= 0 and b > a:
            slots[i] = b - a
    if n >= 2 and slots[n - 1] is None and slots[n - 2] is not None \
            and segments[n - 1].ts_seconds >= 0:
        slots[n - 1] = slots[n - 2]
    return slots


def fit_wav_to_slot(src_wav: str, dst: str, slot: float) -> float:
    """Write ``dst`` lasting exactly ``slot`` seconds: speech longer than the
    slot is sped up (pitch-preserving atempo), shorter speech is padded with
    trailing silence. Returns the speed factor applied (1.0 = unchanged)."""
    d = audio_duration(src_wav)
    factor = 1.0
    af = f"apad=whole_dur={slot:.3f}"
    if d > slot + 0.02:
        factor = d / slot
        af = f"{ffmpeg_runner._atempo_chain(factor)},{af}"
    proc = _run_ffmpeg(["-i", src_wav, "-af", af, "-t", f"{slot:.3f}",
                        "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", dst])
    if proc.returncode != 0 or not Path(dst).exists():
        raise RuntimeError(f"Không khớp được audio vào mốc: {proc.stderr[-400:]}")
    return factor


def audio_duration(path: str) -> float:
    """Duration in seconds parsed from ``ffmpeg -i`` (exact for PCM WAV)."""
    proc = subprocess.run(
        [ffmpeg_runner.get_ffmpeg(), "-hide_banner", "-i", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=ffmpeg_runner.NO_WINDOW)
    m = _DUR_RE.search(proc.stderr or "")
    if not m:
        raise RuntimeError(f"Không đọc được thời lượng audio: {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def encode_voice_mp3(wavs: List[str], out_mp3: str,
                     log: Callable[[str], None]) -> None:
    args: List[str] = []
    for w in wavs:
        args += ["-i", w]
    graph = "".join(f"[{i}:a]" for i in range(len(wavs)))
    graph += f"concat=n={len(wavs)}:v=0:a=1[a]"
    proc = _run_ffmpeg([*args, "-filter_complex", graph, "-map", "[a]",
                        "-c:a", "libmp3lame", "-q:a", "3", out_mp3])
    if proc.returncode != 0 or not Path(out_mp3).exists():
        log("libmp3lame lỗi — thử xuất AAC (.m4a)…")
        alt = str(Path(out_mp3).with_suffix(".m4a"))
        proc = _run_ffmpeg([*args, "-filter_complex", graph, "-map", "[a]",
                            "-c:a", "aac", "-b:a", "160k", alt])
        if proc.returncode != 0 or not Path(alt).exists():
            raise RuntimeError(f"Không ghép được voice: {proc.stderr[-400:]}")


# --------------------------------------------------------------------------
# Slideshow graph
# --------------------------------------------------------------------------
@dataclass
class SubSpec:
    png_path: str
    start: float
    end: float
    effect: str = "none"   # "none" | "fade"
    y: int = 0


def resolve_transitions(choice: str, count: int) -> List[str]:
    """One transition name per cut; ``random`` varies per cut."""
    if choice == "random":
        return [random.choice(TRANSITIONS) for _ in range(count)]
    name = choice if choice in TRANSITIONS else "fade"
    return [name] * count


def build_slideshow_graph(segments: List[Segment], w: int, h: int, fps: int,
                          transitions: List[str], trans_dur: float,
                          subs: List[SubSpec]) -> str:
    """Filter graph: n scaled image branches -> xfade chain -> sub overlays
    -> [vout]; n segment WAVs concatenated -> [aout].

    Input order: 0..n-1 images, n..2n-1 WAVs, 2n.. sub PNGs. Each image
    clip i < n-1 is fed ``dur_i + trans_dur`` long so the xfade overlap is
    absorbed and the video length equals the audio length exactly; the
    xfade offsets are then simply the cumulative audio durations.
    """
    n = len(segments)
    parts: List[str] = []
    for i in range(n):
        parts.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={fps},settb=AVTB,"
            f"format=yuv420p[v{i}]")

    cur = "v0"
    cum = 0.0
    for i in range(1, n):
        cum += segments[i - 1].duration
        parts.append(
            f"[{cur}][v{i}]xfade=transition={transitions[i - 1]}:"
            f"duration={trans_dur:.3f}:offset={cum:.3f}[x{i}]")
        cur = f"x{i}"

    for k, sub in enumerate(subs):
        idx = 2 * n + k
        chain = "format=rgba"
        if sub.effect == "fade":
            win = sub.end - sub.start
            fd = min(0.25, win / 4)
            chain += (f",fade=t=in:st=0:d={fd:.3f}:alpha=1"
                      f",fade=t=out:st={max(0.0, win - fd):.3f}"
                      f":d={fd:.3f}:alpha=1")
        chain += f",setpts=PTS-STARTPTS+{sub.start:.3f}/TB"
        parts.append(f"[{idx}:v]{chain}[s{k}]")
        parts.append(
            f"[{cur}][s{k}]overlay=0:{sub.y}:"
            f"enable='between(t,{sub.start:.3f},{sub.end:.3f})'[o{k}]")
        cur = f"o{k}"

    parts.append(f"[{cur}]format=yuv420p[vout]")
    parts.append("".join(f"[{n + i}:a]" for i in range(n))
                 + f"concat=n={n}:v=0:a=1[aout]")
    return ";\n".join(parts)


def build_slideshow_command(segments: List[Segment], subs: List[SubSpec],
                            graph_path: str, out_path: str, fps: int,
                            trans_dur: float) -> List[str]:
    n = len(segments)
    cmd: List[str] = [ffmpeg_runner.get_ffmpeg(), "-y", "-hide_banner"]
    for i, seg in enumerate(segments):
        clip = seg.duration + (trans_dur if i < n - 1 and n > 1 else 0.0)
        cmd += ["-loop", "1", "-t", f"{clip:.3f}", "-framerate", str(fps),
                "-i", seg.image_path]
    for seg in segments:
        cmd += ["-i", seg.audio_path]
    for sub in subs:
        cmd += ["-loop", "1", "-t", f"{sub.end - sub.start:.3f}",
                "-framerate", str(fps), "-i", sub.png_path]
    cmd += ["-filter_complex_script", graph_path,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", out_path]
    return cmd


SUB_MAX_CHARS = 42  # one comfortable line at the default subtitle size

_PUNCT_STRIP = "".join(set(".,!?…;:\"'“”‘’()[]"))


def restore_punctuation(words: list, text: str) -> list:
    """edge-tts word boundaries carry the bare words without punctuation;
    map them back to the original script tokens so subs keep the ',.?!'
    and sentence-break detection works."""
    tokens = text.split()
    out = []
    j = 0
    for s, e, w in words:
        if j < len(tokens) and tokens[j].strip(_PUNCT_STRIP).lower() == w.lower():
            out.append((s, e, tokens[j]))
        else:
            out.append((s, e, w))
        j += 1
    return out


def chunk_words(words: list, max_chars: int = SUB_MAX_CHARS) -> list:
    """Group TTS word boundaries into short one-line phrases.

    Returns ``[(start_s, end_s, text), …]``. Breaks when the line would grow
    past ``max_chars`` and after sentence-ending punctuation.
    """
    chunks: list = []
    cur: list = []

    def flush() -> None:
        if cur:
            chunks.append((cur[0][0], cur[-1][1],
                           " ".join(x[2] for x in cur)))
            cur.clear()

    for wb in words:
        word = wb[2].strip()
        if not word:
            continue
        if cur:
            ends_sentence = cur[-1][2][-1] in ".!?…"
            too_long = (len(" ".join(x[2] for x in cur)) + 1 + len(word)
                        > max_chars)
            if ends_sentence or too_long:
                flush()
        cur.append((wb[0], wb[1], word))
    flush()
    return chunks


def chunk_text_fallback(text: str, duration: float,
                        max_chars: int = SUB_MAX_CHARS) -> list:
    """No word boundaries available: split into one-line phrases and spread
    them over ``duration`` proportionally to their length."""
    lines: List[str] = []
    cur = ""
    for word in text.split():
        cand = f"{cur} {word}".strip()
        if cur and len(cand) > max_chars:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        lines.append(cur)
    total_chars = sum(len(l) for l in lines) or 1
    chunks = []
    t = 0.0
    for line in lines:
        d = duration * len(line) / total_chars
        chunks.append((t, t + d, line))
        t += d
    return chunks


def render_sub_pngs(segments: List[Segment], w: int, h: int, tmp_dir: str,
                    effect: str, size_pt: int, position: str) -> List[SubSpec]:
    """Karaoke-style subs: one short line at a time, changing in step with
    the voice (timed from the TTS word boundaries)."""
    band_h = max(50, int(h * 0.14))
    margin = int(h * 0.05)
    if position == "top":
        y = margin
    elif position == "middle":
        y = (h - band_h) // 2
    else:  # bottom
        y = h - band_h - margin
    style = TextStyle(size_pt=size_pt, color="#ffffff", bg_color="#000000",
                      bg_enabled=True, bg_style="tight", align="center")

    subs: List[SubSpec] = []
    base = 0.0
    k = 0
    for seg in segments:
        if seg.text:
            if seg.words:
                speed = max(0.01, seg.speed)
                chunks = [(s / speed, e / speed, txt)
                          for s, e, txt in chunk_words(seg.words)]
            else:
                chunks = chunk_text_fallback(seg.text, seg.duration)
            # Continuous lyric feel: each line holds until the next starts.
            timed = [[min(s, seg.duration), min(e, seg.duration), txt]
                     for s, e, txt in chunks]
            for j in range(len(timed) - 1):
                timed[j][1] = timed[j + 1][0]
            if timed:
                timed[-1][1] = min(timed[-1][1] + 0.3, seg.duration)
            for s, e, txt in timed:
                if e - s < 0.05:
                    continue
                png = str(Path(tmp_dir) / f"sub_{k:04d}.png")
                k += 1
                if render_text_png(txt, w, band_h, style, png,
                                   auto_fit=True, max_lines=1):
                    subs.append(SubSpec(png_path=png,
                                        start=round(base + s, 3),
                                        end=round(base + e, 3),
                                        effect=effect, y=y))
        base += seg.duration
    return subs


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class VoiceVideoWorker(QThread):
    """Runs the whole voice/video pipeline off the UI thread."""

    status = Signal(str)
    log = Signal(str)
    progress = Signal(float)          # 0..1 overall
    finished_job = Signal(bool, str)  # ok, output path (or error text)

    def __init__(self, mode: str, script_text: str, out_dir: str, voice: str,
                 width: int, height: int, transition: str = "fade",
                 trans_dur: float = 0.5, subtitles: bool = False,
                 sub_effect: str = "none", sub_size: int = 40,
                 sub_pos: str = "bottom", pause: float = 0.3,
                 fps: int = 30, timing: str = "timestamps", parent=None):
        super().__init__(parent)
        self.mode = mode
        self.timing = timing  # "timestamps" = fit each mark gap | "auto"
        self.script_text = script_text
        self.out_dir = out_dir
        self.voice = voice
        self.width = width
        self.height = height
        self.transition = transition
        self.trans_dur = trans_dur
        self.subtitles = subtitles
        self.sub_effect = sub_effect
        self.sub_size = sub_size
        self.sub_pos = sub_pos
        self.pause = pause
        self.fps = fps
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cv_voicevideo_")
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
        segments = parse_script(self.script_text)
        if not segments:
            raise RuntimeError("Script trống hoặc sai định dạng "
                               "([00:00] + Voice: \"…\").")
        self._synth_all(segments, tmp)

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)
        if self.mode == "voice":
            out = str(Path(self.out_dir) / f"voice_{stamp}.mp3")
            self.status.emit("Đang ghép file voice…")
            encode_voice_mp3([s.audio_path for s in segments], out,
                             self.log.emit)
            self.progress.emit(1.0)
            return out
        return self._render_video(segments, tmp, stamp)

    def _synth_all(self, segments: List[Segment], tmp: str) -> None:
        budget = 0.6 if self.mode == "video" else 0.9
        total = len(segments)
        slots = (compute_slots(segments) if self.timing == "timestamps"
                 else [None] * total)
        for i, seg in enumerate(segments):
            self._check_stop()
            self.status.emit(f"Đọc voice {i + 1}/{total}…")
            wav = str(Path(tmp) / f"seg_{i:03d}.wav")
            slot = slots[i]
            if seg.text:
                mp3 = str(Path(tmp) / f"seg_{i:03d}.mp3")
                words = tts.synthesize(seg.text, self.voice, mp3,
                                       stop_event=self._stop)
                if slot:
                    raw = str(Path(tmp) / f"seg_{i:03d}_raw.wav")
                    transcode_to_wav(mp3, raw, 0.0)
                    # When far over the slot, re-read natively faster first —
                    # edge-tts keeps intonation much better than atempo.
                    need = audio_duration(raw) / slot
                    if need > 1.15:
                        rate = min(100, int(round((need - 1) * 100)))
                        words = tts.synthesize(seg.text, self.voice, mp3,
                                               rate=f"+{rate}%",
                                               stop_event=self._stop)
                        transcode_to_wav(mp3, raw, 0.0)
                    seg.speed = fit_wav_to_slot(raw, wav, slot)
                    if need > 1.05:
                        self.log.emit(
                            f"Đoạn {i + 1} [{seg.ts_raw}]: đọc nhanh "
                            f"×{need:.2f} để vừa {slot:.0f}s")
                else:
                    transcode_to_wav(mp3, wav, self.pause)
                seg.words = restore_punctuation(words, seg.text)
            else:
                nxt = segments[i + 1] if i + 1 < total else None
                if slot:
                    dur = slot
                elif (seg.ts_seconds >= 0 and nxt is not None
                        and nxt.ts_seconds > seg.ts_seconds):
                    dur = max(0.5, nxt.ts_seconds - seg.ts_seconds)
                else:
                    dur = DEFAULT_SILENCE
                make_silence_wav(dur, wav)
            seg.audio_path = wav
            seg.duration = audio_duration(wav)
            self.progress.emit(budget * (i + 1) / total)

    def _render_video(self, segments: List[Segment], tmp: str,
                      stamp: str) -> str:
        self._check_stop()
        images = list_images(self.out_dir)
        if not images:
            raise RuntimeError("Không tìm thấy ảnh nào trong thư mục lưu — "
                               "hãy tạo ảnh trước.")
        match_images(segments, images, self.log.emit)

        subs: List[SubSpec] = []
        if self.subtitles:
            self.status.emit("Tạo phụ đề…")
            subs = render_sub_pngs(segments, self.width, self.height, tmp,
                                   self.sub_effect, self.sub_size,
                                   self.sub_pos)

        # A cut longer than its shortest neighbour clip breaks the xfade
        # math, so clamp the transition to half the shortest segment.
        min_dur = min(s.duration for s in segments)
        d = max(0.1, min(self.trans_dur, min_dur / 2))
        trans = resolve_transitions(self.transition, len(segments) - 1)

        graph = build_slideshow_graph(segments, self.width, self.height,
                                      self.fps, trans, d, subs)
        graph_path = str(Path(tmp) / "graph.txt")
        Path(graph_path).write_text(graph, encoding="utf-8")

        out = str(Path(self.out_dir) / f"video_{stamp}.mp4")
        cmd = build_slideshow_command(segments, subs, graph_path, out,
                                      self.fps, d)
        total = sum(s.duration for s in segments)
        self.status.emit("Đang dựng video…")
        code = ffmpeg_runner.run(
            cmd, total,
            progress_cb=lambda p: self.progress.emit(0.6 + 0.4 * p),
            stop_event=self._stop, log_cb=self.log.emit)
        if code == -1:
            raise _Stopped()
        if code != 0 or not Path(out).exists():
            raise RuntimeError("ffmpeg lỗi khi dựng video (xem log).")
        self.progress.emit(1.0)
        return out


class _Stopped(Exception):
    pass
