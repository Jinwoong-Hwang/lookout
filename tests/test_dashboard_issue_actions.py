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

    def test_work_board_is_a_separate_view_from_review_board(self):
        """작업 레인을 리뷰 보드에 붙이면 컬럼이 16개가 되고 Triage에 PR 카드와
        이슈 카드가 섞인다. 리뷰 보드는 손대지 않는다."""
        review = [k for k, _ in dashboard.LANES]
        work = [k for k, _ in dashboard.WORK_LANES]
        self.assertEqual(len(review), len(set(review)))
        # 리뷰 보드는 종전 11레인 그대로, 작업 상태가 섞이지 않는다
        self.assertEqual(review, ["triage", "intake", "reviewing", "verifying",
                                  "commenting", "commented", "lgtm", "approve_blocked",
                                  "approving", "done", "failed"])
        self.assertEqual(dict(dashboard.LANES)["triage"], "📥 Triage (리뷰 대기)")
        for status in list(dashboard.WORK_START.values()) + ["impl_verify", "pr_blocked"]:
            self.assertIn(status, work)
            self.assertNotIn(status, review)
            self.assertNotIn(status, dashboard.ACTIVE_REVIEW)

    def test_review_and_work_views_never_show_each_others_cards(self):
        """뷰별로 kind를 갈라 보여준다 — 한 보드에 섞으면 Triage가 뒤엉킨다."""
        html = dashboard.HTML
        self.assertIn("VIEW==='work'?DATA.filter(c=>c.kind==='issue')", html)
        self.assertIn("DATA.filter(c=>c.kind!=='issue')", html)
        self.assertIn("renderLanes(VIEW==='work'?WORK_LANES:LANES)", html)


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


class RefreshScopeTest(unittest.TestCase):
    """작업 뷰에서 '이슈 가져오기'를 눌렀는데 PR 폴링이 돌면 리뷰 카드가 예고 없이
    늘어난다. 보고 있는 보드만 갱신한다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect,
                      "poll": dashboard.poller.poll,
                      "issues": dashboard.poller.poll_issues}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c

        dashboard.db.connect = fake_connect
        self.called = []
        dashboard.poller.poll = lambda _c: self.called.append("pr")
        dashboard.poller.poll_issues = lambda _c: self.called.append("issue")

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.poller.poll = self.saved["poll"]
        dashboard.poller.poll_issues = self.saved["issues"]
        self.c.close()

    def test_work_scope_polls_issues_only(self):
        dashboard.refresh_poll("work")
        self.assertEqual(self.called, ["issue"])

    def test_review_scope_polls_prs_only(self):
        dashboard.refresh_poll("review")
        self.assertEqual(self.called, ["pr"])

    def test_default_scope_is_review_so_existing_button_is_unchanged(self):
        dashboard.refresh_poll()
        self.assertEqual(self.called, ["pr"])

    def test_counts_only_the_scoped_kind(self):
        db.upsert_card(self.c, keys.review_key("a/b", 1, "s"), "review", "a/b", 1,
                       status="triage", head_sha="s")
        db.upsert_card(self.c, keys.issue_key(REPO, 2), "issue", REPO, 2, status="triage")
        self.assertEqual(dashboard.refresh_poll("work")["total"], 1)
        self.assertEqual(dashboard.refresh_poll("review")["total"], 1)


class SideNavTest(unittest.TestCase):
    """뷰 전환은 사이드 메뉴로, repo 필터바는 종전 위치·마크업 그대로."""

    def setUp(self):
        self.html = dashboard.HTML

    def test_view_switch_moved_out_of_the_header(self):
        header = self.html[self.html.index("<header>"):self.html.index("</header>")]
        self.assertNotIn('class="toggle"', header)
        self.assertIn('<nav class="side">', self.html)

    def test_side_menu_groups_review_and_work(self):
        for el in ("tLane", "tAuthor", "tFeedback", "tWork"):
            self.assertIn(f'id="{el}"', self.html)
        self.assertIn(">PR 리뷰<", self.html)
        self.assertIn(">작업<", self.html)

    def test_repo_filterbar_is_untouched_and_inside_main(self):
        self.assertIn('<div class="filterbar" id="filterbar"></div>', self.html)
        self.assertLess(self.html.index('<div class="main">'),
                        self.html.index('id="filterbar"'))

    def test_board_and_mentions_stay_with_the_filter_in_main(self):
        main = self.html[self.html.index('<div class="main">'):self.html.index("</nav>") + 10000]
        for el in ('id="filterbar"', 'id="mentions"', 'id="board"'):
            self.assertIn(el, main)
