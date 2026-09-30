# media-to-notebooklm

NotebookLM notebook builder for YouTube. Given a YouTube URL (channel, playlist, or single video), pick the top videos by views, create a NotebookLM notebook, add them as sources, and optionally run a default prompt pack.

Two ways to drive it:

- **Claude Code on the web (default).** Message a cloud session a URL, get back a notebook link and digest. No servers to run.
- **Telegram bridge (optional).** A small bot on a Mac that accepts `/pull <url>` from your phone. See "Optional: Telegram bridge".

## Pipeline

```
YouTube URL
  ├─ YouTube Data API (YOUTUBE_API_KEY set)   (one pass: title/views/date/description)
  └─ else, or on API error: yt-dlp
       └─ yt-dlp --flat-playlist             (fast: id/title/view_count)
          └─ yt-dlp full extract on top pool (adds upload_date + description)
           └─ select: --days, --filter, --limit, --max-sources
              └─ notebooklm create "<name>"
                 └─ notebooklm source add <url> (throttled, 1s)
                    └─ notebooklm ask (default prompt pack)
                       └─ digest.md + notebook URL
```

## Run in Claude Code on the web

The repo ships a `SessionStart` hook (`.claude/hooks/session-start.sh`) that creates `.venv` and installs the project in every fresh cloud container. Once the environment is configured, send a cloud session a message such as:

> pull the top 20 AI videos from https://youtube.com/@SomeChannel in the last 90 days

Claude maps that to `bin/yt-to-nblm` flags, runs it, and replies with the notebook URL and digest.

### One-time environment setup

1. **YouTube Data API key (recommended).** Metadata comes from the official API instead of yt-dlp, so it needs no cookies and is not hit by YouTube's bot checks on cloud IPs. In Google Cloud Console: create or pick a project, enable **YouTube Data API v3**, create an **API key**, and restrict it to that API. Set it as `YOUTUBE_API_KEY` on the environment. The fetch uses read-only list calls (one per 50 videos, plus one per channel lookup), well inside the default daily quota for normal use. Without a key, or if the API fails (quota, outage, `/c/` custom URLs), the CLI falls back to yt-dlp automatically.
2. **Network policy.** Allow egress to `www.googleapis.com` (Data API) and `notebooklm.google.com`, plus `pypi.org` for the hook. `youtube.com` and `googlevideo.com` are needed only for the yt-dlp fallback. The hook prints a warning at startup for any unreachable host.
3. **NotebookLM auth.** On any machine with a browser, install the project and log in once, then export the storage state:

   ```bash
   make venv
   .venv/bin/playwright install chromium
   .venv/bin/notebooklm login      # sign into the Google account that owns the notebooks
   jq -c . ~/.notebooklm/storage_state.json   # paste as NOTEBOOKLM_AUTH_JSON on the environment
   ```

   `notebooklm-py` reads `NOTEBOOKLM_AUTH_JSON` before falling back to the file.
4. **Plan tier.** Set `NBLM_PLAN` on the environment (see "Plan-tier caps").

### How long does the auth last?

The exported blob holds Google account cookies. Each run refreshes NotebookLM's CSRF and session tokens automatically, but nothing can renew the underlying Google cookies without a browser. In practice the export keeps working until Google invalidates the session (sign-out everywhere, password change, security event, or its own rotation policy). When a run fails with an auth error, repeat step 3. There is no non-interactive renewal for a consumer Google account.

### Caveats

- `out/` (including the URL-to-notebook cache) is not persisted across cloud sessions; re-running the same URL creates a new notebook. Pass `--force-new` explicitly if that is what you want on a workstation too.
- The browser is never installed in the cloud; `notebooklm login` only works on a machine you can sign into.
- Without `YOUTUBE_API_KEY`, yt-dlp from a datacenter IP will likely hit YouTube bot checks. Set the key; `YT_DLP_PROXY` is the fallback's only escape hatch.
- The Data API cannot resolve legacy `/c/<name>` URLs; those use yt-dlp. Use the `@handle` or `/channel/UC…` form instead.

## Run locally

```bash
git clone https://github.com/darrentmorgan/media-to-notebooklm
cd media-to-notebooklm
make venv
cp .env.example .env    # set NBLM_PLAN
.venv/bin/playwright install chromium
.venv/bin/notebooklm login
```

`.env` keys:

| key | value |
|---|---|
| `NBLM_PLAN` | `free` \| `plus` \| `pro` \| `ultra` — maps to default `--max-sources` |
| `NBLM_BIN` | (optional) absolute path to the `notebooklm` executable. Auto-resolves via the venv or `$PATH` if unset. |
| `NOTEBOOKLM_AUTH_JSON` | (cloud only) inline storage state; leave unset locally so the file is used |
| `YOUTUBE_API_KEY` | (optional, recommended in the cloud) YouTube Data API v3 key; metadata falls back to yt-dlp without it |

### CLI examples

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

Override per-call with `--max-sources N`. Under-stating the tier just leaves headroom; over-stating it causes server-side quota failures.

## Optional: Telegram bridge

Not required for anything above. Use it if you want to trigger runs from your phone via a bot running on an always-on Mac.

Prerequisites: a Mac with the repo installed and logged in (see "Run locally"), a BotFather token, your numeric Telegram user id (`@userinfobot`), and optionally the `claude` CLI on `$PATH` for free-form text.

```bash
make install-telegram          # adds python-telegram-bot to .venv
# add to .env:
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_ALLOWED_IDS=123456789
make run-bot                   # foreground; Ctrl-C to stop
```

In Telegram:

- `/start` or `/help` — usage
- `/pull <url> [flags]` — build a notebook; flags mirror the CLI
- any other text — forwarded to `claude -p` under `telegram-bridge/claude-settings.json` (allows only `bin/yt-to-nblm`, reads in the repo, writes to `out/`; denies `rm`, `sudo`, `curl`, `ssh`, `git push`, and reads of `.env` / `storage_state.json`)

Run it as a launch agent so it survives reboots:

```bash
make install-launchd     # renders the plist template with this repo's path and loads it
make bot-status          # launchctl state + stderr tail
make reload-launchd      # after changing bot.py or .env
make uninstall-launchd
```

Logs: `out/bot.log` (app-level), `out/bot.stderr.log` and `out/bot.stdout.log` (launchd-captured).

Bot security: any Telegram user not in `TELEGRAM_ALLOWED_IDS` is rejected; an empty list rejects everyone. If `TELEGRAM_BOT_TOKEN` ever leaks, revoke it via BotFather `/revoke`.

## Security posture

- `.env` and `storage_state.json` are gitignored. Don't commit them.
- `NOTEBOOKLM_AUTH_JSON` grants full access to the Google account's NotebookLM. Keep it in the environment's secret settings only; never paste it into chat, logs, or the repo.

## Troubleshooting

**`Authentication expired`** — re-run `notebooklm login` on a machine with a browser and re-export `NOTEBOOKLM_AUTH_JSON`.

**`no videos returned`** — yt-dlp extractor may have broken. `make update-ytdlp`. Verify with `yt-dlp --flat-playlist --dump-json --playlist-end 3 <url>`.

**Egress warning at session start** — the cloud environment's network policy is blocking YouTube or NotebookLM. Fix it in the environment settings; no code change helps.
