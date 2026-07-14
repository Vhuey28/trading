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

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "Python 3 is required to run this bot."
    exit 1
  fi
fi

exec "$PYTHON_BIN" "$BOT_DIR/src/bot.py" --config "$BOT_DIR/config.json"
