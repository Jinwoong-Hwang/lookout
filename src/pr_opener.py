"""pr_opener: 사람이 승인한 뒤에만 브랜치를 push하고 draft PR을 올린다.

PR은 생성 시점부터 draft다. 대상 저장소는 저장소 전체가 코드 소유자 팀으로 지정돼
있어 일반 상태로 만들면 팀 전원에게 리뷰가 자동 요청되고, 생성 후 draft로 내려도
이미 걸린 요청은 회수되지 않는다. ready 전환은 사람이 한다.
"""
import json

from . import db, ghclient, worktree
from .config import CFG


def _body(meta: dict, display: str) -> str:
    impl = meta.get("impl") or {}
    verify = meta.get("verify") or {}
    lines = [f"이슈: {meta.get('url') or display}", ""]
    if impl.get("summary"):
        lines += ["## 변경", impl["summary"], ""]
    if meta.get("instruction"):
        lines += ["## 운영자 지시", meta["instruction"], ""]
    if impl.get("verification"):
        lines += ["## 검증", impl["verification"], ""]
    if verify.get("summary"):
        engine = verify.get("engine", "")
        note = " (동일 엔진 폴백)" if verify.get("fallback") else ""
        lines += [f"## 교차 검증 — {engine}{note}", verify["summary"], ""]
    for q in (impl.get("open_questions") or []):
        lines.append(f"- [ ] 확인 필요: {q}")
    if impl.get("risk"):
        lines += ["", f"위험: {impl['risk']}"]
    lines += ["", f"🤖 Lookout 이 {display} 를 구현했습니다. draft 로 올라갑니다."]
    return "\n".join(lines)


def process(c, card):
    meta = json.loads(card["payload"]) if card["payload"] else {}
    repo, branch = meta.get("target_repo"), meta.get("branch")
    display = meta.get("display") or f"#{card['pr_number']}"
    if not repo or not branch:
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "pr_open_no_branch", card["key"], {"payload_keys": list(meta)})
        return

    parent = worktree.impl_parent(repo)
    base = worktree._impl_base_ref(parent, repo).replace("origin/", "")
    title = f"[{display}] {(meta.get('title') or '').strip()[:80]}"
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
    ghclient.issue_comment(card["repo"], card["pr_number"],
                           f"구현 PR(draft): {url}\n\n브랜치 `{branch}`")
    db.merge_payload(c, card["id"], {"pr_url": url})
    db.set_status(c, card["id"], "done")
    db.log_event(c, "pr_opened", card["key"], {"repo": repo, "branch": branch, "url": url})
