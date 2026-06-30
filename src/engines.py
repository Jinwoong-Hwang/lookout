"""Review-engine dispatcher. A card's `engine` selects which LLM CLI runs the
review/verify stages: 'claude' (Claude Code) or 'codex' (OpenAI Codex)."""
import json
import os
import subprocess
import time

from . import claude_runner, codex_runner, config

ENGINES = ("claude", "codex")


class EngineError(RuntimeError):
    pass


def run(prompt: str, engine: str = "claude", **kw) -> str:
    if engine == "codex":
        return codex_runner.run(prompt, **kw)
    return claude_runner.run(prompt, **kw)


def run_json(prompt: str, engine: str = "claude", **kw):
    text = run(prompt, engine=engine, **kw)
    return claude_runner.parse_json(text)


# ── 가용성 검사 (설치 + 로그인) ──────────────────────────────────────────────
# 대시보드가 "안 되는 엔진" 버튼을 비활성화하는 데 사용. 매 5초 폴링이 security
# 서브프로세스를 반복 호출하지 않도록 짧게 캐시한다.
_CACHE = {"at": 0.0, "data": None}
_TTL = 60.0


def _installed(bin_name: str) -> bool:
    """resolve_bin이 실제 실행 가능한 절대경로를 찾으면 설치된 것으로 본다.
    (못 찾으면 bare 이름을 그대로 돌려주므로 절대경로+존재 여부로 구분)"""
    resolved = config.resolve_bin(bin_name)
    return os.path.isabs(resolved) and os.path.isfile(resolved) and os.access(resolved, os.X_OK)


def _claude_logged_in() -> bool:
    # 1) credentials 파일 (Linux/일부 설정)
    if os.path.isfile(os.path.expanduser("~/.claude/.credentials.json")):
        return True
    # 2) ~/.claude.json 의 oauthAccount.emailAddress (로그인 1회 이상 시 캐시됨)
    try:
        with open(os.path.expanduser("~/.claude.json"), encoding="utf-8") as f:
            if ((json.load(f).get("oauthAccount") or {}).get("emailAddress")):
                return True
    except (OSError, ValueError):
        pass
    # 3) macOS Keychain 항목 존재 — -w 없이 메타만 조회하므로 프롬프트/시크릿 노출 없음
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
            capture_output=True, timeout=4)
        if r.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def _codex_logged_in() -> bool:
    p = os.path.expanduser("~/.codex/auth.json")
    try:
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False


_LOGIN_FN = {"claude": _claude_logged_in, "codex": _codex_logged_in}


def _status(engine: str) -> dict:
    bin_name = config.CFG.get(f"{engine}_bin", engine)
    inst = _installed(bin_name)
    li = bool(inst and _LOGIN_FN[engine]())
    return {"installed": inst, "logged_in": li, "ready": bool(inst and li)}


def availability(force: bool = False) -> dict:
    """{engine: {installed, logged_in, ready}} — 60초 캐시."""
    now = time.time()
    if not force and _CACHE["data"] is not None and (now - _CACHE["at"]) < _TTL:
        return _CACHE["data"]
    data = {e: _status(e) for e in ENGINES}
    _CACHE["at"], _CACHE["data"] = now, data
    return data


def is_ready(engine: str) -> bool:
    return availability().get(engine, {}).get("ready", False)
