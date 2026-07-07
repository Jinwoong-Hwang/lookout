"""Serialized maintenance tick (ADR-004).

A single process-level flock guards the whole tick so concurrent runs never
race on the SQLite DB. Drains webhook inbox, runs the poller fallback on its
interval, then advances every Kanban lane one step. Approve cards that are still
blocked are intentionally skipped.

Run: python -m src.tick
"""
import datetime
import fcntl
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import (approver, commenter, config, db, monitor, poller,
               reviewer, router, verifier, worklog, worktree)

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
TERMINAL_STATUSES = {"done", "archived"}


MAX_STAGE_RETRIES = 3  # 같은 카드가 이만큼 연속 실패하면 포기(무한 재시도 방지)


def _process_one(fn, card, label):
    try:
        with db.connect() as c:
            fn(c, card)
    except Exception:  # noqa: BLE001
        with db.connect() as c:
            db.log_event(c, "stage_error", card["key"],
                         {"stage": label, "trace": traceback.format_exc()[-800:]})
            if label in RETRYABLE_STAGES:
                current = c.execute("SELECT status FROM cards WHERE id=?", (card["id"],)).fetchone()
                if current and current["status"] not in TERMINAL_STATUSES:
                    fails = c.execute(
                        "SELECT COUNT(*) n FROM events WHERE key=? AND type='stage_error' AND ts > ?",
                        (card["key"], db.now() - 1800),
                    ).fetchone()["n"]
                    if fails >= MAX_STAGE_RETRIES:
                        db.set_status(c, card["id"], "archived")  # 포기 — 무한루프 방지
                        db.log_event(c, "review_gave_up", card["key"],
                                     {"stage": label, "fails": fails})
                    else:
                        db.set_status(c, card["id"], card["status"], blocked=card["blocked"])


def _stage(statuses, fn, label):
    """Process cards in `statuses` concurrently (cap MAX_CONCURRENT), each in its
    own transaction; errors isolated. Same-repo git is serialized in worktree.py."""
    with db.connect() as c:
        cards = db.cards_in(c, statuses)
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
        cards = db.cards_in(c, statuses)[:MAX_CONCURRENT]
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
    _stage(["commented"], monitor.process_commented, "monitor_commented")
    _stage(["triage"], monitor.process_triage, "monitor_triage")
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


def maybe_worklog():
    """작업 기록 동기화 — 30분마다 최근 worklog_sync_days를 재스캔해 오늘부터 누적.
    worklog_since(최초=오늘) 이전 작업은 기록 안 함. read-only라 리뷰 흐름과 무관."""
    if not CFG.get("worklog_enabled", True):
        return
    with db.connect() as c:
        last = float(db.get_meta(c, "last_worklog", "0"))
    if time.time() - last < 30 * 60:
        return
    with db.connect() as c:
        n = worklog.sync(c, int(CFG.get("worklog_sync_days", 3)))
    print(f"[tick] worklog synced ({n} entries)")


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
        # 작업 기록 동기화 (백필 1회 + 30분마다 최근분)
        maybe_worklog()
        print("[tick] done")
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
