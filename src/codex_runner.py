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
            raise CodexError(f"codex failed (rc={proc.returncode}): {proc.stderr.strip()[:500]}")
        with open(out_path, encoding="utf-8") as f:
            text = f.read().strip()
        return text or proc.stdout
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
