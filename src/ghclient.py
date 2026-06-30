"""Thin wrappers around the `gh` CLI. Uses the operator's own auth.

Intake reads are deterministic (no LLM). Mutations (comment/approve) are guarded
by dry-run flags in the workers, not here.
"""
import json
import subprocess

from . import config

GH = config.resolve_bin(config.CFG["gh_bin"])


class GhError(RuntimeError):
    pass


def _run(args, check=True):
    proc = subprocess.run([GH, *args], capture_output=True, text=True, env=config.subprocess_env())
    if check and proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def pr_view(repo: str, pr: int) -> dict:
    """Authoritative fresh head/base/state. Never trust webhook payload head."""
    fields = "number,headRefOid,baseRefName,headRefName,state,isDraft,title,author,url,mergeable,reviewDecision,statusCheckRollup"
    proc = _run(["pr", "view", str(pr), "--repo", repo, "--json", fields])
    return json.loads(proc.stdout)


def pr_list_open(repo: str) -> list:
    fields = "number,headRefOid,author,isDraft,state,title,url"
    proc = _run(["pr", "list", "--repo", repo, "--state", "open", "--limit", "100", "--json", fields])
    return json.loads(proc.stdout)


def pr_diff(repo: str, pr: int) -> str:
    proc = _run(["pr", "diff", str(pr), "--repo", repo])
    return proc.stdout


def pr_comment(repo: str, pr: int, body: str) -> str:
    proc = _run(["pr", "comment", str(pr), "--repo", repo, "--body", body])
    return proc.stdout.strip()


_MY_LOGIN = None


def my_login() -> str:
    global _MY_LOGIN
    if _MY_LOGIN is None:
        proc = _run(["api", "user", "-q", ".login"], check=False)
        _MY_LOGIN = proc.stdout.strip() if proc.returncode == 0 else ""
    return _MY_LOGIN


def my_approved(repo: str, pr: int, head_sha: str = None) -> bool:
    """True only if *I* approved the requested head.

    GitHub keeps old review records after pushes. A previous APPROVED review by
    me must not suppress a new explicit approval for the current head.
    """
    me = my_login()
    if not me:
        return False
    proc = _run([
        "api", f"repos/{repo}/pulls/{pr}/reviews",
        "--paginate", "-q", ".[] | @json",
    ], check=False)
    if proc.returncode != 0:
        return False
    reviews = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            reviews.append(json.loads(line))
        except json.JSONDecodeError:
            return False
    mine = [r for r in reviews if ((r.get("user") or {}).get("login") == me)]
    if not mine:
        return False
    latest = mine[-1]
    if latest.get("state") != "APPROVED":
        return False
    return not head_sha or latest.get("commit_id") == head_sha


def pr_approve(repo: str, pr: int, body: str) -> str:
    proc = _run(["pr", "review", str(pr), "--repo", repo, "--approve", "--body", body])
    return proc.stdout.strip()


def pr_conversation(repo: str, pr: int, limit_chars: int = 16000) -> str:
    """Compact transcript of the PR discussion: general comments + inline review
    comments (includes the bot's own past findings and the author's replies)."""
    parts = []
    ic = _run(["pr", "view", str(pr), "--repo", repo, "--json", "comments"], check=False)
    if ic.returncode == 0:
        try:
            for cm in (json.loads(ic.stdout).get("comments") or []):
                a = (cm.get("author") or {}).get("login", "?")
                body = (cm.get("body") or "").strip()
                if body:
                    parts.append(f"[{a}] {body}")
        except json.JSONDecodeError:
            pass
    rc = _run(["api", f"repos/{repo}/pulls/{pr}/comments", "--paginate",
               "-q", ".[] | {login: .user.login, path, line, body}"], check=False)
    if rc.returncode == 0:
        for line in rc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            body = (d.get("body") or "").strip()
            if body:
                parts.append(f"[{d.get('login', '?')} on {d.get('path', '')}:{d.get('line', '')}] {body}")
    text = "\n\n".join(parts)
    if not text:
        return "(이전 대화 없음)"
    # 길면 '최신' 댓글을 보존(뒤쪽 유지) — 작성자 반박/해명은 보통 최근에 달림
    if len(text) > limit_chars:
        text = "…(이전 대화 생략)\n\n" + text[-limit_chars:]
    return text


def list_review_comments(repo: str, pr: int) -> list:
    """Existing bot comments — used for idempotency marker checks."""
    proc = _run([
        "api", f"repos/{repo}/issues/{pr}/comments",
        "--paginate", "-q", ".[] | {id, body}",
    ], check=False)
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
