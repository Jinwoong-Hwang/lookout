"""prreviewer: review the PR head in a detached worktree, emit findings.

Read-only on the target repo. Stale heads are skipped (router/monitor create a
fresh review card for the new head).
"""
from . import db, engines, ghclient, keys, prompt_tpl, worktree
from .config import CFG

ACTIONABLE_CONF = {"high"} if CFG.get("min_confidence") == "high" else {"high", "medium"}

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


def _run_closure(c, card, priors, diff, conversation, engine, wt):
    """Re-judge previously-raised findings at the new head, considering replies."""
    import json as _json
    for pf in priors:
        detail = _json.loads(pf["body"]) if pf["body"] else {}
        cprompt = prompt_tpl.render(
            "closure.md", FILE=pf["file"], LINE=pf["line"], TITLE=pf["title"],
            PROBLEM=detail.get("problem", ""), DIFF=diff[:40000], CONVERSATION=conversation,
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
    if _is_stale(card):
        db.set_status(c, card["id"], "archived")
        db.log_event(c, "review_stale_skipped", card["key"], {"head": head})
        return

    db.set_status(c, card["id"], "reviewing")
    diff = ghclient.pr_diff(repo, pr)
    conversation = ghclient.pr_conversation(repo, pr)
    payload = card["payload"]
    import json as _json
    meta = _json.loads(payload) if payload else {}
    engine = card["engine"] or "claude"
    priors = db.prior_open_findings(c, repo, pr, card["id"])

    wt = None
    try:
        wt = worktree.make_worktree(repo, pr, head)
        prompt = prompt_tpl.render(
            "review.md", REPO=repo, PR=pr, TITLE=meta.get("title", ""),
            AUTHOR=meta.get("author", ""), HEAD=head, DIFF=diff[:120000],
            CONVERSATION=conversation, MAX_FINDINGS=CFG["max_findings_per_review"],
        )
        if engine == "claude":
            prompt += CLAUDE_RECALL_NOTE
        result = engines.run_json(prompt, engine=engine, cwd=wt, add_dir=wt)
        _run_closure(c, card, priors, diff, conversation, engine, wt)
    finally:
        if wt:
            worktree.remove_worktree(repo, wt)

    findings = [f for f in (result.get("findings") or [])
                if (f.get("confidence") in ACTIONABLE_CONF)]
    findings = findings[: CFG["max_findings_per_review"]]

    # LLM이 쓴 인트로를 카드 payload에 저장 → commenter가 사용 (매번 다른 인트로)
    intro = (result.get("intro") or "").strip()
    if intro:
        meta["intro"] = intro
        c.execute("UPDATE cards SET payload=? WHERE id=?",
                  (_json.dumps(meta, ensure_ascii=False), card["id"]))

    if not findings:
        unresolved = db.unresolved_findings(c, repo, pr)
        if unresolved:
            # 새 이슈는 없지만 이전 미해결 지적이 남음 → 현재 카드로 재첨부.
            # force_post=True → 기존 댓글이 있어도 최신 head에 다시 게시(리마인드).
            for pf in unresolved:
                db.reattach_finding(c, pf["id"], card["id"], "confirmed")
            meta["force_post"] = True
            meta["intro"] = "지난 리뷰의 아래 지적이 아직 반영되지 않은 것 같아 다시 확인 부탁드립니다."
            c.execute("UPDATE cards SET payload=? WHERE id=?",
                      (_json.dumps(meta, ensure_ascii=False), card["id"]))
            db.set_status(c, card["id"], "commenting")
            db.log_event(c, "review_prior_unresolved", card["key"],
                         {"count": len(unresolved), "engine": engine})
            return
        db.set_status(c, card["id"], "lgtm")
        db.log_event(c, "review_lgtm", card["key"],
                     {"summary": result.get("summary"), "engine": engine})
        return

    created = 0
    for f in findings:
        fp = keys.finding_fp(repo, pr, f.get("file", "?"), f.get("line", "?"), f.get("rule", "?"))
        # body stores problem + fix-direction together as JSON
        body = _json.dumps({"problem": f.get("problem", ""), "fix": f.get("fix", ""),
                            "evidence": f.get("evidence", "")}, ensure_ascii=False)
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
