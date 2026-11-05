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
| 👤 **사람별** | 같은 카드를 작성자별로 — 머리글의 **🤖 자동 리뷰 ON/OFF** 로 사람마다 자동 리뷰를 켜고 끔 (아래 참고) |
| 💬 **리뷰 피드백** | 게시한 댓글에 달린 반응(👍👎💬) 스냅샷 — 리뷰가 먹혔는지 확인 |
| 🎯 **에픽별** | 나에게 할당된 product-hub 이슈를 **에픽 ▸ 태스크**로 묶어 보여줌 (읽기 전용 — 행을 누르면 GitHub 이슈) |
| 🗂 **세션 요약** | 내 Claude Code·Codex 세션이 하루 동안 한 일을 **일 단위로 묶어** 요약 — 세션마다 한 줄 (아래 참고) |

> **자동 리뷰 토글**이 자동 리뷰의 유일한 설정입니다(config 에는 없음). 켜면 그 사람의 **새 PR·새 커밋**부터 Triage 없이 바로 리뷰하고, 이미 Triage 에 있는 카드는 그대로 둡니다. 누른 적 없는 사람은 OFF 이고, repo 정책 `auto_review: false`(예: product-hub)는 토글보다 우선합니다. 값은 `db/` 에 저장돼 앱·데몬을 껐다 켜거나 `./update.sh` 를 해도 유지됩니다. 예전 config 의 `auto_review_authors` 는 처음 한 번만 토글로 옮겨지고 이후엔 읽지 않습니다.

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
- 멱등 마커 + closure(해결·해명 수용·후속 이관·미해결)
- 리뷰어는 대화 전사가 아니라 **원장**을 봅니다 — 이미 올라간 지적의 `rule · 위치 · 상태 · 작성자 답변` 표. 같은 repo에 다른 사람의 인스턴스가 붙어 있으면 그쪽 지적도 마커에서 읽어 합칩니다
- 작성자 회신은 **PR 본문 · 이슈 코멘트 · 리뷰 본문 · 리뷰 인라인** 네 곳에서 모읍니다
- 작성자가 "의도적입니다 / 후속에서 처리"라고 답하면 그 회신을 근거로 추적하되, **운영자가 수용해야** LGTM으로 넘어감. 새 커밋이 와도 풀리지 않고, 되돌리려면 작성자의 **새 답변**이 있어야 합니다

## 세션 요약
이 맥에서 내가 Claude Code·Codex 와 한 세션이 하루 동안 무슨 일을 했는지 날마다 한 장으로 정리합니다. 팀 스레드에 올릴 글이나 다음 날 이어받을 일을 떠올릴 때 봅니다.

1. 그날 00:00 부터의 **대화형** Claude Code 세션(`~/.claude/projects`)과 Codex 세션(`~/.codex/sessions`)을 압축 — 세션 제목·요청·연결된 PR·커밋·스킬·마지막 응답 (LLM 없음)
2. **codex**(모델 목록 1순위, effort `high`)가 세션마다 한 줄 요약을 쓰고, 같은 일(티켓·PR·브랜치·주제)을 다룬 세션끼리 시간순으로 묶어 한 일과 멈춘 지점을 1~2문장으로 씀. 완료·진행 같은 판정은 하지 않고, 기록에 있는 사실(PR·생성·머지·커밋 수)만 붙인다 — 세션 기록만으로는 세션 밖에서 머지된 PR 을 알 수 없다
   ```
   PH-1234 짧은 제목 — repo#123
     원인을 찾아 고치고 리뷰 반영 후 머지
     - 10:05 원인 분석, 수정 커밋
     - 13:40 리뷰 반영 후 머지

   잡무
     - 09:12 워크트리 정리
   ```
3. 🗂 **세션 요약** 뷰에서 읽고, 필요하면 **📋 복사**

- **언제:** 슬롯(기본 09:40 · 14:05 · 18:10) 5분 전에 오늘 것을 그때까지로 다시 만들고, 다음 날 첫 슬롯에 지난 날(월요일이면 금·토·일)을 **하루 전체로 확정**합니다. 세션 목록이 지난번과 같으면 엔진을 다시 부르지 않습니다.
- **입력은 그날의 세션 기록뿐**입니다 — 전에 올린 글이나 전날 요약을 넘기지 않습니다.
- 빼는 세션: Lookout 자신의 리뷰 세션, 다른 도구가 띄운 headless 하위 세션(red-team 리뷰어 등 — `entrypoint: sdk-*`, `codex exec`). 질문·조회처럼 산출물 없는 짧은 세션은 묶지 않고 **잡무**로 따로 보입니다.
- 켜기: `config.json` 에 `"recap_enabled": true`. 꺼져 있어도 뷰에서 날마다 **↻ 다시 만들기**(오늘 것이 아직 없으면 **✨ 지금 만들기**), `./hermes recap [YYYY-MM-DD]` 로 수동 생성은 됩니다.

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
- 세션 요약은 tick 과 따로 도는 launchd 잡(`io.hermes.recap`, 1분 주기)이 슬롯 차례일 때만 codex 를 부릅니다 — tick 은 리뷰로 30분씩 붙잡혀 있을 수 있어서

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
| `default_review_engine` | 자동 생성 카드의 기본 엔진 |
| `repo_profiles` | repo별 리뷰 정책(문서 repo는 comment-only·dry-run 등) |
| `max_findings_per_review` / `min_confidence` | 지적 개수·최소 확신도 |
| `max_diff_chars` | 프롬프트 diff 예산. 초과분은 파일 목록으로 알려 워크트리에서 직접 열게 함 |
| `claude_timeout` / `codex_timeout` | 엔진별 리뷰 상한(초, 기본 1800 / 1200). 큰 PR에서 잘리면 올립니다 |

**에픽별 뷰** (`issue_repos`가 비면 꺼짐)

| 키 | 설명 |
|---|---|
| `issue_repos` / `issue_assignee` | 이슈를 가져올 repo · `@me` 등 담당자 필터 |
| `issue_title_prefixes` / `issue_display_prefix` | 제목 태그 필터 · 표시 별칭(`PH-1767`) |

**세션 요약** (`recap_enabled` 가 false 면 자동 생성 꺼짐)

| 키 | 설명 |
|---|---|
| `recap_enabled` | 슬롯마다 자동 생성 + 알림 |
| `recap_slots` / `recap_lead_minutes` | 오늘 요약을 갱신할 시각(`HH:MM`) · 몇 분 전에 만들지. 첫 슬롯에 지난 날을 확정 |
| `recap_weekdays_only` | 주말엔 만들지 않음(주말 세션은 월요일 아침에 확정) |
| `recap_model` / `recap_effort` | 비우면 codex 모델 목록 1순위 · `high` |
| `recap_timeout` | codex 1회 상한(초) |

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
릴리스에는 **태그**가 붙습니다(`v1.2.0`). 자기 인스턴스 버전은 `./update.sh --check`
또는 앱 메뉴의 `Lookout v…` 에서 보이고, 무엇이 바뀌었는지는
[CHANGELOG.md](CHANGELOG.md) 와 [Releases](../../releases) 에 있습니다.

> 같은 PR에 인스턴스가 여러 대 붙어 있으면 **한 대만 올려도 작성자 체감은 그대로**입니다 — 릴리스가 나오면 다 같이 올립니다.

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
./hermes recap [YYYY-MM-DD] | recap show [YYYY-MM-DD]       # 세션 요약 지금 (다시) 만들기 · 보기
launchctl list | grep -E "hermes|lookout"   # 데몬 상태
tail -f ~/Library/Logs/Lookout/*.log         # 데몬 로그
./install.sh                                 # 코드 수정 후 재적용
python3 -m unittest discover -s tests -q      # 테스트 (303개, unittest)
python3 run-demo.py [포트]                   # 라이브(:8788) 안 건드리고 대시보드만 띄우기
```

## 제거
```bash
for l in io.hermes.receiver io.hermes.dashboard io.hermes.tick io.hermes.recap io.lookout.app io.lookout.hookdeck; do
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
