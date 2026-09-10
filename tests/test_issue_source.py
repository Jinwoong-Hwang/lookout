import json
import sqlite3
import unittest

from src import db, ghclient, keys, poller

REPO = "acme/product-hub"


def _issue(n, title="[FE] 무언가", assignees=("me",), labels=()):
    return {"number": n, "title": title, "url": f"https://github.com/{REPO}/issues/{n}",
            "labels": [{"name": x} for x in labels],
            "assignees": [{"login": x} for x in assignees], "updatedAt": "2026-09-10T00:00:00Z"}


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
