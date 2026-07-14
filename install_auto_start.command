#!/bin/zsh
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.vaughnhuey.trend-bot.plist"
RUNNER="$BOT_DIR/run_trend_bot.command"
LOG_DIR="$BOT_DIR/logs"

mkdir -p "$PLIST_DIR" "$LOG_DIR" "$BOT_DIR/data" "$BOT_DIR/state"
chmod +x "$RUNNER"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.vaughnhuey.trend-bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>$RUNNER</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$BOT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/launchd.err.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

echo "Trend bot auto-start is installed."
echo "The bot is running now and will start automatically when you log in."
echo "It will only trade during the window configured in config.json."

