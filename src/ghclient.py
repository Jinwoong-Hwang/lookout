"""Thin wrappers around the `gh` CLI. Uses the operator's own auth.

Intake reads are deterministic (no LLM). Mutations (comment/approve) are guarded
by dry-run flags in the workers, not here.
"""
import json
import re
import subprocess

from . import config

GH = config.resolve_bin(config.CFG["gh_bin"])
FP_MARKER = "<!-- hermes:fp="
# PR 본문이 길면(설계 문서째 붙는 PR이 있다) 대화 예산을 혼자 다 먹는다.
PR_BODY_CHARS = 8000
# closure 프롬프트에 실어 보낼 작성자 회신 수 상한(본문은 별도로 항상 포함)
MAX_AUTHOR_REPLIES = 10
# 버려도 되는 글 한 덩이의 상한. CI 실패 로그 덤프가 #10066 에서 60,211자로
# 대화의 71%를 먹었는데, 순서대로 버리면 값싼 지적 목록이 먼저 밀려난다.
MAX_DROPPABLE_PART = 6000
# 봇 코멘트 안에서 지적 제목 줄을 고를 때 건너뛸 소제목(commenter._block 의 라벨)
_BLOCK_LABELS = {"문제", "제안", "영향", "결정 필요"}
_HEADING = re.compile(r"^(?:\d+\.\s+)?\*\*(.+?)\*\*\s*$", re.M)


class GhError(RuntimeError):
    pass


class DiffTooLarge(GhError):
    """GitHub refuses diffs over 20,000 lines (HTTP 406). Callers fall back to a
    local `git diff` in the cached clone."""


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


def issue_list(repo: str, assignee: str = None, title_prefixes=None,
               limit: int = 100) -> list:
    """Open issues for the work board.

    `gh issue list`는 PR을 섞어 주지 않으므로 여기서 얻는 번호는 이슈 번호다.
    title_prefixes는 서버가 못 걸러주는 조건([FE] 같은 제목 태그)이라 클라이언트에서 건다."""
    # issueType/parent/subIssuesSummary 는 GitHub 네이티브 sub-issue 관계다. 목록
    # 한 번에 딸려 오므로 에픽 소속을 알아내는 데 추가 호출이 들지 않는다.
    fields = ("number,title,url,labels,assignees,updatedAt,author,"
              "issueType,parent,subIssuesSummary,projectItems")
    args = ["issue", "list", "--repo", repo, "--state", "open",
            "--limit", str(limit), "--json", fields]
    if assignee:
        args += ["--assignee", assignee]
    rows = json.loads(_run(args).stdout)
    if title_prefixes:
        rows = [r for r in rows
                if any((r.get("title") or "").startswith(p) for p in title_prefixes)]
    return rows


def issue_view(repo: str, number: int) -> dict:
    """본문 포함 단건 조회 — seed를 만들 때만 부른다(목록에는 body가 없다)."""
    fields = "number,title,body,url,state,labels,assignees,author"
    proc = _run(["issue", "view", str(number), "--repo", repo, "--json", fields])
    return json.loads(proc.stdout)


def pr_diff(repo: str, pr: int) -> str:
    """Unified diff via the API. Raises DiffTooLarge when the PR exceeds GitHub's
    20k-line diff cap — worktree.local_diff() computes it from the clone instead."""
    proc = _run(["pr", "diff", str(pr), "--repo", repo], check=False)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "too_large" in err or "exceeded the maximum number of lines" in err:
            raise DiffTooLarge(err)
        raise GhError(f"gh pr diff {pr} --repo {repo} failed: {err}")
    return proc.stdout


def pr_changed_files(repo: str, pr: int) -> list[str]:
    proc = _run(["pr", "diff", str(pr), "--repo", repo, "--name-only"])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def pr_comment(repo: str, pr: int, body: str) -> str:
    proc = _run(["pr", "comment", str(pr), "--repo", repo, "--body", body])
    return proc.stdout.strip()


_MY_LOGIN = None


def pr_create_draft(repo: str, base: str, head: str, title: str, body: str) -> str:
    """draft로 **생성**한다. 일반 상태로 만들면 저장소 전체 코드 소유자 팀에 리뷰가
    자동 요청되고, 나중에 draft로 내려도 이미 걸린 요청은 회수되지 않는다."""
    proc = _run(["pr", "create", "--repo", repo, "--draft",
                 "--base", base, "--head", head, "--title", title, "--body", body])
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


def issue_comment(repo: str, number: int, body: str) -> str:
    proc = _run(["issue", "comment", str(number), "--repo", repo, "--body", body])
    return proc.stdout.strip()


def my_login() -> str:
    global _MY_LOGIN
    if _MY_LOGIN is None:
        proc = _run(["api", "user", "-q", ".login"], check=False)
        login = proc.stdout.strip() if proc.returncode == 0 else ""
        if login:
            _MY_LOGIN = login
            return login
        return ""
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


def _finding_title(segment: str, fallback: str) -> str:
    for candidate in _HEADING.findall(segment):
        title = candidate.strip()
        if title and title not in _BLOCK_LABELS:
            return title
    return fallback


def compact_findings(login: str, body: str) -> str:
    """봇 리뷰 코멘트를 지적 한 줄씩으로 줄인다.

    리뷰어에게 이 글이 필요한 이유는 '이미 뭘 지적했나' 하나뿐인데, 원문은 문제
    서술·코드블록·제안까지 실려 한 건에 2~3천 자다. #10066 에서는 봇 65건이
    64k자를 먹어 대화 예산을 통째로 차지했고, 예산에 밀려 목록이 잘리면 같은
    문제를 새 지문으로 다시 찾는다. 제목·위치·rule 만 남기면 전부 실어도 7k자다.
    """
    lines = []
    rest = body
    for fp in re.findall(r"<!-- hermes:fp=(.+?) -->", body):
        segment, _, rest = rest.partition(f"<!-- hermes:fp={fp} -->")
        _, _, tail = fp.partition("#")
        _, _, loc = tail.partition(":")
        where, _, rule = loc.rpartition(":")
        lines.append(f"- [{login}] {_finding_title(segment, rule)} — {where} (rule: {rule})")
    return "이미 올라간 지적:\n" + "\n".join(lines) if lines else ""


def _clip_body(body: str, limit: int = PR_BODY_CHARS) -> str:
    """머리와 꼬리를 남기고 가운데를 줄인다 — '보류' 표는 보통 본문 끝에 붙는다."""
    if len(body) <= limit:
        return body
    half = limit // 2
    return f"{body[:half]}\n…(본문 중략)…\n{body[-half:]}"


def _droppable(login: str, author: str, body: str) -> bool:
    """예산이 모자랄 때 먼저 버려도 되는 글 — 작성자가 손으로 쓴 글만 지킨다."""
    return not (login and login == author and FP_MARKER not in body)


def _fit_conversation(parts: list, limit_chars: int) -> str:
    """작성자 글은 지키고, 잡음 → 오래된 봇 글 순으로 뺀다.

    뒤에서 N자만 남기던 방식은 봇 인스턴스가 여럿이면 창을 봇 코멘트로만 채워서,
    정작 근거가 되는 작성자 해명이 먼저 잘려 나갔다(#10066: 대화 127k자 중 남은
    16k자가 전부 봇 코멘트였다).
    """
    # 1단계: 덩치 큰 잡음(로그 덤프)부터 통째로 — 자리 순서보다 이게 먼저다
    kept = [p for p in parts if not (p[0] and len(p[1]) > MAX_DROPPABLE_PART)]
    dropped = len(kept) != len(parts)
    # 2단계: 그래도 넘치면 오래된 것부터
    i = 0
    while i < len(kept) and len("\n\n".join(t for _, t in kept)) > limit_chars:
        if kept[i][0]:
            kept.pop(i)
            dropped = True
        else:
            i += 1
    text = "\n\n".join(t for _, t in kept)
    if not text:
        return "(이전 대화 없음)"
    if dropped:
        text = "…(오래된 봇 댓글 생략)\n\n" + text
    if len(text) > limit_chars:  # 작성자 글만으로도 넘치면 최신 쪽을 남긴다
        text = "…(이전 대화 생략)\n\n" + text[-limit_chars:]
    return text


def pr_conversation(repo: str, pr: int, limit_chars: int = 24000) -> str:
    """Compact transcript of the PR discussion: PR body + general comments +
    inline review comments (includes the bot's own past findings and the author's
    replies).

    본문을 함께 넣는다 — 작성자는 '이 지적은 보류' 를 댓글이 아니라 PR 본문 표에
    적어 두는 경우가 많은데, 지금까지 본문은 리뷰어에게 전달되지 않았다(#10066).

    봇 코멘트는 compact_findings 로 지적 한 줄씩 줄여 싣는다 — 목록으로만 쓰이는
    글이라 원문을 다 넣으면 예산을 혼자 먹고, 밀려서 잘리면 같은 문제를 새 지문으로
    다시 찾는다. 예산은 작성자 글(본문·회신) 먼저, 남는 만큼 최신 지적 순으로.
    """
    parts = []  # (버려도 되나, 본문)
    author = ""
    pv = _run(["pr", "view", str(pr), "--repo", repo,
               "--json", "author,body,comments"], check=False)
    if pv.returncode == 0:
        try:
            data = json.loads(pv.stdout)
            author = (data.get("author") or {}).get("login", "")
            body = (data.get("body") or "").strip()
            if body:
                parts.append((False, f"[PR 본문 · {author or '?'}] {_clip_body(body)}"))
            for cm in (data.get("comments") or []):
                a = (cm.get("author") or {}).get("login", "?")
                text = (cm.get("body") or "").strip()
                if not text:
                    continue
                if FP_MARKER in text:
                    text = compact_findings(a, text)
                    if text:
                        parts.append((True, text))
                    continue
                parts.append((_droppable(a, author, text), f"[{a}] {text}"))
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
            if not body:
                continue
            login = d.get("login", "?")
            if FP_MARKER in body:  # 인라인도 봇 지적이면 같은 규칙으로 줄인다
                compact = compact_findings(login, body)
                if compact:
                    parts.append((True, compact))
                continue
            parts.append((
                _droppable(login, author, body),
                f"[{login} on {d.get('path', '')}:{d.get('line', '')}] {body}",
            ))
    return _fit_conversation(parts, limit_chars)


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


def issue_comments(repo: str, pr: int) -> list:
    """Issue comments for feedback snapshots."""
    proc = _run([
        "api", f"repos/{repo}/issues/{pr}/comments",
        "--paginate",
        "-H", "Accept: application/vnd.github+json",
        "-q", ".[] | {id, html_url, body, created_at, user: .user.login}",
    ])
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def pr_author_identity(repo: str, pr: int) -> dict:
    """작성자 신원 + PR 본문. 본문도 작성자가 직접 쓴 글이라 '보류' 근거가 된다."""
    proc = _run([
        "api", f"repos/{repo}/pulls/{pr}",
        "-q", '{login: .user.login, id: (.user.id|tostring), '
              'body: (.body // ""), created_at}',
    ])
    return json.loads(proc.stdout)


def issue_comments_structured(repo: str, pr: int) -> list[dict]:
    """Issue comments with immutable author ids, sorted as GitHub returned them."""
    proc = _run([
        "api", f"repos/{repo}/issues/{pr}/comments", "--paginate",
        "-q", ".[] | {id: (.id|tostring), author: .user.login, "
              "author_id: (.user.id|tostring), created_at, body}",
    ])
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def finding_author_replies(comments: list[dict], fp: str, author_id: str,
                           bot_login: str, pr_body: dict | None = None) -> list[dict]:
    """Verified PR-author replies in this finding's comment windows, plus the PR body.

    묶음 댓글이어도 지문 문자열을 요구하지 않는다 — 사람은 지문을 복붙하지 않으므로,
    지적 2건 이상을 한 댓글로 올리는 지금 구성에서는 작성자 회신이 100% 누락됐다
    (#10066: 같은 지적이 8회 재게시). 어느 지적에 대한 답인지는 closure 판정기가
    가리고, 인용 검증과 운영자 게이트가 한 번 더 받는다.

    봇이 쓴 글은 작성자 계정으로 올라와도 회신이 아니다 — 작성자도 자기 인스턴스를
    돌리면 그 리뷰 코멘트가 같은 author_id 로 섞여 들어온다.

    창이 여러 개면 모두 모은다(예전엔 마지막 창만 남겨서, 첫 라운드에 한 번 답하고
    만 해명이 다음 라운드에 사라졌다). 대신 최신 MAX_AUTHOR_REPLIES 건으로 끊는다.
    """
    marker = f"<!-- hermes:fp={fp} -->"
    found: dict[str, dict] = {}
    if pr_body and (pr_body.get("body") or "").strip():
        found[str(pr_body["id"])] = pr_body
    for idx, source in enumerate(comments):
        if source.get("author") != bot_login or marker not in (source.get("body") or ""):
            continue
        for comment in comments[idx + 1:]:
            body = comment.get("body") or ""
            if comment.get("author") == bot_login and FP_MARKER in body:
                break
            if (str(comment.get("author_id") or "") == str(author_id)
                    and FP_MARKER not in body and body.strip()):
                found[str(comment["id"])] = {
                    "id": str(comment["id"]), "author": comment.get("author", ""),
                    "created_at": comment.get("created_at", ""), "body": body,
                }
    body_entry = found.pop(str(pr_body["id"]), None) if pr_body else None
    replies = sorted(found.values(), key=lambda r: ((r.get("created_at") or ""), r["id"]))
    replies = replies[-MAX_AUTHOR_REPLIES:]
    return ([body_entry] if body_entry else []) + replies


def comment_reactions(repo: str, comment_id: str) -> dict:
    """Count reactions on one issue comment."""
    proc = _run([
        "api", f"repos/{repo}/issues/comments/{comment_id}/reactions",
        "--paginate",
        "-H", "Accept: application/vnd.github+json",
        "-q", ".[].content",
    ])
    counts = {"+1": 0, "-1": 0, "confused": 0, "total_count": 0}
    for line in proc.stdout.splitlines():
        content = line.strip()
        if content in counts:
            counts[content] += 1
        counts["total_count"] += 1
    return counts
