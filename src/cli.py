"""Operator CLI.

  python -m src.cli status            board summary
  python -m src.cli list [status]     list cards (optionally by lane)
  python -m src.cli findings <card>   findings for a review card
  python -m src.cli logs [n]          recent events
  python -m src.cli start <card> [claude|codex]   start review on a triaged PR
  python -m src.cli ignore <card>     dismiss a triaged PR from the list
  python -m src.cli unblock <card>    approve a blocked approve-gate card
  python -m src.cli tick              run one maintenance tick now
"""
import json
import sys

from . import db, tick


def _fmt_ts(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def cmd_status():
    db.init()
    with db.connect() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM cards GROUP BY status ORDER BY status").fetchall()
        print("== Board ==")
        for r in rows:
            print(f"  {r['status']:<16} {r['n']}")
        blocked = c.execute(
            "SELECT id, repo, pr_number, head_sha FROM cards WHERE kind='approve' AND blocked=1 AND status='approve_blocked'"
        ).fetchall()
        if blocked:
            print("\n== Approve gates waiting (run: unblock <id>) ==")
            for b in blocked:
                print(f"  #{b['id']} {b['repo']}#{b['pr_number']} @{b['head_sha'][:10]}")


def cmd_list(status=None):
    db.init()
    with db.connect() as c:
        if status:
            rows = c.execute("SELECT * FROM cards WHERE status=? ORDER BY updated_at DESC", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM cards ORDER BY updated_at DESC LIMIT 50").fetchall()
        for r in rows:
            blk = " [BLOCKED]" if r["blocked"] else ""
            print(f"#{r['id']:<4} {r['kind']:<7} {r['status']:<16} {r['repo']}#{r['pr_number']} @{(r['head_sha'] or '')[:10]}{blk}")


def cmd_findings(card_id):
    db.init()
    with db.connect() as c:
        rows = db.findings_for_card(c, int(card_id))
        for f in rows:
            print(f"[{f['status']}] {f['severity']}/{f['confidence']} {f['file']}:{f['line']} — {f['title']}")


def cmd_logs(n=30):
    db.init()
    with db.connect() as c:
        rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(n),)).fetchall()
        for r in reversed(rows):
            detail = f" {r['detail']}" if r["detail"] else ""
            print(f"{_fmt_ts(r['ts'])} {r['type']:<22} {r['key'] or ''}{detail}")


def cmd_start(card_id, engine="claude"):
    if engine not in ("claude", "codex"):
        print("engine must be claude or codex")
        return
    db.init()
    with db.connect() as c:
        card = c.execute("SELECT * FROM cards WHERE id=?", (int(card_id),)).fetchone()
        if not card or card["kind"] != "review" or card["status"] != "triage":
            print("not a triaged review card")
            return
        db.set_engine(c, card["id"], engine)
        db.set_status(c, card["id"], "intake")
        db.log_event(c, "operator_start", card["key"], {"engine": engine})
        print(f"started review on #{card_id} with {engine}; runs on next tick")


def cmd_ignore(card_id):
    db.init()
    with db.connect() as c:
        card = c.execute("SELECT * FROM cards WHERE id=?", (int(card_id),)).fetchone()
        if not card:
            print("no such card")
            return
        db.set_status(c, card["id"], "archived")
        db.log_event(c, "operator_ignore", card["key"])
        print(f"ignored #{card_id}")


def cmd_stop(card_id):
    from . import worktree
    db.init()
    with db.connect() as c:
        card = c.execute("SELECT * FROM cards WHERE id=?", (int(card_id),)).fetchone()
        if not card or card["status"] not in ("intake", "reviewing", "verifying", "commenting"):
            print("not an active review card")
            return
        repo, pr = card["repo"], card["pr_number"]
        db.set_status(c, card["id"], "archived")
        db.log_event(c, "review_stopped", card["key"], {"from": card["status"]})
    worktree.kill_review_process(repo, pr)
    print(f"stopped review #{card_id} ({repo}#{pr}) — LLM 프로세스 종료 + archived")


def cmd_unblock(card_id):
    db.init()
    with db.connect() as c:
        card = c.execute("SELECT * FROM cards WHERE id=?", (int(card_id),)).fetchone()
        if not card or card["kind"] != "approve":
            print("not an approve card")
            return
        db.set_status(c, card["id"], "approving", blocked=0)
        db.log_event(c, "operator_unblock", card["key"])
        print(f"unblocked #{card_id}; will be re-verified + approved on next tick")


def main(argv):
    if not argv:
        print(__doc__)
        return
    cmd, *rest = argv
    if cmd == "status":
        cmd_status()
    elif cmd == "list":
        cmd_list(rest[0] if rest else None)
    elif cmd == "findings":
        cmd_findings(rest[0])
    elif cmd == "logs":
        cmd_logs(rest[0] if rest else 30)
    elif cmd == "start":
        cmd_start(rest[0], rest[1] if len(rest) > 1 else "claude")
    elif cmd == "ignore":
        cmd_ignore(rest[0])
    elif cmd == "stop":
        cmd_stop(rest[0])
    elif cmd == "unblock":
        cmd_unblock(rest[0])
    elif cmd == "tick":
        tick.main()
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
