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


def finding_fp(repo: str, pr: int, file: str, line, rule: str) -> str:
    """Stable fingerprint for dedupe across re-reviews (head-independent).

    line 은 넣지 않는다. 원장을 보고 rule 을 재사용하게 만들자(#10066 Phase 3)
    바로 이 함정이 드러났다 — 리뷰어가 같은 rule
    (failed-reclaim-leaves-pending-deletion)을 정확히 재사용했는데 인용 구간이
    89-94 에서 87-94 로 두 줄 밀려, 같은 문제가 두 지문으로 갈려 한 묶음에 두 번
    올라갔다. 줄 번호는 커밋마다 움직이므로 같음/다름의 기준이 될 수 없다.

    line 은 findings.line 컬럼과 마커 본문에 그대로 남아 사람이 위치를 찾는 데
    쓰인다. 지문에서만 뺀다.
    """
    return f"{repo}#{pr}:{file}:{rule}"
