"""Idempotency key builders.

ADR: every key MUST include OWNER/REPO. Multi-repo shares one board, so a bare
PR number collides across repos. head changes -> different review key.
"""


def root_key(repo: str, pr: int) -> str:
    return f"pr-auto-review:{repo}#{pr}"


def review_key(repo: str, pr: int, head_sha: str) -> str:
    return f"pr-auto-review:{repo}#{pr}:review:{head_sha}"


def approve_key(repo: str, pr: int, head_sha: str) -> str:
    return f"pr-auto-review:{repo}#{pr}:approve:{head_sha}"


def finding_fp(repo: str, pr: int, file: str, line, rule: str) -> str:
    """Stable fingerprint for dedupe across re-reviews (head-independent)."""
    return f"{repo}#{pr}:{file}:{line}:{rule}"
