"""YouTube Data API v3 fetcher (stdlib only).

Used instead of yt-dlp when `YOUTUBE_API_KEY` is set. It needs no cookies and is
not subject to YouTube's bot checks on datacenter IPs, which makes it the
reliable path for cloud sessions. One pass returns everything selection needs
(title, description, upload_date, view_count, duration, channel), so the yt-dlp
enrich pass is skipped for API results.

Resolution:
- video URL (watch?v=, youtu.be/, /shorts/, /live/)  → videos.list
- playlist URL (list=)                                → playlistItems.list → videos.list
- channel URL (/@handle, /channel/UC…, /user/name)    → channels.list → uploads playlist
- /c/<custom> URLs have no API lookup; raise so the caller falls back to yt-dlp.

Every error raises YouTubeAPIError; callers decide whether to fall back.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
PAGE_SIZE = 50  # API maximum for maxResults and for ids per videos.list call
TIMEOUT = 30

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


class YouTubeAPIError(RuntimeError):
    pass


def api_key() -> str | None:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    return key or None


def _get(endpoint: str, params: dict[str, Any], key: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({**params, "key": key})
    req = urllib.request.Request(
        f"{API_BASE}/{endpoint}?{query}", headers={"Accept": "application/json"}
    )
    # Log without the key.
    log.debug("ytapi GET %s %s", endpoint, params)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8"))
            err = body.get("error", {})
            reasons = [x.get("reason") for x in err.get("errors", []) if x.get("reason")]
            detail = f"{err.get('message', '')} ({', '.join(reasons)})" if reasons else err.get("message", "")
        except Exception:
            pass
        raise YouTubeAPIError(f"{endpoint}: HTTP {e.code} {detail}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise YouTubeAPIError(f"{endpoint}: {e}") from None
    except json.JSONDecodeError as e:
        raise YouTubeAPIError(f"{endpoint}: invalid JSON ({e})") from None


# ---- URL parsing -------------------------------------------------------------


def parse_url(url: str) -> tuple[str, str]:
    """Return (kind, value): ('video', id) | ('playlist', id) | ('handle', h) |
    ('channel_id', UC…) | ('username', name). Raises YouTubeAPIError otherwise."""
    p = urllib.parse.urlparse(url.strip())
    host = (p.netloc or "").lower()
    path = p.path or ""
    qs = urllib.parse.parse_qs(p.query)

    if host.endswith("youtu.be"):
        vid = path.strip("/").split("/")[0]
        if _VIDEO_ID_RE.match(vid):
            return "video", vid
    if "v" in qs and _VIDEO_ID_RE.match(qs["v"][0]):
        return "video", qs["v"][0]
    m = re.match(r"^/(shorts|live|embed)/([A-Za-z0-9_-]{11})", path)
    if m:
        return "video", m.group(2)
    if "list" in qs and qs["list"][0]:
        return "playlist", qs["list"][0]
    m = re.match(r"^/@([^/]+)", path)
    if m:
        return "handle", urllib.parse.unquote(m.group(1))
    m = re.match(r"^/channel/(UC[A-Za-z0-9_-]{22})", path)
    if m:
        return "channel_id", m.group(1)
    m = re.match(r"^/user/([^/]+)", path)
    if m:
        return "username", m.group(1)
    raise YouTubeAPIError(f"unsupported URL for the Data API: {url}")


# ---- normalization -----------------------------------------------------------


def _duration_seconds(iso: str | None) -> int | None:
    if not iso:
        return None
    m = _DURATION_RE.match(iso)
    if not m:
        return None
    d, h, mi, s = (int(m.group(g) or 0) for g in ("d", "h", "m", "s"))
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _upload_date(published_at: str | None) -> str | None:
    # "2024-05-01T12:34:56Z" → "20240501" (yt-dlp format; select.py parses it)
    if not published_at or len(published_at) < 10:
        return None
    return published_at[:10].replace("-", "")


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    details = item.get("contentDetails", {})
    vid = item.get("id")
    views = stats.get("viewCount")
    channel = snippet.get("channelTitle")
    url = f"https://www.youtube.com/watch?v={vid}"
    return {
        "id": vid,
        "title": snippet.get("title"),
        "webpage_url": url,
        "original_url": url,
        "view_count": int(views) if views is not None else None,
        "upload_date": _upload_date(snippet.get("publishedAt")),
        "duration": _duration_seconds(details.get("duration")),
        "description": snippet.get("description") or "",
        "channel": channel,
        "uploader": channel,
    }


# ---- fetch -------------------------------------------------------------------


def _videos(ids: list[str], key: str) -> list[dict[str, Any]]:
    """videos.list in batches of 50, order-preserving; unavailable ids are dropped."""
    by_id: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), PAGE_SIZE):
        batch = ids[i : i + PAGE_SIZE]
        data = _get(
            "videos",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(batch), "maxResults": PAGE_SIZE},
            key,
        )
        for item in data.get("items", []):
            by_id[item["id"]] = _normalize(item)
    return [by_id[v] for v in ids if v in by_id]


def _playlist_video_ids(playlist_id: str, cap: int, key: str) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    while len(ids) < cap:
        params: dict[str, Any] = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": min(PAGE_SIZE, cap - len(ids)),
        }
        if token:
            params["pageToken"] = token
        data = _get("playlistItems", params, key)
        for item in data.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid:
                ids.append(vid)
        token = data.get("nextPageToken")
        if not token:
            break
    return ids[:cap]


def _uploads_playlist(kind: str, value: str, key: str) -> str:
    if kind == "channel_id":
        attempts = [{"id": value}]
    elif kind == "username":
        attempts = [{"forUsername": value}]
    else:  # handle: try with and without the @ prefix
        bare = value.lstrip("@")
        attempts = [{"forHandle": f"@{bare}"}, {"forHandle": bare}]
    for params in attempts:
        data = _get("channels", {"part": "contentDetails", **params}, key)
        items = data.get("items") or []
        if items:
            uploads = (
                items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            )
            if uploads:
                return uploads
    raise YouTubeAPIError(f"channel not found for {kind}={value}")


def fetch(url: str, recent_cap: int = 200, key: str | None = None) -> list[dict[str, Any]]:
    """Return normalized, fully-populated video dicts for a video/playlist/channel URL.

    Channel uploads come newest first, so `recent_cap` keeps the most recent uploads,
    matching yt-dlp's --playlist-end behaviour.
    """
    key = key or api_key()
    if not key:
        raise YouTubeAPIError("YOUTUBE_API_KEY is not set")
    kind, value = parse_url(url)
    if kind == "video":
        ids = [value]
    elif kind == "playlist":
        ids = _playlist_video_ids(value, recent_cap, key)
    else:
        ids = _playlist_video_ids(_uploads_playlist(kind, value, key), recent_cap, key)
    log.info("ytapi: %s=%s → %d video ids", kind, value, len(ids))
    return _videos(ids, key)
