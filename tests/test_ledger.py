"""원장 — 리뷰어가 '이미 지적된 것' 을 어떤 모양으로 보는가.

대화 전사를 대신한다. 전사에는 없던 두 칸이 핵심이다 — 상태와 작성자 답변.
#10066 에서 리뷰어는 '이 지적이 이미 있었다' 까지만 알고 '작성자가 보류로
답했다' 는 몰랐고, 그래서 같은 문제를 새 rule 로 다시 발급했다.
"""
import json
import sqlite3
import unittest

from src import db, ledger


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    return c


def _finding(c, card_id, rule, status, title="제목", file="src/a.ts", line="10",
             evidence=None):
    db.upsert_finding(c, card_id, "owner/repo", 1, "head",
                      f"owner/repo#1:{file}:{line}:{rule}", title,
                      json.dumps({"problem": "p"}), file, line, "medium", "high", status)
    if evidence:
        c.execute("UPDATE findings SET decision_evidence=? WHERE title=?", (evidence, title))


class LedgerRowsTest(unittest.TestCase):
    def setUp(self):
        self.c = _conn()
        self.card = db.upsert_card(self.c, "k", "review", "owner/repo", 1, "intake", "head")

    def test_status_is_rendered_as_a_phrase_not_an_internal_name(self):
        _finding(self.c, self.card, "r1", "defer_pending", title="보류된 것",
                 evidence="PO 결정 대기")
        _finding(self.c, self.card, "r2", "resolved", title="고친 것")
        _finding(self.c, self.card, "r3", "rejected", title="오탐")
        out = {r["rule"]: r for r in ledger.rows(self.c, "owner/repo", 1)}
        self.assertEqual(out["r1"]["state"], "작성자: 후속 이관 (운영자 확인 대기)")
        self.assertEqual(out["r1"]["note"], "PO 결정 대기")
        self.assertEqual(out["r2"]["state"], "고쳐짐")
        self.assertEqual(out["r3"]["state"], "검증에서 기각(오탐)")

    def test_other_instances_findings_are_included_without_a_status(self):
        """#10066 은 인스턴스 4대가 붙어 원장 55행 중 41행이 남의 것이었다."""
        _finding(self.c, self.card, "mine", "posted")
        foreign = [{"login": "breadceo", "fp": "x", "rule": "theirs",
                    "title": "남의 지적", "where": "pkg/src/b.ts:20"}]
        out = {r["rule"]: r for r in ledger.rows(self.c, "owner/repo", 1, foreign)}
        self.assertIn("theirs", out)
        self.assertIn("상태 미상", out["theirs"]["state"])
        self.assertEqual(out["theirs"]["where"], "b.ts:20")

    def test_my_row_wins_when_both_sides_have_the_same_rule(self):
        _finding(self.c, self.card, "same", "deferred", evidence="보류함")
        foreign = [{"login": "breadceo", "fp": "x", "rule": "same",
                    "title": "같은 것", "where": "src/a.ts:10"}]
        out = ledger.rows(self.c, "owner/repo", 1, foreign)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["state"], "작성자: 후속 이관 (수용됨)")

    def test_table_cells_survive_newlines_and_pipes(self):
        _finding(self.c, self.card, "r", "defer_pending",
                 title="줄바꿈\n들어간 제목", evidence="표 | 깨짐\n두 줄")
        table = ledger.render(ledger.rows(self.c, "owner/repo", 1))
        body = [l for l in table.splitlines() if l.startswith("| r ")][0]
        self.assertEqual(body.count("|"), 6)          # 칸 5개 = 파이프 6개
        self.assertIn("줄바꿈 들어간 제목", body)
        self.assertIn("표 / 깨짐 두 줄", body)

    def test_empty_ledger_is_explicit(self):
        self.assertEqual(ledger.render([]), "(이 PR 에 올라간 지적 없음)")


class AuthorNotesTest(unittest.TestCase):
    def test_notes_carry_the_source_so_the_model_can_cite_it(self):
        out = ledger.author_notes([
            {"source": "body", "created_at": "1", "body": "## 보류 항목"},
            {"source": "review", "created_at": "2", "body": "2라운드 회신"},
        ])
        self.assertIn("[body · 1] ## 보류 항목", out)
        self.assertIn("[review · 2] 2라운드 회신", out)

    def test_clipping_keeps_the_tail_where_the_deferral_table_lives(self):
        """앞에서 자르면 끝에 붙은 보류 표가 먼저 사라진다(셀프 리뷰 2회차 지적)."""
        body = "머리" * 2500 + "### 보류 항목 — 이 PR 범위 밖"
        out = ledger.author_notes(
            [{"source": "body", "created_at": "1", "body": body}], budget=2000)
        self.assertLess(len(out), len(body))
        self.assertIn("보류 항목", out)          # 꼬리가 살아 있다
        self.assertIn("본문 중략", out)

    def test_a_note_within_budget_is_untouched(self):
        out = ledger.author_notes(
            [{"source": "review", "created_at": "1", "body": "가" * 5000}])
        self.assertIn("가" * 5000, out)

    def test_no_notes_is_explicit(self):
        self.assertEqual(ledger.author_notes([]), "(작성자가 쓴 글 없음)")


if __name__ == "__main__":
    unittest.main()
