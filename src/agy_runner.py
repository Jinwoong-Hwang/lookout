"""Headless `agy` (Google Antigravity CLI) invocations for LLM review stages.

Read-only by construction: `--sandbox` + `unsandboxed(*)` deny keeps every
shell command inside the OS sandbox (no network, no access outside the
worktree/temp dirs), `--mode plan` disables auto-approval of arbitrary shell
commands (non-allowlisted commands and shell redirections `>` are auto-denied),
and session-only `--settings` restricts `allow` to `read_file(*)` plus pure
stdout commands that have no arbitrary-exec or file-write flags.
`--disable-slash-commands` prevents PR diff text from triggering slash-command
or skill expansion, and the target worktree is detached and never pushed
(ADR-001, ADR-009).

Prompts are sent over stdin as NDJSON (`--input-format stream-json`) so large
120KB+ PR diffs never hit OS `ARG_MAX` limits or non-TTY stdin hangs.
"""
import json
import subprocess

from . import config

CFG = config.CFG
AGY = config.resolve_bin(CFG.get("agy_bin", "agy"))
MODEL = CFG.get("agy_model")    # None -> agy default
EFFORT = CFG.get("agy_effort")  # low|medium|high|xhigh|max, None -> agy default
RUN_TIMEOUT = int(CFG.get("agy_timeout", 1800))

# agy는 모델에 별도 Grep/Glob 도구 대신 read_file + run_command를 노출하고,
# command(...) 규칙은 토큰 프리픽스 매칭을 사용한다(파이프된 각 명령도 개별 검사,
# `>` 리다이렉션은 별도 차단). 임의 실행·파일 쓰기 옵션이 없는 순수 stdout 명령만
# allow하고, 모델이 자주 시도하지만 위험 옵션이 있는 명령(rg --pre, git grep -O,
# git diff/log --output, find -exec/-fprint 등)은 명시적 deny로 두어 턴 중단 대신
# TOOL_ERROR를 받고 grep/git ls-files로 즉시 폴백하게 한다.
READONLY_ALLOW = (
    "read_file(*)",
    "command(grep)",
    "command(ls)",
    "command(git ls-files)",
    "command(head)",
    "command(tail)",
    "command(wc)",
    "command(cat)",
)
READONLY_DENY = (
    "write_file(*)",
    "unsandboxed(*)",
    "mcp(*)",
    "read_url(*)",
    "execute_url(*)",
    "download_url(*)",
    # 위험 옵션이 있는 탐색·git 명령 (명시적 deny → TOOL_ERROR 후 grep/git ls-files로 폴백)
    "command(rg)",
    "command(find)",
    "command(sort)",
    "command(uniq)",
    "command(tree)",
    "command(sed)",
    "command(awk)",
    "command(git grep)",
    "command(git diff)",
    "command(git log)",
    "command(git show)",
    "command(git status)",
    "command(git blame)",
    "command(git branch)",
    # 인터프리터·쉘·테스트 러너·변경/네트워크 명령 (ask로 빠지면 헤드리스 턴이 중단되므로
    # 명시적 deny로 돌려 정적 코드 리뷰로 계속 진행하게 한다)
    "command(sh)",
    "command(bash)",
    "command(zsh)",
    "command(python)",
    "command(python3)",
    "command(node)",
    "command(bun)",
    "command(deno)",
    "command(ruby)",
    "command(perl)",
    "command(npm)",
    "command(npx)",
    "command(pnpm)",
    "command(yarn)",
    "command(pytest)",
    "command(jest)",
    "command(vitest)",
    "command(tsc)",
    "command(eslint)",
    "command(make)",
    "command(go)",
    "command(cargo)",
    "command(swift)",
    "command(curl)",
    "command(wget)",
    "command(gh)",
    "command(rm)",
    "command(mv)",
    "command(cp)",
    "command(touch)",
    "command(mkdir)",
)
READONLY_SETTINGS = json.dumps(
    {"permissions": {"allow": list(READONLY_ALLOW), "deny": list(READONLY_DENY)}},
    separators=(",", ":"),
)


class AgyError(RuntimeError):
    pass


# glog 정보 줄이나 스택트레이스 프레임은 걷어내고 실제 실패 원인이 남는 뒤쪽을 보존한다.
_NOISE_PREFIXES = (
    "I0", "I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9",
    "-- stack trace:", "| ", "\t", "runtime.", "google3/", "Wraps:", "Error types:",
    "warning: --mode plan has no effect",
)


def _clean_stderr(text: str, limit: int = 500) -> str:
    out, seen = [], set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(_NOISE_PREFIXES) or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return (" | ".join(out) or (text or "").strip())[-limit:]


def _extract_result(stdout: str, stderr: str = "") -> str:
    """Parse `stream-json` NDJSON (or single JSON) output and return the final text.

    Fail closed when stream-json events were seen but no non-empty response or
    agent_response delta was emitted — returning raw NDJSON would let parse_obj
    pick up the first metadata event (e.g. `{"event": "init", ...}`) as a review
    reply with zero findings (false LGTM).
    """
    saw_event = False
    result_obj = None
    deltas = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        event = obj.get("event")
        if event is not None:
            saw_event = True
        if event == "result" and isinstance(obj.get("result"), dict):
            result_obj = obj["result"]
        elif event == "step_update" and isinstance(obj.get("step_update"), dict):
            su = obj["step_update"]
            if su.get("step_type") == "agent_response" and isinstance(su.get("text_delta"), str):
                deltas.append(su["text_delta"])
        elif event == "error":
            err = obj.get("error") or obj.get("message") or line
            raise AgyError(f"agy stream error: {str(err)[:500]}")
        elif "response" in obj and result_obj is None:
            saw_event = True
            result_obj = obj

    if result_obj is not None:
        status = str(result_obj.get("status") or "").upper()
        if status and status != "SUCCESS":
            detail = result_obj.get("error") or result_obj.get("message") or result_obj.get("response") or ""
            raise AgyError(f"agy failed (status={status}): {str(detail).strip()[-500:]}")
        if result_obj.get("error"):
            raise AgyError(f"agy failed: {str(result_obj['error']).strip()[-500:]}")
        resp = result_obj.get("response")
        if isinstance(resp, str) and resp.strip():
            return resp
    if deltas:
        joined = "".join(deltas)
        if joined.strip():
            return joined
    if saw_event or not (stdout or "").strip():
        detail = _clean_stderr(stderr)
        suffix = f": {detail}" if detail else ""
        raise AgyError(f"agy returned no response{suffix}")
    return stdout


def run(prompt: str, cwd: str = None, add_dir: str = None, timeout: int = None,
        model: str = None, effort: str = None) -> str:
    """Run agy headless in read-only mode, returning the assistant's final text."""
    args = [
        AGY,
        "--mode", "plan",
        "--sandbox",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--disable-slash-commands",
        "--settings", READONLY_SETTINGS,
    ]
    mdl = MODEL if model is None else model
    if mdl:
        args += ["--model", mdl]
    eff = EFFORT if effort is None else effort
    if eff:
        args += ["--effort", eff]
    # worktree.kill_review_process()는 argv의 worktree 경로(`<slug>__pr<pr>__`)로
    # 프로세스를 찾으므로 --add-dir에 작업 디렉토리를 항상 실어준다.
    target_dir = add_dir or cwd
    if target_dir:
        args += ["--add-dir", target_dir]

    ndjson_in = json.dumps(
        {"event": "user", "message": {"content": prompt}},
        ensure_ascii=False,
    ) + "\n"
    proc = subprocess.run(
        args,
        cwd=cwd,
        input=ndjson_in,
        capture_output=True,
        text=True,
        timeout=timeout or RUN_TIMEOUT,
        env=config.subprocess_env(),
    )
    if proc.returncode != 0:
        detail = _clean_stderr(proc.stderr) or _clean_stderr(proc.stdout)
        raise AgyError(f"agy failed (rc={proc.returncode}): {detail}")
    return _extract_result(proc.stdout, proc.stderr)
