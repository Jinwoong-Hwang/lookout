"""작업 기록(work log) — 날짜별로 쌓이는 작업 저널.

세 소스를 한 타임라인에 병합:
  💻 git    : 내 커밋 + 머지/생성한 PR (gh)
  🤖 claude : ~/.claude/projects/*/*.jsonl 세션의 대표 작업 프롬프트
  🤖 codex  : ~/.codex/history.jsonl 세션의 대표 작업 프롬프트

전부 **로컬** 데이터(이 맥의 ~/.claude·~/.codex). work_log 테이블에 ref UNIQUE로
dedup 저장 → 누적. git·에이전트 모두 과거 로그가 있어 소급 백필 가능.
LLM 미관여(사실만). Lookout 자체 headless 리뷰 세션은 제외.
"""
import datetime
import glob
import hashlib
import json
import os
import re

from . import claude_runner, db, ghclient
from .config import CFG, HERMES_HOME

HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_HISTORY = os.path.join(HOME, ".codex", "history.jsonl")
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
LINK_RE = re.compile(r"https?://|github\.com|sentry\.io|linear\.app|jira", re.I)
PR_RE = re.compile(r"(?:\bPR\b|#)\s*\d{2,}", re.I)
BRANCH_RE = re.compile(r"\b(?:feature|fix|hotfix|release|chore|users)/[A-Za-z0-9._/-]+", re.I)
WORK_WORD_RE = re.compile(
    r"작업|구현|수정|분석|검토|리뷰|배포|머지|PR|충돌|빌드|오류|이슈|센트리|"
    r"요약|기록|설계|정리|확인|테스트|릴리즈|업그레이드|마이그레이션|대응"
)
TECH_WORD_RE = re.compile(
    r"\b(?:node|mise|Xcode|iOS|Sentry|SDK|API|LLM|Claude|Codex|Gemini|DB|SQLite|"
    r"worklog|reservation_id|branch|diff|commit|merge)\b",
    re.I,
)


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


def _known_repo_names() -> set:
    names = {"lookout"}
    for repo in CFG.get("allowlist", []):
        if repo:
            names.add(repo.split("/")[-1].lower())
    return names


def _signal_score(text: str, repo: str = "", branch: str = "") -> tuple[int, bool]:
    """문구 블랙리스트가 아니라 작업 대상/맥락 신호로 AI 세션의 기록 가치를 판단한다."""
    t = (text or "").strip()
    if _is_command(t):
        return 0, False

    low = t.lower()
    score = 0
    anchor = False

    if TICKET_RE.search(t):
        score += 5
        anchor = True
    if LINK_RE.search(t):
        score += 4
        anchor = True
    if PR_RE.search(t):
        score += 3
        anchor = True
    if BRANCH_RE.search(t) or branch:
        score += 3
        anchor = True
    if repo and repo != "?" and not repo.startswith("."):
        score += 2
        anchor = True
    if any(name and name in low for name in _known_repo_names()):
        score += 2
        anchor = True
    if WORK_WORD_RE.search(t):
        score += 2
    if TECH_WORD_RE.search(t):
        score += 1
    if len(t) >= 25:
        score += 1
    if len(t) >= 70:
        score += 1
    return score, anchor


def _has_text_work_signal(text: str) -> bool:
    t = (text or "").strip()
    return (
        bool(TICKET_RE.search(t))
        or bool(LINK_RE.search(t))
        or bool(PR_RE.search(t))
        or bool(BRANCH_RE.search(t))
        or bool(WORK_WORD_RE.search(t))
        or bool(TECH_WORD_RE.search(t))
        or len(t) >= 25
    )


def _is_work_prompt(text: str, repo: str = "", branch: str = "") -> bool:
    score, anchor = _signal_score(text, repo, branch)
    threshold = int(CFG.get("worklog_ai_signal_threshold", 4))
    return score >= threshold and _has_text_work_signal(text) and (anchor or len((text or "").strip()) >= 50)


def _best_prompt(candidates):
    """[(ts, text, repo, branch)] 중 작업 신호가 가장 강한 대표 프롬프트를 고른다."""
    best = None
    for ts, text, repo, branch in candidates:
        score, anchor = _signal_score(text, repo, branch)
        long_enough = len((text or "").strip()) >= 50
        if (
            score < int(CFG.get("worklog_ai_signal_threshold", 4))
            or not _has_text_work_signal(text)
            or not (anchor or long_enough)
        ):
            continue
        rank = (score, 1 if anchor else 0, len(text or ""), -ts)
        if best is None or rank > best[0]:
            best = (rank, ts, text, repo, branch)
    if not best:
        return None
    _, ts, text, repo, branch = best
    return ts, text, repo, branch


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
    """세션 jsonl에서 작업 신호가 가장 강한 사용자 프롬프트 1건 추출. 없으면 None."""
    sid = None
    candidates = []
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
                ts = _iso_ts(d.get("timestamp"))
                if ts is None:
                    continue
                repo = os.path.basename(cwd) or "?"
                branch = d.get("gitBranch") or ""
                candidates.append((ts, txt, repo, branch))
    except OSError:
        return None
    best = _best_prompt(candidates)
    if not best:
        return None
    ts, txt, repo, branch = best
    return {"day": _day(ts), "ts": ts, "source": "claude", "kind": "session",
            "repo": repo, "ref": f"claude:{sid or os.path.basename(path)}",
            "title": _clean(txt), "url": "", "branch": branch}


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
    gap = int(CFG.get("worklog_ai_session_gap_minutes", 120)) * 60
    sessions, out = {}, []
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
                if not sid:
                    continue
                sessions.setdefault(sid, []).append((ts, d.get("text", "")))
    except OSError:
        return []

    for sid, messages in sessions.items():
        messages.sort(key=lambda x: x[0])
        segments = []
        current = []
        last_ts = None
        for ts, text in messages:
            if last_ts is not None and ts - last_ts > gap and current:
                segments.append(current)
                current = []
            current.append((ts, text, "", ""))
            last_ts = ts
        if current:
            segments.append(current)

        for i, seg in enumerate(segments):
            if seg[-1][0] < cutoff:
                continue
            best = _best_prompt(seg)
            if not best:
                continue
            ts, txt, _, _ = best
            ref = f"codex:{sid}" if i == 0 else f"codex:{sid}:{int(seg[0][0])}"
            out.append({"day": _day(ts), "ts": ts, "source": "codex", "kind": "session",
                        "repo": "", "ref": ref, "title": _clean(txt),
                        "url": "", "branch": ""})
    return out


# ── 동기화 / 조회 ───────────────────────────────────────────
def _upsert(c, e):
    cur = c.execute(
        """INSERT INTO work_log(day,ts,source,kind,repo,ref,title,url,branch,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ref) DO UPDATE SET day=excluded.day, ts=excluded.ts,
             repo=excluded.repo, title=excluded.title, url=excluded.url, branch=excluded.branch""",
        (e["day"], e["ts"], e["source"], e["kind"], e["repo"], e["ref"],
         e["title"], e["url"], e["branch"], db.now()),
    )
    return cur.rowcount > 0


def _since(c) -> str:
    """기록 시작 하한(날짜). 최초 실행 시 '오늘'로 고정 → 과거 소급 없이 오늘부터 누적."""
    v = db.get_meta(c, "worklog_since")
    if not v:
        v = datetime.date.today().isoformat()
        db.set_meta(c, "worklog_since", v)
    return v


# 프롬프트를 바꾸면 이 버전을 올린다 → sig가 달라져 기존 요약이 자동 재생성됨.
SUMMARY_PROMPT_VERSION = "10"

SUMMARY_PROMPT = """아래는 개발자가 {DAY}에 남긴 작업 흔적이다.
흔적은 LLM 비용을 줄이려고 이미 압축되어 있다. 원문에 없는 목적·성과를 추측하지 말고,
사람이 하루 작업을 빠르게 이해할 수 있는 작업 단위 요약으로 정리하라.

## 구분
- [완료 근거] git commit / PR opened / PR merged: 실제로 반영·생성·머지된 사실.
- [AI 작업 흔적] Claude/Codex 세션: 요청·논의·시도한 주제. 실제 완료 여부는 단정할 수 없다.

## 출력 형식
- 제목 없이 바로 bullet만 출력한다.
- 최대 6줄. 각 줄은 `- `로 시작한다.
- 같은 티켓/브랜치/repo/주제는 한 줄로 합친다.
- 한 줄 구조는 가능하면 `무엇을 다뤘는지 — 결과/상태 · repo`로 쓴다.
- 완료 근거가 있으면 “반영”, “PR 생성”, “머지”처럼 사실 상태를 적어도 된다.
- AI 흔적만 있으면 맨 끝에 ` (AI)`를 붙이고, “진행/완료/반영”이라고 쓰지 않는다.
- repo는 ` · repo` 형식으로 붙인다.

## 정확성 규칙
- commit/PR 제목, 티켓 번호, 필드명, 함수명, 패키지명은 원문 표현을 최대한 유지한다.
- `reservation_id` 같은 식별자를 일반 표현으로 바꾸지 않는다.
- 원문에 없는 인과관계나 목적(“~하기 위해”, “~의 일환으로”)을 만들지 않는다.
- 압축된 흔적에 없는 사용자 발화나 짧은 후속 문구를 새로 인용하지 않는다.
- 애매하면 해석하지 말고 원문 문구를 짧게 유지한다.

## 압축된 흔적
{ITEMS}
"""


def _today() -> str:
    return datetime.date.today().isoformat()


def _summary_daily_hour() -> int:
    try:
        return max(0, min(23, int(CFG.get("worklog_summary_daily_hour", 2))))
    except (TypeError, ValueError):
        return 2


def _summary_min_interval() -> float:
    try:
        hours = float(CFG.get("worklog_summary_min_interval_hours", 20))
    except (TypeError, ValueError):
        hours = 20
    return max(0.0, hours) * 3600


def _summarize_today_automatically() -> bool:
    return bool(CFG.get("worklog_summary_today", False))


def _is_work_row(r) -> bool:
    return r["source"] == "git" or _is_work_prompt(r["title"], r["repo"], r["branch"])


def _kind_label(kind: str) -> str:
    return {
        "commit": "commit",
        "pr_opened": "PR opened",
        "pr_merged": "PR merged",
        "session": "session",
    }.get(kind or "", kind or "item")


def _topic_key(r) -> str:
    text = " ".join(str(r[k] or "") for k in ("title", "branch", "repo"))
    m = TICKET_RE.search(text)
    if m:
        return m.group(0)
    if r["branch"]:
        return f"branch:{r['branch']}"
    return f"{r['repo'] or '?'}:{_clean(r['title'], 48).lower()}"


def _compact_summary_input(rows) -> str:
    """LLM에 전문 대화/로그를 넣지 않고, 작업 단위로 묶은 작은 입력만 만든다."""
    groups = {}
    for r in rows:
        key = _topic_key(r)
        g = groups.setdefault(key, {
            "repos": [],
            "branches": [],
            "done": [],
            "ai": [],
        })
        if r["repo"] and r["repo"] not in g["repos"]:
            g["repos"].append(r["repo"])
        if r["branch"] and r["branch"] not in g["branches"]:
            g["branches"].append(r["branch"])
        item = f"- {_kind_label(r['kind'])}: {r['title']}"
        if r["url"]:
            item += f" <{r['url']}>"
        if r["source"] == "git":
            g["done"].append(item)
        else:
            g["ai"].append(f"- {r['source']}: {r['title']}")

    parts = []
    for key, g in groups.items():
        repos = ", ".join(g["repos"]) if g["repos"] else "-"
        branches = ", ".join(g["branches"]) if g["branches"] else "-"
        parts.append(f"### 작업 단위: {key}\nrepo: {repos}\nbranch: {branches}")
        if g["done"]:
            parts.append("[완료 근거]\n" + "\n".join(g["done"][:8]))
        if g["ai"]:
            parts.append("[AI 작업 흔적]\n" + "\n".join(g["ai"][:8]))

    max_chars = int(CFG.get("worklog_summary_input_chars", 12000))
    return "\n".join(parts)[:max(2000, max_chars)]


def _llm_summary(day: str, rows) -> str:
    prompt = SUMMARY_PROMPT.replace("{DAY}", day).replace("{ITEMS}", _compact_summary_input(rows))
    return claude_runner.run(
        prompt, model=CFG.get("worklog_model", "sonnet"), effort="", timeout=120
    ).strip()


def _should_skip_summary(c, day: str, sig: str, force: bool) -> bool:
    cur = c.execute("SELECT sig, updated_at FROM worklog_summary WHERE day=?", (day,)).fetchone()
    if cur and cur["sig"] == sig:
        return True
    if force:
        return False

    now_dt = datetime.datetime.now()
    if day == _today() and not _summarize_today_automatically():
        return True
    if now_dt.hour < _summary_daily_hour():
        return True
    if cur and db.now() - float(cur["updated_at"] or 0) < _summary_min_interval():
        return True
    return False


def _summarize_days(c, days, force: bool = False):
    """변경된(sig가 다른) 날만 요약한다. 자동 실행은 하루 단위로 절제한다."""
    if not CFG.get("worklog_llm", True):
        return
    for day in days:
        rows = [r for r in c.execute(
            "SELECT source,kind,repo,title,url,branch FROM work_log WHERE day=? ORDER BY ts", (day,)
        ).fetchall() if _is_work_row(r)]
        if not rows:
            if force:
                c.execute("DELETE FROM worklog_summary WHERE day=?", (day,))
            continue
        raw = SUMMARY_PROMPT_VERSION + "|" + "|".join(
            f"{r['source']}:{r['kind']}:{r['repo']}:{r['branch']}:{r['title']}:{r['url']}" for r in rows
        )
        sig = hashlib.md5(raw.encode("utf-8")).hexdigest()
        if _should_skip_summary(c, day, sig, force):
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


def sync(c, days: int, force_summary: bool = False) -> int:
    since = _since(c)
    n = 0
    seen_days = set()
    for coll in (collect_git, collect_claude, collect_codex):
        try:
            for e in coll(days):
                if e["day"] < since:  # 시작 하한 이전 작업은 기록하지 않음
                    continue
                seen_days.add(e["day"])
                if _upsert(c, e):
                    n += 1
        except Exception as ex:  # noqa: BLE001 - 한 소스 실패해도 나머지 진행
            db.log_event(c, "worklog_source_error", detail={"src": coll.__name__, "err": str(ex)[:150]})
    db.set_meta(c, "last_worklog", str(db.now()))
    _summarize_days(c, seen_days, force=force_summary)
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
    rows = [r for r in c.execute(
        "SELECT day,ts,source,kind,repo,title,url,branch FROM work_log WHERE day>=? ORDER BY ts DESC LIMIT 3000",
        (since,),
    ).fetchall() if _is_work_row(r)]
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


def resummarize(c, since: str = None):
    """기존 work_log를 새 필터/프롬프트 기준으로 다시 요약한다."""
    since = since or _since(c)
    days = [r["day"] for r in c.execute(
        "SELECT DISTINCT day FROM work_log WHERE day>=? ORDER BY day", (since,)
    ).fetchall()]
    c.execute("DELETE FROM worklog_summary WHERE day>=? AND day NOT IN (SELECT DISTINCT day FROM work_log)", (since,))
    _summarize_days(c, days, force=True)
