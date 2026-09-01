"""prreviewer: review the PR head in a detached worktree, emit findings.

Read-only on the target repo. Stale heads are skipped (router/monitor create a
fresh review card for the new head).
"""
import hashlib
import json as _json
import re

from . import db, doc_planner, engines, ghclient, keys, profiles, prompt_tpl, worktree
from .config import CFG

ACTIONABLE_SEVERITY_DOC = {"blocking", "should-fix"}

# Claude(Opus)는 보수적 지침을 곧이곧대로 지켜 lgtm 비율이 높음 → recall 보강.
# Codex엔 미적용(이미 충분히 surfacing). 리뷰 프롬프트에만 append(closure엔 X).
CLAUDE_RECALL_NOTE = """

## 추가 지침 (재현율 — 위 출력 형식은 그대로)
- `lgtm: true`는 변경이 작고 **명백히** 안전할 때만. 의심 신호가 하나라도 있으면 묻어두지 말 것.
- 확신이 100%가 아니어도 머지 전 확인할 가치가 있으면 **medium confidence로 보고**하고,
  evidence로 실제 코드 줄을 인용해 **사람이 판단하게** 하라. (놓치는 것보다 약한 신호라도 올리는 게 낫다)
- 단, evidence(실제 코드 줄)로 가리킬 수 없는 **순수 추측**은 여전히 금지.
- 출력은 위 §5 JSON 스키마만. 다른 텍스트 금지.
"""


def _is_stale(card) -> bool:
    info = ghclient.pr_view(card["repo"], card["pr_number"])
    if info.get("state") != "OPEN":
        return True
    return info["headRefOid"] != card["head_sha"]


def _actionable_conf(policy: dict) -> set[str]:
    return {"high"} if policy.get("min_confidence") == "high" else {"high", "medium"}


def _payload(card) -> dict:
    try:
        return _json.loads(card["payload"]) if card["payload"] else {}
    except _json.JSONDecodeError:
        return {}


def _save_payload(c, card_id: int, meta: dict):
    c.execute("UPDATE cards SET payload=? WHERE id=?",
              (_json.dumps(meta, ensure_ascii=False), card_id))


def _stable_rule(f: dict) -> str:
    rule = (f.get("rule") or "").strip()
    if rule:
        return rule
    raw = "|".join(str(f.get(k, "")) for k in ("category", "title", "file", "line"))
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:48] or "doc-finding"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _verified_reply(verdict: dict, replies: list[dict]):
    comment_id = str(verdict.get("reply_comment_id") or "")
    evidence = (verdict.get("reply_evidence") or "").strip()
    if not comment_id or not evidence:
        return None
    return next((reply for reply in replies
                 if str(reply.get("id")) == comment_id
                 and evidence in (reply.get("body") or "")), None)


def _verified_follow_up(verdict: dict, reply: dict | None) -> str:
    """Keep only a follow-up reference copied verbatim from the verified reply."""
    follow_up = (verdict.get("follow_up") or "").strip()
    token = r"(?:https?://\S+|[A-Z][A-Z0-9_]*-\d+|#\d+)"
    return (follow_up if reply and re.fullmatch(token, follow_up)
            and follow_up in (reply.get("body") or "") else "")


def _run_closure(c, card, priors, diff, engine, wt, policy,
                 author: dict, comments: list[dict], plan=None):
    """Re-judge previous findings using backend-verified PR-author replies."""
    prompt_file = profiles.prompt_name(policy, "closure")
    for pf in priors:
        decision_head = pf["decision_head"]
        if pf["status"] in {"dismissed", "deferred", "dismiss_pending", "defer_pending"}:
            if decision_head == card["head_sha"]:
                continue

        replies = ghclient.finding_author_replies(
            comments, pf["fp"], author.get("id", ""), ghclient.my_login(),
        )
        if decision_head and decision_head != card["head_sha"] and pf["decision_comment_id"]:
            decision_comment = next(
                (x for x in comments if str(x.get("id")) == str(pf["decision_comment_id"])),
                None,
            )
            if decision_comment:
                replies = [x for x in replies
                           if x.get("created_at", "") > decision_comment.get("created_at", "")]
        detail = _json.loads(pf["body"]) if pf["body"] else {}
        cprompt = prompt_tpl.render(
            prompt_file, FILE=pf["file"], LINE=pf["line"], TITLE=pf["title"],
            PROBLEM=detail.get("problem", ""), DIFF=diff[:40000], STATUS=pf["status"],
            AUTHOR=author.get("login", ""),
            REPLIES_JSON=_json.dumps(replies, ensure_ascii=False),
            PLAN_JSON=doc_planner.dumps(plan or {}),
        )
        try:
            verdict = engines.run_json(cprompt, engine=engine, cwd=wt, add_dir=wt)
        except Exception as e:  # noqa: BLE001 - closure failure must block LGTM
            db.clear_finding_decision(c, pf["id"], "unresolved")
            db.log_event(c, "finding_closure_error", card["key"],
                         {"fp": pf["fp"], "error": str(e)})
            continue
        status = verdict.get("status")
        if status not in {"resolved", "dismissed", "deferred", "unresolved"}:
            status = "resolved" if verdict.get("resolved") else "unresolved"
        evidence = (verdict.get("evidence") or "").strip()
        verified_reply = _verified_reply(verdict, replies)
        follow_up = _verified_follow_up(verdict, verified_reply) if status == "deferred" else ""
        if pf["status"] in {"dismissed", "deferred"} and status == pf["status"]:
            # A new head rechecks the decision, but keeps the accepted author's
            # original evidence/reference unless current code refutes it.
            db.set_finding_status(c, pf["id"], status)
        elif status in {"dismissed", "deferred"} and verified_reply:
            status = "dismiss_pending" if status == "dismissed" else "defer_pending"
            db.set_finding_decision(
                c, pf["id"], status, card["head_sha"], verified_reply["id"],
                (verdict.get("reply_evidence") or "").strip(),
                follow_up,
            )
        elif status in {"dismissed", "deferred"}:
            status = "unresolved"
            db.clear_finding_decision(c, pf["id"], status)
        elif pf["status"] in {"dismissed", "deferred"} and status == "unresolved" and not evidence:
            status = pf["status"]
            db.set_finding_status(c, pf["id"], status)
        else:
            db.clear_finding_decision(c, pf["id"], status)
        db.log_event(c, "finding_closure", card["key"],
                     {"fp": pf["fp"], "status": status,
                      "evidence": evidence,
                      "reply_comment_id": verified_reply["id"] if verified_reply else "",
                      "reply_evidence": (verdict.get("reply_evidence") or "").strip()})


def refresh_author_decisions(c, card):
    """Last responsible moment for replies posted while review/verify ran."""
    priors = db.posted_findings_for_closure(c, card["repo"], card["pr_number"])
    if not priors:
        return
    try:
        author = ghclient.pr_author_identity(card["repo"], card["pr_number"])
        comments = ghclient.issue_comments_structured(card["repo"], card["pr_number"])
        diff = ghclient.pr_diff(card["repo"], card["pr_number"])
    except ghclient.GhError as e:
        db.log_event(c, "closure_context_error", card["key"], {"error": str(e)})
        return
    wt = None
    try:
        wt = worktree.make_worktree(card["repo"], card["pr_number"], card["head_sha"])
        _run_closure(c, card, priors, diff, card["engine"] or "claude", wt,
                     profiles.policy_from_card(card), author, comments)
    finally:
        if wt:
            worktree.remove_worktree(card["repo"], wt)


def process(c, card):
    repo, pr, head = card["repo"], card["pr_number"], card["head_sha"]
    policy = profiles.policy_from_card(card)
    if _is_stale(card):
        db.set_status(c, card["id"], "archived")
        db.log_event(c, "review_stale_skipped", card["key"], {"head": head})
        return

    db.set_status(c, card["id"], "reviewing")
    diff = ghclient.pr_diff(repo, pr)
    conversation = ghclient.pr_conversation(repo, pr)
    meta = _payload(card)
    engine = card["engine"] or "claude"
    priors = db.prior_open_findings(c, repo, pr, card["id"])
    try:
        author_identity = ghclient.pr_author_identity(repo, pr) if priors else {}
        structured_comments = ghclient.issue_comments_structured(repo, pr) if priors else []
    except ghclient.GhError as e:
        author_identity, structured_comments = {}, []
        db.log_event(c, "closure_context_error", card["key"], {"error": str(e)})
    is_doc = policy.get("profile_type") == "doc"
    plan = None

    wt = None
    try:
        wt = worktree.make_worktree(repo, pr, head)
        if is_doc:
            changed_files = ghclient.pr_changed_files(repo, pr)
            plan = doc_planner.build_plan(repo, pr, wt, diff, changed_files, policy)
            meta["doc_review_plan"] = plan
            _save_payload(c, card["id"], meta)
            db.log_event(c, "doc_review_plan", card["key"], plan)
            if plan.get("summary_only"):
                meta["doc_summary"] = {
                    "summary": "",
                    "needs_human_review": True,
                    "human_review_reason": (
                        f"large PR: {plan.get('changed_file_count')} files, "
                        f"{plan.get('diff_lines')} diff lines, "
                        f"{len(plan.get('epic_roots') or [])} epic roots"
                    ),
                }
                _save_payload(c, card["id"], meta)
                db.log_event(c, "doc_summary_planned", card["key"], meta["doc_summary"])
            _run_closure(c, card, priors, diff, engine, wt, policy,
                         author_identity, structured_comments, plan)
            context = doc_planner.build_context(wt, diff, changed_files, plan)
            prompt = prompt_tpl.render(
                profiles.prompt_name(policy, "review"),
                REPO=repo, PR=pr, TITLE=meta.get("title", ""),
                AUTHOR=meta.get("author", ""), HEAD=head, DIFF=diff[:120000],
                CONVERSATION=conversation, MAX_FINDINGS=policy["max_findings"],
                REVIEW_MODE=plan["review_mode"], PLAN_JSON=doc_planner.dumps(plan),
                DOC_CONTEXT=context,
            )
        else:
            _run_closure(c, card, priors, diff, engine, wt, policy,
                         author_identity, structured_comments)
            prompt = prompt_tpl.render(
                profiles.prompt_name(policy, "review"), REPO=repo, PR=pr, TITLE=meta.get("title", ""),
                AUTHOR=meta.get("author", ""), HEAD=head, DIFF=diff[:120000],
                CONVERSATION=conversation, MAX_FINDINGS=policy["max_findings"],
            )
        if engine == "claude" and not is_doc:
            prompt += CLAUDE_RECALL_NOTE
        result = engines.run_json(prompt, engine=engine, cwd=wt, add_dir=wt)
    finally:
        if wt:
            worktree.remove_worktree(repo, wt)

    findings = [f for f in (result.get("findings") or [])
                if (f.get("confidence") in _actionable_conf(policy))]
    if is_doc:
        findings = [f for f in findings if f.get("severity") in ACTIONABLE_SEVERITY_DOC]
    findings = findings[: int(policy["max_findings"])]

    # LLM이 쓴 인트로를 카드 payload에 저장 → commenter가 사용 (매번 다른 인트로)
    intro = (result.get("intro") or "").strip()
    if intro:
        meta["intro"] = intro
        _save_payload(c, card["id"], meta)

    if is_doc and plan and plan.get("summary_only"):
        meta["doc_summary"] = {
            "summary": result.get("summary", ""),
            "needs_human_review": result.get("needs_human_review", True),
            "human_review_reason": result.get("human_review_reason", ""),
        }
        _save_payload(c, card["id"], meta)
        db.log_event(c, "doc_summary", card["key"], meta["doc_summary"])
        blockers = db.unresolved_findings(c, repo, pr)
        decisions = db.pending_decision_findings(c, repo, pr)
        if blockers or decisions:
            for pf in blockers:
                db.reattach_finding(c, pf["id"], card["id"], "unresolved")
            for pf in decisions:
                db.reattach_finding(c, pf["id"], card["id"], pf["status"])
            db.set_status(c, card["id"], "commented")
            db.log_event(c, "doc_summary_blocked", card["key"],
                         {"unresolved": len(blockers), "pending_decisions": len(decisions)})
            return
        db.set_status(c, card["id"], policy["no_finding_terminal"])
        return

    to_verify = 0
    repeat_finding = False
    for f in findings:
        rule = _stable_rule(f)
        fp = keys.finding_fp(repo, pr, f.get("file", "?"), f.get("line", "?"), rule)
        # body stores problem + fix-direction together as JSON
        body = _json.dumps({"problem": f.get("problem", ""), "fix": f.get("fix", ""),
                            "evidence": f.get("evidence", ""),
                            "category": f.get("category", ""),
                            "impact": f.get("impact", ""),
                            "required_decision": f.get("required_decision", "")},
                           ensure_ascii=False)
        if db.upsert_finding(
            c, card["id"], repo, pr, head, fp,
            title=f.get("title", ""), body=body,
            file=f.get("file"), line=f.get("line"),
            severity=f.get("severity"), confidence=f.get("confidence"),
            status="pending_verify",
        ):
            to_verify += 1
        else:
            previous = db.revalidate_finding(
                c, card["id"], repo, pr, head, fp,
                title=f.get("title", ""), body=body,
                file=f.get("file"), line=f.get("line"),
                severity=f.get("severity"), confidence=f.get("confidence"),
            )
            if previous not in {"missing", "sticky"}:
                to_verify += 1
                repeat_finding = repeat_finding or previous in {
                    "posted", "confirmed", "unresolved",
                }

    # UNIQUE(repo, pr, fp) keeps a repeated finding on its original row. Closure
    # marks it unresolved; attach that row to this attempt so it is not lost.
    unresolved = db.unresolved_findings(c, repo, pr)
    for pf in unresolved:
        db.reattach_finding(c, pf["id"], card["id"], "confirmed")
    pending = db.pending_decision_findings(c, repo, pr)
    for pf in pending:
        db.reattach_finding(c, pf["id"], card["id"], pf["status"])
    if unresolved or repeat_finding:
        meta["force_post"] = True
        meta["intro"] = "지난 리뷰의 아래 지적이 아직 반영되지 않은 것 같아 다시 확인 부탁드립니다."
        _save_payload(c, card["id"], meta)
    if unresolved:
        db.log_event(c, "review_prior_unresolved", card["key"],
                     {"count": len(unresolved), "engine": engine})

    if to_verify:
        db.set_status(c, card["id"], "verifying")
        db.log_event(c, "review_findings", card["key"],
                     {"count": to_verify, "unresolved": len(unresolved),
                      "pending_decisions": len(pending), "engine": engine})
    elif unresolved:
        db.set_status(c, card["id"], "commenting")
    elif pending:
        db.set_status(c, card["id"], "commented")
        db.log_event(c, "review_author_decision_pending", card["key"],
                     {"count": len(pending)})
    else:
        terminal = policy["no_finding_terminal"]
        db.set_status(c, card["id"], terminal)
        event_type = "review_doc_no_findings" if is_doc else "review_lgtm"
        db.log_event(c, event_type, card["key"],
                     {"summary": result.get("summary"), "engine": engine, "terminal": terminal})
