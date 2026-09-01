import sqlite3
import unittest

from src import db, ghclient, prdiff, worktree


def mkfile(path, adds=0, dels=0):
    body = "".join(f"+line{i}\n" for i in range(adds)) + "".join(f"-old{i}\n" for i in range(dels))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


class SplitByFileTest(unittest.TestCase):
    def test_counts_hunk_lines_and_ignores_the_header(self):
        # `--- a/x` / `+++ b/x` are header lines, not a deletion and an addition
        files = prdiff.split_by_file(mkfile("x.py", adds=3, dels=2))
        self.assertEqual([(p, a, d) for p, _, a, d in files], [("x.py", 3, 2)])

    def test_splits_on_diff_git_boundaries(self):
        files = prdiff.split_by_file(mkfile("a.py", 1) + mkfile("b/c.py", 2))
        self.assertEqual([p for p, _, _, _ in files], ["a.py", "b/c.py"])

    def test_empty_diff_yields_no_files(self):
        self.assertEqual(prdiff.split_by_file(""), [])


class PackTest(unittest.TestCase):
    def test_whole_diff_is_kept_when_it_fits(self):
        diff = mkfile("a.py", 2) + mkfile("b.py", 2)
        packed, manifest, omitted = prdiff.pack(diff, budget=10**6)
        self.assertEqual(packed, diff)
        self.assertEqual(omitted, 0)
        self.assertIn("전체 diff가 위에 포함됨", manifest)

    def test_files_are_never_cut_mid_chunk(self):
        diff = mkfile("a.py", 5) + mkfile("b.py", 500)
        packed, _, omitted = prdiff.pack(diff, budget=len(mkfile("a.py", 5)) + 10)
        self.assertEqual(omitted, 1)
        # every kept file must be a complete chunk from the original
        for path, chunk, _, _ in prdiff.split_by_file(packed):
            self.assertIn(chunk, diff, f"{path} was cut mid-file")

    def test_addition_files_win_the_budget_over_deletion_only_files(self):
        """A mass-deletion refactor must not starve the files with new behavior."""
        diff = mkfile("deleted_only.py", 0, 400) + mkfile("new_behavior.py", 5, 0)
        packed, manifest, omitted = prdiff.pack(diff, budget=400)
        kept = [p for p, _, _, _ in prdiff.split_by_file(packed)]
        self.assertEqual(kept, ["new_behavior.py"])
        self.assertEqual(omitted, 1)
        self.assertIn("[미포함] deleted_only.py", manifest)

    def test_manifest_lists_every_file_so_omissions_are_explicit(self):
        diff = mkfile("kept.py", 2) + mkfile("dropped.py", 0, 500)
        _, manifest, _ = prdiff.pack(diff, budget=len(mkfile("kept.py", 2)) + 10)
        self.assertIn("[포함]   kept.py (+2/-0)", manifest)
        self.assertIn("[미포함] dropped.py (+0/-500)", manifest)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.card_id = db.upsert_card(self.c, "review:1", "review", "owner/repo", 1,
                                      "intake", "head", base_sha="main")
        self.card = self.c.execute("SELECT * FROM cards WHERE id=?", (self.card_id,)).fetchone()
        self.old_diff, self.old_local = ghclient.pr_diff, worktree.local_diff

    def tearDown(self):
        ghclient.pr_diff, worktree.local_diff = self.old_diff, self.old_local
        self.c.close()

    def _events(self, kind):
        return self.c.execute("SELECT COUNT(*) n FROM events WHERE type=?", (kind,)).fetchone()["n"]

    def test_over_sized_diff_falls_back_to_the_local_clone(self):
        def refuse(*_):
            raise ghclient.DiffTooLarge("406 diff exceeded maximum lines")

        ghclient.pr_diff = refuse
        worktree.local_diff = lambda *_: mkfile("a.py", 3)
        self.assertIn("a.py", prdiff.fetch(self.c, self.card))
        self.assertEqual(self._events("diff_local_fallback"), 1)

    def test_other_gh_errors_are_not_swallowed(self):
        def boom(*_):
            raise ghclient.GhError("network down")

        ghclient.pr_diff = boom
        with self.assertRaises(ghclient.GhError):
            prdiff.fetch(self.c, self.card)

    def test_truncation_is_recorded_as_an_event(self):
        ghclient.pr_diff = lambda *_: mkfile("a.py", 5) + mkfile("b.py", 900)
        prdiff.collect(self.c, self.card, budget=len(mkfile("a.py", 5)) + 10)
        self.assertEqual(self._events("diff_truncated"), 1)

    def test_no_event_when_everything_fits(self):
        ghclient.pr_diff = lambda *_: mkfile("a.py", 2)
        prdiff.collect(self.c, self.card)
        self.assertEqual(self._events("diff_truncated"), 0)


if __name__ == "__main__":
    unittest.main()
