"""Poller fallback (ADR-001/004) + onboarding backfill skip (ADR-003).

On a repo's first sight, existing open PR heads are seeded as 'seen' and NOT
reviewed (avoids onboarding noise). Thereafter, any unseen head (new PR or new
push) is routed into review.
"""
from . import db, feedback, ghclient, keys, router
from .config import CFG


ISSUE_TRIAGE = "triage"


def _display(repo: str, number: int) -> str:
    """카드에 보일 별칭(예: PH-1767).

    렌더가 repo를 보고 분기하지 않도록 여기서 확정해 payload에 넣는다 — 나중에 다른
    레포가 붙어도 대시보드는 payload["display"]만 읽으면 된다."""
    prefix = (CFG.get("issue_display_prefix") or {}).get(repo)
    return f"{prefix}-{number}" if prefix else f"{repo.split('/')[-1]}#{number}"


def _issue_payload(repo: str, issue: dict) -> dict:
    return {
        "display": _display(repo, issue["number"]),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "labels": [x["name"] for x in (issue.get("labels") or [])],
        "assignees": [x["login"] for x in (issue.get("assignees") or [])],
    }


def poll_issues(c):
    """할당된 이슈를 작업 카드로 세운다 (ADR-001: 조회+insert만, LLM 없음).

    PR 폴러와 달리 onboarding 스킵이 없다 — 이미 할당된 이슈는 '놓친 것'이 아니라
    지금 해야 할 일이므로 처음 보는 순간부터 카드로 세운다."""
    for repo in CFG.get("issue_repos", []):
        try:
            issues = ghclient.issue_list(
                repo,
                assignee=CFG.get("issue_assignee") or None,
                title_prefixes=CFG.get("issue_title_prefixes") or None,
            )
        except ghclient.GhError as e:
            db.log_event(c, "issue_poller_error", detail={"repo": repo, "error": str(e)})
            continue

        for issue in issues:
            key = keys.issue_key(repo, issue["number"])
            existing = db.get_card(c, key)
            payload = _issue_payload(repo, issue)
            if existing is None:
                db.upsert_card(c, key, "issue", repo, issue["number"],
                               status=ISSUE_TRIAGE, payload=payload)
                db.log_event(c, "issue_card_created", key,
                             {"display": payload["display"], "title": payload["title"]})
            else:
                # 제목·담당·라벨이 바뀌면 카드 표시도 따라가야 한다. 사람이 넣은
                # 추가 지시(instruction)는 merge라서 보존된다.
                db.merge_payload(c, existing["id"], payload)

        # 목록에서 빠진 대기 카드 정리 — 닫혔거나, 담당/필터에서 벗어난 것.
        # 착수한 카드(triage 밖)는 건드리지 않는다.
        listed = {i["number"] for i in issues}
        waiting = c.execute(
            "SELECT id, pr_number, key FROM cards WHERE repo=? AND kind='issue' AND status=?",
            (repo, ISSUE_TRIAGE),
        ).fetchall()
        for card in waiting:
            if card["pr_number"] not in listed:
                db.set_status(c, card["id"], "archived")
                db.log_event(c, "issue_card_delisted", card["key"],
                             {"issue": card["pr_number"], "reason": "not in current listing"})


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
            """SELECT id, pr_number, key FROM cards
               WHERE repo=? AND kind IN ('root','review','approve')
                 AND status IN ('triage','approve_blocked','done','failed')""",
            (repo,),
        ).fetchall()
        for s in stale:
            if s["pr_number"] not in open_nums:
                try:
                    feedback.snapshot_pr(c, repo, s["pr_number"], "pr_closed")
                except ghclient.GhError as e:
                    db.log_event(c, "feedback_close_error", s["key"], {"error": str(e)})
                    continue
                db.set_status(c, s["id"], "archived")
                db.log_event(c, "card_pr_closed", s["key"], {"pr": s["pr_number"]})

        for pr in prs:
            if pr.get("isDraft"):
                continue
            if not db.is_seen_head(c, repo, pr["number"], pr["headRefOid"]):
                router.ensure_pr_cards(c, repo, pr["number"], source="poller")
