import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from src import agy_runner, cli, config, dashboard, db, engines

BIG = "가" * 50_000


class AgyRunnerTest(unittest.TestCase):
    def setUp(self):
        self.saved = subprocess.run
        self.calls = []
        self.stdout = (
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "text_delta": '{"lgtm": true}',
                },
            }) + "\n" +
            json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": "conv-1",
                    "status": "SUCCESS",
                    "response": '{"lgtm": true, "findings": []}\n',
                },
            }) + "\n"
        )
        self.stderr = ""
        self.returncode = 0

        def fake(args, **kw):
            self.calls.append((args, kw))

            class P:
                returncode = self.returncode
                stdout = self.stdout
                stderr = self.stderr

            return P()

        subprocess.run = fake

    def tearDown(self):
        subprocess.run = self.saved

    def test_run_sends_prompt_via_stream_json_stdin_not_argv(self):
        out = agy_runner.run(BIG, cwd="/tmp/org__repo__pr1__abcdef1234",
                             add_dir="/tmp/org__repo__pr1__abcdef1234")
        self.assertEqual(json.loads(out), {"lgtm": True, "findings": []})
        args, kw = self.calls[-1]
        self.assertNotIn(BIG, args)
        self.assertIn("--input-format", args)
        self.assertEqual(args[args.index("--input-format") + 1], "stream-json")
        self.assertIn("--output-format", args)
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertIn("--disable-slash-commands", args)
        self.assertIn("--add-dir", args)
        self.assertEqual(args[args.index("--add-dir") + 1], "/tmp/org__repo__pr1__abcdef1234")
        sent = json.loads(kw["input"])
        self.assertEqual(sent, {"event": "user", "message": {"content": BIG}})

    def test_readonly_settings_allow_read_and_search_and_deny_mutations(self):
        agy_runner.run("review this", cwd="/tmp/wt")
        args, _ = self.calls[-1]
        self.assertIn("--mode", args)
        self.assertEqual(args[args.index("--mode") + 1], "plan")
        self.assertIn("--settings", args)
        settings = json.loads(args[args.index("--settings") + 1])
        perms = settings["permissions"]
        for allowed in ("read_file(*)", "command(rg)", "command(grep)", "command(find)",
                        "command(head)", "command(ls)", "command(git diff)"):
            self.assertIn(allowed, perms["allow"])
        for denied in ("write_file(*)", "mcp(*)", "read_url(*)", "execute_url(*)", "download_url(*)",
                       "command(regex:find .*-(?:exec|execdir|delete|ok|okdir).*)"):
            self.assertIn(denied, perms["deny"])

    def test_empty_response_after_stream_events_fails_closed(self):
        """Regression: empty response must not fall back to raw NDJSON and turn
        the first init/step_update event into a zero-finding LGTM reply."""
        self.stdout = (
            json.dumps({"event": "init", "session_id": "s1"}) + "\n"
            + json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": ""}}) + "\n"
        )
        self.stderr = "jetski: no output produced — tool permission auto-denied\n"
        with self.assertRaises(agy_runner.AgyError) as cm:
            agy_runner.run("hi", cwd="/tmp/wt")
        self.assertIn("agy returned no response", str(cm.exception))
        self.assertIn("auto-denied", str(cm.exception))

        # Only non-agent_response step_update events without a result event
        self.stdout = (
            json.dumps({"event": "init", "session_id": "s1"}) + "\n"
            + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "state": "ERROR"}}) + "\n"
        )
        self.stderr = ""
        with self.assertRaises(agy_runner.AgyError):
            agy_runner.run("hi", cwd="/tmp/wt")

        # Non-event plain JSON fallback still works when no stream events were seen
        self.stdout = '{"lgtm": true}'
        self.assertEqual(agy_runner.run("hi", cwd="/tmp/wt"), '{"lgtm": true}')

    def test_model_and_effort_options(self):
        agy_runner.run("hi", cwd="/tmp/wt", model="gemini-4-neon-high", effort="xhigh")
        args, _ = self.calls[-1]
        self.assertEqual(args[args.index("--model") + 1], "gemini-4-neon-high")
        self.assertEqual(args[args.index("--effort") + 1], "xhigh")

        # effort='' suppresses --effort even when config sets a default
        with mock.patch.object(agy_runner, "EFFORT", "high"):
            agy_runner.run("hi", cwd="/tmp/wt", effort="")
        args2, _ = self.calls[-1]
        self.assertNotIn("--effort", args2)

    def test_nonzero_exit_raises_agy_error_with_cleaned_stderr(self):
        self.returncode = 1
        self.stderr = (
            "I1127 12:00:00.000000 1 server.go:10] Starting language server\n"
            "-- stack trace:\n"
            "| google3/third_party/jetski/foo.go:1\n"
            "RESOURCE_EXHAUSTED: quota exceeded, try again in 5 minutes\n"
        )
        with self.assertRaises(agy_runner.AgyError) as cm:
            agy_runner.run("hi", cwd="/tmp/wt")
        msg = str(cm.exception)
        self.assertIn("rc=1", msg)
        self.assertIn("RESOURCE_EXHAUSTED", msg)
        self.assertNotIn("Starting language server", msg)

    def test_failed_result_status_raises_agy_error(self):
        self.stdout = json.dumps({
            "event": "result",
            "result": {"status": "ERROR", "error": "rate limit exceeded"},
        }) + "\n"
        with self.assertRaises(agy_runner.AgyError) as cm:
            agy_runner.run("hi", cwd="/tmp/wt")
        self.assertIn("rate limit exceeded", str(cm.exception))


class AgyEngineIntegrationTest(unittest.TestCase):
    def test_engine_dispatch_and_run_json(self):
        with mock.patch.object(agy_runner, "run", return_value='{"lgtm": true}') as m:
            out = engines.run_json("prompt", engine="agy", cwd="/tmp/wt", add_dir="/tmp/wt")
        self.assertEqual(out, {"lgtm": True})
        m.assert_called_once_with("prompt", cwd="/tmp/wt", add_dir="/tmp/wt")

    def test_quota_error_detects_resource_exhausted(self):
        self.assertTrue(engines.is_quota_error("agy failed: RESOURCE_EXHAUSTED"))
        self.assertTrue(engines.is_quota_error("Resource exhausted on backend"))
        self.assertFalse(engines.is_quota_error("syntax error in json"))

    def test_agy_logged_in_checks_env_state_and_keychain(self):
        with mock.patch.object(config, "subprocess_env", return_value={"GEMINI_API_KEY": "k"}):
            self.assertTrue(engines._agy_logged_in())

        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, ".gemini", "antigravity-cli", "jetski_state.pbtxt")
            os.makedirs(os.path.dirname(state), exist_ok=True)
            with open(state, "w", encoding="utf-8") as f:
                f.write("post_onboarding: true\n")
            with (mock.patch.object(config, "subprocess_env", return_value={}),
                  mock.patch("os.path.expanduser", return_value=state)):
                self.assertTrue(engines._agy_logged_in())

        fake_proc = mock.Mock(returncode=0)
        with (mock.patch.object(config, "subprocess_env", return_value={}),
              mock.patch("os.path.expanduser", return_value="/nonexistent/jetski_state.pbtxt"),
              mock.patch("subprocess.run", return_value=fake_proc) as mrun):
            self.assertTrue(engines._agy_logged_in())
        self.assertEqual(mrun.call_args[0][0], ["security", "find-generic-password", "-s", "gemini"])

    def test_dashboard_and_cli_accept_agy_engine(self):
        import sqlite3
        from contextlib import contextmanager

        self.assertIn("agy", engines.ENGINES)
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        conn.execute("ALTER TABLE cards ADD COLUMN engine TEXT DEFAULT 'claude'")

        @contextmanager
        def fake_connect():
            yield conn

        try:
            cid1 = db.upsert_card(conn, "review:1", "review", "owner/r", 1, "triage", "aaa")
            cid2 = db.upsert_card(conn, "review:2", "review", "owner/r", 2, "triage", "bbb")
            with (mock.patch.object(db, "connect", fake_connect),
                  mock.patch.object(db, "init"),
                  mock.patch.object(dashboard.engines, "is_ready", return_value=True),
                  mock.patch.object(dashboard, "kick_tick")):
                self.assertTrue(dashboard.do_action("start", cid1, "agy"))
                cli.cmd_start(cid2, "agy")
            c1 = conn.execute("SELECT engine, status FROM cards WHERE id=?", (cid1,)).fetchone()
            c2 = conn.execute("SELECT engine, status FROM cards WHERE id=?", (cid2,)).fetchone()
            self.assertEqual((c1["engine"], c1["status"]), ("agy", "intake"))
            self.assertEqual((c2["engine"], c2["status"]), ("agy", "intake"))
        finally:
            conn.close()

    def test_default_engine_weights_round_robin_and_fallback(self):
        import sqlite3

        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        avail_both = {
            "claude": {"installed": False, "logged_in": False, "ready": False},
            "codex": {"installed": True, "logged_in": True, "ready": True},
            "agy": {"installed": True, "logged_in": True, "ready": True},
        }
        try:
            with (mock.patch.dict(config.CFG, {"default_review_engine_weights": {"codex": 50, "agy": 50}}),
                  mock.patch.object(engines, "availability", return_value=avail_both)):
                picks = [engines.default_engine(conn) for _ in range(6)]
            self.assertEqual(picks, ["codex", "agy", "codex", "agy", "codex", "agy"])

            # Custom ratio (70:30 -> 7 codex, 3 agy per 10)
            conn.execute("DELETE FROM meta WHERE k='default_engine_rr'")
            with (mock.patch.dict(config.CFG, {"default_review_engine_weights": {"codex": 70, "agy": 30}}),
                  mock.patch.object(engines, "availability", return_value=avail_both)):
                picks10 = [engines.default_engine(conn) for _ in range(10)]
            self.assertEqual(picks10.count("codex"), 7)
            self.assertEqual(picks10.count("agy"), 3)

            # Unready engine is skipped automatically
            avail_codex_only = dict(avail_both, agy={"installed": True, "logged_in": False, "ready": False})
            with (mock.patch.dict(config.CFG, {"default_review_engine_weights": {"codex": 50, "agy": 50}}),
                  mock.patch.object(engines, "availability", return_value=avail_codex_only)):
                self.assertEqual(engines.default_engine(conn), "codex")

            # Empty weights falls back to default_review_engine
            with (mock.patch.dict(config.CFG, {"default_review_engine": "agy", "default_review_engine_weights": {}}),
                  mock.patch.object(engines, "availability", return_value=avail_both)):
                self.assertEqual(engines.default_engine(conn), "agy")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
