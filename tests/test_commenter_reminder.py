"""리마인드 묶음이 commenter 자신의 closure 재확인에 지워지지 않아야 한다.

reviewer.process는 이전 미해결 finding을 'confirmed'로 다시 붙이고 force_post를
세워 작성자에게 다시 알린다. 그런데 commenter.process가 게시 직전 돌리는
refresh_author_decisions가 같은 finding을 'unresolved'로 되돌릴 수 있어서,
'confirmed'만 골라 담으면 댓글도 로그도 없이 리마인드가 사라졌다.
"""
import json
import sqlite3
import unittest

from src import commenter, db, ghclient, reviewer

MARKER = "<!-- hermes:fp=owner/repo#1:src/app.ts:10:rule -->"
FP = "owner/repo#1:src/app.ts:10:rule"
INTRO = "지난 리뷰의 아래 지적이 아직 반영되지 않은 것 같아 다시 확인 부탁드립니다."


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
    return c


def _card(c, force_post=True):
    payload = {"author": "author", "review_policy": {"profile_type": "code"}}
    if force_post:
        payload.update({"force_post": True, "intro": INTRO})
    return db.upsert_card(c, "review:head", "review", "owner/repo", 1, "commenting",
                          "head", payload=payload)


def _finding(c, card_id, status, fp=FP):
    db.upsert_finding(c, card_id, "owner/repo", 1, "head", fp, "제목",
                      json.dumps({"problem": "문제"}, ensure_ascii=False),
                      "src/app.ts", 10, "medium", "high", status)


def _row(c, card_id):
    return c.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()


def _events(c, type_):
    return c.execute("SELECT * FROM events WHERE type=?", (type_,)).fetchall()


class CommenterReminderTest(unittest.TestCase):
    def setUp(self):
        self._orig = (reviewer.refresh_author_decisions, ghclient.pr_comment,
                      ghclient.list_review_comments, commenter.CFG["dry_run_comments"])
        self.posted = []
        ghclient.pr_comment = lambda repo, pr, body: self.posted.append(body) or "url"
        ghclient.list_review_comments = lambda repo, pr: [{"body": f"🤖 이전 묶음\n{MARKER}"}]
        commenter.CFG["dry_run_comments"] = False

    def tearDown(self):
        (reviewer.refresh_author_decisions, ghclient.pr_comment,
         ghclient.list_review_comments, commenter.CFG["dry_run_comments"]) = self._orig

    def test_reminder_posts_after_closure_downgrades_to_unresolved(self):
        c = _conn()
        card_id = _card(c)
        _finding(c, card_id, "confirmed")

        def downgrade(conn, card):  # 실제 closure가 '아직 미해결'로 판정한 상황
            conn.execute("UPDATE findings SET status='unresolved' WHERE card_id=?", (card_id,))

        reviewer.refresh_author_decisions = downgrade
        commenter.process(c, _row(c, card_id))

        self.assertEqual(len(self.posted), 1, "리마인드 댓글이 게시되어야 한다")
        self.assertIn(INTRO, self.posted[0])
        self.assertIn(MARKER, self.posted[0])
        self.assertEqual(
            c.execute("SELECT status FROM findings WHERE card_id=?", (card_id,)).fetchone()["status"],
            "posted")
        self.assertEqual(_row(c, card_id)["status"], "commented")

    def test_nothing_to_post_is_logged(self):
        c = _conn()
        card_id = _card(c, force_post=False)
        _finding(c, card_id, "rejected")
        reviewer.refresh_author_decisions = lambda *_: None

        commenter.process(c, _row(c, card_id))

        self.assertEqual(self.posted, [])
        self.assertEqual(_row(c, card_id)["status"], "commented")
        self.assertEqual(len(_events(c, "comment_nothing_to_post")), 1,
                         "게시할 게 없으면 이유를 로그로 남겨야 한다")

    def test_force_post_survives_a_bundle_with_nothing_to_post(self):
        c = _conn()
        card_id = _card(c)
        _finding(c, card_id, "rejected")
        reviewer.refresh_author_decisions = lambda *_: None

        commenter.process(c, _row(c, card_id))

        self.assertEqual(self.posted, [])
        self.assertTrue(json.loads(_row(c, card_id)["payload"]).get("force_post"),
                        "게시하지 않았으면 force_post를 소진하지 않아야 한다")

    def test_force_post_is_consumed_once_posted(self):
        c = _conn()
        card_id = _card(c)
        _finding(c, card_id, "confirmed")
        reviewer.refresh_author_decisions = lambda *_: None

        commenter.process(c, _row(c, card_id))

        self.assertEqual(len(self.posted), 1)
        self.assertNotIn("force_post", json.loads(_row(c, card_id)["payload"]))


if __name__ == "__main__":
    unittest.main()
