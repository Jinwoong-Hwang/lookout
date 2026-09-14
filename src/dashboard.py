"""Local Kanban dashboard (stdlib only).

  python -m src.dashboard      # then open http://127.0.0.1:8788

Board view + operator actions (start / rereview / ignore / unblock).
"""
import json
import os
import re
import subprocess
import csv
import io
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import (commenter, db, engines, feedback, ghclient, impl_verifier, keys,
               poller, profiles, router, worktree)
from .config import CFG


def kick_tick():
    """클릭 즉시 tick을 깨워 리뷰/승인이 바로 시작되게 함 (5분 주기 대기 회피).
    이미 도는 tick이 있으면 flock 때문에 새 프로세스는 즉시 종료(무해)."""
    try:
        subprocess.Popen([os.sys.executable, "-m", "src.tick"],
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:  # noqa: BLE001
        pass

DASHBOARD_HOST = CFG.get("dashboard_host", "127.0.0.1")
PORT = int(CFG.get("dashboard_port", 8788))
WRITE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in
                       CFG.get("dashboard_write_networks", ["127.0.0.0/8", "::1/128"]))

LANES = [
    ("triage", "📥 Triage (리뷰 대기)"),
    ("intake", "⏳ 시작됨"),
    ("reviewing", "🔍 리뷰 중"),
    ("verifying", "🧪 검증 중"),
    ("commenting", "✍️ 댓글 작성"),
    ("commented", "💬 댓글 완료"),
    ("lgtm", "✅ LGTM"),
    ("approve_blocked", "🔒 승인 대기"),
    ("approving", "🚀 승인 중"),
    ("done", "🏁 완료 · 머지 대기"),
    ("failed", "⚠️ 실패 (재시도 필요)"),
]

# 이슈 작업 보드 — 리뷰 보드와 **다른 뷰**다. 같은 보드에 레인을 붙이면 컬럼이
# 16개가 되고 Triage에 PR 카드와 이슈 카드가 섞인다. 리뷰 흐름은 그대로 둔다.
# 상태 이름은 리뷰 레인과 겹치지 않아야 한다(db.cards_in은 status만 보므로).
WORK_LANES = [
    ("triage", "📥 대기 (내 이슈)"),
    ("spec", "🗣 설계 토론"),
    ("spec_blocked", "🧑‍⚖️ 설계 승인 대기"),
    ("implementing", "🛠 구현 중"),
    ("impl_verify", "🧾 구현 검증"),
    ("verify_blocked", "⚖️ 검토 필요"),
    ("pr_blocked", "🔒 PR 승인 대기"),
    ("pr_opening", "🚀 PR 올리는 중"),
    ("done", "🏁 완료"),
    ("failed", "⚠️ 실패 (재시도 필요)"),
]


def build_board():
    with db.connect() as c:
        cards = c.execute(
            "SELECT * FROM cards WHERE kind!='root' AND status!='archived' ORDER BY updated_at DESC"
        ).fetchall()
        out = []
        for card in cards:
            meta = json.loads(card["payload"]) if card["payload"] else {}
            if isinstance(meta, str):  # tolerate legacy double-encoded payloads
                meta = json.loads(meta)
            if card["kind"] == "issue":
                impl_err = ""
                if card["status"] == "failed":
                    ev = c.execute(
                        "SELECT type, detail FROM events WHERE key=? AND type IN"
                        " ('impl_target_unknown','impl_no_changes','review_gave_up')"
                        " ORDER BY id DESC LIMIT 1", (card["key"],)).fetchone()
                    if ev:
                        d = json.loads(ev["detail"]) if ev["detail"] else {}
                        head = {"impl_target_unknown": "대상 저장소 미정 — 카드에서 고르세요",
                                "impl_no_changes": "엔진이 아무 파일도 바꾸지 않음",
                                "review_gave_up": "구현 실패"}.get(ev["type"], ev["type"])
                        tail = (d.get("error") or d.get("summary") or "").strip()
                        impl_err = f"{head} — {tail[:200]}" if tail else head
                elif card["status"] == "triage":
                    q = c.execute(
                        "SELECT detail FROM events WHERE key=? AND type='review_quota_paused'"
                        " ORDER BY id DESC LIMIT 1", (card["key"],)).fetchone()
                    if q and q["detail"]:
                        d = json.loads(q["detail"])
                        impl_err = (f"⏸ {d.get('engine','')} 토큰 소진으로 대기열 복귀"
                                    + (f" · {d.get('retry_at')} 이후 재시도" if d.get("retry_at") else ""))
                # 이슈에는 head/findings/closure/피드백이 없다. PR용 조회를 태우면
                # 전부 빈 값이 나오므로 여기서 끊고 작업 카드에 필요한 것만 싣는다.
                out.append({
                    "id": card["id"], "kind": "issue", "status": card["status"],
                    "engine": card["engine"] or "claude",
                    "repo": card["repo"], "pr": card["pr_number"],
                    "display": meta.get("display") or f"#{card['pr_number']}",
                    "title": meta.get("title", ""), "url": meta.get("url", ""),
                    "author": ", ".join(meta.get("assignees") or []),
                    "labels": meta.get("labels") or [],
                    "assignees": meta.get("assignees") or [],
                    "instruction": meta.get("instruction", ""),
                    "mode": meta.get("mode", ""),
                    "target_repo": meta.get("target_repo", ""),
                    "branch": meta.get("branch", ""),
                    "commit": meta.get("commit", ""),
                    "worktree": meta.get("worktree", ""),
                    "parent_repo_path": _impl_parent_path(meta.get("target_repo")),
                    "changed": meta.get("changed") or [],
                    "impl": meta.get("impl") or {},
                    "verify": meta.get("verify") or {},
                    "verify_exhausted": bool(meta.get("verify_exhausted")),
                    "verify_override": bool(meta.get("verify_override")),
                    "agreement": meta.get("agreement") or {},
                    "spec_amendment": meta.get("spec_amendment", ""),
                    "topic": meta.get("topic", ""),
                    "debate_only": meta.get("mode") == "debate_only",
                    "debate": meta.get("debate") or [],
                    "pr_url": meta.get("pr_url", ""),
                    "pr_dryrun": bool(meta.get("pr_dryrun")),
                    "rounds": meta.get("impl_rounds") or 1,
                    "updated_at": card["updated_at"],
                    "timeline": [
                        {"ts": e["ts"], "type": e["type"],
                         "label": EVENT_LABELS.get(e["type"], e["type"]),
                         "detail": _event_note(e["type"], e["detail"])}
                        for e in c.execute(
                            "SELECT ts, type, detail FROM events WHERE key=?"
                            " ORDER BY id DESC LIMIT 40", (card["key"],)).fetchall()
                    ],
                    "head": "", "blocked": card["blocked"],
                    "findings": [], "comments": [], "dryrun_pending": False,
                    "feedback": None, "closure": {}, "error": impl_err,
                })
                continue
            findings = []
            for f in db.findings_for_card(c, card["id"]):
                detail = json.loads(f["body"]) if f["body"] else {}
                findings.append({
                    "id": f["id"], "status": f["status"], "severity": f["severity"],
                    "confidence": f["confidence"], "file": f["file"], "line": f["line"],
                    "title": f["title"], "problem": detail.get("problem", ""),
                    "fix": detail.get("fix", ""),
                    "decision_comment_id": f["decision_comment_id"],
                    "decision_evidence": f["decision_evidence"],
                    "decision_follow_up": f["decision_follow_up"],
                })
            ev = c.execute(
                "SELECT type, detail FROM events WHERE key=? AND type IN ('comment_dryrun','comment_posted','comment_dryrun_published') ORDER BY id",
                (card["key"],),
            ).fetchall()
            comments = []
            for e in ev:
                d = json.loads(e["detail"]) if e["detail"] else {}
                comments.append({"type": e["type"], "body": d.get("body", ""), "url": d.get("url", "")})
            dryrun_pending = c.execute(
                "SELECT 1 FROM findings WHERE card_id=? AND comment_id='DRYRUN' LIMIT 1",
                (card["id"],),
            ).fetchone() is not None
            clo = db.closure_counts(c, card["repo"], card["pr_number"])
            err = ""
            if card["status"] == "failed":
                g = c.execute(
                    "SELECT detail FROM events WHERE key=? AND type='review_gave_up'"
                    " ORDER BY id DESC LIMIT 1", (card["key"],)).fetchone()
                d = json.loads(g["detail"]) if (g and g["detail"]) else {}
                reason = d.get("error") or ""
                if not reason:  # 구버전 이벤트엔 error가 없다 — trace 마지막 줄로 대체
                    t = c.execute(
                        "SELECT detail FROM events WHERE key=? AND type='stage_error'"
                        " ORDER BY id DESC LIMIT 1", (card["key"],)).fetchone()
                    if t and t["detail"]:
                        tr = (json.loads(t["detail"]).get("trace") or "").strip()
                        # trace 마지막 줄은 예외 메시지에 섞인 stderr 꼬리라 무의미할 수 있다.
                        # 뒤에서부터 실제 예외 줄(XxxError: ...)을 먼저 찾는다.
                        lines = [x.strip() for x in tr.splitlines() if x.strip()]
                        hit = next((x for x in reversed(lines) if re.search(r"\w+Error: ", x)), "")
                        reason = (hit or (lines[-1] if lines else ""))[:300]
                err = f"[{d.get('stage', 'reviewer')}] {reason}".strip()
            elif card["status"] == "triage":
                q = c.execute(
                    "SELECT detail FROM events WHERE key=? AND type='review_quota_paused'"
                    " ORDER BY id DESC LIMIT 1", (card["key"],)).fetchone()
                if q and q["detail"]:
                    d = json.loads(q["detail"])
                    at = d.get("retry_at")
                    err = (f"⏸ {d.get('engine', '')} 토큰 소진으로 대기열 복귀"
                           + (f" · {at} 이후 재시도" if at else ""))
            out.append({
                "id": card["id"], "kind": card["kind"], "status": card["status"],
                "engine": card["engine"] or "claude",
                "repo": card["repo"], "pr": card["pr_number"],
                "head": (card["head_sha"] or "")[:10], "blocked": card["blocked"],
                "title": meta.get("title", ""), "url": meta.get("url", ""),
                "author": meta.get("author", ""),
                "findings": findings, "comments": comments,
                "dryrun_pending": dryrun_pending,
                "feedback": feedback.latest_for_card(c, card["id"]),
                "closure": {"resolved": clo.get("resolved", 0),
                            "dismissed": clo.get("dismissed", 0),
                            "deferred": clo.get("deferred", 0),
                            "unresolved": clo.get("unresolved", 0),
                            "pending": clo.get("dismiss_pending", 0) + clo.get("defer_pending", 0)},
                "error": err,
            })
        return out


def _impl_parent_path(repo: str) -> str:
    """구현 브랜치가 실제로 들어 있는 로컬 체크아웃. 사람이 그 브랜치로 자기
    워크트리를 파려면 이 경로가 필요하다."""
    if not repo:
        return ""
    try:
        return worktree.impl_parent(repo)
    except Exception:  # noqa: BLE001 - 설정 없음/경로 없음 모두 표시만 생략
        return ""


def _event_note(type_: str, detail) -> str:
    """이벤트 detail(JSON)에서 사람이 볼 한 줄만 꺼낸다. 전부 뿌리면 모달이 로그가 된다."""
    try:
        d = json.loads(detail) if detail else {}
    except (TypeError, ValueError):
        return ""
    if not isinstance(d, dict):
        return ""
    for key in ("error", "reason", "note", "summary", "url", "title"):
        if d.get(key):
            return str(d[key])[:200]
    bits = []
    for key in ("engine", "branch", "commit", "secs", "files", "blocking", "round", "approved"):
        if d.get(key) not in (None, ""):
            bits.append(f"{key}={d[key]}")
    return " · ".join(bits)[:200]


ACTIVE_REVIEW = ("intake", "reviewing", "verifying", "commenting")


# 대시보드가 시작시킬 수 있는 스테이지 = tick 에 집어가는 워커가 있는 스테이지.
# 워커 없이 열면 카드가 그 레인에 조용히 서고 아무 일도 일어나지 않는다.
WORK_START = {"start_impl": "implementing", "start_debate": "spec"}
DEBATE_BONUS = 2   # 사람이 개입할 때 늘려주는 토론 라운드 수
IMPL_BONUS = 1     # 사람이 수정을 요청할 때 늘려주는 구현·검증 라운드 수
# 워커가 없어 막아둘 것이 생기면 여기에 둔다(버튼 비활성 + 서버 거부).
WORK_START_PENDING: dict[str, tuple[str, str]] = {}

# events 를 카드 모달에 사람이 읽을 수 있게 뿌리기 위한 라벨
EVENT_LABELS = {
    "issue_card_created": "카드 생성", "issue_card_delisted": "목록에서 빠짐",
    "work_started": "작업 시작", "work_start_blocked": "시작 차단(엔진 미준비)",
    "work_start_unavailable": "시작 차단(워커 없음)",
    "impl_target_unknown": "대상 저장소 미정", "impl_worktree_ready": "워크트리 준비 완료",
    "impl_engine_started": "엔진 편집 시작", "impl_engine_done": "엔진 편집 종료",
    "impl_no_changes": "변경 없음", "impl_committed": "커밋 완료",
    "impl_verify_started": "교차 검증 시작", "impl_verified": "교차 검증 완료",
    "impl_rework": "재구현으로 되돌림", "impl_rounds_exhausted": "라운드 예산 소진 — 사람 판정으로",
    "impl_verify_no_branch": "브랜치 정보 없음", "impl_verify_empty_diff": "diff 없음",
    "operator_pr_approved": "PR 승인(사람)",
    "operator_request_changes": "수정 요청(사람) — 구현으로 되돌림",
    "operator_rerun_verify": "다시 검증(사람)", "reverify_done": "재검증 완료",
    "operator_verify_override": "검증 미통과인데 PR 로(사람)",
    "debate_turn_started": "토론 턴 시작", "debate_turn": "토론 턴",
    "debate_finished": "토론 종료", "debate_engine_missing": "엔진 없음(토론 불가)",
    "operator_spec_approved": "설계 승인(사람)", "operator_spec_rejected": "설계 반려(사람)",
    "operator_debate_steer": "설계 피드백(사람) — 토론 재개",
    "topic_created": "주제 토론 생성", "topic_accepted": "결과 채택(사람)",
    "debate_parse_failed": "엔진 응답 파싱 실패 — 원문으로 진행",
    "topic_promoted": "주제 결론 → 구현 승격(사람)",
    "topic_promote_blocked": "승격 차단 — 대상 저장소 미설정",
    "gate_stale": "게이트 거부 — 카드 상태가 이미 바뀜(중복/낡은 클릭)", "operator_retry": "재시도(사람)",
    "pr_dryrun": "PR dry-run", "pr_opened": "PR 생성", "pr_open_no_branch": "브랜치 정보 없음",
    "review_quota_paused": "토큰 소진 — 대기열 복귀", "review_gave_up": "재시도 포기",
    "stage_error": "스테이지 오류",
}


def do_action(action, card_id, engine="claude", text=None, repo=None):
    if engine not in ("claude", "codex"):
        engine = "claude"
    kick = False
    stop_target = None
    with db.connect() as c:
        card = c.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not card:
            return False
        if action == "start" and card["kind"] == "review" and card["status"] == "triage":
            if not engines.is_ready(engine):  # 로그인/설치 안 된 엔진으로 시작 차단
                db.log_event(c, "operator_start_blocked", card["key"], {"engine": engine})
                return False
            db.set_engine(c, card["id"], engine)
            db.set_status(c, card["id"], "intake")
            db.log_event(c, "operator_start", card["key"], {"engine": engine})
            kick = True
        elif action == "save_instruction" and card["kind"] == "issue":
            # 시작 전에만 고칠 수 있다 — 워커가 seed를 읽은 뒤 바뀌면 로그와 실제 작업이 어긋난다.
            if card["status"] != "triage":
                return False
            db.merge_payload(c, card["id"], {"instruction": (text or "").strip()})
        elif action in WORK_START_PENDING and card["kind"] == "issue":
            stage, why = WORK_START_PENDING[action]
            db.log_event(c, "work_start_unavailable", card["key"],
                         {"stage": stage, "reason": why})
            return False
        elif action in WORK_START and card["kind"] == "issue":
            if card["status"] != "triage":
                return False
            if not engines.is_ready(engine):
                db.log_event(c, "work_start_blocked", card["key"], {"engine": engine})
                return False
            mode = "debate" if action == "start_debate" else "implement"
            db.set_engine(c, card["id"], engine)
            db.merge_payload(c, card["id"], {"mode": mode})
            db.set_status(c, card["id"], WORK_START[action])
            db.log_event(c, "work_started", card["key"], {"mode": mode, "engine": engine})
            kick = True
        elif action == "ignore":
            db.set_status(c, card["id"], "archived")
            db.log_event(c, "operator_ignore", card["key"])
        elif action == "retry" and card["status"] == "failed":
            if card["kind"] == "issue":
                # 실패한 스테이지로 돌아간다. 무조건 implementing 으로 보내면
                # 설계 승인 전에 실패한 토론 카드가 승인을 건너뛰고 코드를 고친다.
                meta_now = json.loads(card["payload"]) if card["payload"] else {}
                back = meta_now.get("failed_from") or (
                    "spec" if meta_now.get("mode") in ("debate", "debate_only")
                    else "implementing")
            else:
                back = "intake"
            db.set_status(c, card["id"], back)
            db.log_event(c, "operator_retry", card["key"],
                         {"engine": card["engine"], "to": back})
            kick = True
        elif action == "approve_spec" and card["kind"] == "issue":
            if card["status"] != "spec_blocked":
                return False
            meta_now = json.loads(card["payload"]) if card["payload"] else {}
            if meta_now.get("mode") == "debate_only":
                # 주제 토론은 구현으로 가지 않는다. 승인 = 결과 채택.
                if (text or "").strip():
                    db.merge_payload(c, card["id"], {"spec_amendment": (text or "").strip()})
                if not db.gate(c, card, "done", blocked=0, event="topic_accepted"):
                    return False
                return True
            amendment = (text or "").strip()
            if amendment:
                # 합의문을 고치지 않고 위에 얹는다 — 토론 기록은 그대로 남아야 한다
                db.merge_payload(c, card["id"], {"spec_amendment": amendment})
            if not db.gate(c, card, "implementing", blocked=0,
                           event="operator_spec_approved", detail={"amended": bool(amendment)}):
                return False
            kick = True
        elif action == "implement_topic" and card["kind"] == "issue":
            # 주제 토론의 결론을 실제 작업으로 승격한다. 대상 저장소가 없으면
            # 구현 워커가 "대상 미정"으로 죽으므로 여기서 막는다.
            meta_now = json.loads(card["payload"]) if card["payload"] else {}
            if card["status"] != "spec_blocked" or meta_now.get("mode") != "debate_only":
                return False
            target = (repo or meta_now.get("target_repo") or "").strip()
            if not target or not (CFG.get("impl_repo_paths") or {}).get(target):
                db.log_event(c, "topic_promote_blocked", card["key"],
                             {"repo": target, "reason": "impl_repo_paths 에 없는 저장소"})
                return False
            patch = {"target_repo": target, "mode": "implement"}
            if (text or "").strip():
                patch["spec_amendment"] = (text or "").strip()
            db.merge_payload(c, card["id"], patch)
            if not db.gate(c, card, "implementing", blocked=0,
                           event="topic_promoted", detail={"repo": target}):
                return False
            kick = True
        elif action == "resume_debate" and card["kind"] == "issue":
            if card["status"] != "spec_blocked":
                return False
            steer = (text or "").strip()
            if not steer:
                return False
            meta = json.loads(card["payload"]) if card["payload"] else {}
            turns = (meta.get("debate") or []) + [{"role": "operator", "claim": steer}]
            # 라운드 예산을 늘려준다 — 상한에 걸려 끝난 토론을 그냥 재개하면
            # 첫 턴에서 다시 상한에 걸린다.
            db.merge_payload(c, card["id"], {
                "debate": turns,
                "debate_bonus": int(meta.get("debate_bonus") or 0) + DEBATE_BONUS,
                "agreement": {}})
            if not db.gate(c, card, "spec", blocked=0, event="operator_debate_steer",
                           detail={"steer": steer[:200]}):
                return False
            kick = True
        elif action == "reject_spec" and card["kind"] == "issue":
            if card["status"] != "spec_blocked":
                return False
            # 기록은 남기고 다시 대기로. 재시작 시 이전 라운드가 이어붙지 않게 비운다.
            meta = json.loads(card["payload"]) if card["payload"] else {}
            db.merge_payload(c, card["id"], {
                "debate_prev": (meta.get("debate_prev") or []) + [{
                    "debate": meta.get("debate") or [],
                    "agreement": meta.get("agreement") or {}}],
                "debate": [], "agreement": {}, "mode": ""})
            if not db.gate(c, card, "triage", blocked=0, event="operator_spec_rejected"):
                return False
        elif action == "request_changes" and card["kind"] == "issue":
            # PR 게이트에서 되돌리는 경로. 사람이 본 diff 의 문제를 구현자에게 넘긴다.
            if card["status"] not in ("pr_blocked", "verify_blocked"):
                return False
            note = (text or "").strip()
            meta_now = json.loads(card["payload"]) if card["payload"] else {}
            verify = meta_now.get("verify") or {}
            # 검증이 남긴 미해결 지적은 사람이 다시 타이핑할 이유가 없다 — 비워두면
            # 그대로 넘어간다. 사람이 쓴 것은 그 위에 얹혀 우선한다.
            auto = "" if verify.get("approved") else impl_verifier.blockers_text(verify)
            if not note and not auto:
                return False
            parts = []
            if auto:
                parts.append(f"[검증 미해결] {auto}")
            if note:
                parts.append(f"[운영자 수정 요청] {note}")
            # 덮어쓴다 — 이전 라운드의 (이미 고친) 지적을 다시 보내면 되돌림이 돈다
            db.merge_payload(c, card["id"], {
                "feedback": "\n\n".join(parts),
                "impl_bonus": int(meta_now.get("impl_bonus") or 0) + IMPL_BONUS,
            })
            if not db.gate(c, card, "implementing", blocked=0,
                           event="operator_request_changes", detail={"note": note[:200]}):
                return False
            kick = True
        elif action == "rerun_verify" and card["kind"] == "issue":
            # 같은 커밋을 다시 검증한다. 새 구현이 없으므로 라운드를 쓰지 않는다.
            if card["status"] != "verify_blocked":
                return False
            # 사람이 적어 보낸 관점을 함께 넘긴다 — 입력이 없으면 그냥 재검증이다.
            # 재검증의 쓸모 대부분은 "이 관점으로 다시 보라"이고, 안 넘기면 완전히
            # 같은 입력으로 같은 판정이 나온다.
            note = (text or "").strip()
            db.merge_payload(c, card["id"],
                             {"reverify_only": True, "reverify_note": note})
            if not db.gate(c, card, "impl_verify", blocked=0,
                           event="operator_rerun_verify", detail={"note": note[:200]}):
                return False
            kick = True
        elif action == "verify_override" and card["kind"] == "issue":
            # 검증이 통과하지 못했는데 사람이 감수하고 PR 게이트로 넘긴다.
            if card["status"] != "verify_blocked":
                return False
            db.merge_payload(c, card["id"], {"verify_override": True})
            if not db.gate(c, card, "pr_blocked", blocked=1,
                           event="operator_verify_override"):
                return False
        elif action == "unblock" and card["kind"] == "issue":
            if card["status"] != "pr_blocked":
                return False
            if not db.gate(c, card, "pr_opening", blocked=0, event="operator_pr_approved"):
                return False
            kick = True
        elif action == "unblock" and card["kind"] == "approve":
            db.set_status(c, card["id"], "approving", blocked=0)
            db.log_event(c, "operator_unblock", card["key"])
            kick = True
        elif action == "publish_dryrun" and card["kind"] == "review":
            if not commenter.publish_dryrun(c, card):
                return False
        elif action == "rereview":
            chosen_engine = card["engine"] if card["kind"] == "review" else None
            if not chosen_engine:
                prior = c.execute(
                    """SELECT engine FROM cards
                       WHERE repo=? AND pr_number=? AND head_sha=? AND kind='review'
                       ORDER BY id DESC LIMIT 1""",
                    (card["repo"], card["pr_number"], card["head_sha"]),
                ).fetchone()
                chosen_engine = prior["engine"] if prior and prior["engine"] else engines.default_engine()
            if not engines.is_ready(chosen_engine):
                db.log_event(c, "operator_rereview_blocked", card["key"],
                             {"engine": chosen_engine})
                return False
            if not router.create_rereview(c, card["id"], chosen_engine):
                return False
            kick = True
        elif action == "stop" and card["status"] in ACTIVE_REVIEW:
            db.set_status(c, card["id"], "archived")  # terminal → 워커가 되살리지 않음
            db.log_event(c, "review_stopped", card["key"], {"from": card["status"]})
            stop_target = (card["repo"], card["pr_number"])
        else:
            return False
    if stop_target:
        worktree.kill_review_process(*stop_target)  # 진행 중 LLM 프로세스 강제 종료
    if kick:
        kick_tick()
    return True


def do_finding_action(action, finding_id):
    if action not in {"accept_author_decision", "operator_dismiss"}:
        return False
    kick = False
    with db.connect() as c:
        finding = c.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        allowed = ({"dismiss_pending", "defer_pending"} if action == "accept_author_decision"
                   else {"posted", "confirmed", "unresolved", "dismiss_pending", "defer_pending"})
        if not finding or finding["status"] not in allowed:
            return False
        card = c.execute("SELECT * FROM cards WHERE id=?", (finding["card_id"],)).fetchone()
        if not card or card["kind"] != "review" or card["status"] != "commented":
            return False
        accepted = ("deferred" if action == "accept_author_decision"
                    and finding["status"] == "defer_pending" else "dismissed")
        if action == "operator_dismiss":
            db.set_finding_decision(c, finding_id, accepted, card["head_sha"], "",
                                    "operator override")
        else:
            db.set_finding_status(c, finding_id, accepted)
        db.log_event(c, "operator_author_decision", card["key"],
                     {"finding_id": finding_id, "status": accepted,
                      "comment_id": finding["decision_comment_id"],
                      "manual_override": action == "operator_dismiss"})
        blockers = c.execute(
            """SELECT COUNT(*) n FROM findings WHERE repo=? AND pr_number=?
               AND status IN ('posted','confirmed','unresolved','pending_verify',
                              'dismiss_pending','defer_pending')""",
            (finding["repo"], finding["pr_number"]),
        ).fetchone()["n"]
        if not blockers:
            info = ghclient.pr_view(finding["repo"], finding["pr_number"])
            if (info.get("state") == "OPEN" and not info.get("isDraft")
                    and info.get("headRefOid") == card["head_sha"]):
                db.set_status(c, card["id"], profiles.policy_from_card(card)["no_finding_terminal"])
                kick = True
    if kick:
        kick_tick()
    return True


TOPIC_REPO = "-"          # 이슈가 없으니 repo/pr_number 는 센티넬 (컬럼이 NOT NULL)


def create_topic(text: str, repo: str = "") -> dict:
    """이슈에 매달리지 않은 순수 토론 카드.

    구현으로 가지 않는다 — 산출물은 합의문 하나다. 설계 스테이지를 그대로 쓰되
    승인은 '결과 채택'이라 done 으로 끝난다."""
    topic = (text or "").strip()
    if not topic:
        return {"ok": False, "reason": "주제가 비었습니다"}
    title = topic.splitlines()[0][:90]
    with db.connect() as c:
        seq = c.execute("SELECT COUNT(*) n FROM cards WHERE repo=?",
                        (TOPIC_REPO,)).fetchone()["n"] + 1
        key = keys.topic_key(seq, db.now())
        card_id = db.upsert_card(
            c, key, "issue", TOPIC_REPO, 0, status="spec",
            payload={"display": f"TOPIC-{seq}", "title": title, "topic": topic,
                     "mode": "debate_only", "target_repo": (repo or "").strip(),
                     "labels": [], "assignees": []})
        db.log_event(c, "topic_created", key, {"title": title, "repo": repo or "(없음)"})
    kick_tick()
    return {"ok": True, "card_id": card_id, "display": f"TOPIC-{seq}"}


def refresh_poll(scope: str = "review"):
    """Run the poller now (bypass the interval).

    보고 있는 보드만 갱신한다 — 작업 뷰에서 '이슈 가져오기'를 눌렀는데 PR 폴링이
    돌면 리뷰 카드가 예고 없이 늘어난다."""
    kind = "issue" if scope == "work" else "review"
    with db.connect() as c:
        before = len(db.cards_in(c, ["triage"], kind=kind))
        if scope == "work":
            poller.poll_issues(c)
        else:
            poller.poll(c)
        after = len(db.cards_in(c, ["triage"], kind=kind))
    return {"added": max(0, after - before), "total": after}


def mutation_allowed(client_ip: str, action_header: str, origin: str, host: str) -> bool:
    """Writes are local-operator only; custom header blocks browser CSRF."""
    try:
        ip = ipaddress.ip_address(client_ip)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
    except ValueError:
        return False
    if not any(ip in network for network in WRITE_NETWORKS) or action_header != "1":
        return False
    return not origin or urlparse(origin).netloc == host


def build_mentions():
    with db.connect() as c:
        rows = db.list_mentions(c)
        return [{
            "id": r["id"],
            "channel": r["channel_name"] or r["channel_id"],
            "user": r["user_name"] or r["user_id"],
            "text": r["text"], "ts": r["ts"],
            "permalink": r["permalink"], "status": r["status"],
        } for r in rows]


def _feedback_row(r, include_private=False):
    reactions = json.loads(r["reactions"]) if r["reactions"] else {}
    replies = json.loads(r["author_replies"]) if r["author_replies"] else []
    outcome = json.loads(r["outcome"]) if r["outcome"] else {}
    payload = json.loads(r["payload"]) if r["payload"] else {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    out = {
        "id": r["id"], "repo": r["repo"], "pr": r["pr_number"],
        "card_id": r["card_id"], "profile": r["profile_type"],
        "snapshot_type": r["snapshot_type"], "status": r["status"] or "archived",
        "title": payload.get("title", ""), "comment_url": r["comment_url"],
        "created_at": r["created_at"],
        "reactions": {
            "+1": int(reactions.get("+1") or 0),
            "-1": int(reactions.get("-1") or 0),
            "confused": int(reactions.get("confused") or 0),
            "total_count": int(reactions.get("total_count") or 0),
        },
        "up": int(reactions.get("+1") or 0),
        "down": int(reactions.get("-1") or 0),
        "confused": int(reactions.get("confused") or 0),
        "replies": len(replies),
        "needs_inspection": bool(int(reactions.get("-1") or 0) or int(reactions.get("confused") or 0) or replies),
    }
    if include_private:
        out["author_replies"] = [
            {k: reply.get(k, "") for k in ("id", "url", "created_at")}
            for reply in replies
        ]
        out["outcome"] = outcome
    return out


def _feedback_filters(params):
    where, vals = [], []
    def first(name, default=""):
        return (params.get(name) or [default])[0]
    for col, name in (("s.repo", "repo"), ("s.profile_type", "profile"), ("s.snapshot_type", "snapshot_type")):
        val = first(name).strip()
        if val:
            where.append(f"{col}=?")
            vals.append(val)
    pr = first("pr").strip()
    if pr:
        where.append("s.pr_number=?")
        vals.append(int(pr))
    card_id = first("card_id").strip()
    if card_id:
        where.append("s.card_id=?")
        vals.append(int(card_id))
    needs = first("needs_inspection").strip().lower()
    if needs in ("1", "true", "yes"):
        where.append("(json_extract(s.reactions, '$.\"-1\"') > 0 OR json_extract(s.reactions, '$.confused') > 0 OR json_array_length(s.author_replies) > 0)")
    limit = max(1, min(int(first("limit", "25") or 25), 500))
    return where, vals, limit


def build_feedback(params=None, include_private=False):
    params = params or {}
    where, vals, limit = _feedback_filters(params)
    clause = "WHERE " + " AND ".join(where) if where else ""
    with db.connect() as c:
        rows = c.execute(
            f"""SELECT s.*, c.payload, c.status
               FROM review_feedback_snapshots s
               LEFT JOIN cards c ON c.id=s.card_id
               {clause}
               ORDER BY s.created_at DESC, s.id DESC
               LIMIT ?""",
            (*vals, limit),
        ).fetchall()
        return [_feedback_row(r, include_private=include_private) for r in rows]


def build_feedback_detail(snapshot_id):
    with db.connect() as c:
        row = c.execute(
            """SELECT s.*, c.payload, c.status
               FROM review_feedback_snapshots s
               LEFT JOIN cards c ON c.id=s.card_id
               WHERE s.id=?""",
            (int(snapshot_id),),
        ).fetchone()
        return _feedback_row(row, include_private=True) if row else None


def build_feedback_csv(params=None):
    rows = build_feedback(params or {}, include_private=False)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[
        "id", "repo", "pr", "card_id", "profile", "snapshot_type", "status",
        "title", "comment_url", "created_at", "up", "down", "confused",
        "replies", "needs_inspection",
    ])
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _csv_cell(row.get(k, "")) for k in writer.fieldnames})
    return out.getvalue()


def _csv_cell(value):
    if not isinstance(value, str):
        return value
    stripped = value.lstrip(" \t\r\n")
    if stripped[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def do_mention_action(action, mention_id):
    if action not in ("read", "archive"):
        return False
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM mentions WHERE id=?", (mention_id,)).fetchone():
            return False
        db.set_mention_status(c, mention_id, "read" if action == "read" else "archived")
    return True


HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lookout</title>
<script>(function(){try{var p=localStorage.getItem('lookout_theme')||'auto';
var t=p==='auto'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):p;
document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>
<style>
:root{--bg:#11151c;--panel:#1b212b;--panel2:#232b38;--line:#333d4d;--ink:#f1f5fb;
--muted:#9fabbe;--dim:#6b7688;--accent:#2dd4bf;--purple:#a78bfa;--good:#4ade80;--warn:#fbbf24;--bad:#fb7185;
--header-bg:rgba(17,21,28,.86);--shadow:rgba(0,0,0,.28);
--btn-accent-bg:#2dd4bf;--btn-accent-fg:#06101f;--btn-accent-bd:#2dd4bf;
--btn-purple-bg:#a78bfa;--btn-purple-fg:#0a0612;--btn-purple-bd:#a78bfa;}
:root[data-theme="light"]{--bg:#eceef3;--panel:#f5f6f9;--panel2:#ffffff;--line:#e0e3ea;--ink:#243040;
--muted:#677488;--dim:#a3abb8;--accent:#3aa99c;--purple:#9a7ee8;--good:#56b87f;--warn:#d99a3e;--bad:#ec7a8f;
--header-bg:rgba(245,246,249,.9);--shadow:rgba(40,55,80,.08);
--btn-accent-bg:#d6f2ed;--btn-accent-fg:#137a6d;--btn-accent-bd:#aee3da;
--btn-purple-bg:#ebe3fc;--btn-purple-fg:#6f4cc4;--btn-purple-bd:#d6c8f5;}
:root[data-theme="light"] .card{box-shadow:0 1px 2px var(--shadow)}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-size:14px;line-height:1.5;
font-family:-apple-system,BlinkMacSystemFont,"Pretendard",Roboto,sans-serif;-webkit-font-smoothing:antialiased;
display:flex;flex-direction:column;overflow:hidden}
header{position:sticky;top:0;z-index:20;padding:13px 22px;display:flex;align-items:center;gap:12px;
background:var(--header-bg);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0;font-weight:750;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:12.5px}
.shell{flex:1 1 auto;min-height:0;display:flex}
.side{flex:0 0 194px;min-width:0;border-right:1px solid var(--line);background:var(--panel);
  padding:12px 10px 18px;display:flex;flex-direction:column;gap:2px;overflow-y:auto}
.side .grp{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);
  padding:12px 11px 5px;font-weight:700}
.side button{width:100%;text-align:left;border:none;background:transparent;color:var(--muted);
  padding:8px 11px;border-radius:9px;font:inherit;font-size:13px;display:flex;
  align-items:center;justify-content:space-between;gap:8px}
.side button:hover{background:var(--panel2);color:var(--ink)}
.side button.active{background:var(--btn-accent-bg);color:var(--btn-accent-fg);font-weight:700}
.side .cnt{background:var(--panel2);border-radius:20px;padding:1px 8px;font-size:11px;color:var(--muted)}
.side button.active .cnt{background:rgba(0,0,0,.14);color:inherit}
.main{flex:1 1 auto;min-width:0;min-height:0;display:flex;flex-direction:column}
@media (max-width:860px){
  .shell{flex-direction:column}
  .side{flex:0 0 auto;flex-direction:row;flex-wrap:wrap;border-right:none;
    border-bottom:1px solid var(--line);padding:8px 10px}
  .side .grp{display:none}
  .side button{width:auto}
}
.board{flex:1 1 auto;min-height:0;display:flex;gap:12px;padding:18px;overflow-x:auto;overflow-y:hidden;align-items:stretch}
.board.stack{display:block;overflow-y:auto;overflow-x:hidden}
.toggle{display:flex;gap:6px;margin-left:6px}
.toggle button.active{background:var(--btn-accent-bg);color:var(--btn-accent-fg);border-color:var(--btn-accent-bd);font-weight:650}
/* 작업 보드는 카드가 아니라 **목록**이다. 항목이 한 자릿수고 행마다 할 일이
   하나뿐이라, 카드로 깔면 262px 짜리 박스가 화면을 먹고 정작 훑기가 어렵다. */
.rows{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:12px;
background:var(--panel);overflow:hidden}
.irow{display:flex;gap:12px;align-items:center;padding:11px 14px;cursor:pointer;
border-left:3px solid transparent;border-top:1px solid var(--line);transition:background .12s}
.irow:first-child{border-top:none}
.irow:hover{background:var(--panel2)}
.irow .idot{flex:0 0 auto;width:8px;height:8px;border-radius:50%}
.irow .imain{flex:1 1 auto;min-width:0}
.irow .ihead{display:flex;gap:8px;align-items:baseline;min-width:0}
.irow .inum{flex:0 0 auto;font-weight:700;font-size:12.5px;color:var(--ink);font-variant-numeric:tabular-nums}
.irow .ititle{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
font-size:13px;color:var(--ink)}
.irow .imeta{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-top:5px}
.irow .iright{flex:0 0 auto;display:flex;gap:9px;align-items:center;color:var(--muted);font-size:11.5px}
.irow .igo{color:var(--dim)}
.irow:hover .igo{color:var(--accent)}
.irow.gate{border-left-color:var(--warn);background:color-mix(in srgb,var(--warn) 7%,transparent)}
.irow.gate .igo{color:var(--warn);font-weight:700}
.irow .xbtn{position:static;opacity:0}
.irow:hover .xbtn{opacity:1}
.ghead{display:flex;gap:8px;align-items:center;margin:0 0 8px;font-size:12.5px;font-weight:700}
.ghead .n{background:var(--panel2);border-radius:20px;padding:1px 9px;color:var(--muted);
font-size:11px;font-weight:400}
.ghint{margin-left:auto;color:var(--muted);font-size:11.5px;font-weight:400}
.sec{margin:0 0 16px}
/* 작업 보드 — 게이트 섹션은 '지금 당신 차례'라서 눈에 먼저 걸려야 한다 */
details.grp>summary{list-style:none;cursor:pointer}
details.grp>summary::-webkit-details-marker{display:none}
details.grp>summary h2{margin:0 0 10px}
details.grp[open]>summary h2{margin-bottom:10px}
.sec h2{font-size:14px;margin:0 0 10px;padding-bottom:7px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
.sec .cards{display:flex;flex-direction:row;flex-wrap:wrap;gap:10px;padding:0}
.sec .card{width:262px}
.statuspill{font-size:11px;font-weight:650;padding:2px 9px;border-radius:20px;white-space:nowrap}
.col{background:var(--panel);border:1px solid var(--line);border-radius:14px;min-width:272px;max-width:300px;flex:0 0 auto;display:flex;flex-direction:column;max-height:100%}
.col h2{flex:0 0 auto;font-size:12.5px;font-weight:700;margin:0;padding:13px 15px;border-bottom:1px solid var(--line);color:var(--ink);display:flex;justify-content:space-between;align-items:center;gap:8px}
.col h2 .lh{display:flex;align-items:center;gap:8px}
.col .n{background:var(--panel2);border-radius:20px;padding:1px 9px;color:var(--muted);font-size:11.5px}
.cards{padding:11px;display:flex;flex-direction:column;gap:11px;min-height:30px}
.col .cards{flex:1 1 auto;overflow-y:auto;min-height:0}
.card{position:relative;background:var(--panel2);border:1px solid var(--line);border-left:4px solid var(--dim);border-radius:11px;padding:13px;cursor:pointer;transition:border-color .14s,transform .08s,box-shadow .14s}
.card:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:0 6px 18px var(--shadow)}
.pr{font-size:13px;font-weight:700;color:var(--ink);display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:7px}
.pr .num{font-size:14.5px}
.pr a{color:var(--ink);text-decoration:none}
.title{font-size:13px;color:var(--ink);opacity:.92;margin-bottom:9px;line-height:1.45;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.high{background:var(--bad)}.medium{background:var(--warn)}.low{background:var(--accent)}
.btns{display:flex;gap:7px;margin-top:11px}
.instr{margin-top:9px}
.instr textarea{width:100%;box-sizing:border-box;min-height:44px;resize:vertical;
  background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:6px;
  padding:6px 7px;font:inherit;font-size:11.5px;line-height:1.4}
.instr textarea::placeholder{color:var(--dim)}
.instr input{width:100%;box-sizing:border-box;margin-top:7px;background:var(--panel);
  color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 7px;
  font:inherit;font-size:11.5px}
.composer{padding:12px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
.composer textarea{width:100%;box-sizing:border-box;min-height:52px;resize:vertical;
  background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:9px;
  padding:8px 10px;font:inherit;font-size:12.5px;line-height:1.45}
.composer .crow{display:flex;gap:8px;margin-top:8px;align-items:center}
.composer input{flex:1 1 auto;min-width:0;background:var(--panel2);color:var(--ink);
  border:1px solid var(--line);border-radius:9px;padding:7px 10px;font:inherit;font-size:12.5px}
.composer button{flex:0 0 auto}
.md{font-size:13px;line-height:1.62;color:var(--ink)}
.md p{margin:6px 0}
.md h5{margin:12px 0 5px;font-size:13px;font-weight:750;color:var(--ink)}
.md ul,.md ol{margin:6px 0;padding-left:19px}
.md li{margin:3px 0}
.md code{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
  padding:.05em .35em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.md pre.code{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
  padding:10px 12px;margin:8px 0;overflow-x:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:11.8px;line-height:1.55;white-space:pre}
.md pre.code code{background:none;border:none;padding:0}
.md blockquote{margin:6px 0;padding:2px 0 2px 10px;border-left:2px solid var(--line);color:var(--muted)}
.md strong{font-weight:700}
details.sec{margin-top:12px;border:1px solid var(--line);border-radius:9px;background:var(--panel)}
details.sec>summary{cursor:pointer;padding:9px 12px;font-size:12.5px;font-weight:700;
  color:var(--ink);list-style:none;display:flex;justify-content:space-between;gap:8px}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary::after{content:"▾";color:var(--dim);font-weight:400}
details.sec[open]>summary::after{content:"▴"}
details.sec>.body{padding:0 12px 12px}
.tl{margin-top:6px;border:1px solid var(--line);border-radius:9px;overflow:hidden}
.tlrow{display:grid;grid-template-columns:66px 116px 1fr;gap:8px;padding:5px 10px;
  font-size:11.5px;border-bottom:1px solid var(--line);align-items:baseline}
.tlrow:last-child{border-bottom:none}
.tlrow code{color:var(--dim);font-size:11px}
.tlrow .tlab{color:var(--ink);font-weight:600}
.tlrow .tdet{color:var(--muted);word-break:break-word}
.instrline{margin-top:6px;font-size:11px;line-height:1.35;color:var(--muted);
  white-space:pre-wrap;word-break:break-word}
.rev{display:flex;gap:7px;margin-top:11px}
.rev button{flex:1}
.rev button:disabled{background:var(--panel);border-color:var(--line);color:var(--dim);filter:none;cursor:not-allowed}
.engnote{margin-top:11px;font-size:11.5px;color:var(--warn);line-height:1.45;
  background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.32);border-radius:9px;padding:8px 10px}
.engnote code{background:var(--bg);padding:1px 5px;border-radius:5px;color:var(--warn)}
button{font:inherit;font-size:12.5px;font-weight:600;border:1px solid var(--line);background:var(--panel2);
color:var(--ink);border-radius:9px;padding:7px 12px;cursor:pointer;transition:filter .12s,transform .05s}
button:hover{filter:brightness(1.13)}
button:active{transform:translateY(1px)}
button:disabled{opacity:.6;cursor:default}
button.go{background:var(--btn-accent-bg);border-color:var(--btn-accent-bd);color:var(--btn-accent-fg);font-weight:700}
button.claude{background:var(--btn-accent-bg);border-color:var(--btn-accent-bd);color:var(--btn-accent-fg);font-weight:700}
button.codex{background:var(--btn-purple-bg);border-color:var(--btn-purple-bd);color:var(--btn-purple-fg);font-weight:700}
button.stop{background:transparent;border-color:var(--bad);color:var(--bad)}
button.stop:hover{background:var(--bad);color:#1a0608}
.filterbar{display:flex;gap:7px;flex-wrap:wrap;padding:11px 22px;border-bottom:1px solid var(--line);
  position:sticky;top:51px;z-index:15;background:var(--header-bg);backdrop-filter:blur(8px)}
.chip{font-size:12px;font-weight:600;border:1px solid var(--line);background:var(--panel2);color:var(--muted);
  border-radius:20px;padding:5px 12px;cursor:pointer;display:flex;align-items:center;gap:6px}
.chip:hover{filter:brightness(1.12)}
.chip.on{background:var(--btn-accent-bg);color:var(--btn-accent-fg);border-color:var(--btn-accent-bd)}
.chip b{font-weight:700}
.rdot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:0 0 auto}
.repopill{font-size:10.5px;font-weight:650;padding:2px 8px;border-radius:20px;border:1px solid var(--line);
  display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.toggle{margin-left:4px;border:1px solid var(--line);border-radius:9px;overflow:hidden;gap:0}
.toggle button{border:none;border-radius:0;background:transparent;color:var(--muted);padding:7px 13px}
.toggle button.active{background:var(--btn-accent-bg);color:var(--btn-accent-fg);font-weight:700}
/* ignore: small, muted, corner — hard to hit by accident, asks to confirm */
.xbtn{position:absolute;top:7px;right:7px;font-size:11px;line-height:1;color:var(--muted);
background:transparent;border:none;padding:3px 5px;border-radius:6px;opacity:.4}
.xbtn:hover{opacity:1;color:var(--bad);background:var(--panel)}
.empty{color:var(--muted);font-size:11px;text-align:center;padding:8px 0}
/* modal */
.ov{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:24px}
.ov.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;max-width:720px;width:100%;max-height:86vh;overflow:auto;padding:22px}
/* 이슈 모달은 '읽고 결정하는' 문서다 — 머리(무엇인가)와 조작부(무엇을 할까)를
   고정하고 가운데 근거만 스크롤한다. 블로커가 길면 버튼이 화면 밖으로 밀려
   끝까지 내려야 결정할 수 있었다. */
.modal.doc{display:flex;flex-direction:column;padding:0;max-width:780px;max-height:88vh;overflow:hidden}
.doc .mhead{flex:0 0 auto;padding:18px 24px 13px;border-bottom:1px solid var(--line)}
.doc .mbody{flex:1 1 auto;min-height:0;overflow-y:auto;padding:16px 24px 22px}
.doc .mfoot{flex:0 0 auto;padding:13px 24px 16px;border-top:1px solid var(--line);background:var(--panel2)}
.doc .mhead h3{margin:0 0 7px;font-size:17px;line-height:1.4}
.doc .mhead .num{color:var(--accent);font-weight:800;margin-right:7px}
/* 왜 지금 당신 차례인지 — 한 줄로, 눈에 먼저 걸리게 */
.doc .why{display:flex;gap:9px;align-items:flex-start;padding:11px 13px;border-radius:10px;
margin:0 0 4px;font-size:13px;line-height:1.55;color:var(--ink);
background:color-mix(in srgb,var(--warn) 12%,transparent);
border:1px solid color-mix(in srgb,var(--warn) 38%,transparent)}
.doc .why.bad{background:color-mix(in srgb,var(--bad) 12%,transparent);
border-color:color-mix(in srgb,var(--bad) 38%,transparent)}
/* 구획 제목 — 대문자 변환은 한글에 아무 일도 안 하고 자간만 벌린다 */
.doc .lbl{display:flex;gap:8px;align-items:center;font-size:12.5px;font-weight:750;
color:var(--ink);letter-spacing:0;text-transform:none;margin:22px 0 9px;
padding-bottom:7px;border-bottom:1px solid var(--line)}
.doc .lbl::before{content:"";flex:0 0 auto;width:3px;height:13px;border-radius:2px;background:var(--accent)}
.doc .lbl2{font-size:12px;font-weight:700;color:var(--muted);margin:14px 0 4px}
.doc .md{font-size:13.5px;line-height:1.75}
.doc .pre{font-size:13px;line-height:1.7}
.doc .finding{padding:13px 15px;margin:11px 0;border-radius:10px}
.doc .finding .ft{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;
font-weight:700;color:var(--muted);margin-bottom:8px;word-break:break-all}
/* 모달에서는 자르지 않는다 — 실패 사유가 3줄에서 잘리면 원인을 못 읽는다 */
.doc .errline{font-size:13px;line-height:1.6;display:block;-webkit-line-clamp:none;
word-break:break-word;margin-top:8px}
/* 끝난 카드는 '무엇을 결정하나'가 아니라 '무엇이 남았나'를 말해야 한다 */
.doc .done{display:flex;gap:9px;align-items:flex-start;padding:11px 13px;border-radius:10px;
margin:0 0 4px;font-size:13px;line-height:1.55;color:var(--ink);
background:color-mix(in srgb,var(--good) 12%,transparent);
border:1px solid color-mix(in srgb,var(--good) 38%,transparent)}
.doc .done.flat{background:var(--panel2);border-color:var(--line);color:var(--muted)}
.doc .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:8px;margin:11px 0 2px}
.doc .stat{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:9px 11px}
.doc .stat .k{font-size:11px;color:var(--muted);margin-bottom:3px}
.doc .stat .v{font-size:14px;font-weight:750;color:var(--ink);font-variant-numeric:tabular-nums}
.doc .stat .v.ok{color:var(--good)}
.doc .stat .v.no{color:var(--bad)}
.doc .outlink{display:inline-block;margin-top:12px;padding:8px 14px;border-radius:9px;
background:var(--btn-accent-bg);color:var(--btn-accent-fg);border:1px solid var(--btn-accent-bd);
font-weight:700;font-size:13px;text-decoration:none}
.doc .instr{margin:0 0 9px}
.doc .btns{margin-top:0;flex-wrap:wrap}
.doc .sub{margin-top:8px;line-height:1.5}
.modal h3{margin:0 0 6px;font-size:18px}
.finding{border:1px solid var(--line);border-left:4px solid var(--dim);border-radius:11px;padding:14px;margin:12px 0;background:var(--panel2)}
.finding .ft{font-weight:700;font-size:14px;margin-bottom:7px;color:var(--ink)}
.finding .meta{font-size:12px;color:var(--muted);margin-bottom:9px;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.lbl{font-size:11.5px;font-weight:700;color:var(--accent);margin-top:14px;margin-bottom:4px;letter-spacing:.03em;text-transform:uppercase}
.cmt{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:13px;white-space:pre-wrap;font-size:12.5px;line-height:1.5;margin-top:8px}
.close{float:right;cursor:pointer;color:var(--muted);font-weight:600}
.close:hover{color:var(--ink)}
.modal code{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:var(--bg);padding:1px 6px;border-radius:5px;color:var(--accent)}
.msub{color:var(--muted);font-size:12.5px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.msub a{color:var(--accent);word-break:break-all}
.mlink{margin:10px 0}.mlink a{color:var(--accent);font-weight:600}
.pre{white-space:pre-wrap;word-break:break-word;font-size:13.5px;line-height:1.65;color:var(--ink)}
.lbl2{font-size:11.5px;font-weight:700;color:var(--muted);margin-top:12px;margin-bottom:3px}
.sevtag{font-size:11px;font-weight:700;padding:1px 8px;border-radius:20px;text-transform:uppercase}
.fstatus{margin-left:auto;font-size:11px;color:var(--dim)}
.errline{margin-top:6px;font-size:11px;line-height:1.35;color:#fb7185;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;word-break:break-all}
.errline.warn{color:#fbbf24}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(12px);
  background:var(--panel2);color:var(--ink);border:1px solid var(--accent);
  padding:11px 18px;border-radius:12px;font-size:13.5px;font-weight:600;
  box-shadow:0 8px 30px rgba(0,0,0,.45);opacity:0;pointer-events:none;z-index:100;
  display:flex;align-items:center;gap:9px;transition:opacity .18s,transform .18s}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.spin{width:13px;height:13px;border:2px solid var(--accent);border-top-color:transparent;
  border-radius:50%;display:inline-block;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.pill{font-size:11px;padding:2px 8px;border-radius:20px;background:var(--panel);border:1px solid var(--line);color:var(--muted);white-space:nowrap}
/* mentions */
.mentions{padding:14px 18px 0}
.mentions h2{font-size:14px;margin:0 0 10px;display:flex;gap:8px;align-items:center}
.mlist{display:flex;flex-direction:column;gap:8px}
.m{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px;display:flex;gap:12px;align-items:flex-start}
.m.read{opacity:.55}
.m .body{flex:1;min-width:0}
.m .meta{font-size:11px;color:var(--muted);margin-bottom:4px}
.m .meta b{color:var(--ink)}
.m .txt{font-size:13px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.m .acts{display:flex;gap:6px;flex:0 0 auto}
.m a.open{text-decoration:none}
.mempty{color:var(--muted);font-size:12px;padding:4px 0 12px}
.unreaddot{width:8px;height:8px;border-radius:50%;background:var(--accent);flex:0 0 auto;margin-top:5px}
</style></head><body>
<header><h1>👁 Lookout</h1>
<button id="refreshBtn" onclick="refresh()">🔄 PR 가져오기</button>
<span class="sub" id="sub">로딩…</span>
<span class="sub" id="engStat" style="margin-left:14px"></span>
<button id="themeBtn" onclick="cycleTheme()" title="테마 전환 (시스템 · 라이트 · 다크)" style="margin-left:auto">🖥 시스템</button>
<span class="sub" style="margin-left:12px">5초마다 자동 새로고침</span></header>
<div class="shell">
<nav class="side">
  <div class="grp">PR 리뷰</div>
  <button id="tLane" class="active" onclick="setView('lane')"><span>🗂 레인별</span><span class="cnt" id="cLane">0</span></button>
  <button id="tAuthor" onclick="setView('author')"><span>👤 사람별</span><span class="cnt" id="cAuthor">0</span></button>
  <button id="tFeedback" onclick="setView('feedback')"><span>💬 리뷰 피드백</span><span class="cnt" id="cFeedback">0</span></button>
  <div class="grp">작업</div>
  <button id="tWork" onclick="setView('work')"><span>🛠 이슈 보드</span><span class="cnt" id="cWork">0</span></button>
</nav>
<div class="main">
<div class="filterbar" id="filterbar"></div>
<section class="composer" id="composer" style="display:none">
  <textarea id="topicText" placeholder="주제를 던지면 두 엔진이 토론해서 결론만 돌려줍니다 — 이슈 없이도 됩니다 (예: 이 파이프라인의 취약점은 무엇인가)"></textarea>
  <div class="crow">
    <input id="topicRepo" placeholder="읽을 저장소 (선택) — 예: zigbang/ceo-client">
    <button class="go" onclick="newTopic()">🗣 토론 시작</button>
  </div>
</section>
<section class="mentions" id="mentions" style="display:none"></section>
<div class="board" id="board"></div>
</div>
</div>
<div class="ov" id="ov"><div class="modal" id="modal"></div></div>
<script>
const LANES=__LANES__;const WORK_LANES=__WORK_LANES__;const TOPIC_REPO=__TOPIC_REPO__;
// Slack 미연동 — 멘션 섹션 숨김. Slack 연결 시 true 로 바꾸면 부활.
const SHOW_MENTIONS=false;
// ── 테마 (시스템/라이트/다크) — 클릭 순환, localStorage 저장 ──
const THEME_KEY='lookout_theme';
const THEME_ORDER=['auto','light','dark'];
const THEME_LABEL={auto:'🖥 시스템',light:'☀️ 라이트',dark:'🌙 다크'};
function themePref(){return localStorage.getItem(THEME_KEY)||'auto';}
function resolveTheme(p){return p==='auto'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):p;}
function isLight(){return resolveTheme(themePref())==='light';}
function renderThemeBtn(){const b=document.getElementById('themeBtn');if(b)b.textContent=THEME_LABEL[themePref()];}
function applyTheme(){document.documentElement.setAttribute('data-theme',resolveTheme(themePref()));renderThemeBtn();}
function repaintBoard(){try{render();}catch(e){}}
function cycleTheme(){const o=THEME_ORDER;localStorage.setItem(THEME_KEY,o[(o.indexOf(themePref())+1)%o.length]);applyTheme();repaintBoard();}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{if(themePref()==='auto'){applyTheme();repaintBoard();}});
// 색 hex를 비율만큼 어둡게 (라이트모드에서 연한 pill 글자색을 진하게)
function darken(hex,f){const h=hex.replace('#','');const n=parseInt(h.length===3?h.split('').map(x=>x+x).join(''):h,16);
  return '#'+[(n>>16)&255,(n>>8)&255,n&255].map(x=>Math.round(x*f).toString(16).padStart(2,'0')).join('');}
// 상태/심각도/repo 색 pill 인라인 스타일 — 라이트모드는 글자색을 어둡게
function pill(c){return isLight()
  ? `background:${c}22;color:${darken(c,.5)};border:1px solid ${c}66`
  : `background:${c}22;color:${c};border:1px solid ${c}55`;}
function stripe(c){return isLight()?darken(c,.72):c;}  // 카드/finding 좌측 컬러 스트라이프
applyTheme();
let DATA=[];let FEEDBACK=[];let VIEW='lane';let REPO='all';
let LANE_SCROLL={};
// 엔진 가용성 — 초기엔 낙관적(true)으로 두고 /api/engines 응답으로 갱신
let ENGINES={claude:{installed:true,logged_in:true,ready:true},codex:{installed:true,logged_in:true,ready:true}};
function engReady(e){return !!(ENGINES&&ENGINES[e]&&ENGINES[e].ready);}
function engReason(e){const s=ENGINES&&ENGINES[e];
  if(!s)return '상태 확인 중';
  if(!s.installed)return e+' CLI 미설치';
  if(!s.logged_in)return e+' 로그인 필요';
  return '';}
const STATUS_META={
  triage:{c:'#2dd4bf',ko:'대기'}, intake:{c:'#6b7688',ko:'시작됨'},
  reviewing:{c:'#fbbf24',ko:'리뷰중'}, verifying:{c:'#fbbf24',ko:'검증중'},
  commenting:{c:'#fbbf24',ko:'댓글작성'}, commented:{c:'#4ade80',ko:'댓글완료'},
  lgtm:{c:'#4ade80',ko:'LGTM'}, approve_blocked:{c:'#a78bfa',ko:'승인대기'},
  approving:{c:'#a78bfa',ko:'승인중'}, done:{c:'#6b7688',ko:'완료'},
  failed:{c:'#fb7185',ko:'실패'},
  spec:{c:'#a78bfa',ko:'설계토론'}, spec_blocked:{c:'#a78bfa',ko:'설계승인대기'}, implementing:{c:'#fbbf24',ko:'구현중'},
  impl_verify:{c:'#fbbf24',ko:'구현검증'}, verify_blocked:{c:'#fb7185',ko:'검토필요'}, pr_blocked:{c:'#a78bfa',ko:'PR승인대기'},
  pr_opening:{c:'#a78bfa',ko:'PR생성중'}};
function smeta(s){return STATUS_META[s]||{c:'#6b7688',ko:s};}
function ago(ts){if(!ts)return '';const s=Math.max(0,Date.now()/1000-ts);
  if(s<60)return Math.floor(s)+'초';if(s<3600)return Math.floor(s/60)+'분';
  if(s<86400)return Math.floor(s/3600)+'시간';return Math.floor(s/86400)+'일';}
function hhmm(ts){const d=new Date(ts*1000);
  return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')
    +':'+String(d.getSeconds()).padStart(2,'0');}
function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
// 엔진은 **굵게**·`코드`·```펜스```·- 목록 으로 답한다. 그대로 뿌리면 기호가 노출되고
// 긴 설계안은 읽을 수가 없다. esc() 로 먼저 막고 우리 태그만 넣는다(XSS 안전).
// 주의: 이 파일의 HTML 은 파이썬 문자열 리터럴이라 JS 안에서 개행 이스케이프를 쓸 수
// 없다(모듈 로드 시 실제 개행이 되어 스크립트가 죽는다). NL 상수로 대신한다.
const NL=String.fromCharCode(10);
function md(t){
  if(!t)return '';
  let s=esc(String(t));
  const blocks=[];
  s=s.replace(/```[a-zA-Z0-9]*([\s\S]*?)```/g,(m,code)=>{
    blocks.push(code.trim());   // 이스케이프(\\r·\\n)를 피한다
    return '@@B'+(blocks.length-1)+'@@';});
  s=s.replace(/`([^`]+?)`/g,'<code>$1</code>');
  s=s.replace(/\*\*([^*]+?)\*\*/g,'<strong>$1</strong>');
  const out=[]; let list=null;
  const close=()=>{if(list){out.push('</'+list+'>');list=null;}};
  for(const line of s.split(NL)){
    const t2=line.trim();
    const blk=t2.match(/^@@B(\d+)@@$/);
    if(blk){close();out.push('<pre class="code">'+blocks[+blk[1]]+'</pre>');continue;}
    const h=t2.match(/^#{1,6}\s+(.+)$/);
    if(h){close();out.push('<h5>'+h[1]+'</h5>');continue;}
    const ul=t2.match(/^[-*·]\s+(.+)$/), ol=t2.match(/^\d+[.)]\s+(.+)$/);
    if(ul||ol){
      const want=ul?'ul':'ol';
      if(list&&list!==want)close();
      if(!list){out.push('<'+want+'>');list=want;}
      out.push('<li>'+(ul?ul[1]:ol[1])+'</li>');continue;}
    close();
    if(!t2)continue;
    if(t2.startsWith('&gt;'))out.push('<blockquote>'+t2.slice(4).trim()+'</blockquote>');
    else out.push('<p>'+t2+'</p>');
  }
  close();
  return '<div class="md">'+out.join('')+'</div>';
}
function repoShort(r){return (r||'').split('/')[1]||r;}
const REPO_COLORS=['#2dd4bf','#a78bfa','#fbbf24','#60a5fa','#4ade80','#fb7185'];
function repoColor(r){let h=0;for(const ch of (r||''))h=(h*31+ch.charCodeAt(0))>>>0;return REPO_COLORS[h%REPO_COLORS.length];}
function setRepo(r){REPO=r;renderFilter();render();}
// 리뷰 뷰는 이슈 카드를 보지 않고, 작업 뷰는 이슈 카드만 본다. 한 보드에 섞으면
// Triage에 PR과 이슈가 뒤엉킨다.
function scopedData(){return VIEW==='work'?DATA.filter(c=>c.kind==='issue')
                                          :DATA.filter(c=>c.kind!=='issue');}
function viewData(){const src=scopedData();return REPO==='all'?src:src.filter(c=>c.repo===REPO);}
function viewFeedbackData(){return REPO==='all'?FEEDBACK:FEEDBACK.filter(f=>f.repo===REPO);}
function filterSource(){return VIEW==='feedback'?FEEDBACK:scopedData();}
function normalizeRepo(){const src=filterSource();if(REPO!=='all'&&!src.some(x=>x.repo===REPO))REPO='all';}
function renderFilter(){
  normalizeRepo();
  const src=filterSource();
  const repos=[...new Set(src.map(c=>c.repo))].sort();
  const bar=document.getElementById('filterbar');
  let h=`<button class="chip ${REPO==='all'?'on':''}" onclick="setRepo('all')">전체 <b>${src.length}</b></button>`;
  repos.forEach(r=>{const n=src.filter(c=>c.repo===r).length;const col=repoColor(r);
    const on=REPO===r;
    const onStyle=on?(isLight()?`background:${col}2e;color:${darken(col,.5)};border-color:${col}66`:`background:${col};color:#06101f;border-color:${col}`):'';
    h+=`<button class="chip ${on?'on':''}" style="${onStyle}" onclick="setRepo('${r}')"><span class="rdot" style="background:${col}"></span>${esc(repoShort(r))} <b>${n}</b></button>`;});
  bar.innerHTML=h;
}
const VIEW_TABS=[['tLane','lane'],['tAuthor','author'],['tFeedback','feedback'],['tWork','work']];
function setView(v){VIEW=v;
  for(const [id,name] of VIEW_TABS)
    document.getElementById(id).classList.toggle('active',v===name);
  document.getElementById('refreshBtn').textContent=(v==='work')?'🔄 이슈 가져오기':'🔄 PR 가져오기';
  document.getElementById('composer').style.display=(v==='work')?'':'none';
  renderFilter();render();}
function renderSideCounts(){
  const review=DATA.filter(c=>c.kind!=='issue').length;
  const work=DATA.filter(c=>c.kind==='issue').length;
  document.getElementById('cLane').textContent=review;
  document.getElementById('cAuthor').textContent=review;
  document.getElementById('cFeedback').textContent=FEEDBACK.length;
  document.getElementById('cWork').textContent=work;
}
function forceRender(){render_();}
async function load(){
  const [rb,re,rf]=await Promise.all([fetch('/api/board'),fetch('/api/engines'),fetch('/api/feedback')]);
  DATA=await rb.json();
  try{FEEDBACK=await rf.json();}catch(e){FEEDBACK=[];}
  try{ENGINES=await re.json();}catch(e){}
  document.getElementById('sub').textContent=
    (VIEW==='work'?scopedData().length+'개 이슈':scopedData().length+'개 카드');
  renderEngStat();
  renderSideCounts();
  renderFilter();
  render();
  if(SHOW_MENTIONS)loadMentions();
}
function renderEngStat(){
  const el=document.getElementById('engStat');if(!el)return;
  const parts=[['claude','Claude'],['codex','Codex']].map(([e,n])=>{
    const r=engReady(e);
    return `<span title="${r?(n+' 사용 가능'):engReason(e)}" style="color:${r?'var(--good)':'var(--dim)'}">${n} ${r?'✓':'✗'}</span>`;
  });
  el.innerHTML='⚙️ '+parts.join(' · ');
}
function reviewButtons(id){
  const defs=[['claude','리뷰 (Claude)'],['codex','리뷰 (Codex)']];
  if(!defs.some(([e])=>engReady(e)))
    return `<div class="engnote">⚠️ 리뷰 엔진 미설정 — <code>claude</code> 또는 <code>codex</code> CLI 로그인이 필요합니다.</div>`;
  let h='<div class="rev">';
  defs.forEach(([e,label])=>{
    h+= engReady(e)
      ? `<button class="${e}" onclick="act(event,'start',${id},'${e}')">${label}</button>`
      : `<button class="${e}" disabled title="${engReason(e)}">${label}</button>`;
  });
  return h+'</div>';
}
async function loadMentions(){
  const r=await fetch('/api/mentions');const M=await r.json();
  const wrap=document.getElementById('mentions');wrap.style.display='';
  const unread=M.filter(m=>m.status==='unread').length;
  let h=`<h2>📢 멘션 / 확인요청 ${unread?`<span class="pill">안읽음 ${unread}</span>`:''}</h2>`;
  if(!M.length){wrap.innerHTML=h+'<div class="mempty">아직 멘션 없음</div>';return;}
  h+='<div class="mlist">';
  M.forEach(m=>{
    const dot=m.status==='unread'?'<span class="unreaddot"></span>':'<span style="width:8px;flex:0 0 auto"></span>';
    const open=m.permalink?`<a class="open" href="${m.permalink}" target="_blank"><button>열기↗</button></a>`:'';
    h+=`<div class="m ${m.status==='unread'?'':'read'}">${dot}
      <div class="body"><div class="meta"><b>@${esc(m.user)}</b> · ${esc(m.channel)}</div>
      <div class="txt">${esc(m.text)}</div></div>
      <div class="acts">${open}
      ${m.status==='unread'?`<button onclick="mAct(${m.id},'read')">읽음</button>`:''}
      <button onclick="mAct(${m.id},'archive')">✕</button></div></div>`;
  });
  wrap.innerHTML=h+'</div>';
}
async function mAct(id,action){
  await fetch('/api/mention-action',{method:'POST',headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},body:JSON.stringify({action,mention_id:id})});
  loadMentions();
}
// 5초 폴링이 카드 DOM 을 통째로 다시 그린다. 입력 중이던 textarea 가 새로 만들어져
// 내용이 날아가므로 (1) 입력값을 DRAFTS 에 붙들고 (2) 보드 안에서 타이핑 중이면
// 그 사이클의 보드 재렌더를 건너뛴다(포커스·캐럿 위치까지 지키려면 이게 필요하다).
const DRAFTS={};
function draft(el){if(el&&el.id)DRAFTS[el.id]=el.value;}
function dval(id,fallback){return DRAFTS[id]!==undefined?DRAFTS[id]:(fallback||'');}
function clearDraft(id){delete DRAFTS[id];}
function typingInBoard(){
  const a=document.activeElement;
  if(!a||!/^(TEXTAREA|INPUT)$/.test(a.tagName))return false;
  const board=document.getElementById('board');
  return !!(board&&board.contains(a));
}
function render(){
  if(typingInBoard())return;          // 입력 중에는 보드를 건드리지 않는다
  return render_();
}
function render_(){VIEW==='feedback'?renderFeedback():VIEW==='author'?renderByAuthor()
    :VIEW==='work'?renderWork():renderLanes(LANES);}
function renderFeedback(){
  const list=viewFeedbackData();
  const board=document.getElementById('board');board.className='board stack';board.innerHTML='';
  const sec=document.createElement('div');sec.className='sec';
  const inspect=list.filter(f=>f.needs_inspection).length;
  sec.innerHTML=`<h2>🧭 리뷰 피드백 <span class="n">${list.length}</span>${inspect?`<span class="pill">확인 ${inspect}</span>`:''}</h2>`;
  const cc=document.createElement('div');cc.className='mlist';
  if(!list.length)cc.innerHTML='<div class="empty">아직 수집된 피드백 없음</div>';
  list.forEach(f=>cc.appendChild(feedbackItem(f)));
  sec.appendChild(cc);board.appendChild(sec);
}
function feedbackItem(f){
  const el=document.createElement('div');el.className=`m ${f.needs_inspection?'':'read'}`;
  const rc=repoColor(f.repo);const open=f.comment_url?`<a class="open" href="${f.comment_url}" target="_blank" onclick="event.stopPropagation()"><button>댓글↗</button></a>`:'';
  el.innerHTML=`<span class="rdot" style="background:${rc};margin-top:5px"></span>
    <div class="body"><div class="meta"><b>${esc(repoShort(f.repo))}#${f.pr}</b> · ${esc(f.profile)} · ${esc(f.snapshot_type)} · ${esc(f.status)}</div>
    <div class="txt">${esc(f.title||'(제목없음)')}</div>
    <div class="meta">👍 ${f.up||0} · 👎 ${f.down||0} · 😕 ${f.confused||0} · 💬 ${f.replies||0}</div></div>
    <div class="acts">${open}</div>`;
  el.onclick=()=>openFeedbackModal(f);
  return el;
}
// 작업 보드는 칸반이 아니다. 레인 이동은 워커가 시키고 사람이 하는 일은
// **게이트에 선 카드에 응답하는 것** 하나뿐이다 — 드래그도, 레인 간 이동도 없다.
// 레인 10개에 카드 4장이면 가로 폭의 80%가 빈 칸이고, 스크롤 비용만 내고
// 정보는 안 나온다. 그래서 '무슨 단계냐'가 아니라 '내가 뭘 해야 하냐'로 묶는다.
// (리뷰 보드는 카드가 수백 장이라 레인이 실제로 채워지므로 그대로 둔다.)
const WORK_GROUPS=[
  {key:'gate',label:'⚠️ 내 차례',lanes:['spec_blocked','verify_blocked','pr_blocked'],
   hint:'응답해야 다음으로 갑니다',empty:'지금 결정할 카드가 없습니다',always:true},
  {key:'run',label:'🔄 돌아가는 중',lanes:['spec','implementing','impl_verify','pr_opening'],
   hint:'엔진이 작업 중 — 기다리면 됩니다',empty:'돌고 있는 작업 없음',always:true},
  {key:'wait',label:'📥 대기 (내 이슈)',lanes:['triage'],
   hint:'여기서 작업을 시작합니다',empty:'할당된 이슈 없음',always:true},
  {key:'end',label:'🏁 끝난 것',lanes:['done','failed'],fold:true}];
const WORK_OPEN={};   // <details> 접힘 상태를 5초 갱신 너머로 보존
function renderWork(){
  const board=document.getElementById('board');
  board.querySelectorAll('details.grp').forEach(d=>WORK_OPEN[d.dataset.g]=d.open);
  const top=board.scrollTop;
  const by={};viewData().forEach(c=>{(by[c.status]=by[c.status]||[]).push(c)});
  board.className='board stack';board.innerHTML='';
  for(const g of WORK_GROUPS){
    const list=g.lanes.reduce((a,k)=>a.concat(by[k]||[]),[]);
    if(!list.length&&!g.always)continue;
    const head=`<span>${g.label}</span><span class="n">${list.length}</span>`
      +(g.hint&&list.length?`<span class="ghint">${g.hint}</span>`:'');
    let host;
    if(g.fold){
      host=document.createElement('details');host.className='grp sec';host.dataset.g=g.key;
      host.open=!!WORK_OPEN[g.key];
      host.innerHTML=`<summary><div class="ghead">${head}</div></summary>`;
    }else{
      host=document.createElement('div');host.className='sec g-'+g.key;
      host.innerHTML=`<div class="ghead">${head}</div>`;
    }
    const cc=document.createElement('div');cc.className=list.length?'rows':'';
    if(!list.length)cc.innerHTML=`<div class="empty">${g.empty||'—'}</div>`;
    list.forEach(c=>cc.appendChild(issueRow(c)));
    host.appendChild(cc);board.appendChild(host);
  }
  board.scrollTop=top;
}
function renderLanes(lanes){
  lanes=lanes||LANES;
  const board=document.getElementById('board');
  LANE_SCROLL={left:board.scrollLeft};
  board.querySelectorAll('.col .cards').forEach(cards=>LANE_SCROLL[cards.dataset.lane]=cards.scrollTop);
  const byLane={};lanes.forEach(([k])=>byLane[k]=[]);
  viewData().forEach(c=>{if(byLane[c.status])byLane[c.status].push(c)});
  board.className='board';board.innerHTML='';
  for(const [key,label] of lanes){
    const list=byLane[key]||[];
    const col=document.createElement('div');col.className='col';
    col.innerHTML=`<h2><span class="lh"><span class="dot" style="background:${smeta(key).c}"></span>${label}</span><span class="n">${list.length}</span></h2>`;
    const cc=document.createElement('div');cc.className='cards';cc.dataset.lane=key;
    if(!list.length)cc.innerHTML='<div class="empty">—</div>';
    list.forEach(c=>cc.appendChild(tile(c)));
    col.appendChild(cc);board.appendChild(col);
  }
  board.scrollLeft=LANE_SCROLL.left||0;
  board.querySelectorAll('.col .cards').forEach(cards=>cards.scrollTop=LANE_SCROLL[cards.dataset.lane]||0);
}
function renderByAuthor(){
  const byA={};viewData().forEach(c=>{(byA[c.author||'(unknown)']=byA[c.author||'(unknown)']||[]).push(c)});
  const board=document.getElementById('board');board.className='board stack';board.innerHTML='';
  Object.keys(byA).sort((a,b)=>byA[b].length-byA[a].length).forEach(author=>{
    const list=byA[author];
    const sec=document.createElement('div');sec.className='sec';
    sec.innerHTML=`<h2>👤 ${esc(author)} <span class="n">${list.length}</span></h2>`;
    const cc=document.createElement('div');cc.className='cards';
    list.forEach(c=>cc.appendChild(tile(c)));
    sec.appendChild(cc);board.appendChild(sec);
  });
}
// 보드는 **훑는 곳**이고 모달은 **결정하는 곳**이다. 카드마다 입력칸과 버튼을
// 달면 항목 4개에 화면이 꽉 차고, 정작 결정에 필요한 근거(블로커·합의문)는
// 카드에 안 들어가 어차피 모달을 열어야 했다. 행은 한 줄로 상태만 말한다.
const GATES=['spec_blocked','verify_blocked','pr_blocked','failed'];
function issueRow(c){
  const el=document.createElement('div');
  const gate=GATES.includes(c.status);
  el.className='irow'+(gate?' gate':'');
  const sm=smeta(c.status), rc=repoColor(c.repo);
  const RUNNING=['spec','implementing','impl_verify','pr_opening'];
  const v=c.verify||{}, ag=c.agreement||{};
  const P=[];
  P.push(`<span class="statuspill" style="${pill(sm.c)}">${sm.ko}</span>`);
  if(c.repo&&c.repo!==TOPIC_REPO)
    P.push(`<span class="repopill" style="${pill(rc)}"><span class="rdot" style="background:${rc}"></span>${esc(repoShort(c.repo))}</span>`);
  if(c.status!=='triage'&&c.engine)P.push(`<span class="pill">${esc(c.engine)}</span>`);
  if(c.mode)P.push(`<span class="pill">${c.mode==='debate'?'설계부터':'바로구현'}</span>`);
  if(v.engine)P.push(`<span class="pill" style="${pill(v.approved?'#4ade80':'#fb7185')}">🧾 ${esc(v.engine)} ${v.approved?'통과':'블로커 '+((v.blocking||[]).length)}</span>`);
  if(c.verify_override)P.push(`<span class="pill" style="${pill('#fbbf24')}">⚠️ 미통과 감수</span>`);
  if(ag.rounds)P.push(`<span class="pill" style="${pill(ag.blocked?'#fb7185':ag.settled?'#4ade80':'#fbbf24')}">🗣 ${ag.rounds}R ${ag.blocked?'결렬':ag.settled?'합의':'미합의'}</span>`);
  if(c.rounds>1)P.push(`<span class="pill">구현 ${c.rounds}R</span>`);
  if((c.changed||[]).length)P.push(`<span class="pill">${c.changed.length}개 파일</span>`);
  if(c.error)P.push(`<span class="pill" style="${pill('#fb7185')}" title="${esc(c.error)}">⚠️ ${esc(c.error.slice(0,40))}</span>`);
  const right=(RUNNING.includes(c.status)?`<span title="이 상태로 머문 시간">⏱ ${ago(c.updated_at)}</span>`
              :`<span>${ago(c.updated_at)} 전</span>`)
    +`<span class="igo">${gate?'확인 →':'→'}</span>`
    +((c.status==='triage'||c.status==='failed')
      ?`<button class="xbtn" title="목록에서 제외" onclick="ignoreCard(event,${c.id})">✕</button>`:'');
  el.innerHTML=`<span class="idot" style="background:${sm.c}"></span>
    <div class="imain">
      <div class="ihead"><span class="inum">${esc(c.display)}</span>
        <span class="ititle">${esc(c.title)||'(제목없음)'}</span></div>
      <div class="imeta">${P.join('')}</div>
    </div>
    <div class="iright">${right}</div>`;
  el.onclick=()=>openIssueModal(c);
  return el;
}
function tile(c){
  if(c.kind==='issue')return issueRow(c);
  const el=document.createElement('div');el.className='card';
  const dots=c.findings.map(f=>`<span class="dot ${f.severity||'low'}"></span>`).join('');
  let btns='', xbtn='';
  if(c.status==='triage'){
    btns=reviewButtons(c.id);
    xbtn=`<button class="xbtn" title="목록에서 제외" onclick="ignoreCard(event,${c.id})">✕</button>`;
  }
  if(c.status==='failed'){
    btns=`<div class="btns"><button class="go" onclick="act(event,'retry',${c.id})">↻ 재시도</button></div>`;
    xbtn=`<button class="xbtn" title="목록에서 제외" onclick="ignoreCard(event,${c.id})">✕</button>`;
  }
  if(c.status==='approve_blocked')btns=`<div class="btns"><button class="go" onclick="act(event,'unblock',${c.id})">🔓 승인(Unblock)</button></div>`;
  if((c.kind==='review'&&['commented','lgtm','done'].includes(c.status))||(c.kind==='approve'&&['approve_blocked','done'].includes(c.status)))
    btns+=`<div class="btns"><button class="go" onclick="reReview(event,${c.id})">🔄 재리뷰</button></div>`;
  if(['intake','reviewing','verifying','commenting'].includes(c.status))
    btns=`<div class="btns"><button class="stop" onclick="stopReview(event,${c.id})">🛑 리뷰 중지</button></div>`;
  if(c.dryrun_pending)
    btns+=`<div class="btns"><button class="go" onclick="publishDryRun(event,${c.id})">💬 dry-run 댓글 게시</button></div>`;
  const sm=smeta(c.status);
  el.style.borderLeftColor=stripe(sm.c);
  const statusPill=`<span class="statuspill" style="${pill(sm.c)}">${sm.ko}</span>`;
  const enginePill=(c.status!=='triage')?`<span class="pill">${c.engine}</span>`:'';
  const clo=c.closure&&(c.closure.resolved||c.closure.dismissed||c.closure.deferred||c.closure.unresolved||c.closure.pending)?`<span class="pill">✅${c.closure.resolved} ↪️${c.closure.deferred||0} ⚠️${c.closure.unresolved} 🧑‍⚖️${c.closure.pending||0}</span>`:'';
  const fb=c.feedback&&(c.feedback.up||c.feedback.down||c.feedback.confused||c.feedback.replies)
    ?`<span class="pill" title="리뷰 피드백 스냅샷">👍${c.feedback.up||0} 👎${c.feedback.down||0} 💬${c.feedback.replies||0}</span>`:'';
  const inspect=c.feedback&&c.feedback.needs_inspection?`<span class="pill" style="${pill('#fbbf24')}">피드백 확인</span>`:'';
  const rc=repoColor(c.repo);
  const repoPill=`<span class="repopill" style="${pill(rc)}"><span class="rdot" style="background:${rc}"></span>${esc(repoShort(c.repo))}</span>`;
  el.innerHTML=`${xbtn}<div class="pr">${repoPill} <span class="num">#${c.pr}</span></div>
    <div class="title">${esc(c.title)||'(제목없음)'}</div>
    <div class="row">${statusPill}<span class="pill">${esc(c.author)}</span>${enginePill}${inspect}</div>
    <div class="row"><span>@${c.head}</span>${dots?`<span class="row">${dots} ${c.findings.length}건</span>`:''}${clo}${fb}</div>
    ${c.error?`<div class="errline ${c.status==='triage'?'warn':''}" title="${esc(c.error)}">${esc(c.error)}</div>`:''}${btns}`;
  el.onclick=()=>openModal(c);
  return el;
}
const SEVC={high:'#fb7185',medium:'#fbbf24',low:'#2dd4bf'};
function openModal(c){
  const m=document.getElementById('modal');const sm=smeta(c.status);
  let html=`<span class="close" onclick="closeM()">✕ 닫기</span>
    <h3>#${c.pr} ${esc(c.title)}</h3>
    <div class="msub">${esc(c.repo)} · @${esc(c.author)} · <code>${c.head}</code>
      <span class="statuspill" style="${pill(sm.c)}">${sm.ko}</span></div>`;
  if(c.url)html+=`<div class="mlink"><a href="${c.url}" target="_blank">GitHub에서 열기 ↗</a></div>`;
  if(c.error)html+=`<div class="lbl">실패 사유</div><div class="pre">${esc(c.error)}</div>`;
  if(c.closure&&(c.closure.resolved||c.closure.dismissed||c.closure.deferred||c.closure.unresolved||c.closure.pending))
    html+=`<div class="lbl">이전 지적 추적</div><div class="pre">✅ ${c.closure.resolved} 해결 · ⏭️ ${c.closure.dismissed||0} 해명 수용 · ↪️ ${c.closure.deferred||0} 후속 작업 · ⚠️ ${c.closure.unresolved} 미해결 · 🧑‍⚖️ ${c.closure.pending||0} 운영자 판단</div>`;
  if(c.feedback&&(c.feedback.up||c.feedback.down||c.feedback.confused||c.feedback.replies)){
    html+=`<div class="lbl">리뷰 피드백</div><div class="pre">👍 ${c.feedback.up||0} · 👎 ${c.feedback.down||0} · 😕 ${c.feedback.confused||0} · 💬 ${c.feedback.replies||0}${c.feedback.needs_inspection?' · 확인 필요':''}</div>`;
  }
  if(c.findings.length){html+=`<div class="lbl">리뷰 결과 · ${c.findings.length}건</div>`;
    c.findings.forEach(f=>{const sc=SEVC[f.severity]||'#6b7688';
      html+=`<div class="finding" style="border-left-color:${stripe(sc)}">
        <div class="ft">${esc(f.title)}</div>
        <div class="meta">
          <span class="sevtag" style="${pill(sc)}">${esc(f.severity||'?')}</span>
          <span>확신도 ${esc(f.confidence||'?')}</span><span>·</span>
          <code>${esc(f.file||'')}${f.line?(':'+esc(f.line)):''}</code>
          <span class="fstatus">${esc(f.status)}</span>
        </div>
        <div class="pre">${esc(f.problem)}</div>
        ${f.fix?`<div class="lbl2">제안</div><div class="pre">${esc(f.fix)}</div>`:''}
        ${['dismiss_pending','defer_pending'].includes(f.status)?`<div class="lbl2">작성자 결정 근거</div><div class="pre">${esc(f.decision_evidence||'')}</div>${f.status==='defer_pending'?`<div class="lbl2">후속 참조</div><div class="pre">${esc(f.decision_follow_up||'후속 참조 없음')}</div>`:''}<div class="btns"><button class="go" onclick="acceptAuthorDecision(event,${f.id},'accept_author_decision')">🧑‍⚖️ 작성자 결정 수용</button></div>`:''}
        ${f.status==='deferred'?`<div class="lbl2">작성자 결정 근거</div><div class="pre">${esc(f.decision_evidence||'')}</div><div class="lbl2">후속 참조</div><div class="pre">${esc(f.decision_follow_up||'후속 참조 없음')}</div>`:''}
        ${['posted','confirmed','unresolved'].includes(f.status)?`<div class="btns"><button class="go" onclick="acceptAuthorDecision(event,${f.id},'operator_dismiss')">🧑‍⚖️ 운영자 직접 수용</button></div>`:''}
      </div>`});
  }else html+='<p class="sub">아직 finding 없음</p>';
  if(c.comments.length){html+='<div class="lbl">게시된 / 게시될 댓글</div>';
    c.comments.forEach(cm=>{const pending=(cm.type==='comment_dryrun'&&c.dryrun_pending);
      html+=`<div class="cmt">${esc(cm.body)}</div>${cm.url?`<div class="msub"><a href="${cm.url}" target="_blank">${esc(cm.url)}</a></div>`:`<div class="msub">${pending?'(dry-run · 미게시)':'(dry-run preview)'}</div>`}`});
    if(c.dryrun_pending)
      html+=`<div class="btns"><button class="go" onclick="publishDryRun(event,${c.id})">💬 dry-run 댓글 게시</button></div>`;}
  m.className='modal';
  m.innerHTML=html;document.getElementById('ov').classList.add('show');
}
function openIssueModal(c){
  // 구성 원칙: 위에는 "지금 결정에 필요한 것"만, 나머지는 접어 둔다.
  // 긴 본문은 md() 로 렌더한다 — 엔진이 마크다운으로 답하므로 원문 그대로는 못 읽는다.
  const m=document.getElementById('modal'), sm=smeta(c.status);
  const AG=c.agreement||{}, IM=c.impl||{}, V=c.verify||{};
  const sec=(title,body,open)=>body?`<details class="sec"${open?' open':''}>`
      +`<summary><span>${title}</span></summary><div class="body">${body}</div></details>`:'';

  const head=`<span class="close" onclick="closeM()">✕ 닫기</span>
    <h3><span class="num">${esc(c.display)}</span>${esc(c.title)||'(제목없음)'}</h3>
    <div class="msub">${esc(c.repo===TOPIC_REPO?'주제 토론':c.repo)}${(c.assignees||[]).length?' · '+esc((c.assignees||[]).join(', ')):''}
      <span class="statuspill" style="${pill(sm.c)}">${sm.ko}</span>
      ${c.mode?`<span class="pill">${c.mode==='debate'?'설계부터':c.mode==='debate_only'?'주제 토론':'바로 구현'}</span>`:''}
      ${c.engine&&c.status!=='triage'?`<span class="pill">${esc(c.engine)}</span>`:''}
      ${c.url?`<a href="${esc(c.url)}" target="_blank">이슈 ↗</a>`:''}
      ${c.pr_url?`<a href="${esc(c.pr_url)}" target="_blank">PR ↗</a>`:''}</div>`;

  // ── 1. 지금 사람이 봐야 할 것 ─────────────────────────────
  // 왜 이 카드가 내 차례인지를 **한 줄로** 먼저 말한다. 근거는 그 아래다.
  const WHY={
    verify_blocked:['⚖️','엔진끼리 합의하지 못했습니다 — 아래 블로커를 직접 판단하세요',''],
    spec_blocked:['🧑‍⚖️','설계가 나왔습니다 — 구현을 시작할지 결정하세요',''],
    pr_blocked:['🔒','검증을 통과했습니다 — PR 로 올릴지 결정하세요',''],
    failed:['⚠️','실패한 카드입니다 — 사유를 보고 재시도할지 정하세요','bad'],
    triage:['📥','아직 시작하지 않은 이슈입니다 — 지시를 적고 방식을 고르세요',''],
  }[c.status];
  let h='';
  if(WHY)h+=`<div class="why ${WHY[2]}"><span>${WHY[0]}</span><span>${WHY[1]}</span></div>`;

  // ── 끝난 카드: 결정이 아니라 **결과**를 먼저 말한다 ─────────
  // 전에는 어떻게 끝났는지가 어디에도 없고, 접힌 섹션 7개를 열어 봐야
  // PR 이 나갔는지 알 수 있었다.
  if(c.status==='done'){
    const done=c.pr_url?['🏁',`PR 로 나갔습니다 — draft 이므로 <b>ready 전환은 직접</b> 하셔야 합니다`,'']
      :c.pr_dryrun?['🧪','dry-run 이라 실제 PR 은 올라가지 않았습니다','flat']
      :c.debate_only?['🗣','주제 토론으로 종료했습니다 — 구현하지 않았습니다','flat']
      :['🏁','완료 처리됐습니다','flat'];
    h+=`<div class="done ${done[2]}"><span>${done[0]}</span><span>${done[1]}</span></div>`;
    const T=c.timeline||[];
    const span=T.length>1?ago(Math.min(...T.map(e=>e.ts))):'';
    const st=[];
    if(c.rounds)st.push(['구현',`${c.rounds}라운드`,'']);
    if(V.engine)st.push(['교차 검증',`${V.engine} ${V.approved?'통과':'미통과'}`,V.approved?'ok':'no']);
    if((c.changed||[]).length)st.push(['변경',`${c.changed.length}개 파일`,'']);
    if((c.debate||[]).length)st.push(['설계 토론',`${c.debate.length}턴`,'']);
    if(span)st.push(['첫 기록',`${span} 전`,'']);
    if(st.length)
      h+=`<div class="stats">`+st.map(x=>`<div class="stat"><div class="k">${x[0]}</div>`
        +`<div class="v ${x[2]}">${esc(x[1])}</div></div>`).join('')+`</div>`;
    if(c.pr_url)h+=`<a class="outlink" href="${esc(c.pr_url)}" target="_blank">PR 열기 ↗</a>`;
  }
  if(c.verify_override)
    h+=`<div class="why bad"><span>⚠️</span><span>검증 미통과를 감수하고 넘어온 카드입니다 — 블로커가 남아 있습니다</span></div>`;
  if(c.error)h+=`<div class="lbl">실패 사유</div><div class="errline">${esc(c.error)}</div>`;

  const blk=(V.blocking||[]).map(b=>`<div class="finding" style="border-left-color:${stripe('#fb7185')}">
      <div class="ft">${esc(b.file||'')}${b.line?(':'+esc(b.line)):''}</div>
      ${md(b.problem)}${b.fix?`<div class="lbl2">고치는 방향</div>${md(b.fix)}`:''}</div>`).join('');
  if(V.engine)
    h+=`<div class="lbl">교차 검증 · ${esc(V.engine)}${V.fallback?' (동일 엔진 폴백)':''} · `
      +`${V.approved?'통과':'미통과 '+(V.blocking||[]).length+'건'}</div>`
      +(V.summary?md(V.summary):'')+blk
      +((V.out_of_scope||[]).length?`<div class="lbl2">스코프 밖 변경</div>${md((V.out_of_scope||[]).map(x=>'- '+x).join(NL))}`:'');

  // 설계 게이트에서만 "결정하라"가 성립한다 — 그 뒤 레인엔 누를 버튼이 없으므로
  // 같은 문구를 띄우면 사람에게 할 수 없는 일을 요구하게 된다.
  if((AG.unresolved||[]).length)
    h+=`<div class="lbl">${c.status==='spec_blocked'
        ?`미합의 ${AG.unresolved.length}건 — 승인 전에 결정해야 합니다`
        :c.status==='done'
        ?`후속 확인 ${AG.unresolved.length}건 — PR 본문에도 체크박스로 들어갔습니다`
        :`설계 단계 미합의 ${AG.unresolved.length}건 — PR 본문에 함께 남습니다`}</div>`
      +md(AG.unresolved.map(x=>'- '+x).join(NL));

  // ── 2. 조작부 — 입력칸과 버튼은 **여기** 하나뿐이다 ─────────
  // 보드 행에도 두면 id 가 겹치고(specText 가 엉뚱한 칸을 읽는다) 무엇보다
  // 근거를 읽기 전에 누르게 된다.
  const INPUT={
    triage:['ins','추가 지시 (선택) — 이 이슈를 어떻게 처리할지',c.instruction],
    spec_blocked:['spec','합의에 대한 피드백 (선택) — 승인 시 수정 지시로, 다시 토론 시 방향 지시로 쓰입니다',''],
    verify_blocked:['spec',"선택 — 수정 요청이면 구현자에게(비우면 남은 블로커 그대로), 다시 검증이면 검증자에게 '이 관점으로 보라'로 갑니다",''],
    pr_blocked:['spec','수정 요청 (선택) — 비워두면 검증이 남긴 지적을 그대로 넘깁니다',''],
  }[c.status];
  let f='';
  if(INPUT)
    f+=`<div class="instr"><textarea id="${INPUT[0]}${c.id}" placeholder="${esc(INPUT[1])}"
        oninput="draft(this)">${esc(dval(INPUT[0]+c.id,INPUT[2]||''))}</textarea>`
      +(c.status==='spec_blocked'&&c.debate_only
        ?`<input id="trepo${c.id}" placeholder="구현할 저장소 (예: zigbang/ceo-client)"
            value="${esc(dval('trepo'+c.id,c.target_repo))}" oninput="draft(this)">`:'')
      +`</div>`;
  if(c.status==='triage')
    f+=`<div class="btns"><button class="go" onclick="startWork(event,${c.id},'implement')">🛠 바로 구현</button>`
      +`<button onclick="startWork(event,${c.id},'debate')">🗣 설계부터</button></div>`;
  if(c.status==='failed')
    f+=`<div class="btns"><button class="go" onclick="act(event,'retry',${c.id})">↻ 재시도</button></div>`;
  if(c.status==='spec_blocked')
    f+=`<div class="btns">${c.debate_only
      ?`<button class="go" onclick="implementTopic(event,${c.id})">🛠 이 결론으로 구현</button><button onclick="approveSpec(event,${c.id})">✅ 완료로 닫기</button>`
      :`<button class="go" onclick="approveSpec(event,${c.id})">${AG.settled?'✅ 설계 승인 — 구현 시작':'⚠️ 미합의인데 승인'}</button>`}`
      +`<button onclick="resumeDebate(event,${c.id})">🔁 다시 토론</button>`
      +`<button onclick="rejectSpec(event,${c.id})">↩︎ 반려</button></div>`;
  if(c.status==='verify_blocked')
    f+=`<div class="btns"><button class="go" onclick="requestChanges(event,${c.id})">↩︎ 수정 요청</button>`
      +`<button onclick="rerunVerify(event,${c.id})">🔁 다시 검증</button>`
      +`<button onclick="verifyOverride(event,${c.id})">⚠️ 그래도 PR 로</button></div>`
      +`<div class="sub">수정 요청·다시 검증 모두 위 입력칸의 내용을 넘깁니다 — 수정 요청은 구현자에게(비우면 위 블로커가 그대로), 다시 검증은 검증자에게 "이 관점으로 보라"로 갑니다.</div>`;
  if(c.status==='pr_blocked')
    f+=`<div class="btns"><button class="go" onclick="approvePr(event,${c.id})">${c.verify_override?'⚠️ 미통과인데 PR 올리기':'🚀 PR 올리기 승인'}</button>`
      +`<button onclick="requestChanges(event,${c.id})">↩︎ 수정 요청</button></div>`;

  // ── 3. 접어 두는 상세 ─────────────────────────────────────
  const implBody=(IM.summary?md(IM.summary):'')
    +(IM.verification?`<div class="lbl2">검증 실행</div>${md(IM.verification)}`:'')
    +((IM.open_questions||[]).length?`<div class="lbl2">남은 결정</div>${md(IM.open_questions.map(x=>'- '+x).join(NL))}`:'')
    +(IM.risk?`<div class="lbl2">위험</div>${md(IM.risk)}`:'');
  h+=sec(`🛠 구현 요약${IM.done===false?' · 부분 구현':''}`, implBody, true);

  const agWarn=(AG.rounds&&(AG.settled===null||AG.settled===undefined))
    ?`<div class="errline warn">합의 여부 미기록(구버전 카드) — 합의된 것인지 판단할 수 없습니다. 토론 기록을 직접 읽으세요.</div>`
    :(AG.rounds&&AG.settled===false
      ?`<div class="errline warn">양쪽이 AGREE 로 끝나지 않았습니다 — 아래는 마지막 제안자 안이고 반대신문이 남아 있습니다.</div>`:'');
  h+=sec(`📄 합의된 설계${AG.rounds?` · ${AG.rounds}라운드`:''}${AG.settled===false?' · 미합의':''}`,
         AG.design?agWarn+md(AG.design):'', c.status==='spec_blocked');

  if((c.debate||[]).length){
    const turns=c.debate.map(t=>{
      if(t.role==='operator')return `<div class="finding" style="border-left-color:${stripe('#2dd4bf')}">
        <div class="ft">🧑 운영자 개입</div>${md(t.claim)}</div>`;
      const col=t.role==='proposer'?'#e19267':'#8faedc';
      return `<div class="finding" style="border-left-color:${stripe(col)}">
        <div class="ft">r${t.round} · ${t.role==='proposer'?'제안':'반대신문'}(${esc(t.engine||'')}) · ${esc(t.verdict||'')}</div>
        ${md(t.claim)}
        ${(t.evidence||[]).length?`<div class="lbl2">근거</div><div class="pre">${esc((t.evidence||[]).join(', '))}</div>`:''}
        ${t.proposal?`<div class="lbl2">안</div>${md(t.proposal)}`:''}</div>`;}).join('');
    h+=sec(`🗣 토론 기록 · ${c.debate.length}턴`, turns);
  }

  h+=sec(`📝 지시·피드백`,
    (c.instruction?`<div class="lbl2">운영자 지시</div>${md(c.instruction)}`:'')
    +(c.spec_amendment?`<div class="lbl2">설계 수정 지시</div>${md(c.spec_amendment)}`:'')
    +(c.feedback?`<div class="lbl2">구현자에게 넘어간 지적</div>${md(c.feedback)}`:''));

  h+=sec(`📁 변경 파일 · ${(c.changed||[]).length}개`,
    (c.changed||[]).length?`<div class="pre">${(c.changed||[]).map(f=>`<div>${esc(f)}</div>`).join('')}</div>`:'');

  if(c.branch&&c.parent_repo_path){
    const name=c.branch.split('/').pop();
    h+=sec('💻 직접 돌려보기',
      `<div class="pre"><div>대상 <code>${esc(c.target_repo||'')}</code> · 브랜치 <code>${esc(c.branch)}</code>`
      +`${c.commit?` · 커밋 <code>${esc(c.commit)}</code>`:''}</div></div>`
      +`<div class="lbl2">내 워크트리 (권장 — orca)</div>`
      +`<div class="pre"><div><code>orca worktree create --repo path:${esc(c.parent_repo_path)} --name ${esc(name)} --setup run</code></div>`
      +`<div class="tdet">봇 워크트리(<code>${esc(c.worktree||'-')}</code>)는 다른 카드가 시작하면 브랜치가 갈립니다</div></div>`);
  }

  if((c.timeline||[]).length){
    const rows=c.timeline.map(e=>`<div class="tlrow"><code>${hhmm(e.ts)}</code>`
      +`<span class="tlab">${esc(e.label)}</span><span class="tdet">${esc(e.detail||'')}</span></div>`).join('');
    h+=sec(`⏱ 진행 기록 · ${c.timeline.length}건`, `<div class="tl">${rows}</div>`);
  }
  if(c.pr_dryrun&&!c.pr_url&&c.status!=='done')
    h+=`<div class="sub" style="margin-top:10px">🧪 dry_run_pr=true — 실제 PR 은 올라가지 않았습니다</div>`;

  m.className='modal doc';
  m.innerHTML=`<div class="mhead">${head}</div><div class="mbody">${h}</div>`
    +(f?`<div class="mfoot">${f}</div>`:'');
  m.querySelector('.mbody').scrollTop=0;
  document.getElementById('ov').classList.add('show');
}
function openFeedbackModal(f){
  const m=document.getElementById('modal');const rc=repoColor(f.repo);
  let html=`<span class="close" onclick="closeM()">✕ 닫기</span>
    <h3>${esc(repoShort(f.repo))}#${f.pr} 리뷰 피드백</h3>
    <div class="msub">${esc(f.repo)} · card #${f.card_id} · snapshot #${f.id}
      <span class="statuspill" style="${pill(f.needs_inspection?'#fbbf24':'#4ade80')}">${f.needs_inspection?'확인 필요':'수집됨'}</span></div>
    <div class="row"><span class="repopill" style="${pill(rc)}"><span class="rdot" style="background:${rc}"></span>${esc(f.profile)}</span><span class="pill">${esc(f.snapshot_type)}</span><span class="pill">${esc(f.status)}</span></div>
    <div class="title" style="margin-top:12px">${esc(f.title)||'(제목없음)'}</div>
    <div class="lbl">반응</div><div class="pre">👍 ${f.up||0} · 👎 ${f.down||0} · 😕 ${f.confused||0} · 💬 ${f.replies||0}</div>`;
  if(f.comment_url)html+=`<div class="mlink"><a href="${f.comment_url}" target="_blank">GitHub 댓글 열기 ↗</a></div>`;
  html+=`<div class="lbl">API</div><div class="pre">/api/feedback/${f.id}</div>`;
  m.className='modal';
  m.innerHTML=html;document.getElementById('ov').classList.add('show');
}
function closeM(){document.getElementById('ov').classList.remove('show')}
document.getElementById('ov').onclick=e=>{if(e.target.id==='ov')closeM()};
const ACT_MSG={approve_spec:'설계 승인 — 구현을 시작합니다 ✅',reject_spec:'설계 반려 — 대기로 되돌렸습니다',start:'리뷰 시작 — 곧 분석을 시작합니다 ⏳',rereview:'재리뷰 시작 — 곧 분석을 시작합니다 🔄',unblock:'승인 진행 중 🔓',ignore:'목록에서 제외됨',stop:'리뷰 중지됨 🛑'};
async function act(e,action,id,engine){e.stopPropagation();
  showToast(ACT_MSG[action]||'처리됨', action!=='ignore');
  let j={};
  try{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},body:JSON.stringify({action,card_id:id,engine:engine||'claude'})});j=await r.json();}catch(err){}
  if(['start','rereview'].includes(action)&&j&&j.ok===false)
    showToast(action==='rereview'?'재리뷰를 시작할 수 없습니다 — PR/head 상태를 확인하세요':'시작할 수 없습니다 — '+(engine?engReason(engine)||'엔진 상태 확인':'엔진 상태 확인'),false);
  load();}
function showToast(msg,spin){
  let t=document.getElementById('toast');
  if(!t){t=document.createElement('div');t.id='toast';t.className='toast';document.body.appendChild(t);}
  t.innerHTML=(spin?'<span class="spin"></span>':'')+msg;
  t.classList.add('show');clearTimeout(window._tt);
  window._tt=setTimeout(()=>t.classList.remove('show'),2600);
}
async function startWork(e,id,mode){e.stopPropagation();
  const ta=document.getElementById('ins'+id);
  const text=ta?ta.value:'';
  showToast(mode==='debate'?'설계 토론 시작 🗣':'구현 시작 🛠',true);
  const send=(body)=>fetch('/api/action',{method:'POST',
    headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},
    body:JSON.stringify(body)}).then(r=>r.json()).catch(()=>({ok:false}));
  // 지시를 먼저 저장한다 — 시작이 먼저 들어가면 워커가 지시 없는 seed를 읽을 수 있다
  if(text.trim()&&!(await send({action:'save_instruction',card_id:id,text})).ok){
    showToast('지시 저장 실패 — 시작하지 않았습니다',false);load();return;}
  const j=await send({action:mode==='debate'?'start_debate':'start_impl',card_id:id,engine:'claude'});
  if(j.ok===false)showToast('시작할 수 없습니다 — 엔진 상태를 확인하세요',false);
  else clearDraft('ins'+id);
  load();}
function specText(id){const t=document.getElementById('spec'+id);return t?t.value.trim():'';}
async function sendAction(body){
  return fetch('/api/action',{method:'POST',
    headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},
    body:JSON.stringify(body)}).then(r=>r.json()).catch(()=>({ok:false}));}
async function approveSpec(e,id){e.stopPropagation();
  const t=specText(id);
  if(!confirm(t?'이 수정 지시를 얹어 구현을 시작할까요? — '+t:'합의된 설계 그대로 구현을 시작할까요?'))return;
  showToast(t?'설계 승인(수정 지시 포함) ✅':'설계 승인 — 구현을 시작합니다 ✅',true);
  const j=await sendAction({action:'approve_spec',card_id:id,text:t});
  if(j.ok===false)showToast('승인할 수 없습니다',false);
  else clearDraft('spec'+id);
  load();}
async function implementTopic(e,id){e.stopPropagation();
  const ri=document.getElementById('trepo'+id), repo=ri?ri.value.trim():'';
  if(!repo){showToast('구현할 저장소를 입력하세요 (config 의 impl_repo_paths 에 있는 것)',false);return;}
  const t=specText(id);
  if(!confirm('이 결론으로 '+repo+' 에서 구현을 시작할까요?'+(t?' 추가 지시: '+t:'')))return;
  showToast('구현 시작 🛠',true);
  const j=await sendAction({action:'implement_topic',card_id:id,repo,text:t});
  if(j.ok===false)showToast('시작할 수 없습니다 — 저장소가 impl_repo_paths 에 있는지 확인하세요',false);
  else {clearDraft('spec'+id);clearDraft('trepo'+id);}
  load();}
async function resumeDebate(e,id){e.stopPropagation();
  const t=specText(id);
  if(!t){showToast('피드백을 입력해야 다시 토론할 수 있습니다',false);return;}
  if(!confirm('이 피드백을 넣고 토론을 재개할까요? — '+t))return;
  showToast('토론 재개 🔁',true);
  const j=await sendAction({action:'resume_debate',card_id:id,text:t});
  if(j.ok===false)showToast('재개할 수 없습니다',false);
  else {clearDraft('spec'+id);}
  load();}
function rejectSpec(e,id){e.stopPropagation();
  if(confirm('설계를 반려하고 대기로 되돌릴까요? 토론 기록은 보관됩니다.'))act(e,'reject_spec',id);}
async function rerunVerify(e,id){e.stopPropagation();
  const t=specText(id);
  if(!confirm(t?'이 관점으로 같은 커밋을 다시 검증할까요? — '+t
               :'같은 커밋을 다시 검증할까요? (구현은 바뀌지 않고 검증만 재실행합니다)'))return;
  showToast(t?'다시 검증 🔁 관점 전달':'다시 검증 🔁',true);
  const j=await sendAction({action:'rerun_verify',card_id:id,text:t});
  if(j.ok===false)showToast('재검증할 수 없습니다',false);
  else clearDraft('spec'+id);
  load();}
async function verifyOverride(e,id){e.stopPropagation();
  if(!confirm('검증이 통과하지 못한 상태 그대로 PR 승인 단계로 넘길까요? 블로커는 PR 본문에 남습니다.'))return;
  const j=await sendAction({action:'verify_override',card_id:id});
  if(j.ok===false)showToast('넘길 수 없습니다',false);
  load();}
async function requestChanges(e,id){e.stopPropagation();
  const t=specText(id);
  if(!confirm(t?'이 지적을 넘겨 구현 단계로 되돌릴까요? — '+t
               :'검증이 남긴 지적을 그대로 넘겨 구현 단계로 되돌릴까요?'))return;
  showToast('수정 요청 ↩︎ 구현으로 되돌립니다',true);
  const j=await sendAction({action:'request_changes',card_id:id,text:t});
  if(j.ok===false)showToast('되돌릴 수 없습니다',false);
  else clearDraft('spec'+id);
  load();}
function approvePr(e,id){e.stopPropagation();
  if(confirm('이 브랜치를 push하고 draft PR을 올릴까요? (ready 전환은 직접 하셔야 합니다)'))act(e,'unblock',id);}
async function newTopic(){
  const ta=document.getElementById('topicText'), ri=document.getElementById('topicRepo');
  const text=ta.value.trim();
  if(!text){showToast('주제를 입력하세요',false);return;}
  showToast('토론 시작 🗣',true);
  const r=await fetch('/api/new-topic',{method:'POST',
    headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},
    body:JSON.stringify({text,repo:ri.value.trim()})}).then(x=>x.json()).catch(()=>({ok:false}));
  if(r.ok){ta.value='';showToast(r.display+' 토론 시작 🗣',true);}
  else showToast('시작할 수 없습니다 — '+(r.reason||''),false);
  load();}
function stopReview(e,id){e.stopPropagation();
  if(confirm('이 리뷰를 강제 중지할까요? (진행 중인 분석을 종료하고 목록에서 제외)'))act(e,'stop',id);}
function reReview(e,id){e.stopPropagation();
  if(confirm('커밋 변경 없이 현재 head를 다시 리뷰할까요? 기존 승인 대기 게이트는 취소됩니다.'))act(e,'rereview',id);}
function ignoreCard(e,id){e.stopPropagation();
  if(confirm('이 PR을 목록에서 제외할까요? (리뷰하지 않음)'))act(e,'ignore',id);}
function publishDryRun(e,id){e.stopPropagation();
  if(confirm('dry-run 댓글을 실제 GitHub PR 댓글로 게시할까요?'))act(e,'publish_dryrun',id);}
async function acceptAuthorDecision(e,id,action){e.stopPropagation();
  if(!confirm(action==='operator_dismiss'?'이 지적을 운영자 판단으로 직접 수용할까요?':'작성자의 미반영/후속 결정을 수용할까요? 이 지적은 더 이상 LGTM을 막지 않습니다.'))return;
  const r=await fetch('/api/finding-action',{method:'POST',headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},body:JSON.stringify({action,finding_id:id})});
  const j=await r.json();showToast(j.ok?'작성자 결정 수용됨':'수용할 수 없습니다',false);closeM();load();}
async function refresh(){
  const b=document.getElementById('refreshBtn');const old=b.textContent;
  b.textContent='가져오는 중…';b.disabled=true;
  try{
    const r=await fetch('/api/refresh',{method:'POST',
      headers:{'Content-Type':'application/json','X-Lookout-Action':'1'},
      body:JSON.stringify({scope:VIEW==='work'?'work':'review'})});const j=await r.json();
    await load();
    b.textContent=j.added>0?`+${j.added}건 추가`:'최신 상태';
  }catch(e){b.textContent='실패';}
  setTimeout(()=>{b.textContent=old;b.disabled=false;},1800);
}
load();setInterval(load,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/" or path.startswith("/index"):
            html = (HTML.replace("__LANES__", json.dumps(LANES, ensure_ascii=False))
                        .replace("__WORK_LANES__", json.dumps(WORK_LANES, ensure_ascii=False))
                        .replace("__TOPIC_REPO__", json.dumps(TOPIC_REPO)))
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/board":
            self._send(200, json.dumps(build_board(), ensure_ascii=False))
        elif path == "/api/mentions":
            self._send(200, json.dumps(build_mentions(), ensure_ascii=False))
        elif path == "/api/feedback":
            try:
                body = json.dumps(build_feedback(params), ensure_ascii=False)
            except ValueError:
                self._send(400, "{}")
                return
            self._send(200, body)
        elif path == "/api/feedback/export.csv":
            try:
                body = build_feedback_csv(params)
            except ValueError:
                self._send(400, "{}")
                return
            self._send(200, body, "text/csv; charset=utf-8")
        elif path.startswith("/api/feedback/"):
            try:
                item = build_feedback_detail(path.rsplit("/", 1)[-1])
            except ValueError:
                item = None
            self._send(200 if item else 404, json.dumps(item or {}, ensure_ascii=False))
        elif path == "/api/engines":
            self._send(200, json.dumps(engines.availability(), ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if not mutation_allowed(
                self.client_address[0], self.headers.get("X-Lookout-Action", ""),
                self.headers.get("Origin", ""), self.headers.get("Host", "")):
            self._send(403, '{"ok":false}')
            return
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or "{}")
        if self.path == "/api/refresh":
            self._send(200, json.dumps(refresh_poll(data.get("scope", "review"))))
            return
        if self.path == "/api/new-topic":
            self._send(200, json.dumps(create_topic(data.get("text", ""),
                                                    data.get("repo", ""))))
            return
        if self.path == "/api/action":
            ok = do_action(data.get("action"), int(data.get("card_id", 0)),
                           data.get("engine", "claude"), data.get("text"),
                           data.get("repo"))
        elif self.path == "/api/finding-action":
            ok = do_finding_action(data.get("action"), int(data.get("finding_id", 0)))
        elif self.path == "/api/mention-action":
            ok = do_mention_action(data.get("action"), int(data.get("mention_id", 0)))
        else:
            self._send(404, "{}")
            return
        self._send(200, json.dumps({"ok": ok}))


def main():
    db.init()
    server = ThreadingHTTPServer((DASHBOARD_HOST, PORT), Handler)
    print(f"[dashboard] http://{DASHBOARD_HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
