# Slack 멘션 Catch-up — 설계/구성도

> 목표: 나(특정 Slack 유저)에게 온 **@멘션을 전부** 잡아서, 기존 Hermes 대시보드(:8788)에
> "📢 멘션 / 확인요청" 섹션으로 모아 보고 → 읽음/보관 처리할 수 있게 한다.
>
> 판별 기준: "멘션이면 전부" (확인요청 분류 같은 건 안 함 — 나중에 선택적 확장).

## 1. 핵심 원칙 — 기존 아키텍처를 그대로 재사용

이미 `GitHub → Hookdeck → receiver(:8787) → inbox → tick(router) → cards → dashboard(:8788)`
파이프라인이 있다. Slack은 **이 파이프라인에 두 번째 source로 합류**시키는 것이지, 새 시스템을 만드는 게 아니다.

- **Hookdeck**: 멘션을 "탐지"하는 게 아니라, Slack Events API가 보내는 이벤트를 안정적으로 받아
  검증·필터·재시도·전달하는 게이트웨이. (이미 GitHub용으로 쓰는 방식과 동일)
- **탐지**(나에 대한 멘션인지)는 Slack Events 구독 + 텍스트 내 내 user-id 필터로 한다.
- ADR-001(intake는 LLM-free) 유지: receiver는 검증+큐잉만, router는 단순 insert만. LLM/워크트리 없음.

## 2. 데이터 흐름

```
Slack Events API
  (message.channels / message.groups / message.im / message.mpim 구독)
        │  HTTPS, Request URL = Hookdeck Source URL
        ▼
Hookdeck
  ├─ Source(type: Slack): signing-secret 검증 + url_verification 핸드셰이크 자동 처리
  ├─ Filter(선택): event.text 에 "<@MY_USER_ID>" 포함된 것만 통과 (트래픽 절감)
  └─ Destination: hookdeck CLI forward → http://127.0.0.1:8787/slack
        │
        ▼
receiver.py :8787   POST /slack
  - url_verification challenge면 즉시 echo (방어적)
  - (Hookdeck가 이미 검증함) dedupe key = Slack event_id
  - inbox 에 enqueue(event_type="slack"), 202 즉시 반환
        │
        ▼
SQLite inbox ──(tick, 5분마다)──► router.drain()
        │                          process_event("slack", payload)
        │                            → MY_USER_ID 멘션 확인 → mentions 테이블 insert
        │                            (LLM-free, dedupe)
        ▼
SQLite `mentions` 테이블 (신규)
        │ READ
        ▼
dashboard.py :8788
  ├─ GET  /api/mentions        멘션 목록 JSON
  ├─ POST /api/mention-action  read / archive
  └─ 기존 칸반 위에 "📢 멘션 / 확인요청" 섹션 렌더
```

**왜 `cards` 테이블을 재사용하지 않고 `mentions` 신규 테이블인가**
`cards`는 `repo`/`pr_number`가 `NOT NULL`이고 PR 리뷰 상태머신(triage→…→done)에 강하게 묶여 있다.
멘션은 그 라이프사이클과 의미가 다르므로(읽음/안읽음만 필요), 별도 테이블로 분리해
PR 칸반 로직을 오염시키지 않는다. 대시보드 "페이지"는 공유하되 데이터 모델은 분리.

## 3. 변경 파일 (최소 침습)

| 파일 | 변경 |
|------|------|
| `config.json` | `slack_signing_secret`, `slack_user_id`(예: `U01ABC`), `slack_workspace`(permalink용), `slack_path`(`/slack`), `slack_bot_token`(이름 해석용, `xoxb-…`) 추가 |
| `src/db.py` | `mentions` + `slack_names`(id→이름 캐시) 테이블 SCHEMA 추가 + `insert_mention` / `list_mentions` / `set_mention_status` / `name_for`(캐시 조회·저장) 함수 |
| `src/slack_names.py` (신규) | id→이름 해석: 캐시에 있으면 그대로, 없으면 `users.info`/`conversations.info` 1회 호출 후 캐시 저장 |
| `src/receiver.py` | `do_POST`에서 path 분기. `/slack`이면 Slack 처리(challenge echo, dedupe=event_id, inbox enqueue) |
| `src/router.py` | `process_event`에 `elif event_type=="slack": _handle_slack(...)`. 멘션이면 `mentions` insert |
| `src/dashboard.py` | `LANES` 위에 멘션 섹션 + `/api/mentions`, `/api/mention-action` 엔드포인트 + JS 렌더 |
| Hookdeck | Slack용 Source + Connection 1개 추가 (CLI forward 대상에 `/slack` 포함) |

### 3-1. 테이블 스키마

```sql
CREATE TABLE IF NOT EXISTS mentions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id     TEXT UNIQUE,          -- Slack event_id (또는 channel:ts) — dedupe
  channel_id   TEXT,
  channel_name TEXT,                 -- 해석된 채널명 (#dev-team)
  user_id      TEXT,                 -- 보낸 사람 id
  user_name    TEXT,                 -- 해석된 표시명 (홍길동)
  text         TEXT,
  ts           TEXT,                 -- Slack message ts
  permalink    TEXT,                 -- 클릭 시 Slack 메시지로 이동
  status       TEXT NOT NULL DEFAULT 'unread',  -- unread | read | archived
  created_at   REAL NOT NULL
);

-- id → 이름 캐시 (한 번 조회한 user/channel 이름 재사용, API 호출 절감)
CREATE TABLE IF NOT EXISTS slack_names (
  id         TEXT PRIMARY KEY,       -- U… 또는 C…
  kind       TEXT,                   -- user | channel
  name       TEXT,
  updated_at REAL NOT NULL
);
```

`permalink`는 id만으로 구성 가능:
`https://{slack_workspace}.slack.com/archives/{channel_id}/p{ts에서 . 제거}`

### 3-2. receiver.py 분기 (개념)

```python
def do_POST(self):
    path = self.path.rstrip("/")
    body = self.rfile.read(int(self.headers.get("Content-Length", 0)))

    if path == SLACK_PATH.rstrip("/"):
        data = json.loads(body or "{}")
        if data.get("type") == "url_verification":      # 최초 핸드셰이크
            self._reply_raw(200, data["challenge"]); return
        # Hookdeck Slack source가 이미 서명 검증 → 여기선 dedupe만
        event_id = data.get("event_id") or f"{...channel}:{...ts}"
        with db.connect() as c:
            db.enqueue_inbox(c, f"slack:{event_id}", "slack", body.decode())
        self._reply(202, "accepted"); return

    # ... 기존 GitHub 경로 (X-Hub-Signature-256 검증) 그대로
```

### 3-3. router.py 핸들러 (개념)

```python
MY = CFG["slack_user_id"]

def _handle_slack(c, payload):
    ev = payload.get("event", {})
    if ev.get("type") not in ("message", "app_mention"):
        return
    text = ev.get("text", "")
    if f"<@{MY}>" not in text:        # "멘션이면 전부" — 내 id 포함만 통과
        return
    ts = ev.get("ts", "")
    ch = ev.get("channel", "")
    uid = ev.get("user", "")
    db.insert_mention(
        c, event_id=payload.get("event_id") or f"{ch}:{ts}",
        channel_id=ch, channel_name=slack_names.channel(c, ch),  # 캐시→API 1회
        user_id=uid, user_name=slack_names.user(c, uid),         # 캐시→API 1회
        text=text, ts=ts,
        permalink=f"https://{CFG['slack_workspace']}.slack.com/archives/{ch}/p{ts.replace('.', '')}",
    )
    db.log_event(c, "slack_mention", detail={"channel": ch, "ts": ts})
```

> **이름 해석**(`slack_names`): `slack_bot_token`으로 `users.info`/`conversations.info`를
> 호출하되, 결과를 `slack_names` 캐시에 저장해 같은 id는 다시 안 부른다.
> API 실패 시 graceful 하게 id를 그대로 표시(ADR-001 정신: intake가 외부 호출 실패로 막히지 않게).

## 4. Slack 앱 설정

1. Slack 앱 생성 → **Event Subscriptions** 활성화.
2. **Request URL** = Hookdeck Source URL 입력.
3. **Subscribe to bot events**: 멘션을 채널에서 잡으려면
   `message.channels`, `message.groups`, `message.im`, `message.mpim`
   (앱/봇이 해당 채널에 멤버로 들어가 있어야 함).
   - 봇 자신에 대한 멘션만이면 `app_mention` 하나로 충분하지만,
     "사람인 나에 대한 멘션"이므로 message.* 이벤트 + 텍스트 필터가 맞다.
4. **OAuth scopes**: `channels:history`, `groups:history`, `im:history`, `mpim:history`
   + 이름 해석용 `users:read`, `channels:read`, `groups:read`.
   설치 후 발급되는 Bot User OAuth Token(`xoxb-…`)을 `config.json`의 `slack_bot_token`에 넣는다.
5. 워크스페이스 설치 후, 내가 멘션받을 채널들에 봇 초대.

## 5. Hookdeck 설정 (GitHub과 동일 패턴)

- **Source 추가**: type **Slack**, signing secret 입력 → 서명 검증 + url_verification을
  Hookdeck이 엣지에서 자동 처리(= 핸드셰이크 부담을 receiver가 안 짊).
- **Filter(선택, 권장)**: `event.text` 에 `<@MY_USER_ID>` 포함 조건 → 채널 전체 트래픽 중
  내 멘션만 통과시켜 receiver 부하/노이즈 감소.
- **Destination/Connection**: 기존 GitHub처럼 hookdeck CLI forward로 `localhost:8787/slack`.
  (실행 중인 hookdeck listen 명령에 `/slack` 경로 포함되도록 connection 1개 추가)

## 6. 대시보드 UI

기존 칸반(:8788) **상단에 별도 섹션** 추가:

```
📢 멘션 / 확인요청 (안읽음 N)
  ┌──────────────────────────────────────────┐
  │ @보낸사람 · #채널 · 2분 전                  │
  │ "...<@나> 이거 확인 부탁..."   [열기↗][읽음][✕]│
  └──────────────────────────────────────────┘
```

- `GET /api/mentions` → `status != 'archived'` 목록.
- `POST /api/mention-action {id, action: read|archive}`.
- "열기↗"는 `permalink`로 Slack 메시지 이동 → 진짜 "catch-up".

## 7. 향후 확장 (지금은 안 함)

- **확인요청 분류**: 멘션 중 "확인/리뷰/approve" 키워드 또는 LLM 분류로
  `mentions.kind` 부여 → 섹션 내 우선순위 표시. router에 분류 단계만 추가하면 끼워짐.
- **스레드 맥락**: `conversations.replies`로 앞뒤 메시지 몇 개 같이 저장.
- **알림**: 안읽음이 쌓이면 본인 Slack DM으로 요약 push.

## 8. 리스크 / 주의점

- **url_verification 핸드셰이크**: Hookdeck Slack Source를 쓰면 Hookdeck이 처리하므로 보통 문제없음.
  만약 직접 노출 경로로 검증해야 하면 receiver의 challenge echo로 대응(위 3-2 포함됨).
- **트래픽**: `message.channels`는 해당 채널의 **모든** 메시지를 보낸다. Hookdeck Filter +
  router 텍스트 필터 2중으로 걸러야 inbox가 노이즈로 차지 않는다.
- **봇 멤버십**: 봇이 안 들어간 채널의 멘션은 안 잡힌다(Slack 제약). 잡고 싶은 채널에 초대 필요.
- **dedupe**: Slack은 같은 이벤트를 재전송할 수 있으므로 `event_id` UNIQUE 로 멱등 처리(이미 반영).

## 9. 구현 상태 (DONE) + 셋업 체크리스트

코드는 구현·검증 완료. 남은 건 외부 설정뿐:

**구현된 파일**
- `src/db.py` — `mentions` / `slack_names` 테이블 + 함수
- `src/slack_names.py` (신규) — id→이름 해석(캐시, 실패 시 id fallback)
- `src/receiver.py` — `POST /slack` (challenge echo, 서명 검증, inbox enqueue)
- `src/router.py` — `_handle_slack` (내 멘션만 `mentions` insert)
- `src/dashboard.py` — `/api/mentions`, `/api/mention-action` + 상단 "📢 멘션" 섹션
- `config.json` — `slack_*` 필드
- `docs/slack-app-manifest.yaml` — Slack 앱 manifest

**셋업 (사람이 할 일)**
1. **Slack 앱 생성**: api.slack.com/apps → "From a manifest" → `docs/slack-app-manifest.yaml` 붙여넣기.
2. 워크스페이스 설치 → **Bot User OAuth Token**(`xoxb-…`)과 **Signing Secret** 복사.
3. **Hookdeck**: Slack Source 추가(Signing Secret 입력) → Connection 만들고
   Destination 을 기존 GitHub과 같은 hookdeck CLI forward 로 `localhost:8787/slack` 지정.
   - (권장) Filter: `event.text` 에 `<@내USERID>` 포함만 통과.
4. Slack 앱 **Event Subscriptions → Request URL** = Hookdeck Source URL (저장 시 자동 검증).
5. **`config.json`** 채우기:
   - `slack_signing_secret`, `slack_bot_token`(`xoxb-…`),
   - `slack_user_id`(내 멤버 ID, 예 `U01ABC234` — Slack 프로필 → "멤버 ID 복사"),
   - `slack_workspace`(예 `myteam` → permalink `myteam.slack.com`).
6. 멘션받을 채널에 봇 초대(`/invite @hermes-mentions`).
7. **재시작**: receiver(:8787) + dashboard(:8788) 프로세스 재기동 → 끝.
   (`mentions`/`slack_names` 테이블은 `db.init()`에서 자동 생성됨)

검증: 채널에서 나를 멘션 → 다음 tick(최대 5분) 후 대시보드 상단 "📢 멘션" 섹션에 표시.
즉시 확인하려면 `python -m src.tick` 1회 수동 실행.
```

