import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from src import dashboard, db, engines, keys

REPO = "acme/product-hub"


class DashboardIssueActionTest(unittest.TestCase):
    """이슈 카드의 시작 버튼: 지시를 먼저 저장하고, triage에서만 전이한다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect, "kick": dashboard.kick_tick,
                      "ready": engines.is_ready}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c          # 테스트 커넥션을 닫지 않는다

        dashboard.db.connect = fake_connect
        self.kicked = []
        dashboard.kick_tick = lambda: self.kicked.append(1)
        engines.is_ready = lambda _e: True

        self.key = keys.issue_key(REPO, 1767)
        self.card_id = db.upsert_card(
            self.c, self.key, "issue", REPO, 1767, status="triage",
            payload={"display": "PH-1767", "title": "[FE] www build file type 제거",
                     "url": f"https://github.com/{REPO}/issues/1767",
                     "labels": [], "assignees": ["me"]})

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        engines.is_ready = self.saved["ready"]
        self.c.close()

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def test_save_instruction_merges_and_keeps_poller_fields(self):
        self.assertTrue(dashboard.do_action("save_instruction", self.card_id, text="  www만 "))
        payload = self._payload()
        self.assertEqual(payload["instruction"], "www만")
        self.assertEqual(payload["display"], "PH-1767")   # poller가 넣은 값 보존

    def test_start_impl_moves_to_implementing_with_mode(self):
        self.assertTrue(dashboard.do_action("start_impl", self.card_id, "claude"))
        self.assertEqual(self._card()["status"], "implementing")
        self.assertEqual(self._payload()["mode"], "implement")
        self.assertEqual(self._card()["engine"], "claude")
        self.assertEqual(len(self.kicked), 1)
        types = [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]
        self.assertIn("work_started", types)

    def test_start_debate_moves_to_spec_with_mode(self):
        self.assertTrue(dashboard.do_action("start_debate", self.card_id, "claude"))
        self.assertEqual(self._card()["status"], "spec")
        self.assertEqual(self._payload()["mode"], "debate")

    def test_spec_gate_needs_human_approval_before_implementing(self):
        db.set_status(self.c, self.card_id, "spec_blocked", blocked=1)
        db.merge_payload(self.c, self.card_id,
                         {"agreement": {"design": "이렇게 한다", "rounds": 4}})
        self.assertTrue(dashboard.do_action("approve_spec", self.card_id))
        card = self._card()
        self.assertEqual(card["status"], "implementing")
        self.assertEqual(card["blocked"], 0)
        types = [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]
        self.assertIn("operator_spec_approved", types)

    def test_spec_approval_is_rejected_outside_the_gate(self):
        self.assertFalse(dashboard.do_action("approve_spec", self.card_id))  # triage
        self.assertEqual(self._card()["status"], "triage")

    def test_rejecting_a_design_keeps_the_record_and_resets_the_debate(self):
        db.set_status(self.c, self.card_id, "spec_blocked", blocked=1)
        db.merge_payload(self.c, self.card_id, {
            "debate": [{"round": 1, "claim": "a"}],
            "agreement": {"design": "버린 안", "rounds": 1}})
        self.assertTrue(dashboard.do_action("reject_spec", self.card_id))
        card, payload = self._card(), self._payload()
        self.assertEqual(card["status"], "triage")
        self.assertEqual(payload["debate"], [])        # 다시 시작하면 새로
        self.assertEqual(payload["agreement"], {})
        self.assertEqual(payload["debate_prev"][0]["agreement"]["design"], "버린 안")

    def test_timeline_is_exposed_with_human_labels(self):
        dashboard.do_action("start_impl", self.card_id, "claude")
        db.log_event(self.c, "impl_engine_done", self.key, {"engine": "claude", "secs": 12.3})
        row = [r for r in dashboard.build_board() if r["kind"] == "issue"][0]
        labels = [e["label"] for e in row["timeline"]]
        self.assertIn("작업 시작", labels)
        self.assertIn("엔진 편집 종료", labels)
        note = next(e["detail"] for e in row["timeline"] if e["type"] == "impl_engine_done")
        self.assertIn("secs=12.3", note)

    def test_row_carries_updated_at_for_the_elapsed_badge(self):
        row = [r for r in dashboard.build_board() if r["kind"] == "issue"][0]
        self.assertGreater(row["updated_at"], 0)

    def test_event_note_prefers_a_human_readable_field(self):
        self.assertEqual(dashboard._event_note("x", '{"error": "터졌다"}'), "터졌다")
        self.assertIn("branch=b", dashboard._event_note("x", '{"branch": "b", "secs": 3}'))
        self.assertEqual(dashboard._event_note("x", None), "")
        self.assertEqual(dashboard._event_note("x", "not json"), "")

    def test_start_is_rejected_once_work_began(self):
        dashboard.do_action("start_impl", self.card_id, "claude")
        self.assertFalse(dashboard.do_action("start_impl", self.card_id, "claude"))
        # 지시 수정도 막힌다 — 워커가 읽은 seed와 로그가 어긋나지 않게
        self.assertFalse(dashboard.do_action("save_instruction", self.card_id, text="늦은 지시"))

    def test_start_blocked_when_engine_not_ready(self):
        engines.is_ready = lambda _e: False
        self.assertFalse(dashboard.do_action("start_impl", self.card_id, "claude"))
        self.assertEqual(self._card()["status"], "triage")
        types = [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]
        self.assertIn("work_start_blocked", types)

    def test_review_actions_do_not_apply_to_issue_cards(self):
        self.assertFalse(dashboard.do_action("start", self.card_id, "claude"))
        self.assertEqual(self._card()["status"], "triage")

    def test_build_board_emits_issue_row_without_pr_machinery(self):
        dashboard.do_action("save_instruction", self.card_id, text="지시")
        rows = [r for r in dashboard.build_board() if r["kind"] == "issue"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["display"], "PH-1767")
        self.assertEqual(row["instruction"], "지시")
        self.assertEqual((row["findings"], row["comments"], row["head"]), ([], [], ""))

    def test_work_board_is_a_separate_view_from_review_board(self):
        """작업 레인을 리뷰 보드에 붙이면 컬럼이 16개가 되고 Triage에 PR 카드와
        이슈 카드가 섞인다. 리뷰 보드는 손대지 않는다."""
        review = [k for k, _ in dashboard.LANES]
        work = [k for k, _ in dashboard.WORK_LANES]
        self.assertEqual(len(review), len(set(review)))
        # 리뷰 보드는 종전 11레인 그대로, 작업 상태가 섞이지 않는다
        self.assertEqual(review, ["triage", "intake", "reviewing", "verifying",
                                  "commenting", "commented", "lgtm", "approve_blocked",
                                  "approving", "done", "failed"])
        self.assertEqual(dict(dashboard.LANES)["triage"], "📥 Triage (리뷰 대기)")
        for status in list(dashboard.WORK_START.values()) + ["impl_verify", "pr_blocked"]:
            self.assertIn(status, work)
            self.assertNotIn(status, review)
            self.assertNotIn(status, dashboard.ACTIVE_REVIEW)

    def test_review_and_work_views_never_show_each_others_cards(self):
        """뷰별로 kind를 갈라 보여준다 — 한 보드에 섞으면 Triage가 뒤엉킨다."""
        html = dashboard.HTML
        self.assertIn("VIEW==='work'?DATA.filter(c=>c.kind==='issue')", html)
        self.assertIn("DATA.filter(c=>c.kind!=='issue')", html)
        self.assertIn("renderLanes(VIEW==='work'?WORK_LANES:LANES)", html)


if __name__ == "__main__":
    unittest.main()


class IssueRetryRoutingTest(unittest.TestCase):
    """failed 재시도는 자기 레인으로 돌아가야 한다 — intake로 보내면 리뷰 레인이라
    kind 게이트 때문에 아무도 집어가지 않고 카드가 영원히 선다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect, "kick": dashboard.kick_tick}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c

        dashboard.db.connect = fake_connect
        dashboard.kick_tick = lambda: None

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        self.c.close()

    def test_issue_card_retries_into_implementing(self):
        key = keys.issue_key(REPO, 9)
        cid = db.upsert_card(self.c, key, "issue", REPO, 9, status="failed")
        self.assertTrue(dashboard.do_action("retry", cid))
        self.assertEqual(db.get_card(self.c, key)["status"], "implementing")

    def test_review_card_still_retries_into_intake(self):
        key = keys.review_key("acme/app", 9, "sha")
        cid = db.upsert_card(self.c, key, "review", "acme/app", 9,
                             status="failed", head_sha="sha")
        self.assertTrue(dashboard.do_action("retry", cid))
        self.assertEqual(db.get_card(self.c, key)["status"], "intake")


class RefreshScopeTest(unittest.TestCase):
    """작업 뷰에서 '이슈 가져오기'를 눌렀는데 PR 폴링이 돌면 리뷰 카드가 예고 없이
    늘어난다. 보고 있는 보드만 갱신한다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect,
                      "poll": dashboard.poller.poll,
                      "issues": dashboard.poller.poll_issues}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c

        dashboard.db.connect = fake_connect
        self.called = []
        dashboard.poller.poll = lambda _c: self.called.append("pr")
        dashboard.poller.poll_issues = lambda _c: self.called.append("issue")

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.poller.poll = self.saved["poll"]
        dashboard.poller.poll_issues = self.saved["issues"]
        self.c.close()

    def test_work_scope_polls_issues_only(self):
        dashboard.refresh_poll("work")
        self.assertEqual(self.called, ["issue"])

    def test_review_scope_polls_prs_only(self):
        dashboard.refresh_poll("review")
        self.assertEqual(self.called, ["pr"])

    def test_default_scope_is_review_so_existing_button_is_unchanged(self):
        dashboard.refresh_poll()
        self.assertEqual(self.called, ["pr"])

    def test_counts_only_the_scoped_kind(self):
        db.upsert_card(self.c, keys.review_key("a/b", 1, "s"), "review", "a/b", 1,
                       status="triage", head_sha="s")
        db.upsert_card(self.c, keys.issue_key(REPO, 2), "issue", REPO, 2, status="triage")
        self.assertEqual(dashboard.refresh_poll("work")["total"], 1)
        self.assertEqual(dashboard.refresh_poll("review")["total"], 1)


class SideNavTest(unittest.TestCase):
    """뷰 전환은 사이드 메뉴로, repo 필터바는 종전 위치·마크업 그대로."""

    def setUp(self):
        self.html = dashboard.HTML

    def test_view_switch_moved_out_of_the_header(self):
        header = self.html[self.html.index("<header>"):self.html.index("</header>")]
        self.assertNotIn('class="toggle"', header)
        self.assertIn('<nav class="side">', self.html)

    def test_side_menu_groups_review_and_work(self):
        for el in ("tLane", "tAuthor", "tFeedback", "tWork"):
            self.assertIn(f'id="{el}"', self.html)
        self.assertIn(">PR 리뷰<", self.html)
        self.assertIn(">작업<", self.html)

    def test_repo_filterbar_is_untouched_and_inside_main(self):
        self.assertIn('<div class="filterbar" id="filterbar"></div>', self.html)
        self.assertLess(self.html.index('<div class="main">'),
                        self.html.index('id="filterbar"'))

    def test_board_and_mentions_stay_with_the_filter_in_main(self):
        main = self.html[self.html.index('<div class="main">'):self.html.index("</nav>") + 10000]
        for el in ('id="filterbar"', 'id="mentions"', 'id="board"'):
            self.assertIn(el, main)


class ServedJsTest(unittest.TestCase):
    """HTML 은 파이썬 문자열 리터럴이다. 소스에 쓴 \\n 이 모듈 로드 시 실제 개행이
    되어 서빙되면, JS 문자열 안에서 줄이 끊겨 스크립트 전체가 죽는다 — 파일만 보면
    정상으로 보이므로 눈으로는 못 잡는다."""

    def test_no_literal_newline_inside_a_js_string(self):
        for opener in ("('", '("'):
            self.assertNotIn(opener + "\n", dashboard.HTML,
                             f"JS 문자열 {opener} 안에 실제 개행이 서빙된다")

    @unittest.skipUnless(shutil.which("node"), "node 없음")
    def test_served_script_blocks_parse(self):
        html = (dashboard.HTML
                .replace("__LANES__", json.dumps(dashboard.LANES, ensure_ascii=False))
                .replace("__WORK_LANES__", json.dumps(dashboard.WORK_LANES, ensure_ascii=False)))
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(blocks)
        for i, js in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as f:
                f.write(js)
                path = f.name
            try:
                proc = subprocess.run(["node", "--check", path],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0,
                                 f"script 블록 {i} 문법 오류: {proc.stderr[:300]}")
            finally:
                os.unlink(path)


class IssueCardShapeTest(unittest.TestCase):
    """카드 골격은 리뷰 카드와 같아야 한다 — 제목은 평문, 상세는 모달."""

    def test_title_is_plain_text_not_a_link(self):
        self.assertNotIn('<div class="title"><a href', dashboard.HTML)

    def test_card_opens_a_modal_like_review_cards(self):
        self.assertIn("el.onclick=()=>openIssueModal(c)", dashboard.HTML)
        self.assertIn("function openIssueModal", dashboard.HTML)

    def test_card_uses_the_same_skeleton_classes(self):
        tile = dashboard.HTML[dashboard.HTML.index("function issueTile"):
                              dashboard.HTML.index("function tile(c)")]
        for cls in ('class="pr"', 'class="title"', 'class="row"', "repopill",
                    "statuspill", 'class="num"'):
            self.assertIn(cls, tile)

    def test_github_link_lives_in_the_modal(self):
        modal = dashboard.HTML[dashboard.HTML.index("function openIssueModal"):
                               dashboard.HTML.index("function openFeedbackModal")]
        self.assertIn("GitHub 이슈 열기", modal)


class ProgressVisibilityTest(unittest.TestCase):
    """진행 중 구간이 무음이면 멈춘 건지 도는 건지 알 수 없다."""

    def test_nothing_is_silently_gated(self):
        """비활성 버튼이 있으면 반드시 이유가 붙어야 한다. WORK_START_PENDING 이
        비어 있으면 열려 있어야 할 것이 다 열린 상태다."""
        self.assertEqual(dashboard.WORK_START_PENDING, {})
        self.assertIn("startWork(event,${c.id},'debate')", dashboard.HTML)
        self.assertIn("startWork(event,${c.id},'implement')", dashboard.HTML)

    def test_running_states_show_an_elapsed_badge(self):
        self.assertIn("const RUNNING=['spec','implementing','impl_verify','pr_opening']",
                      dashboard.HTML)
        self.assertIn("ago(c.updated_at)", dashboard.HTML)

    def test_modal_renders_the_timeline(self):
        modal = dashboard.HTML[dashboard.HTML.index("function openIssueModal"):
                               dashboard.HTML.index("function openFeedbackModal")]
        self.assertIn("진행 기록", modal)
        self.assertIn("hhmm(e.ts)", modal)

    def test_worker_marks_the_silent_window(self):
        import inspect

        from src import impl_worker
        src = inspect.getsource(impl_worker.process)
        for ev in ("impl_worktree_ready", "impl_engine_started", "impl_engine_done"):
            self.assertIn(ev, src)


class WorktreeHandoffTest(unittest.TestCase):
    """구현 브랜치를 사람이 직접 돌려보려면 경로가 필요하다. 봇 워크트리는 repo당
    하나를 공유하므로 다른 카드가 시작하면 브랜치가 갈린다 — 그 사실도 알려야 한다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect,
                      "parent": dashboard.worktree.impl_parent}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c

        dashboard.db.connect = fake_connect
        dashboard.worktree.impl_parent = lambda r: "/checkouts/" + r.split("/")[-1]
        self.key = keys.issue_key(REPO, 3)
        db.upsert_card(self.c, self.key, "issue", REPO, 3, status="impl_verify",
                       payload={"display": "PH-3", "title": "t",
                                "target_repo": "acme/ceo-client",
                                "branch": "feature/PH-3-fix",
                                "worktree": "/ws/acme__ceo-client/impl",
                                "commit": "abc1234"})

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.worktree.impl_parent = self.saved["parent"]
        self.c.close()

    def test_row_carries_bot_worktree_and_parent_checkout(self):
        row = [r for r in dashboard.build_board() if r["kind"] == "issue"][0]
        self.assertEqual(row["worktree"], "/ws/acme__ceo-client/impl")
        self.assertEqual(row["parent_repo_path"], "/checkouts/ceo-client")

    def test_unconfigured_repo_does_not_break_the_board(self):
        dashboard.worktree.impl_parent = lambda _r: (_ for _ in ()).throw(
            dashboard.worktree.ImplRepoUnknown("설정 없음"))
        row = [r for r in dashboard.build_board() if r["kind"] == "issue"][0]
        self.assertEqual(row["parent_repo_path"], "")

    def test_modal_warns_that_the_bot_worktree_gets_switched(self):
        modal = dashboard.HTML[dashboard.HTML.index("function openIssueModal"):
                               dashboard.HTML.index("function openFeedbackModal")]
        self.assertIn("브랜치가 갈립니다", modal)
        self.assertIn("worktree add", modal)


class SpecFeedbackTest(unittest.TestCase):
    """게이트에서 승인/반려만 되면 합의에 손을 댈 수 없다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect, "kick": dashboard.kick_tick}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c

        dashboard.db.connect = fake_connect
        dashboard.kick_tick = lambda: None
        self.key = keys.issue_key(REPO, 1816)
        self.card_id = db.upsert_card(
            self.c, self.key, "issue", REPO, 1816, status="spec_blocked", blocked=1,
            payload={"display": "PH-1816", "title": "t",
                     "debate": [{"round": 1, "role": "proposer", "claim": "안"}],
                     "agreement": {"design": "합의안", "rounds": 2}})

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        self.c.close()

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def test_approve_with_amendment_keeps_the_agreement_intact(self):
        self.assertTrue(dashboard.do_action("approve_spec", self.card_id,
                                           text="  가드는 라우트에서  "))
        payload = self._payload()
        self.assertEqual(payload["spec_amendment"], "가드는 라우트에서")
        self.assertEqual(payload["agreement"]["design"], "합의안")   # 원문 보존
        self.assertEqual(self._card()["status"], "implementing")

    def test_resume_debate_appends_an_operator_turn_and_raises_the_cap(self):
        self.assertTrue(dashboard.do_action("resume_debate", self.card_id,
                                           text="이 쟁점 더 다퉈라"))
        card, payload = self._card(), self._payload()
        self.assertEqual(card["status"], "spec")
        self.assertEqual(card["blocked"], 0)
        self.assertEqual(payload["debate"][-1],
                         {"role": "operator", "claim": "이 쟁점 더 다퉈라"})
        self.assertEqual(payload["debate_bonus"], dashboard.DEBATE_BONUS)
        self.assertEqual(payload["agreement"], {})   # 다시 만들어야 한다

    def test_resume_debate_without_feedback_is_refused(self):
        self.assertFalse(dashboard.do_action("resume_debate", self.card_id, text="   "))
        self.assertEqual(self._card()["status"], "spec_blocked")

    def test_amendment_reaches_the_implementation_prompt(self):
        from src import impl_worker
        text = impl_worker._agreement_text(
            {"agreement": {"design": "합의안"}, "spec_amendment": "라우트에서 풀어라"})
        self.assertIn("합의안", text)
        self.assertIn("운영자 수정 지시", text)
        self.assertIn("라우트에서 풀어라", text)


class TopicCreationTest(unittest.TestCase):
    """주제만 던져 토론시키는 경로 — 이슈 폴러와 무관하게 사람이 만든다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect, "kick": dashboard.kick_tick}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c

        dashboard.db.connect = fake_connect
        dashboard.kick_tick = lambda: None

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        self.c.close()

    def _rows(self):
        return self.c.execute("SELECT * FROM cards").fetchall()

    def test_topic_card_starts_in_the_debate_lane(self):
        out = dashboard.create_topic("이 파이프라인의 취약점은?\n두 번째 줄", "acme/web")
        self.assertTrue(out["ok"])
        self.assertEqual(out["display"], "TOPIC-1")
        row = self._rows()[0]
        payload = json.loads(row["payload"])
        self.assertEqual(row["status"], "spec")
        self.assertEqual((row["repo"], row["pr_number"]), (dashboard.TOPIC_REPO, 0))
        self.assertEqual(payload["mode"], "debate_only")
        self.assertEqual(payload["title"], "이 파이프라인의 취약점은?")   # 첫 줄이 제목
        self.assertIn("두 번째 줄", payload["topic"])                    # 본문은 전체

    def test_empty_topic_is_refused(self):
        self.assertFalse(dashboard.create_topic("   ")["ok"])
        self.assertEqual(self._rows(), [])

    def test_sequence_increments_per_topic(self):
        dashboard.create_topic("a")
        self.assertEqual(dashboard.create_topic("b")["display"], "TOPIC-2")

    def test_accepting_a_topic_result_goes_to_done_not_implementing(self):
        out = dashboard.create_topic("주제")
        cid = out["card_id"]
        db.set_status(self.c, cid, "spec_blocked", blocked=1)
        self.assertTrue(dashboard.do_action("approve_spec", cid, text="이 결론 채택"))
        row = self.c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(json.loads(row["payload"])["spec_amendment"], "이 결론 채택")

    def test_composer_shows_only_in_the_work_view(self):
        self.assertIn('id="composer"', dashboard.HTML)
        self.assertIn("""document.getElementById('composer').style.display=(v==='work')?'':'none';""",
                      dashboard.HTML)
