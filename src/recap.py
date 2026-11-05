"""세션 요약 — 하루 동안 내 에이전트 세션(Claude Code · Codex)이 무슨 일을 했는지 codex 로 요약한다.

같은 일(티켓 · PR · 주제)을 다룬 세션끼리 시간순으로 묶고, 묶음마다 한 일과 멈춘 지점을,
세션마다 한 줄 요약을 남긴다. 완료 · 진행 같은 판정은 하지 않는다 — 세션 기록만 보고는 맞히지 못한다
(세션 밖에서 머지된 PR 이 '진행'으로 남는다). 대신 기록에 있는 사실(PR · 생성 · 머지 · 커밋)을 붙인다.
어느 묶음에도 안 든 짧은 세션은 잡무로 따로 보인다.
대시보드 🗂 세션 요약 뷰에서 읽는다 — 팀 스레드에 올릴 글은 사람이 이걸 보고 쓴다.

언제: 슬롯(기본 09:40 · 14:05 · 18:10) 몇 분 전에 오늘 요약을 그때까지로 다시 만들고,
다음 날 첫 슬롯에 지난 날(월요일이면 금 · 토 · 일)을 하루 전체로 확정한다.
세션 목록이 지난번과 같으면 엔진을 다시 부르지 않는다.

입력은 그날의 세션 기록뿐이다 — 전에 올린 글이나 전날 요약을 넘기지 않는다.

tick 이 아니라 launchd 잡(io.hermes.recap, 1분 주기)으로 돈다. tick 은 리뷰를 동기로
돌려 30분씩 붙잡혀 있을 수 있어서, 거기 얹으면 09:40 요약이 10시 넘어 나온다.

Run: python -m src.recap                      # 1분마다 — 할 때가 아니면 바로 끝난다
     python -m src.recap --now [YYYY-MM-DD]   # 그날(기본 오늘) 요약을 바로 다시 만든다
"""
import datetime
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile

from . import claude_runner, codex_runner, config, db, engines, notify, prompt_tpl, sessions

CFG = config.CFG
LOCK_PATH = config.path("logs/recap.lock")
DEFAULT_SLOTS = ["09:40", "14:05", "18:10"]
MAX_ATTEMPTS = 3
RETRY_AFTER = 600          # 실패한 슬롯을 다시 시도하기까지(초)
MAX_LATE = 3 * 3600        # 슬롯 시각에서 이만큼 지나면(맥이 자고 있었으면) 그 슬롯은 건너뛴다
FINALIZE_LOOKBACK_DAYS = 7  # 확정 못 한 날(그날 아침 맥이 꺼져 있었다)을 이만큼까지 거슬러 챙긴다
FINAL = "final"            # 슬롯 칸에 들어가는 태그 — 다음 날 아침의 확정
MANUAL = "manual"          # 대시보드 · CLI 에서 바로 만든 것
WEEKDAYS = "월화수목금토일"
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ── 설정 ────────────────────────────────────────────────────────────────────
def enabled() -> bool:
    return bool(CFG.get("recap_enabled", False))


def slots() -> list:
    """'HH:MM' 목록 → 정렬된 'HH:MM'. 형식이 틀린 값은 버린다."""
    out = []
    for s in CFG.get("recap_slots") or DEFAULT_SLOTS:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s).strip())
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            out.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    return sorted(set(out)) or DEFAULT_SLOTS


def lead_minutes() -> int:
    try:
        return max(0, int(CFG.get("recap_lead_minutes", 5)))
    except (TypeError, ValueError):
        return 5


def weekdays_only() -> bool:
    return bool(CFG.get("recap_weekdays_only", True))


MODELS_CACHE = os.path.expanduser("~/.codex/models_cache.json")


def latest_codex_model(path: str = None) -> str:
    """codex 모델 목록(~/.codex/models_cache.json)의 1순위. 이름을 코드에 박지 않아야
    codex 가 새 모델을 내도 따라간다. 목록이 없으면 '' → codex 기본 모델."""
    path = path or MODELS_CACHE
    try:
        with open(path, encoding="utf-8") as f:
            models = json.load(f).get("models") or []
    except (OSError, ValueError, AttributeError):
        return ""
    listed = [m for m in models if isinstance(m, dict) and m.get("slug")
              and m.get("visibility", "list") == "list"]
    if not listed:
        return ""
    return min(listed, key=lambda m: m.get("priority", 999))["slug"]


def engine_opts() -> tuple:
    """(model, effort). config 가 비어 있으면 codex 최신 모델 · high."""
    model = (CFG.get("recap_model") or "").strip() or latest_codex_model()
    effort = (CFG.get("recap_effort") or "").strip() or "high"
    return model, effort


def engine_label() -> str:
    model, effort = engine_opts()
    return " · ".join(x for x in (model or "codex 기본", effort) if x)


# ── 날짜 · 슬롯 ─────────────────────────────────────────────────────────────
def _at(day: datetime.date, slot: str) -> datetime.datetime:
    h, m = slot.split(":")
    return datetime.datetime(day.year, day.month, day.day, int(h), int(m))


def day_bounds(day: str) -> tuple:
    """그날 00:00 과 다음 날 00:00 (로컬)."""
    d = datetime.date.fromisoformat(day)
    start = datetime.datetime(d.year, d.month, d.day)
    return start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()


def day_label(day: str) -> str:
    d = datetime.date.fromisoformat(day)
    return f"{day[5:]} ({WEEKDAYS[d.weekday()]})"


def due_slot(now: datetime.datetime, slot_list=None, lead: int = None, weekdays=None):
    """지금 차례인 오늘의 슬롯. 슬롯 시각 lead 분 전부터 그 슬롯 차례이고,
    다음 슬롯 차례가 오면 넘어간다. 슬롯 뒤 MAX_LATE 가 지났으면 None."""
    slot_list = slot_list or slots()
    lead = lead_minutes() if lead is None else lead
    weekdays = weekdays_only() if weekdays is None else weekdays
    if weekdays and now.weekday() >= 5:
        return None
    due = None
    for s in slot_list:
        at = _at(now.date(), s)
        if at - datetime.timedelta(minutes=lead) <= now:
            due = (s, at)
    if not due or (now - due[1]).total_seconds() > MAX_LATE:
        return None
    return due[0]


def prev_workday(today: datetime.date, weekdays=None) -> datetime.date:
    """전 영업일 — 월요일이면 금요일."""
    weekdays = weekdays_only() if weekdays is None else weekdays
    d = today - datetime.timedelta(days=1)
    while weekdays and d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


# ── DB ──────────────────────────────────────────────────────────────────────
def get(c, day: str):
    return c.execute("SELECT * FROM session_recaps WHERE day=?", (day,)).fetchone()


def recent(c, days: int = 7) -> list:
    since = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    return c.execute("SELECT * FROM session_recaps WHERE day>=? ORDER BY day DESC", (since,)).fetchall()


def is_final(row) -> bool:
    """하루가 끝난 뒤의 세션까지 다 보고 만든 요약인가."""
    return bool(row) and float(row["window_end"] or 0) >= day_bounds(row["day"])[1]


def days_to_finalize(c, today: datetime.date) -> list:
    """아침 첫 슬롯에 확정할 지난 날들 — 전 영업일부터 어제까지(월요일이면 금 · 토 · 일),
    그리고 최근 며칠 중 아직 확정 못 한 날(그날 아침에 맥이 꺼져 있었다)."""
    days = set()
    d = prev_workday(today)
    while d < today:
        days.add(d.isoformat())
        d += datetime.timedelta(days=1)
    since = (today - datetime.timedelta(days=FINALIZE_LOOKBACK_DAYS)).isoformat()
    for r in c.execute("SELECT day FROM session_recaps WHERE day>=? AND day<?",
                       (since, today.isoformat())):
        days.add(r["day"])
    out = []
    for day in sorted(days):
        row = get(c, day)
        if not (row and is_final(row) and row["status"] in ("ready", "empty")):
            out.append(day)
    return out


def _start_row(c, day: str, tag: str):
    """'만드는 중' 으로 — 지난 요약 내용은 남겨 둔다(실패해도 보던 걸 잃지 않게)."""
    t = db.now()
    c.execute(
        """INSERT INTO session_recaps(day,slot,window_end,status,attempts,created_at,updated_at)
           VALUES (?,?,0,'running',1,?,?)
           ON CONFLICT(day) DO UPDATE SET status='running', error=NULL,
             attempts=CASE WHEN session_recaps.slot=excluded.slot
                           THEN session_recaps.attempts+1 ELSE 1 END,
             slot=excluded.slot, updated_at=excluded.updated_at""",
        (day, tag, t, t),
    )


def _finish_row(c, day, status, window_end, fp, groups="[]", sessions_json="[]", model=""):
    c.execute(
        "UPDATE session_recaps SET status=?, window_end=?, fingerprint=?, groups=?, sessions=?,"
        " model=?, error=NULL, updated_at=? WHERE day=?",
        (status, window_end, fp, groups, sessions_json, model, db.now(), day),
    )


def _fail_row(c, day, error):
    c.execute("UPDATE session_recaps SET status='failed', error=?, updated_at=? WHERE day=?",
              (error, db.now(), day))


def _stale(row) -> bool:
    """'running' 인데 엔진 상한을 넘도록 소식이 없다 — 생성하던 프로세스가 죽었다."""
    return db.now() - float(row["updated_at"] or 0) > int(CFG.get("recap_timeout", 900)) + 120


def _should_run(row, tag: str) -> bool:
    if row is None:
        return True
    if tag == FINAL and is_final(row) and row["status"] in ("ready", "empty"):
        return False
    if row["status"] == "running":
        return _stale(row) and (row["slot"] != tag or row["attempts"] < MAX_ATTEMPTS)
    if row["slot"] != tag:
        return True                         # 새 슬롯 차례 — 시도 횟수도 새로 센다
    if row["status"] == "failed":
        age = db.now() - float(row["updated_at"] or 0)
        return row["attempts"] < MAX_ATTEMPTS and age >= RETRY_AFTER
    return False


# ── 생성 ────────────────────────────────────────────────────────────────────
def _hm(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"


def fingerprint(found: list) -> str:
    """세션 목록과 각 세션이 어디까지 갔는지. 같으면 요약도 같다."""
    key = sorted((s["ref"], round(s["end"] or 0), s["activity"], len(s["prompts"])) for s in found)
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:16]


def clean_groups(raw, labels) -> list:
    """엔진의 묶음 → 있는 세션만, 세션 하나는 한 묶음에만. 첫 세션이 먼저 시작한 묶음부터."""
    order = {lab: i for i, lab in enumerate(labels)}
    out, used = [], set()
    for g in raw or []:
        if not isinstance(g, dict):
            continue
        title = " ".join(str(g.get("title") or "").split())[:80]
        refs = []
        for r in g.get("refs") or []:
            r = str(r).strip()
            if r in order and r not in used and r not in refs:
                refs.append(r)
        if not title or not refs:
            continue
        used.update(refs)
        refs.sort(key=order.get)
        out.append({"title": title, "refs": refs, "summary": " ".join(str(g.get("summary") or "").split())})
    out.sort(key=lambda g: order[g["refs"][0]])
    return out


def _session_rows(labeled, parsed) -> list:
    by_ref = {}
    for item in parsed or []:
        if isinstance(item, dict) and item.get("ref"):
            by_ref[str(item["ref"]).strip()] = item
    out = []
    for label, s in labeled:
        got = by_ref.get(label, {})
        out.append({
            "ref": label, "source": s["source"], "repo": s["repo"], "branch": s["branch"],
            "title": s["title"], "start": s["start"], "end": s["end"], "prs": s["prs"][:5],
            "pr_actions": s["pr_actions"][:4], "commits": s["commits"][:5],
            "prompts": len(s["prompts"]),
            "summary": " ".join(str(got.get("summary") or "").split()),
        })
    return out


def build_prompt(day: str, since: float, until: float, body: str) -> str:
    end = day_bounds(day)[1]
    window = f"{day} {_hm(since)} ~ " + ("24:00 (하루 전체)" if until >= end else f"{_hm(until)} (아직 하루가 안 끝났다)")
    return prompt_tpl.render("recap.md", DAY=f"{day} ({WEEKDAYS[datetime.date.fromisoformat(day).weekday()]})",
                             WINDOW=window, SESSIONS=body)


def generate(day: str, tag: str, now: datetime.datetime = None, force: bool = False) -> str:
    """그날 00:00 부터 지금(하루가 지났으면 24:00)까지의 요약을 (다시) 만든다.
    반환: ready | unchanged(세션이 그대로라 지난 요약을 씀) | empty | failed."""
    now = now or datetime.datetime.now()
    since, end = day_bounds(day)
    until = min(now.timestamp(), end)
    with db.connect() as c:
        prev = get(c, day)
        _start_row(c, day, tag)
    try:
        found = sessions.collect(since, until)
        fp = fingerprint(found)
        if not found:
            with db.connect() as c:
                _finish_row(c, day, "empty", until, fp)
            return "empty"
        if not force and prev and prev["fingerprint"] == fp and prev["groups"] is not None:
            with db.connect() as c:
                _finish_row(c, day, "ready", until, fp, prev["groups"], prev["sessions"], prev["model"] or "")
            return "unchanged"
        labeled, body = sessions.digest_all(found, int(CFG.get("recap_input_chars", 60000)))
        model, effort = engine_opts()
        with tempfile.TemporaryDirectory(prefix="lookout-recap-") as scratch:
            # 빈 디렉토리에서 돌린다 — 프롬프트에 다 들어 있으니 엔진이 레포를 뒤질 이유가 없다.
            raw = codex_runner.run(build_prompt(day, since, until, body), cwd=scratch,
                                   model=model or None, effort=effort,
                                   timeout=int(CFG.get("recap_timeout", 900)))
        obj = claude_runner.parse_obj(raw)
        groups = clean_groups(obj.get("groups"), [lab for lab, _ in labeled])
        rows = _session_rows(labeled, obj.get("sessions"))
        if not groups and not any(r["summary"] for r in rows):
            raise ValueError(f"요약이 비었다: {str(raw)[:160]!r}")
        with db.connect() as c:
            _finish_row(c, day, "ready", until, fp, json.dumps(groups, ensure_ascii=False),
                        json.dumps(rows, ensure_ascii=False), engine_label())
        return "ready"
    except Exception as ex:  # noqa: BLE001 - 실패는 행에 남기고 다음 분에 재시도
        msg = str(ex)[-500:]
        with db.connect() as c:
            _fail_row(c, day, msg)
            db.log_event(c, "recap_failed", detail={"day": day, "slot": tag, "error": msg[:300]})
        return "failed"


# ── 보기 ────────────────────────────────────────────────────────────────────
def _json_list(text) -> list:
    try:
        v = json.loads(text or "[]")
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def view(row) -> dict:
    """행 → 화면용: 묶음마다 세션 · PR · 생성/머지 · 커밋 수를 붙이고(시간순), 묶음 밖 세션은 잡무로."""
    sess = [s for s in _json_list(row["sessions"]) if isinstance(s, dict) and s.get("ref")]
    by_ref = {s["ref"]: s for s in sess}
    groups, used = [], set()
    for g in _json_list(row["groups"]):
        members = [by_ref[r] for r in g.get("refs") or [] if r in by_ref and r not in used]
        if not members:
            continue
        used.update(s["ref"] for s in members)
        prs = []
        for s in members:
            prs += [u for u in s.get("prs") or [] if u not in prs]
        acts = [a for s in members for a in s.get("pr_actions") or []]
        marks = [m for m, p in (("PR 생성", "PR 생성"), ("머지", "PR 머지")) if any(a.startswith(p) for a in acts)]
        groups.append({"title": g.get("title") or "", "summary": g.get("summary") or "",
                       "sessions": members, "prs": prs, "marks": marks,
                       "commits": sum(len(s.get("commits") or []) for s in members)})
    # 예전 행(상태 순으로 저장)도 시간순으로
    groups.sort(key=lambda g: min(s.get("start") or 0 for s in g["sessions"]))
    return {
        "day": row["day"], "label": day_label(row["day"]), "status": row["status"],
        "slot": row["slot"] or "", "final": is_final(row), "window_end": row["window_end"],
        "model": row["model"] or "", "error": row["error"] or "", "attempts": row["attempts"],
        "updated_at": row["updated_at"], "count": len(sess), "groups": groups,
        "chores": [s for s in sess if s["ref"] not in used],
    }


def _session_line(s) -> str:
    return f"  - {_hm(s.get('start'))} {s.get('summary') or s.get('title') or '(요약 없음)'}"


def recap_text(v: dict) -> str:
    """복사 · CLI 용 일반 텍스트."""
    when = "하루 전체" if v["final"] else f"{_hm(v['window_end'])}까지"
    lines = [f"{v['label']} 세션 요약 — 세션 {v['count']}개 · {when}"]
    for g in v["groups"]:
        prs = ", ".join(sessions._pr_label(u) for u in g["prs"])
        lines += ["", g["title"] + (f" — {prs}" if prs else "")]
        if g["summary"]:
            lines.append(f"  {g['summary']}")
        lines += [_session_line(s) for s in g["sessions"]]
    if v["chores"]:
        lines += ["", "잡무"] + [_session_line(s) for s in v["chores"]]
    return "\n".join(lines)


# ── 스케줄 ──────────────────────────────────────────────────────────────────
def _notify(results: list):
    """새로 만든 요약이 있으면 한 번, 시도를 다 쓴 실패가 있으면 한 번."""
    port = int(CFG.get("dashboard_port", 8788))
    base = f"http://127.0.0.1:{port}/#recap"
    made, failed, first = [], [], {}
    with db.connect() as c:
        for day, status in results:
            row = get(c, day)
            if not row:
                continue
            if status == "ready":
                v = view(row)
                when = "확정" if v["final"] else f"{_hm(v['window_end'])}까지"
                made.append(f"{v['label']} {when} · 묶음 {len(v['groups'])}개")
                first.setdefault("made", day)
            elif status == "failed" and row["attempts"] >= MAX_ATTEMPTS:
                err = row["error"] or ""
                failed.append(f"{day_label(day)} " + ("codex 쿼터 소진" if engines.is_quota_error(err) else err[-100:]))
                first.setdefault("failed", day)
    # 알림을 누르면 첫 날(아침이면 막 확정한 지난 날)이 바로 열린다
    if made:
        notify.send("Lookout — 세션 요약", " / ".join(made), subtitle="대시보드 🗂 세션 요약",
                    group="lookout-recap", open_url=f"{base}/{first['made']}")
    if failed:
        notify.send("Lookout — 세션 요약 실패", " / ".join(failed), group="lookout-recap",
                    open_url=f"{base}/{first['failed']}")


def _lock():
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None
    return fd


def _unlock(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()


def run_scheduled(now: datetime.datetime = None) -> str:
    """launchd 가 1분마다 부른다. 할 일이 없으면 DB 도 안 열고 끝난다."""
    if not enabled():
        return "disabled"
    now = now or datetime.datetime.now()
    slot = due_slot(now)
    if not slot:
        return "idle"
    lock = _lock()
    if lock is None:
        return "busy"
    try:
        db.init()
        jobs = []
        with db.connect() as c:
            if slot == slots()[0]:
                jobs += [(d, FINAL) for d in days_to_finalize(c, now.date())]
            jobs.append((now.date().isoformat(), slot))
            todo = []
            for day, tag in jobs:
                row = get(c, day)
                if (row and row["status"] == "running" and row["slot"] == tag and _stale(row)
                        and row["attempts"] >= MAX_ATTEMPTS):
                    # 시도 한도를 넘긴 채 죽은 생성 — '만드는 중' 으로 굳지 않게 실패로 닫는다
                    _fail_row(c, day, "생성 중 프로세스가 끝났습니다(맥 재시동 · 강제 종료 등)")
                elif _should_run(row, tag):
                    todo.append((day, tag))
        results = [(day, generate(day, tag, now)) for day, tag in todo]
        _notify(results)
        return ",".join(f"{d}:{st}" for d, st in results) or "done"
    finally:
        _unlock(lock)


def run_now(day: str = None) -> str:
    """그날(기본 오늘) 요약을 세션이 그대로여도 다시 만든다."""
    lock = _lock()
    if lock is None:
        return "busy"
    try:
        db.init()
        now = datetime.datetime.now()
        day = day or now.date().isoformat()
        if not DAY_RE.fullmatch(day) or day > now.date().isoformat():
            return "bad day"
        return generate(day, MANUAL, now, force=True)
    finally:
        _unlock(lock)


def main(argv):
    if argv[:1] == ["--now"]:
        print(f"[recap] {run_now(argv[1] if len(argv) > 1 else None)}")
    else:
        status = run_scheduled()
        if status not in ("disabled", "idle", "done", "busy"):
            print(f"[recap] {datetime.datetime.now():%m-%d %H:%M} {status}")


if __name__ == "__main__":
    main(sys.argv[1:])
