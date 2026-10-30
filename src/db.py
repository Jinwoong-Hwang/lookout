"""SQLite Kanban store. All state + audit log lives here.

Lanes (cards.status):
  intake -> reviewing -> verifying -> commenting -> monitoring -> lgtm
  approve cards: approve_blocked -> approving -> done
  terminal: done / archived
"""
import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_id TEXT UNIQUE,
  event_type  TEXT,
  raw         TEXT NOT NULL,
  received_at REAL NOT NULL,
  processed   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cards (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  key        TEXT UNIQUE NOT NULL,
  kind       TEXT NOT NULL,              -- root | review | approve
  repo       TEXT NOT NULL,             -- owner/repo
  pr_number  INTEGER NOT NULL,
  head_sha   TEXT,
  base_sha   TEXT,
  status     TEXT NOT NULL,
  blocked    INTEGER NOT NULL DEFAULT 0,
  assignee   TEXT,
  payload    TEXT,                       -- JSON
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id    INTEGER NOT NULL,
  repo       TEXT NOT NULL,
  pr_number  INTEGER NOT NULL,
  head_sha   TEXT,
  fp         TEXT NOT NULL,              -- fingerprint for dedupe
  title      TEXT,
  body       TEXT,
  file       TEXT,
  line       TEXT,
  severity   TEXT,
  confidence TEXT,
  status     TEXT NOT NULL,             -- pending_verify|confirmed|rejected|posted|resolved|dismissed|deferred|unresolved
  comment_id TEXT,
  decision_head TEXT,
  decision_comment_id TEXT,
  decision_evidence TEXT,
  decision_follow_up TEXT,
  last_judged_head TEXT,              -- closure 를 마지막으로 돌린 head
  last_seen_reply TEXT,               -- 그때 본 작성자 회신 중 가장 최근 시각
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(repo, pr_number, fp)
);

CREATE TABLE IF NOT EXISTS seen_heads (
  repo      TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  head_sha  TEXT NOT NULL,
  seen_at   REAL NOT NULL,
  PRIMARY KEY (repo, pr_number, head_sha)
);

CREATE TABLE IF NOT EXISTS events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     REAL NOT NULL,
  key    TEXT,
  type   TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS review_feedback_snapshots (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id        INTEGER NOT NULL,
  repo           TEXT NOT NULL,
  pr_number      INTEGER NOT NULL,
  head_sha       TEXT,
  profile_type   TEXT,
  snapshot_type  TEXT NOT NULL,          -- pr_closed | weekly_open | manual
  comment_id     TEXT NOT NULL,
  comment_url    TEXT,
  reactions      TEXT,                   -- JSON: +1/-1/confused/total_count
  author_replies TEXT,                   -- JSON array of author replies after bot comment
  outcome        TEXT,                   -- JSON: state, closure counts, etc.
  created_at     REAL NOT NULL,
  UNIQUE(card_id, snapshot_type, comment_id)
);

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

-- Slack mentions catch-up (separate from PR cards; only unread/read/archived).
CREATE TABLE IF NOT EXISTS mentions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id     TEXT UNIQUE,                       -- Slack event_id (or channel:ts) — dedupe
  channel_id   TEXT,
  channel_name TEXT,
  user_id      TEXT,
  user_name    TEXT,
  text         TEXT,
  ts           TEXT,
  permalink    TEXT,
  status       TEXT NOT NULL DEFAULT 'unread',    -- unread | read | archived
  created_at   REAL NOT NULL
);

-- id -> display name cache (avoid re-hitting Slack API for the same id).
CREATE TABLE IF NOT EXISTS slack_names (
  id         TEXT PRIMARY KEY,                     -- U… or C…
  kind       TEXT,                                 -- user | channel
  name       TEXT,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_findings_card ON findings(card_id);
CREATE INDEX IF NOT EXISTS idx_inbox_processed ON inbox(processed);
CREATE INDEX IF NOT EXISTS idx_mentions_status ON mentions(status);
CREATE INDEX IF NOT EXISTS idx_feedback_card ON review_feedback_snapshots(card_id);
CREATE INDEX IF NOT EXISTS idx_events_key_type_ts ON events(key,type,ts);
"""


def now() -> float:
    return time.time()


@contextmanager
def connect():
    # autocommit (isolation_level=None): 각 write가 즉시 커밋 → 긴 LLM 호출 동안
    # write 잠금을 안 쥐어서 다른 프로세스(대시보드/다른 tick)가 'database is locked' 안 남.
    conn = sqlite3.connect(config.path(config.CFG["db_path"]), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init():
    with connect() as c:
        c.executescript(SCHEMA)
        # migration: rowid-backed `id` added no selectivity; `ts` covers cooldown reads.
        c.execute("DROP INDEX IF EXISTS idx_events_key_type_id")
        # migration: review engine per card (claude | codex)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(cards)").fetchall()]
        if "engine" not in cols:
            c.execute("ALTER TABLE cards ADD COLUMN engine TEXT DEFAULT 'claude'")
        finding_cols = {r["name"] for r in c.execute("PRAGMA table_info(findings)").fetchall()}
        for name in ("decision_head", "decision_comment_id", "decision_evidence",
                     "decision_follow_up", "last_judged_head", "last_seen_reply"):
            if name not in finding_cols:
                c.execute(f"ALTER TABLE findings ADD COLUMN {name} TEXT")
        _drop_line_from_fps(c)


def _fp_without_line(fp: str):
    """옛 지문 repo#pr:file:line:rule → repo#pr:file:rule. 이미 새 형식이면 None.

    파일 경로에는 ':' 이 없으므로, rule 을 떼어낸 나머지에 ':' 이 남아 있으면
    그게 줄 번호다.
    """
    head, hash_, tail = fp.partition("#")
    if not hash_ or ":" not in tail:
        return None
    pr, _, rest = tail.partition(":")
    body, sep, rule = rest.rpartition(":")
    if not sep or ":" not in body:
        return None
    return f"{head}#{pr}:{body.rsplit(':', 1)[0]}:{rule}"


def _drop_line_from_fps(c):
    """지문에서 줄 번호를 뺀다 — 안 하면 기존 지적 전부가 새 지문이 되어 한 번씩
    중복 게시된다. 충돌(같은 file+rule 이 여러 줄에 흩어져 있던 경우)은 최근에
    갱신된 행만 남긴다 — 그게 합쳐져야 할 같은 문제다."""
    rows = c.execute("SELECT id, fp, repo, pr_number, updated_at FROM findings").fetchall()
    plan = {}
    for r in rows:
        new = _fp_without_line(r["fp"])
        if new:
            plan.setdefault((r["repo"], r["pr_number"], new), []).append(r)
    merged = 0
    for (_repo, _pr, new), group in plan.items():
        keep = max(group, key=lambda r: r["updated_at"] or 0)
        for r in group:
            if r["id"] != keep["id"]:
                c.execute("DELETE FROM findings WHERE id=?", (r["id"],))
                merged += 1
        existing = c.execute(
            "SELECT id FROM findings WHERE repo=? AND pr_number=? AND fp=? AND id!=?",
            (_repo, _pr, new, keep["id"])).fetchone()
        if existing:  # 이미 새 형식 행이 있으면 옛 행을 버린다
            c.execute("DELETE FROM findings WHERE id=?", (keep["id"],))
            merged += 1
            continue
        c.execute("UPDATE findings SET fp=? WHERE id=?", (new, keep["id"]))
    if plan:
        log_event(c, "fp_line_migration",
                  detail={"rewritten": len(plan), "merged": merged})


def get_meta(c, k: str, default=None):
    row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(c, k: str, v: str):
    c.execute("INSERT INTO meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def log_event(c, type_: str, key: str = None, detail=None):
    c.execute(
        "INSERT INTO events(ts, key, type, detail) VALUES (?,?,?,?)",
        (now(), key, type_, json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


# ---- inbox ----------------------------------------------------------------
def enqueue_inbox(c, delivery_id: str, event_type: str, raw: str) -> bool:
    """Returns False if this delivery_id was already seen (dedupe)."""
    try:
        c.execute(
            "INSERT INTO inbox(delivery_id, event_type, raw, received_at) VALUES (?,?,?,?)",
            (delivery_id, event_type, raw, now()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def pending_inbox(c):
    return c.execute(
        "SELECT * FROM inbox WHERE processed=0 ORDER BY id ASC"
    ).fetchall()


def mark_inbox_done(c, inbox_id: int):
    c.execute("UPDATE inbox SET processed=1 WHERE id=?", (inbox_id,))


# ---- cards ----------------------------------------------------------------
def get_card(c, key: str):
    return c.execute("SELECT * FROM cards WHERE key=?", (key,)).fetchone()


def upsert_card(c, key, kind, repo, pr_number, status, head_sha=None,
                base_sha=None, blocked=0, assignee=None, payload=None):
    existing = get_card(c, key)
    pj = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    if existing:
        c.execute(
            """UPDATE cards SET head_sha=COALESCE(?,head_sha),
               base_sha=COALESCE(?,base_sha), updated_at=? WHERE key=?""",
            (head_sha, base_sha, now(), key),
        )
        return existing["id"]
    c.execute(
        """INSERT INTO cards(key,kind,repo,pr_number,head_sha,base_sha,status,
           blocked,assignee,payload,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, kind, repo, pr_number, head_sha, base_sha, status,
         blocked, assignee, pj, now(), now()),
    )
    return c.execute("SELECT id FROM cards WHERE key=?", (key,)).fetchone()["id"]


def set_status(c, card_id: int, status: str, blocked=None, assignee=None):
    if blocked is None and assignee is None:
        c.execute("UPDATE cards SET status=?, updated_at=? WHERE id=?", (status, now(), card_id))
    else:
        c.execute(
            "UPDATE cards SET status=?, blocked=COALESCE(?,blocked), assignee=COALESCE(?,assignee), updated_at=? WHERE id=?",
            (status, blocked, assignee, now(), card_id),
        )


def gate(c, card, to_status: str, blocked=None, event: str = None, detail=None) -> bool:
    """사람 게이트 전이 — 현재 상태를 조건에 넣어 한 번에 바꾼다(CAS).

    읽고→검사하고→쓰는 세 단계로 하면 대시보드가 ThreadingHTTPServer 이고
    connect() 가 autocommit(아래 connect 참고)이라, 같은 카드에 대한 두 요청이
    둘 다 검사를 통과해 게이트가 두 번 열린다(중복 클릭·낡은 탭). UPDATE 의 WHERE
    에 status 를 넣으면 두 번째는 rowcount 0 이 되어 막힌다.

    막혔으면 gate_stale 을 남긴다 — 사람이 눌렀는데 아무 일도 없는 것이 최악이다."""
    cur = card["status"]
    if blocked is None:
        cur_row = c.execute(
            "UPDATE cards SET status=?, updated_at=? WHERE id=? AND status=?",
            (to_status, now(), card["id"], cur))
    else:
        cur_row = c.execute(
            "UPDATE cards SET status=?, blocked=?, updated_at=? WHERE id=? AND status=?",
            (to_status, blocked, now(), card["id"], cur))
    if cur_row.rowcount != 1:
        actual = c.execute("SELECT status FROM cards WHERE id=?", (card["id"],)).fetchone()
        log_event(c, "gate_stale", card["key"],
                  {"to": to_status, "expected": cur,
                   "actual": actual["status"] if actual else None})
        return False
    if event:
        log_event(c, event, card["key"], detail)
    return True


def set_engine(c, card_id: int, engine: str):
    c.execute("UPDATE cards SET engine=?, updated_at=? WHERE id=?", (engine, now(), card_id))


def cards_in(c, statuses, kind=None):
    """Cards sitting in the given lanes, oldest-touched first.

    이 함수는 status만 본다. 그래서 새 kind가 기존 상태 이름을 하나라도 재사용하면
    그 카드가 남의 스테이지(예: reviewer.process)로 조용히 들어간다. 호출부가 자기
    kind를 넘겨 거르는 것이 유일한 구조적 방어이므로 스테이지 호출은 kind를 명시한다.
    """
    q = ",".join("?" * len(statuses))
    if kind is None:
        return c.execute(
            f"SELECT * FROM cards WHERE status IN ({q}) ORDER BY updated_at ASC", statuses
        ).fetchall()
    return c.execute(
        f"SELECT * FROM cards WHERE status IN ({q}) AND kind=? ORDER BY updated_at ASC",
        (*statuses, kind),
    ).fetchall()


def merge_payload(c, card_id: int, patch: dict) -> dict:
    """payload(JSON)에 키를 병합한다.

    한 카드의 payload를 poller(제목·라벨)·대시보드(추가 지시)·워커(스레드 id)가 각각
    다른 키로 쓴다. 통째로 덮으면 서로의 값을 지우므로 항상 병합한다."""
    row = c.execute("SELECT payload FROM cards WHERE id=?", (card_id,)).fetchone()
    try:
        cur = json.loads(row["payload"]) if row and row["payload"] else {}
    except (TypeError, ValueError):
        cur = {}
    cur.update(patch)
    c.execute("UPDATE cards SET payload=?, updated_at=? WHERE id=?",
              (json.dumps(cur, ensure_ascii=False), now(), card_id))
    return cur


# ---- seen heads (ADR-003 onboarding backfill skip) ------------------------
def is_seen_head(c, repo, pr, head) -> bool:
    return c.execute(
        "SELECT 1 FROM seen_heads WHERE repo=? AND pr_number=? AND head_sha=?",
        (repo, pr, head),
    ).fetchone() is not None


def mark_seen_head(c, repo, pr, head):
    c.execute(
        "INSERT OR IGNORE INTO seen_heads(repo,pr_number,head_sha,seen_at) VALUES (?,?,?,?)",
        (repo, pr, head, now()),
    )


# ---- findings -------------------------------------------------------------
def upsert_finding(c, card_id, repo, pr, head, fp, title, body, file, line,
                   severity, confidence, status):
    try:
        c.execute(
            """INSERT INTO findings(card_id,repo,pr_number,head_sha,fp,title,body,
               file,line,severity,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (card_id, repo, pr, head, fp, title, body, file, str(line),
             severity, confidence, status, now(), now()),
        )
        return True
    except sqlite3.IntegrityError:
        return False  # already known (dedupe by repo/pr/fp)


def findings_for_card(c, card_id, status=None):
    if status:
        return c.execute(
            "SELECT * FROM findings WHERE card_id=? AND status=?", (card_id, status)
        ).fetchall()
    return c.execute("SELECT * FROM findings WHERE card_id=?", (card_id,)).fetchall()


def postable_findings_for_card(c, card_id):
    """Findings on this card the author still needs to see.

    'unresolved' belongs here next to 'confirmed': the reviewer reattaches a
    prior unresolved finding as 'confirmed' to force a reminder, and the
    commenter's own closure re-check can downgrade it back to 'unresolved'
    moments later.  Selecting 'confirmed' alone dropped that reminder silently.
    """
    return c.execute(
        """SELECT * FROM findings WHERE card_id=?
           AND status IN ('confirmed','unresolved') ORDER BY id""",
        (card_id,),
    ).fetchall()


def prior_open_findings(c, repo, pr, exclude_card_id):
    """Earlier findings that need closure at a new head.

    Dismissed/deferred findings are retained here only so new code can reopen
    them; they are never returned by unresolved_findings for re-commenting.
    """
    return c.execute(
        """SELECT * FROM findings WHERE repo=? AND pr_number=? AND card_id!=?
           AND status IN ('posted','confirmed','unresolved','dismissed','deferred',
                          'dismiss_pending','defer_pending')""",
        (repo, pr, exclude_card_id),
    ).fetchall()


def closure_counts(c, repo, pr):
    rows = c.execute(
        """SELECT status, COUNT(*) n FROM findings
           WHERE repo=? AND pr_number=?
             AND status IN ('resolved','dismissed','deferred','unresolved',
                            'dismiss_pending','defer_pending')
           GROUP BY status""",
        (repo, pr),
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def unresolved_findings_count(c, repo, pr) -> int:
    row = c.execute(
        "SELECT COUNT(*) n FROM findings WHERE repo=? AND pr_number=? AND status='unresolved'",
        (repo, pr),
    ).fetchone()
    return int(row["n"] if row else 0)


OPEN_STATUSES = ("posted", "confirmed", "unresolved", "pending_verify",
                 "dismiss_pending", "defer_pending")


def open_findings_count(c, repo, pr) -> int:
    """이 PR 에 아직 닫히지 않은 지적 수 — LGTM 차단 기준.

    unresolved 만 세면 안 된다. 호출 게이트가 재판정을 건너뛰면 지적이 posted /
    confirmed 로 남는데, 그건 '문제가 없다' 가 아니라 '다시 묻지 않았다' 는 뜻이다.
    게이트 전에는 closure 가 매번 판정해 unresolved 로 내려왔기 때문에 이 구멍이
    없었다(셀프 리뷰 2회차 지적).
    """
    row = c.execute(
        "SELECT COUNT(*) n FROM findings WHERE repo=? AND pr_number=? "
        f"AND status IN ({','.join('?' * len(OPEN_STATUSES))})",
        (repo, pr, *OPEN_STATUSES),
    ).fetchone()
    return int(row["n"] if row else 0)


def unresolved_findings(c, repo, pr):
    return c.execute(
        "SELECT * FROM findings WHERE repo=? AND pr_number=? AND status='unresolved'",
        (repo, pr),
    ).fetchall()


def pending_decision_findings(c, repo, pr):
    return c.execute(
        """SELECT * FROM findings WHERE repo=? AND pr_number=?
           AND status IN ('dismiss_pending','defer_pending')""",
        (repo, pr),
    ).fetchall()


def posted_findings_for_closure(c, repo, pr):
    """Findings with a bot marker that can have an author reply to re-check."""
    return c.execute(
        """SELECT * FROM findings WHERE repo=? AND pr_number=? AND comment_id IS NOT NULL
           AND status IN ('posted','confirmed','unresolved','dismissed','deferred',
                          'dismiss_pending','defer_pending')""",
        (repo, pr),
    ).fetchall()


def revalidate_finding(c, card_id, repo, pr, head, fp, title, body, file, line,
                       severity, confidence):
    """Move a duplicate fingerprint into fresh verification unless same-head sticky."""
    row = c.execute(
        "SELECT * FROM findings WHERE repo=? AND pr_number=? AND fp=?",
        (repo, pr, fp),
    ).fetchone()
    if not row:
        return "missing"
    def meaningful_body(raw):
        try:
            value = json.loads(raw or "{}")
            return {k: v for k, v in value.items() if v not in (None, "", [], {})}
        except (json.JSONDecodeError, AttributeError):
            return raw or ""

    # line 은 비교하지 않는다 — 지문에서 뺀 이유와 같다. 인용 구간은 실행마다
    # 흔들리는데(89-94→87-94→91-94 실측) 그걸 '내용이 바뀌었다' 로 읽으면 작성자
    # 결정이 통째로 지워진다. 위치는 표시용이라 아래에서 값만 갱신한다.
    same_payload = meaningful_body(row["body"]) == meaningful_body(body) and all(
        (row[key] or "") == (str(value) if value is not None else "")
        for key, value in {
            "title": title, "file": file,
            "severity": severity, "confidence": confidence,
        }.items()
    )
    sticky = (row["status"] in {"dismiss_pending", "defer_pending"} and same_payload) or (
        row["status"] in {"dismissed", "deferred"}
        and (not row["decision_head"] or row["decision_head"] == head)
        and same_payload)
    if sticky:
        if (row["line"] or "") != (str(line) if line is not None else ""):
            c.execute("UPDATE findings SET line=? WHERE id=?", (line, row["id"]))
        return "sticky"
    previous = row["status"]
    c.execute(
        """UPDATE findings SET card_id=?,head_sha=?,title=?,body=?,file=?,line=?,
             severity=?,confidence=?,status='pending_verify',decision_head=NULL,
             decision_comment_id=NULL,decision_evidence=NULL,decision_follow_up=NULL,updated_at=?
           WHERE id=?""",
        (card_id, head, title, body, file, str(line), severity, confidence,
         now(), row["id"]),
    )
    return previous


def reattach_finding(c, finding_id, card_id, status):
    c.execute("UPDATE findings SET card_id=?, status=?, updated_at=? WHERE id=?",
              (card_id, status, now(), finding_id))


def set_finding_status(c, finding_id, status, comment_id=None):
    c.execute(
        "UPDATE findings SET status=?, comment_id=COALESCE(?,comment_id), updated_at=? WHERE id=?",
        (status, comment_id, now(), finding_id),
    )


def mark_finding_judged(c, finding_id, head, newest_reply):
    """closure 를 돌린 시점을 남긴다 — 다음 head 에서 '뭐가 바뀌었나' 의 기준점."""
    c.execute("UPDATE findings SET last_judged_head=?, last_seen_reply=? WHERE id=?",
              (head, newest_reply or "", finding_id))


def set_finding_decision(c, finding_id, status, head, comment_id, evidence, follow_up=""):
    """Persist a decision; follow-up references belong to deferred decisions only."""
    c.execute(
        """UPDATE findings SET status=?,decision_head=?,decision_comment_id=?,
             decision_evidence=?,decision_follow_up=?,updated_at=? WHERE id=?""",
        (status, head, str(comment_id), evidence,
         follow_up if status in {"defer_pending", "deferred"} and follow_up else None,
         now(), finding_id),
    )


def clear_finding_decision(c, finding_id, status):
    c.execute(
        """UPDATE findings SET status=?,decision_head=NULL,decision_comment_id=NULL,
             decision_evidence=NULL,decision_follow_up=NULL,updated_at=? WHERE id=?""",
        (status, now(), finding_id),
    )


# ---- slack mentions -------------------------------------------------------
def insert_mention(c, event_id, channel_id, channel_name, user_id, user_name,
                   text, ts, permalink) -> bool:
    """Returns False if this event_id was already stored (dedupe)."""
    try:
        c.execute(
            """INSERT INTO mentions(event_id,channel_id,channel_name,user_id,
               user_name,text,ts,permalink,status,created_at)
               VALUES (?,?,?,?,?,?,?,?, 'unread', ?)""",
            (event_id, channel_id, channel_name, user_id, user_name, text, ts,
             permalink, now()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def list_mentions(c, include_archived=False):
    if include_archived:
        return c.execute("SELECT * FROM mentions ORDER BY created_at DESC").fetchall()
    return c.execute(
        "SELECT * FROM mentions WHERE status!='archived' ORDER BY created_at DESC"
    ).fetchall()


def set_mention_status(c, mention_id: int, status: str):
    c.execute("UPDATE mentions SET status=? WHERE id=?", (status, mention_id))


def purge_old(c, days: int = 14) -> dict:
    """N일 지난 종료(archived) 카드 + 거기 묶인 findings/events 삭제.

    살아있는(non-archived) 카드의 데이터는 절대 건드리지 않음. findings/events를
    먼저 지우고(아직 카드 존재) 카드를 지운다. 카드가 이미 사라진 고아 이벤트도 정리.
    열린 지적(OPEN_STATUSES)은 카드가 archived 여도 남긴다 — 아래 주석 참고."""
    cutoff = now() - days * 86400
    sub = "(SELECT id FROM cards WHERE status='archived' AND updated_at < ?)"
    subk = "(SELECT key FROM cards WHERE status='archived' AND updated_at < ?)"
    # 열린 지적은 남긴다. 재게시 쿨다운이 지적을 옛 카드에 남겨 두므로, 카드가
    # archived 되면 열려 있는 PR 의 미해결 지적까지 함께 지워졌다(셀프 리뷰 3회차).
    # 게시 여부와 보존 수명은 별개다.
    findings = c.execute(
        f"DELETE FROM findings WHERE card_id IN {sub} "
        f"AND status NOT IN ({','.join('?' * len(OPEN_STATUSES))})",
        (cutoff, *OPEN_STATUSES)).rowcount
    events = c.execute(f"DELETE FROM events WHERE key IN {subk}", (cutoff,)).rowcount
    cards = c.execute(
        "DELETE FROM cards WHERE status='archived' AND updated_at < ? "
        "AND id NOT IN (SELECT card_id FROM findings)", (cutoff,)
    ).rowcount
    events += c.execute(
        "DELETE FROM events WHERE ts < ? AND key IS NOT NULL "
        "AND key NOT IN (SELECT key FROM cards)", (cutoff,)
    ).rowcount
    return {"cards": cards, "findings": findings, "events": events}


def get_slack_name(c, sid: str):
    row = c.execute("SELECT name FROM slack_names WHERE id=?", (sid,)).fetchone()
    return row["name"] if row else None


def set_slack_name(c, sid: str, kind: str, name: str):
    c.execute(
        """INSERT INTO slack_names(id,kind,name,updated_at) VALUES (?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
           updated_at=excluded.updated_at""",
        (sid, kind, name, now()),
    )
