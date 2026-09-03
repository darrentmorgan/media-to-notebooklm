"""Telegram bridge for media-to-notebooklm.

Two handlers:
- /pull <url> [flags]   → direct call to bin/yt-to-nblm, streams progress, replies with notebook URL + digest
- any other text        → forwarded to `claude -p` running in the project dir;
                          the locked-down telegram-bridge/claude-settings.json
                          constrains what Claude can do.

The bridge is optional. Install its dependency with `pip install -e '.[telegram]'`
(or `make install-telegram`) and run it with `make run-bot`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import shlex
import shutil
import sys
import time
import traceback
from typing import Any

from dotenv import load_dotenv

try:
    from telegram import Update
    from telegram.constants import ChatAction
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:  # pragma: no cover - optional extra not installed
    sys.stderr.write(
        "error: python-telegram-bot is not installed. The Telegram bridge is optional;\n"
        "       install it with: pip install -e '.[telegram]'  (or: make install-telegram)\n"
    )
    raise SystemExit(1)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
YT_BIN = REPO_ROOT / "bin" / "yt-to-nblm"
CLAUDE_SETTINGS = REPO_ROOT / "telegram-bridge" / "claude-settings.json"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
LOG_DIR = REPO_ROOT / "out"
LOG_FILE = LOG_DIR / "bot.log"

TG_CHUNK = 3500  # leave headroom under the 4096 Telegram limit


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


log = logging.getLogger("bot")


def _load_allowlist() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_IDS", "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError:
                log.warning("ignoring invalid id in TELEGRAM_ALLOWED_IDS: %r", part)
    return out


ALLOWED_IDS: set[int] = set()


def _authorized(update: Update) -> bool:
    if not ALLOWED_IDS:
        return False
    user = update.effective_user
    if user is None:
        return False
    return user.id in ALLOWED_IDS


async def _reject(update: Update) -> None:
    user = update.effective_user
    uid = user.id if user else "?"
    uname = user.username if user else "?"
    log.warning("rejected update from unauthorized user id=%s username=%s", uid, uname)


async def _send_chunked(update: Update, text: str) -> None:
    if not text:
        return
    for i in range(0, len(text), TG_CHUNK):
        await update.effective_message.reply_text(text[i : i + TG_CHUNK])


def _split_for_markdown(text: str, limit: int = TG_CHUNK) -> list[str]:
    """Split text into ≤limit-char chunks at safe boundaries (paragraph → line).
    Telegram rejects Markdown messages where a `*` / `[` / ``` is unbalanced,
    so we prefer splitting on blank lines, then single newlines.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Try paragraph break first
        cut = remaining.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_markdown_chunked(update: Update, text: str) -> None:
    """Send long Markdown text in multiple messages, with a plain-text fallback
    per chunk if Markdown parsing fails (unbalanced ** / [ etc).
    """
    if not text:
        return
    for chunk in _split_for_markdown(text):
        try:
            await update.effective_message.reply_text(
                chunk, parse_mode="Markdown", disable_web_page_preview=True
            )
        except Exception as e:
            log.debug("markdown send failed (%s), falling back to plain", e)
            await update.effective_message.reply_text(chunk, disable_web_page_preview=True)


STATUS_INTERVAL = 5.0  # seconds between status edits
STATUS_MAX_LEN = 3500   # Telegram message body limit headroom


async def _stream_subprocess(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    argv: list[str],
    *,
    cwd: pathlib.Path,
    line_transformer: "callable[[str, str], tuple[str, str]] | None" = None,
) -> tuple[int, str, str]:
    """Run a subprocess and stream live progress to Telegram.

    Reads stdout/stderr line-by-line, edits a status message every
    STATUS_INTERVAL seconds with elapsed time + last output line so the user
    sees the bot is alive and what it's doing.
    Returns (returncode, full_stdout, full_stderr).
    """
    log.info("spawn: %s (cwd=%s)", " ".join(shlex.quote(a) for a in argv), cwd)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    last_activity = {"line": "", "src": "", "ts": time.monotonic()}
    current_step = {"text": ""}  # latest "[N/4]" stage line from stdout
    started = time.monotonic()

    import re as _re_local
    _STEP_RE = _re_local.compile(r"^\[\d+/\d+\]")
    _STAGE_RE = _re_local.compile(r"(asking|creating|adding|fetching|enriching|selecting)", _re_local.IGNORECASE)
    # Skip lines that are pure noise in the status display (closing JSON braces,
    # empty brackets, ANSI artifacts). Keep them in the full buffer.
    _NOISE_RE = _re_local.compile(r"^[\s\}\]\{\[,]*$")

    program = pathlib.Path(argv[0]).name
    try:
        status_msg = await update.effective_message.reply_text(f"⏳ {program} starting…")
    except Exception:
        status_msg = None

    async def reader(stream: asyncio.StreamReader, sink: list[str], label: str) -> None:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").rstrip("\r\n")
            sink.append(line)
            display = line
            if line_transformer is not None:
                try:
                    label_out, display = line_transformer(label, line)
                    label = label_out or label
                except Exception:
                    display = line
            stripped = (display or "").strip()
            # Capture pipeline step markers from stdout for the status header
            if label == "out" and _STEP_RE.match(stripped):
                current_step["text"] = stripped
            elif label == "out" and _STAGE_RE.search(stripped) and stripped.startswith(" "):
                # Sub-step like "      adding 30 sources…" or "      asking: overview"
                current_step["text"] = stripped.strip()
            # Update last_activity only for meaningful lines
            if stripped and not _NOISE_RE.match(stripped):
                last_activity["line"] = stripped
                last_activity["src"] = label
                last_activity["ts"] = time.monotonic()

    async def updater() -> None:
        prev_text = ""
        try:
            while True:
                await asyncio.sleep(STATUS_INTERVAL)
                try:
                    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
                except Exception:
                    pass
                if status_msg is None:
                    continue
                elapsed = int(time.monotonic() - started)
                idle = int(time.monotonic() - last_activity["ts"])
                step = current_step["text"]
                tail = last_activity["line"] or "(no output yet)"
                src = last_activity["src"] or "—"
                tail_disp = (tail[:240] + "…") if len(tail) > 240 else tail
                # Header shows current pipeline stage prominently; details below
                lines = [f"⏳ {program} · {elapsed}s elapsed · {idle}s idle"]
                if step:
                    lines.append(f"📍 {step}")
                lines.append(f"[{src}] {tail_disp}")
                # Friendly nudge if it looks stuck on a known-slow ask step
                if idle > 90 and "asking" in step.lower():
                    lines.append("(NotebookLM ask calls take 30-90s each — patience)")
                text = "\n".join(lines)[:STATUS_MAX_LEN]
                if text != prev_text:
                    try:
                        await status_msg.edit_text(text)
                        prev_text = text
                    except Exception as e:
                        # ignore "message is not modified" + transient
                        if "not modified" not in str(e).lower():
                            log.debug("status edit failed: %s", e)
        except asyncio.CancelledError:
            return

    tasks = [
        asyncio.create_task(reader(proc.stdout, stdout_lines, "out")),
        asyncio.create_task(reader(proc.stderr, stderr_lines, "err")),
    ]
    updater_task = asyncio.create_task(updater())

    rc = await proc.wait()
    await asyncio.gather(*tasks, return_exceptions=True)
    updater_task.cancel()
    try:
        await updater_task
    except asyncio.CancelledError:
        pass

    # Final status edit so user sees completion in the same message thread
    if status_msg is not None:
        elapsed = int(time.monotonic() - started)
        final = f"{'✅' if rc == 0 else '❌'} {program} exit {rc} · {elapsed}s"
        try:
            await status_msg.edit_text(final)
        except Exception:
            pass

    return rc, "\n".join(stdout_lines), "\n".join(stderr_lines)


def _extract_notebook_url(stdout: str) -> str | None:
    for line in stdout.splitlines():
        line = line.strip()
        if "notebooklm.google.com/notebook/" in line:
            for token in line.split():
                if "notebooklm.google.com/notebook/" in token:
                    return token
    return None


def _extract_artifacts_dir(stdout: str) -> pathlib.Path | None:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("artifacts:"):
            path = line.split(":", 1)[1].strip()
            return pathlib.Path(path)
    return None


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bypasses allowlist intentionally — used for bootstrap to discover one's own id."""
    user = update.effective_user
    if user is None:
        return
    await update.effective_message.reply_text(
        f"id: {user.id}\nusername: @{user.username or '(none)'}\n\n"
        f"to authorize, add this id to TELEGRAM_ALLOWED_IDS in .env on the host."
    )
    log.info("whoami: id=%s username=%s", user.id, user.username)


_USAGE_TEXT = (
    "📓 *media-to-notebooklm bridge*\n\n"
    "*Send a YouTube URL.* URL type is auto-detected and picks the prompt pack:\n"
    "  • *single video* (`youtu.be/abc`, `watch?v=...`)\n"
    "      → core idea · actionable takeaways · counterpoints\n"
    "  • *playlist* (`/playlist?list=...`)\n"
    "      → through-line · top ideas · synthesis\n"
    "  • *channel* (`@handle`, `/channel/...`, `/videos`)\n"
    "      → overview · top ideas · outlier video\n\n"
    "*Add your own question after the URL* to override the default pack:\n"
    "```\n"
    "https://youtu.be/abc123  what is the speaker's central thesis?\n"
    "https://youtube.com/@foo  what's the best video for a beginner?\n"
    "```\n"
    "Your question runs as the *only* ask — replaces the default 3-prompt pack.\n\n"
    "*Timing*\n"
    "  • single video: ~1-2 min · channel (30 videos): ~5-7 min\n"
    "  • idle counter > 60s during `asking:` is *normal* (NotebookLM is slow).\n"
    "  • wait until you see ✅ or ❌.\n\n"
    "*Commands*\n"
    "  /pull <url> [flags]   explicit flags (see /help)\n"
    "  /new <url>             force a fresh notebook (bypass cache)\n"
    "  /forget <url>          drop URL from cache (next pull rebuilds)\n"
    "  /help                  flag reference\n"
    "  /repair                reinstall deps if something breaks\n"
    "  /whoami                your Telegram id\n\n"
    "Same URL sent twice = same notebook reused (♻️). No more duplicates."
)

_HELP_TEXT = (
    "*Flags for /pull*\n"
    "```\n"
    "--days N         time window (default 365)\n"
    "--limit N        max videos (default 30)\n"
    "--full-channel   ignore time window\n"
    "--filter a,b,c   keyword regex OR-match\n"
    "--name \"...\"     notebook name override\n"
    "--max-sources N  override plan cap\n"
    "--no-prompts     skip digest pack\n"
    "--dry-run        print selection only\n"
    "--ask \"...\"      custom question (replaces default pack)\n"
    "--mode TYPE      force video|playlist|channel (else auto)\n"
    "```\n"
    "*Examples*\n"
    "```\n"
    "/pull https://youtube.com/@foo\n"
    "/pull https://youtube.com/@foo --days 90 --limit 20\n"
    "/pull https://youtube.com/@foo --full-channel --filter ai,agents\n"
    "/pull https://youtu.be/abc123 --ask \"explain the framework in detail\"\n"
    "```"
)


async def _send_md(update: Update, text: str) -> None:
    try:
        await update.effective_message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        # Markdown parse failure (special chars in URL etc) — fall back to plain
        await update.effective_message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _reject(update)
    await _send_md(update, _USAGE_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _reject(update)
    await _send_md(update, _HELP_TEXT)


def _demangle_arg(s: str) -> str:
    """Reverse Telegram's smart-typography mangling so CLI flags parse correctly.

    iOS/iPadOS Telegram auto-replaces consecutive dashes:
      `--`  →  `—` (em dash, U+2014)
      `-`   →  `–` (en dash, U+2013) in some contexts

    A user typing `--force-new` sees their message become `—force-new`, which
    yt-to-nblm then rejects as an unknown argument. Map em → "--" and standalone
    en → "-" only when the token looks like a flag (starts with the dash).
    """
    if not s:
        return s
    if s.startswith("—"):
        s = "--" + s[1:]
    elif s.startswith("–"):
        s = "-" + s[1:]
    # Also handle internal dashes inside flag values (rare but possible)
    return s.replace("—", "--").replace("–", "-") if s.startswith("-") else s


def _demangle_args(args: list[str]) -> list[str]:
    return [_demangle_arg(a) for a in args]


def _coalesce_ask(args: list[str]) -> list[str]:
    """If `--ask` appears, treat everything after it as a single value.

    Telegram's CommandHandler whitespace-splits args and doesn't honor quotes,
    so `/pull <url> --ask what does X mean?` would otherwise deliver
    `--ask=what` plus four orphan positional tokens. Glue them back.
    Assumes --ask is the LAST flag (common usage); anything after is the ask.
    """
    try:
        i = args.index("--ask")
    except ValueError:
        return args
    if i + 1 >= len(args):
        return args
    return [*args[: i + 1], " ".join(args[i + 1 :])]


def _normalize_pull_args(raw: list[str] | None) -> list[str]:
    return _coalesce_ask(_demangle_args(raw or []))


async def cmd_pull(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _reject(update)

    args = _normalize_pull_args(context.args)
    if not args:
        await update.effective_message.reply_text("usage: /pull <url> [flags]")
        return

    url = args[0]
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.effective_message.reply_text("first arg must be an http(s) URL")
        return

    flags = args[1:]
    await _run_yt(update, context, url, flags=flags)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-create a fresh notebook even if the URL is in the cache."""
    if not _authorized(update):
        return await _reject(update)

    args = _normalize_pull_args(context.args)
    if not args:
        await update.effective_message.reply_text("usage: /new <url> [flags]  (forces fresh notebook, bypasses cache)")
        return

    url = args[0]
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.effective_message.reply_text("first arg must be an http(s) URL")
        return

    flags = ["--force-new", *args[1:]]
    await _run_yt(update, context, url, flags=flags)


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a URL from the cache so the next request rebuilds the notebook."""
    if not _authorized(update):
        return await _reject(update)

    args = _normalize_pull_args(context.args)
    if not args:
        await update.effective_message.reply_text("usage: /forget <url>")
        return

    url = args[0]
    try:
        import json as _json
        cache_path = REPO_ROOT / "out" / ".notebook-cache.json"
        if not cache_path.exists():
            await update.effective_message.reply_text("cache is empty")
            return
        data = _json.loads(cache_path.read_text())
        # Match any cache key whose stored url or normalized_url matches
        targets = [k for k, v in data.items()
                   if v.get("url") == url or v.get("normalized_url", "").rstrip("/") in url]
        if not targets:
            await update.effective_message.reply_text(f"no cache entry matches `{url}`")
            return
        for k in targets:
            data.pop(k, None)
        cache_path.write_text(_json.dumps(data, indent=2) + "\n")
        await update.effective_message.reply_text(f"forgot {len(targets)} cache entr{'y' if len(targets) == 1 else 'ies'}")
    except Exception as e:
        await update.effective_message.reply_text(f"forget failed: {e}")


import re as _re
_YT_URL_RE = _re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/[^\s]+|youtu\.be/[^\s]+)",
    _re.IGNORECASE,
)


def _find_youtube_url(text: str) -> str | None:
    m = _YT_URL_RE.search(text)
    return m.group(0) if m else None


def _claude_available() -> bool:
    """Best-effort check: does `claude -p` have a working OAuth session?
    We can't fully verify without a network call, but check for keychain cred
    presence + binary existence. False negatives okay; false positives lead to
    same opaque error users already see.
    """
    if not pathlib.Path(CLAUDE_BIN).exists():
        return False
    # Keychain item presence (macOS): cheap, no auth prompt
    try:
        import subprocess
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return True  # assume available, let the call fail with real error


async def _run_yt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    flags: list[str],
) -> None:
    """Run bin/yt-to-nblm with streaming progress + auto-repair + reply."""
    argv = [str(YT_BIN), url, *flags]
    await update.effective_message.reply_text(f"building notebook from {url}…")
    rc, stdout, stderr = await _stream_subprocess(update, context, argv, cwd=REPO_ROOT)

    if rc != 0 and _looks_like_dep_failure(stderr + stdout):
        log.warning("yt: dep failure detected — auto-repairing and retrying once")
        await update.effective_message.reply_text("🔧 missing dep detected — repairing then retrying…")
        repair_lines = await asyncio.to_thread(_repair, "yt-retry")
        await _send_chunked(update, "\n".join(repair_lines))
        rc, stdout, stderr = await _stream_subprocess(update, context, argv, cwd=REPO_ROOT)

    if rc != 0:
        tail = "\n".join(stderr.splitlines()[-20:]) or "(no stderr)"
        await _send_chunked(update, f"❌ yt-to-nblm failed (exit {rc}):\n{tail}")
        return

    nb_url = _extract_notebook_url(stdout)
    artifacts = _extract_artifacts_dir(stdout)
    was_cached = "[cache hit]" in stdout

    # 1. Send the notebook URL on its own as a plain message (always clickable).
    head = nb_url or "(no notebook URL detected in output — check logs)"
    if was_cached:
        head = "♻️ reused existing notebook (pass `/pull <url> --force-new` to rebuild)\n" + head
    await update.effective_message.reply_text(head, disable_web_page_preview=True)

    # 2. Send the full digest as Markdown chunks so [N](url) citations are
    #    clickable. No more 1500-char truncation.
    if artifacts is not None:
        digest_path = artifacts / "digest.md"
        if digest_path.exists():
            digest_body = digest_path.read_text()
            await _send_markdown_chunked(update, digest_body)


def _make_claude_transformer(final_holder: dict[str, str]):
    """Build a line transformer for `claude -p --output-format stream-json --verbose`.

    Parses each ND-JSON line and returns (label, display_text) so the Telegram
    status message shows human-readable progress instead of raw JSON. Captures
    the final assistant result text in `final_holder["text"]`.
    """
    import json as _json

    def _txt_from_content(content) -> str:
        parts: list[str] = []
        if isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                t = blk.get("type")
                if t == "text":
                    parts.append(blk.get("text", ""))
                elif t == "tool_use":
                    name = blk.get("name", "tool")
                    parts.append(f"🔧 tool: {name}")
                elif t == "tool_result":
                    parts.append("↩ tool_result")
        elif isinstance(content, str):
            parts.append(content)
        return " ".join(p for p in parts if p).strip()

    def transform(label: str, line: str) -> tuple[str, str]:
        line = line.strip()
        if not line or not line.startswith("{"):
            return label, line
        try:
            ev = _json.loads(line)
        except Exception:
            return label, line
        et = ev.get("type")
        if et == "system":
            sub = ev.get("subtype", "system")
            return "claude", f"⚙ {sub}"
        if et == "assistant":
            msg = ev.get("message", {})
            disp = _txt_from_content(msg.get("content"))
            return "assistant", disp[:400] if disp else "(assistant)"
        if et == "user":
            msg = ev.get("message", {})
            disp = _txt_from_content(msg.get("content"))
            return "tool", disp[:400] if disp else "(user/tool)"
        if et == "result":
            res = ev.get("result") or ""
            if isinstance(res, str) and res:
                final_holder["text"] = res
            return "claude", f"✅ done ({ev.get('duration_ms', '?')}ms)"
        return label, line[:200]

    return transform


async def on_freeform(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _reject(update)

    text = (update.effective_message.text or "").strip()
    if not text:
        return

    # Short-circuit: if the message contains a YouTube URL, route directly to
    # yt-to-nblm with defaults. Avoids depending on `claude -p` (which has no
    # OAuth session under launchd) for the common case of "just post a link".
    yt_url = _find_youtube_url(text)
    if yt_url:
        # Anything in the message that isn't the URL is treated as the user's
        # custom question to ask the resulting notebook. Single ask replaces
        # the default prompt pack.
        leftover = _YT_URL_RE.sub("", text).strip()
        # Trim common joiner punctuation users add around URLs
        leftover = leftover.strip(" \t\n\r,;:|—-")
        flags: list[str] = []
        if leftover:
            flags = ["--ask", leftover]
            log.info("freeform: YouTube URL %s + custom ask %r", yt_url, leftover)
        else:
            log.info("freeform: detected YouTube URL %s, routing to yt-to-nblm", yt_url)
        await _run_yt(update, context, yt_url, flags=flags)
        return

    # Non-URL natural language: requires claude. Refuse cleanly if creds missing.
    if not _claude_available():
        await _send_md(
            update,
            "🤔 I only understand YouTube URLs right now.\n\n"
            f"Got: `{text[:200]}`\n\n" + _USAGE_TEXT,
        )
        return

    argv = [
        CLAUDE_BIN,
        "-p",
        text,
        "--settings",
        str(CLAUDE_SETTINGS),
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    final_text_holder: dict[str, str] = {"text": ""}
    transformer = _make_claude_transformer(final_text_holder)
    rc, stdout, stderr = await _stream_subprocess(
        update, context, argv, cwd=REPO_ROOT, line_transformer=transformer
    )

    if rc != 0 and _looks_like_dep_failure(stderr + stdout):
        log.warning("freeform: dep failure detected — auto-repairing and retrying once")
        await update.effective_message.reply_text("🔧 missing dep detected — repairing then retrying…")
        repair_lines = await asyncio.to_thread(_repair, "freeform-retry")
        await _send_chunked(update, "\n".join(repair_lines))
        final_text_holder["text"] = ""
        rc, stdout, stderr = await _stream_subprocess(
            update, context, argv, cwd=REPO_ROOT, line_transformer=transformer
        )

    # Prefer parsed final assistant text over raw JSON stream
    if final_text_holder["text"]:
        stdout = final_text_holder["text"]

    if rc != 0:
        tail = "\n".join(stderr.splitlines()[-20:]) or "(no stderr)"
        await _send_chunked(update, f"❌ claude -p failed (exit {rc}):\n{tail}")
        return

    await _send_chunked(update, stdout.strip() or "(empty response)")


VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"
VENV_PIP = REPO_ROOT / ".venv" / "bin" / "pip"
VENV_NOTEBOOKLM = REPO_ROOT / ".venv" / "bin" / "notebooklm"
VENV_PLAYWRIGHT = REPO_ROOT / ".venv" / "bin" / "playwright"


def _run_blocking(argv: list[str], *, cwd: pathlib.Path | None = None, timeout: int = 600) -> tuple[int, str]:
    """Synchronous subprocess for preflight. Returns (rc, combined_output)."""
    import subprocess
    try:
        r = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, (r.stdout + r.stderr)
    except Exception as e:
        return 1, f"exception: {e!r}"


def _repair(reason: str = "preflight") -> list[str]:
    """Attempt to repair the runtime: install project deps + playwright browser.
    Returns a list of human-readable status lines.
    """
    out: list[str] = []
    log.info("repair start (reason=%s)", reason)

    if not VENV_PY.exists():
        msg = f"venv missing at {VENV_PY} — manual bootstrap required: python3 -m venv .venv"
        log.error(msg)
        out.append(msg)
        return out

    # 1. Install project + deps if notebooklm CLI is missing
    if not VENV_NOTEBOOKLM.exists():
        log.info("repair: installing project (pip install -e .)")
        rc, log_text = _run_blocking([str(VENV_PIP), "install", "-e", "."], cwd=REPO_ROOT, timeout=600)
        ok = rc == 0 and VENV_NOTEBOOKLM.exists()
        out.append(f"{'✅' if ok else '❌'} pip install -e . (rc={rc})")
        if not ok:
            log.error("repair: pip install failed:\n%s", log_text[-2000:])
            return out
    else:
        out.append("✓ notebooklm CLI present")

    # 2. Install playwright chromium if browser binary missing
    if VENV_PLAYWRIGHT.exists():
        rc, log_text = _run_blocking([str(VENV_PLAYWRIGHT), "install", "chromium"], timeout=600)
        out.append(f"{'✅' if rc == 0 else '❌'} playwright install chromium (rc={rc})")
        if rc != 0:
            log.error("repair: playwright install failed:\n%s", log_text[-2000:])

    # 3. Check claude binary
    if pathlib.Path(CLAUDE_BIN).exists():
        out.append(f"✓ claude at {CLAUDE_BIN}")
    else:
        out.append(f"⚠ claude not found at {CLAUDE_BIN}")

    log.info("repair done: %s", "; ".join(out))
    return out


def _preflight() -> None:
    """Run at startup. Auto-install missing deps so user can just post a link."""
    if not VENV_NOTEBOOKLM.exists():
        log.warning("preflight: notebooklm CLI missing — running repair")
        _repair(reason="startup-missing-notebooklm")
    else:
        log.info("preflight ok: notebooklm=%s claude=%s", VENV_NOTEBOOKLM, CLAUDE_BIN)


_DEP_ERROR_HINTS = (
    "ModuleNotFoundError",
    "No module named",
    "command not found",
    "No such file or directory",
    "playwright._impl._errors.Error: Executable doesn't exist",
    "BrowserType.launch",
)


def _looks_like_dep_failure(text: str) -> bool:
    return any(h in text for h in _DEP_ERROR_HINTS)


async def cmd_repair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _reject(update)
    await update.effective_message.reply_text("🔧 running repair…")
    lines = await asyncio.to_thread(_repair, "manual")
    await _send_chunked(update, "\n".join(lines))


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    log.exception("unhandled error in handler", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        tb = "".join(traceback.format_exception_only(type(err), err)).strip()
        try:
            await update.effective_message.reply_text(f"❌ bot error: {tb[:500]}")
        except Exception:
            pass


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    _setup_logging()

    global ALLOWED_IDS
    ALLOWED_IDS = _load_allowlist()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN missing in .env", file=sys.stderr)
        sys.exit(1)

    if not ALLOWED_IDS:
        log.warning(
            "TELEGRAM_ALLOWED_IDS is empty — the bot will reject every message. "
            "Populate it with your numeric Telegram user id (get it from @userinfobot) and restart."
        )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pull", cmd_pull))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("repair", cmd_repair))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_freeform))
    app.add_error_handler(_on_error)

    _preflight()
    log.info("bot starting; allowlist size=%d (claude_bin=%s)", len(ALLOWED_IDS), CLAUDE_BIN)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
