"""notebooklm-py CLI wrapper: create notebook, add sources, ask questions."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any

log = logging.getLogger(__name__)


def _resolve_bin() -> str:
    """Locate notebooklm binary. Priority:
    1. NBLM_BIN env var
    2. Current Python venv (sys.executable's directory)
    3. distribution-plan venv (legacy fallback)
    4. PATH lookup via shutil.which
    5. literal "notebooklm" (will fail loudly with FileNotFoundError)
    """
    env_bin = os.environ.get("NBLM_BIN")
    if env_bin and pathlib.Path(env_bin).exists():
        return env_bin
    venv_bin = pathlib.Path(sys.executable).parent / "notebooklm"
    if venv_bin.exists():
        return str(venv_bin)
    fallback = pathlib.Path.home() / "AI_Projects/distribution-plan/.venv/bin/notebooklm"
    if fallback.exists():
        return str(fallback)
    on_path = shutil.which("notebooklm")
    if on_path:
        return on_path
    return "notebooklm"


def _run(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    cmd = [_resolve_bin(), *args]
    log.debug("run: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _extract_id(stdout: str) -> str | None:
    """Parse a notebook/source UUID from either --json output or plain text fallback."""
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("id", "notebook_id", "source_id"):
                val = data.get(key)
                if isinstance(val, str) and _UUID_RE.fullmatch(val):
                    return val
            nb = data.get("notebook")
            if isinstance(nb, dict) and isinstance(nb.get("id"), str):
                return nb["id"]
    except json.JSONDecodeError:
        pass
    m = _UUID_RE.search(text)
    return m.group(0) if m else None


def create_notebook(title: str) -> str:
    """Create a notebook, return its UUID. Raises RuntimeError on failure."""
    proc = _run(["create", title, "--json"])
    if proc.returncode != 0:
        raise RuntimeError(f"notebooklm create failed: {proc.stderr.strip() or proc.stdout.strip()}")
    nb_id = _extract_id(proc.stdout)
    if not nb_id:
        raise RuntimeError(f"could not parse notebook id from: {proc.stdout!r}")
    log.info("created notebook %s", nb_id)
    return nb_id


def add_source(notebook_id: str, url: str, *, retries: int = 1) -> str | None:
    """Add a URL source. Returns source UUID or None on failure after retries."""
    last_err = ""
    for attempt in range(retries + 1):
        proc = _run(["source", "add", url, "-n", notebook_id, "--json"], timeout=180)
        if proc.returncode == 0:
            sid = _extract_id(proc.stdout)
            if sid:
                return sid
            log.warning("source add succeeded but no id parsed: %s", proc.stdout[:200])
            return None
        last_err = proc.stderr.strip() or proc.stdout.strip()
        log.warning("source add failed (attempt %d): %s", attempt + 1, last_err[:200])
        if attempt < retries:
            time.sleep(2)
    log.error("giving up on %s: %s", url, last_err[:200])
    return None


def add_sources_bulk(
    notebook_id: str,
    urls: list[str],
    *,
    throttle_seconds: float = 1.0,
) -> tuple[dict[str, str], list[str]]:
    """Add many sources with throttle.

    Returns (source_id → url map, failed_urls). The map lets callers rewrite
    NotebookLM `[N]` citations into linked YouTube URLs after asking questions.
    """
    source_map: dict[str, str] = {}
    fail: list[str] = []
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(throttle_seconds)
        sid = add_source(notebook_id, url)
        if sid:
            source_map[sid] = url
        else:
            fail.append(url)
    return source_map, fail


def ask(notebook_id: str, question: str, *, timeout: int = 240) -> dict[str, Any]:
    """Ask a question. Returns parsed JSON dict on success, or {'answer': stdout} as fallback."""
    proc = _run(["ask", question, "-n", notebook_id, "--json"], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"notebooklm ask failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"answer": proc.stdout.strip()}


def notebook_url(notebook_id: str) -> str:
    return f"https://notebooklm.google.com/notebook/{notebook_id}"
