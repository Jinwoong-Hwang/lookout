"""impl worker: 이슈 하나를 대상 저장소의 브랜치에 구현한다 (첫 쓰기 경로).

엔진은 편집만 한다 — 커밋은 이 모듈이 한다. 그래야 커밋 메시지·attribution이
일관되고, 엔진에 git 권한을 줄 이유가 없어진다. PR도 여기서 올리지 않는다:
카드는 impl_verify로 넘어가고, PR은 사람이 승인한 뒤에 올라간다.
"""
import json
import re
import subprocess
import time

from . import claude_runner, db, engines, ghclient, prompt_tpl, worktree
from .config import CFG

BODY_CHARS = 12000        # 이슈 본문 프롬프트 예산
GIT_TIMEOUT = 120


class TargetUnknown(RuntimeError):
    """어느 저장소에 구현할지 정할 수 없다 — 사람이 골라야 한다."""


def _norm(tag: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (tag or "").upper())


def target_repo(meta: dict) -> str:
    """대상 저장소. 사람이 고른 payload.target_repo가 최우선.

    없으면 제목 태그, 그 다음 라벨을 본다. 라벨만 믿을 수는 없다 — 실측에서 [FE]
    이슈들은 라벨이 비어 있고 Service::/Platform:: 같은 라벨은 앱·기획 이슈에만
    붙는다. 그래서 제목 태그를 먼저 보고 라벨은 태그가 없는 이슈를 구제하는 보조로 쓴다.
    태그 표기가 TALK-CEO / TALK_CEO 로 흔들리므로 영숫자만 남겨 비교한다."""
    explicit = (meta.get("target_repo") or "").strip()
    if explicit:
        return explicit
    tmap = {_norm(k): v for k, v in (CFG.get("impl_target_map") or {}).items()}
    for tag in re.findall(r"\[([^\]]+)\]", meta.get("title") or ""):
        hit = tmap.get(_norm(tag))
        if hit:
            return hit
    for label in (meta.get("labels") or []):
        # Service::ZB → ZB. 접두어 없는 라벨도 그대로 시도한다.
        hit = tmap.get(_norm(label.split("::")[-1]))
        if hit:
            return hit
    raise TargetUnknown(
        "제목 태그·라벨로 대상 저장소를 정할 수 없다 — 카드에서 고르거나 impl_target_map에 추가")


def _agreement_text(meta: dict) -> str:
    """설계 토론을 거친 카드면 승인된 합의문을 프롬프트에 싣는다."""
    ag = meta.get("agreement") or {}
    if not ag.get("design"):
        return "(설계 토론 없이 바로 구현)"
    parts = [ag["design"]]
    if meta.get("spec_amendment"):
        # 사람이 승인하면서 붙인 수정 지시 — 합의문보다 우선한다
        parts.append("운영자 수정 지시(최우선, 합의문과 충돌하면 이쪽): "
                     + meta["spec_amendment"])
    if ag.get("unresolved"):
        parts.append("남은 결정: " + " / ".join(ag["unresolved"]))
    if ag.get("risk"):
        parts.append("알려진 위험: " + ag["risk"])
    return "\n\n".join(parts)


def _git(wt: str, *args, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def changed_files(wt: str) -> list[str]:
    """워크트리의 미커밋 변경. ignored(node_modules)는 status에 안 나온다."""
    return [line[3:].strip() for line in _git(wt, "status", "--porcelain").splitlines()
            if line.strip()]


def _commit(wt: str, meta: dict, display: str, summary: str) -> str:
    title = (meta.get("title") or "").strip()
    body = []
    if summary.strip():
        body.append(summary.strip())
    if (meta.get("instruction") or "").strip():
        body.append(f"운영자 지시: {meta['instruction'].strip()}")
    if meta.get("url"):
        body.append(f"Refs: {meta['url']}")
    msg = f"{display}: {title[:72]}"
    if body:
        msg += "\n\n" + "\n\n".join(body)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", msg)
    return _git(wt, "rev-parse", "HEAD").strip()[:10]


def process(c, card):
    meta = json.loads(card["payload"]) if card["payload"] else {}
    display = meta.get("display") or f"#{card['pr_number']}"
    try:
        repo = target_repo(meta)
    except TargetUnknown as e:
        # 재시도해도 결과가 같다 — 예외로 올려 3번 태우지 않고 바로 사람에게 넘긴다
        db.merge_payload(c, card["id"], {"failed_from": "implementing"})
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "impl_target_unknown", card["key"],
                     {"error": str(e), "title": meta.get("title")})
        return

    branch = worktree.impl_branch_name(display, meta.get("title") or "")
    issue = ghclient.issue_view(card["repo"], card["pr_number"])

    # 워크트리는 repo당 하나를 공유한다. 준비~커밋 전체를 락 안에서 돌려야 같은 대상
    # 저장소의 다른 카드가 편집 중인 트리를 리셋하지 못한다.
    with worktree.impl_session(repo):
        _run_turn(c, card, meta, display, repo, branch, issue)


def _run_turn(c, card, meta, display, repo, branch, issue):
    # 여기서부터 커밋까지가 통째로 무음이었다(설치 수 분 + 엔진 수십 분). 어디까지
    # 갔는지 대시보드에서 보이도록 구간마다 이벤트를 남긴다.
    t0 = time.time()
    wt = worktree.make_impl_worktree(repo, branch)
    db.log_event(c, "impl_worktree_ready", card["key"],
                 {"repo": repo, "branch": branch, "secs": round(time.time() - t0, 1)})

    prompt = prompt_tpl.render(
        "impl.md",
        DISPLAY=display, ISSUE_REPO=card["repo"], ISSUE_NUMBER=card["pr_number"],
        TITLE=meta.get("title") or issue.get("title") or "",
        TARGET_REPO=repo, URL=meta.get("url") or issue.get("url") or "",
        BODY=(issue.get("body") or "(본문 없음)")[:BODY_CHARS],
        INSTRUCTION=(meta.get("instruction") or "(없음)"),
        AGREEMENT=_agreement_text(meta),
        FEEDBACK=(meta.get("feedback") or "(없음 — 첫 라운드)"),
        BRANCH=branch,
    )
    engine = card["engine"] or "claude"
    db.log_event(c, "impl_engine_started", card["key"], {"engine": engine, "branch": branch})
    t1 = time.time()
    raw = engines.run_impl(prompt, engine=engine, cwd=wt)
    db.log_event(c, "impl_engine_done", card["key"],
                 {"engine": engine, "secs": round(time.time() - t1, 1), "chars": len(raw or "")})
    try:
        result = claude_runner.parse_obj(raw)
    except claude_runner.ClaudeError:
        # 요약을 못 읽어도 편집은 이미 됐을 수 있다 — diff가 진실이므로 계속 간다
        result = {"summary": raw.strip()[:500], "done": None}

    files = changed_files(wt)
    if not files:
        db.merge_payload(c, card["id"], {"failed_from": "implementing"})
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "impl_no_changes", card["key"],
                     {"engine": engine, "repo": repo, "branch": branch,
                      "summary": (result.get("summary") or raw)[:400]})
        return

    sha = _commit(wt, meta, display, result.get("summary") or "")
    # 카드에 보여줄 것은 **브랜치 누적** 변경이다 — files 는 이번 라운드가 만진
    # 것뿐이라, 여러 라운드를 돈 카드는 PR 규모를 실제보다 작게 보여준다.
    try:
        shown = worktree.branch_files(repo, branch) or files
    except Exception:
        shown = files   # 누적을 못 구해도 라운드를 죽이지는 않는다
    db.merge_payload(c, card["id"], {
        "target_repo": repo, "branch": branch, "worktree": wt, "commit": sha,
        "changed": shown[:60],
        "impl": {k: result.get(k) for k in
                 ("done", "summary", "changes", "verification", "open_questions",
                  "risk", "pr_type", "manual_test")},
    })
    db.set_status(c, card["id"], "impl_verify")
    db.log_event(c, "impl_committed", card["key"],
                 {"repo": repo, "branch": branch, "commit": sha,
                  "files": len(files), "done": result.get("done"), "engine": engine})
