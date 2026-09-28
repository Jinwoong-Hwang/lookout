import json
import os
import shutil
import tempfile
import unittest

from src import config, dashboard, db


class DashboardFeedbackApiTest(unittest.TestCase):
    def test_feedback_filters_build_safe_where_clause(self):
        where, vals, limit = dashboard._feedback_filters({
            "repo": ["zigbang/product-hub"],
            "profile": ["doc"],
            "pr": ["754"],
            "needs_inspection": ["1"],
            "limit": ["9999"],
        })
        self.assertIn("s.repo=?", where)
        self.assertIn("s.profile_type=?", where)
        self.assertIn("s.pr_number=?", where)
        self.assertTrue(any("json_extract" in w for w in where))
        self.assertEqual(vals, ["zigbang/product-hub", "doc", 754])
        self.assertEqual(limit, 500)

    def test_feedback_filters_reject_bad_numeric_params(self):
        with self.assertRaises(ValueError):
            dashboard._feedback_filters({"pr": ["x"]})
        with self.assertRaises(ValueError):
            dashboard._feedback_filters({"card_id": ["x"]})
        with self.assertRaises(ValueError):
            dashboard._feedback_filters({"limit": ["x"]})

    def test_feedback_row_hides_author_replies_by_default(self):
        class Row(dict):
            def __getitem__(self, key):
                return self.get(key)

        row = Row(
            id=1,
            repo="owner/repo",
            pr_number=2,
            card_id=3,
            profile_type="code",
            snapshot_type="manual",
            status="commented",
            payload=json.dumps({"title": "T"}),
            comment_url="https://example/comment",
            created_at=10.0,
            reactions=json.dumps({"+1": 1, "-1": 1, "confused": 0, "total_count": 2}),
            author_replies=json.dumps([{"body": "오탐"}]),
            outcome=json.dumps({"state": "OPEN"}),
        )
        summary = dashboard._feedback_row(row)
        self.assertEqual(summary["up"], 1)
        self.assertEqual(summary["down"], 1)
        self.assertEqual(summary["replies"], 1)
        self.assertTrue(summary["needs_inspection"])
        self.assertNotIn("author_replies", summary)
        detail = dashboard._feedback_row(row, include_private=True)
        self.assertNotIn("body", detail["author_replies"][0])
        self.assertEqual(detail["author_replies"][0]["url"], "")
        self.assertEqual(detail["outcome"]["state"], "OPEN")

    def test_feedback_csv_has_stable_header(self):
        old = dashboard.build_feedback
        try:
            dashboard.build_feedback = lambda params, include_private=False: [{
                "id": 1, "repo": "owner/repo", "pr": 2, "card_id": 3,
                "profile": "code", "snapshot_type": "manual",
                "status": "commented", "title": "T", "comment_url": "u",
                "created_at": 10.0, "up": 1, "down": 0, "confused": 0,
                "replies": 0, "needs_inspection": False,
            }]
            csv_body = dashboard.build_feedback_csv({"repo": ["owner/repo"]})
            self.assertTrue(csv_body.startswith("id,repo,pr,card_id,profile"))
            self.assertIn("owner/repo", csv_body)
        finally:
            dashboard.build_feedback = old

    def test_csv_cell_escapes_spreadsheet_formulas(self):
        self.assertEqual(dashboard._csv_cell("=cmd"), "'=cmd")
        self.assertEqual(dashboard._csv_cell("+cmd"), "'+cmd")
        self.assertEqual(dashboard._csv_cell("-cmd"), "'-cmd")
        self.assertEqual(dashboard._csv_cell("@cmd"), "'@cmd")
        self.assertEqual(dashboard._csv_cell("\t=cmd"), "'\t=cmd")
        self.assertEqual(dashboard._csv_cell("  =cmd"), "'  =cmd")
        self.assertEqual(dashboard._csv_cell("normal"), "normal")


if __name__ == "__main__":
    unittest.main()


class CardSurfacesOperatorGateTest(unittest.TestCase):
    """카드가 '내 차례' 와 '왜 조용했나' 를 말해야 한다.

    실사용에서 #10066 리뷰가 끝난 뒤 카드는 '댓글완료' 만 보였다. 실제로는 작성자
    결정 2건이 운영자를 기다리고 3건은 남이 이미 지적해 생략된 상태였는데, 그걸
    알려면 DB 를 열어야 했다.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = config.CFG["db_path"]
        config.CFG["db_path"] = os.path.join(self.tmp, "t.sqlite")
        db.init()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(config.CFG.__setitem__, "db_path", self.saved)

    def _card(self, c):
        return db.upsert_card(c, "review:h", "review", "o/r", 1, "commented", "h",
                              payload={"title": "t", "author": "a"})

    def _board(self):
        return {x["id"]: x for x in dashboard.build_board()}

    def test_pending_decisions_and_quiet_reason_reach_the_card(self):
        with db.connect() as c:
            card = self._card(c)
            db.upsert_finding(c, card, "o/r", 1, "h", "o/r#1:src/a.ts:held", "보류된 것",
                              "{}", "src/a.ts", "1", "medium", "high", "defer_pending")
            db.upsert_finding(c, card, "o/r", 1, "h", "o/r#1:src/b.ts:dup", "남이 말한 것",
                              "{}", "src/b.ts", "2", "medium", "high", "posted")
            c.execute("UPDATE findings SET comment_id='exists' WHERE fp LIKE '%dup'")
            db.log_event(c, "comment_nothing_to_post", "review:h",
                         {"postable": 3, "held_author_decision": 2, "force": False})

        got = self._board()[card]

        self.assertEqual(got["closure"]["pending"], 1)
        self.assertEqual(got["quiet"]["held_author_decision"], 2)
        dup = next(f for f in got["findings"] if f["title"] == "남이 말한 것")
        self.assertEqual(dup["comment_id"], "exists")

    def test_quiet_reason_clears_once_something_is_actually_posted(self):
        with db.connect() as c:
            card = self._card(c)
            db.log_event(c, "comment_nothing_to_post", "review:h", {"postable": 1})
            db.log_event(c, "comment_posted", "review:h", {"url": "u", "fps": []})

        self.assertIsNone(self._board()[card]["quiet"])
