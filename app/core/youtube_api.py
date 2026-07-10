"""YouTube Data API v3 client (stdlib urllib) + pure parsing helpers.

Qt-free and import-light so the parsing/metric logic can be unit-tested offline
by monkeypatching ``_http_get_json``.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence, Union

API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeApiError(Exception):
    pass


class QuotaExceededError(YouTubeApiError):
    """The API key ran out of quota (HTTP 403 quotaExceeded and friends)."""


_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
                  "userRateLimitExceeded"}


class KeyPool:
    """Ordered pool of API keys; advances to the next key when one hits quota.

    Thread-safe: ``_enrich`` runs in a ThreadPoolExecutor, so several requests
    may report quota exhaustion for the same key concurrently.
    """

    def __init__(self, keys: Sequence[str],
                 on_rotate: Optional[Callable[[int, int], None]] = None):
        self._keys = [k.strip() for k in keys if k and k.strip()]
        if not self._keys:
            raise ValueError("Cần ít nhất 1 API key")
        self._idx = 0
        self._lock = threading.Lock()
        self._on_rotate = on_rotate

    def current(self) -> tuple:
        """Return (key, index) of the active key."""
        with self._lock:
            return self._keys[self._idx], self._idx

    def report_exhausted(self, idx: int) -> bool:
        """Mark the key at ``idx`` as out of quota.

        Returns True if another key is available (caller should retry),
        False when the whole pool is exhausted.
        """
        with self._lock:
            if idx != self._idx:        # another thread already rotated
                return True
            if self._idx + 1 >= len(self._keys):
                return False
            self._idx += 1
            rotate, pos, total = self._on_rotate, self._idx + 1, len(self._keys)
        if rotate:
            rotate(pos, total)
        return True


@dataclass
class ChannelInfo:
    channel_id: str
    title: str = ""
    handle: str = ""
    url: str = ""
    country: str = ""
    published_at: str = ""
    age_days: int = 0
    subs: int = 0
    total_views: int = 0
    total_videos: int = 0
    recent_count: int = 0
    views_per_day: float = 0.0
    views_per_day_high: float = 0.0
    top_video_id: str = ""
    uploads_playlist: str = ""
    thumb_url: str = ""
    thumb_bytes: Optional[bytes] = field(default=None, repr=False)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _http_get_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        msg, reasons = _extract_error(body)
        if e.code == 403 and (reasons & _QUOTA_REASONS
                              or "quota" in (msg or "").lower()):
            raise QuotaExceededError(msg or "Hết quota API key") from e
        raise YouTubeApiError(msg or f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise YouTubeApiError(f"Lỗi mạng: {e.reason}") from e


def _extract_error(body: str) -> tuple:
    """Return (message, {reason, ...}) from an API error body."""
    try:
        err = json.loads(body).get("error", {})
        reasons = {e.get("reason", "") for e in err.get("errors", [])}
        return err.get("message"), reasons
    except (json.JSONDecodeError, AttributeError):
        return None, set()


def _get(endpoint: str, params: dict, key: Union[str, KeyPool]) -> dict:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if not isinstance(key, KeyPool):
        clean["key"] = key
        url = f"{API_BASE}/{endpoint}?" + urllib.parse.urlencode(clean)
        return _http_get_json(url)
    while True:
        k, idx = key.current()
        clean["key"] = k
        url = f"{API_BASE}/{endpoint}?" + urllib.parse.urlencode(clean)
        try:
            return _http_get_json(url)
        except QuotaExceededError:
            if not key.report_exhausted(idx):
                raise


def download_bytes(url: str, timeout: float = 10.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _int(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _chunks(seq: List[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def iso_age_days(iso: str, now: Optional[datetime] = None) -> int:
    if not iso:
        return 0
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (now - dt).days)


def iso_days_ago(days: int, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=max(0, days))).strftime("%Y-%m-%dT%H:%M:%SZ")


def channel_url(channel_id: str, handle: str) -> str:
    if handle and handle.startswith("@"):
        return f"https://www.youtube.com/{handle}"
    return f"https://www.youtube.com/channel/{channel_id}"


# --------------------------------------------------------------------------
# Channel-id collection
# --------------------------------------------------------------------------
def _collect_channel_ids(endpoint: str, base_params: dict, key: str,
                         max_results: int) -> List[str]:
    ids: List[str] = []
    seen = set()
    page_token = None
    fetched = 0
    pages = 0
    while fetched < max_results and pages < 20:
        params = dict(base_params)
        params["maxResults"] = min(50, max_results - fetched)
        params["pageToken"] = page_token
        data = _get(endpoint, params, key)
        items = data.get("items", [])
        for it in items:
            cid = it.get("snippet", {}).get("channelId")
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
        fetched += len(items)
        pages += 1
        page_token = data.get("nextPageToken")
        if not page_token or not items:
            break
    return ids


# regionCode alone only limits results to videos *viewable* in the region,
# so pair it with the region's dominant language to bias relevance.
REGION_LANG = {
    "US": "en", "GB": "en", "AU": "en", "CA": "en", "PH": "en",
    "JP": "ja", "VN": "vi", "KR": "ko", "IN": "hi", "DE": "de",
    "FR": "fr", "BR": "pt", "RU": "ru", "ID": "id", "TH": "th",
    "ES": "es", "MX": "es", "IT": "it",
}


def search_video_channel_ids(key: str, q: str, region: str, published_after: str,
                             max_results: int, order: str = "viewCount") -> List[str]:
    return _collect_channel_ids("search", {
        "part": "snippet", "q": q, "type": "video", "order": order,
        "regionCode": region or None, "publishedAfter": published_after or None,
        "relevanceLanguage": REGION_LANG.get((region or "").upper()),
    }, key, max_results)


def trending_channel_ids(key: str, region: str, max_results: int) -> List[str]:
    return _collect_channel_ids("videos", {
        "part": "snippet", "chart": "mostPopular", "regionCode": region or "US",
    }, key, max_results)


# --------------------------------------------------------------------------
# Channel / video details
# --------------------------------------------------------------------------
def list_channels(key: str, ids: List[str]) -> dict:
    out = {}
    for batch in _chunks(ids, 50):
        data = _get("channels", {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(batch), "maxResults": 50}, key)
        for it in data.get("items", []):
            out[it["id"]] = it
    return out


def recent_video_ids(key: str, uploads_playlist: str, n: int) -> List[str]:
    if not uploads_playlist or n <= 0:
        return []
    data = _get("playlistItems", {
        "part": "contentDetails", "playlistId": uploads_playlist,
        "maxResults": min(50, n)}, key)
    return [it["contentDetails"]["videoId"] for it in data.get("items", [])
            if it.get("contentDetails", {}).get("videoId")]


def list_video_stats(key: str, ids: List[str]) -> List[dict]:
    out: List[dict] = []
    for batch in _chunks(ids, 50):
        data = _get("videos", {"part": "snippet,statistics",
                               "id": ",".join(batch), "maxResults": 50}, key)
        for it in data.get("items", []):
            out.append({
                "id": it.get("id", ""),
                "published_at": it.get("snippet", {}).get("publishedAt", ""),
                "views": _int(it.get("statistics", {}).get("viewCount")),
            })
    return out


# --------------------------------------------------------------------------
# Pure parsing / metrics (unit-tested)
# --------------------------------------------------------------------------
def parse_channel(raw: dict, now: Optional[datetime] = None) -> ChannelInfo:
    sn = raw.get("snippet", {})
    st = raw.get("statistics", {})
    cd = raw.get("contentDetails", {})
    cid = raw.get("id", "")
    handle = sn.get("customUrl", "")
    age = iso_age_days(sn.get("publishedAt", ""), now)
    views = _int(st.get("viewCount"))
    thumbs = sn.get("thumbnails", {})
    thumb = (thumbs.get("default") or thumbs.get("medium") or
             thumbs.get("high") or {}).get("url", "")
    return ChannelInfo(
        channel_id=cid,
        title=sn.get("title", ""),
        handle=handle,
        url=channel_url(cid, handle),
        country=sn.get("country", ""),
        published_at=sn.get("publishedAt", ""),
        age_days=age,
        subs=_int(st.get("subscriberCount")),
        total_views=views,
        total_videos=_int(st.get("videoCount")),
        uploads_playlist=cd.get("relatedPlaylists", {}).get("uploads", ""),
        views_per_day=round(views / max(age, 1), 2),
        thumb_url=thumb,
    )


def recent_metrics(videos: List[dict], now: Optional[datetime] = None):
    """Return (views_per_day_high, top_video_id) over recent videos."""
    best_vpd = 0.0
    best_id = ""
    for v in videos:
        age = max(iso_age_days(v.get("published_at", ""), now), 1)
        vpd = v.get("views", 0) / age
        if vpd > best_vpd:
            best_vpd = vpd
            best_id = v.get("id", "")
    return round(best_vpd, 2), best_id
