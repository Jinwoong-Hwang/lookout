"""prverifier: independently re-check each pending finding (adversarial verify).

Only verifier-confirmed findings advance to commenting.
"""
import json

from . import db, engines, ghclient, profiles, prompt_tpl, worktree


def process(c, card):
    repo, pr, head = card["repo"], card["pr_number"], card["head_sha"]
    policy = profiles.policy_from_card(card)
    pending = db.findings_for_card(c, card["id"], status="pending_verify")
    if not pending:
        decision_pending = db.pending_decision_findings(c, repo, pr)
        terminal = "commented" if decision_pending else (
            policy["no_confirmed_terminal"] if policy.get("profile_type") == "doc" else "commenting"
        )
        db.set_status(c, card["id"], terminal)
        return

    diff = ghclient.pr_diff(repo, pr)
    conversation = ghclient.pr_conversation(repo, pr)
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
                DIFF=diff[:40000], CONVERSATION=conversation,
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
    terminal = "commenting" if confirmed else (
        "commented" if decision_pending else policy["no_confirmed_terminal"]
    )
    db.set_status(c, card["id"], terminal)
