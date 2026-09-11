"""impl verifier: 구현 diff를 반대편 엔진이 읽기 전용으로 검증한다.

쓰기 권한은 구현자 한쪽에만 준다(ADR-009의 정신 유지). 결함이 나오면 구현자에게
되돌리되 되돌림에도 라운드 상한을 둔다 — 상한이 없으면 두 엔진이 서로 미루며
토큰만 태운다.
"""
import json

from . import db, engines, notify, prdiff, prompt_tpl, worktree
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


def blockers_text(verify: dict) -> str:
    """검증 결과를 구현자가 읽을 지적 목록으로. 되돌림 경로와 사람의 수정 요청이
    같은 문구를 쓰도록 한 곳에 둔다."""
    lines = [
        f"- {b.get('file', '?')}:{b.get('line', '?')} — {b.get('problem', '')}"
        f" / 고치는 방향: {b.get('fix', '')}"
        for b in (verify.get("blocking") or [])
    ]
    if verify.get("out_of_scope"):
        lines.append("- 스코프 밖 변경: " + ", ".join(verify["out_of_scope"]))
    return "\n".join(lines) or (verify.get("summary") or "")


def process(c, card):
    meta = json.loads(card["payload"]) if card["payload"] else {}
    repo, branch = meta.get("target_repo"), meta.get("branch")
    if not repo or not branch:
        db.merge_payload(c, card["id"], {"failed_from": "impl_verify"})
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "impl_verify_no_branch", card["key"], {"payload_keys": list(meta)})
        return

    impl_engine = card["engine"] or "claude"
    vengine, fallback = verifier_engine(impl_engine)
    diff, files = branch_diff(repo, branch)
    if not diff.strip():
        db.merge_payload(c, card["id"], {"failed_from": "impl_verify"})
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "impl_verify_empty_diff", card["key"],
                     {"repo": repo, "branch": branch})
        return

    db.log_event(c, "impl_verify_started", card["key"],
                 {"engine": vengine, "fallback": fallback, "branch": branch})
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
    # 워크트리는 repo당 하나를 공유한다 — 다른 카드가 브랜치를 바꿔치기하지 못하게
    # 워크트리 준비부터 엔진 종료까지 락 안에서 돈다.
    with worktree.impl_session(repo):
        wt = worktree.make_impl_worktree(repo, branch, setup=False)
        verdict = engines.run_json(prompt, engine=vengine, cwd=wt, add_dir=wt)
    reverify_only = bool(meta.get("reverify_only"))
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
        # 검증을 통과했다 — 여기서부터가 "PR 올릴 준비가 됐다"는 뜻이다.
        db.merge_payload(c, card["id"], {"verify_exhausted": False, "reverify_only": False})
        db.set_status(c, card["id"], "pr_blocked", blocked=1)
        return

    if reverify_only:
        # 사람이 '다시 검증'만 요청한 경우 — 새 구현이 없었으므로 라운드를 쓰지 않고
        # 결과만 갱신해 검토 게이트로 되돌린다.
        db.merge_payload(c, card["id"], {
            "reverify_only": False, "verify_exhausted": True,
            "feedback": f"[검증 미해결] {blockers_text(verdict)}"})
        db.set_status(c, card["id"], "verify_blocked", blocked=1)
        db.log_event(c, "reverify_done", card["key"], {"blocking": len(blocking)})
        return

    rounds = int(meta.get("impl_rounds") or 1)
    # 사람이 수정을 요청하면 예산을 늘려준다 — 안 늘리면 요청하자마자 소진되어
    # failed 로 떨어진다(자동 되돌림 상한과 사람의 요청은 다른 축이다).
    if rounds >= MAX_IMPL_ROUNDS + int(meta.get("impl_bonus") or 0):
        # 되돌림 예산 소진. **실패가 아니다** — 커밋도 있고 검증 의견도 있으며,
        # 엔진 둘이 합의를 못 했을 뿐이다. failed 로 보내면 크래시처럼 보이고
        # 사람이 그 diff 를 판단할 기회를 잃는다. 사람 게이트로 올려 블로커를
        # 보여주고 승인·재요청·중단을 고르게 한다.
        # 최신 블로커를 feedback 에 남긴다 — 사람이 '수정 요청'을 누를 때 구현자가
        # 받아야 할 것은 게이트를 막은 **이번** 지적이다. 안 남기면 직전 라운드의
        # (이미 고친) 지적이 그대로 다시 나간다.
        db.merge_payload(c, card["id"], {
            "verify_exhausted": True,
            "feedback": f"[검증 미해결] {blockers_text(verdict)}",
        })
        # PR 게이트가 아니라 **검토 게이트**다. PR 승인 대기는 "올릴 준비가 됐다"는
        # 뜻이어야 하고, 여기는 "엔진이 합의하지 못했으니 사람이 봐야 한다"이다.
        db.set_status(c, card["id"], "verify_blocked", blocked=1)
        db.log_event(c, "impl_rounds_exhausted", card["key"],
                     {"rounds": rounds, "blocking": len(blocking), "to": "verify_blocked"})
        notify.send(
            f"Lookout — {meta.get('display') or card['pr_number']} 검증 미통과",
            f"엔진끼리 합의 못 함 · 블로커 {len(blocking)}건 · {rounds}라운드",
            subtitle="검토 필요 — 다시 검증 / 수정 요청 / 그래도 PR",
            group="lookout-impl",
        )
        return

    db.merge_payload(c, card["id"],
                     {"impl_rounds": rounds + 1, "feedback": blockers_text(verdict)})
    db.set_status(c, card["id"], "implementing")
    db.log_event(c, "impl_rework", card["key"], {"round": rounds + 1, "blocking": len(blocking)})
