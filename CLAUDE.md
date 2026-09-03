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
- This file is the Claude integration layer, not a global policy duplicate.
  For repository maintenance, follow `AGENTS.md`.

## Cloud session hosting (Claude Code on the web)

- `.claude/hooks/session-start.sh` bootstraps `.venv` and installs the project
  on every fresh container. Nothing else is needed before running the CLI.
- A message that contains a YouTube URL is a request to run `bin/yt-to-nblm`
  on it, with whatever flags the message implies. Use `--dry-run` first only
  when the source count or selection is unclear; otherwise run the full
  pipeline and report the NotebookLM URL first.
- NotebookLM auth comes from the `NOTEBOOKLM_AUTH_JSON` environment variable
  set on the cloud environment (Playwright storage state exported from a
  `notebooklm login` on the Mac). Never print, log, or commit its value. If a
  run fails with an auth error, tell the user to re-export it; do not try to
  log in from the cloud.
- YouTube and NotebookLM must be reachable under the environment network
  policy. The hook prints an egress warning at startup when they are not;
  report that rather than retrying.
- `out/` is not persisted between cloud sessions, so the URL cache does not
  carry over. Repeated runs of the same URL create a new notebook.
- The Telegram bridge under `telegram-bridge/` is an optional workstation
  service. Do not start it or install its extra in the cloud.
