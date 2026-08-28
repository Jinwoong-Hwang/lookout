"""Serialized maintenance tick (ADR-004).

A single process-level flock guards the whole tick so concurrent runs never
race on the SQLite DB. Drains webhook inbox, runs the poller fallback on its
interval, then advances every Kanban lane one step. Approve cards that are still
blocked are intentionally skipped.

Run: python -m src.tick
"""
import fcntl
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import (approver, commenter, config, db, engines, feedback, monitor,
               notify, poller, reviewer, router, verifier, worktree)

CFG = config.CFG
LOCK_PATH = config.path("logs/tick.lock")


def _acquire_lock():
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None
    return fd


def _maybe_poll():
    interval = CFG["poller_interval_minutes"] * 60
    with db.connect() as c:
        last = float(db.get_meta(c, "last_poll", "0"))
    if time.time() - last < interval:
        return
    with db.connect() as c:
        poller.poll(c)
        db.set_meta(c, "last_poll", str(time.time()))


MAX_CONCURRENT = max(1, int(CFG.get("max_concurrent_reviews", 3)))
RETRYABLE_STAGES = {"reviewer", "verifier", "commenter", "approver", "create_gate"}
TERMINAL_STATUSES = {"done", "archived", "failed"}


MAX_STAGE_RETRIES = 3  # 같은 카드가 이만큼 연속 실패하면 포기(무한 재시도 방지)
QUOTA_NOTIFY_INTERVAL = 900  # 엔진별 쿼터 알림 최소 간격(초) — 카드마다 울리지 않게
RETRY_COOLDOWN = 180   # 실패 직후 재시도 금지 구간(초). 없으면 같은 tick 안에서
                       # 3연속 fail-fast로 재시도 예산이 20초 만에 소진된다.


def _last_error_line(limit: int = 300) -> str:
    """직전 예외의 마지막 줄(진짜 사유)만. failed 카드에 표시할 용도."""
    return traceback.format_exc().strip().splitlines()[-1][:limit]


def _cooling(c, card) -> bool:
    """직전 실패가 RETRY_COOLDOWN 이내면 이번 웨이브는 건너뛴다.
    단 사람이 직접 누른 시작/재시도는 즉시 돈다(쿨다운 무시)."""
    row = c.execute(
        "SELECT MAX(CASE WHEN type='stage_error' THEN ts END) e,"
        "       MAX(CASE WHEN type IN ('operator_retry','operator_start') THEN ts END) r"
        " FROM events WHERE key=?", (card["key"],)
    ).fetchone()
    if not row or not row["e"]:
        return False
    if row["r"] and row["r"] > row["e"]:
        return False
    return row["e"] > db.now() - RETRY_COOLDOWN


def _quota_notify_due(c, engine: str) -> bool:
    """엔진당 QUOTA_NOTIFY_INTERVAL에 한 번만 알린다(카드 5장이면 5번 울리지 않게)."""
    k = f"quota_notified:{engine}"
    if db.now() - float(db.get_meta(c, k, "0") or 0) < QUOTA_NOTIFY_INTERVAL:
        return False
    db.set_meta(c, k, str(db.now()))
    return True


def _requeue_quota(c, card, label, msg):
    """엔진 토큰/쿼터 소진 — 실패가 아니라 '지금은 못 돎'. 재시도 예산을 태우지 않고
    대기목록(triage)으로 되돌리고 사람에게 알린다."""
    engine = card["engine"] or "claude"
    hint = engines.quota_retry_hint(msg)
    db.set_status(c, card["id"], "triage")
    db.log_event(c, "review_quota_paused", card["key"],
                 {"stage": label, "engine": engine, "retry_at": hint, "error": msg[:300]})
    if _quota_notify_due(c, engine):
        notify.send(
            f"Lookout — {engine} 토큰 소진",
            f'{card["repo"]}#{card["pr_number"]} 리뷰를 대기목록으로 되돌렸습니다.'
            + (f" {hint} 이후 재시도하세요." if hint else " 쿼터 회복 후 재시도하세요."),
            subtitle="리뷰 중단 · 카드는 Triage 에 있음",
            group=f"lookout-quota-{engine}",
        )


def _process_one(fn, card, label):
    try:
        with db.connect() as c:
            fn(c, card)
    except Exception as exc:  # noqa: BLE001
        with db.connect() as c:
            # 쿼터 소진은 결함이 아니므로 stage_error로 세지 않는다 — 세면 3번 만에
            # 카드가 failed로 죽고, 쿼터가 풀려도 아무도 다시 돌리지 않는다.
            if label in RETRYABLE_STAGES and engines.is_quota_error(str(exc)):
                _requeue_quota(c, card, label, str(exc))
                return
            db.log_event(c, "stage_error", card["key"],
                         {"stage": label, "trace": traceback.format_exc()[-800:]})
            if label in RETRYABLE_STAGES:
                current = c.execute("SELECT status FROM cards WHERE id=?", (card["id"],)).fetchone()
                if current and current["status"] not in TERMINAL_STATUSES:
                    fails = c.execute(
                        "SELECT COUNT(*) n FROM events WHERE key=? AND type='stage_error'"
                        " AND json_extract(detail,'$.stage')=? AND ts > ?",
                        (card["key"], label, db.now() - 1800),
                    ).fetchone()["n"]
                    if fails >= MAX_STAGE_RETRIES:
                        # archived로 보내면 대시보드에서 그냥 사라져 이유를 알 수 없다.
                        # failed 레인에 남겨 사유를 보여주고 수동 재시도를 받는다.
                        db.set_status(c, card["id"], "failed")
                        db.log_event(c, "review_gave_up", card["key"],
                                     {"stage": label, "fails": fails,
                                      "error": _last_error_line()})
                    else:
                        db.set_status(c, card["id"], card["status"], blocked=card["blocked"])


def _stage(statuses, fn, label):
    """Process cards in `statuses` concurrently (cap MAX_CONCURRENT), each in its
    own transaction; errors isolated. Same-repo git is serialized in worktree.py."""
    with db.connect() as c:
        cards = db.cards_in(c, statuses)
        if label in RETRYABLE_STAGES:  # 실패 직후 같은 tick에서 재시도하지 않게
            cards = [x for x in cards if not _cooling(c, x)]
    if not cards:
        return
    if MAX_CONCURRENT <= 1 or len(cards) == 1:
        for card in cards:
            _process_one(fn, card, label)
        return
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
        list(ex.map(lambda card: _process_one(fn, card, label), cards))


def _drain(statuses, fn, label, max_waves=30):
    """Keep processing `statuses` until empty — so cards that arrive mid-tick
    (e.g. clicked while a review runs) get picked up by the same tick, instead of
    waiting for the next one. Up to MAX_CONCURRENT at a time per wave."""
    for _ in range(max_waves):
        with db.connect() as c:
            has = bool(db.cards_in(c, statuses))
        if not has:
            return
        _stage(statuses, fn, label)


def _monitor_roots():
    with db.connect() as c:
        roots = [r for r in db.cards_in(c, ["monitoring"]) if r["kind"] == "root"]
    for card in roots:
        try:
            with db.connect() as c:
                monitor.process_root(c, card)
        except Exception:  # noqa: BLE001
            with db.connect() as c:
                db.log_event(c, "stage_error", card["key"], {"stage": "monitor_root"})


def _wave(statuses, fn, label):
    """리뷰/검증을 한 번에 MAX_CONCURRENT개씩만 처리(드레인 X) — 사이사이
    다운스트림(게이트/댓글)을 끼워넣어 lgtm이 긴 드레인에 막히지 않게."""
    with db.connect() as c:
        cards = [x for x in db.cards_in(c, statuses) if not _cooling(c, x)][:MAX_CONCURRENT]
    if not cards:
        return 0
    if len(cards) == 1:
        _process_one(fn, cards[0], label)
    else:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
            list(ex.map(lambda card: _process_one(fn, card, label), cards))
    return len(cards)


def _fast_stages():
    """빠른(LLM 없는) 단계 — 게이트 생성/댓글 게시/승인. 자주 돌려도 가벼움."""
    _stage(["lgtm"], approver.create_gate, "create_gate")
    _stage(["commenting"], commenter.process, "commenter")
    _stage(["approving"], approver.process_gate, "approver")


def run_once():
    db.init()
    with db.connect() as c:
        router.drain(c)
    _maybe_poll()

    # 1) 빠른 정리/진행 먼저 — 느린 리뷰에 막히지 않게 (머지·stale 즉시 archive)
    _monitor_roots()                                                  # 머지/닫힘 PR archive
    _stage(["reviewing", "verifying", "commenting"], monitor.process_active_stale, "monitor_active_stale")
    _stage(["commented"], monitor.process_commented, "monitor_commented")
    _stage(["triage", "failed"], monitor.process_triage, "monitor_triage")
    _stage(["approve_blocked"], monitor.process_approve_stale, "monitor_approve_stale")
    _fast_stages()

    # 2) 리뷰/검증을 wave 단위로 — 매 wave 뒤에 게이트/댓글을 끼워넣어, 리뷰가 lgtm을 만들면
    #    같은 tick에서 바로 게이트로 넘어감 (긴 드레인이 lgtm을 막던 문제 해소)
    for _ in range(60):
        did = _wave(["intake"], reviewer.process, "reviewer")
        did += _wave(["verifying"], verifier.process, "verifier")
        _fast_stages()
        if did == 0:
            break


def gc():
    """Periodic workspace cleanup (worktree prune) — 가벼움, gc_interval마다."""
    worktree.gc_worktrees()


def deep_gc():
    """무거운 일일 청소 — run_once 종료 후(진행 중 리뷰 없음)에만 호출.
    ① 캐시 repo object store gc(누적 PR-fetch 객체 회수) ② 오래된 archived 카드/이벤트 purge."""
    worktree.gc_repos()
    with db.connect() as c:
        stats = db.purge_old(c, days=CFG.get("purge_days", 14))
    print(f"[tick] deep_gc: repos gc'd, db purged {stats}")


def main():
    lock = _acquire_lock()
    if lock is None:
        print("[tick] another tick is running; exiting")
        return
    try:
        run_once()
        # opportunistic GC on its own interval
        with db.connect() as c:
            last_gc = float(db.get_meta(c, "last_gc", "0"))
        if time.time() - last_gc > CFG["gc_interval_minutes"] * 60:
            gc()
            with db.connect() as c:
                db.set_meta(c, "last_gc", str(time.time()))
        # 무거운 청소는 하루 1회 (run_once가 끝나 진행 중 리뷰가 없는 시점)
        with db.connect() as c:
            last_deep = float(db.get_meta(c, "last_deep_gc", "0"))
        if time.time() - last_deep > 24 * 3600:
            deep_gc()
            with db.connect() as c:
                db.set_meta(c, "last_deep_gc", str(time.time()))
        with db.connect() as c:
            last_feedback = float(db.get_meta(c, "last_feedback_weekly", "0"))
        if time.time() - last_feedback > feedback.WEEKLY_INTERVAL_SECONDS:
            with db.connect() as c:
                n = feedback.weekly_open(c)
                db.set_meta(c, "last_feedback_weekly", str(time.time()))
            print(f"[tick] feedback_weekly: {n} snapshots")
        print("[tick] done")
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
