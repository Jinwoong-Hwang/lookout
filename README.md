# 👁 Lookout — 개인용 PR 리뷰 자동화

**개인용 macOS 도구.** watch한 작성자의 PR을 Claude/Codex가 읽고 → **한국어 댓글 게시** → 사람이 최종 승인.

댓글·승인은 전부 **본인 GitHub 계정**으로 나갑니다 (1인 1인스턴스, self-host).
바깥으로 나가는 행동 중 approve 에는 **사람 게이트**가 있습니다.

> 이슈 작업(설계 토론·주제 토론·구현·draft PR) 기능은 걷어냈습니다 — headless 엔진으로 열린 작업을 시키는 구조가 의도대로 돌지 않아 처음부터 다시 설계합니다. 이슈는 **에픽별 뷰로 보기만** 합니다.

## 사전 준비 (macOS)
- `gh` 로그인 — `gh auth login`
- `claude` 그리고/또는 `codex` CLI 로그인
  (한쪽만 있어도 됩니다)
- `python3`, `git`, Xcode Command Line Tools (`xcode-select --install` — 앱 빌드용)

## 설치 (한 줄)
```bash
git clone https://github.com/Jinwoong-Hwang/lookout ~/lookout && cd ~/lookout && ./setup.sh
```
`setup.sh`가 **설정(리뷰할 repo·추적 작성자를 물어봄) → 앱 빌드 → launchd 등록**까지 한 번에 합니다.
> 설정은 나중에 `config.json`에서 바꾼 뒤 `./install.sh`로 반영. 처음엔 `dry_run_comments` / `dry_run_approve` 를 `true`(미게시 미리보기)로 두고 확인 후 false 권장.

## 화면
**Lookout 앱**(메뉴바 👁) → 대시보드 창(`127.0.0.1:8788`). 왼쪽 사이드 메뉴로 뷰를 바꿉니다.

| 뷰 | 내용 |
|---|---|
| 🗂 **레인별** | PR 리뷰 카드를 단계(Triage→리뷰→검증→댓글→승인→완료)별로 |
| 👤 **사람별** | 같은 카드를 작성자별로 |
| 💬 **리뷰 피드백** | 게시한 댓글에 달린 반응(👍👎💬) 스냅샷 — 리뷰가 먹혔는지 확인 |
| 🎯 **에픽별** | 나에게 할당된 product-hub 이슈를 **에픽 ▸ 태스크**로 묶어 보여줌 (읽기 전용 — 행을 누르면 GitHub 이슈) |

> **에픽별** 소속은 GitHub 네이티브 sub-issue 관계(`issueType`/`parent`)를 그대로 쓰고 제목 태그로 추정하지 않습니다. 에픽이 내게 할당되지 않아도 자식이 들고 온 부모 정보로 머리글을 세웁니다(`보드 밖`). 행의 🎫 는 GitHub Project 의 Status 이고, Lookout 은 읽기만 합니다. `issue_repos` 가 비면 뷰가 비어 있습니다.

## PR 리뷰
1. 📥 **Triage**에 watch한 사람들의 새 PR이 5분마다 자동으로 쌓임
2. 카드에서 **[리뷰 (Claude)] / [리뷰 (Codex)]** 클릭 → 몇 초 내 시작
3. 봇이 PR을 읽고 — 문제 있으면 **한국어 댓글 게시**, 없으면 통과
4. **남의 PR** → 🔒 승인 대기 → **[🔓 승인]** = 내 계정으로 approve
   **내 PR** → 🏁 완료·머지대기 (self-approve 불가라 게이트 없이 통과 표시)
5. PR 머지/닫히면 → 카드 자동 정리

| 동작 | 방법 |
|---|---|
| 새 PR 즉시 가져오기 | 🔄 PR 가져오기 |
| repo 필터 / 뷰 전환 | repo 칩 · 사이드 메뉴 |
| 리뷰 중단 | 🛑 리뷰 중지 |
| 목록에서 제외 | 카드 우상단 ✕ |
| 실패한 카드 | ↻ 재시도 (실패 사유가 카드에 남음) |
| 테마 전환 | 헤더 우측 토글 — 시스템 · 라이트 · 다크 |

- 리뷰 스코프: 이 PR이 도입/영향 준 것만 / 스타일·CLAUDE.md 관례는 제외
- 멱등 마커 + closure(해결·해명 수용·후속 이관·미해결) + 대화 인지
- 작성자가 "의도적입니다 / 후속에서 처리"라고 답하면 그 회신을 근거로 추적하되, **운영자가 수용해야** LGTM으로 넘어감

## 구조 (요약)
```
poller(5분) ─ PR ──→ SQLite kanban → tick(flock, 5분) ─┬ reviewer(worktree, read-only)
                                                        ├ verifier(독립 검증)
대시보드 :8788 ── 클릭(start/gate/stop) ────────────────┤ commenter(한국어 묶음댓글)
Lookout.app(메뉴바+창) ─────────────────────────────────┘ approver(사람 unblock 시 approve)
```
- 엔진: Claude `claude-opus-5`(effort 조절) / Codex(기본 `~/.codex/config.toml`, 현재 `gpt-6-astra`) — 카드별 선택
- tick은 프로세스 flock 하나로 직렬화되고, 리뷰는 `max_concurrent_reviews`까지 병렬
- 엔진 토큰이 소진되면 카드를 대기열로 되돌리고 macOS 알림을 띄웁니다(같은 엔진은 15분에 한 번만)

## 안전성
- **리뷰·검증은 read-only** — detached worktree에서 `Read/Grep/Glob`만 허용하고 `Write/Edit/Bash`·push는 차단.
- **자동 승인 없음** — 댓글은 자동 게시되지만 approve는 항상 **사람이 게이트를 통과**시켜야 진행.
- 시크릿·상태(`config.json`·`db/`·`worktrees/`·`repos/`·`workspaces/`·`logs/`)는 `.gitignore`라 repo에 안 올라감.
- 디스크는 자동 정리 — 리뷰 워크트리는 리뷰 후 삭제, 캐시 repo gc·오래된 카드 purge는 하루 1회.

## 설정 (`config.json`)
`config.example.json`에 키마다 `_주석`이 붙어 있습니다. 자주 건드리는 것만:

**리뷰**

| 키 | 설명 |
|---|---|
| `allowlist` | 리뷰 대상 `owner/repo` |
| `watch_authors` | 추적할 PR 작성자(비우면 전체) |
| `auto_review_authors` | triage 없이 자동 리뷰할 작성자 (`["*"]` 또는 `["all"]` = 전체) |
| `default_review_engine` | 자동 생성 카드의 기본 엔진 |
| `repo_profiles` | repo별 리뷰 정책(문서 repo는 comment-only·dry-run 등) |
| `max_findings_per_review` / `min_confidence` | 지적 개수·최소 확신도 |
| `max_diff_chars` | 프롬프트 diff 예산. 초과분은 파일 목록으로 알려 워크트리에서 직접 열게 함 |

**에픽별 뷰** (`issue_repos`가 비면 꺼짐)

| 키 | 설명 |
|---|---|
| `issue_repos` / `issue_assignee` | 이슈를 가져올 repo · `@me` 등 담당자 필터 |
| `issue_title_prefixes` / `issue_display_prefix` | 제목 태그 필터 · 표시 별칭(`PH-1767`) |

**공통**

| 키 | 설명 |
|---|---|
| `claude_model` / `claude_effort` | Claude 모델·추론강도(low~max) |
| `codex_model` | Codex 모델(null=codex 기본) |
| `dry_run_comments` / `dry_run_approve` | 실게시·실승인 차단(검증용) |
| `max_concurrent_reviews` | 동시 리뷰 수 |
| `dashboard_host` / `dashboard_port` | 대시보드 바인딩(기본 `127.0.0.1:8788`) |
| `dashboard_write_networks` | 쓰기 API 허용 CIDR — 내부망에 열 때만 넓힘 |
| `env_file` | launchd에서 `gh` 인증이 안 될 때 `GH_TOKEN`을 읽을 private 파일 |
| `poller_interval_minutes` / `purge_days` | 폴링 주기 · archived 카드 보관일 |
| `notify_enabled` | 토큰 소진 등으로 멈출 때 macOS 알림 |

## 업데이트
메인테이너가 repo에 push하면, 받아서 적용:

- **앱에서**: Lookout 메뉴(또는 메뉴바 👁) → **업데이트 확인…** (⌘U) → 있으면 팝업 승인 한 번으로 끝.
- **터미널에서**:
```bash
./update.sh --check   # origin(GitHub repo) 기준으로 새 버전 있는지만 확인
./update.sh           # origin 기준 정렬 + 데몬 재시작 + (변경 시) 앱 재빌드/재설치 + config 새 키 머지
```
> 업데이트 확인 기준은 clone의 `origin`(이 repo)입니다. config.json은 gitignore라 덮어쓰지 않고, 새로 생긴 키만 비워서 채워줍니다. 앱 자체가 갱신되면 "재실행" 팝업이 뜹니다.

> **clone은 배포 타겟입니다** — 설정·상태는 전부 gitignore라, 추적되는 파일은 upstream과 같아야 정상입니다. 그래서 머지가 아니라 `origin` 기준 강제 정렬로 적용합니다.
> 로컬에서 손댄 파일이나 push 안 된 커밋이 있으면 **버리지 않고 `backup/pre-update-<시각>` 브랜치에 통째로 보존한 뒤** 정렬합니다 (미추적 파일 포함). 되돌리려면 `git checkout backup/pre-update-…`.
> 이 clone에서 직접 개발하지 마세요 — 매 업데이트마다 백업 브랜치가 쌓입니다.

## 운영
```bash
./hermes status | list [상태] | findings <id> | logs [n]     # 조회
./hermes start <id> [claude|codex] | stop <id> | ignore <id> # 리뷰 카드 조작
./hermes unblock <id> | publish-dryrun <id>                  # 승인 게이트 · dry-run 댓글 실게시
./hermes tick                                                # 파이프라인 1회 수동 실행
./hermes feedback-snapshot <id> | feedback-weekly            # 리뷰 피드백 수집
launchctl list | grep -E "hermes|lookout"   # 데몬 상태
tail -f ~/Library/Logs/Lookout/*.log         # 데몬 로그
./install.sh                                 # 코드 수정 후 재적용
python3 -m unittest discover -s tests -q      # 테스트 (303개, unittest)
python3 run-demo.py [포트]                   # 라이브(:8788) 안 건드리고 대시보드만 띄우기
```

## 제거
```bash
for l in io.hermes.receiver io.hermes.dashboard io.hermes.tick io.lookout.app io.lookout.hookdeck; do
  launchctl unload "$HOME/Library/LaunchAgents/$l.plist" 2>/dev/null
  rm -f "$HOME/Library/LaunchAgents/$l.plist"
done
rm -rf /Applications/Lookout.app "$HOME/Applications/Lookout.app"
rm -rf ~/lookout "$HOME/Library/Logs/Lookout"   # clone 디렉토리(상태·config 포함) + 로그
```
> 예전 버전에서 이슈 작업을 썼다면 `~/lookout/workspaces/` 의 구현 워크트리가 **부모 체크아웃**(당시 `impl_repo_paths`)에 등록돼 있습니다. 지운 뒤 각 repo에서 `git worktree prune`.

## 한계
- **macOS 전용** (launchd · WKWebView 앱)
- 1인 1인스턴스 — 호스팅 공용 서비스 아님 (댓글·승인은 본인 계정)
- 토큰 비용은 본인 claude/codex 사용량으로 나감
