import json
import sqlite3
import unittest

from src import db, engines, ghclient, impl_verifier, keys, pr_opener, prdiff, prompt_tpl, worktree

REPO = "acme/product-hub"
TARGET = "acme/ceo-client"


class _Base(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.key = keys.issue_key(REPO, 1765)
        self.card_id = db.upsert_card(
            self.c, self.key, "issue", REPO, 1765, status="impl_verify",
            payload={"display": "PH-1765", "title": "401 처리", "url": "u",
                     "target_repo": TARGET, "branch": "feature/PH-1765",
                     "impl": {"summary": "고쳤다", "verification": "yarn test 통과"}})
        self.c.execute("UPDATE cards SET engine='claude' WHERE id=?", (self.card_id,))
        self.saved = {
            "parent": worktree.impl_parent, "base": worktree._impl_base_ref,
            "git": worktree._git, "mk": worktree.make_impl_worktree,
            "pack": prdiff.pack, "render": prompt_tpl.render,
            "runjson": engines.run_json, "ready": engines.is_ready,
            "prcreate": getattr(ghclient, "pr_create_draft", None),
            "icomment": getattr(ghclient, "issue_comment", None),
        }
        worktree.impl_parent = lambda _r: "/parent"
        worktree._impl_base_ref = lambda *_a: "origin/master"
        worktree.make_impl_worktree = lambda *_a, **_k: "/wt"
        prdiff.pack = lambda raw, n: (raw, "a.txt", None)
        prompt_tpl.render = lambda *_a, **_k: "PROMPT"
        engines.is_ready = lambda _e: True
        self.git_calls = []

        class P:
            stdout = "diff --git a/a.txt b/a.txt\n+x\n"
        worktree._git = lambda *a, **k: (self.git_calls.append(a[1:]), P())[1]

    def tearDown(self):
        worktree.impl_parent = self.saved["parent"]
        worktree._impl_base_ref = self.saved["base"]
        worktree._git = self.saved["git"]
        worktree.make_impl_worktree = self.saved["mk"]
        prdiff.pack = self.saved["pack"]
        prompt_tpl.render = self.saved["render"]
        engines.run_json = self.saved["runjson"]
        engines.is_ready = self.saved["ready"]
        if self.saved["prcreate"]:
            ghclient.pr_create_draft = self.saved["prcreate"]
        if self.saved["icomment"]:
            ghclient.issue_comment = self.saved["icomment"]
        self.c.close()

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def _events(self):
        return [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]


class VerifierEngineTest(_Base):
    def test_opposite_engine_verifies(self):
        self.assertEqual(impl_verifier.verifier_engine("claude"), ("codex", False))
        self.assertEqual(impl_verifier.verifier_engine("codex"), ("claude", False))

    def test_falls_back_to_same_engine_but_marks_it(self):
        engines.is_ready = lambda e: e == "claude"
        engine, fallback = impl_verifier.verifier_engine("claude")
        self.assertEqual((engine, fallback), ("claude", True))

    def test_raises_when_no_engine_is_ready(self):
        engines.is_ready = lambda _e: False
        with self.assertRaises(RuntimeError):
            impl_verifier.verifier_engine("claude")


class VerifyOutcomeTest(_Base):
    def test_approved_stops_at_human_gate(self):
        engines.run_json = lambda *_a, **_k: {"approved": True, "summary": "깨끗하다",
                                              "meets_requirement": True}
        impl_verifier.process(self.c, self._card())
        card = self._card()
        self.assertEqual(card["status"], "pr_blocked")
        self.assertEqual(card["blocked"], 1)      # 사람이 눌러야 PR이 올라간다
        self.assertTrue(self._payload()["verify"]["approved"])
        self.assertIn("impl_verified", self._events())

    def test_blocking_findings_send_it_back_with_feedback(self):
        engines.run_json = lambda *_a, **_k: {
            "approved": False, "summary": "널 가드 없음",
            "blocking": [{"file": "a.ts", "line": "12", "problem": "user가 없으면 크래시",
                          "fix": "옵셔널 체이닝"}]}
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "implementing")
        payload = self._payload()
        self.assertEqual(payload["impl_rounds"], 2)
        self.assertIn("a.ts:12", payload["feedback"])
        self.assertIn("옵셔널 체이닝", payload["feedback"])
        self.assertIn("impl_rework", self._events())

    def test_approved_true_with_blockers_is_not_approved(self):
        engines.run_json = lambda *_a, **_k: {
            "approved": True, "blocking": [{"file": "a.ts", "problem": "깨진다"}]}
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "implementing")

    def test_operator_bonus_raises_the_round_cap(self):
        """사람이 수정을 요청하면 예산이 늘어 다시 한 바퀴 돈다."""
        db.merge_payload(self.c, self.card_id,
                         {"impl_rounds": impl_verifier.MAX_IMPL_ROUNDS, "impl_bonus": 1})
        engines.run_json = lambda *_a, **_k: {"approved": False,
                                             "blocking": [{"file": "a", "problem": "b"}]}
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "implementing")   # failed 가 아니다
        self.assertIn("impl_rework", self._events())

    def test_round_cap_hands_it_to_the_human_not_to_failed(self):
        """엔진 불일치는 실패가 아니다 — 커밋도 검증 의견도 있다. failed 로 보내면
        크래시처럼 보이고 사람이 그 diff 를 판단할 기회를 잃는다."""
        db.merge_payload(self.c, self.card_id, {"impl_rounds": impl_verifier.MAX_IMPL_ROUNDS})
        engines.run_json = lambda *_a, **_k: {"approved": False,
                                             "blocking": [{"file": "a", "problem": "b"}]}
        impl_verifier.process(self.c, self._card())
        card = self._card()
        self.assertEqual(card["status"], "pr_blocked")
        self.assertEqual(card["blocked"], 1)
        self.assertTrue(self._payload()["verify_exhausted"])   # 미통과 표식
        self.assertIn("impl_rounds_exhausted", self._events())

    def test_passing_verification_clears_the_exhausted_mark(self):
        db.merge_payload(self.c, self.card_id, {"verify_exhausted": True})
        engines.run_json = lambda *_a, **_k: {"approved": True, "summary": "ok"}
        impl_verifier.process(self.c, self._card())
        self.assertFalse(self._payload()["verify_exhausted"])

    def test_missing_branch_fails_loudly(self):
        db.merge_payload(self.c, self.card_id, {"branch": ""})
        engines.run_json = lambda *_a, **_k: self.fail("엔진을 불러선 안 된다")
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "failed")
        self.assertIn("impl_verify_no_branch", self._events())

    def test_empty_diff_fails_instead_of_approving_nothing(self):
        class Empty:
            stdout = ""
        worktree._git = lambda *_a, **_k: Empty()
        engines.run_json = lambda *_a, **_k: self.fail("엔진을 불러선 안 된다")
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "failed")
        self.assertIn("impl_verify_empty_diff", self._events())

    def test_diff_is_computed_in_parent_not_worktree(self):
        # 워크트리는 repo당 하나를 공유하므로 다른 카드가 브랜치를 바꿔놨을 수 있다
        engines.run_json = lambda *_a, **_k: {"approved": True}
        impl_verifier.process(self.c, self._card())
        self.assertIn(("diff", "origin/master...feature/PH-1765"), self.git_calls)


class PrOpenerTest(_Base):
    def setUp(self):
        super().setUp()
        db.set_status(self.c, self.card_id, "pr_opening")
        self.created = []
        self.comments = []
        ghclient.pr_create_draft = lambda *a: (self.created.append(a), "https://pr/1")[1]
        ghclient.issue_comment = lambda *a: self.comments.append(a)
        self.saved_dry = pr_opener.CFG.get("dry_run_pr")

    def tearDown(self):
        if self.saved_dry is None:
            pr_opener.CFG.pop("dry_run_pr", None)
        else:
            pr_opener.CFG["dry_run_pr"] = self.saved_dry
        super().tearDown()

    def test_dry_run_is_the_default_and_pushes_nothing(self):
        pr_opener.CFG.pop("dry_run_pr", None)
        pr_opener.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "done")
        self.assertEqual(self.created, [])
        self.assertNotIn(("push", "--quiet", "-u", "origin", "feature/PH-1765"), self.git_calls)
        self.assertIn("pr_dryrun", self._events())
        self.assertIn("title", self._payload()["pr_dryrun"])

    def test_live_run_pushes_and_creates_draft_pr(self):
        pr_opener.CFG["dry_run_pr"] = False
        pr_opener.process(self.c, self._card())
        self.assertIn(("push", "--quiet", "-u", "origin", "feature/PH-1765"), self.git_calls)
        repo, base, head, title, body = self.created[0]
        self.assertEqual((repo, base, head), (TARGET, "master", "feature/PH-1765"))
        self.assertIn("PH-1765", title)
        self.assertIn("고쳤다", body)
        self.assertEqual(self._payload()["pr_url"], "https://pr/1")
        self.assertEqual(self._card()["status"], "done")
        # 이슈에 링크를 남긴다
        self.assertEqual(self.comments[0][0], REPO)
        self.assertIn("https://pr/1", self.comments[0][2])

    def test_pr_is_created_as_draft_not_promoted_later(self):
        """생성 후 draft로 내려도 이미 걸린 코드 소유자 리뷰 요청은 회수되지 않는다.
        그래서 --draft 는 생성 인자에 있어야 한다."""
        with open("src/ghclient.py", encoding="utf-8") as f:
            gh = f.read()
        self.assertIn('"pr", "create", "--repo", repo, "--draft"', gh)


if __name__ == "__main__":
    unittest.main()


class ExhaustedFeedbackTest(_Base):
    """소진으로 사람에게 올릴 때도 최신 블로커를 feedback 에 남겨야, 사람이
    '수정 요청'을 눌렀을 때 구현자가 그 지적을 받는다."""

    def test_exhaustion_records_the_blockers_for_the_next_round(self):
        db.merge_payload(self.c, self.card_id,
                         {"impl_rounds": impl_verifier.MAX_IMPL_ROUNDS,
                          "feedback": "낡은 지적"})
        engines.run_json = lambda *_a, **_k: {
            "approved": False,
            "blocking": [{"file": "x.ts", "line": "10", "problem": "터진다", "fix": "가드"}]}
        impl_verifier.process(self.c, self._card())
        fb = self._payload()["feedback"]
        self.assertIn("x.ts:10", fb)
        self.assertIn("[검증 미해결]", fb)
        self.assertNotIn("낡은 지적", fb)

    def test_rework_path_uses_the_same_wording(self):
        engines.run_json = lambda *_a, **_k: {
            "approved": False,
            "blocking": [{"file": "y.ts", "line": "2", "problem": "p", "fix": "f"}]}
        impl_verifier.process(self.c, self._card())
        self.assertIn("y.ts:2 — p / 고치는 방향: f", self._payload()["feedback"])
