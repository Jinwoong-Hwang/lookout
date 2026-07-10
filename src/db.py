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
  status     TEXT NOT NULL,             -- pending_verify|confirmed|rejected|posted|resolved|unresolved
  comment_id TEXT,
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

-- 작업 기록(work log): 날짜별로 쌓이는 저널. git 커밋/PR + Claude/Codex 세션.
-- ref UNIQUE로 재스캔 시 중복 방지. 누적이 목적이라 purge_old에서 건드리지 않음.
CREATE TABLE IF NOT EXISTS work_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  day        TEXT NOT NULL,             -- YYYY-MM-DD (local)
  ts         REAL NOT NULL,             -- epoch, 정렬용
  source     TEXT NOT NULL,             -- git | claude | codex
  kind       TEXT,                      -- commit | pr_merged | pr_opened | session
  repo       TEXT,
  ref        TEXT UNIQUE,               -- dedupe key (sha / repo#pr / claude:sid / codex:sid)
  title      TEXT,
  url        TEXT,
  branch     TEXT,
  created_at REAL NOT NULL
);

-- 작업 기록 날짜별 요약. sig(그날 항목 해시)가 바뀔 때만 재생성.
CREATE TABLE IF NOT EXISTS worklog_summary (
  day        TEXT PRIMARY KEY,
  summary    TEXT,
  sig        TEXT,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_findings_card ON findings(card_id);
CREATE INDEX IF NOT EXISTS idx_inbox_processed ON inbox(processed);
CREATE INDEX IF NOT EXISTS idx_mentions_status ON mentions(status);
CREATE INDEX IF NOT EXISTS idx_worklog_ts ON work_log(ts);
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
        # migration: review engine per card (claude | codex)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(cards)").fetchall()]
        if "engine" not in cols:
            c.execute("ALTER TABLE cards ADD COLUMN engine TEXT DEFAULT 'claude'")


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


def set_engine(c, card_id: int, engine: str):
    c.execute("UPDATE cards SET engine=?, updated_at=? WHERE id=?", (engine, now(), card_id))


def cards_in(c, statuses):
    q = ",".join("?" * len(statuses))
    return c.execute(
        f"SELECT * FROM cards WHERE status IN ({q}) ORDER BY updated_at ASC", statuses
    ).fetchall()


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


def prior_open_findings(c, repo, pr, exclude_card_id):
    """Findings raised on earlier review cards of this PR that aren't resolved yet."""
    return c.execute(
        """SELECT * FROM findings WHERE repo=? AND pr_number=? AND card_id!=?
           AND status IN ('posted','confirmed','unresolved')""",
        (repo, pr, exclude_card_id),
    ).fetchall()


def closure_counts(c, repo, pr):
    rows = c.execute(
        "SELECT status, COUNT(*) n FROM findings WHERE repo=? AND pr_number=? AND status IN ('resolved','unresolved') GROUP BY status",
        (repo, pr),
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def unresolved_findings_count(c, repo, pr) -> int:
    row = c.execute(
        "SELECT COUNT(*) n FROM findings WHERE repo=? AND pr_number=? AND status='unresolved'",
        (repo, pr),
    ).fetchone()
    return int(row["n"] if row else 0)


def unresolved_findings(c, repo, pr):
    return c.execute(
        "SELECT * FROM findings WHERE repo=? AND pr_number=? AND status='unresolved'",
        (repo, pr),
    ).fetchall()


def reattach_finding(c, finding_id, card_id, status):
    c.execute("UPDATE findings SET card_id=?, status=?, updated_at=? WHERE id=?",
              (card_id, status, now(), finding_id))


def set_finding_status(c, finding_id, status, comment_id=None):
    c.execute(
        "UPDATE findings SET status=?, comment_id=COALESCE(?,comment_id), updated_at=? WHERE id=?",
        (status, comment_id, now(), finding_id),
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
    먼저 지우고(아직 카드 존재) 카드를 지운다. 카드가 이미 사라진 고아 이벤트도 정리."""
    cutoff = now() - days * 86400
    sub = "(SELECT id FROM cards WHERE status='archived' AND updated_at < ?)"
    subk = "(SELECT key FROM cards WHERE status='archived' AND updated_at < ?)"
    findings = c.execute(f"DELETE FROM findings WHERE card_id IN {sub}", (cutoff,)).rowcount
    events = c.execute(f"DELETE FROM events WHERE key IN {subk}", (cutoff,)).rowcount
    cards = c.execute(
        "DELETE FROM cards WHERE status='archived' AND updated_at < ?", (cutoff,)
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
