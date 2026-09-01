#!/bin/bash
# Install/refresh the launchd agents (receiver + tick).
set -euo pipefail
HERMES_HOME="$(cd "$(dirname "$0")" && pwd)"
USER_HOME="$HOME"
PYTHON="$(command -v python3)"
HOOKDECK="$(command -v hookdeck || true)"
AGENTS="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/Lookout"
mkdir -p "$AGENTS"
mkdir -p "$HERMES_HOME/logs" "$LOG_DIR"

# locate the built Mac app (menu-bar UI)
if [ -x "/Applications/Lookout.app/Contents/MacOS/Lookout" ]; then
  APPBIN="/Applications/Lookout.app/Contents/MacOS/Lookout"
else
  APPBIN="$HOME/Applications/Lookout.app/Contents/MacOS/Lookout"
fi

# stop any manual (nohup) instances so launchd can bind the ports
pkill -f "src.receiver" 2>/dev/null || true
pkill -f "src.dashboard" 2>/dev/null || true
if [ -n "$HOOKDECK" ]; then
  pkill -f "hookdeck listen 8787 lookout" 2>/dev/null || true
  pkill -f "hookdeck listen 8787 github-pr-auto-review" 2>/dev/null || true
fi
sleep 1

labels=(io.hermes.receiver io.hermes.dashboard io.hermes.tick io.lookout.app)
if [ -n "$HOOKDECK" ]; then
  labels+=(io.lookout.hookdeck)
else
  echo "skip io.lookout.hookdeck (hookdeck CLI not found)"
fi

for label in "${labels[@]}"; do
  src="$HERMES_HOME/launchd/$label.plist"
  dst="$AGENTS/$label.plist"
  sed -e "s#__HERMES_HOME__#$HERMES_HOME#g" \
      -e "s#__USER_HOME__#$USER_HOME#g" \
      -e "s#__LOG_DIR__#$LOG_DIR#g" \
      -e "s#__PYTHON__#$PYTHON#g" \
      -e "s#__HOOKDECK__#$HOOKDECK#g" \
      -e "s#__APPBIN__#$APPBIN#g" "$src" > "$dst"
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "loaded $label"
done

echo
echo "Receiver:  launchctl list | grep -E 'hermes|lookout'"
echo "Logs:      tail -f $LOG_DIR/*.log"
echo "Stop:      launchctl unload $AGENTS/io.hermes.{receiver,tick}.plist $AGENTS/io.lookout.hookdeck.plist"
