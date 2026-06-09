"""Local URL → notebook cache.

Avoids creating duplicate NotebookLM notebooks when the same YouTube URL is
processed multiple times. Cache key is (mode, normalized URL); cache value
stores the notebook id, name, source map (for citation linkification), and
timestamp.

Cache file lives at `<repo>/out/.notebook-cache.json` so it's gitignored
together with the rest of `out/`.
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

CACHE_FILE_NAME = ".notebook-cache.json"


def normalize_url(url: str) -> str:
    """Canonicalize a YouTube URL so cosmetically-different forms hash equal.

    - https → forced scheme
    - hostname collapsed (m./www./youtube.com → youtube.com)
    - query params filtered to only the ones that identify content (`v`, `list`)
      (drops `si`, `feature`, `t`, etc. that vary between shares)
    - trailing slash removed from path
    """
    p = urlparse(url.strip())
    host = (p.netloc or "").lower()
    if host in ("m.youtube.com", "www.youtube.com"):
        host = "youtube.com"
    qs = parse_qs(p.query)
    keep = {k: qs[k][0] for k in ("v", "list") if k in qs}
    path = p.path.rstrip("/")
    return urlunparse(("https", host, path, "", urlencode(keep), ""))


def _key(url: str, mode: str) -> str:
    return f"{mode}::{normalize_url(url)}"


def load(cache_dir: pathlib.Path) -> dict[str, Any]:
    p = cache_dir / CACHE_FILE_NAME
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save(cache_dir: pathlib.Path, data: dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / CACHE_FILE_NAME).write_text(json.dumps(data, indent=2) + "\n")


def get(cache_dir: pathlib.Path, url: str, mode: str) -> dict[str, Any] | None:
    data = load(cache_dir)
    return data.get(_key(url, mode))


def put(
    cache_dir: pathlib.Path,
    url: str,
    mode: str,
    *,
    notebook_id: str,
    name: str,
    source_map: dict[str, str],
) -> None:
    data = load(cache_dir)
    data[_key(url, mode)] = {
        "notebook_id": notebook_id,
        "name": name,
        "source_map": source_map,
        "source_count": len(source_map),
        "url": url,
        "normalized_url": normalize_url(url),
        "created_at": int(time.time()),
    }
    save(cache_dir, data)


def humanize_age(epoch_seconds: int) -> str:
    delta = max(0, int(time.time()) - epoch_seconds)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"
