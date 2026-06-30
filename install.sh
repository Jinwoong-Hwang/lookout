#!/bin/bash
# Install/refresh the launchd agents (receiver + tick).
set -euo pipefail
HERMES_HOME="$(cd "$(dirname "$0")" && pwd)"
USER_HOME="$HOME"
PYTHON="$(command -v python3)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS"

# locate the built Mac app (menu-bar UI)
if [ -x "/Applications/Lookout.app/Contents/MacOS/Lookout" ]; then
  APPBIN="/Applications/Lookout.app/Contents/MacOS/Lookout"
else
  APPBIN="$HOME/Applications/Lookout.app/Contents/MacOS/Lookout"
fi

# stop any manual (nohup) instances so launchd can bind the ports
pkill -f "src.receiver" 2>/dev/null || true
pkill -f "src.dashboard" 2>/dev/null || true
sleep 1

for label in io.hermes.receiver io.hermes.dashboard io.hermes.tick io.lookout.app; do
  src="$HERMES_HOME/launchd/$label.plist"
  dst="$AGENTS/$label.plist"
  sed -e "s#__HERMES_HOME__#$HERMES_HOME#g" \
      -e "s#__USER_HOME__#$USER_HOME#g" \
      -e "s#__PYTHON__#$PYTHON#g" \
      -e "s#__APPBIN__#$APPBIN#g" "$src" > "$dst"
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "loaded $label"
done

echo
echo "Receiver:  launchctl list | grep hermes"
echo "Logs:      tail -f $HERMES_HOME/logs/*.log"
echo "Stop:      launchctl unload $AGENTS/io.hermes.{receiver,tick}.plist"
