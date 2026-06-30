#!/bin/bash
# Lookout 최초 설정: config.json 생성 + webhook_secret 발급 + 상태 디렉토리 준비.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f config.json ]; then
  echo "• config.json 이미 있음 — 건너뜀"
else
  cp config.example.json config.json
  secret=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  python3 - "$secret" <<'PY'
import json, sys
c = json.load(open("config.json"))
c["webhook_secret"] = sys.argv[1]
json.dump(c, open("config.json", "w"), ensure_ascii=False, indent=2)
PY
  echo "• config.json 생성 + webhook_secret 발급"
fi

mkdir -p db worktrees repos logs
echo
echo "다음 단계:"
echo "  1) config.json 편집 — allowlist(리뷰할 repo), watch_authors(추적 작성자)"
echo "  2) ./install.sh                 # launchd 등록(백그라운드 + 메뉴바 앱 자동실행)"
echo "  3) ./macapp/build_app.sh        # Lookout.app 빌드 → /Applications"
echo
echo "사전 준비: gh(로그인), claude 또는 codex CLI(로그인), python3, macOS"
echo "처음엔 config.json의 dry_run_comments=true로 두고 미리보기 확인 후 false 권장."
