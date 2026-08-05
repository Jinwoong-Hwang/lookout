"""prcommenter: 검증된 finding들을 PR당 1개 묶음 댓글로 게시.

Ethan 봇 스타일 — 이모지/severity 데코 없이, 시니어 동료의 대화체 평문으로
번호 매긴 지적을 묶어서 한 댓글에. finding별 마커를 댓글 안에 심어 멱등성/closure
추적은 유지. dry_run_comments면 렌더+기록만(미게시).
"""
import json

from . import db, ghclient, profiles, reviewer
from .config import CFG

BOT_PREFIX = "🤖 "
FEEDBACK_SENTINEL = "리뷰가 도움이 되었나요?"


def _feedback_footer(subject: str) -> str:
    reasons = "오탐 / 맥락 부족 / 영향 과장 / 중복 / 톤" if subject == "코드" else "사실 오류 / 맥락 부족 / 불명확 / 중복 / 톤"
    return "\n".join([
        "---",
        f"이 {subject} 리뷰가 도움이 되었나요? 이 댓글에 👍 / 👎 반응을 남겨주세요.",
        f"도움이 안 됐다면 가능할 때 짧게 이유를 남겨주세요: {reasons}",
    ])


def _review_subject(policy: dict) -> str:
    return "문서" if policy.get("profile_type") == "doc" else "코드"


def _feedback_subject(policy: dict, posted_bodies) -> str:
    if any(FEEDBACK_SENTINEL in b for b in posted_bodies):
        return ""
    return _review_subject(policy)


def _marker(fp: str) -> str:
    return f"<!-- hermes:fp={fp} -->"


def _intro(author: str, n: int, mention: bool, custom: str = "", subject: str = "코드") -> str:
    who = f"@{author} " if mention and author else ""
    custom = (custom or "").strip()
    if custom:  # LLM이 PR마다 쓴 인트로 사용
        return who + custom
    # fallback (LLM 인트로 없을 때)
    if n == 1:
        return f"{who}{subject} 확인하다가 아래 한 가지는 머지 전에 한 번 더 보면 좋을 것 같아 코멘트 남깁니다."
    return f"{who}{subject} 확인하다가 아래 {n}가지는 머지 전에 한 번 더 보완하면 좋아 보여 코멘트 남깁니다."


def _lang(path: str) -> str:
    p = (path or "").lower()
    if p.endswith((".ts", ".tsx")):
        return "ts"
    if p.endswith((".js", ".jsx")):
        return "js"
    if p.endswith((".yml", ".yaml")):
        return "yaml"
    if p.endswith(".py"):
        return "python"
    if p.endswith(".json"):
        return "json"
    return ""


def _block(idx: int, f, numbered: bool) -> str:
    detail = json.loads(f["body"]) if f["body"] else {}
    loc = f"`{f['file']}`" + (f":{f['line']}" if f["line"] else "")
    title = (f["title"] or "").strip()
    parts = []
    if title:  # 제목 있을 때만 — 빈 제목으로 '****' 깨지는 것 방지
        parts += [(f"{idx}. " if numbered else "") + f"**{title}**", ""]
    elif numbered:
        parts += [f"**{idx}.**", ""]
    parts += ["**문제**", (detail.get("problem", "") or "").strip()]
    ev = (detail.get("evidence") or "").strip()
    if ev:
        # 위치를 코드블록 바로 위 캡션으로 붙여 코드와 연결
        parts += ["", loc, f"```{_lang(f['file'])}", ev, "```"]
    else:
        parts += ["", loc]
    fix = (detail.get("fix") or "").strip()
    if fix:
        parts += ["", "**제안**", fix]
    impact = (detail.get("impact") or "").strip()
    if impact:
        parts += ["", "**영향**", impact]
    decision = (detail.get("required_decision") or "").strip()
    if decision:
        parts += ["", "**결정 필요**", decision]
    parts += ["", _marker(f["fp"])]
    return "\n".join(parts)


def render_bundle(author: str, findings, mention: bool = True, intro: str = "",
                  subject: str = "코드", feedback_subject: str = "") -> str:
    numbered = len(findings) > 1  # 단일이면 번호 생략 (인트로가 이미 '한 가지')
    blocks = [BOT_PREFIX + _intro(author, len(findings), mention, intro, subject), ""]
    for i, f in enumerate(findings, 1):
        blocks.append(_block(i, f, numbered))
        blocks.append("")
    if feedback_subject:
        blocks.append(_feedback_footer(feedback_subject))
    return "\n".join(blocks).rstrip()


def process(c, card):
    repo, pr = card["repo"], card["pr_number"]
    policy = profiles.policy_from_card(card)
    meta = json.loads(card["payload"]) if card["payload"] else {}
    author = meta.get("author", "")
    intro = meta.get("intro", "")
    force = bool(meta.get("force_post"))  # 미해결 리마인드 — 기존 댓글 있어도 다시 게시
    # A PR author can reply after review/verify but before this irreversible post.
    # Re-check only findings that have already been posted and can therefore have
    # a linked reply.  Hold this bundle if the reply now awaits operator approval.
    reviewer.refresh_author_decisions(c, card)
    if db.pending_decision_findings(c, repo, pr):
        db.set_status(c, card["id"], "commented")
        db.log_event(c, "comment_held_author_decision", card["key"])
        return

    confirmed = db.findings_for_card(c, card["id"], status="confirmed")
    comment_policy = policy.get("comment_policy", "global")
    effective_dry_run = bool(CFG["dry_run_comments"]) or comment_policy == "dry_run"
    silent = comment_policy == "silent"

    posted_bodies = []
    already = False
    if not silent:
        existing = ghclient.list_review_comments(repo, pr)
        posted_bodies = [com["body"] for com in existing if com.get("body")]
        already = any("hermes:fp" in b for b in posted_bodies)

    # force면 마커 중복 무시하고 전부 게시, 아니면 아직 안 올라간 것만
    fresh = []
    for f in confirmed:
        if not force and any(_marker(f["fp"]) in b for b in posted_bodies):
            db.set_finding_status(c, f["id"], "posted", comment_id="exists")
        else:
            fresh.append(f)

    if force:  # 1회성 플래그 — 게시 후 재사용 방지
        meta.pop("force_post", None)
        c.execute("UPDATE cards SET payload=? WHERE id=?",
                  (json.dumps(meta, ensure_ascii=False), card["id"]))

    if not fresh:
        db.set_status(c, card["id"], "commented")
        return

    subject = _review_subject(policy)
    body = render_bundle(author, fresh, mention=force or not already, intro=intro,
                         subject=subject,
                         feedback_subject=_feedback_subject(policy, posted_bodies))
    fps = [f["fp"] for f in fresh]
    if silent:
        for f in fresh:
            db.set_finding_status(c, f["id"], "posted", comment_id="SILENT")
        db.log_event(c, "comment_silent", card["key"], {"fps": fps, "body": body})
    elif effective_dry_run:
        for f in fresh:
            db.set_finding_status(c, f["id"], "posted", comment_id="DRYRUN")
        db.log_event(c, "comment_dryrun", card["key"], {"fps": fps, "body": body})
    else:
        out = ghclient.pr_comment(repo, pr, body)
        for f in fresh:
            db.set_finding_status(c, f["id"], "posted", comment_id=out or "posted")
        db.log_event(c, "comment_posted", card["key"], {"fps": fps, "url": out})

    db.set_status(c, card["id"], "commented")


def publish_dryrun(c, card) -> bool:
    """Operator override: publish a previously rendered dry-run bundle."""
    repo, pr = card["repo"], card["pr_number"]
    policy = profiles.policy_from_card(card)
    meta = json.loads(card["payload"]) if card["payload"] else {}
    author = meta.get("author", "")
    intro = meta.get("intro", "")
    dry = c.execute(
        "SELECT * FROM findings WHERE card_id=? AND comment_id='DRYRUN' ORDER BY id",
        (card["id"],),
    ).fetchall()
    if not dry:
        return False

    existing = ghclient.list_review_comments(repo, pr)
    posted_bodies = [com["body"] for com in existing if com.get("body")]
    already = any("hermes:fp" in b for b in posted_bodies)
    fresh = []
    for f in dry:
        if any(_marker(f["fp"]) in b for b in posted_bodies):
            db.set_finding_status(c, f["id"], "posted", comment_id="exists")
        else:
            fresh.append(f)
    if not fresh:
        db.set_status(c, card["id"], "commented")
        return True

    subject = _review_subject(policy)
    body = render_bundle(author, fresh, mention=not already, intro=intro,
                         subject=subject,
                         feedback_subject=_feedback_subject(policy, posted_bodies))
    out = ghclient.pr_comment(repo, pr, body)
    for f in fresh:
        db.set_finding_status(c, f["id"], "posted", comment_id=out or "posted")
    db.log_event(c, "comment_dryrun_published", card["key"],
                 {"fps": [f["fp"] for f in fresh], "url": out, "body": body})
    db.set_status(c, card["id"], "commented")
    return True
