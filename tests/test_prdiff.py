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


class PackPreferTest(unittest.TestCase):
    def _diff(self, sizes):
        out = []
        for path, n in sizes.items():
            out.append(f"diff --git a/{path} b/{path}\n@@ -1 +1 @@\n")
            out.append("+x" * n + "\n")
        return "".join(out)

    def test_preferred_file_is_included_even_when_it_blows_the_budget(self):
        """지적 하나를 판정하는 호출에서 정작 그 파일이 예산에 밀려 빠지면 무의미하다."""
        diff = self._diff({"big.ts": 4000, "other.ts": 100, "another.ts": 100})
        text, manifest, _ = prdiff.pack(diff, budget=500, prefer=["big.ts"])
        self.assertIn("a/big.ts", text)
        self.assertIn("[포함]   big.ts", manifest)

    def test_remaining_budget_still_goes_to_other_files(self):
        diff = self._diff({"target.ts": 50, "other.ts": 50, "huge.ts": 9000})
        text, _manifest, omitted = prdiff.pack(diff, budget=400, prefer=["target.ts"])
        self.assertIn("a/target.ts", text)
        self.assertIn("a/other.ts", text)
        self.assertNotIn("a/huge.ts", text)
        self.assertEqual(omitted, 1)

    def test_prefer_is_optional_and_changes_nothing_by_default(self):
        diff = self._diff({"a.ts": 50, "b.ts": 9000})
        self.assertEqual(prdiff.pack(diff, budget=400),
                         prdiff.pack(diff, budget=400, prefer=[]))


class FpLineMigrationTest(unittest.TestCase):
    """지문에서 줄 번호를 뺄 때 기존 행을 옮겨 놓지 않으면, 열려 있던 지적 전부가
    '처음 보는 지적' 이 되어 한 번씩 중복 게시된다."""

    def _conn(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        return c

    def test_old_shape_is_rewritten(self):
        self.assertEqual(db._fp_without_line("o/r#1:src/a.ts:89-94:rule-x"),
                         "o/r#1:src/a.ts:rule-x")
        self.assertEqual(db._fp_without_line("o/r#1:pkg/src/a.ts:10:rule-x"),
                         "o/r#1:pkg/src/a.ts:rule-x")

    def test_new_shape_is_left_alone(self):
        self.assertIsNone(db._fp_without_line("o/r#1:src/a.ts:rule-x"))
        self.assertIsNone(db._fp_without_line("topic:123-4"))

    def test_rows_split_by_line_are_merged_keeping_the_newest(self):
        c = self._conn()
        card = db.upsert_card(c, "k", "review", "o/r", 1, "intake", "head")
        for line, status in (("89-94", "resolved"), ("87-94", "posted")):
            db.upsert_finding(c, card, "o/r", 1, "head", f"o/r#1:src/a.ts:{line}:same",
                              "제목", "{}", "src/a.ts", line, "medium", "high", status)
        c.execute("UPDATE findings SET updated_at=? WHERE line=?", (1.0, "89-94"))
        c.execute("UPDATE findings SET updated_at=? WHERE line=?", (2.0, "87-94"))

        db._drop_line_from_fps(c)

        rows = c.execute("SELECT fp, line, status FROM findings").fetchall()
        self.assertEqual(len(rows), 1, "같은 문제는 한 행으로 합쳐진다")
        self.assertEqual(rows[0]["fp"], "o/r#1:src/a.ts:same")
        self.assertEqual(rows[0]["line"], "87-94")     # 최근 갱신된 쪽
        self.assertEqual(rows[0]["status"], "posted")

    def test_migration_is_idempotent(self):
        c = self._conn()
        card = db.upsert_card(c, "k", "review", "o/r", 1, "intake", "head")
        db.upsert_finding(c, card, "o/r", 1, "head", "o/r#1:src/a.ts:10:r",
                          "제목", "{}", "src/a.ts", "10", "medium", "high", "posted")
        db._drop_line_from_fps(c)
        db._drop_line_from_fps(c)
        rows = c.execute("SELECT fp FROM findings").fetchall()
        self.assertEqual([r["fp"] for r in rows], ["o/r#1:src/a.ts:r"])


class PurgeKeepsOpenFindingsTest(unittest.TestCase):
    """재게시 쿨다운이 지적을 옛 카드에 남겨 두므로, archived 정리가 열려 있는 PR 의
    미해결 지적까지 지우기 시작했다(셀프 리뷰 3회차). 게시 여부와 보존 수명은 별개다."""

    def _conn(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        return c

    def test_open_finding_survives_its_archived_card(self):
        c = self._conn()
        old = db.upsert_card(c, "old", "review", "o/r", 1, "archived", "h1")
        db.upsert_finding(c, old, "o/r", 1, "h1", "o/r#1:src/a.ts:open-one", "열림",
                          "{}", "src/a.ts", "1", "medium", "high", "posted")
        db.upsert_finding(c, old, "o/r", 1, "h1", "o/r#1:src/b.ts:done-one", "닫힘",
                          "{}", "src/b.ts", "2", "medium", "high", "resolved")
        c.execute("UPDATE cards SET updated_at=0 WHERE id=?", (old,))

        db.purge_old(c, days=1)

        rows = {r["fp"].rsplit(":", 1)[-1]: r["status"]
                for r in c.execute("SELECT fp, status FROM findings")}
        self.assertEqual(rows, {"open-one": "posted"})
        self.assertEqual(db.open_findings_count(c, "o/r", 1), 1)
        # 지적이 남은 카드는 함께 지우지 않는다 — 고아 finding 을 만들지 않기 위해
        self.assertIsNotNone(c.execute("SELECT 1 FROM cards WHERE id=?", (old,)).fetchone())

    def test_a_fully_closed_card_is_still_purged(self):
        c = self._conn()
        old = db.upsert_card(c, "old", "review", "o/r", 1, "archived", "h1")
        db.upsert_finding(c, old, "o/r", 1, "h1", "o/r#1:src/a.ts:done", "닫힘",
                          "{}", "src/a.ts", "1", "medium", "high", "resolved")
        c.execute("UPDATE cards SET updated_at=0 WHERE id=?", (old,))

        out = db.purge_old(c, days=1)

        self.assertEqual(out["findings"], 1)
        self.assertEqual(out["cards"], 1)
