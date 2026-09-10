"""Idempotency key builders.

ADR: every key MUST include OWNER/REPO. Multi-repo shares one board, so a bare
PR number collides across repos. head changes -> different review key.
"""


def root_key(repo: str, pr: int) -> str:
    return f"pr-auto-review:{repo}#{pr}"


def review_key(repo: str, pr: int, head_sha: str) -> str:
    return f"pr-auto-review:{repo}#{pr}:review:{head_sha}"


def rereview_key(repo: str, pr: int, head_sha: str, source_card_id: int) -> str:
    return f"pr-auto-review:{repo}#{pr}:review:{head_sha}:rereview:{source_card_id}"


def approve_key(repo: str, pr: int, head_sha: str) -> str:
    return f"pr-auto-review:{repo}#{pr}:approve:{head_sha}"


def issue_key(repo: str, number: int) -> str:
    """이슈 작업 카드. PR 키와 프리픽스가 달라 번호가 같아도 충돌하지 않는다.
    (GitHub은 이슈와 PR이 번호를 공유하므로 repo#number 자체는 레포 안에서 유일)"""
    return f"issue-work:{repo}#{number}"


def topic_key(seq: int, ts: float) -> str:
    """이슈에 매달리지 않은 순수 토론 카드. repo#번호가 없으므로 시각+순번으로
    유일성을 만든다."""
    return f"topic:{int(ts)}-{seq}"


def finding_fp(repo: str, pr: int, file: str, line, rule: str) -> str:
    """Stable fingerprint for dedupe across re-reviews (head-independent)."""
    return f"{repo}#{pr}:{file}:{line}:{rule}"
