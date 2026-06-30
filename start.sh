#!/bin/bash
# 수동 실행: receiver + dashboard 백그라운드 기동. (tick은 launchd 또는 ./hermes tick)
cd "$(dirname "$0")" || exit 1
mkdir -p logs

start_one() {  # name, module
  if pgrep -f "src.$2" >/dev/null; then
    echo "• $1 이미 실행 중"
  else
    nohup python3 -m "src.$2" > "logs/$2.log" 2>&1 &
    echo "• $1 시작 (pid $!)"
  fi
}

start_one "receiver " "receiver"
start_one "dashboard" "dashboard"
sleep 1
echo
echo "📥 receiver  : http://127.0.0.1:8787/health"
echo "📊 dashboard : http://127.0.0.1:8788"
echo "🔁 tick 한번 : ./hermes tick   (또는 ./install.sh 로 5분마다 자동)"
