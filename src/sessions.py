"""내 에이전트 세션(Claude Code · Codex) 기록을 시간 구간으로 잘라 압축한다 — LLM 미관여.

  💬 claude : ~/.claude/projects/*/*.jsonl   (대화형 세션만 — entrypoint 'sdk-*' 제외)
  💬 codex  : ~/.codex/sessions/**/rollout-*.jsonl

세션 하나에서 구간 [since, until) 안에 든 것만 뽑는다:
  - 요청   : 사람이 에이전트에게 한 말(메타·훅 주입·도구 결과 제외, 슬래시 명령은 이름만)
  - PR     : 세션에 연결된 PR (claude 의 pr-link · `gh pr create/merge`)
  - 커밋   : `git commit` 메시지 첫 줄
  - 스킬   : 호출한 스킬 이름
  - 마지막 응답 : 구간 안 에이전트의 마지막 텍스트

빼는 것:
  - Lookout 자신이 돌린 headless 세션 (cwd 가 HERMES_HOME 아래)
  - 다른 도구가 띄운 headless 하위 세션 (claude entrypoint 'sdk-*', codex originator
    'codex_exec') — red-team 리뷰어처럼 대화형 세션이 부른 것이라, 그 일은 부모
    세션에 이미 잡힌다. 세면 같은 일이 열 번씩 나온다.
  - 사이드체인(서브에이전트) 메시지
"""
import datetime
import glob
import json
import os
import re
import subprocess

from .config import CFG, HERMES_HOME

HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")

# 사람이 친 말이 아니라 하네스가 끼워 넣은 것 — 요청으로 세지 않는다.
_INJECTED_PREFIXES = (
    "<local-command", "<system-reminder", "<task-notification", "<bash-",
    "<user-prompt-submit-hook", "Caveat:", "[Request interrupted",
    "<environment_context", "<user_instructions", "# AGENTS.md",
)
# 진행 상황과 무관한 내장 명령 — 이름조차 남기지 않는다.
_TRIVIAL_COMMANDS = {
    "clear", "exit", "compact", "model", "resume", "config", "cost", "status",
    "login", "logout", "mcp", "context", "rename", "effort", "fast", "help",
    "permissions", "memory", "doctor", "hooks", "agents", "theme", "vim", "init",
    "usage", "add-dir", "ide", "terminal-setup", "plugin", "statusline", "rewind",
}
_CMD_NAME_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_COMMIT_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b")
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*?)(?:\n|$)", re.S)
_DASH_M_RE = re.compile(r"-m\s+(\"|')(.*?)(?<!\\)\1", re.S)
_PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b")
_PR_TITLE_RE = re.compile(r"--title\s+(\"|')(.*?)(?<!\\)\1", re.S)
_PR_MERGE_RE = re.compile(r"\bgh\s+pr\s+merge\s+(\S+)")
_PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


def _iso_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# Lookout 이 리뷰를 돌리는 작업 디렉토리. HERMES_HOME 전체를 빼면 그 체크아웃에서
# 사람이 직접 한 개발 세션까지 사라진다(실측: 이 기능을 만든 세션이 빠졌다).
_BOT_DIRS = tuple(os.path.join(HERMES_HOME, d) for d in ("worktrees", "repos", "workspaces"))


def _excluded_cwd(cwd: str) -> bool:
    if not cwd:
        return False
    return any(cwd == d or cwd.startswith(d + os.sep) for d in _BOT_DIRS)


def _one_line(text: str, n: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def clean_prompt(text: str) -> str:
    """사람이 친 요청 한 건을 정리한다. 요청이 아니면 ''.

    슬래시 명령은 `/이름 인자` 로 줄이고, 내장 명령(/clear 등)은 버린다."""
    t = (text or "").strip()
    if not t:
        return ""
    if "<command-name>" in t:
        m = _CMD_NAME_RE.search(t)
        name = (m.group(1) if m else "").strip()
        if not name or name.split(":")[-1] in _TRIVIAL_COMMANDS:
            return ""
        a = _CMD_ARGS_RE.search(t)
        args = _one_line(a.group(1), 80) if a else ""
        return f"/{name} {args}".strip()
    t = _REMINDER_RE.sub("", t).strip()
    if t.startswith(_INJECTED_PREFIXES):
        return ""
    return t


def user_prompt(content) -> str:
    """user 메시지 content(문자열 또는 블록 목록) → 사람이 친 요청. 블록마다 따로
    걸러야 한다 — 하네스가 앞 블록에 system-reminder 를 끼우면 합친 문자열 전체가
    주입물처럼 보인다."""
    if isinstance(content, str):
        return clean_prompt(content)
    if isinstance(content, list):
        parts = [clean_prompt(p.get("text", "")) for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def _commit_subject(cmd: str):
    m = _COMMIT_RE.search(cmd or "")
    if not m:
        return None
    rest = cmd[m.end():]
    h = _HEREDOC_RE.search(rest)
    if h and h.group(2).strip():
        return h.group(2).strip()
    q = _DASH_M_RE.search(rest)
    if q and q.group(2).strip():
        return q.group(2).strip().splitlines()[0]
    return "(amend)" if "--amend" in rest else "(메시지 없음)"


def _shell_signals(cmd: str, s: dict):
    """셸 명령 하나에서 커밋·PR 생성/머지를 건진다."""
    subj = _commit_subject(cmd)
    if subj:
        _add(s["commits"], _one_line(subj, 120))
    if _PR_CREATE_RE.search(cmd or ""):
        t = _PR_TITLE_RE.search(cmd)
        _add(s["pr_actions"], "PR 생성" + (f": {_one_line(t.group(2), 100)}" if t else ""))
    m = _PR_MERGE_RE.search(cmd or "")
    if m:
        _add(s["pr_actions"], f"PR 머지: {m.group(1)}")


def _add(lst: list, item):
    if item and item not in lst:
        lst.append(item)


def _new_session(source: str, sid: str, path: str) -> dict:
    return {
        "ref": f"{source}:{sid}", "source": source, "sid": sid, "path": path,
        "cwd": "", "repo": "", "branch": "", "title": "",
        "start": None, "end": None,
        "prompts": [], "prior": [], "commits": [], "prs": [], "pr_actions": [],
        "skills": [], "last_reply": "", "activity": 0,
    }


def _touch(s: dict, ts: float):
    s["start"] = ts if s["start"] is None else min(s["start"], ts)
    s["end"] = ts if s["end"] is None else max(s["end"], ts)
    s["activity"] += 1


def _prompt(s: dict, ts: float, text: str, since: float):
    """구간 전 요청은 맥락으로 최근 2건만, 구간 안 요청은 전부."""
    if ts < since:
        s["prior"] = (s["prior"] + [(ts, text)])[-2:]
    else:
        s["prompts"].append((ts, text))
        _touch(s, ts)


# 구간 안에 사람의 요청이 없어도, 구간 전에 맡긴 일을 에이전트가 구간 안에서 계속
# 했으면 그건 이 구간의 일이다(실측: 13시대에 맡기고 17시까지 360줄 일한 세션이 빠졌다).
# 다만 응답 한두 줄짜리 꼬리는 보고 거리가 아니다.
MIN_TAIL_ACTIVITY = 5


def _worth(s) -> bool:
    return bool(s) and (bool(s["prompts"]) or s["activity"] >= MIN_TAIL_ACTIVITY)


# ── Claude Code ─────────────────────────────────────────────────────────────
# 줄 대부분은 첨부·API 요청 기록이다. json.loads 전에 문자열로 걸러 수십 MB 세션도 빨리 훑는다.
_CLAUDE_WANTED = ('"type":"user"', '"type":"assistant"', '"type":"ai-title"', '"type":"pr-link"')


def parse_claude(path: str, since: float, until: float):
    """세션 파일 하나 → 구간 안 활동 dict. 대화형이 아니거나 구간 안에 한 일이
    없으면 None."""
    s = None
    title = ""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not any(k in line for k in _CLAUDE_WANTED):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t == "ai-title":
                    title = d.get("aiTitle") or title
                    continue
                if s is None:
                    s = _new_session("claude", d.get("sessionId") or os.path.basename(path)[:-6], path)
                if str(d.get("entrypoint") or "").startswith("sdk"):
                    return None
                cwd = d.get("cwd")
                if cwd:
                    if _excluded_cwd(cwd):
                        return None
                    s["cwd"] = cwd
                ts = _iso_ts(d.get("timestamp"))
                if ts is None or ts >= until:
                    continue
                if d.get("isSidechain"):
                    continue
                msg = d.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                if t == "user":
                    if d.get("isMeta") or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
                        continue
                    p = user_prompt(content)
                    if p:
                        _prompt(s, ts, p, since)
                    continue
                if ts < since:
                    continue
                if t == "pr-link":
                    _add(s["prs"], d.get("prUrl") or "")
                    continue
                if d.get("gitBranch"):
                    s["branch"] = d["gitBranch"]
                if t == "assistant" and isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "text" and part.get("text", "").strip():
                            s["last_reply"] = part["text"].strip()
                            _touch(s, ts)
                        elif part.get("type") == "tool_use":
                            _claude_tool(part, s)
                            _touch(s, ts)
    except OSError:
        return None
    if not _worth(s):
        return None
    s["title"] = title
    return s


def _claude_tool(part: dict, s: dict):
    name = part.get("name") or ""
    inp = part.get("input") or {}
    if name == "Bash":
        _shell_signals(inp.get("command") or "", s)
    elif name == "Skill":
        _add(s["skills"], inp.get("skill") or "")


# ── Codex ───────────────────────────────────────────────────────────────────
def parse_codex(path: str, since: float, until: float):
    s = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = d.get("payload") or {}
                if d.get("type") == "session_meta":
                    if p.get("originator") == "codex_exec" or _excluded_cwd(p.get("cwd", "")):
                        return None
                    s = _new_session("codex", p.get("id") or os.path.basename(path), path)
                    s["cwd"] = p.get("cwd") or ""
                    git = p.get("git") or {}
                    s["branch"] = git.get("branch") or ""
                    s["repo"] = _repo_from_url(git.get("repository_url") or "")
                    continue
                if s is None:
                    continue
                ts = _iso_ts(d.get("timestamp"))
                if ts is None or ts >= until:
                    continue
                pt = p.get("type")
                if d.get("type") == "event_msg" and pt == "user_message":
                    txt = clean_prompt(p.get("message") or "")
                    if txt:
                        if not s["title"]:
                            s["title"] = _one_line(txt, 40)
                        _prompt(s, ts, txt, since)
                    continue
                if ts < since:
                    continue
                if d.get("type") == "event_msg" and pt == "agent_message":
                    if (p.get("message") or "").strip():
                        s["last_reply"] = p["message"].strip()
                        _touch(s, ts)
                elif d.get("type") == "response_item" and pt == "function_call":
                    _shell_signals(_codex_command(p.get("arguments")), s)
                    _touch(s, ts)
    except OSError:
        return None
    return s if _worth(s) else None


def _codex_command(arguments) -> str:
    try:
        a = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except json.JSONDecodeError:
        return ""
    cmd = a.get("command") or a.get("cmd") or ""
    return " ".join(cmd) if isinstance(cmd, list) else str(cmd)


# ── repo 이름 ───────────────────────────────────────────────────────────────
def _repo_from_url(url: str) -> str:
    m = re.search(r"[:/]([\w.-]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else ""


_REPO_CACHE = {}


def _known_repos() -> list:
    return [r.split("/")[-1] for r in CFG.get("allowlist", []) if r]


def _repo_of(cwd: str, prs=()) -> str:
    """cwd 의 repo 이름. 워크트리 디렉토리 이름(PH-3205-chat-badge)은 repo 가 아니다.

    세션에 PR 이 있으면 그 repo, 아니면 git 에 묻고, 워크트리가 이미 지워졌으면 경로
    조각 중 리뷰 대상 repo 이름(…/workspaces/ceo-client/PH-4205-x → ceo-client)을 찾는다."""
    for url in prs:
        m = _PR_URL_RE.search(url or "")
        if m:
            return m.group(1).split("/")[-1]
    if not cwd:
        return ""
    if cwd in _REPO_CACHE:
        return _REPO_CACHE[cwd]
    name = ""
    if os.path.isdir(cwd):
        try:
            r = subprocess.run(["git", "-C", cwd, "config", "--get", "remote.origin.url"],
                               capture_output=True, text=True, timeout=3)
            name = _repo_from_url(r.stdout.strip()) if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            name = ""
    if not name:
        parts = cwd.rstrip(os.sep).split(os.sep)
        name = next((p for p in reversed(parts) if p in _known_repos()), "")
    _REPO_CACHE[cwd] = name or os.path.basename(cwd.rstrip(os.sep))
    return _REPO_CACHE[cwd]


# ── 수집 ────────────────────────────────────────────────────────────────────
def _recent_files(pattern: str, since: float):
    for f in glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(f) >= since:
                yield f
        except OSError:
            continue


def collect(since: float, until: float) -> list:
    """구간 안에 사람이 말을 건 세션들을 시작 시각 순으로."""
    out = []
    for f in _recent_files(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl"), since):
        s = parse_claude(f, since, until)
        if s:
            out.append(s)
    for f in _recent_files(os.path.join(CODEX_SESSIONS, "**", "rollout-*.jsonl"), since):
        s = parse_codex(f, since, until)
        if s:
            out.append(s)
    for s in out:
        if not s["repo"]:
            s["repo"] = _repo_of(s["cwd"], s["prs"])
    out.sort(key=lambda x: x["start"] or 0)
    return out


# ── 프롬프트용 압축 ─────────────────────────────────────────────────────────
def _hm(ts) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"


def _pr_label(url: str) -> str:
    m = _PR_URL_RE.search(url or "")
    return f"{m.group(1)}#{m.group(2)}" if m else url


def digest(s: dict, label: str, budget: int = 3000) -> str:
    """세션 하나를 프롬프트에 넣을 텍스트로. budget 을 넘지 않게 요청부터 줄인다.

    첫 요청은 세션의 목적을 담는 경우가 많아 길게, 나머지는 짧게 둔다. 요청이
    많으면 앞 2건 + 뒤쪽을 남긴다 — 뒤쪽이 결론(머지해줘·배포해줘)에 가깝다."""
    src = "Claude" if s["source"] == "claude" else "Codex"
    head = [f"### {label} · {src} · {s['repo'] or '?'}"
            + (f" · {s['branch']}" if s["branch"] else "")
            + f" · {_hm(s['start'])}~{_hm(s['end'])[-5:]}"]
    if s["title"]:
        head.append(f"제목: {s['title']}")
    if s["prs"]:
        head.append("연결된 PR: " + ", ".join(_pr_label(u) for u in s["prs"][:5]))
    if s["pr_actions"]:
        head.append("PR 조작: " + " / ".join(s["pr_actions"][:4]))
    if s["commits"]:
        head.append("커밋: " + " / ".join(s["commits"][:5]))
    if s["skills"]:
        head.append("스킬: " + ", ".join(s["skills"][:8]))
    reply = _one_line(s["last_reply"], 400)
    tail = [f"마지막 응답: {reply}"] if reply else []

    prompts = [p for _, p in s["prompts"]]
    for first_n, rest_n, keep in ((600, 200, 14), (400, 140, 10), (240, 100, 6), (160, 80, 4)):
        prior = [f"- {_one_line(p, rest_n)}" for _, p in s["prior"]]
        picked = prompts if len(prompts) <= keep else prompts[:2] + prompts[-(keep - 2):]
        lines = [f"- {_one_line(p, first_n if i == 0 else rest_n)}" for i, p in enumerate(picked)]
        if len(prompts) > keep:
            lines.insert(2, f"- …(요청 {len(prompts) - keep}건 생략)")
        body = (["구간 전 요청(맥락 — 이번 구간의 일이 아님):"] + prior if prior else [])
        body += (["요청:"] + lines) if lines else ["요청: (구간 안 요청 없음 — 위 요청을 에이전트가 이어서 진행)"]
        text = "\n".join(head + body + tail)
        if len(text) <= budget:
            return text
    return text[:budget]


def digest_all(sessions: list, max_chars: int = 60000) -> tuple:
    """[(label, session)] 과 프롬프트 본문. 세션이 많으면 세션당 예산을 줄인다."""
    labeled = [(f"S{i + 1}", s) for i, s in enumerate(sessions)]
    if not labeled:
        return [], ""
    per = max(800, min(3000, max_chars // len(labeled)))
    body = "\n\n".join(digest(s, label, per) for label, s in labeled)
    return labeled, body[:max_chars]
