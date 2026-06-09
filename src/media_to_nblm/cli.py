"""yt-to-nblm command-line entrypoint."""
from __future__ import annotations

import logging
import os
import pathlib
import re
import sys
from datetime import datetime

import click
from dotenv import load_dotenv

from . import cache, fetch, notebook, prompts, select

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "out"


def _slug(url: str) -> str:
    """Derive a filesystem-safe slug from a YouTube URL for artifact paths."""
    m = re.search(r"@([A-Za-z0-9_\-.]+)", url)
    if m:
        base = m.group(1)
    else:
        m = re.search(r"/(channel|c|user|playlist|watch)/?([A-Za-z0-9_\-]+)", url)
        base = m.group(2) if m else "source"
    return f"{base}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _classify_url(url: str) -> str:
    """Classify a YouTube URL into 'video' | 'playlist' | 'channel'.

    Rules:
    - youtu.be/<id>              → video
    - youtube.com/watch?v=...    → video (even if also has list=)
    - youtube.com/playlist?list= → playlist
    - bare ?list= or /playlist   → playlist
    - everything else (@handle, /channel/, /c/, /user/, /videos suffix) → channel
    """
    u = url.lower()
    if "youtu.be/" in u and "/playlist" not in u:
        return "video"
    if "/watch?" in u or "&v=" in u or "?v=" in u:
        return "video"
    if "/playlist" in u or "list=" in u:
        return "playlist"
    return "channel"


_NAME_MAX = 80  # Telegram-friendly + NotebookLM card legibility


def _truncate(s: str, n: int = _NAME_MAX) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _derive_notebook_name(selected: list[dict], slug: str) -> str:
    """Build a human-readable notebook name from the selected video set.

    - 1 video → use the video's title (truncated)
    - 2-N videos, same channel → "<Channel> — N videos · <YYYY-MM-DD>"
    - mixed channels → "<First title> +N more · <YYYY-MM-DD>"
    Falls back to the slug-based name if metadata is missing.
    """
    if not selected:
        return f"{slug} — auto"

    today = datetime.now().strftime("%Y-%m-%d")
    n = len(selected)

    if n == 1:
        title = (selected[0].get("title") or "").strip()
        if title:
            return _truncate(title)
        return f"{slug} — auto"

    channels = {(v.get("channel") or v.get("uploader") or "").strip() for v in selected}
    channels.discard("")
    if len(channels) == 1:
        channel = next(iter(channels))
        return _truncate(f"{channel} — {n} videos · {today}")

    first_title = (selected[0].get("title") or "").strip()
    if first_title:
        return _truncate(f"{first_title} +{n - 1} more · {today}")
    return f"{slug} — auto"


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("url")
@click.option("--days", type=int, default=365, show_default=True, help="Time window (ignored with --full-channel).")
@click.option("--limit", type=int, default=30, show_default=True, help="Max selected videos before source cap.")
@click.option("--full-channel", is_flag=True, help="Disable time window, pull all available (subject to --max-sources).")
@click.option("--filter", "filter_", default="", help="Comma-separated regex keywords, OR-matched against title+description.")
@click.option("--name", "name_override", default=None, help="Notebook name override.")
@click.option("--max-sources", type=int, default=None, help="Override plan-tier default cap.")
@click.option("--no-prompts", is_flag=True, help="Skip default prompt pack.")
@click.option("--dry-run", is_flag=True, help="Print selection and exit; no NotebookLM calls.")
@click.option("--recent-cap", type=int, default=200, show_default=True, help="Flat-pass upload ceiling (higher = slower but covers more history).")
@click.option("--ask", "ask_text", default=None, help="Custom question to ask the notebook. Replaces the default prompt pack.")
@click.option("--mode", "mode_override", default=None, type=click.Choice(["video", "playlist", "channel"]),
              help="Override URL-type detection (controls which default prompt pack runs).")
@click.option("--force-new", is_flag=True, help="Skip the URL cache and always create a fresh notebook.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
def main(
    url: str,
    days: int,
    limit: int,
    full_channel: bool,
    filter_: str,
    name_override: str | None,
    max_sources: int | None,
    no_prompts: bool,
    dry_run: bool,
    recent_cap: int,
    ask_text: str | None,
    mode_override: str | None,
    force_new: bool,
    verbose: bool,
) -> None:
    """Build a NotebookLM notebook from a YouTube URL (channel, playlist, or single video)."""
    load_dotenv(REPO_ROOT / ".env")
    _setup_logging(verbose)

    plan = os.environ.get("NBLM_PLAN", "free")
    cap = max_sources if max_sources is not None else select.default_max_sources(plan)
    keywords = [k.strip() for k in filter_.split(",") if k.strip()]

    mode = mode_override or _classify_url(url)
    click.echo(f"      url-type: {mode}{' (override)' if mode_override else ''}"
               + (f"  ·  custom ask: {ask_text!r}" if ask_text else ""))

    # Single-video URLs: tighten selection to just that video. yt-dlp returns 1
    # entry for a watch URL via flat-playlist, but channel-style defaults would
    # then enrich/filter pointlessly. Force --full-channel + limit=1.
    if mode == "video":
        full_channel = True
        limit = 1

    slug = _slug(url)
    work = OUT_DIR / slug
    work.mkdir(parents=True, exist_ok=True)

    # Cache check: skip the entire fetch/select/create/add pipeline if we have
    # already built a notebook for this (mode, normalized URL) pair.
    cached = None if force_new else cache.get(OUT_DIR, url, mode)
    if cached:
        nb_id = cached["notebook_id"]
        cached_name = cached.get("name") or "(cached)"
        source_map = cached.get("source_map") or {}
        nb_url = notebook.notebook_url(nb_id)
        click.echo(
            f"[cache hit] reusing notebook {nb_id}  ·  {cached_name}  "
            f"·  created {cache.humanize_age(cached.get('created_at', 0))}  "
            f"·  {len(source_map)} sources"
        )
        click.echo("            (pass --force-new to rebuild from scratch)")

        if no_prompts:
            click.echo("[4/4] skipping prompt pack (--no-prompts)")
        else:
            if ask_text:
                click.echo(f"[4/4] asking custom question on cached notebook (mode={mode})")
            else:
                click.echo(f"[4/4] re-running {mode} prompt pack on cached notebook")
            try:
                prompts.run_default_pack(
                    nb_id,
                    work / "digest.md",
                    mode=mode,
                    custom_ask=ask_text,
                    source_map=source_map,
                )
            except Exception as e:
                click.echo(f"prompts failed: {e}", err=True)

        click.echo("")
        click.echo(f"notebook: {nb_url}")
        click.echo(f"artifacts: {work}")
        return

    click.echo(f"[1/4] fetching metadata: {url}")
    flat_videos = fetch.fetch_flat(url, recent_cap=recent_cap)
    if not flat_videos:
        click.echo("no videos returned — check URL / yt-dlp version", err=True)
        sys.exit(2)
    fetch.write_jsonl(flat_videos, work / "videos_flat.jsonl")
    click.echo(f"      {len(flat_videos)} videos (flat)")

    needs_enrich = (not full_channel) or bool(keywords)
    if needs_enrich:
        flat_sorted = sorted(flat_videos, key=lambda v: v.get("view_count") or 0, reverse=True)
        pool_size = min(max(limit * 2, 30), cap * 2, len(flat_sorted))
        pool = flat_sorted[:pool_size]
        click.echo(f"      enriching top {len(pool)} by views (need upload_date / description)…")
        videos = fetch.enrich(pool)
        fetch.write_jsonl(videos, work / "videos.jsonl")
    else:
        videos = flat_videos

    click.echo(f"[2/4] selecting (plan={plan} cap={cap} limit={limit} days={days} full={full_channel})")
    selected = select.select(
        videos,
        days=days,
        limit=limit,
        max_sources=cap,
        keywords=keywords,
        full_channel=full_channel,
    )
    if not selected:
        click.echo("selection empty — widen --days or drop --filter", err=True)
        sys.exit(2)
    fetch.write_jsonl(selected, work / "selected.jsonl")
    select.write_selected_meta(selected, work / "selected_meta.md")
    click.echo(f"      {len(selected)} selected")

    if dry_run:
        click.echo("[dry-run] selection:")
        for i, v in enumerate(selected, 1):
            views = v.get("view_count") or 0
            click.echo(f"  {i:3d}. {views:>8,}  {v.get('webpage_url')}  — {v.get('title')}")
        click.echo(f"artifacts: {work}")
        return

    name = name_override or _derive_notebook_name(selected, slug)
    click.echo(f"[3/4] creating notebook: {name}")
    try:
        nb_id = notebook.create_notebook(name)
    except RuntimeError as e:
        click.echo(f"create failed: {e}", err=True)
        if "auth" in str(e).lower() or "login" in str(e).lower() or "storage" in str(e).lower():
            click.echo("hint: run `notebooklm login` on this host, then retry.", err=True)
        sys.exit(3)

    urls = [v["webpage_url"] for v in selected if v.get("webpage_url")]
    click.echo(f"      adding {len(urls)} sources…")
    source_map, fail = notebook.add_sources_bulk(nb_id, urls)
    click.echo(f"      added {len(source_map)} ok, {len(fail)} failed")
    if fail:
        (work / "failed_urls.txt").write_text("\n".join(fail) + "\n")
    # Persist source_id → url mapping for offline post-processing / citation
    # linkification by downstream consumers.
    import json as _json
    (work / "source_map.json").write_text(
        _json.dumps(source_map, indent=2) + "\n"
    )

    # Record in the URL cache so the next request for this URL reuses this
    # notebook instead of creating a duplicate. Only cache when at least one
    # source landed successfully — otherwise the notebook is effectively empty.
    if source_map:
        cache.put(
            OUT_DIR,
            url,
            mode,
            notebook_id=nb_id,
            name=name,
            source_map=source_map,
        )

    nb_url = notebook.notebook_url(nb_id)

    if no_prompts:
        click.echo(f"[4/4] skipping prompt pack (--no-prompts)")
    else:
        if ask_text:
            click.echo(f"[4/4] asking custom question (mode={mode})")
        else:
            click.echo(f"[4/4] running {mode} prompt pack")
        try:
            prompts.run_default_pack(
                nb_id,
                work / "digest.md",
                mode=mode,
                custom_ask=ask_text,
                source_map=source_map,
            )
        except Exception as e:
            click.echo(f"prompts failed: {e}", err=True)

    click.echo("")
    click.echo(f"notebook: {nb_url}")
    click.echo(f"artifacts: {work}")


if __name__ == "__main__":
    main()
