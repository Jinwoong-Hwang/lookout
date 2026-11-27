"""Review-engine dispatcher. A card's `engine` selects which LLM CLI runs the
review/verify stages: 'claude' (Claude Code), 'codex' (OpenAI Codex),
or 'agy' (Google Antigravity CLI)."""
from functools import reduce
import json
import math
import os
import random
import re
import subprocess
import time

from . import agy_runner, claude_runner, codex_runner, config

ENGINES = ("claude", "codex", "agy")


class EngineError(RuntimeError):
    pass


# ── 쿼터/레이트리밋 판별 ─────────────────────────────────────────────────────
# 이건 "코드가 틀려서 실패"가 아니라 "지금은 못 돈다"이다. 재시도 예산을 태우고
# 카드를 죽이는 대신 대기목록으로 돌려보내야 한다. 숫자 429는 sha/경로에 우연히
# 섞일 수 있어 단독으로는 쓰지 않는다.
_QUOTA_MARKERS = (
    "usage limit",          # codex: You've hit your usage limit
    "usage_limit",
    "limit reached",        # claude: Claude usage limit reached
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota",
    "resource_exhausted",   # agy/gemini: RESOURCE_EXHAUSTED
    "resource exhausted",
    "credit balance",
    "out of credits",
)


def is_quota_error(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _QUOTA_MARKERS)


def quota_retry_hint(text: str) -> str:
    """엔진이 알려주는 재시도 시각(예: "try again at 8:25 PM")을 그대로 뽑아온다."""
    m = re.search(r"try again (?:at|after|in) ([^.\n|]{1,40})", text or "", re.I)
    return m.group(1).strip() if m else ""


def run(prompt: str, engine: str = "claude", **kw) -> str:
    if engine == "codex":
        return codex_runner.run(prompt, **kw)
    if engine == "agy":
        return agy_runner.run(prompt, **kw)
    return claude_runner.run(prompt, **kw)


def run_json(prompt: str, engine: str = "claude", **kw) -> dict:
    """엔진 응답을 dict 로 돌려준다 — 모델이 배열로 답해도 호출부가 죽지 않게."""
    return claude_runner.parse_obj(run(prompt, engine=engine, **kw))


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


def _agy_logged_in() -> bool:
    # 1) Gemini API key 모드 (env 또는 env_file)
    if config.subprocess_env().get("GEMINI_API_KEY"):
        return True
    # 2) 온보딩 완료 상태 파일 (~/.gemini/antigravity-cli/jetski_state.pbtxt)
    state_path = os.path.expanduser("~/.gemini/antigravity-cli/jetski_state.pbtxt")
    try:
        with open(state_path, encoding="utf-8", errors="replace") as f:
            if "post_onboarding" in f.read():
                return True
    except OSError:
        pass
    # 3) macOS Keychain 항목 존재 — -w 없이 메타만 조회하므로 프롬프트/시크릿 노출 없음
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "gemini"],
            capture_output=True, timeout=4)
        if r.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


_LOGIN_FN = {"claude": _claude_logged_in, "codex": _codex_logged_in, "agy": _agy_logged_in}


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


def _weighted_schedule(candidates: list[tuple[str, int]]) -> list[str]:
    """Build an interleaved deterministic schedule matching integer weights."""
    divisor = reduce(math.gcd, (w for _, w in candidates))
    counts = [(e, max(1, w // divisor)) for e, w in candidates]
    total = sum(w for _, w in counts)
    current = {e: 0 for e, _ in counts}
    weights = dict(counts)
    schedule = []
    for _ in range(total):
        for e in current:
            current[e] += weights[e]
        best = max(current, key=current.get)
        current[best] -= total
        schedule.append(best)
    return schedule


def _pick_weighted(candidates: list[tuple[str, int]], c=None) -> str:
    if len(candidates) == 1:
        return candidates[0][0]
    if c is not None:
        schedule = _weighted_schedule(candidates)
        try:
            row = c.execute("SELECT v FROM meta WHERE k='default_engine_rr'").fetchone()
            idx = int(row["v"]) if row and row["v"] is not None else 0
            chosen = schedule[idx % len(schedule)]
            c.execute(
                "INSERT INTO meta(k,v) VALUES('default_engine_rr',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(idx + 1),),
            )
            return chosen
        except Exception:  # noqa: BLE001 - fall back to weighted random
            pass
    return random.choices(
        [e for e, _ in candidates],
        weights=[w for _, w in candidates],
        k=1,
    )[0]


def default_engine(c=None) -> str:
    """Engine for automatic review cards.

    Supports optional `default_review_engine_weights` (e.g. {"codex": 50, "agy": 50})
    using a DB-persisted balanced round-robin when `c` is given (so short-lived
    tick processes maintain the exact ratio), falling back to weighted random.
    Otherwise prefers a ready configured `default_review_engine`.
    """
    avail = availability()
    weights_cfg = config.CFG.get("default_review_engine_weights")
    if isinstance(weights_cfg, dict) and weights_cfg:
        candidates = []
        for e, w in weights_cfg.items():
            if e in ENGINES and avail.get(e, {}).get("ready"):
                try:
                    wi = int(w)
                except (TypeError, ValueError):
                    wi = 0
                if wi > 0:
                    candidates.append((e, wi))
        if candidates:
            return _pick_weighted(candidates, c=c)

    configured = config.CFG.get("default_review_engine")
    if configured in ENGINES and avail.get(configured, {}).get("ready"):
        return configured
    ready = [e for e in ENGINES if avail.get(e, {}).get("ready")]
    if len(ready) == 1:
        return ready[0]
    if configured in ENGINES:
        return configured
    return "claude"
