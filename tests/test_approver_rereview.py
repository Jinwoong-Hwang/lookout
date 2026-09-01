import sqlite3
import unittest

from src import approver, db, ghclient


class ApproverRereviewTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.policy = {
            "profile_type": "code", "mode": "review",
            "approval_allowed": True,
        }
        self.review_id = db.upsert_card(
            self.c, "review:new", "review", "owner/repo", 1, "lgtm", "head",
            payload={"review_policy": self.policy},
        )
        self.gate_id = db.upsert_card(
            self.c, "pr-auto-review:owner/repo#1:approve:head", "approve",
            "owner/repo", 1, "archived", "head", blocked=1,
            payload={"review_policy": self.policy},
        )
        self.old_view, self.old_login = ghclient.pr_view, ghclient.my_login
        ghclient.pr_view = lambda *_: {
            "state": "OPEN", "headRefOid": "head", "author": {"login": "other"},
        }
        ghclient.my_login = lambda: "me"

    def tearDown(self):
        ghclient.pr_view, ghclient.my_login = self.old_view, self.old_login
        self.c.close()

    def test_lgtm_reactivates_archived_same_head_gate(self):
        approver.create_gate(
            self.c, self.c.execute("SELECT * FROM cards WHERE id=?", (self.review_id,)).fetchone(),
        )
        gate = self.c.execute("SELECT * FROM cards WHERE id=?", (self.gate_id,)).fetchone()
        self.assertEqual(gate["status"], "approve_blocked")
        self.assertEqual(gate["blocked"], 1)

    def test_archived_review_cannot_recreate_gate_from_stale_worker_row(self):
        stale_row = self.c.execute("SELECT * FROM cards WHERE id=?", (self.review_id,)).fetchone()
        db.set_status(self.c, self.review_id, "archived")
        approver.create_gate(self.c, stale_row)
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE id=?", (self.gate_id,)).fetchone()["status"],
            "archived",
        )


if __name__ == "__main__":
    unittest.main()
