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

    def test_a_held_finding_does_not_block_the_others(self):
        """보류 하나가 묶음 전체를 잡으면, 과잉 재제기를 고친 대가로 진짜 지적이 묻힌다.

        #10066 재현에서 매 패스마다 신규 결함(prod 회귀 포함)이 보류 2건에 묶여
        나가지 못했다.
        """
        other = "owner/repo#1:src/other.ts:20:another-rule"
        c = _conn()
        card_id = _card(c)
        _finding(c, card_id, "defer_pending")            # 작성자 답변 대기
        _finding(c, card_id, "confirmed", fp=other)      # 무관한 신규 지적
        reviewer.refresh_author_decisions = lambda *_: None

        commenter.process(c, _row(c, card_id))

        self.assertEqual(len(self.posted), 1, "보류가 아닌 지적은 나가야 한다")
        self.assertIn(other, self.posted[0])
        self.assertNotIn(FP, self.posted[0])             # 보류 건은 빠진다
        rows = dict(c.execute(
            "SELECT fp, status FROM findings WHERE card_id=?", (card_id,)).fetchall())
        self.assertEqual(rows[FP], "defer_pending")      # 보류는 그대로 대기
        self.assertEqual(rows[other], "posted")
        self.assertEqual(len(_events(c, "comment_held_author_decision")), 1)

    def test_reminder_skips_a_finding_another_instance_posted(self):
        """남이 올린 지적(comment_id='exists')은 리마인드 대상이 아니다.

        같은 PR 에 인스턴스가 여러 대 붙는다(#10066 은 4대). 남의 마커를 알아보게
        된 뒤로 우리는 그 지적을 'exists' 로 받아만 두는데, 다음 리뷰에서 closure
        가 그걸 '아직 안 고쳐짐' 으로 판정하면 force_post 가 켜지고, force 는
        마커 검사를 통째로 건너뛰었다 — 남의 지적 3건을 내 이름으로 전문 재게시
        하려 했다(#10066 재현에서 확인). 1회차에는 force 가 꺼져 있어 안 보인다.
        """
        mine = "owner/repo#1:src/mine.ts:30:my-rule"
        c = _conn()
        card_id = _card(c)                                # force_post=True (리마인드)
        _finding(c, card_id, "unresolved")                # 남이 올린 것
        c.execute("UPDATE findings SET comment_id='exists' WHERE fp=?", (FP,))
        _finding(c, card_id, "unresolved", fp=mine)       # 내가 올린 것
        ghclient.list_review_comments = lambda repo, pr: [
            {"body": f"🤖 남의 인스턴스 묶음\n{MARKER}"},
            {"body": "🤖 내 지난 묶음\n<!-- hermes:fp=owner/repo#1:src/mine.ts:30:my-rule -->"},
        ]
        reviewer.refresh_author_decisions = lambda *_: None

        commenter.process(c, _row(c, card_id))

        self.assertEqual(len(self.posted), 1)
        self.assertIn(mine, self.posted[0], "내가 올린 것은 리마인드한다")
        self.assertNotIn(FP, self.posted[0], "남이 올린 것은 다시 올리지 않는다")
        rows = dict(c.execute(
            "SELECT fp, comment_id FROM findings WHERE card_id=?", (card_id,)).fetchall())
        self.assertEqual(rows[FP], "exists", "남의 것이라는 표시는 유지된다")

    def test_only_held_findings_means_nothing_is_posted(self):
        c = _conn()
        card_id = _card(c)
        _finding(c, card_id, "defer_pending")
        reviewer.refresh_author_decisions = lambda *_: None

        commenter.process(c, _row(c, card_id))

        self.assertEqual(self.posted, [])
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
