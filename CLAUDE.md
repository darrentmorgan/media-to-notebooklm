@AGENTS.md

## Claude adapter

- For a request containing a YouTube URL or asking to pull videos, use
  `bin/yt-to-nblm` and follow the selection and effect boundaries in
  `AGENTS.md`.
- Use `--dry-run` when URL scope, selection, or source count needs confirmation.
  Treat `--force-new`, notebook creation, source additions, and prompt asks as
  external effects that require an intentional request.
- For a successful full run, put the NotebookLM URL first, followed by a short
  digest. For a dry-run, report the selected sources and artifact directory.
  Preserve errors and never invent a URL or expose local credentials.
- The Telegram bridge may route free-form text through Claude; this file is a
  Claude integration layer, not a Telegram-only prompt or global policy
  duplicate. For repository maintenance, follow `AGENTS.md`.
