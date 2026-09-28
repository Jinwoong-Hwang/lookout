"""리뷰어·검증자에게 주는 '이미 지적된 것' 원장과 '작성자가 쓴 글'.

대화 전사를 그대로 싣던 방식을 대신한다. 전사는 봇 코멘트가 대부분이라 예산을
혼자 먹었고(#10066 실측: 127,397자 중 봇 코멘트 63,989 + CI 로그 60,211, 사람이
손으로 쓴 글은 2,529), 예산에 밀려 잘리면 '이미 지적함' 목록이 먼저 사라졌다.
그러면 리뷰어가 같은 문제를 새 rule 로 다시 발급한다 — #10066 의
direct-review-exit-loses-import-edits 와 import-review-edits-not-persisted 가
같은 문제인데 지문이 둘로 갈린 것이 그 결과다.

원장은 두 곳에서 온다.
  · 내 DB — rule·위치·제목에 더해 **상태와 작성자 결정**까지 정확히 안다.
    마크다운에는 없던 정보고, 재제기를 멈추는 신호가 바로 이것이다.
  · 남의 인스턴스 코멘트의 마커 — 제목·위치만. 상태는 우리가 알 수 없다.
    #10066 은 인스턴스 4대가 붙어 원장 55행 중 41행이 남의 것이었다.
"""
from . import ghclient

# 상태 → 리뷰어가 읽을 한 마디. 내부 이름을 그대로 노출하면 모델이 오해한다.
PHRASE = {
    "resolved": "고쳐짐",
    "rejected": "검증에서 기각(오탐)",
    "posted": "열려 있음", "confirmed": "열려 있음", "unresolved": "열려 있음",
    "pending_verify": "검증 중",
    "dismissed": "작성자: 의도적 (수용됨)",
    "dismiss_pending": "작성자: 의도적 (운영자 확인 대기)",
    "deferred": "작성자: 후속 이관 (수용됨)",
    "defer_pending": "작성자: 후속 이관 (운영자 확인 대기)",
}
EVIDENCE_CHARS = 90
NOTE_CHARS = 1200


def _cell(text: str, limit: int = 0) -> str:
    """표 한 칸 — 줄바꿈과 파이프가 들어가면 표가 깨진다."""
    out = " ".join((text or "").split()).replace("|", "/")
    return out[:limit] + "…" if limit and len(out) > limit else out


def rows(c, repo: str, pr: int, foreign=None):
    mine, seen = [], set()
    for f in c.execute(
            "SELECT * FROM findings WHERE repo=? AND pr_number=? ORDER BY id", (repo, pr)):
        rule = f["fp"].rsplit(":", 1)[-1]
        seen.add(rule)
        mine.append({
            "rule": rule,
            "where": f"{(f['file'] or '?').split('/')[-1]}:{f['line'] or ''}",
            "title": _cell(f["title"]),
            "state": PHRASE.get(f["status"], f["status"]),
            "note": _cell(f["decision_evidence"] or "", EVIDENCE_CHARS),
        })
    for x in (foreign or []):
        if x["rule"] in seen:
            continue
        seen.add(x["rule"])
        mine.append({
            "rule": x["rule"],
            "where": (x["where"] or "?").split("/")[-1],
            "title": _cell(x["title"]),
            "state": f"다른 리뷰어({x['login']}) 지적 — 상태 미상",
            "note": "",
        })
    return mine


def render(entries) -> str:
    if not entries:
        return "(이 PR 에 올라간 지적 없음)"
    out = ["| rule | 위치 | 제목 | 상태 | 작성자 답변 |", "|---|---|---|---|---|"]
    out += [f"| {e['rule']} | {e['where']} | {e['title']} | {e['state']} | {e['note']} |"
            for e in entries]
    return "\n".join(out)


def author_notes(replies) -> str:
    """작성자가 손으로 쓴 글. 사람이 쓴 분량은 원래 작아서 줄일 필요가 없다 —
    #10066 도 본문 9,141 + 회신 23,328자다. 봇 글이 빠지면 예산 문제가 사라진다."""
    if not replies:
        return "(작성자가 쓴 글 없음)"
    return "\n\n".join(
        f"[{r['source']} · {r['created_at']}] {r['body'][:NOTE_CHARS]}"
        + ("…" if len(r["body"]) > NOTE_CHARS else "")
        for r in replies)


def build(c, repo: str, pr: int, replies) -> tuple:
    """(원장, 작성자 글) — 리뷰·검증 프롬프트에 그대로 꽂는다."""
    try:
        foreign = ghclient.other_bot_findings(repo, pr, ghclient.my_login())
    except Exception:  # noqa: BLE001 - 남의 지적을 못 읽어도 내 원장은 내보낸다
        foreign = []
    return render(rows(c, repo, pr, foreign)), author_notes(replies)
