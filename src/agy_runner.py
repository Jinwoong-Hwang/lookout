"""Headless `agy` (Google Antigravity CLI) invocations for LLM review stages.

Read-only by construction: session-only `--settings` restricts permissions to
`read_file(*)` and denies write/command/MCP/network actions, `--disable-slash-commands`
prevents PR diff text from triggering slash-command or skill expansion, and
the target worktree is detached and never pushed (ADR-001, ADR-009).

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

READONLY_ALLOW = ("read_file(*)",)
READONLY_DENY = (
    "write_file(*)",
    "command(*)",
    "unsandboxed(*)",
    "mcp(*)",
    "read_url(*)",
    "execute_url(*)",
    "download_url(*)",
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


def _extract_result(stdout: str) -> str:
    """Parse `stream-json` NDJSON (or single JSON) output and return the final text."""
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
    return stdout


def run(prompt: str, cwd: str = None, add_dir: str = None, timeout: int = None,
        model: str = None, effort: str = None) -> str:
    """Run agy headless in read-only mode, returning the assistant's final text."""
    args = [
        AGY,
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
    return _extract_result(proc.stdout)
