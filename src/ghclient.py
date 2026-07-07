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


def review_requested_prs(limit: int = 30) -> list:
    """내가 리뷰어로 지정된 열린 PR (전체 GitHub). 호출부에서 allowlist로 걸러 씀.
    gh 없거나 실패해도 예외 대신 빈 리스트 — 브리핑의 부가 섹션이라 조용히 생략."""
    proc = _run([
        "search", "prs", "--review-requested=@me", "--state=open",
        "--limit", str(limit), "--json", "number,title,url,repository,isDraft",
    ], check=False)
    if proc.returncode != 0:
        return []
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    out = []
    for r in rows:
        if r.get("isDraft"):
            continue
        repo = (r.get("repository") or {}).get("nameWithOwner", "")
        out.append({"repo": repo, "number": r.get("number"),
                    "title": r.get("title", ""), "url": r.get("url", "")})
    return out


def my_commits(repo: str, since_iso: str, until_iso: str, cap: int = 30) -> list:
    """내가 author인 커밋(주어진 UTC 구간) — 스탠드업 '어제 한 일'용. 병합커밋 제외."""
    me = my_login()
    if not me:
        return []
    q = f"repos/{repo}/commits?author={me}&since={since_iso}&until={until_iso}&per_page=100"
    proc = _run(["api", q, "--paginate",
                 "-q", ".[] | {sha: .sha, msg: .commit.message, url: .html_url, date: .commit.committer.date}"],
                check=False)
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = (d.get("msg") or "").splitlines()[0].strip()  # 첫 줄만
        if not msg or msg.startswith("Merge "):
            continue
        out.append({"sha": (d.get("sha") or "")[:8], "msg": msg,
                    "url": d.get("url", ""), "date": d.get("date", "")})
        if len(out) >= cap:
            break
    return out


def _my_prs_on(date_flag: str, day: str, cap: int = 30) -> list:
    """date_flag: 'created'|'merged'. day는 'YYYY-MM-DD' 또는 'A..B' 범위."""
    proc = _run(["search", "prs", "--author=@me", f"--{date_flag}={day}", "--limit", str(cap),
                 "--json", "number,title,url,repository,createdAt,closedAt"], check=False)
    if proc.returncode != 0:
        return []
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    return [{"repo": (r.get("repository") or {}).get("nameWithOwner", ""),
             "number": r.get("number"), "title": r.get("title", ""), "url": r.get("url", ""),
             "created_at": r.get("createdAt", ""), "closed_at": r.get("closedAt", "")} for r in rows]


def my_prs_created(day: str) -> list:
    return _my_prs_on("created", day)


def my_prs_merged(day: str) -> list:
    return _my_prs_on("merged", day)


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
