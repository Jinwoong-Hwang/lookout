"""prcommenter: 검증된 finding들을 PR당 1개 묶음 댓글로 게시.

Ethan 봇 스타일 — 이모지/severity 데코 없이, 시니어 동료의 대화체 평문으로
번호 매긴 지적을 묶어서 한 댓글에. finding별 마커를 댓글 안에 심어 멱등성/closure
추적은 유지. dry_run_comments면 렌더+기록만(미게시).
"""
import json

from . import db, ghclient
from .config import CFG


def _marker(fp: str) -> str:
    return f"<!-- hermes:fp={fp} -->"


def _intro(author: str, n: int, mention: bool, custom: str = "") -> str:
    who = f"@{author} " if mention and author else ""
    custom = (custom or "").strip()
    if custom:  # LLM이 PR마다 쓴 인트로 사용
        return who + custom
    # fallback (LLM 인트로 없을 때)
    if n == 1:
        return f"{who}코드 확인하다가 아래 한 가지는 머지 전에 한 번 더 보면 좋을 것 같아 코멘트 남깁니다."
    return f"{who}코드 확인하다가 아래 {n}가지는 머지 전에 한 번 더 보완하면 좋아 보여 코멘트 남깁니다."


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
    parts += ["", _marker(f["fp"])]
    return "\n".join(parts)


def render_bundle(author: str, findings, mention: bool = True, intro: str = "") -> str:
    numbered = len(findings) > 1  # 단일이면 번호 생략 (인트로가 이미 '한 가지')
    blocks = [_intro(author, len(findings), mention, intro), ""]
    for i, f in enumerate(findings, 1):
        blocks.append(_block(i, f, numbered))
        blocks.append("")
    return "\n".join(blocks).rstrip()


def process(c, card):
    repo, pr = card["repo"], card["pr_number"]
    meta = json.loads(card["payload"]) if card["payload"] else {}
    author = meta.get("author", "")
    intro = meta.get("intro", "")
    force = bool(meta.get("force_post"))  # 미해결 리마인드 — 기존 댓글 있어도 다시 게시
    confirmed = db.findings_for_card(c, card["id"], status="confirmed")

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

    body = render_bundle(author, fresh, mention=force or not already, intro=intro)
    fps = [f["fp"] for f in fresh]
    if CFG["dry_run_comments"]:
        for f in fresh:
            db.set_finding_status(c, f["id"], "posted", comment_id="DRYRUN")
        db.log_event(c, "comment_dryrun", card["key"], {"fps": fps, "body": body})
    else:
        out = ghclient.pr_comment(repo, pr, body)
        for f in fresh:
            db.set_finding_status(c, f["id"], "posted", comment_id=out or "posted")
        db.log_event(c, "comment_posted", card["key"], {"fps": fps, "url": out})

    db.set_status(c, card["id"], "commented")
