"""Headless `claude` invocations for the LLM worker stages (ADR-001: LLM only here).

Read-only by construction: tools restricted to Read/Grep/Glob, Edit/Write/Bash
disallowed, target worktree is detached + never pushed (ADR-009).
"""
import json
import subprocess

from . import config

CFG = config.CFG
CLAUDE = config.resolve_bin(CFG["claude_bin"])
MODEL = CFG["claude_model"]
EFFORT = CFG.get("claude_effort")  # low|medium|high|xhigh|max, None = 기본

READONLY_ALLOWED = ["Read", "Grep", "Glob"]
DISALLOWED = ["Write", "Edit", "Bash", "NotebookEdit", "WebFetch", "WebSearch"]

# 느린 엔진이 더 짧은 상한을 갖고 있었다 — codex 는 1200s 인데 claude 는 900s 였고,
# #10066 리뷰가 872s·908s·983s 로 그 경계에 걸려 간헐 실패했다. 큰 PR 에서
# 리뷰를 다 하고도 결과를 버리는 게 제일 아깝다.
RUN_TIMEOUT = int(CFG.get("claude_timeout", 1800))


class ClaudeError(RuntimeError):
    pass


def run(prompt: str, cwd: str = None, add_dir: str = None, timeout: int = None,
        model: str = None, effort: str = None) -> str:
    """Run claude headless, return the assistant's final text (the `result`).

    model/effort 미지정 시 config 기본(리뷰용 opus/xhigh). 브리핑처럼 가벼운 잡은
    model='haiku', effort='' 로 넘겨 값싸게 돌린다(effort=''면 --effort 미첨부)."""
    args = [
        CLAUDE, "-p", prompt,
        "--output-format", "json",
        "--model", model or MODEL,
        "--permission-mode", "bypassPermissions",
        "--allowedTools", *READONLY_ALLOWED,
        "--disallowedTools", *DISALLOWED,
    ]
    eff = EFFORT if effort is None else effort
    if eff:
        args += ["--effort", eff]
    if add_dir:
        args += ["--add-dir", add_dir]
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout or RUN_TIMEOUT, env=config.subprocess_env())
    if proc.returncode != 0:
        # 실패 사유는 stderr 맨 끝에 찍히므로 앞이 아니라 뒤를 남긴다
        raise ClaudeError(f"claude failed (rc={proc.returncode}): {proc.stderr.strip()[-500:]}")
    try:
        env = json.loads(proc.stdout)
        return env.get("result", proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout


def parse_obj(text: str) -> dict:
    """엔진 응답을 **dict 로** 돌려준다.

    parse_json 은 배열도 반환한다 — 모델이 `[{...}]` 로 답하면 호출부가
    obj.update()/obj.get() 에서 AttributeError 로 죽는다(실측: 토론 codex 턴).
    한 겹 배열은 풀어주고, 그래도 dict 가 아니면 파싱 실패로 취급한다."""
    out = parse_json(text)
    if isinstance(out, list):
        out = next((x for x in out if isinstance(x, dict)), None)
    if not isinstance(out, dict):
        raise ClaudeError(f"expected a JSON object, got {type(out).__name__}: {text[:200]}")
    return out


def run_json(prompt: str, **kw) -> dict:
    """Run claude and parse its reply as a JSON object."""
    return parse_obj(run(prompt, **kw))


def parse_json(text: str):
    """응답에서 완결된 JSON 객체를 뽑는다.

    첫 ``` 와 다음 ``` 사이를 그냥 자르면, 본문(proposal 등)에 코드펜스가 들어간
    순간 JSON 이 중간에서 끊긴다 — 실제로 토론의 제안자 응답 3개가 전부 이 경로로
    파싱에 실패했고, 폴백이 응답을 잘라 다음 턴이 반쪽 입력으로 논쟁했다.
    그래서 문자열 리터럴을 인식하며 중괄호 깊이를 세어 **완결된** 객체를 찾는다."""
    t = (text or "").strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(t)):
                ch = t[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(t[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = t.find(opener, start + 1)
    raise ClaudeError(f"could not parse JSON from claude reply: {text[:300]}")
