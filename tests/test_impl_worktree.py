import os
import shutil
import subprocess
import tempfile
import unittest

from src import worktree


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _git(cwd, *args):
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


class ImplWorktreeTest(unittest.TestCase):
    """구현 워크트리는 사용자 체크아웃을 부모로 쓰되, 쓰기(reset/clean)는 우리
    워크스페이스 안에서만 돌아야 한다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lookout_impl_")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.parent = os.path.join(self.tmp, "checkout")
        self.ws = os.path.join(self.tmp, "workspaces")
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", self.origin], check=True)
        subprocess.run(["git", "clone", "--quiet", self.origin, self.parent], check=True)
        _git(self.parent, "config", "user.email", "t@t")
        _git(self.parent, "config", "user.name", "t")
        _write(os.path.join(self.parent, "a.txt"), "hello\n")
        _write(os.path.join(self.parent, ".gitignore"), "node_modules/\n")
        _git(self.parent, "add", "-A")
        _git(self.parent, "commit", "--quiet", "-m", "init")
        _git(self.parent, "push", "--quiet", "-u", "origin", "main")
        _git(self.parent, "remote", "set-head", "origin", "--auto")

        self.repo = "acme/thing"
        self.saved = {k: worktree.CFG.get(k) for k in
                      ("impl_repo_paths", "impl_workspace_dir", "impl_base_ref",
                       "impl_setup_cmd")}
        worktree.CFG["impl_repo_paths"] = {self.repo: self.parent}
        worktree.CFG["impl_workspace_dir"] = self.ws
        worktree.CFG["impl_base_ref"] = {}
        worktree.CFG["impl_setup_cmd"] = {}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                worktree.CFG.pop(k, None)
            else:
                worktree.CFG[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_worktree_on_configured_checkout(self):
        wt = worktree.make_impl_worktree(self.repo, "lookout/ph-1")
        self.assertTrue(os.path.exists(os.path.join(wt, "a.txt")))
        self.assertEqual(_git(wt, "rev-parse", "--abbrev-ref", "HEAD"), "lookout/ph-1")
        # 부모가 사용자 체크아웃이어야 오브젝트 스토어를 공유한다
        self.assertIn(os.path.realpath(wt), _git(self.parent, "worktree", "list"))
        # origin/HEAD 를 따라 base 를 잡는다
        self.assertEqual(_git(wt, "rev-parse", "HEAD"), _git(self.parent, "rev-parse", "main"))

    def test_reuses_worktree_and_keeps_node_modules(self):
        wt = worktree.make_impl_worktree(self.repo, "lookout/ph-1")
        os.makedirs(os.path.join(wt, "node_modules", "dep"), exist_ok=True)
        _write(os.path.join(wt, "node_modules", "dep", "index.js"), "x")
        _write(os.path.join(wt, "leftover.txt"), "전 작업 잔여물")
        _write(os.path.join(wt, "a.txt"), "dirty\n")

        again = worktree.make_impl_worktree(self.repo, "lookout/ph-2")
        self.assertEqual(again, wt)                                     # 상주 1개
        self.assertEqual(_git(wt, "rev-parse", "--abbrev-ref", "HEAD"), "lookout/ph-2")
        # 설치물은 살아남고(ignored), 추적 안 되는 잔여물과 더러운 수정은 정리된다
        self.assertTrue(os.path.exists(os.path.join(wt, "node_modules", "dep", "index.js")))
        self.assertFalse(os.path.exists(os.path.join(wt, "leftover.txt")))
        with open(os.path.join(wt, "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello\n")

    def test_setup_cmd_runs_once_on_fresh_worktree(self):
        worktree.CFG["impl_setup_cmd"] = {self.repo: "echo installed > .setup-marker"}
        wt = worktree.make_impl_worktree(self.repo, "lookout/ph-1")
        marker = os.path.join(wt, ".setup-marker")
        self.assertTrue(os.path.exists(marker))
        os.remove(marker)
        worktree.make_impl_worktree(self.repo, "lookout/ph-2")   # 재사용 시엔 안 돈다
        self.assertFalse(os.path.exists(marker))

    def test_refuses_to_reset_outside_workspace(self):
        with self.assertRaises(RuntimeError) as cm:
            worktree._assert_bot_worktree(self.parent)   # 사용자 체크아웃
        self.assertIn("refusing", str(cm.exception))

    def test_unknown_repo_raises_instead_of_guessing(self):
        with self.assertRaises(worktree.ImplRepoUnknown):
            worktree.impl_parent("acme/not-configured")

    def test_remove_keeps_branch_for_open_pr(self):
        wt = worktree.make_impl_worktree(self.repo, "lookout/ph-1")
        worktree.remove_impl_worktree(self.repo)
        self.assertFalse(os.path.exists(os.path.join(wt, ".git")))
        self.assertIn("lookout/ph-1", _git(self.parent, "branch", "--list", "lookout/ph-1"))

    def test_impl_and_review_worktrees_use_different_paths(self):
        self.assertNotIn(worktree.config.path(worktree.CFG["worktree_dir"]),
                         worktree.impl_worktree_path(self.repo))


if __name__ == "__main__":
    unittest.main()


class ImplBranchNameTest(unittest.TestCase):
    """브랜치는 대상 repo 관례(feature/PH-1682)를 따라야 CI·보호규칙 글롭에 걸린다."""

    def setUp(self):
        self.saved = worktree.CFG.get("impl_branch_template")
        worktree.CFG.pop("impl_branch_template", None)

    def tearDown(self):
        if self.saved is None:
            worktree.CFG.pop("impl_branch_template", None)
        else:
            worktree.CFG["impl_branch_template"] = self.saved

    def test_strips_leading_routing_tags(self):
        self.assertEqual(
            worktree.impl_branch_name("PH-1767", "[FE] www build file type 제거"),
            "feature/PH-1767-www-build-file-type")
        self.assertEqual(
            worktree.impl_branch_name("PH-1765", "[FE][CEO_APP] 401 에러시 사용자 정보 함께 전송"),
            "feature/PH-1765-401")

    def test_korean_only_title_keeps_number_alone(self):
        self.assertEqual(
            worktree.impl_branch_name("PH-1816", "임대인 온보딩 페이지 로그인 검증 우회"),
            "feature/PH-1816")

    def test_template_is_configurable(self):
        worktree.CFG["impl_branch_template"] = "bot/{display}"
        self.assertEqual(worktree.impl_branch_name("PH-1", "무엇"), "bot/PH-1")


class ImplWorktreeBranchReuseTest(ImplWorktreeTest):
    """같은 카드가 검증·재구현으로 워크트리를 다시 요구한다. 그때 브랜치를 base로
    되감으면 앞선 커밋이 조용히 사라진다."""

    def test_existing_branch_keeps_its_commits(self):
        wt = worktree.make_impl_worktree(self.repo, "feature/PH-1")
        _write(os.path.join(wt, "impl.txt"), "구현물\n")
        _git(wt, "add", "-A")
        _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--quiet", "-m", "impl")
        sha = _git(wt, "rev-parse", "HEAD")

        again = worktree.make_impl_worktree(self.repo, "feature/PH-1")   # 재진입
        self.assertEqual(_git(again, "rev-parse", "HEAD"), sha)
        self.assertTrue(os.path.exists(os.path.join(again, "impl.txt")))

    def test_switching_away_and_back_keeps_commits(self):
        wt = worktree.make_impl_worktree(self.repo, "feature/PH-1")
        _write(os.path.join(wt, "impl.txt"), "구현물\n")
        _git(wt, "add", "-A")
        _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--quiet", "-m", "impl")
        sha = _git(wt, "rev-parse", "HEAD")

        worktree.make_impl_worktree(self.repo, "feature/PH-2")           # 다른 카드
        back = worktree.make_impl_worktree(self.repo, "feature/PH-1")    # 돌아옴
        self.assertEqual(_git(back, "rev-parse", "HEAD"), sha)


class ImplSessionLockTest(unittest.TestCase):
    """워크트리는 repo당 하나를 공유한다. 준비 구간만 락으로 감싸면, 같은 대상
    저장소의 다른 카드가 편집 중인 트리를 reset --hard 로 지우고 브랜치를 바꿔치기해
    앞 카드의 커밋이 남의 브랜치에 올라간다."""

    def test_same_repo_turns_are_serialized(self):
        import threading
        import time
        order = []

        def turn(n):
            with worktree.impl_session("acme/x"):
                order.append(f"in{n}")
                time.sleep(0.12)
                order.append(f"out{n}")

        threads = [threading.Thread(target=turn, args=(i,)) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn(order, ([f"in1", "out1", "in2", "out2"],
                              ["in2", "out2", "in1", "out1"]))

    def test_different_repos_do_not_block_each_other(self):
        import threading
        import time
        started = []

        def turn(repo):
            with worktree.impl_session(repo):
                started.append(repo)
                time.sleep(0.2)

        threads = [threading.Thread(target=turn, args=(r,)) for r in ("acme/a", "acme/b")]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLess(time.time() - t0, 0.38, "다른 repo 끼리 직렬화됐다")
        self.assertEqual(sorted(started), ["acme/a", "acme/b"])

    def test_lock_is_reentrant_so_make_impl_worktree_nests(self):
        with worktree.impl_session("acme/x"):
            with worktree.impl_session("acme/x"):
                pass   # RLock 이 아니면 여기서 데드락
