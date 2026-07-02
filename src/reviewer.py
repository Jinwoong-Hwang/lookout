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


def _run_closure(c, card, priors, diff, conversation, engine, wt, policy, plan=None):
    """Re-judge previously-raised findings at the new head, considering replies."""
    prompt_file = profiles.prompt_name(policy, "closure")
    for pf in priors:
        detail = _json.loads(pf["body"]) if pf["body"] else {}
        cprompt = prompt_tpl.render(
            prompt_file, FILE=pf["file"], LINE=pf["line"], TITLE=pf["title"],
            PROBLEM=detail.get("problem", ""), DIFF=diff[:40000], CONVERSATION=conversation,
            PLAN_JSON=doc_planner.dumps(plan or {}),
        )
        try:
            verdict = engines.run_json(cprompt, engine=engine, cwd=wt, add_dir=wt)
        except Exception:  # noqa: BLE001 - keep prior status on failure
            continue
        status = "resolved" if verdict.get("resolved") else "unresolved"
        db.set_finding_status(c, pf["id"], status)
        db.log_event(c, "finding_closure", card["key"],
                     {"fp": pf["fp"], "resolved": verdict.get("resolved"),
                      "reason": verdict.get("reason")})


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
            prompt = prompt_tpl.render(
                profiles.prompt_name(policy, "review"), REPO=repo, PR=pr, TITLE=meta.get("title", ""),
                AUTHOR=meta.get("author", ""), HEAD=head, DIFF=diff[:120000],
                CONVERSATION=conversation, MAX_FINDINGS=policy["max_findings"],
            )
        if engine == "claude" and not is_doc:
            prompt += CLAUDE_RECALL_NOTE
        result = engines.run_json(prompt, engine=engine, cwd=wt, add_dir=wt)
        _run_closure(c, card, priors, diff, conversation, engine, wt, policy, plan)
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
        db.set_status(c, card["id"], policy["no_finding_terminal"])
        return

    if not findings:
        unresolved = db.unresolved_findings(c, repo, pr)
        if unresolved:
            # 새 이슈는 없지만 이전 미해결 지적이 남음 → 현재 카드로 재첨부.
            # force_post=True → 기존 댓글이 있어도 최신 head에 다시 게시(리마인드).
            for pf in unresolved:
                db.reattach_finding(c, pf["id"], card["id"], "confirmed")
            meta["force_post"] = True
            meta["intro"] = "지난 리뷰의 아래 지적이 아직 반영되지 않은 것 같아 다시 확인 부탁드립니다."
            _save_payload(c, card["id"], meta)
            db.set_status(c, card["id"], "commenting")
            db.log_event(c, "review_prior_unresolved", card["key"],
                         {"count": len(unresolved), "engine": engine})
            return
        terminal = policy["no_finding_terminal"]
        db.set_status(c, card["id"], terminal)
        event_type = "review_doc_no_findings" if is_doc else "review_lgtm"
        db.log_event(c, event_type, card["key"],
                     {"summary": result.get("summary"), "engine": engine, "terminal": terminal})
        return

    created = 0
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
            created += 1
    db.set_status(c, card["id"], "verifying")
    db.log_event(c, "review_findings", card["key"], {"count": created, "engine": engine})
