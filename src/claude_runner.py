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


class ClaudeError(RuntimeError):
    pass


def run(prompt: str, cwd: str = None, add_dir: str = None, timeout: int = 900,
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
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          env=config.subprocess_env())
    if proc.returncode != 0:
        raise ClaudeError(f"claude failed (rc={proc.returncode}): {proc.stderr.strip()[:500]}")
    try:
        env = json.loads(proc.stdout)
        return env.get("result", proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout


def run_json(prompt: str, **kw) -> dict:
    """Run claude and parse its reply as JSON (tolerates ```json fences)."""
    text = run(prompt, **kw)
    return parse_json(text)


def parse_json(text: str):
    t = text.strip()
    if "```" in t:
        # extract first fenced block
        start = t.find("```")
        nl = t.find("\n", start)
        end = t.find("```", nl + 1)
        if nl != -1 and end != -1:
            t = t[nl + 1:end].strip()
    # find outermost JSON object/array
    for opener, closer in (("{", "}"), ("[", "]")):
        s = t.find(opener)
        e = t.rfind(closer)
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(t[s:e + 1])
            except json.JSONDecodeError:
                continue
    raise ClaudeError(f"could not parse JSON from claude reply: {text[:300]}")
