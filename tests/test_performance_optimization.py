import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from src import config, db, ghclient, monitor, tick


class PerformanceOptimizationTest(unittest.TestCase):
    def test_events_lookup_index_covers_key_type_and_ts(self):
        c = sqlite3.connect(":memory:")
        try:
            c.executescript(db.SCHEMA)
            indexes = {row[1] for row in c.execute("PRAGMA index_list(events)")}
            self.assertIn("idx_events_key_type_ts", indexes)
            columns = [row[2] for row in c.execute(
                "PRAGMA index_info(idx_events_key_type_ts)"
            )]
            self.assertEqual(columns, ["key", "type", "ts"])
        finally:
            c.close()

    def test_pr_states_batches_tracked_prs_in_one_graphql_call(self):
        calls = []
        old_run = ghclient._run

        def fake_run(args, check=True):
            calls.append(args)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "data": {
                        "r0": {
                            "p0": {"number": 1, "state": "OPEN", "headRefOid": "h1"},
                            "p1": {"number": 2, "state": "MERGED", "headRefOid": "h2"},
                        },
                        "r1": {
                            "p0": {"number": 9, "state": "CLOSED", "headRefOid": "h9"},
                        },
                    }
                }),
            )

        try:
            ghclient._run = fake_run
            rows = ghclient.pr_states([
                ("owner/repo", 1), ("owner/repo", 2), ("other/project", 9)
            ])
        finally:
            ghclient._run = old_run

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["api", "graphql"])
        self.assertEqual(rows[("owner/repo", 1)]["headRefOid"], "h1")
        self.assertEqual(rows[("owner/repo", 2)]["state"], "MERGED")
        self.assertEqual(rows[("other/project", 9)]["state"], "CLOSED")

    def test_pr_states_keeps_successful_batches_when_a_later_batch_fails(self):
        calls = []
        old_run = ghclient._run

        def fake_run(args, check=True):
            calls.append(args)
            if len(calls) == 2:
                raise ghclient.GhError("temporary batch failure")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "data": {"r0": {"p0": {
                        "number": 1, "state": "OPEN", "headRefOid": "h1"
                    }}}
                }),
            )

        try:
            ghclient._run = fake_run
            with self.assertLogs(ghclient.LOG, level="WARNING"):
                rows = ghclient.pr_states(
                    [("owner/repo", 1), ("owner/repo", 2)], batch_size=1
                )
        finally:
            ghclient._run = old_run

        self.assertEqual(len(calls), 2)
        self.assertEqual(rows, {
            ("owner/repo", 1): {"number": 1, "state": "OPEN", "headRefOid": "h1"}
        })

    def test_pr_states_keeps_partial_data_when_graphql_exits_one(self):
        payload = {
            "data": {
                "r0": {
                    "p0": {
                        "number": 1,
                        "state": "OPEN",
                        "headRefOid": "h1",
                        "author": {"login": "octocat"},
                    },
                    "p1": None,
                }
            },
            "errors": [{"type": "NOT_FOUND", "message": "missing PR"}],
        }
        proc = SimpleNamespace(
            returncode=1,
            stdout=json.dumps(payload),
            stderr="gh: Could not resolve to a PullRequest",
        )

        with patch.object(ghclient, "_run", return_value=proc) as run, \
             self.assertLogs(ghclient.LOG, level="WARNING"):
            rows = ghclient.pr_states([("owner/repo", 1), ("owner/repo", 99999)])

        self.assertFalse(run.call_args.kwargs.get("check", True))
        self.assertEqual(rows[("owner/repo", 1)]["author"]["login"], "octocat")
        self.assertNotIn(("owner/repo", 99999), rows)

    def test_pr_states_raises_with_stderr_when_graphql_has_no_data(self):
        proc = SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"data": None, "errors": [{"message": "denied"}]}),
            stderr="gh: GraphQL denied",
        )

        with patch.object(ghclient, "_run", return_value=proc), \
             self.assertLogs(ghclient.LOG, level="WARNING"):
            with self.assertRaisesRegex(ghclient.GhError, "GraphQL denied"):
                ghclient.pr_states([("owner/repo", 1)])

    def test_monitor_github_state_batches_roots_and_triage_once(self):
        # check_same_thread=False: _monitor_state_stage 는 카드가 2장 이상이면
        # ThreadPoolExecutor 로 넘어간다. 여기서는 MAX_CONCURRENT 를 1로 고정해
        # 순서를 단언하지만, 커넥션 자체는 스레드로 넘어가도 깨지지 않게 둔다.
        c = sqlite3.connect(":memory:", check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        now = db.now()
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('root','root','owner/repo',1,'h1','monitoring',?,?)""", (now, now)
        )
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/repo',2,'h2','triage',?,?)""", (now, now)
        )
        calls, processed = [], []

        @contextmanager
        def connect():
            yield c

        def fake_states(refs):
            calls.append(list(refs))
            return {("owner/repo", n): {
                "number": n, "state": "OPEN", "headRefOid": f"h{n}"
            } for n in (1, 2)}

        try:
            with patch.object(tick.db, "connect", connect), \
                 patch.object(ghclient, "pr_states", fake_states), \
                 patch.object(monitor, "process_root",
                              lambda _c, card, info=None: processed.append(
                                  ("root", info["number"]))), \
                 patch.object(monitor, "process_triage",
                              lambda _c, card, info=None: processed.append(
                                  ("triage", info["number"]))), \
                 patch.object(tick, "MAX_CONCURRENT", 1):
                tick._monitor_github_state()
        finally:
            c.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {("owner/repo", 1), ("owner/repo", 2)})
        self.assertEqual(processed, [("root", 1), ("triage", 2)])

    def test_monitor_missing_batch_entry_uses_per_card_fallback(self):
        c = sqlite3.connect(":memory:", check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        now = db.now()
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/repo',2,'h2','triage',?,?)""", (now, now)
        )
        seen = []

        @contextmanager
        def connect():
            yield c

        def fallback(_c, card, info=None):
            seen.append((card["key"], info))

        try:
            with patch.object(tick.db, "connect", connect), \
                 patch.object(ghclient, "pr_states", return_value={}), \
                 patch.object(monitor, "process_triage", fallback), \
                 patch.object(tick, "MAX_CONCURRENT", 1):
                tick._monitor_github_state()
        finally:
            c.close()

        self.assertEqual(seen, [("review", None)])

    def test_monitor_batch_failure_falls_back_and_logs_keyed_trace(self):
        c = sqlite3.connect(":memory:", check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        now = db.now()
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/repo',2,'h2','triage',?,?)""", (now, now)
        )

        @contextmanager
        def connect():
            yield c

        def failed_fallback(_c, card, info=None):
            raise ghclient.GhError("single PR fallback failed")

        try:
            with patch.object(tick.db, "connect", connect), \
                 patch.object(ghclient, "pr_states", side_effect=ghclient.GhError("batch failed")), \
                 patch.object(monitor, "process_triage", failed_fallback), \
                 patch.object(tick, "MAX_CONCURRENT", 1), \
                 self.assertLogs(tick.LOG, level="ERROR"):
                tick._monitor_github_state()
            rows = c.execute(
                "SELECT key, detail FROM events WHERE type='stage_error' ORDER BY id"
            ).fetchall()
        finally:
            c.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "review")
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["stage"], "monitor_triage")
        self.assertIn("single PR fallback failed", detail["trace"])


    # ── 이 PR 이 새로 만든 경로들 — 아래 셋은 커밋 당시 테스트가 없었다 ──────────

    def test_pr_states_skips_malformed_repo_refs_without_losing_the_batch(self):
        """repo 형식이 깨진 ref 하나가 배치 전체를 끌고 내려가지 않는다."""
        proc = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"data": {"r0": {"p0": {
                "number": 7, "state": "OPEN", "headRefOid": "h7",
                "author": {"login": "octocat"},
            }}}}),
            stderr="",
        )

        with patch.object(ghclient, "_run", return_value=proc) as run, \
             self.assertLogs(ghclient.LOG, level="WARNING"):
            rows = ghclient.pr_states([("badrepo", 1), ("a/b/c", 2),
                                       ("owner/", 3), ("owner/repo", 7)])

        self.assertEqual(list(rows), [("owner/repo", 7)])
        query = run.call_args.args[0][-1]
        self.assertIn('"owner"', query)
        self.assertNotIn("badrepo", query)
        self.assertNotIn("a/b/c", query)

    def test_pr_states_raises_without_calling_github_when_all_refs_malformed(self):
        with patch.object(ghclient, "_run") as run, \
             self.assertLogs(ghclient.LOG, level="WARNING"):
            with self.assertRaisesRegex(ghclient.GhError, "invalid GitHub repository"):
                ghclient.pr_states([("badrepo", 1)])
        run.assert_not_called()

    def test_init_drops_the_superseded_events_index(self):
        """옛 (key,type,id) 인덱스를 들고 있던 DB 도 init() 한 번으로 정리된다."""
        self._use_temp_db()
        with db.connect() as c:
            c.executescript(db.SCHEMA)
            c.execute("DROP INDEX IF EXISTS idx_events_key_type_ts")
            c.execute("CREATE INDEX idx_events_key_type_id ON events(key,type,id)")
            before = {r["name"] for r in c.execute("PRAGMA index_list(events)")}
        self.assertIn("idx_events_key_type_id", before)

        db.init()

        with db.connect() as c:
            after = {r["name"] for r in c.execute("PRAGMA index_list(events)")}
        self.assertNotIn("idx_events_key_type_id", after)
        self.assertIn("idx_events_key_type_ts", after)

    def test_monitor_state_stage_processes_cards_off_the_main_thread(self):
        """카드가 여럿이면 ThreadPoolExecutor 분기로 간다 — 스레드마다 제 커넥션을
        여는 운영 형태 그대로(여기서는 db.connect 를 가로채지 않는다) 돌려,
        배치에 있는 카드는 info 를, 없는 카드는 None 을 받는지 함께 본다."""
        self._use_temp_db()
        db.init()
        with db.connect() as c:
            for n in (1, 2, 3, 4, 5):
                db.upsert_card(c, f"review:owner/repo#{n}", "review", "owner/repo",
                               n, "triage", f"h{n}")

        seen, threads, lock = [], set(), threading.Lock()

        def record(_c, card, info=None):
            with lock:
                seen.append((card["pr_number"], info and info["headRefOid"]))
                threads.add(threading.current_thread().name)

        prefetched = {("owner/repo", n): {"number": n, "state": "OPEN",
                                          "headRefOid": f"h{n}"} for n in (1, 2, 3)}
        with patch.object(ghclient, "pr_states", return_value=prefetched), \
             patch.object(monitor, "process_triage", record), \
             patch.object(tick, "MAX_CONCURRENT", 3):
            tick._monitor_github_state()

        self.assertEqual(sorted(seen),
                         [(1, "h1"), (2, "h2"), (3, "h3"), (4, None), (5, None)])
        # 스레드 수는 스케줄러에 달렸지만, 워커 스레드에서 돌았다는 것은 확정이다.
        self.assertNotIn("MainThread", threads)
        with db.connect() as c:
            errors = c.execute(
                "SELECT COUNT(*) n FROM events WHERE type='stage_error'"
            ).fetchone()["n"]
        self.assertEqual(errors, 0)

    def _use_temp_db(self):
        tmp = tempfile.mkdtemp()
        saved = config.CFG["db_path"]
        config.CFG["db_path"] = os.path.join(tmp, "t.sqlite")
        self.addCleanup(config.CFG.__setitem__, "db_path", saved)
        self.addCleanup(shutil.rmtree, tmp, True)


if __name__ == "__main__":
    unittest.main()
