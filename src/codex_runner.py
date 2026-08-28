"""Headless `codex exec` invocations (read-only sandbox).

Mirrors claude_runner: read-only review engine. Captures the agent's final
message via `-o <file>` for reliable parsing.
"""
import os
import subprocess
import tempfile

from . import config

CFG = config.CFG
CODEX = config.resolve_bin(CFG.get("codex_bin", "codex"))
MODEL = CFG.get("codex_model")  # None -> codex default


class CodexError(RuntimeError):
    pass


# codex는 stderr 앞부분에 배너/MCP 연결 실패/훅 로그를 쏟고, 진짜 실패 사유(usage
# limit, 인증 만료 등)는 맨 끝 줄에 찍는다. 앞에서 자르면 모든 실패가 똑같은
# "rmcp transport closed"로 보여 원인 파악이 불가능해진다 — 잡음을 걷고 뒤에서 남긴다.
_NOISE = ("rmcp::transport", "Reading additional input from stdin", "hook: ")
_BANNER = ("OpenAI Codex v", "--------", "workdir:", "model:", "provider:",
           "approval:", "sandbox:", "reasoning effort:", "reasoning summaries:",
           "session id:", "tokens used:")


def _clean_stderr(text: str, limit: int = 500) -> str:
    out, seen = [], set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(_BANNER) or any(n in s for n in _NOISE) or s in seen:
            continue
        seen.add(s)  # 같은 ERROR 줄을 여러 번 찍으므로 중복 제거
        out.append(s)
    return (" | ".join(out) or (text or "").strip())[-limit:]


def run(prompt: str, cwd: str = None, add_dir: str = None, timeout: int = 1200) -> str:
    out_fd, out_path = tempfile.mkstemp(suffix=".txt", prefix="codex_")
    os.close(out_fd)
    args = [
        CODEX, "exec",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color", "never",
        "-o", out_path,
    ]
    if cwd:
        args += ["-C", cwd]
    if MODEL:
        args += ["-m", MODEL]
    args.append(prompt)
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env=config.subprocess_env())
        if proc.returncode != 0:
            raise CodexError(f"codex failed (rc={proc.returncode}): {_clean_stderr(proc.stderr)}")
        with open(out_path, encoding="utf-8") as f:
            text = f.read().strip()
        return text or proc.stdout
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
