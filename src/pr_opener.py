"""pr_opener: 사람이 승인한 뒤에만 브랜치를 push하고 draft PR을 올린다.

PR은 생성 시점부터 draft다. 대상 저장소는 저장소 전체가 코드 소유자 팀으로 지정돼
있어 일반 상태로 만들면 팀 전원에게 리뷰가 자동 요청되고, 생성 후 draft로 내려도
이미 걸린 요청은 회수되지 않는다. ready 전환은 사람이 한다.
"""
import json
import re

from . import db, ghclient, worktree
from .config import CFG


# 팀 관례는 Conventional Commits 다 — 실측(zigbang-client 최근 머지):
#   fix(PH-1609): PC웹 매물리스트 랜딩에서 utm 유실 수정
#   chore(PH-1609): zigbang-www 테스트를 CI 게이트에 연결
# 우리만 `[PH-1816] …` 로 올리면 PR 목록에서 혼자 튄다.
PR_TYPES = ("fix", "feat", "refactor", "chore", "docs", "test",
            "perf", "style", "build", "ci")
TITLE_MAX = 72


def _title(meta: dict, display: str) -> str:
    impl = meta.get("impl") or {}
    kind = (impl.get("pr_type") or "").strip().lower()
    if kind not in PR_TYPES:
        kind = "fix"   # 엔진이 엉뚱한 값을 주면 형식을 깨느니 가장 흔한 쪽으로
    prefix = f"{kind}({display}): "
    subject = (meta.get("title") or "").strip()
    # 이슈 제목의 [FE][CEO_APP] 같은 태그는 scope 가 이미 하는 일이라 뺀다
    subject = re.sub(r"^(\s*\[[^\]]+\])+\s*", "", subject)
    room = TITLE_MAX - len(prefix)
    if len(subject) > room:
        subject = subject[:max(room - 1, 1)].rstrip() + "…"
    return prefix + (subject or display)


def _checklist() -> list[str]:
    """ready 전환 전에 **사람이** 확인할 것. 엔진의 자기 보고(## 검증)와 다른 축이라
    체크된 채로 내보내지 않는다 — 우리 PR 은 draft 로 나가고, 이 목록은 draft 를
    ready 로 올리는 사람이 쓴다."""
    return ["빌드 성공 확인", "린트/타입 체크 통과",
            "관련 테스트 통과 또는 작성", "관련 문서 업데이트"]


def _body(meta: dict, display: str) -> str:
    impl = meta.get("impl") or {}
    verify = meta.get("verify") or {}
    lines = [f"이슈: {meta.get('url') or display}", ""]
    if impl.get("summary"):
        lines += ["## 변경 요약", impl["summary"], ""]
    # '해볼 것'과 '정할 것'은 다른 목록이다. 한데 묶으면 리뷰어가 '무엇을 해봐야
    # 하나'를 찾지 못하고, 결정 사항이 테스트 항목으로 위장된다.
    todo = [q for q in (impl.get("manual_test") or []) if str(q).strip()]
    if todo:
        lines += ["## 테스트 방법"] + [f"- [ ] {q}" for q in todo] + [""]
    if impl.get("verification"):
        lines += ["## 검증 — 엔진이 실행함", impl["verification"], ""]
    if verify.get("summary"):
        engine = verify.get("engine", "")
        note = " (동일 엔진 폴백)" if verify.get("fallback") else ""
        lines += [f"## 교차 검증 — {engine}{note}", verify["summary"], ""]
    # 설계 토론이 끝내 합의하지 못한 것들. 카드에만 두면 PR 리뷰어는 이 다툼이
    # 있었다는 사실조차 모른다 — 승인 화면에서 사라지는 대신 본문에 남긴다.
    unresolved = (meta.get("agreement") or {}).get("unresolved") or []
    if unresolved:
        lines += ["## 설계 단계 미합의 — 리뷰에서 판단 필요"]
        lines += [f"- [ ] {u}" for u in unresolved]
        lines.append("")
    if meta.get("instruction"):
        lines += ["## 운영자 지시", meta["instruction"], ""]
    decisions = [q for q in (impl.get("open_questions") or []) if str(q).strip()]
    if decisions:
        lines += ["## 남은 결정"] + [f"- [ ] {q}" for q in decisions] + [""]
    if impl.get("risk"):
        lines += ["## 위험", impl["risk"], ""]
    lines += ["## 체크리스트 (ready 전환 전 확인)"]
    lines += [f"- [ ] {x}" for x in _checklist()]
    lines += ["", f"🤖 Lookout 이 {display} 를 구현했습니다. draft 로 올라갑니다."]
    return "\n".join(lines)


def process(c, card):
    meta = json.loads(card["payload"]) if card["payload"] else {}
    repo, branch = meta.get("target_repo"), meta.get("branch")
    display = meta.get("display") or f"#{card['pr_number']}"
    if not repo or not branch:
        db.merge_payload(c, card["id"], {"failed_from": "pr_opening"})
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "pr_open_no_branch", card["key"], {"payload_keys": list(meta)})
        return

    parent = worktree.impl_parent(repo)
    base = worktree._impl_base_ref(parent, repo).replace("origin/", "")
    title = _title(meta, display)
    body = _body(meta, display)

    if CFG.get("dry_run_pr", True):
        # 기본값이 dry-run이다. 첫 배포에서 사람 모르게 PR이 나가면 안 된다.
        db.merge_payload(c, card["id"], {"pr_dryrun": {"title": title, "body": body,
                                                       "base": base, "head": branch}})
        db.set_status(c, card["id"], "done")
        db.log_event(c, "pr_dryrun", card["key"],
                     {"repo": repo, "branch": branch, "base": base, "title": title})
        return

    worktree._git(parent, "push", "--quiet", "-u", "origin", branch)
    url = ghclient.pr_create_draft(repo, base, branch, title, body)
    if card["pr_number"]:
        # 주제 카드(repo='-', pr_number=0)는 코멘트할 이슈가 없다
        ghclient.issue_comment(card["repo"], card["pr_number"],
                               f"구현 PR(draft): {url} · 브랜치 `{branch}`")
    else:
        db.log_event(c, "pr_issue_comment_skipped", card["key"],
                     {"reason": "이슈 없는 주제 카드"})
    db.merge_payload(c, card["id"], {"pr_url": url})
    db.set_status(c, card["id"], "done")
    db.log_event(c, "pr_opened", card["key"], {"repo": repo, "branch": branch, "url": url})
