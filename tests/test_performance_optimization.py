import json
import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from src import db, ghclient, monitor, tick


class PerformanceOptimizationTest(unittest.TestCase):
    def test_events_lookup_index_exists(self):
        c = sqlite3.connect(":memory:")
        try:
            c.executescript(db.SCHEMA)
            indexes = {row[1] for row in c.execute("PRAGMA index_list(events)")}
            self.assertIn("idx_events_key_type_id", indexes)
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

    def test_monitor_github_state_batches_roots_and_triage_once(self):
        c = sqlite3.connect(":memory:")
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
        old_connect = tick.db.connect
        old_states = getattr(ghclient, "pr_states", None)
        old_root = monitor.process_root
        old_triage = monitor.process_triage
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
            tick.db.connect = connect
            ghclient.pr_states = fake_states
            monitor.process_root = lambda _c, card, info=None: processed.append(
                ("root", info["number"])
            )
            monitor.process_triage = lambda _c, card, info=None: processed.append(
                ("triage", info["number"])
            )
            tick._monitor_github_state()
        finally:
            tick.db.connect = old_connect
            if old_states is None:
                del ghclient.pr_states
            else:
                ghclient.pr_states = old_states
            monitor.process_root = old_root
            monitor.process_triage = old_triage
            c.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {("owner/repo", 1), ("owner/repo", 2)})
        self.assertEqual(processed, [("root", 1), ("triage", 2)])


if __name__ == "__main__":
    unittest.main()
