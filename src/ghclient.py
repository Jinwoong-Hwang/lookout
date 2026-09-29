"""Thin wrappers around the `gh` CLI. Uses the operator's own auth.

Intake reads are deterministic (no LLM). Mutations (comment/approve) are guarded
by dry-run flags in the workers, not here.
"""
import hashlib
import json
import re
import subprocess

from . import config

GH = config.resolve_bin(config.CFG["gh_bin"])
FP_MARKER = "<!-- hermes:fp="
# PR 본문이 길면(설계 문서째 붙는 PR이 있다) 대화 예산을 혼자 다 먹는다.
PR_BODY_CHARS = 8000
# closure 프롬프트에 실어 보낼 작성자 회신 수 상한(본문은 별도로 항상 포함)
# closure 프롬프트에 실을 작성자 회신 총량. 건수가 아니라 문자로 끊는다 —
# #10066 은 회신이 14건이라 "최신 10건" 이면 가장 오래된 1라운드 회신(보류
# 근거가 거기 있다)이 잘렸다. 예산은 넉넉히 두고, 호출 횟수로 비용을 잡는다.
AUTHOR_REPLY_CHARS = 40000
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


def pr_states(refs: list[tuple[str, int]], batch_size: int = 75) -> dict:
    """Fetch state/head for tracked PRs with one GraphQL call per bounded batch."""
    unique = list(dict.fromkeys((repo, int(pr)) for repo, pr in refs))
    out = {}
    for start in range(0, len(unique), batch_size):
        batch = unique[start:start + batch_size]
        grouped = {}
        for repo, pr in batch:
            grouped.setdefault(repo, []).append(pr)
        fields, aliases = [], {}
        for repo_idx, (repo, prs) in enumerate(grouped.items()):
            owner, name = repo.split("/", 1)
            pr_fields = []
            for pr_idx, pr in enumerate(prs):
                alias = f"p{pr_idx}"
                aliases[(f"r{repo_idx}", alias)] = (repo, pr)
                pr_fields.append(
                    f"{alias}: pullRequest(number: {pr}) "
                    "{ number state headRefOid }"
                )
            fields.append(
                f"r{repo_idx}: repository(owner: {json.dumps(owner)}, "
                f"name: {json.dumps(name)}) {{ {' '.join(pr_fields)} }}"
            )
        query = "query { " + " ".join(fields) + " }"
        payload = json.loads(_run(["api", "graphql", "-f", f"query={query}"]).stdout)
        if payload.get("errors") and not payload.get("data"):
            raise GhError(f"GitHub GraphQL failed: {payload['errors']}")
        data = payload.get("data") or {}
        for (repo_alias, pr_alias), ref in aliases.items():
            info = (data.get(repo_alias) or {}).get(pr_alias)
            if info is not None:
                out[ref] = info
    return out


def issue_list(repo: str, assignee: str = None, title_prefixes=None,
               limit: int = 100) -> list:
    """Open issues for the issue view (에픽별).

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


def parse_findings(body: str):
    """봇 리뷰 코멘트에서 (fp, 제목, 위치) 를 뽑는다 — 우리가 쓴 마커가 근거다."""
    out, rest = [], body
    for fp in re.findall(r"<!-- hermes:fp=(.+?) -->", body):
        segment, _, rest = rest.partition(f"<!-- hermes:fp={fp} -->")
        _, _, tail = fp.partition("#")
        _, _, loc = tail.partition(":")
        where, _, rule = loc.rpartition(":")
        out.append((fp, _finding_title(segment, rule), where, rule))
    return out


def compact_findings(login: str, body: str) -> str:
    """봇 리뷰 코멘트를 지적 한 줄씩으로 줄인다 — doc 프로필의 대화 전사용."""
    lines = [f"- [{login}] {title} — {where} (rule: {rule})"
             for _fp, title, where, rule in parse_findings(body)]
    return "이미 올라간 지적:\n" + "\n".join(lines) if lines else ""


def other_bot_findings(repo: str, pr: int, my: str):
    """다른 인스턴스가 올린 지적 — 제목·위치만. 상태는 우리 DB 에 없어서 모른다.

    #10066 은 인스턴스 4대가 붙어 55행 중 41행이 남의 것이었다. 이걸 빼면
    리뷰어가 남이 이미 지적한 것을 다시 만든다.
    """
    seen, out = set(), []
    for args, login_key in (
        ([f"repos/{repo}/issues/{pr}/comments"], "user"),
        ([f"repos/{repo}/pulls/{pr}/comments"], "user"),
        ([f"repos/{repo}/pulls/{pr}/reviews"], "user"),
    ):
        proc = _run(["api", *args, "--paginate",
                     "-q", f".[] | {{login: .{login_key}.login, body}}"], check=False)
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            login = d.get("login", "?")
            if login == my:
                continue
            for fp, title, where, rule in parse_findings(d.get("body") or ""):
                if rule in seen:
                    continue
                seen.add(rule)
                out.append({"login": login, "fp": fp, "title": title,
                            "where": where, "rule": rule})
    return out


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


def _author_record(source: str, ident, login: str, created_at: str, url: str,
                   body: str) -> dict:
    """출처가 다르면 id 공간도 다르다 — (출처, id) 를 합쳐 한 값으로 쓴다.

    이슈 코멘트·리뷰·리뷰 코멘트는 서로 다른 번호 체계라, 그냥 id 만 저장하면
    나중에 어느 글이었는지 되짚을 수 없다.
    """
    return {"id": f"{source}:{ident}", "source": source, "author": login,
            "created_at": created_at or "", "url": url or "", "body": body}


def collect_author_replies(repo: str, pr: int, author: dict) -> list[dict]:
    """작성자가 직접 쓴 글을 네 출처에서 모은다 — 오래된 순.

    closure 가 /issues/comments 하나만 읽고 있었다. #10066 실측으로 작성자 회신
    10라운드 중 9라운드가 PR '리뷰 본문'(/pulls/reviews)에 있었고, 판정기에 닿은
    건 작성자 글 32,469자 중 2,529자(7.8%)뿐이었다.

    봇이 쓴 글은 제외한다 — 작성자도 자기 인스턴스를 돌리면 그 리뷰 코멘트가 같은
    author_id 로 올라와(#10066 에서 10건) '작성자 회신'으로 섞인다.
    """
    author_id = str(author.get("id") or "")
    login = author.get("login", "")
    out = []

    body = (author.get("body") or "").strip()
    if body:
        out.append(_author_record(
            "body", "pr", login, author.get("created_at", ""),
            f"https://github.com/{repo}/pull/{pr}", _clip_body(body)))

    def take(source, args, url_key="html_url"):
        proc = _run(args, check=False)
        if proc.returncode != 0:
            return
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = (d.get("body") or "").strip()
            if (not text or FP_MARKER in text
                    or str(d.get("author_id") or "") != author_id):
                continue
            out.append(_author_record(source, d.get("id"), login,
                                      d.get("created_at", ""), d.get(url_key, ""), text))

    q = ("{id, author_id: (.user.id|tostring), created_at, body, html_url}")
    take("issue", ["api", f"repos/{repo}/issues/{pr}/comments", "--paginate", "-q", f".[] | {q}"])
    take("review", ["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate",
                    "-q", ".[] | {id, author_id: (.user.id|tostring), "
                          "created_at: .submitted_at, body, html_url}"])
    take("review_comment", ["api", f"repos/{repo}/pulls/{pr}/comments", "--paginate",
                            "-q", f".[] | {q}"])
    return sorted(out, key=lambda r: (r["created_at"], r["id"]))


def replies_digest(replies: list[dict]) -> str:
    """작성자 글 전체의 지문. 생성 시각만 보면 '수정' 을 놓친다.

    #10066 의 작성자는 보류 표를 PR 본문에 나중에 추가했다. 본문·댓글을 고치면
    created_at 은 그대로이므로, 시각만 기준으로 삼으면 정작 우리가 기다리던 답변이
    왔는데도 closure 를 건너뛴다(셀프 리뷰 지적).
    """
    h = hashlib.sha1()
    for r in sorted(replies, key=lambda x: str(x.get("id"))):
        h.update(str(r.get("id")).encode())
        h.update(b"\x00")
        h.update((r.get("body") or "").encode())
        h.update(b"\x00")
    return h.hexdigest()


def trim_author_replies(replies: list[dict], pinned: str = "",
                        budget: int = AUTHOR_REPLY_CHARS) -> list[dict]:
    """예산을 넘으면 오래된 것부터 뺀다. 단 pinned(이미 결정 근거로 인용된 글)와
    PR 본문은 남긴다 — 보류 근거는 대개 가장 오래된 회신에 있어서, 최신부터
    담다가 끊으면 정작 필요한 글이 먼저 사라진다."""
    keep = [r for r in replies if r["source"] == "body" or r["id"] == pinned]
    rest = [r for r in replies if r not in keep]
    used = sum(len(r["body"]) for r in keep)
    chosen = list(keep)
    for r in reversed(rest):  # 최신부터 채운다
        if used + len(r["body"]) > budget:
            continue
        chosen.append(r)
        used += len(r["body"])
    return sorted(chosen, key=lambda r: (r["created_at"], r["id"]))


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
