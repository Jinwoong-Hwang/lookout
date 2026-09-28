import ipaddress
import json
import sqlite3
import unittest

from src import dashboard, db, ghclient, reviewer


class AuthorDecisionBoundaryTest(unittest.TestCase):
    def _wire(self, pages):
        """gh 호출을 출처별 고정 응답으로 대체한다."""
        saved = ghclient._run
        self.addCleanup(setattr, ghclient, "_run", saved)

        class Proc:
            def __init__(self, out):
                self.returncode, self.stdout = 0, out

        def fake_run(args, check=True):
            path = args[1] if len(args) > 1 else ""
            key = ("issues" if "/issues/" in path else
                   "reviews" if path.endswith("/reviews") else "comments")
            return Proc("\n".join(json.dumps(x) for x in pages.get(key, [])))

        ghclient._run = fake_run

    def test_only_the_author_own_text_is_collected(self):
        """봇 글과 남의 글은 회신이 아니다.

        작성자도 자기 lookout 을 돌리면 그 리뷰 코멘트가 같은 author_id 로
        올라온다(#10066 에서 10건). id 만으로 거르면 봇 글이 '작성자 회신' 이 된다.
        """
        pages = {
            "issues": [
                {"id": 1, "author_id": "42", "created_at": "2", "body": "의도적으로 유지합니다",
                 "html_url": "u1"},
                {"id": 2, "author_id": "42", "created_at": "3",
                 "body": f"지적입니다\n{ghclient.FP_MARKER}x -->", "html_url": "u2"},
                {"id": 3, "author_id": "7", "created_at": "4", "body": "남의 글", "html_url": "u3"},
                {"id": 4, "author_id": "42", "created_at": "5", "body": "   ", "html_url": "u4"},
            ],
            "reviews": [
                {"id": 9, "author_id": "42", "created_at": "6", "body": "2라운드 회신",
                 "html_url": "u9"},
            ],
            "comments": [],
        }
        self._wire(pages)
        got = ghclient.collect_author_replies(
            "owner/repo", 1, {"id": "42", "login": "author", "body": "## 보류 표",
                              "created_at": "1"})
        self.assertEqual([(r["source"], r["id"]) for r in got],
                         [("body", "body:pr"), ("issue", "issue:1"), ("review", "review:9")])

    def test_review_bodies_are_collected(self):
        """#10066 에서 작성자 회신 10라운드 중 9라운드가 리뷰 본문에 있었다."""
        self._wire({"issues": [], "comments": [],
                    "reviews": [{"id": 9, "author_id": "42", "created_at": "2",
                                 "body": "보류 — PO 결정 대기", "html_url": "u"}]})
        got = ghclient.collect_author_replies(
            "owner/repo", 1, {"id": "42", "login": "author", "body": "", "created_at": "1"})
        self.assertEqual([r["id"] for r in got], ["review:9"])
        self.assertEqual(got[0]["url"], "u")

    def test_trim_keeps_the_pinned_reply_and_the_pr_body(self):
        """보류 근거는 대개 가장 오래된 회신에 있다 — 최신부터 채우다 끊으면 그게 먼저 사라진다."""
        replies = [
            {"id": "body:pr", "source": "body", "created_at": "0", "body": "본문"},
            {"id": "issue:1", "source": "issue", "created_at": "1", "body": "오래된 보류 근거"},
            {"id": "issue:2", "source": "issue", "created_at": "2", "body": "x" * 100},
            {"id": "issue:3", "source": "issue", "created_at": "3", "body": "y" * 100},
        ]
        got = ghclient.trim_author_replies(replies, pinned="issue:1", budget=120)
        ids = [r["id"] for r in got]
        self.assertIn("body:pr", ids)
        self.assertIn("issue:1", ids)      # 예산과 무관하게 남는다
        self.assertIn("issue:3", ids)      # 남는 예산은 최신부터
        self.assertNotIn("issue:2", ids)

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
