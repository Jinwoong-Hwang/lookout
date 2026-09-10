"""impl verifier: 구현 diff를 반대편 엔진이 읽기 전용으로 검증한다.

쓰기 권한은 구현자 한쪽에만 준다(ADR-009의 정신 유지). 결함이 나오면 구현자에게
되돌리되 되돌림에도 라운드 상한을 둔다 — 상한이 없으면 두 엔진이 서로 미루며
토큰만 태운다.
"""
import json

from . import db, engines, prdiff, prompt_tpl, worktree
from .config import CFG

VERIFY_DIFF_CHARS = 60000
MAX_IMPL_ROUNDS = 2       # 구현 시도 총 횟수(최초 + 되돌림 1회)


def _opposite(engine: str) -> str:
    return "codex" if engine == "claude" else "claude"


def verifier_engine(impl_engine: str) -> tuple[str, bool]:
    """반대편 엔진이 검증한다.

    그쪽이 준비되지 않았으면 준비된 엔진으로 내려가되 그 사실을 이벤트에 남긴다 —
    검증을 건너뛰는 것보다 낫고, 같은 엔진이 자기 구현을 검증했다는 사실을 숨기는
    것은 더 나쁘다."""
    other = _opposite(impl_engine)
    if engines.is_ready(other):
        return other, False
    if engines.is_ready(impl_engine):
        return impl_engine, True
    raise RuntimeError("검증 가능한 엔진이 없다 — claude/codex 로그인 상태 확인")


def branch_diff(repo: str, branch: str) -> tuple[str, str]:
    """base...branch 3-dot diff. 워크트리가 아니라 부모 저장소에서 계산한다 —
    워크트리는 repo당 하나를 공유하므로 다른 카드가 브랜치를 바꿔놨을 수 있다."""
    parent = worktree.impl_parent(repo)
    base = worktree._impl_base_ref(parent, repo)
    raw = worktree._git(parent, "diff", f"{base}...{branch}").stdout
    packed, files, _ = prdiff.pack(raw, VERIFY_DIFF_CHARS)
    return packed, files


def process(c, card):
    meta = json.loads(card["payload"]) if card["payload"] else {}
    repo, branch = meta.get("target_repo"), meta.get("branch")
    if not repo or not branch:
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "impl_verify_no_branch", card["key"], {"payload_keys": list(meta)})
        return

    impl_engine = card["engine"] or "claude"
    vengine, fallback = verifier_engine(impl_engine)
    diff, files = branch_diff(repo, branch)
    if not diff.strip():
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "impl_verify_empty_diff", card["key"],
                     {"repo": repo, "branch": branch})
        return

    wt = worktree.make_impl_worktree(repo, branch, setup=False)
    impl = meta.get("impl") or {}
    prompt = prompt_tpl.render(
        "impl_verify.md",
        DISPLAY=meta.get("display") or f"#{card['pr_number']}",
        TITLE=meta.get("title", ""), TARGET_REPO=repo, BRANCH=branch,
        BODY=(meta.get("body") or "(생략)"),
        INSTRUCTION=(meta.get("instruction") or "(없음)"),
        IMPL_SUMMARY=(impl.get("summary") or "(요약 없음)"),
        DIFF=diff, FILES=files,
    )
    verdict = engines.run_json(prompt, engine=vengine, cwd=wt, add_dir=wt)
    blocking = verdict.get("blocking") or []
    out_of_scope = verdict.get("out_of_scope") or []
    approved = bool(verdict.get("approved")) and not blocking

    db.merge_payload(c, card["id"], {"verify": {
        "engine": vengine, "fallback": fallback, "approved": approved,
        "meets_requirement": verdict.get("meets_requirement"),
        "summary": verdict.get("summary", ""),
        "blocking": blocking[:20], "out_of_scope": out_of_scope[:20],
    }})
    db.log_event(c, "impl_verified", card["key"],
                 {"engine": vengine, "fallback": fallback, "approved": approved,
                  "blocking": len(blocking), "out_of_scope": len(out_of_scope)})

    if approved:
        # 사람 승인 게이트 — 여기서 멈춘다. PR은 사람이 누른 뒤에 올라간다.
        db.set_status(c, card["id"], "pr_blocked", blocked=1)
        return

    rounds = int(meta.get("impl_rounds") or 1)
    if rounds >= MAX_IMPL_ROUNDS:
        # 되돌림 예산 소진 — 사람이 보게 failed 레인에 세운다. 계속 돌리면
        # 두 엔진이 서로 미루며 토큰만 태운다.
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "impl_rounds_exhausted", card["key"],
                     {"rounds": rounds, "blocking": len(blocking)})
        return

    feedback = "\n".join(
        f"- {b.get('file','?')}:{b.get('line','?')} — {b.get('problem','')} / 고치는 방향: {b.get('fix','')}"
        for b in blocking) or (verdict.get("summary") or "")
    if out_of_scope:
        feedback += "\n- 스코프 밖 변경: " + ", ".join(out_of_scope)
    db.merge_payload(c, card["id"], {"impl_rounds": rounds + 1, "feedback": feedback})
    db.set_status(c, card["id"], "implementing")
    db.log_event(c, "impl_rework", card["key"], {"round": rounds + 1, "blocking": len(blocking)})
