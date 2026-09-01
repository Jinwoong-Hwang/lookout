import sqlite3
import unittest

from src import db, router


class RouterDrainRetryTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.old_process = router.process_event

    def tearDown(self):
        router.process_event = self.old_process
        self.c.close()

    def _enqueue(self):
        db.enqueue_inbox(self.c, "delivery-1", "pull_request", '{"action": "opened"}')
        return self.c.execute("SELECT id FROM inbox").fetchone()["id"]

    def _drain_until_done(self, waves):
        for _ in range(waves):
            router.drain(self.c)
        return self.c.execute("SELECT processed FROM inbox").fetchone()["processed"]

    def test_failing_event_retries_then_gives_up(self):
        inbox_id = self._enqueue()

        def boom(*_):
            raise RuntimeError("gh unavailable")

        router.process_event = boom

        # still retryable below the cap
        self.assertEqual(self._drain_until_done(router.MAX_INBOX_RETRIES - 1), 0)
        # the cap marks it done so it stops being replayed forever
        self.assertEqual(self._drain_until_done(1), 1)
        gave_up = self.c.execute(
            "SELECT detail FROM events WHERE type='router_gave_up' AND key=?",
            (f"inbox:{inbox_id}",),
        ).fetchall()
        self.assertEqual(len(gave_up), 1)
        self.assertEqual(router.drain(self.c), 0)

    def test_transient_failure_still_succeeds_on_a_later_drain(self):
        self._enqueue()
        attempts = []

        def flaky(*_):
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("transient")

        router.process_event = flaky
        self.assertEqual(self._drain_until_done(3), 1)
        self.assertEqual(
            self.c.execute("SELECT COUNT(*) n FROM events WHERE type='router_gave_up'").fetchone()["n"],
            0,
        )

    def test_malformed_payload_is_dropped_without_retrying(self):
        db.enqueue_inbox(self.c, "delivery-2", "pull_request", "not json")
        router.drain(self.c)
        self.assertEqual(self.c.execute("SELECT processed FROM inbox").fetchone()["processed"], 1)


if __name__ == "__main__":
    unittest.main()
