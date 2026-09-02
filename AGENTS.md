# media-to-notebooklm repository guide

This repository builds NotebookLM notebooks from YouTube channels, playlists, and
videos. The deterministic entry point is `bin/yt-to-nblm`. It is meant to be run
from a Claude Code on the web session or directly on a workstation.

## Runtime and setup

- Use Python 3.11 or newer and the repository virtual environment.
- `make venv` creates `.venv` and installs the project with `pip install -e .`.
  The `bin/yt-to-nblm` shim requires `.venv/bin/python` and adds `src/` to its
  import path. `make install` reinstalls into an existing venv.
- In Claude Code on the web, `.claude/hooks/session-start.sh` does the same
  bootstrap automatically on every fresh container.
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
- Results are cached by normalized URL and detected mode in `out/`. Use
  `--force-new` when a deliberate fresh notebook is wanted; this bypasses the
  cache. The cache does not survive a fresh cloud container.
- `--recent-cap` controls the flat metadata pass. `--mode` overrides URL
  detection when the prompt pack needs a different source mode.

## Configuration

- `NBLM_PLAN` selects the default source cap. `NBLM_BIN` optionally selects an
  absolute `notebooklm` executable; otherwise the project resolves it from the
  virtual environment or `PATH`.
- `NOTEBOOKLM_AUTH_JSON` carries the Playwright storage state inline for hosts
  that cannot run an interactive login (cloud sessions). It is set on the
  Claude Code environment, never in the repository.
- `YT_DLP_PROXY` (or `HTTPS_PROXY`) routes yt-dlp through a proxy when the
  host network blocks YouTube.
- `make update-ytdlp` upgrades the yt-dlp dependency. Use it when extraction
  errors indicate an outdated extractor, then rerun a bounded dry-run first.

## Authentication and effect boundaries

- `notebooklm-py` authenticates with a Playwright storage state, either from
  `~/.notebooklm/storage_state.json` or from `NOTEBOOKLM_AUTH_JSON`. Only
  `notebooklm login` on a machine with a browser can create it. Do not copy
  that file or any token into the repository, logs, reports, or chat.
- Cookie expiry surfaces as an auth error on `create`, `source add`, or `ask`.
  The fix is a fresh login on a workstation and a re-export of the env var;
  there is no non-interactive renewal.
- A non-dry CLI run creates or changes NotebookLM resources. Confirm the URL
  scope, source cap, name, and prompt mode before such a run; use `--dry-run`
  to inspect selection.
- Treat NotebookLM login, notebook creation, source additions, and prompt asks
  as external effects, not tests.

## Repository checks

Run `make check` (`git diff --check`, `python3 -m compileall -q src`, and the
CLI help check) after dependencies are installed. Confirm
`git diff --name-only` contains only the files authorized for the task; do not
broaden a docs-only change into code, configuration, generated output,
credentials, or service state.
