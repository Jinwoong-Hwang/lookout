#!/bin/bash
# 수동 종료: receiver + dashboard (+ hookdeck listen) 정지.
for p in src.receiver src.dashboard "hookdeck listen"; do
  if pgrep -f "$p" >/dev/null; then
    pkill -f "$p" && echo "• stopped: $p"
  fi
done
echo "완료. (launchd로 등록했다면 ./install.sh 의 unload 명령으로 정지하세요)"
