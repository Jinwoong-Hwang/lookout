"""prapprover (ADR-005, human-gated).

An approve card is created BLOCKED. It does nothing until the operator unblocks
it (`hermes unblock <id>`), which flips it to 'approving'. Even then, approval
only fires after re-resolving head + checks. Approval is NEVER automatic.
"""
from . import db, ghclient, keys, router
from .config import CFG


def create_gate(c, review_card):
    """Create the blocked approve card after an LGTM review, archive the review card.
    머지/닫힘 또는 stale head면 게이트를 만들지 않고 그냥 archive."""
    import json
    repo, pr, head = review_card["repo"], review_card["pr_number"], review_card["head_sha"]
    info = ghclient.pr_view(repo, pr)
    if info.get("state") != "OPEN" or info.get("headRefOid") != head:
        db.set_status(c, review_card["id"], "archived")
        db.log_event(c, "lgtm_no_gate", review_card["key"],
                     {"state": info.get("state"), "stale": info.get("headRefOid") != head})
        return
    # 본인 PR은 self-approve 불가 → 게이트 없이 'done'(완료·머지대기)으로. 머지되면
    # monitor/poller가 archive. (이전엔 archived로 바로 사라졌음)
    if (info.get("author") or {}).get("login") == ghclient.my_login():
        db.set_status(c, review_card["id"], "done")
        db.log_event(c, "lgtm_own_pr", review_card["key"], {"note": "본인 PR — 리뷰 통과, 머지 대기"})
        return
    # payload comes off the DB row as a JSON string; parse so upsert re-encodes once
    meta = json.loads(review_card["payload"]) if review_card["payload"] else None
    akey = keys.approve_key(repo, pr, head)
    if db.get_card(c, akey) is None:
        db.upsert_card(c, akey, "approve", repo, pr, status="approve_blocked",
                       head_sha=head, blocked=1, payload=meta)
        db.log_event(c, "approve_gate_created", akey, {"head": head})
    db.set_status(c, review_card["id"], "archived")


def _checks_ok(info) -> bool:
    rollup = info.get("statusCheckRollup") or []
    if not rollup:
        return False
    ok = {"SUCCESS", "SKIPPED", "NEUTRAL"}
    for ch in rollup:
        status = (ch.get("status") or "").upper()
        conclusion = (ch.get("conclusion") or "").upper()
        state = (ch.get("state") or "").upper()
        if status and status != "COMPLETED":
            return False
        if conclusion:
            if conclusion not in ok:
                return False
            continue
        if state and state not in ok:
            return False
        if not state and not conclusion:
            return False
    return True


def process_gate(c, card):
    """Only runs once operator unblocked the card (status='approving')."""
    repo, pr, head = card["repo"], card["pr_number"], card["head_sha"]
    info = ghclient.pr_view(repo, pr)

    # ADR-007: stale head -> supersede instead of repeat-unblock
    if info.get("state") != "OPEN" or info["headRefOid"] != head:
        db.set_status(c, card["id"], "archived")
        db.log_event(c, "approve_superseded", card["key"],
                     {"old": head, "new": info.get("headRefOid"), "state": info.get("state")})
        if info.get("state") == "OPEN":
            router.ensure_pr_cards(c, repo, pr, source="approve-superseded")
        return

    # 내가 이미 approve 한 경우에만 skip. (남이 승인했어도 내 명의로는 approve)
    if ghclient.my_approved(repo, pr, head):
        db.set_status(c, card["id"], "done")
        db.log_event(c, "approve_already_by_me", card["key"], {"head": head})
        return

    # CI 체크 실패는 머지의 문제이지 '승인'을 막을 이유는 아님 — 사람이 이미 unblock으로
    # 결정했으므로 경고만 남기고 진행 (체크 게이트는 정보성).
    if not _checks_ok(info):
        db.log_event(c, "approve_checks_failing_proceed", card["key"],
                     {"note": "CI 실패하지만 사람이 승인 결정 → 진행"})

    body = "LGTM — Hermes 자동 리뷰 통과 후 검수자 승인."
    if CFG["dry_run_approve"]:
        db.log_event(c, "approve_dryrun", card["key"], {"would_approve": True})
    else:
        ghclient.pr_approve(repo, pr, body)
        db.log_event(c, "approve_done", card["key"])
    db.set_status(c, card["id"], "done")
