#!/bin/bash
# Lookout 원클릭 설치: 설정(대화형) → 상태 디렉토리 → 앱 빌드 → launchd 등록.
# 재실행해도 안전(이미 있으면 설정은 건너뜀). 유지보수용 개별 스크립트:
#   ./install.sh            # launchd만 재적용
#   ./macapp/build_app.sh   # 앱만 재빌드
#   ./update.sh             # origin에서 업데이트
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p db worktrees repos logs

if [ -f config.json ]; then
  echo "• config.json 이미 있음 — 설정 단계 건너뜀"
else
  cp config.example.json config.json
  secret=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  echo
  echo "── 기본 설정 (엔터로 건너뛰고 나중에 config.json에서 수정 가능) ──"
  read -r -p "리뷰할 repo (owner/repo, 쉼표로 여러 개): " repos || true
  read -r -p "추적할 PR 작성자 GitHub 아이디 (쉼표, 비우면 전체): " authors || true
  python3 - "$secret" "${repos:-}" "${authors:-}" <<'PY'
import json, sys
secret, repos, authors = sys.argv[1], sys.argv[2], sys.argv[3]
c = json.load(open("config.json"))
c["webhook_secret"] = secret
split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
if split(repos):
    c["allowlist"] = split(repos)
c["watch_authors"] = split(authors)
json.dump(c, open("config.json", "w"), ensure_ascii=False, indent=2)
PY
  echo "• config.json 생성 완료"
fi

echo
echo "▸ Lookout.app 빌드…"
./macapp/build_app.sh
echo
echo "▸ launchd 등록(백그라운드 데몬 + 메뉴바 앱)…"
./install.sh

echo
echo "✅ 설치 완료 — 메뉴바 👁 Lookout · 대시보드 http://127.0.0.1:8788"
echo "   • 설정 변경: config.json 편집 후 ./install.sh"
echo "   • 처음엔 dry_run_comments=true(미게시 미리보기) 권장 → 만족하면 false"
