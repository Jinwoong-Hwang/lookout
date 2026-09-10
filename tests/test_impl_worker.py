import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from src import db, engines, ghclient, impl_worker, keys, prompt_tpl, worktree

REPO = "acme/product-hub"
TARGET = "acme/ceo-client"


def _git(cwd, *args):
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class TargetRepoTest(unittest.TestCase):
    """대상 저장소는 라벨로 추정할 수 없다 — 제목 태그이거나, 사람이 고른 값이다."""

    def setUp(self):
        self.saved = impl_worker.CFG.get("impl_target_map")
        impl_worker.CFG["impl_target_map"] = {"CEO_APP": TARGET, "TALK_CEO": "acme/talk"}

    def tearDown(self):
        if self.saved is None:
            impl_worker.CFG.pop("impl_target_map", None)
        else:
            impl_worker.CFG["impl_target_map"] = self.saved

    def test_operator_choice_wins_over_title_tag(self):
        meta = {"title": "[FE][CEO_APP] 무언가", "target_repo": "acme/chosen"}
        self.assertEqual(impl_worker.target_repo(meta), "acme/chosen")

    def test_title_tag_maps_to_repo(self):
        self.assertEqual(impl_worker.target_repo({"title": "[FE][CEO_APP] 무언가"}), TARGET)

    def test_tag_separator_wobble_is_normalized(self):
        # 실측: 같은 대상이 [TALK-CEO]와 [TALK_CEO] 두 표기로 쓰인다
        self.assertEqual(impl_worker.target_repo({"title": "[FE][TALK-CEO] a"}), "acme/talk")
        self.assertEqual(impl_worker.target_repo({"title": "[FE][TALK_CEO] b"}), "acme/talk")

    def test_unmappable_title_raises_instead_of_guessing(self):
        with self.assertRaises(impl_worker.TargetUnknown):
            impl_worker.target_repo({"title": "[FE] www build file type 제거"})


class ImplWorkerTest(unittest.TestCase):
    """엔진은 편집만 하고 커밋은 워커가 한다. 변경이 0이면 조용히 넘어가지 않는다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lookout_worker_")
        self.wt = os.path.join(self.tmp, "wt")
        os.makedirs(self.wt)
        _git(self.wt, "init", "--quiet", "-b", "main")
        _git(self.wt, "config", "user.email", "t@t")
        _git(self.wt, "config", "user.name", "t")
        with open(os.path.join(self.wt, "a.txt"), "w") as f:
            f.write("before\n")
        _git(self.wt, "add", "-A")
        _git(self.wt, "commit", "--quiet", "-m", "init")

        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.key = keys.issue_key(REPO, 1765)
        self.card_id = db.upsert_card(
            self.c, self.key, "issue", REPO, 1765, status="implementing",
            payload={"display": "PH-1765", "title": "[FE][CEO_APP] 401 처리",
                     "url": f"https://github.com/{REPO}/issues/1765",
                     "instruction": "ceo-client 만 건드려라",
                     "target_repo": TARGET, "mode": "implement"})
        self.c.execute("UPDATE cards SET engine='claude' WHERE id=?", (self.card_id,))

        self.saved = {"view": ghclient.issue_view, "mk": worktree.make_impl_worktree,
                      "run": engines.run_impl, "render": prompt_tpl.render,
                      "map": impl_worker.CFG.get("impl_target_map")}
        ghclient.issue_view = lambda *_a, **_k: {"title": "[FE][CEO_APP] 401 처리",
                                                "body": "본문", "url": "u"}
        worktree.make_impl_worktree = lambda *_a, **_k: self.wt
        prompt_tpl.render = lambda *_a, **_k: "PROMPT"
        self.calls = []

    def tearDown(self):
        ghclient.issue_view = self.saved["view"]
        worktree.make_impl_worktree = self.saved["mk"]
        engines.run_impl = self.saved["run"]
        prompt_tpl.render = self.saved["render"]
        if self.saved["map"] is None:
            impl_worker.CFG.pop("impl_target_map", None)
        else:
            impl_worker.CFG["impl_target_map"] = self.saved["map"]
        self.c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, edit=None, reply='{"done": true, "summary": "고쳤다", "files": ["a.txt"]}'):
        def fake(prompt, engine="claude", **kw):
            self.calls.append((engine, kw.get("cwd")))
            if edit:
                edit()
            return reply
        engines.run_impl = fake
        card = db.get_card(self.c, self.key)
        impl_worker.process(self.c, card)

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def _events(self):
        return [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]

    def test_commits_and_advances_to_verify(self):
        def edit():
            with open(os.path.join(self.wt, "a.txt"), "w") as f:
                f.write("after\n")
        self._run(edit=edit)

        self.assertEqual(self._card()["status"], "impl_verify")
        payload = self._payload()
        self.assertEqual(payload["target_repo"], TARGET)
        self.assertTrue(payload["commit"])
        self.assertEqual(payload["changed"], ["a.txt"])
        self.assertEqual(payload["impl"]["summary"], "고쳤다")
        self.assertIn("impl_committed", self._events())
        # 워커가 커밋했고 워크트리는 깨끗하다
        self.assertEqual(_git(self.wt, "status", "--porcelain"), "")

    def test_commit_message_carries_issue_and_instruction(self):
        def edit():
            with open(os.path.join(self.wt, "a.txt"), "w") as f:
                f.write("after\n")
        self._run(edit=edit)
        msg = _git(self.wt, "log", "-1", "--format=%B")
        self.assertIn("PH-1765", msg)
        self.assertIn("고쳤다", msg)
        self.assertIn("운영자 지시: ceo-client 만 건드려라", msg)
        self.assertIn(f"Refs: https://github.com/{REPO}/issues/1765", msg)

    def test_no_changes_fails_loudly(self):
        self._run(edit=None, reply='{"done": false, "summary": "고칠 게 없다"}')
        self.assertEqual(self._card()["status"], "failed")
        self.assertIn("impl_no_changes", self._events())
        self.assertNotIn("impl_committed", self._events())

    def test_unparsable_reply_still_commits_because_diff_is_the_truth(self):
        def edit():
            with open(os.path.join(self.wt, "b.txt"), "w") as f:
                f.write("new\n")
        self._run(edit=edit, reply="JSON 이 아닌 산문 응답")
        self.assertEqual(self._card()["status"], "impl_verify")
        self.assertIn("b.txt", self._payload()["changed"])

    def test_unknown_target_lands_in_failed_without_raising(self):
        impl_worker.CFG["impl_target_map"] = {}
        db.merge_payload(self.c, self.card_id, {"target_repo": "", "title": "[FE] 정할 수 없음"})
        engines.run_impl = lambda *_a, **_k: self.fail("엔진을 불러선 안 된다")
        impl_worker.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "failed")
        self.assertIn("impl_target_unknown", self._events())

    def test_card_engine_selects_the_runner(self):
        self.c.execute("UPDATE cards SET engine='codex' WHERE id=?", (self.card_id,))

        def edit():
            with open(os.path.join(self.wt, "a.txt"), "w") as f:
                f.write("after\n")
        self._run(edit=edit)
        self.assertEqual(self.calls[0][0], "codex")
        self.assertEqual(self.calls[0][1], self.wt)


if __name__ == "__main__":
    unittest.main()
