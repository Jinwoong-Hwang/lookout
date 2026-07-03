import json
import sqlite3
import unittest

from src import config, db, feedback, ghclient, monitor, poller


class FeedbackTest(unittest.TestCase):
    def test_reaction_counts_uses_github_reaction_endpoint(self):
        old = ghclient.comment_reactions
        calls = []
        try:
            def fake(repo, comment_id):
                calls.append((repo, comment_id))
                return {"+1": 2, "-1": 1, "confused": 3, "total_count": 6}
            ghclient.comment_reactions = fake
            self.assertEqual(
                feedback._reaction_counts("owner/repo", {"id": 123}),
                {"+1": 2, "-1": 1, "confused": 3, "total_count": 6},
            )
            self.assertEqual(calls, [("owner/repo", "123")])
        finally:
            ghclient.comment_reactions = old

    def test_author_replies_stop_at_next_bot_review_comment(self):
        comments = [
            {"user": "bot", "body": "리뷰\n<!-- hermes:fp=a -->", "id": 1},
            {"user": "author", "body": "맥락 부족", "id": 2, "html_url": "u2"},
            {"user": "other", "body": "not author", "id": 3},
            {"user": "bot", "body": "다음 리뷰\n<!-- hermes:fp=b -->", "id": 4},
            {"user": "author", "body": "should not attach", "id": 5},
        ]
        replies = feedback._author_replies(comments, 0, "author", "bot")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["body"], "맥락 부족")
        self.assertEqual(replies[0]["url"], "u2")

    def test_bot_review_comment_requires_bot_login_when_given(self):
        comment = {"user": "author", "body": "인용 <!-- hermes:fp=a -->"}
        self.assertFalse(feedback._is_bot_review_comment(comment, "bot"))
        self.assertTrue(feedback._is_bot_review_comment({"user": "bot", "body": "<!-- hermes:fp=a -->"}, "bot"))

    def test_latest_for_card_dedupes_same_comment(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        base = {
            "card_id": 1, "repo": "owner/repo", "pr_number": 1,
            "head_sha": "h", "profile_type": "code", "comment_id": "99",
            "comment_url": "u", "author_replies": "[]",
            "outcome": "{}", "created_at": 1.0,
        }
        c.execute(
            """INSERT INTO review_feedback_snapshots(card_id,repo,pr_number,head_sha,profile_type,snapshot_type,
               comment_id,comment_url,reactions,author_replies,outcome,created_at)
               VALUES (:card_id,:repo,:pr_number,:head_sha,:profile_type,'manual',
               :comment_id,:comment_url,:reactions,:author_replies,:outcome,:created_at)""",
            {**base, "reactions": json.dumps({"+1": 1, "-1": 0, "confused": 0})},
        )
        c.execute(
            """INSERT INTO review_feedback_snapshots(card_id,repo,pr_number,head_sha,profile_type,snapshot_type,
               comment_id,comment_url,reactions,author_replies,outcome,created_at)
               VALUES (:card_id,:repo,:pr_number,:head_sha,:profile_type,'pr_closed',
               :comment_id,:comment_url,:reactions,:author_replies,:outcome,2.0)""",
            {**base, "reactions": json.dumps({"+1": 2, "-1": 1, "confused": 0})},
        )
        self.assertEqual(feedback.latest_for_card(c, 1)["up"], 2)
        self.assertEqual(feedback.latest_for_card(c, 1)["down"], 1)

    def test_monitor_does_not_archive_when_close_snapshot_fails(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('root:1','root','owner/repo',1,'h','monitoring',1,1)"""
        )
        card = c.execute("SELECT * FROM cards WHERE key='root:1'").fetchone()
        old_view = ghclient.pr_view
        old_snapshot = feedback.snapshot_pr
        try:
            ghclient.pr_view = lambda repo, pr: {"state": "MERGED"}
            def fail(*_args, **_kwargs):
                raise ghclient.GhError("api down")
            feedback.snapshot_pr = fail
            with self.assertRaises(ghclient.GhError):
                monitor.process_root(c, card)
            status = c.execute("SELECT status FROM cards WHERE key='root:1'").fetchone()["status"]
            self.assertEqual(status, "monitoring")
        finally:
            ghclient.pr_view = old_view
            feedback.snapshot_pr = old_snapshot

    def test_poller_does_not_archive_when_close_snapshot_fails(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review:1','review','owner/repo',1,'h','done',1,1)"""
        )
        old_allowlist = config.CFG["allowlist"]
        old_list = ghclient.pr_list_open
        old_snapshot = feedback.snapshot_pr
        try:
            config.CFG["allowlist"] = ["owner/repo"]
            ghclient.pr_list_open = lambda repo: []
            def fail(*_args, **_kwargs):
                raise ghclient.GhError("api down")
            feedback.snapshot_pr = fail
            poller.poll(c)
            status = c.execute("SELECT status FROM cards WHERE key='review:1'").fetchone()["status"]
            self.assertEqual(status, "done")
        finally:
            config.CFG["allowlist"] = old_allowlist
            ghclient.pr_list_open = old_list
            feedback.snapshot_pr = old_snapshot

    def test_classify_reply_reason(self):
        self.assertEqual(feedback.classify_reply_reason("이건 오탐 같아요"), "false-positive")
        self.assertEqual(feedback.classify_reply_reason("맥락 부족입니다"), "low-context")
        self.assertEqual(feedback.classify_reply_reason("영향 과장"), "overstated")
        self.assertEqual(feedback.classify_reply_reason("고마워요"), "other")


if __name__ == "__main__":
    unittest.main()
