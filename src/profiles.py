"""Repository-specific review policy helpers.

Policies are snapshotted onto review cards when they are created. Runtime stages
prefer that snapshot so an in-flight card does not change behavior if config is
edited later.
"""
import copy
import json

from . import config

# 엔진별 분리 이전에 쓰이던 단일 리뷰 프롬프트 이름(지금은 파일 없음).
LEGACY_REVIEW_PROMPT = "review.md"


CODE_POLICY = {
    "policy_schema_version": 1,
    "profile_type": "code",
    "mode": "normal",
    "approval_allowed": True,
    "comment_policy": "global",
    "max_findings": None,
    "min_confidence": None,
    "prompt_set": {
        # 리뷰 프롬프트는 엔진별로 분리. Codex는 review.codex.md, Claude는
        # review.claude.md(재현율 우선 + 경로/상태 열거). 출력 JSON 스키마만
        # 양쪽 공유(verifier/commenter 계약). verify/closure는 엔진 공통.
        #
        # 두 파일의 내용 차이는 drift가 아니라 의도다 — Codex는 넓은 커버리지
        # (UI/반응형·환경분기·mock 잔존 등 항목 나열), Claude는 좁고 확실한
        # 머지블로커(보고 전 게이트 통과분만). 한쪽 항목을 다른 쪽에 맞추려
        # 하기 전에 이 결정을 먼저 확인할 것.
        "review": {"claude": "review.claude.md", "codex": "review.codex.md"},
        "verify": "verify.md",
        "closure": "closure.md",
    },
    "no_finding_terminal": "lgtm",
    "no_confirmed_terminal": "lgtm",
    "large_pr_policy": "review",
    "large_pr": {"files": 20, "diff_lines": 3000, "epic_roots": 2},
    "no_finding_comment": False,
}


DOC_POLICY = {
    "policy_schema_version": 1,
    "profile_type": "doc",
    "mode": "comment_only",
    "approval_allowed": False,
    "comment_policy": "dry_run",
    "max_findings": 3,
    "min_confidence": "medium",
    "prompt_set": {
        "review": "doc_review.md",
        "verify": "doc_verify.md",
        "closure": "doc_closure.md",
    },
    "no_finding_terminal": "done",
    "no_confirmed_terminal": "done",
    "large_pr_policy": "summary_only",
    "large_pr": {"files": 20, "diff_lines": 3000, "epic_roots": 2},
    "no_finding_comment": False,
}


def _deep_update(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_update(out[key], value)
        else:
            out[key] = value
    return out


def _normalize(policy: dict) -> dict:
    if not isinstance(policy, dict):
        policy = {}
    ptype = policy.get("profile_type") or policy.get("type") or "code"
    base = DOC_POLICY if ptype == "doc" else CODE_POLICY
    merged = _deep_update(base, policy)
    merged["profile_type"] = ptype
    if merged.get("max_findings") is None:
        merged["max_findings"] = config.CFG.get("max_findings_per_review", 8)
    if merged.get("min_confidence") is None:
        merged["min_confidence"] = config.CFG.get("min_confidence", "medium")
    if merged.get("comment_policy") not in {"global", "dry_run", "post", "silent"}:
        merged["comment_policy"] = "global"
    allowed = merged.get("approval_allowed")
    if isinstance(allowed, str):
        allowed = allowed.strip().lower() == "true"
    merged["approval_allowed"] = allowed is True
    return merged


def policy_for_repo(repo: str) -> dict:
    profiles = config.CFG.get("repo_profiles") or {}
    override = profiles.get(repo) or {}
    return _normalize(override)


def policy_from_payload(payload) -> dict | None:
    if not payload:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    policy = (payload or {}).get("review_policy")
    if not isinstance(policy, dict):
        return None
    return _normalize(policy)


def policy_from_card(card) -> dict:
    return policy_from_payload(card["payload"]) or policy_for_repo(card["repo"])


def prompt_name(policy: dict, stage: str, engine: str = "") -> str:
    """Resolve one stage's prompt file.

    `review` may be an engine map ({"claude": ..., "codex": ...}); a plain string
    means every engine shares it. A repo profile can override with either shape.
    """
    name = (policy.get("prompt_set") or {}).get(stage) or CODE_POLICY["prompt_set"][stage]
    if name == LEGACY_REVIEW_PROMPT:
        # Cards created before the engine split snapshot the old single-file name
        # onto their payload; that file no longer exists.
        name = CODE_POLICY["prompt_set"][stage]
    if isinstance(name, dict):
        name = name.get(engine) or name.get("codex") or next(iter(name.values()))
    return name
