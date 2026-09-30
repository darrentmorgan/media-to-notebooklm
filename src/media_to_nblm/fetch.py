"""Metadata fetcher: YouTube Data API when YOUTUBE_API_KEY is set, else yt-dlp.

With a key, `fetch_flat` returns fully populated entries from the Data API
(see ytapi.py) and `enrich` has nothing left to do. Without a key, or if the API
call fails, the yt-dlp two-pass path below runs.

Pass 1 (flat-playlist): fast listing of id/title/view_count/duration for up to
`recent_cap` most recent uploads. No upload_date from YouTube at this layer.

Pass 2 (enrich): full-extract a smaller candidate pool to populate upload_date
and description (needed for --days window filtering and keyword matching on
descriptions). Callers that only need views-sorted, no-date-window, no-keyword
selection can skip pass 2 entirely.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any

from . import ytapi

log = logging.getLogger(__name__)


def _proxy_args() -> list[str]:
    """Return --proxy flag if YT_DLP_PROXY / HTTPS_PROXY / HTTP_PROXY is set.

    Lets users route yt-dlp through a SOCKS or HTTP proxy when the local
    network blocks YouTube (e.g. SNI-filtering home router, parental control,
    or DNS family filter). Example:
        YT_DLP_PROXY=socks5://127.0.0.1:1080
    """
    proxy = (
        os.environ.get("YT_DLP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    return ["--proxy", proxy] if proxy else []


def _yt_dlp_cmd() -> list[str]:
    """Resolve yt-dlp invocation robustly across cron/venv/cloud contexts.

    Prefers the venv-local binary, then PATH, then `python -m yt_dlp` fallback.
    """
    # 1. venv-local binary (most reliable when running via .venv/bin/python)
    venv_bin = pathlib.Path(sys.executable).parent / "yt-dlp"
    if venv_bin.exists():
        return [str(venv_bin)]
    # 2. PATH lookup
    on_path = shutil.which("yt-dlp")
    if on_path:
        return [on_path]
    # 3. python module fallback (works as long as yt-dlp is pip-installed)
    return [sys.executable, "-m", "yt_dlp"]

KEEP_FIELDS = (
    "id",
    "title",
    "webpage_url",
    "original_url",
    "view_count",
    "upload_date",
    "duration",
    "description",
    "channel",
    "uploader",
)


def _video_url(entry: dict[str, Any]) -> str | None:
    url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    if url:
        return url
    vid = entry.get("id")
    if vid and len(vid) == 11:
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = {k: raw.get(k) for k in KEEP_FIELDS}
    if not normalized.get("webpage_url"):
        normalized["webpage_url"] = _video_url(raw)
    return normalized


def fetch_flat(url: str, recent_cap: int = 200) -> list[dict[str, Any]]:
    """Listing for a video/channel/playlist URL.

    With YOUTUBE_API_KEY set, uses the YouTube Data API (fully populated entries,
    no enrich pass needed) and falls back to yt-dlp on any API error. Without a
    key, uses the yt-dlp flat-playlist pass.
    """
    if ytapi.api_key():
        try:
            videos = ytapi.fetch(url, recent_cap=recent_cap)
            if videos:
                log.info("fetch_flat: YouTube Data API returned %d videos", len(videos))
                return videos
            log.warning("fetch_flat: YouTube Data API returned no videos; falling back to yt-dlp")
        except ytapi.YouTubeAPIError as e:
            log.warning("fetch_flat: YouTube Data API failed (%s); falling back to yt-dlp", e)
    return _fetch_flat_ytdlp(url, recent_cap)


def _fetch_flat_ytdlp(url: str, recent_cap: int = 200) -> list[dict[str, Any]]:
    """Fast: flat-playlist listing for channel/playlist. Single video URLs return [one entry]."""
    cmd = [
        *_yt_dlp_cmd(),
        *_proxy_args(),
        "--flat-playlist",
        "--dump-json",
        "--ignore-errors",
        "--no-warnings",
        "--playlist-end",
        str(recent_cap),
        url,
    ]
    log.info("fetch_flat: recent_cap=%d", recent_cap)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(_normalize(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return out


def enrich(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Full-extract each video in a single yt-dlp invocation to add upload_date + description.

    One yt-dlp process handling N URLs amortizes startup cost (~0.3s) across N calls,
    far cheaper than spawning N processes. Order-preserving by URL.
    Entries that already carry an upload_date (e.g. from the Data API) are skipped.
    """
    urls = [v["webpage_url"] for v in videos if v.get("webpage_url") and not v.get("upload_date")]
    if not urls:
        return videos

    log.info("enrich: full-extract %d urls", len(urls))
    cmd = [
        *_yt_dlp_cmd(),
        *_proxy_args(),
        "--dump-json",
        "--ignore-errors",
        "--no-warnings",
        "--skip-download",
        *urls,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

    enriched_by_id: dict[str, dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        n = _normalize(raw)
        vid = n.get("id")
        if vid:
            enriched_by_id[vid] = n

    merged: list[dict[str, Any]] = []
    for v in videos:
        vid = v.get("id")
        if vid and vid in enriched_by_id:
            combined = {**v, **{k: val for k, val in enriched_by_id[vid].items() if val is not None}}
            merged.append(combined)
        else:
            merged.append(v)
    return merged


def write_jsonl(videos: list[dict[str, Any]], path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for v in videos:
            f.write(json.dumps(v) + "\n")


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
