"""검증자는 '그 지적의 파일' 을 반드시 본다.

실사용에서 걸렸다 — #10066 은 diff 537KB / 81파일이고 검증 예산은 40,000 이라
10개만 들어갔는데, 그날 검증한 지적 3건의 파일이 **전부** 빠져 있었다. 매니페스트가
`[미포함]` 이라고 알려주고 워크트리를 열어 두긴 하지만, 판정에 필요한 코드가 빠진
프롬프트를 보내는 것은 그 자체로 결함이다. closure 는 이미 `prefer` 로 같은 문제를
막고 있었고(reviewer._run_closure), 검증자만 빠져 있었다.
"""
import json
import sqlite3
import unittest

from src import db, engines, ghclient, ledger, prdiff, profiles, prompt_tpl, verifier, worktree


def _diff(*names):
    """이름당 한 파일 — 앞의 것이 예산을 다 먹도록 크게."""
    return "".join(
        f"diff --git a/{n} b/{n}\n@@ -1 +1 @@\n" + f"+{n}\n" * 400 for n in names)


class VerifierPrefersTheFindingsFileTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.card = db.upsert_card(self.c, "k", "review", "o/r", 1, "verifying", "head",
                                   payload={"title": "t", "author": "a"})
        db.upsert_finding(self.c, self.card, "o/r", 1, "head", "o/r#1:late.ts:rule",
                          "제목", json.dumps({"problem": "p"}), "late.ts", "9",
                          "medium", "high", "pending_verify")
        # 예산을 앞 파일들이 다 먹는다 — late.ts 는 순서상 맨 뒤다.
        self.raw = _diff("a.ts", "b.ts", "c.ts", "late.ts")
        self.prompts = []
        self.saved = {
            "fetch": prdiff.fetch, "author": ghclient.pr_author_identity,
            "replies": ghclient.collect_author_replies, "ledger": ledger.build,
            "render": prompt_tpl.render, "run": engines.run_json,
            "mk": worktree.make_worktree, "rm": worktree.remove_worktree,
            "policy": profiles.policy_from_card,
        }
        prdiff.fetch = lambda *_a, **_k: self.raw
        ghclient.pr_author_identity = lambda *_a: {"login": "a"}
        ghclient.collect_author_replies = lambda *_a: []
        ledger.build = lambda *_a: ("원장", "메모")
        worktree.make_worktree = lambda *_a: "/tmp/wt"
        worktree.remove_worktree = lambda *_a: None
        profiles.policy_from_card = lambda *_a: {"profile_type": "code",
                                                 "no_confirmed_terminal": "commenting"}
        engines.run_json = lambda *_a, **_k: {"confirmed": True, "reason": "ok"}

        def render(_name, **kw):
            self.prompts.append(kw)
            return "PROMPT"
        prompt_tpl.render = render
        self.addCleanup(self._restore)

    def _restore(self):
        prdiff.fetch = self.saved["fetch"]
        ghclient.pr_author_identity = self.saved["author"]
        ghclient.collect_author_replies = self.saved["replies"]
        ledger.build = self.saved["ledger"]
        prompt_tpl.render = self.saved["render"]
        engines.run_json = self.saved["run"]
        worktree.make_worktree = self.saved["mk"]
        worktree.remove_worktree = self.saved["rm"]
        profiles.policy_from_card = self.saved["policy"]

    def test_the_findings_file_is_in_the_diff_even_past_the_budget(self):
        # 예산이 파일 하나치도 안 되게 작아도 late.ts 는 들어가야 한다
        verifier.VERIFY_DIFF_CHARS = 100
        try:
            verifier.process(self.c, db.get_card(self.c, "k"))
        finally:
            verifier.VERIFY_DIFF_CHARS = 40000
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("late.ts", self.prompts[0]["DIFF"])
        kept = [l for l in self.prompts[0]["FILES"].splitlines()
                if "late.ts" in l]
        self.assertTrue(kept and kept[0].startswith("[포함]"), kept)

    def test_omission_is_recorded_with_the_fingerprint(self):
        verifier.VERIFY_DIFF_CHARS = 100
        try:
            verifier.process(self.c, db.get_card(self.c, "k"))
        finally:
            verifier.VERIFY_DIFF_CHARS = 40000
        row = self.c.execute(
            "SELECT detail FROM events WHERE type='diff_truncated'").fetchone()
        self.assertIsNotNone(row, "빠진 파일이 있으면 어느 지적에서였는지 남아야 한다")
        self.assertEqual(json.loads(row["detail"])["fp"], "o/r#1:late.ts:rule")


if __name__ == "__main__":
    unittest.main()
