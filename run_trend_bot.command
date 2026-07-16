#!/bin/zsh
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"

if [[ -f "$BOT_DIR/.env" ]]; then
  set -a
  source "$BOT_DIR/.env"
  set +a
fi

mkdir -p "$BOT_DIR/logs" "$BOT_DIR/data" "$BOT_DIR/state"

# Use virtual environment Python if available, otherwise fall back to system Python
if [[ -f "$BOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$BOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "Python 3 is required to run this bot."
  exit 1
fi

exec "$PYTHON_BIN" "$BOT_DIR/src/bot.py" --config "$BOT_DIR/config.json"
