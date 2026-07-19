"""Batch image generation via the Gemini API (stdlib urllib) + filename helpers.

Only ``ImageGenWorker`` touches Qt; the API client, prompt parsing and
filename building are pure Python so they can be unit-tested offline,
same philosophy as ``app/core/youtube_api.py``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QThread, Signal

GEMINI_MODEL = "gemini-2.5-flash-image"
GEMINI_TEXT_MODEL = "gemini-2.5-flash"  # vision input works on the free tier
# ``gemini-2.5-flash-image`` uses the v1beta GenerateContent schema.
# Its stable-v1 schema rejects generationConfig fields with "Invalid JSON".
API_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
TEXT_API_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_TEXT_MODEL}:generateContent")

DESCRIBE_PROMPT = (
    "Describe this character's appearance in detail for an image generation "
    "prompt: gender, age, face, hair, eyes, outfit, notable accessories, art "
    "style. One compact English paragraph, no preamble.")
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/"
CF_API = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"

# (label, model id, supports width/height)
CF_MODELS = [
    ("FLUX.1 schnell (nét nhất, ảnh vuông 1024)",
     "@cf/black-forest-labs/flux-1-schnell", False),
    ("Leonardo Lucid Origin (chọn được tỷ lệ)",
     "@cf/leonardo/lucid-origin", True),
    ("SDXL Lightning (chọn được tỷ lệ, nhanh)",
     "@cf/bytedance/stable-diffusion-xl-lightning", True),
]

_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
MAX_IMAGE_ATTEMPTS = 3


class ImageGenError(Exception):
    def __init__(self, message: str, code: int = 0, status: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def is_key_error(self) -> bool:
        """True when the error is tied to the key (quota/invalid) → rotate."""
        if self.code in (401, 403, 429) or self.status == "RESOURCE_EXHAUSTED":
            return True
        return self.code == 400 and "api key not valid" in str(self).lower()

    @property
    def short_reason(self) -> str:
        """Human-readable Vietnamese summary of a key-level error."""
        msg = str(self)
        if "limit: 0" in msg:
            return ("key không có quota tạo ảnh — free tier = 0, "
                    "cần bật billing trên Google Cloud")
        if "api key not valid" in msg.lower():
            return "key không hợp lệ"
        if self.code == 401:
            return "token không hợp lệ hoặc thiếu quyền"
        if self.code == 429 or self.status == "RESOURCE_EXHAUSTED":
            return "hết quota"
        return msg[:80]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _gemini_body(prompt: str, ref_image: bytes | None = None,
                 ref_mime: str = "image/png", aspect: str = "") -> dict:
    parts: list[dict] = []
    if ref_image:
        parts.append({"inlineData": {
            "mimeType": ref_mime,
            "data": base64.b64encode(ref_image).decode("ascii"),
        }})
    parts.append({"text": prompt})
    # The model's v1beta REST schema uses imageConfig.  The v1 endpoint has a
    # different schema and rejects these fields as an invalid JSON payload.
    gen_cfg: dict = {"responseModalities": ["TEXT", "IMAGE"]}
    if aspect:
        gen_cfg["imageConfig"] = {"aspectRatio": aspect}
    return {"contents": [{"parts": parts}], "generationConfig": gen_cfg}


def generate_image(prompt: str, api_key: str,
                   ref_image: bytes | None = None,
                   ref_mime: str = "image/png",
                   aspect: str = "",
                   timeout: float = 120.0) -> tuple[bytes, str]:
    """Return (image_bytes, mime_type) for *prompt*; raise ImageGenError."""
    body = json.dumps(
        _gemini_body(prompt, ref_image, ref_mime, aspect)).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers={
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

    return _extract_image(data)


def describe_character(image_bytes: bytes, mime: str, api_key: str,
                       timeout: float = 60.0) -> str:
    """Turn a character photo into a rich text description (free-tier Gemini).

    Lets keyless providers (Pollinations/Cloudflare) approximate the
    reference-image lock that only the paid Gemini image model supports.
    """
    body = json.dumps({"contents": [{"parts": [
        {"inlineData": {"mimeType": mime,
                        "data": base64.b64encode(image_bytes).decode("ascii")}},
        {"text": DESCRIBE_PROMPT},
    ]}]}).encode("utf-8")
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
    except (KeyError, IndexError):
        raise ImageGenError("không có mô tả trong phản hồi") from None
    if not text:
        raise ImageGenError("không có mô tả trong phản hồi")
    return text


class DescribeWorker(QThread):
    """One-shot: photo → character description, off the GUI thread."""

    done = Signal(str)
    failed = Signal(str)

    def __init__(self, image_path: str, api_key: str, parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self.api_key = api_key

    def run(self) -> None:
        try:
            data = Path(self.image_path).read_bytes()
            desc = describe_character(data, _sniff_mime(data), self.api_key)
            self.done.emit(desc)
        except (OSError, ImageGenError) as e:
            self.failed.emit(str(e))


def generate_image_pollinations(prompt: str, width: int = 1280,
                                height: int = 720, seed: int | None = None,
                                timeout: float = 300.0) -> tuple[bytes, str]:
    """Free keyless provider; returns (image_bytes, mime_type)."""
    url = (POLLINATIONS_URL + urllib.parse.quote(prompt, safe="")
           + f"?width={width}&height={height}&nologo=true")
    if seed is not None:
        url += f"&seed={seed}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            mime = (resp.headers.get("Content-Type")
                    or "image/jpeg").split(";")[0].strip()
            return resp.read(), mime
    except urllib.error.HTTPError as e:
        raise ImageGenError(f"HTTP {e.code}", e.code) from e
    except urllib.error.URLError as e:
        raise ImageGenError(f"Lỗi mạng: {e.reason}") from e


def generate_image_cloudflare(prompt: str, account_id: str, token: str,
                              model: str, width: int = 1280,
                              height: int = 720,
                              timeout: float = 180.0) -> tuple[bytes, str]:
    """Cloudflare Workers AI text-to-image; returns (image_bytes, mime)."""
    payload: dict = {"prompt": prompt}
    supports_size = next((s for _, m, s in CF_MODELS if m == model), True)
    if supports_size:
        payload["width"] = width
        payload["height"] = height
    else:
        payload["steps"] = 8  # flux-1-schnell: max quality within free tier
    url = CF_API.format(account=account_id, model=model)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0]
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        raise ImageGenError(_extract_cf_error(body) or f"HTTP {e.code}",
                            e.code) from e
    except urllib.error.URLError as e:
        raise ImageGenError(f"Lỗi mạng: {e.reason}") from e

    if ctype == "application/json":  # base64 wrapped (flux, lucid-origin)
        b64 = (json.loads(raw).get("result") or {}).get("image", "")
        if not b64:
            raise ImageGenError("không có ảnh trong phản hồi")
        raw = base64.b64decode(b64)
    return raw, _sniff_mime(raw)


def _extract_cf_error(body: str) -> str:
    try:
        errs = json.loads(body).get("errors") or []
        return "; ".join(e.get("message", "") for e in errs if e.get("message"))
    except (json.JSONDecodeError, AttributeError):
        return ""


def _sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _extract_error(body: str) -> tuple[str, str]:
    try:
        err = json.loads(body).get("error", {})
        return err.get("message", ""), err.get("status", "")
    except (json.JSONDecodeError, AttributeError):
        return "", ""


def _extract_image(data: dict) -> tuple[bytes, str]:
    candidates = data.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts")
             if candidates else None) or []
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            return base64.b64decode(inline["data"]), mime
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise ImageGenError(f"bị chặn (safety: {block})")
    raise ImageGenError("không có ảnh trong phản hồi")


# --------------------------------------------------------------------------
# Prompt parsing / filename building
# --------------------------------------------------------------------------
TIMESTAMP_RE = re.compile(r"^\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$")
ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def parse_prompt_line(line: str) -> tuple[str, str]:
    """'[00:15] Cô gái...' -> ('00:15', 'Cô gái...'); no bracket -> ('', line)."""
    m = TIMESTAMP_RE.match(line)
    if m:
        return m.group(1), m.group(2).strip()
    return "", line.strip()


def normalize_prompts(text: str) -> list[str]:
    """Turn the prompt box content into one prompt per entry.

    Accepts both layouts — inline and block — even mixed::

        [00:00] A cute stickman storyteller     ->  as-is

        [00:05]
        A cute stickman storyteller             ->  "[00:05] A cute stickman…"

    A ``[mm:ss]`` line starts a prompt; following non-timestamp lines belong
    to it (joined with spaces). Without any timestamp in the box, every
    non-empty line is its own prompt (the original behaviour).
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    if not any(TIMESTAMP_RE.match(ln) for ln in lines if ln):
        return [ln for ln in lines if ln]

    prompts: list[str] = []
    cur_ts: str | None = None
    cur_parts: list[str] = []

    def flush() -> None:
        nonlocal cur_ts, cur_parts
        if cur_ts is not None:
            body = " ".join(cur_parts).strip()
            prompts.append(f"[{cur_ts}] {body}".rstrip())
        cur_ts, cur_parts = None, []

    for ln in lines:
        if not ln:
            continue
        m = TIMESTAMP_RE.match(ln)
        if m:
            flush()
            cur_ts = m.group(1)
            rest = m.group(2).strip()
            cur_parts = [rest] if rest else []
        elif cur_ts is not None:
            cur_parts.append(ln)
        else:  # loose line before the first timestamp
            prompts.append(ln)
    flush()
    return prompts


def build_filename(index: int, total: int, line: str, ext: str = ".png",
                   max_stem: int = 48) -> str:
    """Build a compact, Windows-safe generated-image filename.

    The filename is later passed to ffmpeg when making a slideshow.  Keeping
    it short avoids Windows command/path length limits for large batches.
    """
    pad = max(3, len(str(total)))
    ts, title = parse_prompt_line(line)
    if ts:
        stem = f"{index:0{pad}d}_[{ts.replace(':', '-')}] {title}"
    else:
        stem = f"{index:0{pad}d}_{title or 'image'}"
    stem = ILLEGAL_RE.sub("", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    stem = stem[:max_stem].rstrip(" .")
    return stem + ext


def ext_for_mime(mime: str) -> str:
    return _MIME_EXT.get(mime, ".png")


_PIL_FMT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


def ensure_aspect(data: bytes, mime: str, width: int,
                  height: int) -> tuple[bytes, str]:
    """Center-crop the image to width:height if its ratio differs (>2%).

    Providers don't always honour the requested size (Gemini ignores it,
    FLUX schnell is square-only), so the ratio is enforced locally.
    Resolution is kept — only the ratio is corrected, never upscaled.
    """
    target = width / height
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
    except Exception:
        return data, mime  # not decodable — save as-is
    if abs(w / h - target) / target < 0.02:
        return data, mime
    if w / h > target:  # too wide → trim sides
        new_w = round(h * target)
        left = (w - new_w) // 2
        im = im.crop((left, 0, left + new_w, h))
    else:               # too tall → trim, biased to keep the top (faces)
        new_h = round(w / target)
        top = round((h - new_h) * 0.25)
        im = im.crop((0, top, w, top + new_h))
    fmt = _PIL_FMT.get(mime, "PNG")
    if fmt == "JPEG" and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, fmt, quality=92)
    return buf.getvalue(), mime


def compose_prompt(prompt: str, char_name: str = "", char_desc: str = "",
                   has_ref_image: bool = False) -> str:
    """Prefix *prompt* with the locked character so every image reuses them."""
    char = ", ".join(p for p in (char_name.strip(), char_desc.strip()) if p)
    if has_ref_image:
        who = f" ({char})" if char else ""
        return ("Generate an image of the exact character from the reference "
                f"image{who} — keep the same face, hair and outfit. "
                f"Scene: {prompt}")
    if char:
        return (f"Character: {char}. Scene: {prompt}. "
                "Keep the character's appearance consistent.")
    return prompt


def aspect_hint(width: int, height: int) -> str:
    """Composition guidance so text-only models frame the shot correctly.

    Without it they squeeze a portrait close-up into a wide canvas and the
    top of the head lands outside the frame.
    """
    ratio = width / height
    if ratio > 1.15:
        return (" Wide cinematic landscape composition, camera pulled back "
                "to a medium-long shot, subject fully inside the frame with "
                "clear headroom above the head, no cropped close-up.")
    if ratio < 0.87:
        return (" Vertical composition, subject fully inside the frame with "
                "headroom above the head, no cropped close-up.")
    return ""


# --------------------------------------------------------------------------
# Key rotation
# --------------------------------------------------------------------------
class KeyPool:
    """A list of API keys consumed in order; rotate() moves to the next."""

    def __init__(self, keys: list[str]):
        self._keys = [k for k in keys if k.strip()]
        self._i = 0

    def current(self) -> str:
        return self._keys[self._i]

    def rotate(self) -> bool:
        """Advance to the next key; False when every key has been burned."""
        if self._i + 1 >= len(self._keys):
            return False
        self._i += 1
        return True

    @property
    def index(self) -> int:
        return self._i

    @property
    def total(self) -> int:
        return len(self._keys)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class ImageGenWorker(QThread):
    item_started = Signal(int)              # row
    item_status = Signal(int, str)          # row, transient status text
    item_finished = Signal(int, bool, str)  # row, ok, filename | error message
    progress = Signal(int, int)             # done, total
    status = Signal(str)
    log = Signal(str)
    finished_all = Signal(int, int)         # ok_count, fail_count

    def __init__(self, prompts: list[str], out_dir: str, keys: list[str],
                 provider: str = "gemini", width: int = 1280,
                 height: int = 720, aspect: str = "", account_id: str = "",
                 account_ids: list[str] | None = None, model: str = "",
                 char_name: str = "", char_desc: str = "",
                 char_image_path: str = "", describe_key: str = "",
                 parent=None):
        super().__init__(parent)
        self.prompts = prompts
        self.out_dir = out_dir
        self.pool = KeyPool(keys)
        self.provider = provider
        self.width = width
        self.height = height
        self.aspect = aspect
        # Cloudflare credentials are paired by index: account_ids[n] only
        # ever runs with keys[n].  account_id remains a legacy fallback.
        self.account_ids = [a.strip() for a in (account_ids or []) if a.strip()]
        if not self.account_ids and account_id.strip():
            self.account_ids = [account_id.strip()]
        self.model = model
        self.char_name = char_name
        self.char_desc = char_desc
        self.char_image_path = char_image_path
        self.describe_key = describe_key
        self._ref_image: bytes | None = None
        self._ref_mime = "image/png"
        # one seed per batch keeps Pollinations' style/character more uniform
        self._seed = random.randint(1, 2_000_000_000)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # noqa: BLE001 - surface anything to the UI log
            self.log.emit(f"✗ Lỗi: {e}")
            self.finished_all.emit(0, 0)

    def _run(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        self._load_ref_image()
        self._auto_describe()
        total = len(self.prompts)
        ok = fail = 0
        for row, line in enumerate(self.prompts):
            if self._stop.is_set():
                self.status.emit(f"Đã dừng ({ok}/{total} ảnh).")
                self.finished_all.emit(ok, fail)
                return
            self.item_started.emit(row)
            success = self._generate_one(row, total, line)
            if success is None:  # all keys exhausted
                fail += 1
                for r in range(row + 1, total):
                    self.item_finished.emit(r, False, "Bỏ qua (hết key khả dụng)")
                    fail += 1
                self.status.emit("Tất cả API key đều lỗi — xem cột Trạng thái.")
                self.finished_all.emit(ok, fail)
                return
            if success:
                ok += 1
            else:
                fail += 1
            self.progress.emit(ok + fail, total)
            if row + 1 < total:
                self._stop.wait(2.0)  # free tier is RPM-limited

        self.status.emit(f"Hoàn tất: {ok} ảnh, {fail} lỗi.")
        self.finished_all.emit(ok, fail)

    def _load_ref_image(self) -> None:
        """Reference image locks the character — only Gemini accepts one."""
        if not (self.char_image_path and self.provider == "gemini"):
            return
        try:
            self._ref_image = Path(self.char_image_path).read_bytes()
            self._ref_mime = _sniff_mime(self._ref_image)
        except OSError as e:
            self.log.emit(f"⚠ Không đọc được ảnh nhân vật: {e}")

    def _auto_describe(self) -> None:
        """Non-Gemini providers can't take the reference image, so distil it
        into a text description automatically before the batch starts."""
        if (self.provider == "gemini" or not self.char_image_path
                or self.char_desc.strip()):
            return  # image sent directly / no image / user already has a desc
        if not self.describe_key:
            self.log.emit(
                "⚠ Ảnh nhân vật bị bỏ qua: nguồn này không nhận ảnh đầu vào. "
                "Nhập 'Key Gemini (free)' để tự phân tích ảnh thành mô tả.")
            return
        self.status.emit("Đang phân tích ảnh nhân vật…")
        try:
            data = Path(self.char_image_path).read_bytes()
            self.char_desc = describe_character(
                data, _sniff_mime(data), self.describe_key)
            self.log.emit("✔ Đã tạo mô tả nhân vật từ ảnh — áp dụng cho mọi prompt.")
        except (OSError, ImageGenError) as e:
            self.log.emit(f"⚠ Không phân tích được ảnh nhân vật ({e}) — "
                          "tiếp tục không khóa nhân vật.")

    def _generate_one(self, row: int, total: int, line: str) -> bool | None:
        """True = saved, False = prompt-level failure, None = keys exhausted."""
        # the [00:15] timestamp is filename metadata, not part of the prompt
        ts, title = parse_prompt_line(line)
        prompt = compose_prompt(title or line, self.char_name, self.char_desc,
                                has_ref_image=self._ref_image is not None)
        if self.provider != "gemini":  # Gemini frames via imageConfig instead
            prompt += aspect_hint(self.width, self.height)

        if self.provider == "pollinations":
            attempts = MAX_IMAGE_ATTEMPTS  # community service throws transient 5xx errors
            for attempt in range(1, attempts + 1):
                try:
                    img, mime = generate_image_pollinations(
                        prompt, self.width, self.height, self._seed)
                    return self._save(row, total, line, img, mime)
                except ImageGenError as e:
                    if attempt < attempts:
                        self.item_status.emit(
                            row, f"Lỗi tạm ({e}) – thử lại "
                                 f"{attempt + 1}/{attempts}")
                        if self._stop.wait(3.0):
                            break
                        continue
                    self.item_finished.emit(row, False, f"Lỗi: {e}")
                    self.log.emit(f"✗ Dòng {row + 1}: {e}")
                    return False
            self.item_finished.emit(row, False, "Đã dừng")
            return False

        # Gemini & Cloudflare retry a failed prompt before proceeding to the
        # next one. Quota/auth failures rotate immediately because the same
        # credential cannot recover from those errors.
        attempts = 0
        while True:
            try:
                if self.provider == "cloudflare":
                    img, mime = generate_image_cloudflare(
                        prompt, self.account_ids[self.pool.index], self.pool.current(),
                        self.model, self.width, self.height)
                else:
                    img, mime = generate_image(prompt, self.pool.current(),
                                               self._ref_image, self._ref_mime,
                                               self.aspect)
            except ImageGenError as e:
                if not e.is_key_error:
                    attempts += 1
                    if attempts < MAX_IMAGE_ATTEMPTS:
                        self.item_status.emit(
                            row, f"Retry {attempts + 1}/{MAX_IMAGE_ATTEMPTS}: {e}")
                        self.log.emit(
                            f"Prompt {row + 1} failed; retry "
                            f"{attempts + 1}/{MAX_IMAGE_ATTEMPTS}: {e}")
                        if not self._stop.wait(3.0):
                            continue
                        self.item_finished.emit(row, False, "Stopped")
                        return False
                if not e.is_key_error:
                    self.item_finished.emit(row, False, f"Lỗi: {e}")
                    self.log.emit(f"✗ Dòng {row + 1}: {e}")
                    return False
                old = self.pool.index + 1
                if not self.pool.rotate():
                    self.item_finished.emit(
                        row, False, f"Lỗi: hết key ({e.short_reason})")
                    self.log.emit(f"✗ Key {old}/{self.pool.total}: "
                                  f"{e.short_reason}; không còn key nào khác.")
                    return None
                new = self.pool.index + 1
                self.item_status.emit(
                    row,
                    f"Key {old} lỗi ({e.short_reason}) – đổi key "
                    f"{new}/{self.pool.total}")
                self.status.emit(
                    f"Key {old}: {e.short_reason} → chuyển key "
                    f"{new}/{self.pool.total}")
                self.log.emit(f"⚠ Key {old} lỗi ({e}) → dùng key {new}")
                continue  # retry same prompt with the next key

            return self._save(row, total, line, img, mime)

    def _save(self, row: int, total: int, line: str,
              img: bytes, mime: str) -> bool:
        img, mime = ensure_aspect(img, mime, self.width, self.height)
        name = build_filename(row + 1, total, line, ext_for_mime(mime))
        try:
            (Path(self.out_dir) / name).write_bytes(img)
        except OSError as e:
            self.item_finished.emit(row, False, f"Lỗi ghi file: {e}")
            return False
        self.item_finished.emit(row, True, name)
        return True
