import json
import pathlib
import sqlite3
import unittest

from src import db, ghclient, keys, poller

REPO = "acme/product-hub"


def _issue(n, title="[FE] 무언가", assignees=("me",), labels=(),
           issue_type=None, parent=None, sub=None, project_items=None):
    row = {"number": n, "title": title, "url": f"https://github.com/{REPO}/issues/{n}",
           "labels": [{"name": x} for x in labels],
           "assignees": [{"login": x} for x in assignees], "updatedAt": "2026-09-10T00:00:00Z",
           "issueType": {"name": issue_type} if issue_type else None,
           "parent": ({"number": parent, "title": f"에픽 {parent}",
                       "url": f"https://github.com/{REPO}/issues/{parent}"}
                      if parent else None),
           "subIssuesSummary": {"completed": (sub or (0, 0))[0], "total": (sub or (0, 0))[1]},
           "projectItems": project_items or []}
    return row


class IssueSourceTest(unittest.TestCase):
    """이슈는 PR과 같은 보드를 쓰되, 리뷰 스테이지로는 절대 새어 들어가지 않아야 한다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved_list = ghclient.issue_list
        self.saved_cfg = {k: poller.CFG.get(k) for k in
                          ("issue_repos", "issue_assignee", "issue_title_prefixes",
                           "issue_display_prefix")}
        poller.CFG["issue_repos"] = [REPO]
        poller.CFG["issue_assignee"] = "@me"
        poller.CFG["issue_title_prefixes"] = []
        poller.CFG["issue_display_prefix"] = {REPO: "PH"}

    def tearDown(self):
        ghclient.issue_list = self.saved_list
        for k, v in self.saved_cfg.items():
            if v is None:
                poller.CFG.pop(k, None)
            else:
                poller.CFG[k] = v
        self.c.close()

    def _cards(self, **where):
        sql = "SELECT * FROM cards"
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        return self.c.execute(sql, tuple(where.values())).fetchall()

    def _payload(self, key):
        row = db.get_card(self.c, key)
        return json.loads(row["payload"])

    # ── kind 게이트 ───────────────────────────────────────────────
    def test_issue_card_is_invisible_to_review_stages(self):
        """triage를 PR 카드와 공유하지만 kind로 걸러지지 않으면 reviewer가 집어간다."""
        db.upsert_card(self.c, keys.issue_key(REPO, 7), "issue", REPO, 7, status="triage")
        db.upsert_card(self.c, keys.review_key("acme/app", 7, "sha"), "review",
                       "acme/app", 7, status="triage", head_sha="sha")

        review_lane = db.cards_in(self.c, ["triage"], kind="review")
        self.assertEqual([r["kind"] for r in review_lane], ["review"])

        issue_lane = db.cards_in(self.c, ["triage"], kind="issue")
        self.assertEqual([r["kind"] for r in issue_lane], ["issue"])

        # kind 없이 부르면 둘 다 나온다 — 그래서 스테이지 호출은 kind를 명시해야 한다
        self.assertEqual(len(db.cards_in(self.c, ["triage"])), 2)

    # ── 에픽 소속 ─────────────────────────────────────────────────
    def test_poll_stores_epic_membership_from_github(self):
        """소속은 GitHub 네이티브 sub-issue 관계를 그대로 싣는다 — 제목 태그로
        추정하지 않는다. 추정하면 표기가 흔들리는 순간 소속이 틀린다."""
        ghclient.issue_list = lambda *_a, **_k: [
            _issue(2015, "폴더블 대응", issue_type="Epic", sub=(1, 6)),
            _issue(2017, "[1] edge-to-edge", issue_type="Task", parent=2015),
        ]
        poller.poll_issues(self.c)

        epic = self._payload(keys.issue_key(REPO, 2015))
        self.assertEqual(epic["issue_type"], "Epic")
        self.assertIsNone(epic["parent"])
        self.assertEqual(epic["sub"], {"done": 1, "total": 6})

        task = self._payload(keys.issue_key(REPO, 2017))
        self.assertEqual(task["parent"]["number"], 2015)
        # 부모도 별칭으로 — 렌더가 repo를 보고 분기하지 않게 여기서 확정한다
        self.assertEqual(task["parent"]["display"], "PH-2015")
        self.assertEqual(task["parent"]["title"], "에픽 2015")

    def test_parent_outside_the_board_still_carries_its_title(self):
        """실측 11건 중 4건은 부모가 내게 할당되지 않아 카드가 없다. 자식이 제목·
        링크를 들고 와야 에픽 뷰가 머리글을 세울 수 있다."""
        ghclient.issue_list = lambda *_a, **_k: [_issue(1765, parent=1680)]
        poller.poll_issues(self.c)
        task = self._payload(keys.issue_key(REPO, 1765))
        self.assertEqual(task["parent"]["display"], "PH-1680")
        self.assertTrue(task["parent"]["url"].endswith("/1680"))
        self.assertEqual(self._cards(pr_number=1680), [])   # 부모는 카드가 아니다

    def test_losing_a_parent_clears_the_old_membership(self):
        """merge_payload 는 키를 덮는다 — 부모가 떨어져 나가면 None 이 실려야
        옛 소속이 화면에 남지 않는다."""
        ghclient.issue_list = lambda *_a, **_k: [_issue(1765, parent=1680)]
        poller.poll_issues(self.c)
        ghclient.issue_list = lambda *_a, **_k: [_issue(1765)]
        poller.poll_issues(self.c)
        self.assertIsNone(self._payload(keys.issue_key(REPO, 1765))["parent"])

    # ── 티켓 진행상태(읽기 전용) ───────────────────────────────────
    def test_poll_carries_the_project_status(self):
        """보드 레인과 티켓 진행상태는 다른 축이다 — 대기 12건이 전부 같은 얼굴이던
        문제가 여기서 갈린다(Backlog / Ready dev / Developing)."""
        ghclient.issue_list = lambda *_a, **_k: [_issue(
            2163, project_items=[{"status": {"name": "Ready dev"}, "title": "product backlog"}])]
        poller.poll_issues(self.c)
        m = self._payload(keys.issue_key(REPO, 2163))
        self.assertEqual(m["ticket_status"], "Ready dev")
        self.assertEqual(m["ticket_board"], "product backlog")

    def test_status_comes_from_the_first_board_that_has_one(self):
        """한 이슈가 여러 프로젝트에 올라가 있다(실측 #1842 = product backlog + QA).
        상태 없는 항목이 먼저 와도 값을 찾아내야 한다."""
        ghclient.issue_list = lambda *_a, **_k: [_issue(1842, project_items=[
            {"status": None, "title": "상태 없는 보드"},
            {"status": {"name": "NextPatch"}, "title": "product backlog"},
            {"status": {"name": "Next Patch"}, "title": "QA"}])]
        poller.poll_issues(self.c)
        m = self._payload(keys.issue_key(REPO, 1842))
        self.assertEqual((m["ticket_status"], m["ticket_board"]), ("NextPatch", "product backlog"))

    def test_issue_outside_any_project_has_no_status(self):
        """프로젝트에 없는 이슈도 보드에 떠야 한다 — 빈 값이 곧 '미상'이다."""
        ghclient.issue_list = lambda *_a, **_k: [_issue(1767)]
        poller.poll_issues(self.c)
        self.assertEqual(self._payload(keys.issue_key(REPO, 1767))["ticket_status"], "")

    def test_issue_and_pr_keys_do_not_collide_on_same_number(self):
        self.assertNotEqual(keys.issue_key(REPO, 7), keys.root_key(REPO, 7))

    # ── 폴링 ─────────────────────────────────────────────────────
    def test_poll_creates_card_with_display_alias(self):
        ghclient.issue_list = lambda *_a, **_k: [_issue(1767, "[FE] www build file type 제거")]
        poller.poll_issues(self.c)

        cards = self._cards(kind="issue")
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0]["repo"], cards[0]["pr_number"]), (REPO, 1767))
        self.assertEqual(cards[0]["status"], "triage")
        self.assertEqual(self._payload(keys.issue_key(REPO, 1767))["display"], "PH-1767")

    def test_poll_is_idempotent_and_preserves_operator_instruction(self):
        ghclient.issue_list = lambda *_a, **_k: [_issue(1767, "옛 제목")]
        poller.poll_issues(self.c)
        key = keys.issue_key(REPO, 1767)
        db.merge_payload(self.c, db.get_card(self.c, key)["id"],
                         {"instruction": "www 쪽만 건드려라"})

        ghclient.issue_list = lambda *_a, **_k: [_issue(1767, "새 제목")]
        poller.poll_issues(self.c)

        self.assertEqual(len(self._cards(kind="issue")), 1)
        payload = self._payload(key)
        self.assertEqual(payload["title"], "새 제목")          # 제목은 따라간다
        self.assertEqual(payload["instruction"], "www 쪽만 건드려라")  # 지시는 살아남는다

    def test_delisted_issue_is_archived_only_while_waiting(self):
        ghclient.issue_list = lambda *_a, **_k: [_issue(10), _issue(11)]
        poller.poll_issues(self.c)
        started = db.get_card(self.c, keys.issue_key(REPO, 11))
        db.set_status(self.c, started["id"], "implementing")

        ghclient.issue_list = lambda *_a, **_k: []   # 둘 다 목록에서 빠짐
        poller.poll_issues(self.c)

        self.assertEqual(db.get_card(self.c, keys.issue_key(REPO, 10))["status"], "archived")
        # 착수한 카드는 목록에서 빠져도 유지 — 작업 중인 것을 지우면 조용히 사라진다
        self.assertEqual(db.get_card(self.c, keys.issue_key(REPO, 11))["status"], "implementing")

    def test_gh_error_is_logged_not_raised(self):
        def boom(*_a, **_k):
            raise ghclient.GhError("gh issue list failed: rate limit")
        ghclient.issue_list = boom
        poller.poll_issues(self.c)   # 폴링 하나가 죽어도 tick 전체를 세우면 안 된다
        types = [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]
        self.assertIn("issue_poller_error", types)


if __name__ == "__main__":
    unittest.main()


class ProjectStatusIsReadOnlyTest(unittest.TestCase):
    """진행상태는 팀 공용 보드(GitHub Project)의 값이다. 읽기만 한다고 정했으니
    쓰기 경로가 생기면 여기서 막는다 — 봇이 남의 보드를 옮기는 건 되돌리기 어렵고,
    '동기화'라는 이름으로 조용히 들어오기 쉬운 변경이다."""

    WRITES = ("updateProjectV2", "ProjectV2ItemFieldValue",
              "project item-edit", "item-edit")

    def test_no_source_file_moves_a_ticket_on_the_project_board(self):
        src = pathlib.Path(__file__).resolve().parent.parent / "src"
        for f in sorted(src.glob("*.py")):
            text = f.read_text(encoding="utf-8")
            for needle in self.WRITES:
                self.assertNotIn(needle, text,
                                 f"{f.name} 가 Project 필드를 쓴다 — 읽기 전용 결정 위반")
