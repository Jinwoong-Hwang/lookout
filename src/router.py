"""Event router (ADR-001/002, LLM-free).

Drains the inbox and turns webhook events into Kanban cards:
  - allowlist filter on repository.full_name (ADR-002, shared board)
  - optional author filter (personalization)
  - dedupe via idempotency keys (always OWNER/REPO + head, ADR-002)
  - ALWAYS re-resolves the authoritative head via `gh pr view` (never trusts
    the webhook payload head)
"""
from . import config, db, ghclient, keys, slack_names

CFG = config.CFG
ALLOWLIST = set(CFG["allowlist"])
WATCH_AUTHORS = set(CFG.get("watch_authors") or [])
AUTO_REVIEW_AUTHORS = set(CFG.get("auto_review_authors") or [])
MY_SLACK = CFG.get("slack_user_id", "")
SLACK_WORKSPACE = CFG.get("slack_workspace", "")


def _allowed(repo: str) -> bool:
    return repo in ALLOWLIST


def _author_ok(login: str) -> bool:
    """Track PRs from watched authors (empty watch list = everyone)."""
    return not WATCH_AUTHORS or login in WATCH_AUTHORS


def _initial_status(login: str) -> str:
    """Auto-review authors skip triage; everyone else waits for manual start."""
    return "intake" if login in AUTO_REVIEW_AUTHORS else "triage"


def ensure_pr_cards(c, repo: str, pr: int, source: str = "webhook"):
    """Idempotently ensure root + current-head review cards for a PR."""
    info = ghclient.pr_view(repo, pr)
    if info.get("state") != "OPEN" or info.get("isDraft"):
        return None
    author = (info.get("author") or {}).get("login", "")
    if not _author_ok(author):
        db.log_event(c, "skip_author", keys.root_key(repo, pr), {"author": author})
        return None

    head = info["headRefOid"]
    # root card (per PR, head-independent)
    rkey = keys.root_key(repo, pr)
    root_id = db.upsert_card(
        c, rkey, "root", repo, pr, status="monitoring", head_sha=head,
        base_sha=info.get("baseRefName"),
        payload={"title": info.get("title"), "url": info.get("url"), "author": author},
    )

    # review card (per head). New head -> new key -> fresh review.
    vkey = keys.review_key(repo, pr, head)
    if db.get_card(c, vkey) is None:
        status = _initial_status(author)
        db.upsert_card(
            c, vkey, "review", repo, pr, status=status, head_sha=head,
            base_sha=info.get("baseRefName"),
            payload={"title": info.get("title"), "url": info.get("url"),
                     "author": author, "source": source},
        )
        db.log_event(c, "review_card_created", vkey,
                     {"head": head, "source": source, "status": status})
    db.mark_seen_head(c, repo, pr, head)
    return root_id


def _handle_pull_request(c, payload):
    action = payload.get("action")
    if action not in ("opened", "synchronize", "reopened", "ready_for_review"):
        return
    repo = payload["repository"]["full_name"]
    if not _allowed(repo):
        db.log_event(c, "skip_repo", keys.root_key(repo, payload["pull_request"]["number"]))
        return
    pr = payload["pull_request"]["number"]
    ensure_pr_cards(c, repo, pr)


def _handle_slack(c, payload):
    """Store mentions of me (MY_SLACK). 'Mention = anything' per design: any
    message whose text contains <@MY_SLACK>. LLM-free (ADR-001)."""
    ev = payload.get("event", {}) or {}
    if ev.get("type") not in ("message", "app_mention"):
        return
    if ev.get("subtype") or ev.get("bot_id"):  # edits/joins/bot echoes — skip
        return
    text = ev.get("text", "") or ""
    if not MY_SLACK or f"<@{MY_SLACK}>" not in text:
        return
    ch, ts, uid = ev.get("channel", ""), ev.get("ts", ""), ev.get("user", "")
    event_id = payload.get("event_id") or f"{ch}:{ts}"
    permalink = ""
    if SLACK_WORKSPACE and ch and ts:
        permalink = f"https://{SLACK_WORKSPACE}.slack.com/archives/{ch}/p{ts.replace('.', '')}"
    db.insert_mention(
        c, event_id=event_id, channel_id=ch,
        channel_name=slack_names.channel(c, ch),
        user_id=uid, user_name=slack_names.user(c, uid),
        text=text, ts=ts, permalink=permalink,
    )
    db.log_event(c, "slack_mention", event_id, {"channel": ch, "user": uid})


def process_event(c, event_type: str, payload: dict):
    if event_type == "pull_request":
        _handle_pull_request(c, payload)
    elif event_type == "slack":
        _handle_slack(c, payload)
    else:
        # push / issue_comment / pull_request_review handled by monitor stage
        db.log_event(c, "event_noted", detail={"event": event_type})


def drain(c):
    import json
    rows = db.pending_inbox(c)
    for row in rows:
        try:
            payload = json.loads(row["raw"])
            process_event(c, row["event_type"], payload)
        except Exception as e:  # noqa: BLE001 - keep draining; record failure
            db.log_event(c, "router_error", detail={"inbox_id": row["id"], "error": str(e)})
        finally:
            db.mark_inbox_done(c, row["id"])
    return len(rows)
