import json
import pathlib
import sqlite3
import unittest

from src import commenter, config, db, ghclient, prompt_tpl, reviewer, verifier, worktree

prompts_dir = pathlib.Path(config.path("prompts"))


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

    def _run(self, closure_status, findings, evidence="", reply_evidence="", follow_up="",
             closure_error=False, changed=None):
        calls = []
        rendered = []
        old_view, old_diff, old_conversation = ghclient.pr_view, ghclient.pr_diff, ghclient.pr_conversation
        old_author = ghclient.pr_author_identity
        old_comments = ghclient.collect_author_replies
        old_login = ghclient.my_login
        old_changed_files = ghclient.pr_changed_files
        old_make, old_remove = worktree.make_worktree, worktree.remove_worktree
        old_changed = worktree.changed_files_between
        old_plan, old_context = reviewer.doc_planner.build_plan, reviewer.doc_planner.build_context
        old_render, old_run = prompt_tpl.render, reviewer.engines.run_json
        try:
            ghclient.pr_view = lambda *_: {"state": "OPEN", "headRefOid": "new"}
            ghclient.pr_diff = lambda *_: "diff"
            ghclient.pr_conversation = lambda *_: "author reply"
            ghclient.pr_author_identity = lambda *_: {"login": "author", "id": "42",
                                                      "body": "", "created_at": "0"}
            ghclient.my_login = lambda: "bot"
            ghclient.pr_changed_files = lambda *_: []
            ghclient.collect_author_replies = lambda *_: [
                {"id": "issue:11", "source": "issue", "author": "author",
                 "created_at": "2", "url": "", "body": reply_evidence or "일반 답변"},
            ]
            worktree.make_worktree = lambda *_: "/tmp/review"
            worktree.changed_files_between = lambda *_: changed
            worktree.remove_worktree = lambda *_: None
            reviewer.doc_planner.build_plan = lambda *_: {"summary_only": False, "review_mode": "full"}
            reviewer.doc_planner.build_context = lambda *_: ""
            def render(name, **tokens):
                rendered.append((name, tokens))
                return name

            prompt_tpl.render = render

            def run(prompt, **_):
                calls.append(prompt)
                if prompt.endswith("closure.md"):
                    if closure_error:
                        raise RuntimeError("closure unavailable")
                    return {"status": closure_status, "reason": "author reply judged",
                            "evidence": evidence, "reply_evidence": reply_evidence,
                            "reply_comment_id": "issue:11" if reply_evidence else "",
                            "follow_up": follow_up}
                return {"findings": findings}

            reviewer.engines.run_json = run
            reviewer.process(self.c, self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone())
        finally:
            ghclient.pr_view, ghclient.pr_diff, ghclient.pr_conversation = old_view, old_diff, old_conversation
            ghclient.pr_author_identity = old_author
            ghclient.collect_author_replies = old_comments
            ghclient.my_login = old_login
            ghclient.pr_changed_files = old_changed_files
            worktree.make_worktree, worktree.remove_worktree = old_make, old_remove
            worktree.changed_files_between = old_changed
            reviewer.doc_planner.build_plan, reviewer.doc_planner.build_context = old_plan, old_context
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
                self.assertEqual(calls, ["closure.md", "review.codex.md"])
                self.assertEqual(finding["status"],
                                 "dismiss_pending" if status == "dismissed" else "defer_pending")
                self.assertEqual(finding["card_id"], self.new_id)
                self.assertEqual(finding["decision_comment_id"], "issue:11")
                self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"], 1)
                self.assertEqual(self.c.execute("SELECT status FROM cards WHERE id=?", (self.new_id,)).fetchone()["status"], "commented")

    def test_pending_author_decision_survives_a_new_head(self):
        """새 커밋마다 운영자 수용 대기 결정이 unresolved 로 떨어지면 리마인드가 다시 나간다.

        작성자 회신은 보통 한 번뿐이라 새 head 에서는 '그 회신 이후의 새 답변'이
        없다. 반박할 코드 근거가 없으면 결정을 그대로 두어야 한다.
        """
        for status in ("dismiss_pending", "defer_pending"):
            with self.subTest(status=status):  # 보류는 DEFERRED 분기가 먼저 잡는다
                self.c.execute("UPDATE cards SET status='intake' WHERE id=?", (self.new_id,))
                self.c.execute(
                    """UPDATE findings SET status=?,card_id=?,decision_head='old',
                       decision_comment_id='issue:11',decision_evidence='의도적으로 유지'""",
                    (status, self.old_id),
                )
                self._run("unresolved", [])
                finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
                self.assertEqual(finding["status"], status)
                self.assertEqual(finding["decision_comment_id"], "issue:11")
                card = self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone()
                self.assertEqual(card["status"], "commented")
                self.assertNotIn("force_post", json.loads(card["payload"]))

    def test_new_code_evidence_still_reopens_a_dismissal(self):
        """반려는 다르다 — 작성자가 "의도적" 이라 했어도 새 코드가 그 주장을 뒤집으면 연다.

        보류(deferred)와 갈라지는 지점이다. 보류는 "고장 난 걸 안다" 가 전제라
        코드 근거가 새 정보가 아니지만, 반려는 "이 동작이 맞다" 는 주장이라
        현재 head 가 그 주장을 반박할 수 있다.
        """
        self.c.execute(
            """UPDATE findings SET status='dismiss_pending',card_id=?,decision_head='old',
               decision_comment_id='issue:11',decision_evidence='의도적으로 유지'""", (self.old_id,)
        )
        self._run("unresolved", [], evidence="src/example.ts:10 에서 여전히 저장 없이 goBack()")
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["status"], "confirmed")
        card = self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone()
        self.assertTrue(json.loads(card["payload"])["force_post"])

    def _judged(self, head="old", reply="2"):
        self.c.execute("UPDATE findings SET last_judged_head=?, last_seen_reply=?",
                       (head, reply))

    def _skips(self):
        return [e["detail"] for e in self.c.execute(
            "SELECT detail FROM events WHERE type='finding_closure_skipped'")]

    def test_closure_is_skipped_when_nothing_about_the_finding_changed(self):
        """#10066 에서 closure 35회 중 26회가 "아무것도 안 바뀜" 결론이었다.

        건너뛰어도 동작은 같다 — 재게시는 호출이 아니라 status 가 결정한다.
        """
        self._judged()
        calls, _ = self._run("unresolved", [], changed={"src/other.ts"})
        self.assertEqual(calls, ["review.codex.md"])   # closure.md 호출 없음
        self.assertEqual(len(self._skips()), 1)
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["status"], "posted")  # 상태가 그대로라 재게시도 안 된다
        card = self.c.execute("SELECT * FROM cards WHERE id=?", (self.new_id,)).fetchone()
        self.assertNotIn("force_post", json.loads(card["payload"]))

    def test_closure_runs_when_the_finding_file_changed(self):
        self._judged()
        calls, _ = self._run("unresolved", [], changed={"src/example.ts"})
        self.assertEqual(calls, ["closure.md", "review.codex.md"])
        self.assertEqual(self._skips(), [])

    def test_closure_runs_when_a_new_author_reply_arrived(self):
        self._judged(reply="1")   # 수집된 최신 회신은 "2"
        calls, _ = self._run("unresolved", [], changed={"src/other.ts"})
        self.assertEqual(calls, ["closure.md", "review.codex.md"])

    def test_closure_runs_when_the_change_set_is_unknown(self):
        """옛 sha 가 force-push·gc 로 사라질 수 있다 — 모르면 건너뛰지 않는다."""
        self._judged()
        calls, _ = self._run("unresolved", [], changed=None)
        self.assertEqual(calls, ["closure.md", "review.codex.md"])

    def test_first_judgement_is_never_skipped(self):
        calls, _ = self._run("unresolved", [], changed={"src/other.ts"})
        self.assertEqual(calls, ["closure.md", "review.codex.md"])

    def test_judging_records_the_baseline_for_next_time(self):
        self._run("unresolved", [])
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["last_judged_head"], "new")
        self.assertEqual(finding["last_seen_reply"], "2")

    def test_code_profile_picks_the_engine_specific_review_prompt(self):
        """Regression: the code profile once pointed at a single review.md that
        no longer exists, which crashed every code review."""
        finding = [{
            "file": "src/example.ts", "line": 10, "rule": "same-rule",
            "title": "same finding", "problem": "still relevant",
            "severity": "medium", "confidence": "high",
        }]
        for engine, expected in (("codex", "review.codex.md"), ("claude", "review.claude.md")):
            with self.subTest(engine=engine):
                self.c.execute("UPDATE cards SET engine=? WHERE id=?", (engine, self.new_id))
                self.c.execute("UPDATE cards SET status='intake' WHERE id=?", (self.new_id,))
                self.c.execute(
                    """UPDATE findings SET status='posted',card_id=?,decision_head=NULL,
                       decision_comment_id=NULL,decision_evidence=NULL""", (self.old_id,)
                )
                calls, _ = self._run("unresolved", finding)
                self.assertEqual(calls, ["closure.md", expected])
                self.assertTrue((prompts_dir / expected).is_file(), f"{expected} must exist")

    def test_legacy_snapshotted_policy_still_resolves_to_a_real_prompt(self):
        """A card whose payload predates the engine split must not crash."""
        legacy = {"profile_type": "code", "prompt_set": {"review": "review.md"}}
        for engine, expected in (("codex", "review.codex.md"), ("claude", "review.claude.md")):
            name = reviewer.profiles.prompt_name(
                reviewer.profiles._normalize(legacy), "review", engine)
            self.assertEqual(name, expected)
            self.assertTrue((prompts_dir / name).is_file())

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
        # This asserts the real post path, so neutralize the operator's
        # dry_run_comments setting instead of inheriting it from config.json.
        old_dry = commenter.CFG["dry_run_comments"]
        commenter.CFG["dry_run_comments"] = False
        self.addCleanup(commenter.CFG.__setitem__, "dry_run_comments", old_dry)
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

    def test_deferred_follow_up_must_be_copied_from_author_reply(self):
        reply = "별도 후속으로 처리합니다: LOOK-123"
        self._run("deferred", [], reply_evidence=reply, follow_up="LOOK-123")
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["decision_follow_up"], "LOOK-123")

        self.c.execute("UPDATE findings SET status='posted',card_id=?,decision_head=NULL WHERE fp=?",
                       (self.old_id, self.fp))
        self._run("deferred", [], reply_evidence=reply, follow_up="LOOK-999")
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertIsNone(finding["decision_follow_up"])

    def test_doc_closure_persists_verified_follow_up(self):
        policy = {"profile_type": "doc", "max_findings": 3, "min_confidence": "medium"}
        for card_id in (self.old_id, self.new_id):
            self.c.execute("UPDATE cards SET payload=? WHERE id=?",
                           (json.dumps({"review_policy": policy}), card_id))
        reply = "문서 보완은 DOC-123으로 별도 후속합니다"
        _, rendered = self._run("deferred", [], reply_evidence=reply, follow_up="DOC-123")
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(rendered[0][0], "doc_closure.md")
        self.assertEqual(finding["decision_follow_up"], "DOC-123")

        self.c.execute("UPDATE findings SET status='posted',card_id=?,decision_head=NULL WHERE fp=?",
                       (self.old_id, self.fp))
        self._run("deferred", [], reply_evidence=reply, follow_up="LOOK")
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertIsNone(finding["decision_follow_up"])

    def test_deferred_is_not_reopened_by_code_evidence(self):
        """보류는 "고장 난 걸 알고 미룬다" — "아직 고장 나 있다" 는 재개 근거가 아니다.

        #10066 재현에서 판정기가 작성자가 보류한 내용을 그대로 인용해(코드에 적힌
        "PO 결정 대기" 주석까지) 재개를 시도했다. 게시 직전 재확인이 우연히
        되돌려 줬을 뿐, 두 번 다 그랬으면 리마인드가 다시 나갔다.
        """
        for status in ("deferred", "defer_pending"):
            with self.subTest(status=status):
                self.c.execute("UPDATE cards SET status='intake' WHERE id=?", (self.new_id,))
                self.c.execute(
                    """UPDATE findings SET status=?,card_id=?,decision_head='old',
                       decision_comment_id='issue:11',decision_follow_up='LOOK-123'""",
                    (status, self.old_id),
                )
                self._run("unresolved", [],
                          evidence="src/example.ts:10 여전히 저장 없이 goBack()")
                finding = self.c.execute("SELECT * FROM findings WHERE fp=?",
                                         (self.fp,)).fetchone()
                self.assertEqual(finding["status"], status)
                self.assertEqual(finding["decision_follow_up"], "LOOK-123")
                card = self.c.execute("SELECT * FROM cards WHERE id=?",
                                      (self.new_id,)).fetchone()
                self.assertNotIn("force_post", json.loads(card["payload"]))

    def test_deferred_still_closes_when_the_code_is_actually_fixed(self):
        """재개만 막는 것이지 영원히 붙잡아 두는 게 아니다."""
        self.c.execute(
            """UPDATE findings SET status='deferred',card_id=?,decision_head='old',
               decision_follow_up='LOOK-123'""", (self.old_id,))
        self._run("resolved", [])
        finding = self.c.execute("SELECT * FROM findings WHERE fp=?", (self.fp,)).fetchone()
        self.assertEqual(finding["status"], "resolved")

    def test_operator_decision_is_rechecked_on_every_new_head(self):
        db.set_finding_decision(
            self.c,
            self.c.execute("SELECT id FROM findings WHERE fp=?", (self.fp,)).fetchone()["id"],
            "dismissed", "old", "issue:11", "의도적으로 유지",
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
