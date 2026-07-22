import json
import sqlite3
import unittest

from src import commenter, db, ghclient, prompt_tpl, reviewer, verifier, worktree


class ReviewerClosureTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        policy = {"profile_type": "code", "max_findings": 8, "min_confidence": "medium"}
        self.old_id = db.upsert_card(
            self.c, "review:old", "review", "owner/repo", 1, "commented", "old",
            payload={"review_policy": policy},
        )
        self.new_id = db.upsert_card(
            self.c, "review:new", "review", "owner/repo", 1, "intake", "new",
            payload={"review_policy": policy},
        )
        self.c.execute("UPDATE cards SET engine='codex' WHERE id=?", (self.new_id,))
        self.fp = "owner/repo#1:src/example.ts:10:same-rule"
        db.upsert_finding(
            self.c, self.old_id, "owner/repo", 1, "old", self.fp, "same finding",
            json.dumps({"problem": "still relevant"}), "src/example.ts", 10,
            "medium", "high", "posted",
        )

    def tearDown(self):
        self.c.close()

    def _run(self, closure_status, findings, evidence="", reply_evidence=""):
        calls = []
        rendered = []
        old_view, old_diff, old_conversation = ghclient.pr_view, ghclient.pr_diff, ghclient.pr_conversation
        old_make, old_remove = worktree.make_worktree, worktree.remove_worktree
        old_render, old_run = prompt_tpl.render, reviewer.engines.run_json
        try:
            ghclient.pr_view = lambda *_: {"state": "OPEN", "headRefOid": "new"}
            ghclient.pr_diff = lambda *_: "diff"
            ghclient.pr_conversation = lambda *_: "author reply"
            worktree.make_worktree = lambda *_: "/tmp/review"
            worktree.remove_worktree = lambda *_: None
            def render(name, **tokens):
                rendered.append((name, tokens))
                return name

            prompt_tpl.render = render

            def run(prompt, **_):
                calls.append(prompt)
                if prompt == "closure.md":
                    return {"status": closure_status, "reason": "author reply judged",
                            "evidence": evidence, "reply_evidence": reply_evidence}
                return {"findings": findings}

            reviewer.engines.run_json = run
            reviewer.process(self.c, self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone())
        finally:
            ghclient.pr_view, ghclient.pr_diff, ghclient.pr_conversation = old_view, old_diff, old_conversation
            worktree.make_worktree, worktree.remove_worktree = old_make, old_remove
            prompt_tpl.render, reviewer.engines.run_json = old_render, old_run
        return calls, rendered

    def test_dismissed_and_deferred_findings_are_not_recreated(self):
        duplicate = [{
            "file": "src/example.ts", "line": 10, "rule": "same-rule",
            "title": "same finding", "severity": "medium", "confidence": "high",
        }]
        for status in ("dismissed", "deferred"):
            with self.subTest(status=status):
                self.c.execute("UPDATE findings SET status='posted', card_id=?", (self.old_id,))
                calls, _ = self._run(status, duplicate,
                                     reply_evidence="별도 후속으로 처리" if status == "deferred" else "")
                finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
                self.assertEqual(calls, ["closure.md", "review.md"])
                self.assertEqual(finding["status"], status)
                self.assertEqual(finding["card_id"], self.old_id)
                self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"], 1)
                verifier.process(self.c, self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone())
                old_comments, old_comment = ghclient.list_review_comments, ghclient.pr_comment
                posted = []
                try:
                    ghclient.list_review_comments = lambda *_: []
                    ghclient.pr_comment = lambda *args: posted.append(args)
                    commenter.process(self.c, self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone())
                finally:
                    ghclient.list_review_comments, ghclient.pr_comment = old_comments, old_comment
                self.assertEqual(posted, [])
                self.assertEqual(self.c.execute("SELECT status FROM cards WHERE id=?", (self.new_id,)).fetchone()["status"], "commented")

    def test_unresolved_closure_reattaches_existing_finding(self):
        self._run("unresolved", [])
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["card_id"], self.new_id)
        self.assertEqual(finding["status"], "confirmed")
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE id=?", (self.new_id,)).fetchone()["status"],
            "commenting",
        )

    def test_deferred_is_tracked_but_not_unresolved(self):
        self.c.execute("UPDATE findings SET status='deferred'")
        _, rendered = self._run("deferred", [], reply_evidence="관측되면 처리")
        self.assertEqual(rendered[0][1]["STATUS"], "deferred")
        self.assertEqual(db.unresolved_findings(self.c, "owner/repo", 1), [])
        self.assertEqual(db.closure_counts(self.c, "owner/repo", 1).get("deferred"), 1)

    def test_deferred_requires_author_reply_evidence(self):
        self._run("deferred", [])
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["status"], "confirmed")
        self.assertEqual(finding["card_id"], self.new_id)

    def test_deferred_reopens_only_with_current_code_evidence(self):
        self.c.execute("UPDATE findings SET status='deferred'")
        self._run("unresolved", [])
        self.assertEqual(self.c.execute("SELECT status FROM findings WHERE fp=?", (self.fp,)).fetchone()["status"], "deferred")

        self._run("unresolved", [], evidence="src/example.ts:10 changed behavior")
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["status"], "confirmed")
        self.assertEqual(finding["card_id"], self.new_id)


if __name__ == "__main__":
    unittest.main()
