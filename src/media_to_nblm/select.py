"""Selection logic: time window + keyword filter + view-count ranking + hard cap."""
from __future__ import annotations

import datetime as dt
import pathlib
import re
from typing import Any

PLAN_CAPS: dict[str, int] = {
    "free": 45,
    "plus": 95,
    "pro": 295,
    "ultra": 595,
}

PLAN_PUBLISHED_CEILING: dict[str, int] = {
    "free": 50,
    "plus": 100,
    "pro": 300,
    "ultra": 600,
}


def default_max_sources(plan: str) -> int:
    return PLAN_CAPS.get(plan.lower(), PLAN_CAPS["free"])


def _parse_upload_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _matches_keywords(video: dict[str, Any], patterns: list[re.Pattern[str]]) -> bool:
    if not patterns:
        return True
    haystack = f"{video.get('title') or ''}\n{video.get('description') or ''}"
    return any(p.search(haystack) for p in patterns)


def select(
    videos: list[dict[str, Any]],
    *,
    days: int | None = 365,
    limit: int = 30,
    max_sources: int = 45,
    keywords: list[str] | None = None,
    full_channel: bool = False,
) -> list[dict[str, Any]]:
    """Filter, rank, and cap a list of video metadata dicts.

    - full_channel=True disables the time window.
    - keywords (regex fragments, OR-matched) are applied to title + description.
    - Sorts by view_count desc (None treated as 0), then takes min(limit, max_sources).
    """
    patterns: list[re.Pattern[str]] = []
    if keywords:
        for k in keywords:
            k = k.strip()
            if k:
                patterns.append(re.compile(k, re.IGNORECASE))

    cutoff: dt.date | None = None
    if not full_channel and days is not None:
        cutoff = dt.date.today() - dt.timedelta(days=days)

    filtered: list[dict[str, Any]] = []
    for v in videos:
        if cutoff is not None:
            upload = _parse_upload_date(v.get("upload_date"))
            if upload is None or upload < cutoff:
                continue
        if patterns and not _matches_keywords(v, patterns):
            continue
        filtered.append(v)

    filtered.sort(key=lambda v: v.get("view_count") or 0, reverse=True)

    hard_cap = min(limit, max_sources)
    return filtered[:hard_cap]


def write_selected_meta(selected: list[dict[str, Any]], path: pathlib.Path) -> None:
    """Markdown digest of the selection, parity with distribution-plan's selected_meta.md."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Selected sources ({len(selected)})\n"]
    for i, v in enumerate(selected, 1):
        title = v.get("title") or "(untitled)"
        url = v.get("webpage_url") or ""
        views = v.get("view_count")
        upload = v.get("upload_date") or ""
        views_s = f"{views:,}" if isinstance(views, int) else "?"
        lines.append(f"{i}. [{title}]({url}) — {views_s} views — {upload}")
    path.write_text("\n".join(lines) + "\n")
