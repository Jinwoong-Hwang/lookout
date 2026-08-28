"""Local Kanban dashboard (stdlib only).

  python -m src.dashboard      # then open http://127.0.0.1:8788

Board view + operator actions (start / rereview / ignore / unblock).
"""
import json
import os
import re
import subprocess
import csv
import io
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import commenter, db, engines, feedback, ghclient, poller, profiles, router, worktree
from .config import CFG


def kick_tick():
    """클릭 즉시 tick을 깨워 리뷰/승인이 바로 시작되게 함 (5분 주기 대기 회피).
    이미 도는 tick이 있으면 flock 때문에 새 프로세스는 즉시 종료(무해)."""
    try:
        subprocess.Popen([os.sys.executable, "-m", "src.tick"],
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:  # noqa: BLE001
        pass

DASHBOARD_HOST = CFG.get("dashboard_host", "127.0.0.1")
PORT = int(CFG.get("dashboard_port", 8788))
WRITE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in
                       CFG.get("dashboard_write_networks", ["127.0.0.0/8", "::1/128"]))

LANES = [
    ("triage", "📥 Triage (리뷰 대기)"),
    ("intake", "⏳ 시작됨"),
    ("reviewing", "🔍 리뷰 중"),
    ("verifying", "🧪 검증 중"),
    ("commenting", "✍️ 댓글 작성"),
    ("commented", "💬 댓글 완료"),
    ("lgtm", "✅ LGTM"),
    ("approve_blocked", "🔒 승인 대기"),
    ("approving", "🚀 승인 중"),
    ("done", "🏁 완료 · 머지 대기"),
    ("failed", "⚠️ 실패 (재시도 필요)"),
]


def build_board():
    with db.connect() as c:
        cards = c.execute(
            "SELECT * FROM cards WHERE kind!='root' AND status!='archived' ORDER BY updated_at DESC"
        ).fetchall()
        out = []
        for card in cards:
            meta = json.loads(card["payload"]) if card["payload"] else {}
            if isinstance(meta, str):  # tolerate legacy double-encoded payloads
                meta = json.loads(meta)
            findings = []
            for f in db.findings_for_card(c, card["id"]):
                detail = json.loads(f["body"]) if f["body"] else {}
                findings.append({
                    "id": f["id"], "status": f["status"], "severity": f["severity"],
                    "confidence": f["confidence"], "file": f["file"], "line": f["line"],
                    "title": f["title"], "problem": detail.get("problem", ""),
                    "fix": detail.get("fix", ""),
                    "decision_comment_id": f["decision_comment_id"],
                    "decision_evidence": f["decision_evidence"],
                    "decision_follow_up": f["decision_follow_up"],
                })
            ev = c.execute(
                "SELECT type, detail FROM events WHERE key=? AND type IN ('comment_dryrun','comment_posted','comment_dryrun_published') ORDER BY id",
                (card["key"],),
            ).fetchall()
            comments = []
            for e in ev:
                d = json.loads(e["detail"]) if e["detail"] else {}
                comments.append({"type": e["type"], "body": d.get("body", ""), "url": d.get("url", "")})
            dryrun_pending = c.execute(
                "SELECT 1 FROM findings WHERE card_id=? AND comment_id='DRYRUN' LIMIT 1",
                (card["id"],),
            ).fetchone() is not None
            clo = db.closure_counts(c, card["repo"], card["pr_number"])
            err = ""
            if card["status"] == "failed":
                g = c.execute(
                    "SELECT detail FROM events WHERE key=? AND type='review_gave_up'"
                    " ORDER BY id DESC LIMIT 1", (card["key"],)).fetchone()
                d = json.loads(g["detail"]) if (g and g["detail"]) else {}
                reason = d.get("error") or ""
                if not reason:  # 구버전 이벤트엔 error가 없다 — trace 마지막 줄로 대체
                    t = c.execute(
                        "SELECT detail FROM events WHERE key=? AND type='stage_error'"
                        " ORDER BY id DESC LIMIT 1", (card["key"],)).fetchone()
                    if t and t["detail"]:
                        tr = (json.loads(t["detail"]).get("trace") or "").strip()
                        # trace 마지막 줄은 예외 메시지에 섞인 stderr 꼬리라 무의미할 수 있다.
                        # 뒤에서부터 실제 예외 줄(XxxError: ...)을 먼저 찾는다.
                        lines = [x.strip() for x in tr.splitlines() if x.strip()]
                        hit = next((x for x in reversed(lines) if re.search(r"\w+Error: ", x)), "")
                        reason = (hit or (lines[-1] if lines else ""))[:300]
                err = f"[{d.get('stage', 'reviewer')}] {reason}".strip()
            elif card["status"] == "triage":
                q = c.execute(
                    "SELECT detail FROM events WHERE key=? AND type='review_quota_paused'"
                    " ORDER BY id DESC LIMIT 1", (card["key"],)).fetchone()
                if q and q["detail"]:
                    d = json.loads(q["detail"])
                    at = d.get("retry_at")
                    err = (f"⏸ {d.get('engine', '')} 토큰 소진으로 대기열 복귀"
                           + (f" · {at} 이후 재시도" if at else ""))
            out.append({
                "id": card["id"], "kind": card["kind"], "status": card["status"],
                "engine": card["engine"] or "claude",
                "repo": card["repo"], "pr": card["pr_number"],
                "head": (card["head_sha"] or "")[:10], "blocked": card["blocked"],
                "title": meta.get("title", ""), "url": meta.get("url", ""),
                "author": meta.get("author", ""),
                "findings": findings, "comments": comments,
                "dryrun_pending": dryrun_pending,
                "feedback": feedback.latest_for_card(c, card["id"]),
                "closure": {"resolved": clo.get("resolved", 0),
                            "dismissed": clo.get("dismissed", 0),
                            "deferred": clo.get("deferred", 0),
                            "unresolved": clo.get("unresolved", 0),
                            "pending": clo.get("dismiss_pending", 0) + clo.get("defer_pending", 0)},
                "error": err,
            })
        return out


ACTIVE_REVIEW = ("intake", "reviewing", "verifying", "commenting")


def do_action(action, card_id, engine="claude"):
    if engine not in ("claude", "codex"):
        engine = "claude"
    kick = False
    stop_target = None
    with db.connect() as c:
        card = c.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not card:
            return False
        if action == "start" and card["kind"] == "review" and card["status"] == "triage":
            if not engines.is_ready(engine):  # 로그인/설치 안 된 엔진으로 시작 차단
                db.log_event(c, "operator_start_blocked", card["key"], {"engine": engine})
                return False
            db.set_engine(c, card["id"], engine)
            db.set_status(c, card["id"], "intake")
            db.log_event(c, "operator_start", card["key"], {"engine": engine})
            kick = True
        elif action == "ignore":
            db.set_status(c, card["id"], "archived")
            db.log_event(c, "operator_ignore", card["key"])
        elif action == "retry" and card["status"] == "failed":
            db.set_status(c, card["id"], "intake")
            db.log_event(c, "operator_retry", card["key"], {"engine": card["engine"]})
            kick = True
        elif action == "unblock" and card["kind"] == "approve":
            db.set_status(c, card["id"], "approving", blocked=0)
            db.log_event(c, "operator_unblock", card["key"])
            kick = True
        elif action == "publish_dryrun" and card["kind"] == "review":
            if not commenter.publish_dryrun(c, card):
                return False
        elif action == "rereview":
            chosen_engine = card["engine"] if card["kind"] == "review" else None
            if not chosen_engine:
                prior = c.execute(
                    """SELECT engine FROM cards
                       WHERE repo=? AND pr_number=? AND head_sha=? AND kind='review'
                       ORDER BY id DESC LIMIT 1""",
                    (card["repo"], card["pr_number"], card["head_sha"]),
                ).fetchone()
                chosen_engine = prior["engine"] if prior and prior["engine"] else engines.default_engine()
            if not engines.is_ready(chosen_engine):
                db.log_event(c, "operator_rereview_blocked", card["key"],
                             {"engine": chosen_engine})
                return False
            if not router.create_rereview(c, card["id"], chosen_engine):
                return False
            kick = True
        elif action == "stop" and card["status"] in ACTIVE_REVIEW:
            db.set_status(c, card["id"], "archived")  # terminal → 워커가 되살리지 않음
            db.log_event(c, "review_stopped", card["key"], {"from": card["status"]})
            stop_target = (card["repo"], card["pr_number"])
        else:
            return False
    if stop_target:
        worktree.kill_review_process(*stop_target)  # 진행 중 LLM 프로세스 강제 종료
    if kick:
        kick_tick()
    return True


def do_finding_action(action, finding_id):
    if action not in {"accept_author_decision", "operator_dismiss"}:
        return False
    kick = False
    with db.connect() as c:
        finding = c.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        allowed = ({"dismiss_pending", "defer_pending"} if action == "accept_author_decision"
                   else {"posted", "confirmed", "unresolved", "dismiss_pending", "defer_pending"})
        if not finding or finding["status"] not in allowed:
            return False
        card = c.execute("SELECT * FROM cards WHERE id=?", (finding["card_id"],)).fetchone()
        if not card or card["kind"] != "review" or card["status"] != "commented":
            return False
        accepted = ("deferred" if action == "accept_author_decision"
                    and finding["status"] == "defer_pending" else "dismissed")
        if action == "operator_dismiss":
            db.set_finding_decision(c, finding_id, accepted, card["head_sha"], "",
                                    "operator override")
        else:
            db.set_finding_status(c, finding_id, accepted)
        db.log_event(c, "operator_author_decision", card["key"],
                     {"finding_id": finding_id, "status": accepted,
                      "comment_id": finding["decision_comment_id"],
                      "manual_override": action == "operator_dismiss"})
        blockers = c.execute(
            """SELECT COUNT(*) n FROM findings WHERE repo=? AND pr_number=?
               AND status IN ('posted','confirmed','unresolved','pending_verify',
                              'dismiss_pending','defer_pending')""",
            (finding["repo"], finding["pr_number"]),
        ).fetchone()["n"]
        if not blockers:
            info = ghclient.pr_view(finding["repo"], finding["pr_number"])
            if (info.get("state") == "OPEN" and not info.get("isDraft")
                    and info.get("headRefOid") == card["head_sha"]):
                db.set_status(c, card["id"], profiles.policy_from_card(card)["no_finding_terminal"])
                kick = True
    if kick:
        kick_tick()
    return True


def refresh_poll():
    """Run the poller now (bypass the interval) — pull new PRs/heads into triage."""
    with db.connect() as c:
        before = len(db.cards_in(c, ["triage"]))
        poller.poll(c)
        after = len(db.cards_in(c, ["triage"]))
    return {"added": max(0, after - before), "total": after}


def mutation_allowed(client_ip: str, action_header: str, origin: str, host: str) -> bool:
    """Writes are local-operator only; custom header blocks browser CSRF."""
    try:
        ip = ipaddress.ip_address(client_ip)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
    except ValueError:
        return False
    if not any(ip in network for network in WRITE_NETWORKS) or action_header != "1":
        return False
    return not origin or urlparse(origin).netloc == host


def build_mentions():
    with db.connect() as c:
        rows = db.list_mentions(c)
        return [{
            "id": r["id"],
            "channel": r["channel_name"] or r["channel_id"],
            "user": r["user_name"] or r["user_id"],
            "text": r["text"], "ts": r["ts"],
            "permalink": r["permalink"], "status": r["status"],
        } for r in rows]


def _feedback_row(r, include_private=False):
    reactions = json.loads(r["reactions"]) if r["reactions"] else {}
    replies = json.loads(r["author_replies"]) if r["author_replies"] else []
    outcome = json.loads(r["outcome"]) if r["outcome"] else {}
    payload = json.loads(r["payload"]) if r["payload"] else {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    out = {
        "id": r["id"], "repo": r["repo"], "pr": r["pr_number"],
        "card_id": r["card_id"], "profile": r["profile_type"],
        "snapshot_type": r["snapshot_type"], "status": r["status"] or "archived",
        "title": payload.get("title", ""), "comment_url": r["comment_url"],
        "created_at": r["created_at"],
        "reactions": {
            "+1": int(reactions.get("+1") or 0),
            "-1": int(reactions.get("-1") or 0),
            "confused": int(reactions.get("confused") or 0),
            "total_count": int(reactions.get("total_count") or 0),
        },
        "up": int(reactions.get("+1") or 0),
        "down": int(reactions.get("-1") or 0),
        "confused": int(reactions.get("confused") or 0),
        "replies": len(replies),
        "needs_inspection": bool(int(reactions.get("-1") or 0) or int(reactions.get("confused") or 0) or replies),
    }
    if include_private:
        out["author_replies"] = [
            {k: reply.get(k, "") for k in ("id", "url", "created_at")}
            for reply in replies
        ]
        out["outcome"] = outcome
    return out


def _feedback_filters(params):
    where, vals = [], []
    def first(name, default=""):
        return (params.get(name) or [default])[0]
    for col, name in (("s.repo", "repo"), ("s.profile_type", "profile"), ("s.snapshot_type", "snapshot_type")):
        val = first(name).strip()
        if val:
            where.append(f"{col}=?")
            vals.append(val)
    pr = first("pr").strip()
    if pr:
        where.append("s.pr_number=?")
        vals.append(int(pr))
    card_id = first("card_id").strip()
    if card_id:
        where.append("s.card_id=?")
        vals.append(int(card_id))
    needs = first("needs_inspection").strip().lower()
    if needs in ("1", "true", "yes"):
        where.append("(json_extract(s.reactions, '$.\"-1\"') > 0 OR json_extract(s.reactions, '$.confused') > 0 OR json_array_length(s.author_replies) > 0)")
    limit = max(1, min(int(first("limit", "25") or 25), 500))
    return where, vals, limit


def build_feedback(params=None, include_private=False):
    params = params or {}
    where, vals, limit = _feedback_filters(params)
    clause = "WHERE " + " AND ".join(where) if where else ""
    with db.connect() as c:
        rows = c.execute(
            f"""SELECT s.*, c.payload, c.status
               FROM review_feedback_snapshots s
               LEFT JOIN cards c ON c.id=s.card_id
               {clause}
               ORDER BY s.created_at DESC, s.id DESC
               LIMIT ?""",
            (*vals, limit),
        ).fetchall()
        return [_feedback_row(r, include_private=include_private) for r in rows]


def build_feedback_detail(snapshot_id):
    with db.connect() as c:
        row = c.execute(
            """SELECT s.*, c.payload, c.status
               FROM review_feedback_snapshots s
               LEFT JOIN cards c ON c.id=s.card_id
               WHERE s.id=?""",
            (int(snapshot_id),),
        ).fetchone()
        return _feedback_row(row, include_private=True) if row else None


def build_feedback_csv(params=None):
    rows = build_feedback(params or {}, include_private=False)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[
        "id", "repo", "pr", "card_id", "profile", "snapshot_type", "status",
        "title", "comment_url", "created_at", "up", "down", "confused",
        "replies", "needs_inspection",
    ])
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _csv_cell(row.get(k, "")) for k in writer.fieldnames})
    return out.getvalue()


def _csv_cell(value):
    if not isinstance(value, str):
        return value
    stripped = value.lstrip(" \t\r\n")
    if stripped[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def do_mention_action(action, mention_id):
    if action not in ("read", "archive"):
        return False
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM mentions WHERE id=?", (mention_id,)).fetchone():
            return False
        db.set_mention_status(c, mention_id, "read" if action == "read" else "archived")
    return True


HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lookout</title>
<script>(function(){try{var p=localStorage.getItem('lookout_theme')||'auto';
var t=p==='auto'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):p;
document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>
<style>
:root{--bg:#11151c;--panel:#1b212b;--panel2:#232b38;--line:#333d4d;--ink:#f1f5fb;
--muted:#9fabbe;--dim:#6b7688;--accent:#2dd4bf;--purple:#a78bfa;--good:#4ade80;--warn:#fbbf24;--bad:#fb7185;
--header-bg:rgba(17,21,28,.86);--shadow:rgba(0,0,0,.28);
--btn-accent-bg:#2dd4bf;--btn-accent-fg:#06101f;--btn-accent-bd:#2dd4bf;
--btn-purple-bg:#a78bfa;--btn-purple-fg:#0a0612;--btn-purple-bd:#a78bfa;}
:root[data-theme="light"]{--bg:#eceef3;--panel:#f5f6f9;--panel2:#ffffff;--line:#e0e3ea;--ink:#243040;
--muted:#677488;--dim:#a3abb8;--accent:#3aa99c;--purple:#9a7ee8;--good:#56b87f;--warn:#d99a3e;--bad:#ec7a8f;
--header-bg:rgba(245,246,249,.9);--shadow:rgba(40,55,80,.08);
--btn-accent-bg:#d6f2ed;--btn-accent-fg:#137a6d;--btn-accent-bd:#aee3da;
--btn-purple-bg:#ebe3fc;--btn-purple-fg:#6f4cc4;--btn-purple-bd:#d6c8f5;}
:root[data-theme="light"] .card{box-shadow:0 1px 2px var(--shadow)}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-size:14px;line-height:1.5;
font-family:-apple-system,BlinkMacSystemFont,"Pretendard",Roboto,sans-serif;-webkit-font-smoothing:antialiased;
display:flex;flex-direction:column;overflow:hidden}
header{position:sticky;top:0;z-index:20;padding:13px 22px;display:flex;align-items:center;gap:12px;
background:var(--header-bg);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0;font-weight:750;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:12.5px}
.board{flex:1 1 auto;min-height:0;display:flex;gap:12px;padding:18px;overflow-x:auto;overflow-y:hidden;align-items:stretch}
.board.stack{display:block;overflow-y:auto;overflow-x:hidden}
.toggle{display:flex;gap:6px;margin-left:6px}
.toggle button.active{background:var(--btn-accent-bg);color:var(--btn-accent-fg);border-color:var(--btn-accent-bd);font-weight:650}
.sec{margin:0 0 16px}
.sec h2{font-size:14px;margin:0 0 10px;padding-bottom:7px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
.sec .cards{display:flex;flex-direction:row;flex-wrap:wrap;gap:10px;padding:0}
.sec .card{width:262px}
.statuspill{font-size:11px;font-weight:650;padding:2px 9px;border-radius:20px;white-space:nowrap}
.col{background:var(--panel);border:1px solid var(--line);border-radius:14px;min-width:272px;max-width:300px;flex:0 0 auto;display:flex;flex-direction:column;max-height:100%}
.col h2{flex:0 0 auto;font-size:12.5px;font-weight:700;margin:0;padding:13px 15px;border-bottom:1px solid var(--line);color:var(--ink);display:flex;justify-content:space-between;align-items:center;gap:8px}
.col h2 .lh{display:flex;align-items:center;gap:8px}
.col .n{background:var(--panel2);border-radius:20px;padding:1px 9px;color:var(--muted);font-size:11.5px}
.cards{padding:11px;display:flex;flex-direction:column;gap:11px;min-height:30px}
.col .cards{flex:1 1 auto;overflow-y:auto;min-height:0}
.card{position:relative;background:var(--panel2);border:1px solid var(--line);border-left:4px solid var(--dim);border-radius:11px;padding:13px;cursor:pointer;transition:border-color .14s,transform .08s,box-shadow .14s}
.card:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:0 6px 18px var(--shadow)}
.pr{font-size:13px;font-weight:700;color:var(--ink);display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:7px}
.pr .num{font-size:14.5px}
.pr a{color:var(--ink);text-decoration:none}
.title{font-size:13px;color:var(--ink);opacity:.92;margin-bottom:9px;line-height:1.45;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.high{background:var(--bad)}.medium{background:var(--warn)}.low{background:var(--accent)}
.btns{display:flex;gap:7px;margin-top:11px}
.rev{display:flex;gap:7px;margin-top:11px}
.rev button{flex:1}
.rev button:disabled{background:var(--panel);border-color:var(--line);color:var(--dim);filter:none;cursor:not-allowed}
.engnote{margin-top:11px;font-size:11.5px;color:var(--warn);line-height:1.45;
  background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.32);border-radius:9px;padding:8px 10px}
.engnote code{background:var(--bg);padding:1px 5px;border-radius:5px;color:var(--warn)}
button{font:inherit;font-size:12.5px;font-weight:600;border:1px solid var(--line);background:var(--panel2);
color:var(--ink);border-radius:9px;padding:7px 12px;cursor:pointer;transition:filter .12s,transform .05s}
button:hover{filter:brightness(1.13)}
button:active{transform:translateY(1px)}
button:disabled{opacity:.6;cursor:default}
button.go{background:var(--btn-accent-bg);border-color:var(--btn-accent-bd);color:var(--btn-accent-fg);font-weight:700}
button.claude{background:var(--btn-accent-bg);border-color:var(--btn-accent-bd);color:var(--btn-accent-fg);font-weight:700}
button.codex{background:var(--btn-purple-bg);border-color:var(--btn-purple-bd);color:var(--btn-purple-fg);font-weight:700}
button.stop{background:transparent;border-color:var(--bad);color:var(--bad)}
button.stop:hover{background:var(--bad);color:#1a0608}
.filterbar{display:flex;gap:7px;flex-wrap:wrap;padding:11px 22px;border-bottom:1px solid var(--line);
  position:sticky;top:51px;z-index:15;background:var(--header-bg);backdrop-filter:blur(8px)}
.chip{font-size:12px;font-weight:600;border:1px solid var(--line);background:var(--panel2);color:var(--muted);
  border-radius:20px;padding:5px 12px;cursor:pointer;display:flex;align-items:center;gap:6px}
.chip:hover{filter:brightness(1.12)}
.chip.on{background:var(--btn-accent-bg);color:var(--btn-accent-fg);border-color:var(--btn-accent-bd)}
.chip b{font-weight:700}
.rdot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:0 0 auto}
.repopill{font-size:10.5px;font-weight:650;padding:2px 8px;border-radius:20px;border:1px solid var(--line);
  display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.toggle{margin-left:4px;border:1px solid var(--line);border-radius:9px;overflow:hidden;gap:0}
.toggle button{border:none;border-radius:0;background:transparent;color:var(--muted);padding:7px 13px}
.toggle button.active{background:var(--btn-accent-bg);color:var(--btn-accent-fg);font-weight:700}
/* ignore: small, muted, corner — hard to hit by accident, asks to confirm */
.xbtn{position:absolute;top:7px;right:7px;font-size:11px;line-height:1;color:var(--muted);
background:transparent;border:none;padding:3px 5px;border-radius:6px;opacity:.4}
.xbtn:hover{opacity:1;color:var(--bad);background:var(--panel)}
.empty{color:var(--muted);font-size:11px;text-align:center;padding:8px 0}
/* modal */
.ov{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:24px}
.ov.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;max-width:720px;width:100%;max-height:86vh;overflow:auto;padding:22px}
.modal h3{margin:0 0 6px;font-size:18px}
.finding{border:1px solid var(--line);border-left:4px solid var(--dim);border-radius:11px;padding:14px;margin:12px 0;background:var(--panel2)}
.finding .ft{font-weight:700;font-size:14px;margin-bottom:7px;color:var(--ink)}
.finding .meta{font-size:12px;color:var(--muted);margin-bottom:9px;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.lbl{font-size:11.5px;font-weight:700;color:var(--accent);margin-top:14px;margin-bottom:4px;letter-spacing:.03em;text-transform:uppercase}
.cmt{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:13px;white-space:pre-wrap;font-size:12.5px;line-height:1.5;margin-top:8px}
.close{float:right;cursor:pointer;color:var(--muted);font-weight:600}
.close:hover{color:var(--ink)}
.modal code{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:var(--bg);padding:1px 6px;border-radius:5px;color:var(--accent)}
.msub{color:var(--muted);font-size:12.5px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.msub a{color:var(--accent);word-break:break-all}
.mlink{margin:10px 0}.mlink a{color:var(--accent);font-weight:600}
.pre{white-space:pre-wrap;word-break:break-word;font-size:13.5px;line-height:1.65;color:var(--ink)}
.lbl2{font-size:11.5px;font-weight:700;color:var(--muted);margin-top:12px;margin-bottom:3px}
.sevtag{font-size:11px;font-weight:700;padding:1px 8px;border-radius:20px;text-transform:uppercase}
.fstatus{margin-left:auto;font-size:11px;color:var(--dim)}
.errline{margin-top:6px;font-size:11px;line-height:1.35;color:#fb7185;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;word-break:break-all}
.errline.warn{color:#fbbf24}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(12px);
  background:var(--panel2);color:var(--ink);border:1px solid var(--accent);
  padding:11px 18px;border-radius:12px;font-size:13.5px;font-weight:600;
  box-shadow:0 8px 30px rgba(0,0,0,.45);opacity:0;pointer-events:none;z-index:100;
  display:flex;align-items:center;gap:9px;transition:opacity .18s,transform .18s}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.spin{width:13px;height:13px;border:2px solid var(--accent);border-top-color:transparent;
  border-radius:50%;display:inline-block;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.pill{font-size:11px;padding:2px 8px;border-radius:20px;background:var(--panel);border:1px solid var(--line);color:var(--muted);white-space:nowrap}
/* mentions */
.mentions{padding:14px 18px 0}
.mentions h2{font-size:14px;margin:0 0 10px;display:flex;gap:8px;align-items:center}
.mlist{display:flex;flex-direction:column;gap:8px}
.m{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px;display:flex;gap:12px;align-items:flex-start}
.m.read{opacity:.55}
.m .body{flex:1;min-width:0}
.m .meta{font-size:11px;color:var(--muted);margin-bottom:4px}
.m .meta b{color:var(--ink)}
.m .txt{font-size:13px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.m .acts{display:flex;gap:6px;flex:0 0 auto}
.m a.open{text-decoration:none}
.mempty{color:var(--muted);font-size:12px;padding:4px 0 12px}
.unreaddot{width:8px;height:8px;border-radius:50%;background:var(--accent);flex:0 0 auto;margin-top:5px}
</style></head><body>
<header><h1>👁 Lookout</h1>
<div class="toggle"><button id="tLane" class="active" onclick="setView('lane')">레인별</button><button id="tAuthor" onclick="setView('author')">사람별</button><button id="tFeedback" onclick="setView('feedback')">리뷰 피드백</button></div>
<button id="refreshBtn" onclick="refresh()">🔄 PR 가져오기</button>
<span class="sub" id="sub">로딩…</span>
<span class="sub" id="engStat" style="margin-left:14px"></span>
<button id="themeBtn" onclick="cycleTheme()" title="테마 전환 (시스템 · 라이트 · 다크)" style="margin-left:auto">🖥 시스템</button>
<span class="sub" style="margin-left:12px">5초마다 자동 새로고침</span></header>
<div class="filterbar" id="filterbar"></div>
<section class="mentions" id="mentions" style="display:none"></section>
<div class="board" id="board"></div>
<div class="ov" id="ov"><div class="modal" id="modal"></div></div>
<script>
const LANES=__LANES__;
// Slack 미연동 — 멘션 섹션 숨김. Slack 연결 시 true 로 바꾸면 부활.
const SHOW_MENTIONS=false;
// ── 테마 (시스템/라이트/다크) — 클릭 순환, localStorage 저장 ──
const THEME_KEY='lookout_theme';
const THEME_ORDER=['auto','light','dark'];
const THEME_LABEL={auto:'🖥 시스템',light:'☀️ 라이트',dark:'🌙 다크'};
function themePref(){return localStorage.getItem(THEME_KEY)||'auto';}
function resolveTheme(p){return p==='auto'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):p;}
function isLight(){return resolveTheme(themePref())==='light';}
function renderThemeBtn(){const b=document.getElementById('themeBtn');if(b)b.textContent=THEME_LABEL[themePref()];}
function applyTheme(){document.documentElement.setAttribute('data-theme',resolveTheme(themePref()));renderThemeBtn();}
function repaintBoard(){try{render();}catch(e){}}
function cycleTheme(){const o=THEME_ORDER;localStorage.setItem(THEME_KEY,o[(o.indexOf(themePref())+1)%o.length]);applyTheme();repaintBoard();}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{if(themePref()==='auto'){applyTheme();repaintBoard();}});
// 색 hex를 비율만큼 어둡게 (라이트모드에서 연한 pill 글자색을 진하게)
function darken(hex,f){const h=hex.replace('#','');const n=parseInt(h.length===3?h.split('').map(x=>x+x).join(''):h,16);
  return '#'+[(n>>16)&255,(n>>8)&255,n&255].map(x=>Math.round(x*f).toString(16).padStart(2,'0')).join('');}
// 상태/심각도/repo 색 pill 인라인 스타일 — 라이트모드는 글자색을 어둡게
function pill(c){return isLight()
  ? `background:${c}22;color:${darken(c,.5)};border:1px solid ${c}66`
  : `background:${c}22;color:${c};border:1px solid ${c}55`;}
function stripe(c){return isLight()?darken(c,.72):c;}  // 카드/finding 좌측 컬러 스트라이프
applyTheme();
let DATA=[];let FEEDBACK=[];let VIEW='lane';let REPO='all';
let LANE_SCROLL={};
// 엔진 가용성 — 초기엔 낙관적(true)으로 두고 /api/engines 응답으로 갱신
let ENGINES={claude:{installed:true,logged_in:true,ready:true},codex:{installed:true,logged_in:true,ready:true}};
function engReady(e){return !!(ENGINES&&ENGINES[e]&&ENGINES[e].ready);}
function engReason(e){const s=ENGINES&&ENGINES[e];
  if(!s)return '상태 확인 중';
  if(!s.installed)return e+' CLI 미설치';
  if(!s.logged_in)return e+' 로그인 필요';
  return '';}
const STATUS_META={
  triage:{c:'#2dd4bf',ko:'대기'}, intake:{c:'#6b7688',ko:'시작됨'},
  reviewing:{c:'#fbbf24',ko:'리뷰중'}, verifying:{c:'#fbbf24',ko:'검증중'},
  commenting:{c:'#fbbf24',ko:'댓글작성'}, commented:{c:'#4ade80',ko:'댓글완료'},
  lgtm:{c:'#4ade80',ko:'LGTM'}, approve_blocked:{c:'#a78bfa',ko:'승인대기'},
  approving:{c:'#a78bfa',ko:'승인중'}, done:{c:'#6b7688',ko:'완료'},
  failed:{c:'#fb7185',ko:'실패'}};
function smeta(s){return STATUS_META[s]||{c:'#6b7688',ko:s};}
function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
function repoShort(r){return (r||'').split('/')[1]||r;}
const REPO_COLORS=['#2dd4bf','#a78bfa','#fbbf24','#60a5fa','#4ade80','#fb7185'];
function repoColor(r){let h=0;for(const ch of (r||''))h=(h*31+ch.charCodeAt(0))>>>0;return REPO_COLORS[h%REPO_COLORS.length];}
function setRepo(r){REPO=r;renderFilter();render();}
function viewData(){return REPO==='all'?DATA:DATA.filter(c=>c.repo===REPO);}
function viewFeedbackData(){return REPO==='all'?FEEDBACK:FEEDBACK.filter(f=>f.repo===REPO);}
function filterSource(){return VIEW==='feedback'?FEEDBACK:DATA;}
function normalizeRepo(){const src=filterSource();if(REPO!=='all'&&!src.some(x=>x.repo===REPO))REPO='all';}
function renderFilter(){
  normalizeRepo();
  const src=filterSource();
  const repos=[...new Set(src.map(c=>c.repo))].sort();
  const bar=document.getElementById('filterbar');
  let h=`<button class="chip ${REPO==='all'?'on':''}" onclick="setRepo('all')">전체 <b>${src.length}</b></button>`;
  repos.forEach(r=>{const n=src.filter(c=>c.repo===r).length;const col=repoColor(r);
    const on=REPO===r;
    const onStyle=on?(isLight()?`background:${col}2e;color:${darken(col,.5)};border-color:${col}66`:`background:${col};color:#06101f;border-color:${col}`):'';
    h+=`<button class="chip ${on?'on':''}" style="${onStyle}" onclick="setRepo('${r}')"><span class="rdot" style="background:${col}"></span>${esc(repoShort(r))} <b>${n}</b></button>`;});
  bar.innerHTML=h;
}
function setView(v){VIEW=v;
  document.getElementById('tLane').classList.toggle('active',v==='lane');
  document.getElementById('tAuthor').classList.toggle('active',v==='author');
  document.getElementById('tFeedback').classList.toggle('active',v==='feedback');
  renderFilter();render();}
async function load(){
  const [rb,re,rf]=await Promise.all([fetch('/api/board'),fetch('/api/engines'),fetch('/api/feedback')]);
  DATA=await rb.json();
  try{FEEDBACK=await rf.json();}catch(e){FEEDBACK=[];}
  try{ENGINES=await re.json();}catch(e){}
  document.getElementById('sub').textContent=DATA.length+'개 카드';
  renderEngStat();
  renderFilter();
  render();
  if(SHOW_MENTIONS)loadMentions();
}
function renderEngStat(){
  const el=document.getElementById('engStat');if(!el)return;
  const parts=[['claude','Claude'],['codex','Codex']].map(([e,n])=>{
    const r=engReady(e);
    return `<span title="${r?(n+' 사용 가능'):engReason(e)}" style="color:${r?'var(--good)':'var(--dim)'}">${n} ${r?'✓':'✗'}</span>`;
  });
  el.innerHTML='⚙️ '+parts.join(' · ');
}
function reviewButtons(id){
  const defs=[['claude','리뷰 (Claude)'],['codex','리뷰 (Codex)']];
  if(!defs.some(([e])=>engReady(e)))
    return `<div class="engnote">⚠️ 리뷰 엔진 미설정 — <code>claude</code> 또는 <code>codex</code> CLI 로그인이 필요합니다.</div>`;
  let h='<div class="rev">';
  defs.forEach(([e,label])=>{
    h+= engReady(e)
      ? `<button class="${e}" onclick="act(event,'start',${id},'${e}')">${label}</button>`
      : `<button class="${e}" disabled title="${engReason(e)}">${label}</button>`;
  });
  return h+'</div>';
}
async function loadMentions(){
  const r=await fetch('/api/mentions');const M=await r.json();
  const wrap=document.getElementById('mentions');wrap.style.display='';
  const unread=M.filter(m=>m.status==='unread').length;
  let h=`<h2>📢 멘션 / 확인요청 ${unread?`<span class="pill">안읽음 ${unread}</span>`:''}</h2>`;
  if(!M.length){wrap.innerHTML=h+'<div class="mempty">아직 멘션 없음</div>';return;}
  h+='<div class="mlist">';
  M.forEach(m=>{
    const dot=m.status==='unread'?'<span class="unreaddot"></span>':'<span style="width:8px;flex:0 0 auto"></span>';
    const open=m.permalink?`<a class="open" href="${m.permalink}" target="_blank"><button>열기↗</button></a>`:'';
    h+=`<div class="m ${m.status==='unread'?'':'read'}">${dot}
      <div class="body"><div class="meta"><b>@${esc(m.user)}</b> · ${esc(m.channel)}</div>
      <div class="txt">${esc(m.text)}</div></div>
      <div class="acts">${open}
      ${m.status==='unread'?`<button onclick="mAct(${m.id},'read')">읽음</button>`:''}
      <button onclick="mAct(${m.id},'archive')">✕</button></div></div>`;
  });
  wrap.innerHTML=h+'</div>';
}
async function mAct(id,action){
  await fetch('/api/mention-action',{method:'POST',headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},body:JSON.stringify({action,mention_id:id})});
  loadMentions();
}
function render(){VIEW==='feedback'?renderFeedback():VIEW==='author'?renderByAuthor():renderLanes();}
function renderFeedback(){
  const list=viewFeedbackData();
  const board=document.getElementById('board');board.className='board stack';board.innerHTML='';
  const sec=document.createElement('div');sec.className='sec';
  const inspect=list.filter(f=>f.needs_inspection).length;
  sec.innerHTML=`<h2>🧭 리뷰 피드백 <span class="n">${list.length}</span>${inspect?`<span class="pill">확인 ${inspect}</span>`:''}</h2>`;
  const cc=document.createElement('div');cc.className='mlist';
  if(!list.length)cc.innerHTML='<div class="empty">아직 수집된 피드백 없음</div>';
  list.forEach(f=>cc.appendChild(feedbackItem(f)));
  sec.appendChild(cc);board.appendChild(sec);
}
function feedbackItem(f){
  const el=document.createElement('div');el.className=`m ${f.needs_inspection?'':'read'}`;
  const rc=repoColor(f.repo);const open=f.comment_url?`<a class="open" href="${f.comment_url}" target="_blank" onclick="event.stopPropagation()"><button>댓글↗</button></a>`:'';
  el.innerHTML=`<span class="rdot" style="background:${rc};margin-top:5px"></span>
    <div class="body"><div class="meta"><b>${esc(repoShort(f.repo))}#${f.pr}</b> · ${esc(f.profile)} · ${esc(f.snapshot_type)} · ${esc(f.status)}</div>
    <div class="txt">${esc(f.title||'(제목없음)')}</div>
    <div class="meta">👍 ${f.up||0} · 👎 ${f.down||0} · 😕 ${f.confused||0} · 💬 ${f.replies||0}</div></div>
    <div class="acts">${open}</div>`;
  el.onclick=()=>openFeedbackModal(f);
  return el;
}
function renderLanes(){
  const board=document.getElementById('board');
  LANE_SCROLL={left:board.scrollLeft};
  board.querySelectorAll('.col .cards').forEach(cards=>LANE_SCROLL[cards.dataset.lane]=cards.scrollTop);
  const byLane={};LANES.forEach(([k])=>byLane[k]=[]);
  viewData().forEach(c=>{if(byLane[c.status])byLane[c.status].push(c)});
  board.className='board';board.innerHTML='';
  for(const [key,label] of LANES){
    const list=byLane[key]||[];
    const col=document.createElement('div');col.className='col';
    col.innerHTML=`<h2><span class="lh"><span class="dot" style="background:${smeta(key).c}"></span>${label}</span><span class="n">${list.length}</span></h2>`;
    const cc=document.createElement('div');cc.className='cards';cc.dataset.lane=key;
    if(!list.length)cc.innerHTML='<div class="empty">—</div>';
    list.forEach(c=>cc.appendChild(tile(c)));
    col.appendChild(cc);board.appendChild(col);
  }
  board.scrollLeft=LANE_SCROLL.left||0;
  board.querySelectorAll('.col .cards').forEach(cards=>cards.scrollTop=LANE_SCROLL[cards.dataset.lane]||0);
}
function renderByAuthor(){
  const byA={};viewData().forEach(c=>{(byA[c.author||'(unknown)']=byA[c.author||'(unknown)']||[]).push(c)});
  const board=document.getElementById('board');board.className='board stack';board.innerHTML='';
  Object.keys(byA).sort((a,b)=>byA[b].length-byA[a].length).forEach(author=>{
    const list=byA[author];
    const sec=document.createElement('div');sec.className='sec';
    sec.innerHTML=`<h2>👤 ${esc(author)} <span class="n">${list.length}</span></h2>`;
    const cc=document.createElement('div');cc.className='cards';
    list.forEach(c=>cc.appendChild(tile(c)));
    sec.appendChild(cc);board.appendChild(sec);
  });
}
function tile(c){
  const el=document.createElement('div');el.className='card';
  const dots=c.findings.map(f=>`<span class="dot ${f.severity||'low'}"></span>`).join('');
  let btns='', xbtn='';
  if(c.status==='triage'){
    btns=reviewButtons(c.id);
    xbtn=`<button class="xbtn" title="목록에서 제외" onclick="ignoreCard(event,${c.id})">✕</button>`;
  }
  if(c.status==='failed'){
    btns=`<div class="btns"><button class="go" onclick="act(event,'retry',${c.id})">↻ 재시도</button></div>`;
    xbtn=`<button class="xbtn" title="목록에서 제외" onclick="ignoreCard(event,${c.id})">✕</button>`;
  }
  if(c.status==='approve_blocked')btns=`<div class="btns"><button class="go" onclick="act(event,'unblock',${c.id})">🔓 승인(Unblock)</button></div>`;
  if((c.kind==='review'&&['commented','lgtm','done'].includes(c.status))||(c.kind==='approve'&&['approve_blocked','done'].includes(c.status)))
    btns+=`<div class="btns"><button class="go" onclick="reReview(event,${c.id})">🔄 재리뷰</button></div>`;
  if(['intake','reviewing','verifying','commenting'].includes(c.status))
    btns=`<div class="btns"><button class="stop" onclick="stopReview(event,${c.id})">🛑 리뷰 중지</button></div>`;
  if(c.dryrun_pending)
    btns+=`<div class="btns"><button class="go" onclick="publishDryRun(event,${c.id})">💬 dry-run 댓글 게시</button></div>`;
  const sm=smeta(c.status);
  el.style.borderLeftColor=stripe(sm.c);
  const statusPill=`<span class="statuspill" style="${pill(sm.c)}">${sm.ko}</span>`;
  const enginePill=(c.status!=='triage')?`<span class="pill">${c.engine}</span>`:'';
  const clo=c.closure&&(c.closure.resolved||c.closure.dismissed||c.closure.deferred||c.closure.unresolved||c.closure.pending)?`<span class="pill">✅${c.closure.resolved} ↪️${c.closure.deferred||0} ⚠️${c.closure.unresolved} 🧑‍⚖️${c.closure.pending||0}</span>`:'';
  const fb=c.feedback&&(c.feedback.up||c.feedback.down||c.feedback.confused||c.feedback.replies)
    ?`<span class="pill" title="리뷰 피드백 스냅샷">👍${c.feedback.up||0} 👎${c.feedback.down||0} 💬${c.feedback.replies||0}</span>`:'';
  const inspect=c.feedback&&c.feedback.needs_inspection?`<span class="pill" style="${pill('#fbbf24')}">피드백 확인</span>`:'';
  const rc=repoColor(c.repo);
  const repoPill=`<span class="repopill" style="${pill(rc)}"><span class="rdot" style="background:${rc}"></span>${esc(repoShort(c.repo))}</span>`;
  el.innerHTML=`${xbtn}<div class="pr">${repoPill} <span class="num">#${c.pr}</span></div>
    <div class="title">${esc(c.title)||'(제목없음)'}</div>
    <div class="row">${statusPill}<span class="pill">${esc(c.author)}</span>${enginePill}${inspect}</div>
    <div class="row"><span>@${c.head}</span>${dots?`<span class="row">${dots} ${c.findings.length}건</span>`:''}${clo}${fb}</div>
    ${c.error?`<div class="errline ${c.status==='triage'?'warn':''}" title="${esc(c.error)}">${esc(c.error)}</div>`:''}${btns}`;
  el.onclick=()=>openModal(c);
  return el;
}
const SEVC={high:'#fb7185',medium:'#fbbf24',low:'#2dd4bf'};
function openModal(c){
  const m=document.getElementById('modal');const sm=smeta(c.status);
  let html=`<span class="close" onclick="closeM()">✕ 닫기</span>
    <h3>#${c.pr} ${esc(c.title)}</h3>
    <div class="msub">${esc(c.repo)} · @${esc(c.author)} · <code>${c.head}</code>
      <span class="statuspill" style="${pill(sm.c)}">${sm.ko}</span></div>`;
  if(c.url)html+=`<div class="mlink"><a href="${c.url}" target="_blank">GitHub에서 열기 ↗</a></div>`;
  if(c.error)html+=`<div class="lbl">실패 사유</div><div class="pre">${esc(c.error)}</div>`;
  if(c.closure&&(c.closure.resolved||c.closure.dismissed||c.closure.deferred||c.closure.unresolved||c.closure.pending))
    html+=`<div class="lbl">이전 지적 추적</div><div class="pre">✅ ${c.closure.resolved} 해결 · ⏭️ ${c.closure.dismissed||0} 해명 수용 · ↪️ ${c.closure.deferred||0} 후속 작업 · ⚠️ ${c.closure.unresolved} 미해결 · 🧑‍⚖️ ${c.closure.pending||0} 운영자 판단</div>`;
  if(c.feedback&&(c.feedback.up||c.feedback.down||c.feedback.confused||c.feedback.replies)){
    html+=`<div class="lbl">리뷰 피드백</div><div class="pre">👍 ${c.feedback.up||0} · 👎 ${c.feedback.down||0} · 😕 ${c.feedback.confused||0} · 💬 ${c.feedback.replies||0}${c.feedback.needs_inspection?' · 확인 필요':''}</div>`;
  }
  if(c.findings.length){html+=`<div class="lbl">리뷰 결과 · ${c.findings.length}건</div>`;
    c.findings.forEach(f=>{const sc=SEVC[f.severity]||'#6b7688';
      html+=`<div class="finding" style="border-left-color:${stripe(sc)}">
        <div class="ft">${esc(f.title)}</div>
        <div class="meta">
          <span class="sevtag" style="${pill(sc)}">${esc(f.severity||'?')}</span>
          <span>확신도 ${esc(f.confidence||'?')}</span><span>·</span>
          <code>${esc(f.file||'')}${f.line?(':'+esc(f.line)):''}</code>
          <span class="fstatus">${esc(f.status)}</span>
        </div>
        <div class="pre">${esc(f.problem)}</div>
        ${f.fix?`<div class="lbl2">제안</div><div class="pre">${esc(f.fix)}</div>`:''}
        ${['dismiss_pending','defer_pending'].includes(f.status)?`<div class="lbl2">작성자 결정 근거</div><div class="pre">${esc(f.decision_evidence||'')}</div>${f.status==='defer_pending'?`<div class="lbl2">후속 참조</div><div class="pre">${esc(f.decision_follow_up||'후속 참조 없음')}</div>`:''}<div class="btns"><button class="go" onclick="acceptAuthorDecision(event,${f.id},'accept_author_decision')">🧑‍⚖️ 작성자 결정 수용</button></div>`:''}
        ${f.status==='deferred'?`<div class="lbl2">작성자 결정 근거</div><div class="pre">${esc(f.decision_evidence||'')}</div><div class="lbl2">후속 참조</div><div class="pre">${esc(f.decision_follow_up||'후속 참조 없음')}</div>`:''}
        ${['posted','confirmed','unresolved'].includes(f.status)?`<div class="btns"><button class="go" onclick="acceptAuthorDecision(event,${f.id},'operator_dismiss')">🧑‍⚖️ 운영자 직접 수용</button></div>`:''}
      </div>`});
  }else html+='<p class="sub">아직 finding 없음</p>';
  if(c.comments.length){html+='<div class="lbl">게시된 / 게시될 댓글</div>';
    c.comments.forEach(cm=>{const pending=(cm.type==='comment_dryrun'&&c.dryrun_pending);
      html+=`<div class="cmt">${esc(cm.body)}</div>${cm.url?`<div class="msub"><a href="${cm.url}" target="_blank">${esc(cm.url)}</a></div>`:`<div class="msub">${pending?'(dry-run · 미게시)':'(dry-run preview)'}</div>`}`});
    if(c.dryrun_pending)
      html+=`<div class="btns"><button class="go" onclick="publishDryRun(event,${c.id})">💬 dry-run 댓글 게시</button></div>`;}
  m.innerHTML=html;document.getElementById('ov').classList.add('show');
}
function openFeedbackModal(f){
  const m=document.getElementById('modal');const rc=repoColor(f.repo);
  let html=`<span class="close" onclick="closeM()">✕ 닫기</span>
    <h3>${esc(repoShort(f.repo))}#${f.pr} 리뷰 피드백</h3>
    <div class="msub">${esc(f.repo)} · card #${f.card_id} · snapshot #${f.id}
      <span class="statuspill" style="${pill(f.needs_inspection?'#fbbf24':'#4ade80')}">${f.needs_inspection?'확인 필요':'수집됨'}</span></div>
    <div class="row"><span class="repopill" style="${pill(rc)}"><span class="rdot" style="background:${rc}"></span>${esc(f.profile)}</span><span class="pill">${esc(f.snapshot_type)}</span><span class="pill">${esc(f.status)}</span></div>
    <div class="title" style="margin-top:12px">${esc(f.title)||'(제목없음)'}</div>
    <div class="lbl">반응</div><div class="pre">👍 ${f.up||0} · 👎 ${f.down||0} · 😕 ${f.confused||0} · 💬 ${f.replies||0}</div>`;
  if(f.comment_url)html+=`<div class="mlink"><a href="${f.comment_url}" target="_blank">GitHub 댓글 열기 ↗</a></div>`;
  html+=`<div class="lbl">API</div><div class="pre">/api/feedback/${f.id}</div>`;
  m.innerHTML=html;document.getElementById('ov').classList.add('show');
}
function closeM(){document.getElementById('ov').classList.remove('show')}
document.getElementById('ov').onclick=e=>{if(e.target.id==='ov')closeM()};
const ACT_MSG={start:'리뷰 시작 — 곧 분석을 시작합니다 ⏳',rereview:'재리뷰 시작 — 곧 분석을 시작합니다 🔄',unblock:'승인 진행 중 🔓',ignore:'목록에서 제외됨',stop:'리뷰 중지됨 🛑'};
async function act(e,action,id,engine){e.stopPropagation();
  showToast(ACT_MSG[action]||'처리됨', action!=='ignore');
  let j={};
  try{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},body:JSON.stringify({action,card_id:id,engine:engine||'claude'})});j=await r.json();}catch(err){}
  if(['start','rereview'].includes(action)&&j&&j.ok===false)
    showToast(action==='rereview'?'재리뷰를 시작할 수 없습니다 — PR/head 상태를 확인하세요':'시작할 수 없습니다 — '+(engine?engReason(engine)||'엔진 상태 확인':'엔진 상태 확인'),false);
  load();}
function showToast(msg,spin){
  let t=document.getElementById('toast');
  if(!t){t=document.createElement('div');t.id='toast';t.className='toast';document.body.appendChild(t);}
  t.innerHTML=(spin?'<span class="spin"></span>':'')+msg;
  t.classList.add('show');clearTimeout(window._tt);
  window._tt=setTimeout(()=>t.classList.remove('show'),2600);
}
function stopReview(e,id){e.stopPropagation();
  if(confirm('이 리뷰를 강제 중지할까요? (진행 중인 분석을 종료하고 목록에서 제외)'))act(e,'stop',id);}
function reReview(e,id){e.stopPropagation();
  if(confirm('커밋 변경 없이 현재 head를 다시 리뷰할까요? 기존 승인 대기 게이트는 취소됩니다.'))act(e,'rereview',id);}
function ignoreCard(e,id){e.stopPropagation();
  if(confirm('이 PR을 목록에서 제외할까요? (리뷰하지 않음)'))act(e,'ignore',id);}
function publishDryRun(e,id){e.stopPropagation();
  if(confirm('dry-run 댓글을 실제 GitHub PR 댓글로 게시할까요?'))act(e,'publish_dryrun',id);}
async function acceptAuthorDecision(e,id,action){e.stopPropagation();
  if(!confirm(action==='operator_dismiss'?'이 지적을 운영자 판단으로 직접 수용할까요?':'작성자의 미반영/후속 결정을 수용할까요? 이 지적은 더 이상 LGTM을 막지 않습니다.'))return;
  const r=await fetch('/api/finding-action',{method:'POST',headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},body:JSON.stringify({action,finding_id:id})});
  const j=await r.json();showToast(j.ok?'작성자 결정 수용됨':'수용할 수 없습니다',false);closeM();load();}
async function refresh(){
  const b=document.getElementById('refreshBtn');const old=b.textContent;
  b.textContent='가져오는 중…';b.disabled=true;
  try{
    const r=await fetch('/api/refresh',{method:'POST',headers:{'X-Lookout-Action':'1'}});const j=await r.json();
    await load();
    b.textContent=j.added>0?`+${j.added}건 추가`:'최신 상태';
  }catch(e){b.textContent='실패';}
  setTimeout(()=>{b.textContent=old;b.disabled=false;},1800);
}
load();setInterval(load,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/" or path.startswith("/index"):
            html = HTML.replace("__LANES__", json.dumps(LANES, ensure_ascii=False))
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/board":
            self._send(200, json.dumps(build_board(), ensure_ascii=False))
        elif path == "/api/mentions":
            self._send(200, json.dumps(build_mentions(), ensure_ascii=False))
        elif path == "/api/feedback":
            try:
                body = json.dumps(build_feedback(params), ensure_ascii=False)
            except ValueError:
                self._send(400, "{}")
                return
            self._send(200, body)
        elif path == "/api/feedback/export.csv":
            try:
                body = build_feedback_csv(params)
            except ValueError:
                self._send(400, "{}")
                return
            self._send(200, body, "text/csv; charset=utf-8")
        elif path.startswith("/api/feedback/"):
            try:
                item = build_feedback_detail(path.rsplit("/", 1)[-1])
            except ValueError:
                item = None
            self._send(200 if item else 404, json.dumps(item or {}, ensure_ascii=False))
        elif path == "/api/engines":
            self._send(200, json.dumps(engines.availability(), ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if not mutation_allowed(
                self.client_address[0], self.headers.get("X-Lookout-Action", ""),
                self.headers.get("Origin", ""), self.headers.get("Host", "")):
            self._send(403, '{"ok":false}')
            return
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or "{}")
        if self.path == "/api/refresh":
            self._send(200, json.dumps(refresh_poll()))
            return
        if self.path == "/api/action":
            ok = do_action(data.get("action"), int(data.get("card_id", 0)),
                           data.get("engine", "claude"))
        elif self.path == "/api/finding-action":
            ok = do_finding_action(data.get("action"), int(data.get("finding_id", 0)))
        elif self.path == "/api/mention-action":
            ok = do_mention_action(data.get("action"), int(data.get("mention_id", 0)))
        else:
            self._send(404, "{}")
            return
        self._send(200, json.dumps({"ok": ok}))


def main():
    db.init()
    server = ThreadingHTTPServer((DASHBOARD_HOST, PORT), Handler)
    print(f"[dashboard] http://{DASHBOARD_HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
