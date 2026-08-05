import sqlite3
import unittest
from contextlib import contextmanager

from src import dashboard, db, ghclient, router


class RereviewTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:", isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.source_id = db.upsert_card(
            self.c, "review:source", "review", "owner/repo", 1, "commented", "head",
            payload={"title": "old", "intro": "do not copy"},
        )
        db.set_engine(self.c, self.source_id, "codex")
        self.old_pr_view = ghclient.pr_view
        ghclient.pr_view = lambda *_: {
            "state": "OPEN", "isDraft": False, "headRefOid": "head",
            "baseRefName": "main", "title": "title", "url": "https://example/pr/1",
            "author": {"login": "author"},
        }

    def tearDown(self):
        ghclient.pr_view = self.old_pr_view
        self.c.close()

    def test_same_source_is_idempotent_and_archives_blocked_gate(self):
        gate_id = db.upsert_card(
            self.c, "approve:head", "approve", "owner/repo", 1,
            "approve_blocked", "head", blocked=1,
        )
        first = router.create_rereview(self.c, self.source_id, "codex")
        second = router.create_rereview(self.c, self.source_id, "codex")

        self.assertEqual(first, second)
        self.assertEqual(
            self.c.execute("SELECT COUNT(*) n FROM cards WHERE kind='review'").fetchone()["n"], 2,
        )
        target = self.c.execute("SELECT * FROM cards WHERE id=?", (first,)).fetchone()
        self.assertEqual(target["status"], "intake")
        self.assertEqual(target["engine"], "codex")
        self.assertNotIn("intro", target["payload"])
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE id=?", (self.source_id,)).fetchone()["status"],
            "archived",
        )
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE id=?", (gate_id,)).fetchone()["status"],
            "archived",
        )

    def test_stale_or_closed_pr_is_rejected(self):
        for state, head in (("OPEN", "new-head"), ("CLOSED", "head")):
            with self.subTest(state=state, head=head):
                ghclient.pr_view = lambda *_, state=state, head=head: {
                    "state": state, "isDraft": False, "headRefOid": head,
                }
                self.assertIsNone(router.create_rereview(self.c, self.source_id, "codex"))
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE id=?", (self.source_id,)).fetchone()["status"],
            "commented",
        )

    def test_dashboard_action_starts_rereview(self):
        old_connect = db.connect
        old_ready, old_kick = dashboard.engines.is_ready, dashboard.kick_tick

        @contextmanager
        def connect():
            yield self.c

        try:
            db.connect = connect
            dashboard.engines.is_ready = lambda engine: engine == "codex"
            dashboard.kick_tick = lambda: None
            self.assertTrue(dashboard.do_action("rereview", self.source_id))
        finally:
            db.connect = old_connect
            dashboard.engines.is_ready, dashboard.kick_tick = old_ready, old_kick

        target = self.c.execute(
            "SELECT * FROM cards WHERE kind='review' AND id!=?", (self.source_id,),
        ).fetchone()
        self.assertIsNotNone(target)
        self.assertEqual(target["status"], "intake")

    def test_operator_acceptance_is_required_before_lgtm(self):
        finding_id = db.upsert_finding(
            self.c, self.source_id, "owner/repo", 1, "head", "fp", "security finding",
            "{}", "src/a.ts", 1, "high", "high", "dismiss_pending",
        )
        self.assertTrue(finding_id)
        finding = self.c.execute("SELECT id FROM findings WHERE fp='fp'").fetchone()
        db.set_finding_decision(
            self.c, finding["id"], "dismiss_pending", "head", "123",
            "의도적으로 미반영",
        )
        old_connect = db.connect
        old_view, old_kick = dashboard.ghclient.pr_view, dashboard.kick_tick

        @contextmanager
        def connect():
            yield self.c

        try:
            db.connect = connect
            dashboard.ghclient.pr_view = lambda *_: {
                "state": "OPEN", "isDraft": False, "headRefOid": "head",
            }
            dashboard.kick_tick = lambda: None
            self.assertEqual(
                self.c.execute("SELECT status FROM cards WHERE id=?", (self.source_id,)).fetchone()["status"],
                "commented",
            )
            self.assertTrue(dashboard.do_finding_action(
                "accept_author_decision", finding["id"],
            ))
        finally:
            db.connect = old_connect
            dashboard.ghclient.pr_view, dashboard.kick_tick = old_view, old_kick

        self.assertEqual(
            self.c.execute("SELECT status FROM findings WHERE id=?", (finding["id"],)).fetchone()["status"],
            "dismissed",
        )
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE id=?", (self.source_id,)).fetchone()["status"],
            "lgtm",
        )

    def test_operator_can_directly_accept_existing_posted_finding(self):
        db.upsert_finding(
            self.c, self.source_id, "owner/repo", 1, "head", "existing-fp",
            "existing security finding", "{}", "src/a.ts", 1, "high", "high", "posted",
        )
        finding_id = self.c.execute(
            "SELECT id FROM findings WHERE fp='existing-fp'",
        ).fetchone()["id"]
        old_connect = db.connect
        old_view, old_kick = dashboard.ghclient.pr_view, dashboard.kick_tick

        @contextmanager
        def connect():
            yield self.c

        try:
            db.connect = connect
            dashboard.ghclient.pr_view = lambda *_: {
                "state": "OPEN", "isDraft": False, "headRefOid": "head",
            }
            dashboard.kick_tick = lambda: None
            self.assertTrue(dashboard.do_finding_action("operator_dismiss", finding_id))
        finally:
            db.connect = old_connect
            dashboard.ghclient.pr_view, dashboard.kick_tick = old_view, old_kick

        finding = self.c.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        self.assertEqual(finding["status"], "dismissed")
        self.assertEqual(finding["decision_head"], "head")
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE id=?", (self.source_id,)).fetchone()["status"],
            "lgtm",
        )

    def test_operator_dismiss_clears_deferred_follow_up(self):
        db.upsert_finding(
            self.c, self.source_id, "owner/repo", 1, "head", "defer-fp",
            "deferred finding", "{}", "src/a.ts", 1, "high", "high", "defer_pending",
        )
        finding_id = self.c.execute("SELECT id FROM findings WHERE fp='defer-fp'").fetchone()["id"]
        db.set_finding_decision(self.c, finding_id, "defer_pending", "head", "123",
                                "후속 LOOK-123", "LOOK-123")
        old_connect, old_kick = db.connect, dashboard.kick_tick

        @contextmanager
        def connect():
            yield self.c

        try:
            db.connect = connect
            dashboard.kick_tick = lambda: None
            self.assertTrue(dashboard.do_finding_action("operator_dismiss", finding_id))
        finally:
            db.connect, dashboard.kick_tick = old_connect, old_kick

        finding = self.c.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        self.assertEqual(finding["status"], "dismissed")
        self.assertIsNone(finding["decision_follow_up"])


if __name__ == "__main__":
    unittest.main()
