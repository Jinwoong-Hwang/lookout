"""작업 기록(work log) — 날짜별로 쌓이는 작업 저널.

세 소스를 한 타임라인에 병합:
  💻 git    : 내 커밋 + 머지/생성한 PR (gh)
  🤖 claude : ~/.claude/projects/*/*.jsonl 세션의 첫 실질 프롬프트(=태스크)
  🤖 codex  : ~/.codex/history.jsonl 세션의 첫 실질 프롬프트

전부 **로컬** 데이터(이 맥의 ~/.claude·~/.codex). work_log 테이블에 ref UNIQUE로
dedup 저장 → 누적. git·에이전트 모두 과거 로그가 있어 소급 백필 가능.
LLM 미관여(사실만). Lookout 자체 headless 리뷰 세션은 제외.
"""
import datetime
import glob
import hashlib
import json
import os

from . import claude_runner, db, ghclient
from .config import CFG, HERMES_HOME

HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_HISTORY = os.path.join(HOME, ".codex", "history.jsonl")


def _excluded_cwd(cwd: str) -> bool:
    """Lookout 자신이 돌린 headless 세션 제외 — 데몬 홈(HERMES_HOME) 및 그 하위
    워크트리에서 난 claude 호출은 '내 작업'이 아니라 봇 작업이라 로그에서 뺀다."""
    if not cwd:
        return False
    return cwd == HERMES_HOME or cwd.startswith(HERMES_HOME + os.sep)


def _day(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def _iso_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _is_command(text: str) -> bool:
    t = (text or "").strip()
    return (not t) or t.startswith("<command-") or t.startswith("/") or len(t) < 4


def _clean(text: str, n: int = 140) -> str:
    if not text:
        return ""
    return text.strip().splitlines()[0].strip()[:n]


def _range(days: int):
    start_d = datetime.date.today() - datetime.timedelta(days=max(0, days - 1))
    start_local = datetime.datetime(start_d.year, start_d.month, start_d.day).astimezone()
    since = start_local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.datetime.now().astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_range = f"{start_d.isoformat()}..{datetime.date.today().isoformat()}"
    return since, until, day_range


# ── 소스별 수집 ─────────────────────────────────────────────
def collect_git(days: int) -> list:
    since, until, day_range = _range(days)
    allow = set(CFG.get("allowlist", []))
    out = []
    for repo in CFG.get("allowlist", []):
        short = repo.split("/")[-1]
        try:
            commits = ghclient.my_commits(repo, since, until, cap=200)
        except ghclient.GhError:
            commits = []
        for cm in commits:
            ts = _iso_ts(cm.get("date"))
            if ts is None:
                continue
            out.append({"day": _day(ts), "ts": ts, "source": "git", "kind": "commit",
                        "repo": short, "ref": f"git:{cm['sha']}", "title": cm["msg"],
                        "url": cm.get("url", ""), "branch": ""})
    for p in ghclient.my_prs_merged(day_range):
        if p["repo"] not in allow:
            continue
        ts = _iso_ts(p.get("closed_at")) or _iso_ts(p.get("created_at"))
        if ts is None:
            continue
        out.append({"day": _day(ts), "ts": ts, "source": "git", "kind": "pr_merged",
                    "repo": p["repo"].split("/")[-1], "ref": f"prm:{p['repo']}#{p['number']}",
                    "title": p["title"], "url": p.get("url", ""), "branch": ""})
    for p in ghclient.my_prs_created(day_range):
        if p["repo"] not in allow:
            continue
        ts = _iso_ts(p.get("created_at"))
        if ts is None:
            continue
        out.append({"day": _day(ts), "ts": ts, "source": "git", "kind": "pr_opened",
                    "repo": p["repo"].split("/")[-1], "ref": f"pro:{p['repo']}#{p['number']}",
                    "title": p["title"], "url": p.get("url", ""), "branch": ""})
    return out


def _claude_session(path: str):
    """세션 jsonl에서 첫 실질 사용자 프롬프트(=태스크) 1건 추출. 없으면 None."""
    sid = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = d.get("sessionId") or sid
                if d.get("type") != "user" or d.get("isMeta"):
                    continue
                cwd = d.get("cwd", "") or ""
                if _excluded_cwd(cwd):
                    return None
                m = d.get("message", {}) or {}
                c = m.get("content")
                txt = c if isinstance(c, str) else " ".join(
                    p.get("text", "") for p in (c or []) if isinstance(p, dict) and p.get("type") == "text")
                if _is_command(txt):
                    continue
                ts = _iso_ts(d.get("timestamp"))
                if ts is None:
                    continue
                return {"day": _day(ts), "ts": ts, "source": "claude", "kind": "session",
                        "repo": os.path.basename(cwd) or "?",
                        "ref": f"claude:{sid or os.path.basename(path)}",
                        "title": _clean(txt), "url": "", "branch": d.get("gitBranch") or ""}
    except OSError:
        return None
    return None


def collect_claude(days: int) -> list:
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).timestamp()
    out = []
    for f in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        try:
            if os.path.getmtime(f) < cutoff:
                continue
        except OSError:
            continue
        e = _claude_session(f)
        if e:
            out.append(e)
    return out


def collect_codex(days: int) -> list:
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).timestamp()
    if not os.path.isfile(CODEX_HISTORY):
        return []
    seen, out = set(), []
    try:
        with open(CODEX_HISTORY, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = d.get("session_id")
                try:
                    ts = float(d.get("ts"))
                except (TypeError, ValueError):
                    continue
                if ts < cutoff or sid in seen or _is_command(d.get("text", "")):
                    continue
                seen.add(sid)  # 세션당 첫 실질 프롬프트만
                out.append({"day": _day(ts), "ts": ts, "source": "codex", "kind": "session",
                            "repo": "", "ref": f"codex:{sid}", "title": _clean(d.get("text", "")),
                            "url": "", "branch": ""})
    except OSError:
        return []
    return out


# ── 동기화 / 조회 ───────────────────────────────────────────
def _upsert(c, e):
    c.execute(
        """INSERT OR IGNORE INTO work_log(day,ts,source,kind,repo,ref,title,url,branch,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (e["day"], e["ts"], e["source"], e["kind"], e["repo"], e["ref"],
         e["title"], e["url"], e["branch"], db.now()),
    )


def _since(c) -> str:
    """기록 시작 하한(날짜). 최초 실행 시 '오늘'로 고정 → 과거 소급 없이 오늘부터 누적."""
    v = db.get_meta(c, "worklog_since")
    if not v:
        v = datetime.date.today().isoformat()
        db.set_meta(c, "worklog_since", v)
    return v


# 프롬프트를 바꾸면 이 버전을 올린다 → sig가 달라져 기존 요약이 자동 재생성됨.
SUMMARY_PROMPT_VERSION = "9"

SUMMARY_PROMPT = """아래는 개발자가 {DAY}에 남긴 흔적이다. **두 종류를 엄격히 구분**해서 준다:
- [실제 한 일] git 커밋/PR — 실제로 완료·반영된 사실.
- [AI에 요청/질문] 그날 Claude/Codex에게 물어보거나 시킨 주제 — **실제로 구현·완료했는지는 알 수 없음.**

이걸로 그날 무엇을 했는지 요약하라.

### 출력 형식 (아래 예시를 형식 그대로 따르라)
- 한 줄에 하나씩 "- " dot. 여러 항목을 한 줄에 몰아넣지 말 것.
- **git 커밋/PR 먼저**(실제 한 일) — 원문 용어 그대로.
- 그다음 **AI 세션 각각 한 줄**, 맨 끝에 ` (AI)`. 그 앞엔 **주제(명사)만** — `검토/분석/질문/요청/수집/아이디어` 같은 행위어는 **빼라**. (세션은 물어본 것일 수도, 작업 중일 수도 있어 단정 불가)
- 같은 티켓(예: B2C-52153)은 git·AI 통틀어 **한 줄로 합친다.**
- repo는 전부 ` · repo` 형식으로 통일(괄호 X). 서두·제목 없이 바로 dot부터. 최대 8줄.

예시:
- @zams/logger 0.4.754 + z3301 채팅 전환 reservation_id 반영 · zigbang-client
- [REALTY/PROD] Release 2026-07-06 · single-page
- B2C-52153 · zigbang-client (AI)
- RN 웹화 브랜치 방향 · zigbang-client (AI)
- lookout 추가 기능 · lookout (AI)

### 절대 규칙 — 용어를 바꾸지 마라 (가장 중요)
- 커밋/PR의 **원래 문구·기술 용어를 그대로 유지**한다. 네 말로 바꾸거나 해석을 덧붙이지 마라.
- 파라미터/필드명(예: `reservation_id`)을 "~기능"으로 바꾸지 마라. 이벤트/용어(예: `채팅 전환`)를 비슷한 다른 말(예: "채팅 예약")로 바꾸지 마라.
- 뜻이 확실치 않으면 **커밋 문구를 짧게 그대로 인용**만 하고, 의미를 지어내지 마라.

### 그 외 규칙
- "~했다/반영했다/진행했다"는 **[실제 한 일](커밋/PR)에 있는 것만**. [AI에 요청/질문]은 "~를 검토/분석/질문했다"로만.
  (예: 'RN 웹화 브랜치 방향 분석 요청' → "RN 웹화 방향을 분석했다"는 OK, "웹화 작업을 진행했다"는 금지)
- 없는 목적·인과("~의 일환으로", "~하기 위해") 금지. 이모지·군더더기 금지.
- 커밋/PR이 없으면 성과를 억지로 만들지 말고 "AI로 ~를 살펴봄" 정도로.

## 흔적
{ITEMS}
"""


def _llm_summary(day: str, rows) -> str:
    done, asked = [], []
    for r in rows:
        loc = f" ({r['repo']})" if r["repo"] else ""
        (done if r["source"] == "git" else asked).append(f"- {r['title']}{loc}")
    items = "[실제 한 일] (git 커밋/PR — 사실)\n" + ("\n".join(done) if done else "- (없음)") \
        + "\n\n[AI에 요청/질문] (완료 여부 불명 — 참고만)\n" + ("\n".join(asked) if asked else "- (없음)")
    prompt = SUMMARY_PROMPT.replace("{DAY}", day).replace("{ITEMS}", items)
    return claude_runner.run(
        prompt, model=CFG.get("worklog_model", "sonnet"), effort="", timeout=120
    ).strip()


def _summarize_days(c, days):
    """변경된(sig가 다른) 날만 haiku로 요약 → worklog_summary 갱신. LLM off면 skip."""
    if not CFG.get("worklog_llm", True):
        return
    for day in days:
        rows = c.execute(
            "SELECT source,repo,title FROM work_log WHERE day=? ORDER BY ts", (day,)
        ).fetchall()
        if not rows:
            continue
        raw = SUMMARY_PROMPT_VERSION + "|" + "|".join(f"{r['source']}:{r['title']}" for r in rows)
        sig = hashlib.md5(raw.encode("utf-8")).hexdigest()
        cur = c.execute("SELECT sig FROM worklog_summary WHERE day=?", (day,)).fetchone()
        if cur and cur["sig"] == sig:  # 내용 그대로 → haiku 호출 안 함
            continue
        try:
            summary = _llm_summary(day, rows)
        except Exception as ex:  # noqa: BLE001 - 요약 실패해도 로그 자체는 유지
            db.log_event(c, "worklog_summary_error", detail={"day": day, "err": str(ex)[:150]})
            continue
        c.execute(
            """INSERT INTO worklog_summary(day,summary,sig,updated_at) VALUES (?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET summary=excluded.summary, sig=excluded.sig,
               updated_at=excluded.updated_at""",
            (day, summary, sig, db.now()),
        )


def sync(c, days: int) -> int:
    since = _since(c)
    n = 0
    touched = set()
    for coll in (collect_git, collect_claude, collect_codex):
        try:
            for e in coll(days):
                if e["day"] < since:  # 시작 하한 이전 작업은 기록하지 않음
                    continue
                _upsert(c, e)
                n += 1
                touched.add(e["day"])
        except Exception as ex:  # noqa: BLE001 - 한 소스 실패해도 나머지 진행
            db.log_event(c, "worklog_source_error", detail={"src": coll.__name__, "err": str(ex)[:150]})
    db.set_meta(c, "last_worklog", str(db.now()))
    _summarize_days(c, touched)  # 변경된 날만 haiku 요약(내부에서 sig 비교)
    return n


def reset(c, since: str = None):
    """작업 기록 초기화 — 저장분·요약 전부 삭제하고 시작 하한을 오늘(또는 지정일)로."""
    day = since or datetime.date.today().isoformat()
    c.execute("DELETE FROM work_log")
    c.execute("DELETE FROM worklog_summary")
    db.set_meta(c, "worklog_since", day)
    db.log_event(c, "worklog_reset", detail={"since": day})


def by_day(c, limit_days: int = 60) -> list:
    since = _since(c)
    rows = c.execute(
        "SELECT day,ts,source,kind,repo,title,url,branch FROM work_log WHERE day>=? ORDER BY ts DESC LIMIT 3000",
        (since,),
    ).fetchall()
    grouped, order = {}, []
    for r in rows:
        d = r["day"]
        if d not in grouped:
            if len(order) >= limit_days:
                continue
            grouped[d] = []
            order.append(d)
        grouped[d].append({k: r[k] for k in ("source", "kind", "repo", "title", "url", "branch", "ts")})
    summaries = {r["day"]: r["summary"]
                 for r in c.execute("SELECT day, summary FROM worklog_summary").fetchall()}
    return [{"day": d, "summary": summaries.get(d, ""), "items": grouped[d]} for d in order]
