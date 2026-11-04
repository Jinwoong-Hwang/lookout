"""사람별 자동 리뷰 on/off — 대시보드 토글이 유일한 출처이고, 재시작해도 유지된다."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from src import config, dashboard, db, engines, ghclient, router


def _memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    # engine 컬럼은 SCHEMA 가 아니라 db.init() 의 마이그레이션이 붙인다
    c.execute("ALTER TABLE cards ADD COLUMN engine TEXT DEFAULT 'claude'")
    return c


class AutoReviewToggleTest(unittest.TestCase):
    def setUp(self):
        self.c = _memdb()
        self.addCleanup(self.c.close)

    def test_never_toggled_is_off(self):
        self.assertFalse(db.get_auto_review(self.c, "bob"))

    def test_toggle_is_the_value(self):
        db.set_auto_review(self.c, "bob", True)
        self.assertTrue(db.get_auto_review(self.c, "bob"))
        db.set_auto_review(self.c, "bob", False)
        self.assertFalse(db.get_auto_review(self.c, "bob"))
        self.assertEqual(db.auto_review_settings(self.c), {"bob": False})

    def test_config_list_is_not_read_at_decision_time(self):
        # 이관이 끝난 뒤엔 config 에 누가 있든 토글만 본다
        db.set_meta(self.c, db.AUTO_REVIEW_MIGRATED, "1")
        with mock.patch.dict(config.CFG, {"auto_review_authors": ["*"]}):
            self.assertFalse(db.get_auto_review(self.c, "bob"))

    def test_repo_policy_still_beats_toggle_on(self):
        # product-hub 는 repo_profiles 에서 auto_review:false — 켜도 triage
        self.assertEqual(router._initial_status("zigbang/product-hub", True), "triage")
        self.assertEqual(router._initial_status("zigbang/zigbang-client", True), "intake")
        self.assertEqual(router._initial_status("zigbang/zigbang-client", False), "triage")

    def _ensure(self):
        info = {"state": "OPEN", "isDraft": False, "headRefOid": "abc123",
                "baseRefName": "main", "title": "t", "url": "u",
                "author": {"login": "bob"}}
        with mock.patch.object(router, "WATCH_AUTHORS", set()), \
                mock.patch.object(ghclient, "pr_view", return_value=info), \
                mock.patch.object(engines, "default_engine", return_value="codex"):
            router.ensure_pr_cards(self.c, "zigbang/zigbang-client", 7, source="poller")
        return self.c.execute("SELECT status FROM cards WHERE kind='review'").fetchone()["status"]

    def test_ensure_pr_cards_uses_the_toggle_on(self):
        db.set_auto_review(self.c, "bob", True)
        self.assertEqual(self._ensure(), "intake")

    def test_ensure_pr_cards_defaults_to_triage(self):
        self.assertEqual(self._ensure(), "triage")


class AutoReviewMigrationTest(unittest.TestCase):
    """config 의 auto_review_authors 는 한 번만 토글로 옮긴다."""

    def setUp(self):
        self.c = _memdb()
        self.addCleanup(self.c.close)

    def _migrate(self, cfg):
        with mock.patch.dict(config.CFG, cfg):
            db._migrate_auto_review_from_config(self.c)

    def test_explicit_logins_become_on(self):
        self._migrate({"auto_review_authors": ["alice", "bob"]})
        self.assertEqual(db.auto_review_settings(self.c), {"alice": True, "bob": True})

    def test_wildcard_expands_to_watch_list_and_board(self):
        db.upsert_card(self.c, "review:1", "review", "o/r", 1, "triage", "h",
                       payload={"author": "carol"})
        self._migrate({"auto_review_authors": ["*"], "watch_authors": ["alice"]})
        self.assertEqual(db.auto_review_settings(self.c), {"alice": True, "carol": True})

    def test_existing_toggle_is_not_overwritten(self):
        db.set_auto_review(self.c, "alice", False)
        self._migrate({"auto_review_authors": ["alice"]})
        self.assertFalse(db.get_auto_review(self.c, "alice"))

    def test_runs_only_once(self):
        self._migrate({"auto_review_authors": []})
        # 이관 뒤에 config 를 고쳐도 토글은 그대로다
        self._migrate({"auto_review_authors": ["alice"]})
        self.assertEqual(db.auto_review_settings(self.c), {})

    def test_turning_off_after_migration_sticks(self):
        self._migrate({"auto_review_authors": ["alice"]})
        db.set_auto_review(self.c, "alice", False)
        self._migrate({"auto_review_authors": ["alice"]})
        self.assertFalse(db.get_auto_review(self.c, "alice"))


class DashboardAuthorSettingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = config.CFG["db_path"]
        config.CFG["db_path"] = os.path.join(self.tmp, "t.sqlite")
        with mock.patch.dict(config.CFG, {"auto_review_authors": []}):  # 로컬 config.json 과 무관하게
            db.init()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(config.CFG.__setitem__, "db_path", self.saved)

    def test_rejects_bad_input(self):
        self.assertFalse(dashboard.do_author_setting("", True))
        self.assertFalse(dashboard.do_author_setting("bob'); drop", True))
        self.assertFalse(dashboard.do_author_setting("bob", "yes"))
        self.assertFalse(dashboard.do_author_setting(None, True))
        with db.connect() as c:
            self.assertEqual(db.auto_review_settings(c), {})

    def test_setting_is_stored_and_logged(self):
        self.assertTrue(dashboard.do_author_setting("bob", True))
        with db.connect() as c:
            self.assertEqual(db.auto_review_settings(c), {"bob": True})
            ev = c.execute("SELECT COUNT(*) n FROM events WHERE type='operator_auto_review'"
                           ).fetchone()["n"]
        self.assertEqual(ev, 1)

    def test_toggle_survives_restart(self):
        """껐다 켜도 유지 — 데몬 재시작은 새 프로세스가 db.init() 을 다시 부르는 것이다."""
        dashboard.do_author_setting("alice", True)
        dashboard.do_author_setting("bob", False)
        with mock.patch.dict(config.CFG, {"auto_review_authors": ["bob"]}):
            db.init()   # 재시작: 스키마·이관을 다시 돌려도 토글을 건드리지 않는다
            db.init()
        with db.connect() as c:
            self.assertEqual(db.auto_review_settings(c), {"alice": True, "bob": False})

    def test_purge_keeps_toggles(self):
        dashboard.do_author_setting("alice", True)
        with db.connect() as c:
            db.purge_old(c, days=0)
            self.assertTrue(db.get_auto_review(c, "alice"))

    def test_build_authors_lists_watched_board_and_turned_on(self):
        cfg = {"watch_authors": ["alice"]}
        with db.connect() as c:
            db.upsert_card(c, "review:1", "review", "o/r", 1, "triage", "h",
                           payload={"author": "bob"})
            db.upsert_card(c, "review:2", "review", "o/r", 2, "archived", "h",
                           payload={"author": "gone"})
            db.set_auto_review(c, "bob", True)
            db.set_auto_review(c, "dave", True)    # 카드도 없고 추적 대상도 아니지만 켜져 있음
            db.set_auto_review(c, "erin", False)   # 꺼져 있고 카드도 없음 → 목록에 안 뜸
        with mock.patch.object(config, "load", return_value=cfg):
            rows = {a["login"]: a["auto_review"] for a in dashboard.build_authors()}
        self.assertEqual(rows, {"alice": False, "bob": True, "dave": True})


if __name__ == "__main__":
    unittest.main()
