"""Feedback snapshots for review comments.

This intentionally avoids real-time scoring. It captures low-friction GitHub
signals at PR close and periodic open-PR sampling so humans can inspect prompt
quality later.
"""
import json
import re
import time

from . import db, ghclient, profiles

BOT_MARKER = "hermes:fp="
FEEDBACK_SENTINEL = "리뷰가 도움이 되었나요?"
WEEKLY_INTERVAL_SECONDS = 7 * 86400
OPEN_SAMPLE_MIN_AGE_SECONDS = 72 * 3600


def _payload(card) -> dict:
    try:
        data = json.loads(card["payload"]) if card["payload"] else {}
        return json.loads(data) if isinstance(data, str) else data
    except json.JSONDecodeError:
        return {}


def _marker(fp: str) -> str:
    return f"<!-- hermes:fp={fp} -->"


def _is_bot_review_comment(comment: dict, bot_login: str = "") -> bool:
    if bot_login and comment.get("user") != bot_login:
        return False
    body = comment.get("body") or ""
    return BOT_MARKER in body or FEEDBACK_SENTINEL in body


def _reaction_counts(repo: str, comment: dict) -> dict:
    return ghclient.comment_reactions(repo, str(comment.get("id") or ""))


def _author_replies(comments: list[dict], idx: int, author: str, bot_login: str = "") -> list[dict]:
    replies = []
    for c in comments[idx + 1:]:
        if _is_bot_review_comment(c, bot_login):
            break
        if (c.get("user") or "") != author:
            continue
        body = (c.get("body") or "").strip()
        if not body:
            continue
        replies.append({
            "id": str(c.get("id") or ""),
            "url": c.get("html_url") or "",
            "created_at": c.get("created_at") or "",
            "body": body[:2000],
        })
    return replies


def _profile_type(card) -> str:
    return profiles.policy_from_card(card).get("profile_type", "code")


def _outcome(c, card, pr_info: dict | None) -> dict:
    clo = db.closure_counts(c, card["repo"], card["pr_number"])
    current_head = (pr_info or {}).get("headRefOid")
    return {
        "state": (pr_info or {}).get("state", ""),
        "current_head": current_head or "",
        "head_changed": bool(current_head and current_head != card["head_sha"]),
        "card_status": card["status"],
        "resolved": int(clo.get("resolved", 0)),
        "unresolved": int(clo.get("unresolved", 0)),
    }


def _upsert_snapshot(c, card, snapshot_type: str, comment: dict,
                     replies: list[dict], pr_info: dict | None):
    reactions = _reaction_counts(card["repo"], comment)
    outcome = _outcome(c, card, pr_info)
    c.execute(
        """INSERT INTO review_feedback_snapshots(
             card_id, repo, pr_number, head_sha, profile_type, snapshot_type,
             comment_id, comment_url, reactions, author_replies, outcome,
             created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(card_id, snapshot_type, comment_id) DO UPDATE SET
             comment_url=excluded.comment_url,
             reactions=excluded.reactions,
             author_replies=excluded.author_replies,
             outcome=excluded.outcome,
             created_at=excluded.created_at""",
        (
            card["id"], card["repo"], card["pr_number"], card["head_sha"],
            _profile_type(card), snapshot_type, str(comment.get("id") or ""),
            comment.get("html_url") or "",
            json.dumps(reactions, ensure_ascii=False),
            json.dumps(replies, ensure_ascii=False),
            json.dumps(outcome, ensure_ascii=False),
            db.now(),
        ),
    )
    return {
        "comment_id": str(comment.get("id") or ""),
        "comment_url": comment.get("html_url") or "",
        "reactions": reactions,
        "reply_count": len(replies),
        "outcome": outcome,
    }


def snapshot_card(c, card, snapshot_type: str = "manual",
                  pr_info: dict | None = None, comments: list[dict] | None = None) -> list[dict]:
    """Capture feedback for bot comments that contain this card's findings."""
    findings = db.findings_for_card(c, card["id"])
    markers = [_marker(f["fp"]) for f in findings if f["fp"]]
    if not markers:
        return []
    if comments is None:
        comments = ghclient.issue_comments(card["repo"], card["pr_number"])
    if pr_info is None:
        try:
            pr_info = ghclient.pr_view(card["repo"], card["pr_number"])
        except ghclient.GhError:
            pr_info = {}
    author = (_payload(card).get("author") or ((pr_info.get("author") or {}).get("login")) or "")
    bot_login = ghclient.my_login()
    if not bot_login:
        raise ghclient.GhError("cannot resolve gh authenticated user for feedback snapshot")

    snapshots = []
    for idx, comment in enumerate(comments):
        if not _is_bot_review_comment(comment, bot_login):
            continue
        body = comment.get("body") or ""
        if not any(m in body for m in markers):
            continue
        replies = _author_replies(comments, idx, author, bot_login)
        snapshots.append(_upsert_snapshot(c, card, snapshot_type, comment, replies, pr_info))
    if snapshots:
        db.log_event(c, "feedback_snapshot", card["key"],
                     {"type": snapshot_type, "count": len(snapshots)})
    return snapshots


def snapshot_pr(c, repo: str, pr: int, snapshot_type: str = "pr_closed",
                pr_info: dict | None = None) -> int:
    comments = ghclient.issue_comments(repo, pr)
    if not comments:
        return 0
    cards = c.execute(
        "SELECT * FROM cards WHERE repo=? AND pr_number=? AND kind='review'",
        (repo, pr),
    ).fetchall()
    total = 0
    for card in cards:
        total += len(snapshot_card(c, card, snapshot_type, pr_info=pr_info, comments=comments))
    return total


def weekly_open(c, limit: int = 30) -> int:
    cutoff = db.now() - OPEN_SAMPLE_MIN_AGE_SECONDS
    weekly_type = f"weekly_open:{time.strftime('%G-W%V')}"
    last_weekly_cutoff = db.now() - WEEKLY_INTERVAL_SECONDS
    rows = c.execute(
        """SELECT * FROM cards card
           WHERE kind='review'
             AND status IN ('commented','done','lgtm','approve_blocked')
             AND updated_at < ?
             AND EXISTS (
               SELECT 1 FROM findings f
               WHERE f.card_id=card.id
                 AND f.comment_id NOT IN ('DRYRUN','SILENT','exists')
                 AND f.comment_id IS NOT NULL
             )
             AND NOT EXISTS (
               SELECT 1 FROM review_feedback_snapshots s
               WHERE s.card_id=card.id
                 AND s.snapshot_type LIKE 'weekly_open:%'
                 AND s.created_at > ?
             )
           ORDER BY updated_at ASC
           LIMIT ?""",
        (cutoff, last_weekly_cutoff, int(limit)),
    ).fetchall()
    total = 0
    for card in rows:
        try:
            info = ghclient.pr_view(card["repo"], card["pr_number"])
        except ghclient.GhError as e:
            db.log_event(c, "feedback_weekly_error", card["key"], {"error": str(e)})
            continue
        if info.get("state") != "OPEN":
            continue
        try:
            total += len(snapshot_card(c, card, weekly_type, pr_info=info))
        except ghclient.GhError as e:
            db.log_event(c, "feedback_weekly_error", card["key"], {"error": str(e)})
            continue
    if total:
        db.log_event(c, "feedback_weekly", detail={"snapshots": total})
    return total


def latest_for_card(c, card_id: int) -> dict:
    rows = c.execute(
        """SELECT * FROM review_feedback_snapshots
           WHERE card_id=?
           ORDER BY created_at DESC, id DESC""",
        (card_id,),
    ).fetchall()
    if not rows:
        return {}
    up = down = confused = replies = 0
    last = rows[0]
    seen = set()
    for r in rows:
        if r["comment_id"] in seen:
            continue
        seen.add(r["comment_id"])
        reactions = json.loads(r["reactions"]) if r["reactions"] else {}
        author_replies = json.loads(r["author_replies"]) if r["author_replies"] else []
        up += int(reactions.get("+1") or 0)
        down += int(reactions.get("-1") or 0)
        confused += int(reactions.get("confused") or 0)
        replies += len(author_replies)
    return {
        "snapshot_type": last["snapshot_type"],
        "up": up,
        "down": down,
        "confused": confused,
        "replies": replies,
        "needs_inspection": bool(down or confused or replies),
    }


def classify_reply_reason(body: str) -> str:
    """Tiny helper for future reports; keep matching transparent."""
    text = (body or "").lower()
    patterns = [
        ("false-positive", r"오탐|틀림|사실 오류|wrong|false"),
        ("low-context", r"맥락 부족|context"),
        ("unclear", r"불명확|unclear"),
        ("duplicate", r"중복|duplicate"),
        ("tone", r"말투|톤|tone"),
        ("overstated", r"영향 과장|과장"),
    ]
    for name, pat in patterns:
        if re.search(pat, text):
            return name
    return "other"
