"""prreviewer: review the PR head in a detached worktree, emit findings.

Read-only on the target repo. Stale heads are skipped (router/monitor create a
fresh review card for the new head).
"""
import hashlib
import json as _json
import re

from . import (db, doc_planner, engines, ghclient, keys, ledger, prdiff, profiles,
               prompt_tpl, worktree)
from .config import CFG

ACTIONABLE_SEVERITY_DOC = {"blocking", "should-fix"}
# 작성자 답변이 한 번 붙은 finding — 근거 없이 되돌리지 않는다
DECIDED = {"dismissed", "deferred", "dismiss_pending", "defer_pending"}
# 그 중 보류는 코드 근거로 재개하지 않는다 — 아래 _run_closure 참고
DEFERRED = {"deferred", "defer_pending"}

# closure 는 지적 하나만 판단한다. 그 지적의 파일은 prefer 로 반드시 싣고, 나머지
# 파일은 이 예산만큼만 곁들인다 — 예전엔 PR 전체 diff 를 40,000자씩 지적마다
# 다시 보냈고 그게 #10066 프롬프트 비용의 46%였다.
CLOSURE_DIFF_CHARS = 4000


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
    # line 을 넣으면 줄이 밀릴 때마다 rule 이 새로 생겨 지문에서 line 을 뺀 효과가
    # 사라진다 — 모델이 rule 을 안 줄 때의 폴백도 위치에 흔들리지 않아야 한다
    raw = "|".join(str(f.get(k, "")) for k in ("category", "title", "file"))
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


def _nothing_changed(card, pf, head, newest_reply, cache) -> bool:
    """마지막 판정 이후 이 지적에 관해 달라진 게 없으면 다시 묻지 않는다.

    #10066 에서 closure 35회 중 26회가 "아무것도 안 바뀜" 결론이었다.

    건너뛰면 status 가 그대로 남는다. 그래서 posted 인 지적은 다시 올라가지 않는다
    — 코드도 안 바뀌고 작성자 말도 없는데 매 라운드 같은 말을 반복하던 것을 여기서
    끊는다. 이미 unresolved 로 내려간 지적은 종전대로 리마인드 대상이다.

    같은 head 에서의 재확인(commenter 가 게시 직전에 부르는 것)은 건너뛰지 않는다.
    그게 리뷰 패스의 오판을 되돌린 적이 실제로 있어서, 마지막 방어선으로 남긴다.
    """
    since = pf["last_judged_head"]
    if not since or since == head:
        return False
    if (pf["last_seen_reply"] or "") != newest_reply:
        return False  # 새 회신이 왔거나 기존 글이 고쳐졌다
    if since not in cache:
        cache[since] = worktree.changed_files_between(card["repo"], since, head)
    changed = cache[since]
    if changed is None:
        return False  # 뭐가 바뀌었는지 모르면 판정한다
    return (pf["file"] or "") not in changed


def _run_closure(c, card, priors, diff, engine, wt, policy,
                 author: dict, all_replies: list[dict], plan=None):
    """Re-judge previous findings using backend-verified PR-author replies.

    회신은 카드당 한 번만 모은다. 예전엔 지적마다 '그 지적의 봇 댓글 뒤 창' 을
    다시 훑었는데, 그 창 규칙이 #10066 에서 회신을 통째로 놓친 원인이었다.
    지금은 작성자가 쓴 글 전부를 후보로 넘기고, 어느 지적에 대한 답인지는
    판정기가 고른다 — 인용 검증과 운영자 게이트가 뒤를 받는다.
    """
    prompt_file = profiles.prompt_name(policy, "closure")
    # 문자로 자르면 파일 중간에서 끊기므로 여기서도 파일 단위로 다시 담는다
    sources = {}
    for r in all_replies:
        sources[r["source"]] = sources.get(r["source"], 0) + 1
    db.log_event(c, "closure_replies_collected", card["key"],
                 {"count": len(all_replies), "sources": sources,
                  "chars": sum(len(r["body"]) for r in all_replies)})
    # 시각이 아니라 내용 지문 — 본문·댓글 '수정' 도 새 답변으로 잡아야 한다
    newest_reply = ghclient.replies_digest(all_replies)
    head = card["head_sha"]
    changed_cache = {}
    judged = set()
    for pf in priors:
        decision_head = pf["decision_head"]
        if pf["status"] in DECIDED and decision_head == head:
            continue
        if _nothing_changed(card, pf, head, newest_reply, changed_cache):
            db.log_event(c, "finding_closure_skipped", card["key"],
                         {"fp": pf["fp"], "status": pf["status"],
                          "since": pf["last_judged_head"]})
            continue

        # 이미 근거로 쓴 회신은 예산과 무관하게 남긴다
        replies = ghclient.trim_author_replies(
            all_replies, pinned=str(pf["decision_comment_id"] or ""))
        cdiff, cfiles, _ = prdiff.pack(diff, CLOSURE_DIFF_CHARS, prefer=[pf["file"]])
        detail = _json.loads(pf["body"]) if pf["body"] else {}
        cprompt = prompt_tpl.render(
            prompt_file, FILE=pf["file"], LINE=pf["line"], TITLE=pf["title"],
            PROBLEM=detail.get("problem", ""), DIFF=cdiff, FILES=cfiles,
            STATUS=pf["status"],
            AUTHOR=author.get("login", ""),
            REPLIES_JSON=_json.dumps(replies, ensure_ascii=False),
            PLAN_JSON=doc_planner.dumps(plan or {}),
        )
        try:
            verdict = engines.run_json(cprompt, engine=engine, cwd=wt, add_dir=wt)
        except Exception as e:  # noqa: BLE001 - closure failure must block LGTM
            db.clear_finding_decision(c, pf["id"], "unresolved")
            # 판정한 것으로 친다 — 실패를 '조용히 넘어간 지적' 으로 두면 LGTM 을
            # 막으려던 의도가 쿨다운에 먹힌다
            judged.add(pf["fp"])
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
        elif pf["status"] in DECIDED and status == "unresolved" and verified_reply:
            # 작성자가 답을 뒤집었다 — 검증된 인용이 근거다. 보류 유지 분기보다
            # 먼저 와야 한다. 안 그러면 철회 경로가 아예 막혀, 봇도 사람도 못 여는
            # 일방통행이 된다(셀프 리뷰 지적).
            db.clear_finding_decision(c, pf["id"], status)
        elif pf["status"] in DEFERRED and status != "resolved":
            # 보류는 "고장 난 걸 아는데 지금 안 고친다" 는 뜻이라, "아직 고장 나
            # 있다" 는 코드 근거로는 재개될 수 없다 — 동어반복이기 때문이다.
            # #10066 재현에서 판정기가 작성자가 보류한 내용을 그대로 인용해
            # (심지어 "PO 결정 대기" 라고 적힌 코드 주석까지) 재개를 시도했고,
            # 게시 직전 재확인이 아니었으면 리마인드가 다시 나갔다. 철회는
            # 작성자의 새 회신이나 운영자만 할 수 있다.
            status = pf["status"]
            db.set_finding_status(c, pf["id"], status)
        elif pf["status"] in DECIDED and status != "resolved" and not evidence:
            # 새 head 재확인에서 반박 근거가 없으면 이전 결정을 그대로 둔다.
            # 예전엔 dismissed/deferred 만 지켜서, 운영자 수용을 기다리던 *_pending
            # 이 다음 커밋에 조용히 unresolved 로 떨어져 리마인드가 다시 나갔다.
            status = pf["status"]
            db.set_finding_status(c, pf["id"], status)
        elif status in {"dismissed", "deferred"}:
            status = "unresolved"
            db.clear_finding_decision(c, pf["id"], status)
        else:
            db.clear_finding_decision(c, pf["id"], status)
        db.mark_finding_judged(c, pf["id"], head, newest_reply)
        judged.add(pf["fp"])
        db.log_event(c, "finding_closure", card["key"],
                     {"fp": pf["fp"], "status": status,
                      "replies_seen": len(replies),
                      "evidence": evidence,
                      "reply_comment_id": verified_reply["id"] if verified_reply else "",
                      "reply_evidence": (verdict.get("reply_evidence") or "").strip()})
    return judged


def refresh_author_decisions(c, card):
    """Last responsible moment for replies posted while review/verify ran."""
    priors = db.posted_findings_for_closure(c, card["repo"], card["pr_number"])
    if not priors:
        return
    try:
        author = ghclient.pr_author_identity(card["repo"], card["pr_number"])
        replies = ghclient.collect_author_replies(card["repo"], card["pr_number"], author)
        # pr_diff는 대형 PR에서 DiffTooLarge(=GhError)를 던져 아래 except가 삼킨다.
        # 그러면 작성자 회신 재확인이 조용히 건너뛰어지므로 로컬 폴백을 쓴다.
        diff = prdiff.fetch(c, card)
    except ghclient.GhError as e:
        db.log_event(c, "closure_context_error", card["key"], {"error": str(e)})
        return
    wt = None
    try:
        wt = worktree.make_worktree(card["repo"], card["pr_number"], card["head_sha"])
        _run_closure(c, card, priors, diff, card["engine"] or "claude", wt,
                     profiles.policy_from_card(card), author, replies)
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
    raw_diff = prdiff.fetch(c, card)
    diff, manifest = prdiff.pack_logged(c, card, raw_diff)
    meta = _payload(card)
    engine = card["engine"] or "claude"
    is_doc = policy.get("profile_type") == "doc"
    priors = db.prior_open_findings(c, repo, pr, card["id"])
    try:
        author_identity = ghclient.pr_author_identity(repo, pr)
        author_replies = ghclient.collect_author_replies(repo, pr, author_identity)
    except ghclient.GhError as e:
        author_identity, author_replies = {}, []
        db.log_event(c, "closure_context_error", card["key"], {"error": str(e)})
    prior_findings, author_notes = ledger.build(c, repo, pr, author_replies)
    # 전사는 doc 프로필 프롬프트만 쓴다 — 코드 리뷰는 원장으로 대체됐다
    conversation = ghclient.pr_conversation(repo, pr) if is_doc else ""
    plan = None
    judged: set[str] = set()

    wt = None
    try:
        wt = worktree.make_worktree(repo, pr, head)
        if is_doc:
            changed_files = ghclient.pr_changed_files(repo, pr)
            plan = doc_planner.build_plan(repo, pr, wt, raw_diff, changed_files, policy)
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
            judged = _run_closure(c, card, priors, diff, engine, wt, policy,
                                  author_identity, author_replies, plan)
            context = doc_planner.build_context(wt, diff, changed_files, plan)
            prompt = prompt_tpl.render(
                profiles.prompt_name(policy, "review", engine),
                REPO=repo, PR=pr, TITLE=meta.get("title", ""),
                AUTHOR=meta.get("author", ""), HEAD=head, DIFF=diff, FILES=manifest,
                CONVERSATION=conversation, PRIOR_FINDINGS=prior_findings,
                AUTHOR_NOTES=author_notes, MAX_FINDINGS=policy["max_findings"],
                REVIEW_MODE=plan["review_mode"], PLAN_JSON=doc_planner.dumps(plan),
                DOC_CONTEXT=context,
            )
        else:
            judged = _run_closure(c, card, priors, diff, engine, wt, policy,
                                  author_identity, author_replies)
            prompt = prompt_tpl.render(
                profiles.prompt_name(policy, "review", engine),
                REPO=repo, PR=pr, TITLE=meta.get("title", ""),
                AUTHOR=meta.get("author", ""), HEAD=head, DIFF=diff, FILES=manifest,
                CONVERSATION=conversation, PRIOR_FINDINGS=prior_findings,
                AUTHOR_NOTES=author_notes, MAX_FINDINGS=policy["max_findings"],
            )
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
    #
    # 단, 이번 라운드에 실제로 다시 판정한 것만 끌어온다. 코드도 안 바뀌고 작성자
    # 말도 없어 건너뛴 지적을 매번 다시 올리면, 같은 말을 8번 반복하는 원래 증상이
    # 그대로 남는다(#10066). 건너뛴 지적은 제 카드에 unresolved 로 남아 있다가
    # 다음에 뭔가 달라지면 그때 다시 나간다.
    all_unresolved = db.unresolved_findings(c, repo, pr)
    unresolved = [pf for pf in all_unresolved if pf["fp"] in judged]
    quiet = len(all_unresolved) - len(unresolved)
    if quiet:
        db.log_event(c, "review_unresolved_quiet", card["key"], {"count": quiet})
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

    # 리마인드 대상과 LGTM 차단 대상은 다르다. 쿨다운으로 말을 아끼는 것과 "문제가
    # 없다" 고 선언하는 것은 별개인데, 한 변수로 둬서 조용히 넘어간 미해결 지적이
    # 있어도 카드가 lgtm 으로 갔다(셀프 리뷰 지적).
    if to_verify:
        db.set_status(c, card["id"], "verifying")
        db.log_event(c, "review_findings", card["key"],
                     {"count": to_verify, "unresolved": len(unresolved),
                      "pending_decisions": len(pending), "engine": engine})
    elif all_unresolved:
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
