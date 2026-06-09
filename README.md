# media-to-notebooklm

Telegram-triggered NotebookLM notebook builder. Given a YouTube URL (channel, playlist, or single video), pick the top videos by views, create a NotebookLM notebook, add them as sources, and optionally run a default prompt pack — all from your phone via Telegram, executed on a Mac mini.

## Pipeline

```
YouTube URL
  └─ yt-dlp --flat-playlist          (fast: id/title/view_count)
     └─ sort by views, take candidate pool
        └─ yt-dlp (full extract)     (pool only — adds upload_date + description)
           └─ select: --days, --filter, --limit, --max-sources
              └─ notebooklm create "<name>"
                 └─ notebooklm source add <url> (throttled, 1s)
                    └─ notebooklm ask (default prompt pack)
                       └─ digest.md + notebook URL
```

## Prerequisites

- macOS host with `yt-dlp` (`brew install yt-dlp`)
- Python 3.11+
- [`notebooklm-py`](https://pypi.org/project/notebooklm-py/) authenticated on this host — see "NotebookLM auth" below
- Telegram bot token (BotFather) + your numeric Telegram user id (`@userinfobot`)
- `claude` CLI on `$PATH` (for the free-form bridge handler). Optional — `/pull` works without it.

## First-time setup

```bash
git clone https://github.com/darrentmorgan/media-to-notebooklm
cd media-to-notebooklm

# venv + Python deps
make venv

# copy and fill in .env
cp .env.example .env
$EDITOR .env
```

`.env` keys:

| key | value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_ALLOWED_IDS` | comma-separated numeric user ids allowed to use the bot. Empty = everyone rejected. |
| `NBLM_PLAN` | `free` \| `plus` \| `pro` \| `ultra` — maps to default `--max-sources` |
| `NBLM_BIN` | (optional) absolute path to the `notebooklm` executable. Auto-resolves via `$PATH` if unset. |

## NotebookLM auth

`notebooklm-py` stores a Playwright session at `~/.notebooklm/storage_state.json`. Log in once on the Mac mini:

```bash
notebooklm login          # opens Chrome; sign into the Google account that owns the notebooks
```

Re-run `notebooklm login` whenever sources stop being added with an auth error. This is host-bound — it cannot be done on a cloud VM you can't log into.

## BotFather

In Telegram, message `@BotFather`:

1. `/newbot` → name the bot (e.g. `ClawdyNotebookBot`), pick a username ending in `bot`.
2. Copy the HTTP API token → `.env` as `TELEGRAM_BOT_TOKEN`.
3. Message `@userinfobot` from your account → copy numeric id → `.env` as `TELEGRAM_ALLOWED_IDS`.

## Run the CLI

```bash
# dry-run: fetch + select only, no NotebookLM calls
bin/yt-to-nblm https://youtube.com/@ApertureThinking --limit 10 --dry-run

# full pipeline (top 30 videos of the last 365 days)
bin/yt-to-nblm https://youtube.com/@ApertureThinking

# all videos (still capped by plan-tier max)
bin/yt-to-nblm https://youtube.com/@ApertureThinking --full-channel

# only videos mentioning keywords in title or description
bin/yt-to-nblm https://youtube.com/@SomeChannel --filter "ai,agents,llm"

# single video
bin/yt-to-nblm "https://www.youtube.com/watch?v=<id>" --full-channel
```

`--help` prints the full flag list.

### Plan-tier caps

NotebookLM limits sources per notebook. `notebooklm-py` exposes no quota endpoint, so tier is declared via `NBLM_PLAN`:

| plan | published ceiling | default `--max-sources` |
|---|---|---|
| free | 50 | 45 |
| plus | 100 | 95 |
| pro | 300 | 295 |
| ultra | 600 | 595 |

Override per-call with `--max-sources N`. If you upgrade/downgrade, update `.env` — under-stating the tier just leaves headroom; over-stating it causes server-side quota failures.

## Run the Telegram bot (manually)

```bash
make run-bot     # foreground; Ctrl-C to stop
```

In Telegram:

- `/start` or `/help` — usage
- `/pull <url> [flags]` — build a notebook; flags mirror the CLI
- any other text — forwarded to `claude -p` with `CLAUDE.md` loaded and `.claude/settings.json` enforced (narrow allowlist)

Free-form examples:

- `pull @ApertureThinking's last 30 days`
- `grab the top 20 ai videos from @foo`

## Run the Telegram bot as a launch agent

```bash
make install-launchd
```

The template `telegram-bridge/launchd/com.user.mediatonblm.plist.template` has repo paths substituted and installed to `~/Library/LaunchAgents/com.user.mediatonblm.plist`. Starts on login, auto-restarts on crash (with 10s throttle).

Management:

```bash
make bot-status        # check launchctl + tail stderr
make reload-launchd    # after changing bot.py or .env
make uninstall-launchd
```

Logs: `out/bot.log` (app-level) + `out/bot.stderr.log`, `out/bot.stdout.log` (launchd-captured).

## Security posture

- Bot rejects any Telegram user not in `TELEGRAM_ALLOWED_IDS`. Default-deny if the list is empty.
- The free-form handler runs `claude -p` under `.claude/settings.json` which allows only `bin/yt-to-nblm`, `Read(./**)`, `Write(./out/**)`, and denies `rm`, `sudo`, `curl`, `ssh`, `git push`, and reads of `.env` / `storage_state.json`.
- `.env` and `storage_state.json` are gitignored. Don't commit them.
- `TELEGRAM_BOT_TOKEN` in a chat transcript is a leak — revoke via BotFather `/revoke` and regenerate if exposed.

## Troubleshooting

**`NotebookLM auth expired`** — re-run `notebooklm login` on the Mac mini. Sessions have a finite lifetime and this re-auth is interactive (Chrome).

**`no videos returned`** — yt-dlp extractor may have broken. `make update-ytdlp`. Verify with `yt-dlp --flat-playlist --dump-json --playlist-end 3 <url>`.

**`source add` repeatedly fails** — likely hit the plan quota. Lower `--max-sources` or upgrade tier, then update `NBLM_PLAN`.

**Bot not responding** — `make bot-status`. Check the last 20 lines of stderr. Common causes: launchd couldn't find `python3` (fix `EnvironmentVariables.PATH` in the plist template), `.env` missing, allowlist empty.

**Telegram reply is silent but `/pull` ran** — check if the reply exceeded 4096 chars. Bot chunks at 3500 but very long digests may still get reordered; full text is in `out/<slug>/digest.md`.

## Layout

```
bin/yt-to-nblm                 # CLI shim (calls .venv python with src/ on path)
src/media_to_nblm/
  fetch.py                     # yt-dlp wrapper (two-pass: flat + enrich)
  select.py                    # time window + keyword filter + view-count rank + cap
  notebook.py                  # notebooklm CLI wrapper
  prompts.py                   # default prompt pack
  cli.py                       # click entrypoint
telegram-bridge/
  bot.py                       # python-telegram-bot
  launchd/
    com.user.mediatonblm.plist.template
.claude/settings.json          # bridge lockdown
CLAUDE.md                      # bridge persona
out/                           # per-run artifacts (gitignored)
```

## Out of scope (for now)

- Other media sources (podcast, RSS, article). Add CLIs under `bin/` later.
- Multi-user Telegram. Single allowlist is all we need.
- Web UI. Notebooks live in NotebookLM.
- Auth refresh automation.
