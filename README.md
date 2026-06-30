# 👁 Lookout — 개인용 PR 자동 리뷰

GitHub PR을 **Claude/Codex로 리뷰 → 한국어 댓글 게시 → 사람이 최종 승인**까지 처리하는
**개인용 macOS 도구**. watch한 작성자의 PR이 대시보드에 쌓이고, 내가 고른 것만 리뷰가
돌아갑니다. 댓글·승인은 전부 **본인 GitHub 계정**으로 나갑니다 (1인 1인스턴스, self-host).

## 사전 준비 (macOS)
- `gh` 로그인 — `gh auth login`
- `claude` 그리고/또는 `codex` CLI 로그인 (쓸 엔진)
- `python3`, `git`, Xcode Command Line Tools (`xcode-select --install` — 앱 빌드용)

## 설치 (한 줄)
```bash
git clone https://github.com/Jinwoong-Hwang/lookout ~/lookout && cd ~/lookout && ./setup.sh
```
`setup.sh`가 **설정(리뷰할 repo·추적 작성자를 물어봄) → 앱 빌드 → launchd 등록**까지 한 번에 합니다.
> 설정은 나중에 `config.json`에서 바꾼 뒤 `./install.sh`로 반영. 처음엔 `dry_run_comments=true`(미게시 미리보기)로 확인 후 false 권장.

## 일상 사용
1. **Lookout 앱**(메뉴바 👁) → 대시보드 창
2. 📥 **Triage**에 watch한 사람들의 새 PR이 5분마다 자동으로 쌓임
3. 카드에서 **[리뷰 (Claude)] / [리뷰 (Codex)]** 클릭 → 몇 초 내 시작
4. 봇이 PR을 읽고 — 문제 있으면 **한국어 댓글 게시**, 없으면 통과
5. **남의 PR** → 🔒 승인 대기 → **[🔓 승인]** = 내 계정으로 approve
   **내 PR** → 🏁 완료·머지대기 (self-approve 불가라 게이트 없이 통과 표시)
6. PR 머지/닫히면 → 카드 자동 정리

| 동작 | 방법 |
|---|---|
| 새 PR 즉시 가져오기 | 🔄 PR 가져오기 |
| repo 필터 / 뷰 전환 | repo 칩 · 레인별·사람별 토글 |
| 리뷰 중단 | 🛑 리뷰 중지 |
| 목록에서 제외 | 카드 우상단 ✕ |
| 테마 전환 | 헤더 우측 토글 — 시스템 · 라이트 · 다크 |

## 구조 (요약)
```
poller(5분) → SQLite kanban → tick(flock, 5분) ─┐
                                                 ├ reviewer(worktree, read-only)
대시보드 :8788 ── 클릭(start/stop/unblock) ──────┤ verifier(독립 검증)
Lookout.app(메뉴바+창) ──────────────────────────┤ commenter(한국어 묶음댓글)
                                                 └ approver(사람 unblock 시 approve)
```
- 엔진: Claude `opus-4-8`(effort 조절) / Codex `gpt-5.5` — 카드별 선택
- 리뷰 스코프: 이 PR이 도입/영향 준 것만 / 스타일·CLAUDE.md 관례는 제외
- 멱등 마커 + closure(해결/미해결) + 대화 인지(작성자 반박 제외)

## 안전성
- **리뷰는 read-only** — detached git worktree에서 `Read/Grep/Glob`만 허용하고 `Write/Edit/Bash`·push는 차단. 대상 코드를 수정하지 않음.
- **자동 승인 없음** — 댓글은 자동 게시되지만 approve는 항상 **사람이 직접 unblock**해야 진행. 댓글·승인은 본인 GitHub 계정으로 나감.
- 시크릿·상태(`config.json`·`db/`·`worktrees/`·`repos/`·`logs/`)는 `.gitignore`라 repo에 안 올라감.
- 디스크는 자동 정리 — 워크트리는 리뷰 후 삭제, 캐시 repo gc·오래된 카드 purge는 하루 1회.

## 설정 (`config.json`)
| 키 | 설명 |
|---|---|
| `allowlist` | 리뷰 대상 `owner/repo` |
| `watch_authors` | 추적할 PR 작성자(비우면 전체) |
| `auto_review_authors` | triage 없이 자동 리뷰할 작성자 |
| `claude_model` / `claude_effort` | Claude 모델·추론강도(low~max) |
| `codex_model` | Codex 모델(null=codex 기본) |
| `dry_run_comments` / `dry_run_approve` | 실게시/실승인 차단(검증용) |
| `max_concurrent_reviews` | 동시 리뷰 수 |

## 업데이트
메인테이너가 repo에 push하면, 받아서 적용:

- **앱에서**: Lookout 메뉴(또는 메뉴바 👁) → **업데이트 확인…** (⌘U) → 있으면 팝업 승인 한 번으로 끝.
- **터미널에서**:
```bash
./update.sh --check   # origin(GitHub repo) 기준으로 새 버전 있는지만 확인
./update.sh           # git pull + 데몬 재시작 + (변경 시) 앱 재빌드/재설치 + config 새 키 머지
```
> 업데이트 확인 기준은 clone의 `origin`(이 repo)입니다. config.json은 gitignore라 덮어쓰지 않고, 새로 생긴 키만 비워서 채워줍니다. 앱 자체가 갱신되면 "재실행" 팝업이 뜹니다.

## 운영
```bash
./hermes status | list | logs | start <id> [claude|codex] | stop <id> | unblock <id>
launchctl list | grep -E "hermes|lookout"   # 데몬 상태
./install.sh                                 # 코드 수정 후 재적용
```

## 제거
```bash
for l in io.hermes.receiver io.hermes.dashboard io.hermes.tick io.lookout.app; do
  launchctl unload "$HOME/Library/LaunchAgents/$l.plist" 2>/dev/null
  rm -f "$HOME/Library/LaunchAgents/$l.plist"
done
rm -rf /Applications/Lookout.app "$HOME/Applications/Lookout.app"
rm -rf ~/lookout   # clone 디렉토리(상태·config 포함)
```

## 한계
- **macOS 전용** (launchd · WKWebView 앱)
- 1인 1인스턴스 — 호스팅 공용 서비스 아님 (댓글·승인은 본인 계정)
- 토큰 비용은 본인 claude/codex 사용량으로 나감
