# media-to-notebooklm

NotebookLM notebook builder for YouTube. Given a YouTube URL (channel, playlist, or single video), pick the top videos by views, create a NotebookLM notebook, add them as sources, and optionally run a default prompt pack. Designed to be driven from a Claude Code on the web session: message it a URL, get back a notebook link and digest.

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

## Run in Claude Code on the web

The repo ships a `SessionStart` hook (`.claude/hooks/session-start.sh`) that creates `.venv` and installs the project in every fresh cloud container. Once the environment is configured, send a cloud session a message such as:

> pull the top 20 AI videos from https://youtube.com/@SomeChannel in the last 90 days

Claude maps that to `bin/yt-to-nblm` flags, runs it, and replies with the notebook URL and digest.

### One-time environment setup

1. **Network policy.** The cloud environment must allow egress to `youtube.com`, `googlevideo.com`, and `notebooklm.google.com` (plus `pypi.org` for the hook). The hook prints a warning at startup if either host is unreachable.
2. **NotebookLM auth.** On any machine with a browser, install the project and log in once, then export the storage state:

   ```bash
   make venv
   .venv/bin/playwright install chromium
   .venv/bin/notebooklm login      # sign into the Google account that owns the notebooks
   jq -c . ~/.notebooklm/storage_state.json   # paste as NOTEBOOKLM_AUTH_JSON on the environment
   ```

   `notebooklm-py` reads `NOTEBOOKLM_AUTH_JSON` before falling back to the file.
3. **Plan tier.** Set `NBLM_PLAN` on the environment (see "Plan-tier caps").

### How long does the auth last?

The exported blob holds Google account cookies. Each run refreshes NotebookLM's CSRF and session tokens automatically, but nothing can renew the underlying Google cookies without a browser. In practice the export keeps working until Google invalidates the session (sign-out everywhere, password change, security event, or its own rotation policy). When a run fails with an auth error, repeat step 2. There is no non-interactive renewal for a consumer Google account.

### Caveats

- `out/` (including the URL-to-notebook cache) is not persisted across cloud sessions; re-running the same URL creates a new notebook. Pass `--force-new` explicitly if that is what you want on a workstation too.
- The browser is never installed in the cloud; `notebooklm login` only works on a machine you can sign into.
- yt-dlp from a datacenter IP may hit YouTube bot checks. If that happens, set `YT_DLP_PROXY` on the environment or pass cookies per the yt-dlp docs.

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

## Security posture

- `.env` and `storage_state.json` are gitignored. Don't commit them.
- `NOTEBOOKLM_AUTH_JSON` grants full access to the Google account's NotebookLM. Keep it in the environment's secret settings only; never paste it into chat, logs, or the repo.

## Troubleshooting

**`Authentication expired`** — re-run `notebooklm login` on a machine with a browser and re-export `NOTEBOOKLM_AUTH_JSON`.

**`no videos returned`** — yt-dlp extractor may have broken. `make update-ytdlp`. Verify with `yt-dlp --flat-playlist --dump-json --playlist-end 3 <url>`.

**Egress warning at session start** — the cloud environment's network policy is blocking YouTube or NotebookLM. Fix it in the environment settings; no code change helps.
