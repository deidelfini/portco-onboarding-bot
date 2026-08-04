#!/bin/bash
set -euo pipefail

# Only run this setup in Claude Code on the web (remote) sessions.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Python dependencies for the bot itself (lets Claude run/lint bot.py).
# --user avoids clashing with system-managed packages (e.g. apt's cryptography).
pip install -q --user -r "$CLAUDE_PROJECT_DIR/requirements.txt"

# Railway CLI, so Claude can check deploy status / logs against this project.
if ! command -v railway >/dev/null 2>&1; then
  npm install -g @railway/cli >/dev/null 2>&1
fi

# RAILWAY_TOKEN (set as an environment variable on this Claude Code environment)
# authenticates the CLI non-interactively — no `railway login` needed.
if [ -n "${RAILWAY_TOKEN:-}" ]; then
  railway whoami >/dev/null 2>&1 && echo "Railway CLI authenticated." || echo "Railway CLI installed, but RAILWAY_TOKEN did not authenticate — check the token."
else
  echo "Railway CLI installed. Set RAILWAY_TOKEN in this environment's variables to authenticate it."
fi
