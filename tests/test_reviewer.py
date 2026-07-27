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

    def _run(self, closure_status, findings, evidence="", reply_evidence="",
             closure_error=False):
        calls = []
        rendered = []
        old_view, old_diff, old_conversation = ghclient.pr_view, ghclient.pr_diff, ghclient.pr_conversation
        old_author = ghclient.pr_author_identity
        old_comments = ghclient.issue_comments_structured
        old_login = ghclient.my_login
        old_make, old_remove = worktree.make_worktree, worktree.remove_worktree
        old_render, old_run = prompt_tpl.render, reviewer.engines.run_json
        try:
            ghclient.pr_view = lambda *_: {"state": "OPEN", "headRefOid": "new"}
            ghclient.pr_diff = lambda *_: "diff"
            ghclient.pr_conversation = lambda *_: "author reply"
            ghclient.pr_author_identity = lambda *_: {"login": "author", "id": "42"}
            ghclient.my_login = lambda: "bot"
            ghclient.issue_comments_structured = lambda *_: [
                {"id": "10", "author": "bot", "author_id": "1", "created_at": "1",
                 "body": commenter._marker(self.fp)},
                {"id": "11", "author": "author", "author_id": "42", "created_at": "2",
                 "body": reply_evidence or "일반 답변"},
            ]
            worktree.make_worktree = lambda *_: "/tmp/review"
            worktree.remove_worktree = lambda *_: None
            def render(name, **tokens):
                rendered.append((name, tokens))
                return name

            prompt_tpl.render = render

            def run(prompt, **_):
                calls.append(prompt)
                if prompt == "closure.md":
                    if closure_error:
                        raise RuntimeError("closure unavailable")
                    return {"status": closure_status, "reason": "author reply judged",
                            "evidence": evidence, "reply_evidence": reply_evidence,
                            "reply_comment_id": "11" if reply_evidence else ""}
                return {"findings": findings}

            reviewer.engines.run_json = run
            reviewer.process(self.c, self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone())
        finally:
            ghclient.pr_view, ghclient.pr_diff, ghclient.pr_conversation = old_view, old_diff, old_conversation
            ghclient.pr_author_identity = old_author
            ghclient.issue_comments_structured = old_comments
            ghclient.my_login = old_login
            worktree.make_worktree, worktree.remove_worktree = old_make, old_remove
            prompt_tpl.render, reviewer.engines.run_json = old_render, old_run
        return calls, rendered

    def test_author_decisions_wait_for_operator_and_are_not_recreated(self):
        duplicate = [{
            "file": "src/example.ts", "line": 10, "rule": "same-rule",
            "title": "same finding", "problem": "still relevant",
            "severity": "medium", "confidence": "high",
        }]
        for status in ("dismissed", "deferred"):
            with self.subTest(status=status):
                self.c.execute(
                    """UPDATE findings SET status='posted',card_id=?,decision_head=NULL,
                       decision_comment_id=NULL,decision_evidence=NULL""", (self.old_id,)
                )
                calls, _ = self._run(status, duplicate,
                                     reply_evidence=("별도 후속으로 처리" if status == "deferred"
                                                     else "의도적으로 현재 동작을 유지"))
                finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
                self.assertEqual(calls, ["closure.md", "review.md"])
                self.assertEqual(finding["status"],
                                 "dismiss_pending" if status == "dismissed" else "defer_pending")
                self.assertEqual(finding["card_id"], self.new_id)
                self.assertEqual(finding["decision_comment_id"], "11")
                self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"], 1)
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

    def test_duplicate_unresolved_finding_is_reverified_and_reposted_once(self):
        duplicate = [{
            "file": "src/example.ts", "line": 10, "rule": "same-rule",
            "title": "same finding", "severity": "medium", "confidence": "high",
        }]
        self._run("unresolved", duplicate)
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["card_id"], self.new_id)
        self.assertEqual(finding["status"], "pending_verify")
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"], 1)

        old_diff, old_conversation = ghclient.pr_diff, ghclient.pr_conversation
        old_make, old_remove = worktree.make_worktree, worktree.remove_worktree
        old_render, old_run = prompt_tpl.render, verifier.engines.run_json
        try:
            ghclient.pr_diff = lambda *_: "diff"
            ghclient.pr_conversation = lambda *_: "conversation"
            worktree.make_worktree = lambda *_: "/tmp/review"
            worktree.remove_worktree = lambda *_: None
            prompt_tpl.render = lambda *_args, **_kwargs: "verify"
            verifier.engines.run_json = lambda *_args, **_kwargs: {"confirmed": True}
            verifier.process(
                self.c,
                self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone(),
            )
        finally:
            ghclient.pr_diff, ghclient.pr_conversation = old_diff, old_conversation
            worktree.make_worktree, worktree.remove_worktree = old_make, old_remove
            prompt_tpl.render, verifier.engines.run_json = old_render, old_run

        old_comments, old_comment = ghclient.list_review_comments, ghclient.pr_comment
        posted = []
        try:
            ghclient.list_review_comments = lambda *_: [{"body": commenter._marker(self.fp)}]
            ghclient.pr_comment = lambda *args: posted.append(args) or "comment-url"
            commenter.process(
                self.c,
                self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone(),
            )
        finally:
            ghclient.list_review_comments, ghclient.pr_comment = old_comments, old_comment
        self.assertEqual(len(posted), 1)
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE id=?", (self.new_id,)).fetchone()["status"],
            "commented",
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

    def test_operator_decision_is_rechecked_on_every_new_head(self):
        db.set_finding_decision(
            self.c,
            self.c.execute("SELECT id FROM findings WHERE fp=?", (self.fp,)).fetchone()["id"],
            "dismissed", "old", "11", "의도적으로 유지",
        )
        self._run("unresolved", [], evidence="src/helper.ts changed the trust boundary")
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["status"], "confirmed")
        self.assertEqual(finding["card_id"], self.new_id)

    def test_closure_engine_failure_blocks_lgtm(self):
        for status, decision_head in (("posted", None), ("dismissed", "old")):
            with self.subTest(status=status):
                self.c.execute(
                    """UPDATE findings SET status=?,card_id=?,decision_head=?,
                       decision_comment_id=NULL,decision_evidence=NULL""",
                    (status, self.old_id, decision_head),
                )
                self.c.execute("UPDATE cards SET status='intake' WHERE id=?", (self.new_id,))
                self._run("unresolved", [], closure_error=True)
                finding = self.c.execute(
                    "SELECT * FROM findings WHERE fp=?", (self.fp,),
                ).fetchone()
                self.assertEqual(finding["status"], "confirmed")
                self.assertEqual(finding["card_id"], self.new_id)
                self.assertEqual(
                    self.c.execute("SELECT status FROM cards WHERE id=?", (self.new_id,)).fetchone()["status"],
                    "commenting",
                )


if __name__ == "__main__":
    unittest.main()
