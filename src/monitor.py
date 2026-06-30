"""follow-up monitor: track PR lifecycle and supersede stale review cards.

Head-change re-review is primarily driven by webhooks/poller (which create a new
review card for the new head). Monitor is the deterministic fallback + cleanup:
closed/merged PRs are archived, stale commented cards are superseded.
"""
from . import db, ghclient


def process_root(c, card):
    info = ghclient.pr_view(card["repo"], card["pr_number"])
    if info.get("state") != "OPEN":
        # PR 머지/닫힘 → 그 PR의 모든 카드 archive (done 포함 — 머지됐으니 목록서 제거)
        rows = c.execute(
            "SELECT id FROM cards WHERE repo=? AND pr_number=? AND status != 'archived'",
            (card["repo"], card["pr_number"]),
        ).fetchall()
        for r in rows:
            db.set_status(c, r["id"], "archived")
        db.log_event(c, "pr_closed_archived", card["key"], {"state": info.get("state")})
        return
    # keep root head fresh
    db.upsert_card(c, card["key"], "root", card["repo"], card["pr_number"],
                   status="monitoring", head_sha=info["headRefOid"])


def process_commented(c, card):
    info = ghclient.pr_view(card["repo"], card["pr_number"])
    if info.get("state") != "OPEN" or info["headRefOid"] != card["head_sha"]:
        db.set_status(c, card["id"], "archived")
        db.log_event(c, "review_superseded", card["key"],
                     {"old": card["head_sha"], "new": info.get("headRefOid")})


def process_approve_stale(c, card):
    """승인대기(approve_blocked) 카드가 옛 head면 정리 — 그 사이 새 head로 재리뷰
    중인 카드가 따로 있으므로 유령 게이트를 archive. (현재 head 게이트는 그대로 둠)"""
    info = ghclient.pr_view(card["repo"], card["pr_number"])
    if info.get("state") != "OPEN" or info["headRefOid"] != card["head_sha"]:
        db.set_status(c, card["id"], "archived")
        db.log_event(c, "approve_superseded", card["key"],
                     {"old": card["head_sha"], "new": info.get("headRefOid"),
                      "state": info.get("state")})


def process_triage(c, card):
    """Drop a waiting (un-started) card if its head is no longer current."""
    info = ghclient.pr_view(card["repo"], card["pr_number"])
    if info.get("state") != "OPEN" or info["headRefOid"] != card["head_sha"]:
        db.set_status(c, card["id"], "archived")
        db.log_event(c, "triage_superseded", card["key"],
                     {"old": card["head_sha"], "new": info.get("headRefOid")})
