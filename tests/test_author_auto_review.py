"""사람별 자동 리뷰 on/off — 대시보드에서 누른 값이 config 보다 우선한다."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from src import config, dashboard, db, engines, ghclient, router


class _RouterConfig:
    """router 의 import 시점 config 값을 테스트 동안만 바꾼다."""

    def __init__(self, authors):
        self.authors = set(authors)

    def __enter__(self):
        self.saved = (router.AUTO_REVIEW_ALL, router.AUTO_REVIEW_AUTHORS)
        router.AUTO_REVIEW_AUTHORS = self.authors
        router.AUTO_REVIEW_ALL = bool(self.authors & {"*", "all"})

    def __exit__(self, *_):
        router.AUTO_REVIEW_ALL, router.AUTO_REVIEW_AUTHORS = self.saved


class AutoReviewOverrideTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        # engine 컬럼은 SCHEMA 가 아니라 db.init() 의 마이그레이션이 붙인다
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT DEFAULT 'claude'")
        self.addCleanup(self.c.close)

    def test_without_override_config_decides(self):
        with _RouterConfig({"alice"}):
            self.assertTrue(router.auto_review_for(self.c, "alice"))
            self.assertFalse(router.auto_review_for(self.c, "bob"))

    def test_override_on_beats_empty_config(self):
        db.set_auto_review_override(self.c, "bob", True)
        with _RouterConfig(set()):
            self.assertTrue(router.auto_review_for(self.c, "bob"))

    def test_override_off_beats_wildcard_config(self):
        db.set_auto_review_override(self.c, "alice", False)
        with _RouterConfig({"*"}):
            self.assertFalse(router.auto_review_for(self.c, "alice"))
            self.assertTrue(router.auto_review_for(self.c, "carol"))

    def test_toggling_back_updates_the_same_row(self):
        db.set_auto_review_override(self.c, "bob", True)
        db.set_auto_review_override(self.c, "bob", False)
        self.assertEqual(db.auto_review_overrides(self.c), {"bob": False})

    def test_repo_policy_still_beats_person_on(self):
        # product-hub 는 repo_profiles 에서 auto_review:false — 사람을 켜도 triage
        self.assertEqual(router._initial_status("zigbang/product-hub", "bob", True), "triage")
        self.assertEqual(router._initial_status("zigbang/zigbang-client", "bob", True), "intake")
        self.assertEqual(router._initial_status("zigbang/zigbang-client", "bob", False), "triage")

    def test_config_auto_review_with_fresh_list(self):
        self.assertTrue(router.config_auto_review("x", ["all"]))
        self.assertTrue(router.config_auto_review("x", ["x"]))
        self.assertFalse(router.config_auto_review("x", ["y"]))
        self.assertFalse(router.config_auto_review("x", []))

    def test_ensure_pr_cards_uses_the_override(self):
        info = {"state": "OPEN", "isDraft": False, "headRefOid": "abc123",
                "baseRefName": "main", "title": "t", "url": "u",
                "author": {"login": "bob"}}
        db.set_auto_review_override(self.c, "bob", True)
        with _RouterConfig(set()), \
                mock.patch.object(router, "WATCH_AUTHORS", set()), \
                mock.patch.object(ghclient, "pr_view", return_value=info), \
                mock.patch.object(engines, "default_engine", return_value="codex"):
            router.ensure_pr_cards(self.c, "zigbang/zigbang-client", 7, source="poller")
        card = self.c.execute("SELECT status FROM cards WHERE kind='review'").fetchone()
        self.assertEqual(card["status"], "intake")


class DashboardAuthorSettingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = config.CFG["db_path"]
        config.CFG["db_path"] = os.path.join(self.tmp, "t.sqlite")
        db.init()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(config.CFG.__setitem__, "db_path", self.saved)

    def test_rejects_bad_input(self):
        self.assertFalse(dashboard.do_author_setting("", True))
        self.assertFalse(dashboard.do_author_setting("bob'); drop", True))
        self.assertFalse(dashboard.do_author_setting("bob", "yes"))
        self.assertFalse(dashboard.do_author_setting(None, True))
        with db.connect() as c:
            self.assertEqual(db.auto_review_overrides(c), {})

    def test_setting_is_stored_and_logged(self):
        self.assertTrue(dashboard.do_author_setting("bob", True))
        with db.connect() as c:
            self.assertEqual(db.auto_review_overrides(c), {"bob": True})
            ev = c.execute("SELECT COUNT(*) n FROM events WHERE type='operator_auto_review'"
                           ).fetchone()["n"]
        self.assertEqual(ev, 1)

    def test_build_authors_merges_watch_list_and_board(self):
        cfg = {"watch_authors": ["alice"], "auto_review_authors": ["alice"]}
        with db.connect() as c:
            db.upsert_card(c, "review:1", "review", "o/r", 1, "triage", "h",
                           payload={"author": "bob"})
            db.upsert_card(c, "review:2", "review", "o/r", 2, "archived", "h",
                           payload={"author": "gone"})
            db.set_auto_review_override(c, "bob", True)
        with mock.patch.object(config, "load", return_value=cfg):
            rows = {a["login"]: a for a in dashboard.build_authors()}
        self.assertEqual(set(rows), {"alice", "bob"})
        self.assertEqual((rows["alice"]["auto_review"], rows["alice"]["source"]), (True, "config"))
        self.assertEqual((rows["bob"]["auto_review"], rows["bob"]["source"]), (True, "dashboard"))


if __name__ == "__main__":
    unittest.main()
