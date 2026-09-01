import sqlite3
import unittest

from src import db, doc_planner, engines, ghclient, prdiff, prompt_tpl, reviewer, worktree


class ReviewerDiffBudgetTest(unittest.TestCase):
    """The reviewer must plan on the raw diff but prompt with the packed one."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.raw = "RAW" * 40000          # 큰 원본
        self.packed = "PACKED"            # 예산 안에 담긴 결과
        self.saved = {
            "pr_view": ghclient.pr_view, "conv": ghclient.pr_conversation,
            "changed": ghclient.pr_changed_files, "fetch": prdiff.fetch,
            "pack": prdiff.pack_logged, "plan": doc_planner.build_plan,
            "ctx": doc_planner.build_context, "render": prompt_tpl.render,
            "run": engines.run_json, "mk": worktree.make_worktree,
            "rm": worktree.remove_worktree,
        }
        ghclient.pr_view = lambda *_: {"state": "OPEN", "headRefOid": "head"}
        ghclient.pr_conversation = lambda *_: ""
        ghclient.pr_changed_files = lambda *_: ["epics/x/docs/prd/a.md"]
        prdiff.fetch = lambda *_: self.raw
        prdiff.pack_logged = lambda *_a, **_k: (self.packed, "MANIFEST")
        worktree.make_worktree = lambda *_: "/tmp/wt"
        worktree.remove_worktree = lambda *_: None
        doc_planner.build_context = lambda *_: "CTX"
        engines.run_json = lambda *_a, **_k: {"findings": [], "summary": ""}

    def tearDown(self):
        ghclient.pr_view, ghclient.pr_conversation = self.saved["pr_view"], self.saved["conv"]
        ghclient.pr_changed_files, prdiff.fetch = self.saved["changed"], self.saved["fetch"]
        prdiff.pack_logged, doc_planner.build_plan = self.saved["pack"], self.saved["plan"]
        doc_planner.build_context, prompt_tpl.render = self.saved["ctx"], self.saved["render"]
        engines.run_json = self.saved["run"]
        worktree.make_worktree, worktree.remove_worktree = self.saved["mk"], self.saved["rm"]
        self.c.close()

    def _card(self, profile_type):
        cid = db.upsert_card(
            self.c, f"review:{profile_type}", "review", "owner/repo", 1, "intake", "head",
            payload={"review_policy": {"profile_type": profile_type, "max_findings": 8,
                                       "min_confidence": "medium"}},
        )
        self.c.execute("UPDATE cards SET engine='codex' WHERE id=?", (cid,))
        return self.c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()

    def test_doc_plan_sees_the_raw_diff_so_large_pr_detection_survives(self):
        seen = {}
        doc_planner.build_plan = lambda repo, pr, wt, diff, files, policy: (
            seen.update(diff=diff) or {"summary_only": False, "review_mode": "prd_quality"})
        prompt_tpl.render = lambda *_a, **_k: "prompt"
        reviewer.process(self.c, self._card("doc"))
        self.assertEqual(seen["diff"], self.raw,
                         "planner got the packed diff — large-PR thresholds would under-count")

    def test_prompts_get_the_packed_diff_and_the_manifest(self):
        tokens = {}
        prompt_tpl.render = lambda name, **kw: tokens.setdefault(name, kw) and "p" or "p"
        reviewer.process(self.c, self._card("code"))
        kw = tokens["review.codex.md"]
        self.assertEqual(kw["DIFF"], self.packed)
        self.assertEqual(kw["FILES"], "MANIFEST")
        self.assertNotIn(self.raw, str(kw["DIFF"]))


if __name__ == "__main__":
    unittest.main()
