#!/bin/bash
# Lookout 업데이트: origin(GitHub repo) 기준으로 최신 코드를 받아 적용한다.
#   ./update.sh           # 받아서 적용(서비스 재시작 + 필요 시 앱 재빌드/재설치)
#   ./update.sh --check   # 적용 없이 "업데이트 있는지"만 확인
#
# 업데이트 확인 기준 = git remote 'origin'. clone 시 origin이 이 repo로 박혀 있으므로,
# 메인테이너가 push → 사용자가 ./update.sh 하면 그 repo에서 받아온다.
#
# 이 clone은 배포 타겟이다 — 설정·상태(config.json·db/·worktrees/·repos/·logs/)는
# 전부 gitignore라, 추적되는 파일은 upstream과 100% 같아야 정상이다. 그래서 머지가
# 아니라 origin 기준 강제 정렬(reset --hard)로 적용한다. 다만 로컬에 손댄 흔적이나
# push 안 된 커밋이 있으면 조용히 버리지 않고 backup/* 브랜치에 통째로 보존한 뒤
# 정렬한다 — 앱 메뉴(⌘U)처럼 사람이 개입할 수 없는 경로에서도 멈추지 않아야 한다.
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

# 로컬 수정(추적/미추적) 또는 push 안 된 커밋이 있으면 통째로 백업 브랜치에 남긴다.
# 백업은 HEAD에서 분기하므로 로컬 커밋까지 그대로 따라가고, `git add -A`로 미추적
# 파일도 함께 담긴다. 이후 원래 브랜치로 돌아가면 그 파일들은 워킹트리에서 빠진다
# (백업 브랜치에만 존재) — 따라서 별도 clean이 필요 없다.
AHEAD=$(git rev-list --count "$UPSTREAM..HEAD" 2>/dev/null || echo 0)
if [ -n "$(git status --porcelain)" ] || [ "$AHEAD" != "0" ]; then
  BACKUP="backup/pre-update-$(date +%Y%m%d-%H%M%S)"
  echo "▸ 로컬 변경 감지 → $BACKUP 에 보존"
  git status --short | sed 's/^/    /'
  [ "$AHEAD" = "0" ] || echo "    (+ push 안 된 커밋 ${AHEAD}개)"
  git checkout --quiet -b "$BACKUP"
  git add -A
  # 커밋할 워킹트리 변경이 없어도(로컬 커밋만 앞선 경우) 백업 브랜치는 유효하다.
  git commit --quiet --no-verify -m "backup: ./update.sh 적용 직전 로컬 상태" >/dev/null 2>&1 || true
  git checkout --quiet "$BRANCH"
  echo "    보존됨 — 되돌리려면: git checkout $BACKUP"
fi

echo "▸ origin 기준으로 정렬(reset --hard)…"
git reset --hard --quiet "$UPSTREAM"

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
