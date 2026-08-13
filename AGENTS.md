# media-to-notebooklm repository guide

This repository builds NotebookLM notebooks from YouTube channels, playlists, and
videos. The deterministic entry point is `bin/yt-to-nblm`; the Telegram bridge in
`telegram-bridge/bot.py` invokes that same CLI for `/pull` requests.

## Runtime and setup

- Use Python 3.11 or newer and the repository virtual environment.
- For the complete project, run `python3 -m venv .venv` and then
  `.venv/bin/pip install -e .`. The `bin/yt-to-nblm` shim requires
  `.venv/bin/python` and adds `src/` to its import path.
- `make venv` and `make install-deps` install the bridge's basic dependencies;
  install the project with `pip install -e .` when the `notebooklm` CLI and all
  `pyproject.toml` dependencies are needed.
- Generated run artifacts belong under `out/<slug>/`; `.venv/`, `.env`, and
  `out/` are local and must not be committed.

## CLI

Run:

```bash
bin/yt-to-nblm <youtube-url> [--days N] [--limit N] [--full-channel]
bin/yt-to-nblm <youtube-url> [--filter "keyword,other"] [--name "Notebook"]
bin/yt-to-nblm <youtube-url> [--max-sources N] [--no-prompts] [--dry-run]
```

Additional options are `--recent-cap N`, `--ask "question"`,
`--mode video|playlist|channel`, `--force-new`, and `--verbose`. Use
`bin/yt-to-nblm --help` for Click's complete interface.

- A video URL is treated as one video. A channel or playlist uses the default
  365-day window, 30-video selection limit, and view-count ordering.
- `--full-channel` removes the time window; `--filter` is an OR-match against
  title and description. `--max-sources` overrides the `NBLM_PLAN` default.
- The plan defaults are `free=45`, `plus=95`, `pro=295`, and `ultra=595`
  sources. Keep the declared cap within the NotebookLM plan quota.
- `--dry-run` fetches and selects only; it makes no NotebookLM calls. Without
  it, the CLI may create a notebook, add sources, and run the default prompt
  pack. `--no-prompts` skips that pack; `--ask` replaces it with one question.
- Results are cached by normalized URL and detected mode. Use `--force-new`
  when a deliberate fresh notebook is wanted; this bypasses the cache.
- `--recent-cap` controls the flat metadata pass. `--mode` overrides URL
  detection when the prompt pack needs a different source mode.

## Telegram bridge and host services

- `make run-bot` runs the bot in the foreground. `/pull <url> [flags]` mirrors
  the CLI; `/start`, `/help`, `/repair`, `/status`, and `/whoami` are bridge
  commands. Free-form text is handled by the configured Claude bridge in
  `telegram-bridge/bot.py`.
- `TELEGRAM_ALLOWED_IDS` is a comma-separated numeric allowlist; empty means
  every user is rejected. `TELEGRAM_BOT_TOKEN` stays in the local `.env` only.
- `NBLM_PLAN` selects the default source cap. `NBLM_BIN` optionally selects an
  absolute `notebooklm` executable; otherwise the project resolves it from the
  virtual environment or `PATH`.
- `make install-launchd`, `make reload-launchd`, and `make uninstall-launchd`
  change the macOS launchd service. Review the generated plist and host effect
  before running them. `make bot-status` is the read-only status/log check.
- `make update-ytdlp` upgrades the yt-dlp dependency. Use it when extraction
  errors indicate an outdated extractor, then rerun a bounded dry-run first.

## Authentication and effect boundaries

- `notebooklm-py` uses the host-bound Playwright session at
  `~/.notebooklm/storage_state.json`; authenticate interactively with
  `notebooklm login` on the Mac that owns the session. Do not copy that file or
  any token into the repository, logs, reports, or chat.
- `.env` contains bot and plan configuration. Use `.env.example` as the shape,
  keep real values local, and do not print them while diagnosing the bridge.
- A non-dry CLI run creates or changes NotebookLM resources and may send the
  resulting digest through Telegram. Confirm the URL scope, source cap, name,
  and prompt mode before such a run; use `--dry-run` to inspect selection.
- Treat launchd changes, Telegram sends, NotebookLM login, notebook creation,
  source additions, and prompt asks as host or external effects, not tests.

## Repository checks

For instruction or documentation changes, run `git diff --check`,
`python3 -m compileall -q src telegram-bridge`, and the CLI help check after
dependencies are installed. Confirm `git diff --name-only` contains only the
files authorized for the task; do not broaden a docs-only change into code,
configuration, generated output, credentials, or service state.
