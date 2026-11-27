from contextlib import nullcontext
import sqlite3
import unittest
from unittest import mock

from src import db, ghclient, ledger, pathway_auditor, prdiff, prompt_tpl, reviewer, tick, verifier, worktree


class PathwayAuditorTriggerTest(unittest.TestCase):
    def test_failed_message_resend_path_triggers_audit(self):
        diff = """diff --git a/src/Chat.tsx b/src/Chat.tsx
--- a/src/Chat.tsx
+++ b/src/Chat.tsx
@@ -1 +1,4 @@
+const retry = () => onResendFailedMessage(message).catch(onResendFailure)
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_queue_flush_path_triggers_audit(self):
        diff = """diff --git a/src/session.ts b/src/session.ts
--- a/src/session.ts
+++ b/src/session.ts
@@ -1 +1 @@
+failedMessageQueue.flush(userId)
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_sdk_callback_registration_triggers_audit(self):
        diff = """diff --git a/src/sdk.ts b/src/sdk.ts
--- a/src/sdk.ts
+++ b/src/sdk.ts
@@ -1 +1 @@
+client.setCallback(handleResult)
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_ui_lifecycle_callback_triggers_audit(self):
        diff = """diff --git a/src/Dialog.tsx b/src/Dialog.tsx
--- a/src/Dialog.tsx
+++ b/src/Dialog.tsx
@@ -1 +1 @@
+<Dialog onClose={clearPendingRequest} />
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_test_only_interaction_change_does_not_trigger_audit(self):
        diff = """diff --git a/tests/Button.test.tsx b/tests/Button.test.tsx
--- a/tests/Button.test.tsx
+++ b/tests/Button.test.tsx
@@ -1 +1 @@
+render(<Button onClick={retry} />)
"""
        self.assertFalse(pathway_auditor.should_run(diff))

    def test_top_level_python_test_change_does_not_trigger_audit(self):
        diff = """diff --git a/test_worker.py b/test_worker.py
--- a/test_worker.py
+++ b/test_worker.py
@@ -1 +1 @@
+worker.consume(job)
"""
        self.assertFalse(pathway_auditor.should_run(diff))

    def test_queue_put_triggers_audit(self):
        diff = """diff --git a/src/jobs.py b/src/jobs.py
--- a/src/jobs.py
+++ b/src/jobs.py
@@ -1 +1 @@
+queue.put(job)
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_effect_lifecycle_triggers_audit(self):
        diff = """diff --git a/src/Dialog.tsx b/src/Dialog.tsx
--- a/src/Dialog.tsx
+++ b/src/Dialog.tsx
@@ -1 +1 @@
+useEffect(() => syncState(), [])
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_sdk_completion_handler_triggers_audit(self):
        diff = """diff --git a/src/sdk.swift b/src/sdk.swift
--- a/src/sdk.swift
+++ b/src/sdk.swift
@@ -1 +1 @@
+completionHandler: handleResult
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_go_test_change_does_not_trigger_audit(self):
        diff = """diff --git a/src/worker_test.go b/src/worker_test.go
--- a/src/worker_test.go
+++ b/src/worker_test.go
@@ -1 +1 @@
+worker.consume(job)
"""
        self.assertFalse(pathway_auditor.should_run(diff))

    def test_plain_send_function_triggers_audit(self):
        diff = """diff --git a/src/messages.ts b/src/messages.ts
--- a/src/messages.ts
+++ b/src/messages.ts
@@ -1 +1 @@
+const sendMessage = async () => api.send(message)
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_plain_submit_function_triggers_audit(self):
        diff = """diff --git a/src/form.ts b/src/form.ts
--- a/src/form.ts
+++ b/src/form.ts
@@ -1 +1 @@
+async function submitForm() { await api.save(values) }
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_conventional_handle_action_names_trigger_audit(self):
        names = ("handleRetry", "handleClick", "handleError", "handle_retry")
        for name in names:
            with self.subTest(name=name):
                diff = f"""diff --git a/src/actions.ts b/src/actions.ts
--- a/src/actions.ts
+++ b/src/actions.ts
@@ -1 +1 @@
+const {name} = () => updateState()
"""
                self.assertTrue(pathway_auditor.should_run(diff))

    def test_rename_into_code_file_uses_the_new_path(self):
        diff = """diff --git a/template.txt b/src/worker.py
similarity index 80%
rename from template.txt
rename to src/worker.py
--- a/template.txt
+++ b/src/worker.py
@@ -1 +1 @@
+worker.consume(job)
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_plain_data_change_does_not_trigger_audit(self):
        diff = """diff --git a/src/constants.ts b/src/constants.ts
--- a/src/constants.ts
+++ b/src/constants.ts
@@ -1 +1 @@
-export const LIMIT = 10
+export const LIMIT = 20
"""
        self.assertFalse(pathway_auditor.should_run(diff))

    def test_removed_interaction_still_triggers_audit(self):
        diff = """diff --git a/src/Button.tsx b/src/Button.tsx
--- a/src/Button.tsx
+++ b/src/Button.tsx
@@ -1 +1 @@
-onPress={() => retry()}
+disabled={true}
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_promise_rejection_routing_triggers_audit(self):
        diff = """diff --git a/src/client.ts b/src/client.ts
--- a/src/client.ts
+++ b/src/client.ts
@@ -1 +1 @@
+return Promise.reject(response.reason)
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_worker_consumption_triggers_audit(self):
        diff = """diff --git a/src/jobs.py b/src/jobs.py
--- a/src/jobs.py
+++ b/src/jobs.py
@@ -1 +1 @@
+worker.consume(job)
"""
        self.assertTrue(pathway_auditor.should_run(diff))

    def test_generic_tokens_and_comment_lines_do_not_trigger_audit(self):
        for line in (
            "r = requests.get(url, timeout=30)", "// send analytics later",
            "# retry later", "/* send later */", " * retry later",
            "const label = useCallback(() => fmt(x), [x])",
            "import { ErrorHandler } from './x'", "except ValueError:", "finally:",
        ):
            with self.subTest(line=line):
                diff = "diff --git a/src/app.ts b/src/app.ts\n+" + line
                self.assertFalse(pathway_auditor.should_run(diff))


class PathwayAuditorPromptTest(unittest.TestCase):
    def test_sdk_owned_paths_search_repository_evidence_before_being_excluded(self):
        with open("prompts/pathway_review.md", encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("repository patches, vendored sources, and exact handler excerpts", prompt)
        rendered = prompt_tpl.render("pathway_review.md", CURRENT_FINDINGS='[{"rule": "known-bug"}]')
        self.assertIn('known-bug', rendered)
        self.assertIn("Do not report these same defects again", rendered)


class PathwayAuditorMergeTest(unittest.TestCase):
    def test_audit_without_coverage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "coverage"):
            pathway_auditor.validate_result({"findings": []})

    def test_partial_coverage_row_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "coverage rows"):
            pathway_auditor.validate_result({
                "coverage": [{"entry_point": "src/Chat.tsx:onPress", "evidence": ["line 1"]}],
                "findings": [],
            })

    def test_duplicate_rule_keeps_the_higher_impact_version(self):
        primary = [{
            "file": "src/Chat.tsx", "line": "10", "rule": "missing-resend-path",
            "severity": "medium", "confidence": "high", "title": "primary",
        }]
        audit = [{
            "file": "src/Chat.tsx", "line": "12", "rule": "missing-resend-path",
            "severity": "high", "confidence": "high", "title": "audit duplicate",
        }]

        merged = pathway_auditor.merge_findings(primary, audit, limit=8)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "audit duplicate")

    def test_high_severity_auditor_finding_survives_the_combined_limit(self):
        primary = [
            {"file": "a.ts", "rule": "a", "severity": "medium", "confidence": "high"},
            {"file": "b.ts", "rule": "b", "severity": "medium", "confidence": "high"},
        ]
        audit = [
            {"file": "c.ts", "rule": "c", "severity": "high", "confidence": "high"},
        ]

        merged = pathway_auditor.merge_findings(primary, audit, limit=2)

        self.assertEqual([f["rule"] for f in merged], ["c", "a"])


class PathwayAuditorIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.card_id = db.upsert_card(
            self.c, "review:pathway", "review", "owner/repo", 7, "intake", "head",
            payload={
                "title": "retry failed messages",
                "author": "author",
                "review_policy": {
                    "profile_type": "code", "max_findings": 8, "min_confidence": "medium"
                },
            },
        )
        self.c.execute("UPDATE cards SET engine='codex' WHERE id=?", (self.card_id,))

    def tearDown(self):
        self.c.close()

    def test_risky_code_diff_runs_independent_audit_and_persists_its_finding(self):
        raw_diff = """diff --git a/src/Chat.tsx b/src/Chat.tsx
--- a/src/Chat.tsx
+++ b/src/Chat.tsx
@@ -1 +1 @@
+onPress={() => onResendFailedMessage(message).catch(onResendFailure)}
"""
        calls = []

        def run(prompt, **_kwargs):
            calls.append(prompt)
            if prompt == "pathway_review.md":
                return {
                    "coverage": [{
                        "outcome": "failed message retry", "entry_point": "src/Chat.tsx:onPress",
                        "trigger": "tap failed bubble", "handler_chain": ["onResendFailedMessage"],
                        "success_sink": "message sent", "failure_sink": "onResendFailure",
                        "covered_by_change": False,
                        "evidence": ["src/Chat.tsx:1 — onResendFailedMessage(message)"],
                    }],
                    "findings": [{
                        "file": "src/Chat.tsx", "line": "1",
                        "rule": "resend-bypasses-policy",
                        "severity": "medium", "confidence": "high",
                        "title": "재전송 실패가 정책을 우회합니다",
                        "evidence": "onResendFailedMessage(message).catch(onResendFailure)",
                        "problem": "실패 버블 재전송이 별도 오류 처리로 빠집니다.",
                        "fix": "공통 실패 정책을 연결하세요.",
                    }],
                }
            return {"findings": [], "summary": "base pass"}

        card = self.c.execute("SELECT * FROM cards WHERE id=?", (self.card_id,)).fetchone()
        with mock.patch.object(ghclient, "pr_view", return_value={"state": "OPEN", "headRefOid": "head"}), \
                mock.patch.object(ghclient, "pr_author_identity", return_value={"login": "author"}), \
                mock.patch.object(ghclient, "collect_author_replies", return_value=[]), \
                mock.patch.object(ledger, "build", return_value=("", "")), \
                mock.patch.object(prdiff, "fetch", return_value=raw_diff), \
                mock.patch.object(prdiff, "pack_logged", return_value=(raw_diff, "MANIFEST")), \
                mock.patch.object(worktree, "make_worktree", return_value="/tmp/wt"), \
                mock.patch.object(worktree, "remove_worktree"), \
                mock.patch.object(prompt_tpl, "render", side_effect=lambda name, **_kw: name), \
                mock.patch.object(reviewer.engines, "run_json", side_effect=run), \
                mock.patch.dict(reviewer.CFG, {"pathway_audit_enabled": True}):
            reviewer.process(self.c, card)

        self.assertEqual(calls, ["review.codex.md", "pathway_review.md"])
        finding = self.c.execute(
            "SELECT title, status FROM findings WHERE title=?",
            ("재전송 실패가 정책을 우회합니다",),
        ).fetchone()
        self.assertIsNotNone(finding)
        self.assertEqual(finding["status"], "pending_verify")
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE id=?", (self.card_id,)).fetchone()["status"],
            "verifying",
        )
        event = self.c.execute("SELECT detail FROM events WHERE type='pathway_audit_completed'").fetchone()
        self.assertIsNotNone(event)

    def _run_failed_audit(self, primary, audit):
        card = self.c.execute("SELECT * FROM cards WHERE id=?", (self.card_id,)).fetchone()
        with mock.patch.object(ghclient, "pr_view", return_value={"state": "OPEN", "headRefOid": card["head_sha"]}), \
                mock.patch.object(ghclient, "pr_author_identity", return_value={"login": "author"}), \
                mock.patch.object(ghclient, "collect_author_replies", return_value=[]), \
                mock.patch.object(ledger, "build", return_value=("", "")), \
                mock.patch.object(prdiff, "fetch", return_value="diff --git a/src/app.ts b/src/app.ts\n+onClick={send}"), \
                mock.patch.object(prdiff, "pack_logged", return_value=("DIFF", "MANIFEST")), \
                mock.patch.object(worktree, "make_worktree", return_value="/tmp/wt"), \
                mock.patch.object(worktree, "remove_worktree"), \
                mock.patch.object(prompt_tpl, "render", side_effect=lambda name, **_kw: name), \
                mock.patch.object(reviewer.engines, "run_json", side_effect=[primary, audit]), \
                mock.patch.dict(reviewer.CFG, {"pathway_audit_enabled": True}):
            reviewer.process(self.c, card)

        return self.c.execute("SELECT * FROM cards WHERE id=?", (self.card_id,)).fetchone()

    def test_invalid_audit_preserves_primary_findings_and_blocks_lgtm_after_rejection(self):
        primary = {"findings": [{
            "file": "src/app.ts", "line": "1", "rule": "bug",
            "severity": "high", "confidence": "high", "title": "primary bug",
        }]}
        card = self._run_failed_audit(primary, {"coverage": [], "findings": []})
        self.assertEqual(card["status"], "verifying")
        finding = self.c.execute("SELECT * FROM findings").fetchone()
        self.assertEqual(finding["title"], "primary bug")
        db.set_finding_status(self.c, finding["id"], "rejected")
        verifier.process(self.c, card)
        self.assertEqual(self.c.execute("SELECT status FROM cards").fetchone()[0], "commented")
        self.assertIsNotNone(self.c.execute(
            "SELECT 1 FROM events WHERE type='pathway_audit_invalid'").fetchone())

    def test_quota_error_propagates_before_primary_findings_are_saved(self):
        primary = {"findings": [{
            "file": "src/app.ts", "line": "1", "rule": "bug",
            "severity": "high", "confidence": "high", "title": "primary bug",
        }]}
        with self.assertRaisesRegex(RuntimeError, "quota"):
            self._run_failed_audit(primary, RuntimeError("quota exhausted"))
        finding = self.c.execute("SELECT * FROM findings").fetchone()
        self.assertIsNone(finding)

    def test_audit_quota_uses_existing_notification_and_triage_path(self):
        card = self.c.execute("SELECT * FROM cards WHERE id=?", (self.card_id,)).fetchone()
        with mock.patch.object(db, "connect", side_effect=lambda: nullcontext(self.c)), \
                mock.patch.object(tick.notify, "send") as notify:
            tick._process_one(
                lambda c, card: self._run_failed_audit(
                    {"findings": []}, RuntimeError("quota exhausted")), card, "reviewer")
        self.assertEqual(self.c.execute("SELECT status FROM cards").fetchone()[0], "triage")
        notify.assert_called_once()
        self.assertIsNotNone(self.c.execute(
            "SELECT 1 FROM events WHERE type='review_quota_paused'").fetchone())
        self.assertIsNone(self.c.execute("SELECT 1 FROM events WHERE type='stage_error'").fetchone())

    def _clean_audit(self):
        return {"coverage": [{
            "outcome": "send", "entry_point": "src/app.ts:onClick", "trigger": "click",
            "handler_chain": ["send"], "success_sink": "sent", "failure_sink": "error",
            "covered_by_change": True, "evidence": ["src/app.ts:1 send"],
        }], "findings": []}

    def _verify_recovered(self, confirmed):
        card = self.c.execute("SELECT * FROM cards WHERE id=?", (self.card_id,)).fetchone()
        with mock.patch.object(prdiff, "fetch", return_value="diff"), \
                mock.patch.object(ghclient, "pr_author_identity", return_value={}), \
                mock.patch.object(ghclient, "collect_author_replies", return_value=[]), \
                mock.patch.object(ledger, "build", return_value=("", "")), \
                mock.patch.object(worktree, "make_worktree", return_value="/tmp/wt"), \
                mock.patch.object(worktree, "remove_worktree"), \
                mock.patch.object(verifier.engines, "run_json", return_value={"confirmed": confirmed}):
            verifier.process(self.c, card)

    def test_existing_orphans_and_same_card_retries_reach_current_head_verification(self):
        for source_status in ("triage", "archived", "same-card"):
            for confirmed in (True, False):
                with self.subTest(source=source_status, confirmed=confirmed):
                    self.c.execute("DELETE FROM findings")
                    source = self.card_id if source_status == "same-card" else db.upsert_card(
                        self.c, "old:" + source_status, "review", "owner/repo", 7,
                        source_status, "old-head")
                    db.upsert_finding(self.c, source, "owner/repo", 7, "old-head", "orphan",
                                      "old unverified bug", "{}", "src/app.ts", "1",
                                      "high", "high", "pending_verify")
                    card = self._run_failed_audit({"findings": []}, self._clean_audit())
                    self.assertEqual(card["status"], "verifying")
                    finding = self.c.execute("SELECT * FROM findings").fetchone()
                    self.assertEqual(finding["card_id"], self.card_id)
                    self.assertEqual(finding["status"], "pending_verify")
                    self._verify_recovered(confirmed)
                    finding = self.c.execute("SELECT * FROM findings").fetchone()
                    self.assertEqual(finding["status"], "confirmed" if confirmed else "rejected")
                    status = self.c.execute("SELECT status FROM cards WHERE id=?", (self.card_id,)).fetchone()[0]
                    self.assertEqual(status, "commenting" if confirmed else "lgtm")

    def test_active_attempts_and_other_pr_findings_are_not_taken(self):
        source = db.upsert_card(self.c, "active", "review", "owner/repo", 7, "verifying", "head")
        for repo, pr, fp in (("owner/repo", 7, "active"), ("other/repo", 7, "other-repo"),
                             ("owner/repo", 8, "other-pr")):
            db.upsert_finding(self.c, source, repo, pr, "head", fp, fp, "{}",
                              "src/app.ts", "1", "high", "high", "pending_verify")
        other_head = db.upsert_card(self.c, "other-head-active", "review", "owner/repo", 7,
                                    "verifying", "other-head")
        db.upsert_finding(self.c, other_head, "owner/repo", 7, "other-head", "other-head",
                          "bug", "{}", "src/app.ts", "1", "high", "high", "pending_verify")
        self._run_failed_audit({"findings": []}, self._clean_audit())
        self.assertEqual(len(db.findings_for_card(self.c, other_head, "pending_verify")), 1)
        self.assertEqual(len(db.findings_for_card(self.c, source, "pending_verify")), 3)
        self.assertEqual(db.findings_for_card(self.c, self.card_id), [])

    def test_quota_then_new_clean_head_can_reach_lgtm(self):
        old_card = self.c.execute("SELECT * FROM cards WHERE id=?", (self.card_id,)).fetchone()
        with mock.patch.object(db, "connect", side_effect=lambda: nullcontext(self.c)), \
                mock.patch.object(tick.notify, "send"):
            tick._process_one(lambda c, card: self._run_failed_audit({"findings": [{
                "file": "src/app.ts", "line": "1", "rule": "bug", "severity": "high",
                "confidence": "high", "title": "bug",
            }]}, RuntimeError("quota exhausted")), old_card, "reviewer")
        self.assertEqual(self.c.execute("SELECT status FROM cards WHERE id=?",
                                       (old_card["id"],)).fetchone()[0], "triage")
        with mock.patch.object(ghclient, "pr_view", return_value={
                "state": "OPEN", "headRefOid": "new-head"}):
            reviewer.process(self.c, old_card)
        self.assertEqual(self.c.execute("SELECT status FROM cards WHERE id=?",
                                       (old_card["id"],)).fetchone()[0], "archived")
        self.card_id = db.upsert_card(self.c, "review:new-head", "review", "owner/repo", 7,
                                     "intake", "new-head")
        card = self._run_failed_audit({"findings": []}, self._clean_audit())
        self.assertEqual(card["status"], "lgtm")
        self.assertEqual(db.open_findings_count(self.c, "owner/repo", 7), 0)

    def test_engine_failure_without_primary_findings_blocks_lgtm(self):
        card = self._run_failed_audit({"findings": []}, RuntimeError("audit failed"))
        self.assertEqual(card["status"], "commented")


if __name__ == "__main__":
    unittest.main()
