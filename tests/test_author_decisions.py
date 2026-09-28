import ipaddress
import sqlite3
import unittest

from src import dashboard, db, ghclient, reviewer


class AuthorDecisionBoundaryTest(unittest.TestCase):
    def test_only_immutable_author_id_in_finding_window_is_accepted(self):
        fp = "owner/repo#1:src/a.ts:1:rule"
        marker = f"<!-- hermes:fp={fp} -->"
        comments = [
            {"id": "1", "author": "bot", "author_id": "9", "created_at": "1", "body": marker},
            {"id": "2", "author": "attacker", "author_id": "7", "created_at": "2",
             "body": "\n[author] 의도적으로 미반영"},
            {"id": "3", "author": "author", "author_id": "42", "created_at": "3",
             "body": "이 동작은 의도적으로 유지합니다"},
            {"id": "4", "author": "bot", "author_id": "9", "created_at": "4",
             "body": "<!-- hermes:fp=other -->"},
            {"id": "5", "author": "author", "author_id": "42", "created_at": "5",
             "body": "다른 지적 답변"},
        ]
        replies = ghclient.finding_author_replies(comments, fp, "42", "bot")
        self.assertEqual([r["id"] for r in replies], ["3"])

    def test_repeat_comment_does_not_hide_earlier_author_reply(self):
        fp = "owner/repo#1:src/a.ts:1:rule"
        marker = f"<!-- hermes:fp={fp} -->"
        comments = [
            {"id": "1", "author": "bot", "author_id": "9", "created_at": "1", "body": marker},
            {"id": "2", "author": "author", "author_id": "42", "created_at": "2",
             "body": "의도적으로 미반영"},
            {"id": "3", "author": "bot", "author_id": "9", "created_at": "3", "body": marker},
        ]
        replies = ghclient.finding_author_replies(comments, fp, "42", "bot")
        self.assertEqual([r["id"] for r in replies], ["2"])

    def test_reply_evidence_must_match_verified_comment_exactly(self):
        replies = [{"id": "2", "body": "의도적으로 미반영합니다"}]
        self.assertIsNotNone(reviewer._verified_reply(
            {"reply_comment_id": "2", "reply_evidence": "의도적으로 미반영"}, replies,
        ))
        self.assertIsNone(reviewer._verified_reply(
            {"reply_comment_id": "7", "reply_evidence": "의도적으로 미반영"}, replies,
        ))
        self.assertIsNone(reviewer._verified_reply(
            {"reply_comment_id": "2", "reply_evidence": "모델이 만든 문구"}, replies,
        ))

    def test_bundled_comment_links_plain_author_reply(self):
        """묶음 댓글이라고 지문 복붙을 요구하면 사람 회신은 절대 안 잡힌다(#10066)."""
        fp = "owner/repo#1:src/a.ts:1:rule-a"
        comments = [
            {"id": "1", "author": "bot", "author_id": "9", "created_at": "1",
             "body": f"<!-- hermes:fp={fp} -->\n<!-- hermes:fp=rule-b -->"},
            {"id": "2", "author": "author", "author_id": "42", "created_at": "2",
             "body": "A만 의도적으로 유지"},
        ]
        self.assertEqual(
            [r["id"] for r in ghclient.finding_author_replies(comments, fp, "42", "bot")],
            ["2"],
        )

    def test_author_own_bot_comment_is_not_a_reply(self):
        """작성자도 자기 인스턴스를 돌리면 봇 리뷰가 같은 author_id 로 들어온다."""
        fp = "owner/repo#1:src/a.ts:1:rule"
        comments = [
            {"id": "1", "author": "bot", "author_id": "9", "created_at": "1",
             "body": f"<!-- hermes:fp={fp} -->"},
            {"id": "2", "author": "author", "author_id": "42", "created_at": "2",
             "body": "지적입니다\n<!-- hermes:fp=owner/repo#1:src/b.ts:2:other -->"},
            {"id": "3", "author": "author", "author_id": "42", "created_at": "3",
             "body": "의도적으로 유지합니다"},
        ]
        self.assertEqual(
            [r["id"] for r in ghclient.finding_author_replies(comments, fp, "42", "bot")],
            ["3"],
        )

    def test_reply_from_an_earlier_window_is_not_dropped(self):
        """한 번 답하고 만 해명이 다음 라운드에 사라지면 안 된다."""
        fp = "owner/repo#1:src/a.ts:1:rule"
        marker = f"<!-- hermes:fp={fp} -->"
        comments = [
            {"id": "1", "author": "bot", "author_id": "9", "created_at": "1",
             "body": marker},
            {"id": "2", "author": "author", "author_id": "42", "created_at": "2",
             "body": "보류 — PO 결정 대기"},
            {"id": "3", "author": "bot", "author_id": "9", "created_at": "3",
             "body": marker},
            {"id": "4", "author": "author", "author_id": "42", "created_at": "4",
             "body": "다른 라운드 코멘트"},
        ]
        self.assertEqual(
            [r["id"] for r in ghclient.finding_author_replies(comments, fp, "42", "bot")],
            ["2", "4"],
        )

    def test_pr_body_is_offered_as_an_author_statement(self):
        """작성자는 '보류' 를 댓글이 아니라 PR 본문 표에 적어 둔다."""
        fp = "owner/repo#1:src/a.ts:1:rule"
        comments = [
            {"id": "1", "author": "bot", "author_id": "9", "created_at": "1",
             "body": f"<!-- hermes:fp={fp} -->"},
        ]
        body = reviewer._author_body_reply(
            {"login": "author", "id": "42", "created_at": "0",
             "body": "## 보류 항목\n| 지적 | 사유 |"},
        )
        self.assertEqual(
            [r["id"] for r in
             ghclient.finding_author_replies(comments, fp, "42", "bot", body)],
            ["pr-body"],
        )
        self.assertIsNone(reviewer._author_body_reply({"login": "author", "body": "  "}))

    def test_sticky_fingerprint_is_reverified_when_payload_changes(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        card_id = db.upsert_card(c, "review", "review", "owner/repo", 1,
                                 "commented", "head")
        db.upsert_finding(c, card_id, "owner/repo", 1, "head", "fp", "old", "{}",
                          "src/a.ts", 1, "high", "high", "dismiss_pending")
        same = db.revalidate_finding(c, card_id, "owner/repo", 1, "head", "fp",
                                     "old", "{}", "src/a.ts", 1, "high", "high")
        changed = db.revalidate_finding(c, card_id, "owner/repo", 1, "head", "fp",
                                        "new security issue", '{"problem":"new"}',
                                        "src/a.ts", 1, "high", "high")
        self.assertEqual(same, "sticky")
        self.assertEqual(changed, "dismiss_pending")
        self.assertEqual(c.execute("SELECT status FROM findings").fetchone()["status"],
                         "pending_verify")
        c.close()

    def test_mutations_require_loopback_and_csrf_header(self):
        # Pin the allowed networks so the assertion does not depend on whatever
        # dashboard_write_networks the operator happens to have in config.json.
        old_networks = dashboard.WRITE_NETWORKS
        dashboard.WRITE_NETWORKS = tuple(
            ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "192.168.0.0/16")
        )
        self.addCleanup(setattr, dashboard, "WRITE_NETWORKS", old_networks)
        self.assertTrue(dashboard.mutation_allowed(
            "127.0.0.1", "1", "http://127.0.0.1:8788", "127.0.0.1:8788",
        ))
        self.assertTrue(dashboard.mutation_allowed(
            "192.168.0.2", "1", "http://host:8788", "host:8788",
        ))
        self.assertFalse(dashboard.mutation_allowed(
            "203.0.113.2", "1", "http://host:8788", "host:8788",
        ))
        self.assertFalse(dashboard.mutation_allowed(
            "127.0.0.1", "", "http://127.0.0.1:8788", "127.0.0.1:8788",
        ))
        self.assertFalse(dashboard.mutation_allowed(
            "127.0.0.1", "1", "https://attacker.example", "127.0.0.1:8788",
        ))


if __name__ == "__main__":
    unittest.main()
