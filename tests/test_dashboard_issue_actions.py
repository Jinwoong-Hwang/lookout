import contextlib
import json
import sqlite3
import unittest

from src import dashboard, db, engines, keys

REPO = "acme/product-hub"


class DashboardIssueActionTest(unittest.TestCase):
    """이슈 카드의 시작 버튼: 지시를 먼저 저장하고, triage에서만 전이한다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect, "kick": dashboard.kick_tick,
                      "ready": engines.is_ready}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c          # 테스트 커넥션을 닫지 않는다

        dashboard.db.connect = fake_connect
        self.kicked = []
        dashboard.kick_tick = lambda: self.kicked.append(1)
        engines.is_ready = lambda _e: True

        self.key = keys.issue_key(REPO, 1767)
        self.card_id = db.upsert_card(
            self.c, self.key, "issue", REPO, 1767, status="triage",
            payload={"display": "PH-1767", "title": "[FE] www build file type 제거",
                     "url": f"https://github.com/{REPO}/issues/1767",
                     "labels": [], "assignees": ["me"]})

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        engines.is_ready = self.saved["ready"]
        self.c.close()

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def test_save_instruction_merges_and_keeps_poller_fields(self):
        self.assertTrue(dashboard.do_action("save_instruction", self.card_id, text="  www만 "))
        payload = self._payload()
        self.assertEqual(payload["instruction"], "www만")
        self.assertEqual(payload["display"], "PH-1767")   # poller가 넣은 값 보존

    def test_start_impl_moves_to_implementing_with_mode(self):
        self.assertTrue(dashboard.do_action("start_impl", self.card_id, "claude"))
        self.assertEqual(self._card()["status"], "implementing")
        self.assertEqual(self._payload()["mode"], "implement")
        self.assertEqual(self._card()["engine"], "claude")
        self.assertEqual(len(self.kicked), 1)
        types = [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]
        self.assertIn("work_started", types)

    def test_start_debate_moves_to_spec(self):
        self.assertTrue(dashboard.do_action("start_debate", self.card_id, "codex"))
        self.assertEqual(self._card()["status"], "spec")
        self.assertEqual(self._payload()["mode"], "debate")

    def test_start_is_rejected_once_work_began(self):
        dashboard.do_action("start_impl", self.card_id, "claude")
        self.assertFalse(dashboard.do_action("start_impl", self.card_id, "claude"))
        # 지시 수정도 막힌다 — 워커가 읽은 seed와 로그가 어긋나지 않게
        self.assertFalse(dashboard.do_action("save_instruction", self.card_id, text="늦은 지시"))

    def test_start_blocked_when_engine_not_ready(self):
        engines.is_ready = lambda _e: False
        self.assertFalse(dashboard.do_action("start_impl", self.card_id, "claude"))
        self.assertEqual(self._card()["status"], "triage")
        types = [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]
        self.assertIn("work_start_blocked", types)

    def test_review_actions_do_not_apply_to_issue_cards(self):
        self.assertFalse(dashboard.do_action("start", self.card_id, "claude"))
        self.assertEqual(self._card()["status"], "triage")

    def test_build_board_emits_issue_row_without_pr_machinery(self):
        dashboard.do_action("save_instruction", self.card_id, text="지시")
        rows = [r for r in dashboard.build_board() if r["kind"] == "issue"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["display"], "PH-1767")
        self.assertEqual(row["instruction"], "지시")
        self.assertEqual((row["findings"], row["comments"], row["head"]), ([], [], ""))

    def test_work_lanes_are_disjoint_from_review_lanes(self):
        lanes = [k for k, _ in dashboard.LANES]
        self.assertEqual(len(lanes), len(set(lanes)))
        for s in dashboard.WORK_START.values():
            self.assertIn(s, lanes)
            self.assertNotIn(s, dashboard.ACTIVE_REVIEW)


if __name__ == "__main__":
    unittest.main()


class IssueRetryRoutingTest(unittest.TestCase):
    """failed 재시도는 자기 레인으로 돌아가야 한다 — intake로 보내면 리뷰 레인이라
    kind 게이트 때문에 아무도 집어가지 않고 카드가 영원히 선다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect, "kick": dashboard.kick_tick}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c

        dashboard.db.connect = fake_connect
        dashboard.kick_tick = lambda: None

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        self.c.close()

    def test_issue_card_retries_into_implementing(self):
        key = keys.issue_key(REPO, 9)
        cid = db.upsert_card(self.c, key, "issue", REPO, 9, status="failed")
        self.assertTrue(dashboard.do_action("retry", cid))
        self.assertEqual(db.get_card(self.c, key)["status"], "implementing")

    def test_review_card_still_retries_into_intake(self):
        key = keys.review_key("acme/app", 9, "sha")
        cid = db.upsert_card(self.c, key, "review", "acme/app", 9,
                             status="failed", head_sha="sha")
        self.assertTrue(dashboard.do_action("retry", cid))
        self.assertEqual(db.get_card(self.c, key)["status"], "intake")
