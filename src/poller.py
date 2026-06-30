"""Poller fallback (ADR-001/004) + onboarding backfill skip (ADR-003).

On a repo's first sight, existing open PR heads are seeded as 'seen' and NOT
reviewed (avoids onboarding noise). Thereafter, any unseen head (new PR or new
push) is routed into review.
"""
from . import db, ghclient, router
from .config import CFG


def poll(c):
    for repo in CFG["allowlist"]:
        try:
            prs = ghclient.pr_list_open(repo)
        except ghclient.GhError as e:
            db.log_event(c, "poller_error", detail={"repo": repo, "error": str(e)})
            continue

        if not db.get_meta(c, f"onboarded:{repo}"):
            for pr in prs:
                db.mark_seen_head(c, repo, pr["number"], pr["headRefOid"])
            db.set_meta(c, f"onboarded:{repo}", "1")
            db.log_event(c, "repo_onboarded", detail={"repo": repo, "seeded": len(prs)})
            continue

        open_nums = {pr["number"] for pr in prs}

        # 머지/닫힘된 PR의 대기 카드 정리 — open 목록에 없으면 목록에서 제외 (archive).
        # (reviewing/verifying 진행 중인 건 건드리지 않음)
        stale = c.execute(
            "SELECT id, pr_number, key FROM cards WHERE repo=? AND status IN ('triage','approve_blocked','done')",
            (repo,),
        ).fetchall()
        for s in stale:
            if s["pr_number"] not in open_nums:
                db.set_status(c, s["id"], "archived")
                db.log_event(c, "card_pr_closed", s["key"], {"pr": s["pr_number"]})

        for pr in prs:
            if pr.get("isDraft"):
                continue
            if not db.is_seen_head(c, repo, pr["number"], pr["headRefOid"]):
                router.ensure_pr_cards(c, repo, pr["number"], source="poller")
