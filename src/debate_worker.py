"""debate worker: 구현 전에 두 엔진이 설계를 놓고 다투게 한다 (읽기 전용).

브로커가 턴을 소유한다 — 두 엔진이 서로에게 말을 걸지 않고, 이 워커가 번갈아
headless 로 호출한다. 그래서 승인 프롬프트가 0이다(대화형 판에서 자율 루프를
돌리면 명령마다 사람에게 물어 진행이 막힌다 — 실측으로 확인했다).

한 번의 process() 는 **한 라운드만** 돈다. 카드는 spec 에 머물고 tick 의 wave 가
다시 집어간다 — 중간에 죽어도 그 라운드부터 재개되고, 진행이 타임라인에 남는다.
"""
import hashlib
import json
import re

from . import (claude_runner, config, db, engines, ghclient, impl_worker, notify,
               prompt_tpl, worktree)
from .config import CFG

MAX_ROUNDS = 6            # 라운드 상한(제안·반박 합쳐서)
BODY_CHARS = 8000
TRANSCRIPT_CHARS = 9000   # 상대 발언 전체가 아니라 요약만 넘긴다
ROLES = {"proposer": "claude", "critic": "codex"}


ENGINE_ROLES = ("proposer", "critic")


def _role(round_no: int) -> str:
    """짝수 라운드 제안자, 홀수 라운드 반대신문."""
    return "proposer" if round_no % 2 == 0 else "critic"


def _engine_turns(turns: list[dict]) -> list[dict]:
    """사람이 끼워넣은 턴(role="operator")은 역할 교대에서 세지 않는다 —
    세면 사람이 한마디 할 때마다 같은 엔진이 두 번 연달아 말한다."""
    return [t for t in turns if t.get("role") in ENGINE_ROLES]


def _since_steer(turns: list[dict]) -> list[dict]:
    """마지막 사람 개입 이후의 턴만. 사람이 방향을 틀면 새 국면이므로 그 전의
    주장과 같아졌다고 '새 정보 없음'으로 끝내면 안 된다."""
    for i in range(len(turns) - 1, -1, -1):
        if turns[i].get("role") == "operator":
            return turns[i + 1:]
    return turns


def _claim_hash(turn: dict) -> str:
    text = re.sub(r"\s+", " ", (turn.get("claim") or "")).strip().lower()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _render_transcript(turns: list[dict]) -> str:
    if not turns:
        return "(첫 라운드입니다)"
    out = []
    for t in turns:
        if t.get("role") == "operator":
            out.append("### 🧑 운영자 개입 — 이 지시가 양쪽 주장보다 우선한다\n"
                       + (t.get("claim") or ""))
            continue
        out.append(
            f"### r{t['round']} · {t['role']}({t['engine']}) · {t.get('verdict', '?')}\n"
            f"CLAIM: {t.get('claim', '')}\n"
            f"EVIDENCE: {', '.join(t.get('evidence') or []) or '(없음)'}\n"
            f"PROPOSAL: {t.get('proposal', '')}\n"
            f"OPEN: {', '.join(t.get('open_questions') or []) or '(없음)'}"
        )
    text = "\n\n".join(out)
    return text[-TRANSCRIPT_CHARS:] if len(text) > TRANSCRIPT_CHARS else text


def _agreement(turns: list[dict], end_reason: str = "") -> dict:
    """합의문을 LLM 없이 트랜스크립트에서 조립한다.

    한 턴을 더 태워 요약시키는 대신 트랜스크립트에서 뽑는다 — 요약 턴은 비용이고,
    무엇보다 요약이 대화 내용과 어긋나면 사람이 검증할 방법이 없다.

    **상한으로 끝난 토론을 '합의'로 표시하면 안 된다.** 반박된 안을 사람이 승인하면
    그대로 구현으로 간다. 그래서 (1) design 은 마지막 *제안자* 안을 쓰고(마지막 턴이
    반대신문이면 그건 합의안이 아니라 반박이다) (2) 아직 답하지 못한 반대신문 주장을
    미합의로 올린다."""
    eng = _engine_turns(turns)
    last = eng[-1] if eng else {}
    proposals = [t for t in eng if t.get("role") == "proposer" and (t.get("proposal") or "").strip()]
    unresolved, seen = [], []

    def add(item):
        key = (item or "").strip()
        if key and key not in seen:
            seen.append(key)
            unresolved.append(key)

    for t in eng[-2:]:
        for q in (t.get("open_questions") or []):
            add(q)
    # 상한·결렬로 끝났고 마지막 발언이 반대신문의 미해결 반박이면 그것이 최대 쟁점이다
    settled = len(eng) >= 2 and all(t.get("verdict") == "AGREE" for t in eng[-2:])
    if not settled:
        for t in reversed(eng):
            if t.get("role") == "critic" and t.get("verdict") != "AGREE":
                add(f"[미해결 반박 r{t.get('round')}] {(t.get('claim') or '')[:400]}")
                break
    verdicts = [t.get("verdict") for t in eng[-2:]]
    return {
        "design": (proposals[-1].get("proposal") if proposals else ""),
        "design_round": (proposals[-1].get("round") if proposals else None),
        "unresolved": unresolved,
        "risk": last.get("risk", ""),
        "rounds": len(eng),
        "steers": len([t for t in turns if t.get("role") == "operator"]),
        "verdicts": verdicts,
        "settled": settled,          # 양쪽이 실제로 AGREE 로 끝났는가
        "end_reason": end_reason,
        "blocked": "BLOCKED" in verdicts,
    }


def _finish(c, card, meta, turns, reason: str):
    agreement = _agreement(turns, reason)
    db.merge_payload(c, card["id"], {"agreement": agreement, "debate_end": reason})
    db.set_status(c, card["id"], "spec_blocked", blocked=1)
    db.log_event(c, "debate_finished", card["key"],
                 {"reason": reason, "rounds": agreement["rounds"],
                  "unresolved": len(agreement["unresolved"]),
                  "blocked": agreement["blocked"]})
    display = meta.get("display") or f"#{card['pr_number']}"
    n = len(agreement["unresolved"])
    head = "설계 합의" if agreement["settled"] else "설계 미합의"
    notify.send(
        f"Lookout — {display} {head}",
        (f"미합의 {n}건" if n else "미합의 없음") + f" · {agreement['rounds']}라운드 · {reason}",
        subtitle=("승인해야 구현이 시작됩니다" if agreement["settled"]
                  else "합의 못 함 — 승인 전에 반드시 읽으세요"),
        group="lookout-debate",
    )


def _context(c, card, meta) -> tuple[str, str]:
    """(대상 repo, 엔진이 볼 디렉터리).

    이슈 카드는 구현으로 이어지므로 구현 브랜치·워크트리를 미리 만들어 그 위에서
    토론한다. 주제 카드(debate_only)는 구현으로 가지 않으니 브랜치를 만들지 않고
    체크아웃을 그대로 읽는다 — 토론 단계는 양쪽 다 read-only 라서 안전하다."""
    if meta.get("mode") == "debate_only":
        repo = (meta.get("target_repo") or "").strip()
        if not repo:
            return "", config.HERMES_HOME
        try:
            return repo, worktree.impl_parent(repo)
        except worktree.ImplRepoUnknown:
            return repo, config.HERMES_HOME
    repo = impl_worker.target_repo(meta)          # TargetUnknown 은 호출부가 처리
    display = meta.get("display") or f"#{card['pr_number']}"
    branch = worktree.impl_branch_name(display, meta.get("title") or "")
    db.merge_payload(c, card["id"], {"branch": branch})
    # 설치는 건너뛴다 — 토론은 읽기만 하고, 구현 스테이지가 같은 워크트리를 이어받아
    # 그때 설치한다.
    return repo, worktree.make_impl_worktree(repo, branch, setup=False)


def process(c, card):
    meta = json.loads(card["payload"]) if card["payload"] else {}
    display = meta.get("display") or f"#{card['pr_number']}"
    topic_only = meta.get("mode") == "debate_only"
    try:
        repo, cwd = _context(c, card, meta)
    except impl_worker.TargetUnknown as e:
        db.merge_payload(c, card["id"], {"failed_from": "spec"})
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "impl_target_unknown", card["key"],
                     {"error": str(e), "title": meta.get("title")})
        return

    turns = meta.get("debate") or []
    round_no = len(_engine_turns(turns))
    role = _role(round_no)
    engine = (CFG.get("debate_roles") or ROLES).get(role, ROLES[role])
    if not engines.is_ready(engine):
        # 한쪽 엔진이 없으면 토론이 성립하지 않는다. 같은 엔진으로 대체하면
        # 자기 안을 자기가 반박하는 꼴이라 의미가 없으므로 사람에게 넘긴다.
        db.merge_payload(c, card["id"], {"failed_from": "spec"})
        db.set_status(c, card["id"], "failed")
        db.log_event(c, "debate_engine_missing", card["key"],
                     {"role": role, "engine": engine})
        return

    if topic_only:
        body = meta.get("topic") or ""
    else:
        body = (ghclient.issue_view(card["repo"], card["pr_number"]).get("body") or "")

    prompt = prompt_tpl.render(
        f"debate.{role}.md",
        DISPLAY=display, TITLE=meta.get("title") or "",
        TARGET_REPO=repo or "(특정 저장소 없음 — 주어진 주제만으로 논의)",
        BODY=(body or "(본문 없음)")[:BODY_CHARS],
        INSTRUCTION=(meta.get("instruction") or "(없음)"),
        ROUND=round_no + 1, TRANSCRIPT=_render_transcript(turns),
    )
    db.log_event(c, "debate_turn_started", card["key"],
                 {"round": round_no + 1, "role": role, "engine": engine})
    # 이슈 토론은 공유 워크트리를 쓴다 — 엔진 실행 구간까지 락 안에서 돈다.
    # 주제 토론(topic_only)은 체크아웃을 읽기만 하므로 락이 필요 없다.
    if topic_only:
        raw = engines.run(prompt, engine=engine, cwd=cwd, add_dir=cwd)
    else:
        with worktree.impl_session(repo):
            raw = engines.run(prompt, engine=engine, cwd=cwd, add_dir=cwd)
    try:
        turn = claude_runner.parse_json(raw)
    except claude_runner.ClaudeError:
        turn = {"claim": raw.strip()[:300], "verdict": "CONTINUE"}

    turn.update({"round": round_no + 1, "role": role, "engine": engine,
                 "hash": _claim_hash(turn)})
    turns.append(turn)
    patch = {"debate": turns, "worktree": cwd}
    if repo:
        patch["target_repo"] = repo
    db.merge_payload(c, card["id"], patch)
    db.log_event(c, "debate_turn", card["key"],
                 {"round": turn["round"], "role": role, "engine": engine,
                  "verdict": turn.get("verdict"), "claim": (turn.get("claim") or "")[:200]})

    # ── 3중 종료 ─────────────────────────────────────────────────
    same_role = [t for t in _since_steer(turns)[:-1] if t.get("role") == role]
    if any(t.get("hash") == turn["hash"] for t in same_role):
        # 새 정보 없음 — 무한 예의 루프의 유일한 방어선
        _finish(c, card, meta, turns, "새 정보 없음")
        return
    if turn.get("verdict") == "BLOCKED":
        _finish(c, card, meta, turns, "결렬")
        return
    recent = _engine_turns(turns)[-2:]
    if len(recent) == 2 and all(t.get("verdict") == "AGREE" for t in recent):
        _finish(c, card, meta, turns, "양쪽 합의")
        return
    if len(_engine_turns(turns)) >= MAX_ROUNDS + int(meta.get("debate_bonus") or 0):
        _finish(c, card, meta, turns, "라운드 상한")
        return
    # 계속 — 카드는 spec 에 머물고 다음 wave 가 반대 역할로 집어간다
