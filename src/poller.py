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


def _ticket_status(issue: dict) -> tuple[str, str]:
    """Project 의 Status 필드(진행상태)와 그 보드 이름.

    한 이슈가 여러 프로젝트에 올라가 있을 수 있어(실측: #1842 는 product backlog +
    QA) 값이 있는 첫 항목을 쓴다. 어느 보드에서 온 값인지도 같이 싣는다 — 화면에서
    출처를 밝히지 않으면 두 보드가 다른 값을 줄 때 어느 쪽인지 알 수 없다."""
    for item in (issue.get("projectItems") or []):
        name = ((item.get("status") or {}).get("name") or "").strip()
        if name:
            return name, (item.get("title") or "").strip()
    return "", ""


def _issue_payload(repo: str, issue: dict) -> dict:
    parent = issue.get("parent") or None
    sub = issue.get("subIssuesSummary") or {}
    ticket_status, ticket_board = _ticket_status(issue)
    return {
        "display": _display(repo, issue["number"]),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "labels": [x["name"] for x in (issue.get("labels") or [])],
        "assignees": [x["login"] for x in (issue.get("assignees") or [])],
        # 에픽 소속 — 부모가 **내 보드에 없어도** 자식이 제목·링크를 들고 오므로
        # 에픽 뷰가 머리글을 세울 수 있다(실측: [FE] 태스크들의 부모는 미할당).
        "issue_type": (issue.get("issueType") or {}).get("name") or "",
        "parent": {
            "number": parent["number"],
            "display": _display(repo, parent["number"]),
            "title": parent.get("title") or "",
            "url": parent.get("url") or "",
        } if parent else None,
        # GitHub 기준 자식 진행도(내게 할당되지 않은 자식까지 포함). 보드에 보이는
        # 건수와 다를 수 있어 화면에서도 출처를 갈라 적는다.
        "sub": {"done": sub.get("completed") or 0, "total": sub.get("total") or 0},
        # 티켓 진행상태 — 읽기 전용이다. Lookout 은 Project 에 되돌려 쓰지 않는다
        # (팀 공용 보드라 봇이 건드릴 일이 아니다). 빈 값 = 프로젝트에 없거나 미상.
        "ticket_status": ticket_status,
        "ticket_board": ticket_board,
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
