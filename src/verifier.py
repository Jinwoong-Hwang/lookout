"""prverifier: independently re-check each pending finding (adversarial verify).

Only verifier-confirmed findings advance to commenting.
"""
import json

from . import db, engines, ghclient, ledger, prdiff, profiles, prompt_tpl, worktree

# finding 하나만 재검증하므로 리뷰보다 적은 예산으로 충분
VERIFY_DIFF_CHARS = 40000


def process(c, card):
    repo, pr, head = card["repo"], card["pr_number"], card["head_sha"]
    policy = profiles.policy_from_card(card)
    pending = db.findings_for_card(c, card["id"], status="pending_verify")
    if not pending:
        decision_pending = db.pending_decision_findings(c, repo, pr)
        if decision_pending or db.open_findings_count(c, repo, pr):
            terminal = "commented"
        else:
            terminal = (policy["no_confirmed_terminal"]
                        if policy.get("profile_type") == "doc" else "commenting")
        db.set_status(c, card["id"], terminal)
        return

    diff, manifest = prdiff.collect(c, card, VERIFY_DIFF_CHARS)
    is_doc = policy.get("profile_type") == "doc"
    try:
        author = ghclient.pr_author_identity(repo, pr)
        replies = ghclient.collect_author_replies(repo, pr, author)
    except ghclient.GhError:
        replies = []
    prior_findings, author_notes = ledger.build(c, repo, pr, replies)
    conversation = ghclient.pr_conversation(repo, pr) if is_doc else ""
    engine = card["engine"] or "claude"
    wt = None
    try:
        wt = worktree.make_worktree(repo, pr, head)
        for f in pending:
            detail = json.loads(f["body"]) if f["body"] else {}
            prompt = prompt_tpl.render(
                profiles.prompt_name(policy, "verify"), REPO=repo, PR=pr, HEAD=head,
                FILE=f["file"], LINE=f["line"], TITLE=f["title"],
                PROBLEM=detail.get("problem", ""), FIX=detail.get("fix", ""),
                DIFF=diff, FILES=manifest, CONVERSATION=conversation,
                PRIOR_FINDINGS=prior_findings, AUTHOR_NOTES=author_notes,
                CATEGORY=detail.get("category", ""),
                IMPACT=detail.get("impact", ""),
                REQUIRED_DECISION=detail.get("required_decision", ""),
            )
            try:
                verdict = engines.run_json(prompt, engine=engine, cwd=wt, add_dir=wt)
            except Exception:  # noqa: BLE001 - engine failure -> treat as unverified
                verdict = {"confirmed": False, "reason": "verify failed"}
            status = "confirmed" if verdict.get("confirmed") else "rejected"
            db.set_finding_status(c, f["id"], status)
            db.log_event(c, "finding_verified", card["key"],
                         {"fp": f["fp"], "confirmed": verdict.get("confirmed"),
                          "reason": verdict.get("reason")})
    finally:
        if wt:
            worktree.remove_worktree(repo, wt)

    confirmed = db.findings_for_card(c, card["id"], status="confirmed")
    decision_pending = db.pending_decision_findings(c, repo, pr)
    if confirmed:
        terminal = "commenting"
    elif decision_pending or db.open_findings_count(c, repo, pr):
        # 신규 지적이 전부 기각돼도, 재판정을 건너뛴 기존 지적이 남아 있으면
        # LGTM 이 아니다 — reviewer 쪽만 막아 두고 이 경로를 비워 뒀다(셀프 3회차)
        terminal = "commented"
    else:
        terminal = policy["no_confirmed_terminal"]
    db.set_status(c, card["id"], terminal)
