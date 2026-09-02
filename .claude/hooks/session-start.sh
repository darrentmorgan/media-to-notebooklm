#!/bin/bash
# SessionStart hook for Claude Code on the web.
# Bootstraps the repo venv so `bin/yt-to-nblm` and the `notebooklm` CLI work in a
# fresh cloud container. Idempotent; local (non-remote) sessions are a no-op.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

REPO="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO"

# Playwright is only needed for interactive `notebooklm login` on a real host;
# never download browsers in the cloud.
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

if [ ! -x .venv/bin/python ]; then
  echo "[session-start] creating .venv"
  python3 -m venv .venv
fi

if [ ! -x .venv/bin/notebooklm ] || ! .venv/bin/python -c "import media_to_nblm" 2>/dev/null; then
  echo "[session-start] installing project deps"
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e .
else
  echo "[session-start] deps already installed"
fi

mkdir -p out

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo 'export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1' >> "$CLAUDE_ENV_FILE"
  echo "export NBLM_BIN=\"$REPO/.venv/bin/notebooklm\"" >> "$CLAUDE_ENV_FILE"
fi

# Preflight: report (never print) the auth secret and check egress.
if [ -n "${NOTEBOOKLM_AUTH_JSON:-}" ]; then
  echo "[session-start] NOTEBOOKLM_AUTH_JSON is set"
else
  echo "[session-start] WARNING: NOTEBOOKLM_AUTH_JSON is not set; non-dry runs will fail auth" >&2
fi

for host in https://www.youtube.com https://notebooklm.google.com; do
  if curl -sS -o /dev/null --max-time 10 "$host" 2>/dev/null; then
    echo "[session-start] egress ok: $host"
  else
    echo "[session-start] WARNING: cannot reach $host; check the environment network policy" >&2
  fi
done

echo "[session-start] ready: bin/yt-to-nblm --help"
