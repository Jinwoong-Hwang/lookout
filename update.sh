#!/bin/bash
# Lookout 업데이트: origin(GitHub repo) 기준으로 최신 코드를 받아 적용한다.
#   ./update.sh           # 받아서 적용(서비스 재시작 + 필요 시 앱 재빌드/재설치)
#   ./update.sh --check   # 적용 없이 "업데이트 있는지"만 확인
#
# 업데이트 확인 기준 = git remote 'origin'. clone 시 origin이 이 repo로 박혀 있으므로,
# 메인테이너가 push → 사용자가 ./update.sh 하면 그 repo에서 받아온다.
set -euo pipefail
cd "$(dirname "$0")"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
UPSTREAM="origin/$BRANCH"
UIDN=$(id -u)

echo "▸ origin($BRANCH)에서 변경 확인…"
git fetch --quiet origin "$BRANCH"
BEHIND=$(git rev-list --count "HEAD..$UPSTREAM" 2>/dev/null || echo 0)

if [ "$BEHIND" = "0" ]; then
  echo "✓ 이미 최신 ($(git rev-parse --short HEAD))"
  exit 0
fi
echo "  origin이 ${BEHIND}개 커밋 앞섬:"
git --no-pager log --oneline "HEAD..$UPSTREAM" | sed 's/^/    /'

if [ "${1:-}" = "--check" ]; then
  echo "→ 적용하려면: ./update.sh"
  exit 0
fi

# 적용 전 변경 파일 목록 확보(앱/설치 스크립트 변경 감지용)
CHANGED=$(git diff --name-only "HEAD..$UPSTREAM")

echo "▸ git pull (fast-forward)…"
git pull --ff-only origin "$BRANCH"

# config.example.json에 새로 생긴 키를 본인 config.json에 머지(실 키는 값 비움)
if [ -f config.json ]; then
  python3 - <<'PY'
import json, collections
ex  = json.load(open("config.example.json"), object_pairs_hook=collections.OrderedDict)
cur = json.load(open("config.json"),         object_pairs_hook=collections.OrderedDict)
added = []
for k, v in ex.items():
    if k not in cur:
        cur[k] = v if k.startswith("_") else ("" if isinstance(v, str) else v)
        if not k.startswith("_"):
            added.append(k)
if added:
    json.dump(cur, open("config.json", "w"), ensure_ascii=False, indent=2)
    print("  + config.json 새 키 추가(값 비움 — 필요 시 채우세요):", ", ".join(added))
PY
fi

# launchd 설정/플리스트가 바뀌었으면 재설치
if echo "$CHANGED" | grep -qE 'install\.sh|\.plist'; then
  echo "▸ launchd 재설치(install.sh)…"
  ./install.sh
fi

# 상주 데몬 재시작 → Python 코드 변경 반영 (tick은 매 실행 새 프로세스라 자동 반영)
echo "▸ 데몬 재시작…"
for svc in io.hermes.dashboard io.hermes.receiver; do
  launchctl kickstart -k "gui/$UIDN/$svc" 2>/dev/null \
    && echo "    restarted $svc" || echo "    (skip $svc — 미등록)"
done

# 맥 앱 소스가 바뀌었으면 재빌드
if echo "$CHANGED" | grep -qE '^macapp/'; then
  echo "▸ Lookout.app 재빌드…"
  ./macapp/build_app.sh
fi

echo "✓ 업데이트 완료 → $(git rev-parse --short HEAD)"
