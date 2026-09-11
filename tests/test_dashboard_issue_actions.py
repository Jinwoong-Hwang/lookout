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
        # 두 뷰는 렌더러부터 다르다 — 작업은 그룹, 리뷰는 레인
        self.assertIn("VIEW==='work'?renderWork():renderLanes(LANES)", html)


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
        self.assertIn("GitHub 이슈", modal)


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
        src = inspect.getsource(impl_worker)   # 턴 본문이 _run_turn 으로 갈렸다
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
        self.assertIn("orca worktree create", modal)   # 사람이 쓸 독립 워크트리


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


class RetryRoutesToFailedStageTest(unittest.TestCase):
    """실패한 카드를 무조건 implementing 으로 보내면, 설계 승인 전에 실패한 토론
    카드가 승인을 건너뛰고 코드를 고친다 (토론이 지적한 결함)."""

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

    def _card(self, n, **payload):
        key = keys.issue_key(REPO, n)
        cid = db.upsert_card(self.c, key, "issue", REPO, n, status="failed",
                             payload={"display": f"PH-{n}", "title": "t", **payload})
        return key, cid

    def test_failed_debate_returns_to_spec_not_implementing(self):
        key, cid = self._card(1, mode="debate", failed_from="spec")
        self.assertTrue(dashboard.do_action("retry", cid))
        self.assertEqual(db.get_card(self.c, key)["status"], "spec")

    def test_failed_implementation_returns_to_implementing(self):
        key, cid = self._card(2, mode="implement", failed_from="implementing")
        self.assertTrue(dashboard.do_action("retry", cid))
        self.assertEqual(db.get_card(self.c, key)["status"], "implementing")

    def test_failed_verify_returns_to_verify(self):
        key, cid = self._card(3, mode="implement", failed_from="impl_verify")
        self.assertTrue(dashboard.do_action("retry", cid))
        self.assertEqual(db.get_card(self.c, key)["status"], "impl_verify")

    def test_debate_card_without_marker_still_falls_back_to_spec(self):
        """예외로 죽어 failed_from 을 남기지 못한 경우 — mode 로 판정한다."""
        key, cid = self._card(4, mode="debate")
        self.assertTrue(dashboard.do_action("retry", cid))
        self.assertEqual(db.get_card(self.c, key)["status"], "spec")

    def test_review_card_is_untouched(self):
        key = keys.review_key("acme/app", 9, "sha")
        cid = db.upsert_card(self.c, key, "review", "acme/app", 9,
                             status="failed", head_sha="sha")
        self.assertTrue(dashboard.do_action("retry", cid))
        self.assertEqual(db.get_card(self.c, key)["status"], "intake")


class GateIsAtomicTest(unittest.TestCase):
    """게이트를 '읽고→검사하고→쓰는' 세 단계로 두면 중복 클릭·낡은 탭이 게이트를
    두 번 연다. connect() 는 autocommit 이고 대시보드는 ThreadingHTTPServer 다."""

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
        self.key = keys.issue_key(REPO, 77)
        self.cid = db.upsert_card(self.c, self.key, "issue", REPO, 77,
                                  status="spec_blocked", blocked=1,
                                  payload={"display": "PH-77", "title": "t",
                                           "agreement": {"design": "d", "rounds": 2}})

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        self.c.close()

    def _types(self):
        return [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]

    def test_second_approval_is_refused_and_logged(self):
        card = db.get_card(self.c, self.key)
        self.assertTrue(db.gate(self.c, card, "implementing", blocked=0,
                                event="operator_spec_approved"))
        # 같은(낡은) 카드 스냅샷으로 한 번 더 — 중복 클릭
        self.assertFalse(db.gate(self.c, card, "implementing", blocked=0,
                                 event="operator_spec_approved"))
        self.assertIn("gate_stale", self._types())
        self.assertEqual(self._types().count("operator_spec_approved"), 1)

    def test_status_only_moves_once_through_do_action(self):
        self.assertTrue(dashboard.do_action("approve_spec", self.cid))
        self.assertEqual(db.get_card(self.c, self.key)["status"], "implementing")
        # 이미 implementing 이므로 두 번째 승인은 상태 검사에서 막힌다
        self.assertFalse(dashboard.do_action("approve_spec", self.cid))
        self.assertEqual(self._types().count("operator_spec_approved"), 1)

    def test_pr_gate_is_atomic_too(self):
        db.set_status(self.c, self.cid, "pr_blocked", blocked=1)
        card = db.get_card(self.c, self.key)
        self.assertTrue(db.gate(self.c, card, "pr_opening", blocked=0,
                                event="operator_pr_approved"))
        self.assertFalse(db.gate(self.c, card, "pr_opening", blocked=0,
                                 event="operator_pr_approved"))
        self.assertEqual(self._types().count("operator_pr_approved"), 1)

    def test_gate_stale_carries_expected_and_actual(self):
        card = db.get_card(self.c, self.key)
        db.set_status(self.c, self.cid, "implementing")     # 다른 경로가 먼저 옮김
        self.assertFalse(db.gate(self.c, card, "pr_opening", event="x"))
        row = self.c.execute(
            "SELECT detail FROM events WHERE type='gate_stale'").fetchone()
        d = json.loads(row["detail"])
        self.assertEqual((d["expected"], d["actual"]), ("spec_blocked", "implementing"))


class TopicPromotionTest(unittest.TestCase):
    """주제 토론 결과를 게이트에서 고른다 — 구현으로 승격하거나 완료로 닫는다.
    승격 경로가 없으면 결론이 나와도 사람이 손으로 옮겨야 한다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"connect": dashboard.db.connect, "kick": dashboard.kick_tick,
                      "paths": dashboard.CFG.get("impl_repo_paths")}

        @contextlib.contextmanager
        def fake_connect():
            yield self.c

        dashboard.db.connect = fake_connect
        dashboard.kick_tick = lambda: None
        dashboard.CFG["impl_repo_paths"] = {"acme/web": "/checkouts/web"}
        self.key = keys.topic_key(1, 1000.0)
        self.cid = db.upsert_card(
            self.c, self.key, "issue", dashboard.TOPIC_REPO, 0,
            status="spec_blocked", blocked=1,
            payload={"display": "TOPIC-1", "title": "취약점?", "mode": "debate_only",
                     "topic": "취약점?", "agreement": {"design": "이렇게 고쳐라",
                                                     "rounds": 6, "settled": False}})

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        if self.saved["paths"] is None:
            dashboard.CFG.pop("impl_repo_paths", None)
        else:
            dashboard.CFG["impl_repo_paths"] = self.saved["paths"]
        self.c.close()

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def _types(self):
        return [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]

    def test_promote_moves_to_implementing_with_the_agreement_kept(self):
        self.assertTrue(dashboard.do_action("implement_topic", self.cid,
                                            text="www 쪽만", repo="acme/web"))
        card, payload = self._card(), self._payload()
        self.assertEqual(card["status"], "implementing")
        self.assertEqual(card["blocked"], 0)
        self.assertEqual(payload["target_repo"], "acme/web")
        self.assertEqual(payload["mode"], "implement")     # 이제 일반 작업이다
        self.assertEqual(payload["spec_amendment"], "www 쪽만")
        self.assertEqual(payload["agreement"]["design"], "이렇게 고쳐라")  # 보존
        self.assertIn("topic_promoted", self._types())

    def test_close_path_still_goes_to_done(self):
        self.assertTrue(dashboard.do_action("approve_spec", self.cid))
        self.assertEqual(self._card()["status"], "done")
        self.assertIn("topic_accepted", self._types())

    def test_unconfigured_repo_is_refused_with_a_logged_reason(self):
        self.assertFalse(dashboard.do_action("implement_topic", self.cid,
                                            repo="acme/not-configured"))
        self.assertEqual(self._card()["status"], "spec_blocked")
        self.assertIn("topic_promote_blocked", self._types())

    def test_missing_repo_is_refused(self):
        self.assertFalse(dashboard.do_action("implement_topic", self.cid, repo=""))
        self.assertEqual(self._card()["status"], "spec_blocked")

    def test_promote_only_from_the_gate(self):
        db.set_status(self.c, self.cid, "spec")
        self.assertFalse(dashboard.do_action("implement_topic", self.cid, repo="acme/web"))

    def test_issue_cards_are_not_promotable_this_way(self):
        key = keys.issue_key(REPO, 5)
        cid = db.upsert_card(self.c, key, "issue", REPO, 5, status="spec_blocked",
                             blocked=1, payload={"display": "PH-5", "mode": "debate"})
        self.assertFalse(dashboard.do_action("implement_topic", cid, repo="acme/web"))

    def test_gate_offers_both_choices_in_the_ui(self):
        self.assertIn("🛠 이 결론으로 구현", dashboard.HTML)
        self.assertIn("✅ 완료로 닫기", dashboard.HTML)
        self.assertIn("function implementTopic", dashboard.HTML)

    def test_legacy_agreement_without_settled_is_flagged(self):
        self.assertIn("합의 여부 미기록", dashboard.HTML)


class InputPreservationTest(unittest.TestCase):
    """5초 폴링이 카드 DOM 을 다시 그린다 — 입력 중이던 textarea 가 새로 만들어져
    타이핑하던 내용이 사라졌다(실측 제보)."""

    def setUp(self):
        self.html = dashboard.HTML

    def test_render_is_skipped_while_typing_in_the_board(self):
        self.assertIn("function typingInBoard", self.html)
        self.assertIn("if(typingInBoard())return;", self.html)
        # 보드 안의 입력만 막는다 — 다른 곳 포커스는 갱신을 멈추지 않는다
        self.assertIn("board.contains(a)", self.html)

    def test_every_card_input_keeps_a_draft(self):
        """카드 안의 모든 입력이 초안을 붙들어야 한다 — 하나라도 빠지면 그 칸만
        5초마다 지워진다. 개수를 못박는 대신 실제 태그를 훑는다."""
        tile = self.html[self.html.index("function issueTile"):
                         self.html.index("function tile(c)")]
        inputs = re.findall(r"<(?:textarea|input)\b[^>]*>", tile, re.S)
        self.assertGreaterEqual(len(inputs), 4)   # 지시·설계 피드백·저장소·수정 요청
        for tag in inputs:
            self.assertIn('oninput="draft(this)"', tag, f"초안 미보관: {tag[:80]}")
            self.assertIn("event.stopPropagation()", tag, f"모달이 열린다: {tag[:80]}")
        for el in ("'ins'+c.id", "'spec'+c.id", "'trepo'+c.id"):
            self.assertIn(f"dval({el}", self.html)

    def test_draft_survives_a_rerender_and_beats_the_server_value(self):
        # dval(id, fallback): 초안이 있으면 서버 값보다 우선한다
        self.assertIn("function dval(id,fallback){return DRAFTS[id]!==undefined?DRAFTS[id]:(fallback||'');}",
                      self.html.replace("\n", "").replace("  ", ""))

    def test_drafts_are_cleared_after_a_successful_submit(self):
        for call in ("clearDraft('ins'+id)", "clearDraft('spec'+id)", "clearDraft('trepo'+id)"):
            self.assertIn(call, self.html)


class PrGateReworkTest(unittest.TestCase):
    """PR 게이트에 승인만 있으면, diff 에서 문제를 봐도 되돌릴 길이 없다."""

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
        self.cid = db.upsert_card(
            self.c, self.key, "issue", REPO, 1816, status="pr_blocked", blocked=1,
            payload={"display": "PH-1816", "title": "t", "branch": "feature/PH-1816",
                     "impl_rounds": 2, "feedback": "[검증] 널 가드 없음"})

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        self.c.close()

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def test_request_changes_sends_it_back_to_implementing(self):
        self.assertTrue(dashboard.do_action("request_changes", self.cid,
                                            text="취소 시 모달이 다시 열린다"))
        card = self._card()
        self.assertEqual(card["status"], "implementing")
        self.assertEqual(card["blocked"], 0)
        types = [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]
        self.assertIn("operator_request_changes", types)

    def test_latest_blockers_are_carried_not_the_stale_ones(self):
        """구현자가 받아야 할 것은 게이트를 막은 **이번** 지적이다. 직전 라운드의
        (이미 고친) 지적을 다시 보내면 되돌림이 돈다."""
        db.merge_payload(self.c, self.cid, {"verify": {
            "approved": False,
            "blocking": [{"file": "IntroStepContent.tsx", "line": "327-330",
                          "problem": "닫힌 창을 가리킨다", "fix": "closed 검사"}]}})
        dashboard.do_action("request_changes", self.cid, text="취소 시 모달 재개방")
        fb = self._payload()["feedback"]
        self.assertIn("IntroStepContent.tsx:327-330", fb)          # 최신 블로커
        self.assertIn("[운영자 수정 요청] 취소 시 모달 재개방", fb)
        self.assertNotIn("[검증] 널 가드 없음", fb)                # 낡은 것은 안 실린다

    def test_empty_note_is_allowed_when_blockers_exist(self):
        """검증이 남긴 지적을 사람이 다시 타이핑할 이유가 없다."""
        db.merge_payload(self.c, self.cid, {"verify": {
            "approved": False,
            "blocking": [{"file": "a.ts", "line": "1", "problem": "깨짐", "fix": "고쳐"}]}})
        self.assertTrue(dashboard.do_action("request_changes", self.cid, text=""))
        self.assertIn("a.ts:1", self._payload()["feedback"])
        self.assertEqual(self._card()["status"], "implementing")

    def test_approved_verification_does_not_inject_blockers(self):
        db.merge_payload(self.c, self.cid, {"verify": {"approved": True, "blocking": []}})
        self.assertTrue(dashboard.do_action("request_changes", self.cid, text="그래도 이건 고쳐라"))
        fb = self._payload()["feedback"]
        self.assertIn("[운영자 수정 요청] 그래도 이건 고쳐라", fb)
        self.assertNotIn("[검증 미해결]", fb)

    def test_round_budget_is_raised_so_it_does_not_die_immediately(self):
        """impl_rounds 가 이미 상한이면 되돌리자마자 impl_rounds_exhausted 로 죽는다."""
        self.assertTrue(dashboard.do_action("request_changes", self.cid, text="고쳐라"))
        self.assertEqual(self._payload()["impl_bonus"], dashboard.IMPL_BONUS)

    def test_empty_note_with_nothing_to_say_is_refused(self):
        """넘길 지적도 없고 사람도 안 썼으면 되돌릴 이유가 없다."""
        db.merge_payload(self.c, self.cid, {"verify": {"approved": True, "blocking": []}})
        self.assertFalse(dashboard.do_action("request_changes", self.cid, text="  "))
        self.assertEqual(self._card()["status"], "pr_blocked")

    def test_only_from_the_pr_gate(self):
        db.set_status(self.c, self.cid, "implementing")
        self.assertFalse(dashboard.do_action("request_changes", self.cid, text="x"))

    def test_gate_offers_both_paths(self):
        self.assertIn("🚀 PR 올리기 승인", dashboard.HTML)
        self.assertIn("↩︎ 수정 요청", dashboard.HTML)
        self.assertIn("function requestChanges", dashboard.HTML)


class ReviewGateIsSeparateTest(unittest.TestCase):
    """PR 승인 대기는 '올릴 준비가 됐다'는 뜻이어야 한다 — 검증 미통과는 다른 레인."""

    def test_review_gate_lane_exists_between_verify_and_pr(self):
        lanes = [k for k, _ in dashboard.WORK_LANES]
        self.assertEqual(lanes[lanes.index("impl_verify") + 1], "verify_blocked")
        self.assertEqual(lanes[lanes.index("verify_blocked") + 1], "pr_blocked")

    def test_review_gate_offers_three_paths(self):
        for label in ("↩︎ 수정 요청", "🔁 다시 검증", "⚠️ 그래도 PR 로"):
            self.assertIn(label, dashboard.HTML)
        for fn in ("function rerunVerify", "function verifyOverride"):
            self.assertIn(fn, dashboard.HTML)

    def test_override_is_marked_on_the_pr_gate(self):
        self.assertIn("⚠️ 미통과인데 PR 올리기", dashboard.HTML)
        self.assertIn("검증 미통과를 감수하고 넘어온 카드", dashboard.HTML)

    def test_board_row_carries_the_flag(self):
        self.assertIn('"verify_exhausted": bool(meta.get("verify_exhausted"))',
                      open("src/dashboard.py", encoding="utf-8").read())


class ReviewGateActionsTest(unittest.TestCase):
    """검토 게이트의 세 갈래가 실제로 동작해야 한다."""

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
        self.cid = db.upsert_card(
            self.c, self.key, "issue", REPO, 1816, status="verify_blocked", blocked=1,
            payload={"display": "PH-1816", "title": "t", "branch": "feature/PH-1816",
                     "impl_rounds": 3, "verify_exhausted": True,
                     "verify": {"approved": False,
                                "blocking": [{"file": "a.ts", "line": "1",
                                              "problem": "깨짐", "fix": "가드"}]}})

    def tearDown(self):
        dashboard.db.connect = self.saved["connect"]
        dashboard.kick_tick = self.saved["kick"]
        self.c.close()

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def test_rerun_verify_goes_back_to_verification_with_a_flag(self):
        self.assertTrue(dashboard.do_action("rerun_verify", self.cid))
        self.assertEqual(self._card()["status"], "impl_verify")
        self.assertTrue(self._payload()["reverify_only"])
        self.assertEqual(self._payload()["impl_rounds"], 3)   # 라운드 소비 없음

    def test_rerun_verify_carries_the_operators_note_to_the_verifier(self):
        """재검증의 쓸모 대부분은 '이 관점으로 다시 보라'다. 입력을 버리면 완전히
        같은 입력으로 같은 판정이 나온다."""
        self.assertTrue(dashboard.do_action("rerun_verify", self.cid,
                                            text="  캐시 false 경로만 봐라  "))
        self.assertEqual(self._payload()["reverify_note"], "캐시 false 경로만 봐라")
        note = self.c.execute(
            "SELECT detail FROM events WHERE type='operator_rerun_verify'").fetchone()[0]
        self.assertIn("캐시 false", note)

    def test_rerun_verify_without_a_note_still_works(self):
        self.assertTrue(dashboard.do_action("rerun_verify", self.cid))
        self.assertEqual(self._card()["status"], "impl_verify")
        self.assertEqual(self._payload()["reverify_note"], "")

    def test_override_moves_to_the_pr_gate_and_is_recorded(self):
        self.assertTrue(dashboard.do_action("verify_override", self.cid))
        card = self._card()
        self.assertEqual(card["status"], "pr_blocked")
        self.assertEqual(card["blocked"], 1)
        self.assertTrue(self._payload()["verify_override"])
        types = [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]
        self.assertIn("operator_verify_override", types)

    def test_request_changes_works_from_the_review_gate_too(self):
        self.assertTrue(dashboard.do_action("request_changes", self.cid, text=""))
        self.assertEqual(self._card()["status"], "implementing")
        self.assertIn("a.ts:1", self._payload()["feedback"])

    def test_these_actions_only_apply_at_the_review_gate(self):
        db.set_status(self.c, self.cid, "pr_blocked")
        self.assertFalse(dashboard.do_action("rerun_verify", self.cid))
        self.assertFalse(dashboard.do_action("verify_override", self.cid))


class ModalReadabilityTest(unittest.TestCase):
    """엔진은 마크다운으로 답한다 — 원문 그대로 뿌리면 기호가 노출되고 긴 설계안은
    읽을 수 없다. 그리고 긴 블록을 순서 없이 쌓으면 지금 결정할 것이 안 보인다."""

    def test_unresolved_items_do_not_demand_a_decision_after_the_design_gate(self):
        """설계 게이트에만 '결정하라'가 성립한다 — PR 게이트엔 누를 버튼이 없어서
        같은 문구를 띄우면 사람에게 할 수 없는 일을 요구하게 된다."""
        html = dashboard.HTML
        self.assertIn("승인 전에 결정해야 합니다", html)
        self.assertIn("PR 본문에 함께 남습니다", html)
        # 설계 게이트 분기 안에서만 '결정하라'가 나온다
        demand = html.index("승인 전에 결정해야 합니다")
        guard = html.rindex("c.status==='spec_blocked'", 0, demand)
        self.assertLess(demand - guard, 200)

    def setUp(self):
        self.html = dashboard.HTML
        self.modal = self.html[self.html.index("function openIssueModal"):
                               self.html.index("function openFeedbackModal")]

    def test_markdown_renderer_exists_and_escapes_first(self):
        self.assertIn("function md(t)", self.html)
        body = self.html[self.html.index("function md(t)"):
                         self.html.index("function repoShort")]
        self.assertIn("esc(String(t))", body)          # XSS: 먼저 이스케이프
        self.assertLess(body.index("esc(String(t))"), body.index("<strong>"))

    def test_long_fields_go_through_the_renderer(self):
        for field in ("md(AG.design)", "md(IM.summary)", "md(t.claim)",
                      "md(b.problem)", "md(c.feedback)"):
            self.assertIn(field, self.modal, f"{field} 가 원문 그대로 나간다")

    def test_detail_sections_are_collapsible(self):
        self.assertIn("details.sec", self.html)         # CSS
        self.assertIn("<details class=\"sec\"", self.modal)
        for title in ("🗣 토론 기록", "⏱ 진행 기록", "📁 변경 파일", "💻 직접 돌려보기"):
            self.assertIn(title, self.modal)

    def test_decision_material_is_above_the_fold(self):
        """블로커·미합의·게이트 버튼은 접힌 섹션보다 위에 있어야 한다."""
        first_details = self.modal.index("h+=sec(")   # 헬퍼 정의가 아니라 첫 호출
        for must_be_early in ("교차 검증", "미합의", "onclick=\"requestChanges"):
            self.assertLess(self.modal.index(must_be_early), first_details,
                            f"{must_be_early} 가 접힌 섹션 아래에 있다")

    def test_no_newline_escape_inside_the_renderer(self):
        """HTML 은 파이썬 문자열이라 JS 안의 개행 이스케이프가 실제 개행이 된다."""
        body = self.html[self.html.index("function md(t)"):
                         self.html.index("function repoShort")]
        self.assertIn("String.fromCharCode(10)", self.html)   # NL 상수를 쓴다


class WorkBoardLayoutTest(unittest.TestCase):
    """작업 보드는 칸반이 아니다 — 레인 이동은 워커가 하고, 사람이 하는 일은
    게이트 응답 하나뿐이다. 단계별 컬럼은 대부분 비어 가로 스크롤만 만든다."""

    def _groups(self):
        import re
        return re.findall(r"lanes:\[([^\]]*)\]", dashboard.HTML)

    def test_every_work_lane_lands_in_exactly_one_group(self):
        """빠진 레인이 있으면 그 상태의 카드는 화면에서 **사라진다** — 조용히
        일이 멈추는 가장 나쁜 실패다."""
        seen = []
        for g in self._groups():
            seen += [x.strip().strip("'") for x in g.split(",") if x.strip()]
        for key, _label in dashboard.WORK_LANES:
            self.assertIn(key, seen, f"{key} 레인이 어느 그룹에도 없다")
        self.assertEqual(len(seen), len(set(seen)), "같은 레인이 두 그룹에 있다")

    def test_the_gate_group_comes_first(self):
        """'내 차례'가 맨 위에 있어야 스크롤 없이 할 일이 보인다."""
        html = dashboard.HTML
        gate = html.index("'spec_blocked','verify_blocked','pr_blocked'")
        for other in ("'triage'", "'done','failed'"):
            self.assertLess(gate, html.index(other))

    def test_work_view_does_not_use_the_kanban_renderer(self):
        self.assertIn("VIEW==='work'?renderWork()", dashboard.HTML)

    def test_the_review_board_keeps_its_lanes(self):
        """리뷰는 카드가 수백 장이라 레인이 실제로 채워진다 — 건드리지 않는다."""
        self.assertIn("renderLanes(LANES)", dashboard.HTML)
        self.assertIn("function renderLanes", dashboard.HTML)


if __name__ == "__main__":
    unittest.main()
