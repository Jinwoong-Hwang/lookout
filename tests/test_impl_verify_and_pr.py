import json
import sqlite3
import unittest

from src import db, engines, ghclient, impl_verifier, keys, pr_opener, prdiff, prompt_tpl, worktree

REPO = "acme/product-hub"
TARGET = "acme/ceo-client"


class _Base(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.key = keys.issue_key(REPO, 1765)
        self.card_id = db.upsert_card(
            self.c, self.key, "issue", REPO, 1765, status="impl_verify",
            payload={"display": "PH-1765", "title": "401 처리", "url": "u",
                     "target_repo": TARGET, "branch": "feature/PH-1765",
                     "impl": {"summary": "고쳤다", "verification": "yarn test 통과"}})
        self.c.execute("UPDATE cards SET engine='claude' WHERE id=?", (self.card_id,))
        self.saved = {
            "parent": worktree.impl_parent, "base": worktree._impl_base_ref,
            "git": worktree._git, "mk": worktree.make_impl_worktree,
            "pack": prdiff.pack, "render": prompt_tpl.render,
            "runjson": engines.run_json, "ready": engines.is_ready,
            "prcreate": getattr(ghclient, "pr_create_draft", None),
            "icomment": getattr(ghclient, "issue_comment", None),
        }
        worktree.impl_parent = lambda _r: "/parent"
        worktree._impl_base_ref = lambda *_a: "origin/master"
        worktree.make_impl_worktree = lambda *_a, **_k: "/wt"
        prdiff.pack = lambda raw, n: (raw, "a.txt", None)
        prompt_tpl.render = lambda *_a, **_k: "PROMPT"
        engines.is_ready = lambda _e: True
        self.git_calls = []

        class P:
            stdout = "diff --git a/a.txt b/a.txt\n+x\n"
        worktree._git = lambda *a, **k: (self.git_calls.append(a[1:]), P())[1]

    def tearDown(self):
        worktree.impl_parent = self.saved["parent"]
        worktree._impl_base_ref = self.saved["base"]
        worktree._git = self.saved["git"]
        worktree.make_impl_worktree = self.saved["mk"]
        prdiff.pack = self.saved["pack"]
        prompt_tpl.render = self.saved["render"]
        engines.run_json = self.saved["runjson"]
        engines.is_ready = self.saved["ready"]
        if self.saved["prcreate"]:
            ghclient.pr_create_draft = self.saved["prcreate"]
        if self.saved["icomment"]:
            ghclient.issue_comment = self.saved["icomment"]
        self.c.close()

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def _events(self):
        return [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]


class VerifierEngineTest(_Base):
    def test_opposite_engine_verifies(self):
        self.assertEqual(impl_verifier.verifier_engine("claude"), ("codex", False))
        self.assertEqual(impl_verifier.verifier_engine("codex"), ("claude", False))

    def test_falls_back_to_same_engine_but_marks_it(self):
        engines.is_ready = lambda e: e == "claude"
        engine, fallback = impl_verifier.verifier_engine("claude")
        self.assertEqual((engine, fallback), ("claude", True))

    def test_raises_when_no_engine_is_ready(self):
        engines.is_ready = lambda _e: False
        with self.assertRaises(RuntimeError):
            impl_verifier.verifier_engine("claude")


class VerifyOutcomeTest(_Base):
    def test_approved_stops_at_human_gate(self):
        engines.run_json = lambda *_a, **_k: {"approved": True, "summary": "깨끗하다",
                                              "meets_requirement": True}
        impl_verifier.process(self.c, self._card())
        card = self._card()
        self.assertEqual(card["status"], "pr_blocked")
        self.assertEqual(card["blocked"], 1)      # 사람이 눌러야 PR이 올라간다
        self.assertTrue(self._payload()["verify"]["approved"])
        self.assertIn("impl_verified", self._events())

    def test_blocking_findings_send_it_back_with_feedback(self):
        engines.run_json = lambda *_a, **_k: {
            "approved": False, "summary": "널 가드 없음",
            "blocking": [{"file": "a.ts", "line": "12", "problem": "user가 없으면 크래시",
                          "fix": "옵셔널 체이닝"}]}
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "implementing")
        payload = self._payload()
        self.assertEqual(payload["impl_rounds"], 2)
        self.assertIn("a.ts:12", payload["feedback"])
        self.assertIn("옵셔널 체이닝", payload["feedback"])
        self.assertIn("impl_rework", self._events())

    def test_approved_true_with_blockers_is_not_approved(self):
        engines.run_json = lambda *_a, **_k: {
            "approved": True, "blocking": [{"file": "a.ts", "problem": "깨진다"}]}
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "implementing")

    def test_operator_bonus_raises_the_round_cap(self):
        """사람이 수정을 요청하면 예산이 늘어 다시 한 바퀴 돈다."""
        db.merge_payload(self.c, self.card_id,
                         {"impl_rounds": impl_verifier.MAX_IMPL_ROUNDS, "impl_bonus": 1})
        engines.run_json = lambda *_a, **_k: {"approved": False,
                                             "blocking": [{"file": "a", "problem": "b"}]}
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "implementing")   # failed 가 아니다
        self.assertIn("impl_rework", self._events())

    def test_round_cap_goes_to_the_review_gate_not_the_pr_gate(self):
        """PR 승인 대기는 '올릴 준비가 됐다'는 뜻이어야 한다. 엔진이 합의하지 못한
        카드를 같은 레인에 두면 배지 하나로 구분해야 하고, 사람이 놓친다."""
        db.merge_payload(self.c, self.card_id, {"impl_rounds": impl_verifier.MAX_IMPL_ROUNDS})
        engines.run_json = lambda *_a, **_k: {"approved": False,
                                             "blocking": [{"file": "a", "problem": "b"}]}
        impl_verifier.process(self.c, self._card())
        card = self._card()
        self.assertEqual(card["status"], "verify_blocked")
        self.assertEqual(card["blocked"], 1)
        self.assertTrue(self._payload()["verify_exhausted"])
        self.assertIn("impl_rounds_exhausted", self._events())

    def test_passing_verification_is_the_only_way_into_the_pr_gate(self):
        engines.run_json = lambda *_a, **_k: {"approved": True, "summary": "ok"}
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "pr_blocked")

    def test_reverify_only_does_not_spend_an_implementation_round(self):
        """사람이 '다시 검증'만 요청하면 새 구현이 없다 — 라운드를 쓰거나
        구현으로 되돌리면 안 되고 검토 게이트로 돌아와야 한다."""
        db.merge_payload(self.c, self.card_id, {"impl_rounds": 1, "reverify_only": True})
        engines.run_json = lambda *_a, **_k: {"approved": False,
                                             "blocking": [{"file": "a", "problem": "b"}]}
        impl_verifier.process(self.c, self._card())
        payload = self._payload()
        self.assertEqual(self._card()["status"], "verify_blocked")
        self.assertEqual(payload["impl_rounds"], 1)        # 그대로
        self.assertFalse(payload["reverify_only"])         # 소비됨
        self.assertIn("reverify_done", self._events())

    def _render_tokens(self):
        """프롬프트에 실제로 들어간 토큰을 잡는다(_Base 가 render 를 스텁한다)."""
        seen = {}
        prompt_tpl.render = lambda _name, **kw: (seen.update(kw), "PROMPT")[1]
        return seen

    def test_the_operators_reverify_note_reaches_the_verifier(self):
        """사람이 적은 관점은 최초 지시와 **구분되어** 들어가야 한다 — 섞으면
        검증자가 '이슈가 요구한 것'과 '이번에 볼 것'을 구별하지 못한다."""
        db.merge_payload(self.c, self.card_id,
                         {"reverify_only": True, "reverify_note": "캐시 false 경로만 봐라",
                          "instruction": "원래 지시"})
        seen = self._render_tokens()
        engines.run_json = lambda *_a, **_k: {"approved": True, "summary": "ok"}
        impl_verifier.process(self.c, self._card())
        self.assertIn("캐시 false 경로만 봐라", seen["REVERIFY_NOTE"])
        self.assertIn("재검증 관점", seen["REVERIFY_NOTE"])
        self.assertEqual(seen["INSTRUCTION"], "원래 지시")   # 섞이지 않았다
        # 한 번 쓰고 비운다 — 남기면 다음 라운드가 낡은 관점으로 판정한다
        self.assertEqual(self._payload()["reverify_note"], "")

    def test_no_note_means_no_reverify_section_at_all(self):
        seen = self._render_tokens()
        engines.run_json = lambda *_a, **_k: {"approved": True, "summary": "ok"}
        impl_verifier.process(self.c, self._card())
        self.assertEqual(seen["REVERIFY_NOTE"], "")

    def test_the_real_template_consumes_the_reverify_token(self):
        """토큰 이름이 템플릿과 어긋나면 관점이 조용히 사라진다 — 진짜 파일로 확인."""
        prompt_tpl.render = self.saved["render"]
        out = prompt_tpl.render(
            "impl_verify.md", DISPLAY="", TITLE="", TARGET_REPO="", BRANCH="",
            BODY="", INSTRUCTION="", IMPL_SUMMARY="", DIFF="", FILES="",
            REVERIFY_NOTE="### 이번 재검증 관점 (사람이 지금 요청)\n캐시 false")
        self.assertIn("캐시 false", out)
        self.assertNotIn("{REVERIFY_NOTE}", out)

    def test_passing_verification_clears_the_exhausted_mark(self):
        db.merge_payload(self.c, self.card_id, {"verify_exhausted": True})
        engines.run_json = lambda *_a, **_k: {"approved": True, "summary": "ok"}
        impl_verifier.process(self.c, self._card())
        self.assertFalse(self._payload()["verify_exhausted"])

    def test_missing_branch_fails_loudly(self):
        db.merge_payload(self.c, self.card_id, {"branch": ""})
        engines.run_json = lambda *_a, **_k: self.fail("엔진을 불러선 안 된다")
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "failed")
        self.assertIn("impl_verify_no_branch", self._events())

    def test_empty_diff_fails_instead_of_approving_nothing(self):
        class Empty:
            stdout = ""
        worktree._git = lambda *_a, **_k: Empty()
        engines.run_json = lambda *_a, **_k: self.fail("엔진을 불러선 안 된다")
        impl_verifier.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "failed")
        self.assertIn("impl_verify_empty_diff", self._events())

    def test_diff_is_computed_in_parent_not_worktree(self):
        # 워크트리는 repo당 하나를 공유하므로 다른 카드가 브랜치를 바꿔놨을 수 있다
        engines.run_json = lambda *_a, **_k: {"approved": True}
        impl_verifier.process(self.c, self._card())
        self.assertIn(("diff", "origin/master...feature/PH-1765"), self.git_calls)


class PrOpenerTest(_Base):
    def setUp(self):
        super().setUp()
        db.set_status(self.c, self.card_id, "pr_opening")
        self.created = []
        self.comments = []
        ghclient.pr_create_draft = lambda *a: (self.created.append(a), "https://pr/1")[1]
        ghclient.issue_comment = lambda *a: self.comments.append(a)
        self.saved_dry = pr_opener.CFG.get("dry_run_pr")

    def tearDown(self):
        if self.saved_dry is None:
            pr_opener.CFG.pop("dry_run_pr", None)
        else:
            pr_opener.CFG["dry_run_pr"] = self.saved_dry
        super().tearDown()

    def test_dry_run_is_the_default_and_pushes_nothing(self):
        pr_opener.CFG.pop("dry_run_pr", None)
        pr_opener.process(self.c, self._card())
        self.assertEqual(self._card()["status"], "done")
        self.assertEqual(self.created, [])
        self.assertNotIn(("push", "--quiet", "-u", "origin", "feature/PH-1765"), self.git_calls)
        self.assertIn("pr_dryrun", self._events())
        self.assertIn("title", self._payload()["pr_dryrun"])

    def test_live_run_pushes_and_creates_draft_pr(self):
        pr_opener.CFG["dry_run_pr"] = False
        pr_opener.process(self.c, self._card())
        self.assertIn(("push", "--quiet", "-u", "origin", "feature/PH-1765"), self.git_calls)
        repo, base, head, title, body = self.created[0]
        self.assertEqual((repo, base, head), (TARGET, "master", "feature/PH-1765"))
        self.assertIn("PH-1765", title)
        self.assertIn("고쳤다", body)
        self.assertEqual(self._payload()["pr_url"], "https://pr/1")
        self.assertEqual(self._card()["status"], "done")
        # 이슈에 링크를 남긴다
        self.assertEqual(self.comments[0][0], REPO)
        self.assertIn("https://pr/1", self.comments[0][2])

    def test_title_follows_the_team_convention(self):
        """팀 관례는 Conventional Commits 다(실측: fix(PH-1609): …). 우리만
        `[PH-1816] …` 로 올리면 PR 목록에서 혼자 튄다."""
        pr_opener.CFG["dry_run_pr"] = False
        db.merge_payload(self.c, self.card_id, {
            "title": "[FE][CEO_APP] 401 에러시 사용자 정보 함께 전송",
            "impl": {"summary": "고쳤다", "pr_type": "feat"}})
        pr_opener.process(self.c, self._card())
        title = self.created[0][3]
        # scope 가 이슈 키를 들고 있으므로 제목의 [FE][CEO_APP] 태그는 뺀다
        self.assertEqual(title, "feat(PH-1765): 401 에러시 사용자 정보 함께 전송")

    def test_a_bogus_type_falls_back_instead_of_breaking_the_format(self):
        pr_opener.CFG["dry_run_pr"] = False
        db.merge_payload(self.c, self.card_id, {"impl": {"pr_type": "버그수정"}})
        pr_opener.process(self.c, self._card())
        self.assertTrue(self.created[0][3].startswith("fix(PH-1765): "))

    def test_a_long_title_is_trimmed_to_stay_scannable(self):
        pr_opener.CFG["dry_run_pr"] = False
        db.merge_payload(self.c, self.card_id, {"title": "가" * 200})
        pr_opener.process(self.c, self._card())
        self.assertLessEqual(len(self.created[0][3]), pr_opener.TITLE_MAX)

    def test_body_follows_the_team_template_order(self):
        """리뷰어가 늘 보던 순서(변경 요약 → 변경 내용 → 테스트 방법 → … →
        체크리스트)를 먼저 만나야 한다 — 우리 근거 섹션은 그 사이에 낀다."""
        pr_opener.CFG["dry_run_pr"] = False
        db.merge_payload(self.c, self.card_id, {
            "changed": ["a.ts"], "instruction": "ceo-client 만",
            "agreement": {"unresolved": ["미합의 하나"]},
            "verify": {"engine": "codex", "summary": "교차 검증 요약"},
            "impl": {"summary": "고쳤다", "changes": ["a.ts 에서 널 가드"],
                     "verification": "테스트 통과", "manual_test": ["실기 1회"],
                     "open_questions": ["정할 것"], "risk": "느려질 수 있다"}})
        pr_opener.process(self.c, self._card())
        body = self.created[0][4]
        order = ["## 변경 요약", "## 변경 내용", "## 테스트 방법",
                 "## 검증 — 엔진이 실행함", "## 교차 검증", "## 설계 단계 미합의",
                 "## 남은 결정", "## 위험", "## 운영자 지시", "## 체크리스트"]
        seen = [body.index(h) for h in order]
        self.assertEqual(seen, sorted(seen), "섹션 순서가 템플릿과 다르다")

    def test_change_bullets_prefer_the_engine_over_the_file_list(self):
        pr_opener.CFG["dry_run_pr"] = False
        db.merge_payload(self.c, self.card_id, {
            "changed": ["a.ts", "b.ts"],
            "impl": {"summary": "x", "changes": ["널 가드 추가", "테스트 2건"]}})
        pr_opener.process(self.c, self._card())
        body = self.created[0][4]
        self.assertIn("- 널 가드 추가", body)
        self.assertNotIn("- `a.ts`", body)

    def test_without_engine_bullets_it_falls_back_to_files_not_invention(self):
        """불릿이 없다고 지어내면 PR 본문이 거짓이 된다 — 파일 목록은 사실이다."""
        pr_opener.CFG["dry_run_pr"] = False
        db.merge_payload(self.c, self.card_id, {
            "changed": [f"f{i}.ts" for i in range(20)], "impl": {"summary": "x"}})
        pr_opener.process(self.c, self._card())
        body = self.created[0][4]
        self.assertIn("- `f0.ts`", body)
        self.assertIn(f"외 {20 - pr_opener.FILES_SHOWN}개 파일", body)

    def test_manual_tests_and_open_decisions_are_separate_lists(self):
        """'해볼 것'과 '정할 것'을 한데 묶으면 리뷰어가 무엇을 해봐야 하는지
        찾지 못하고, 결정 사항이 테스트 항목으로 위장된다."""
        pr_opener.CFG["dry_run_pr"] = False
        db.merge_payload(self.c, self.card_id, {"impl": {
            "summary": "고쳤다",
            "manual_test": ["www 에서 팝업이 실제로 뜨는지"],
            "open_questions": ["부모 게이트를 조회 기반으로 바꿀지"]}})
        pr_opener.process(self.c, self._card())
        body = self.created[0][4]
        self.assertIn("## 테스트 방법", body)
        self.assertIn("- [ ] www 에서 팝업이 실제로 뜨는지", body)
        self.assertIn("## 남은 결정", body)
        self.assertIn("- [ ] 부모 게이트를 조회 기반으로 바꿀지", body)
        self.assertLess(body.index("## 테스트 방법"), body.index("## 남은 결정"))

    def test_the_reviewer_checklist_is_never_pre_checked(self):
        """엔진의 자기 보고(## 검증)와 다른 축이다 — 사람이 ready 로 올릴 때 쓴다."""
        pr_opener.CFG["dry_run_pr"] = False
        pr_opener.process(self.c, self._card())
        body = self.created[0][4]
        self.assertIn("## 체크리스트 (ready 전환 전 확인)", body)
        self.assertIn("- [ ] 빌드 성공 확인", body)
        self.assertNotIn("- [x]", body)

    def test_unresolved_design_items_reach_the_pr_body(self):
        """카드에만 두면 PR 리뷰어는 그 다툼이 있었다는 사실조차 모른다."""
        pr_opener.CFG["dry_run_pr"] = False
        db.merge_payload(self.c, self.card_id, {"agreement": {
            "settled": False, "unresolved": ["www 실기 1회 확인 필요", "별도 티켓으로"]}})
        pr_opener.process(self.c, self._card())
        body = self.created[0][4]
        self.assertIn("설계 단계 미합의", body)
        self.assertIn("- [ ] www 실기 1회 확인 필요", body)
        self.assertIn("- [ ] 별도 티켓으로", body)

    def test_no_unresolved_items_adds_no_empty_section(self):
        pr_opener.CFG["dry_run_pr"] = False
        pr_opener.process(self.c, self._card())
        self.assertNotIn("설계 단계 미합의", self.created[0][4])

    def test_pr_is_created_as_draft_not_promoted_later(self):
        """생성 후 draft로 내려도 이미 걸린 코드 소유자 리뷰 요청은 회수되지 않는다.
        그래서 --draft 는 생성 인자에 있어야 한다."""
        with open("src/ghclient.py", encoding="utf-8") as f:
            gh = f.read()
        self.assertIn('"pr", "create", "--repo", repo, "--draft"', gh)


if __name__ == "__main__":
    unittest.main()


class ExhaustedFeedbackTest(_Base):
    """소진으로 사람에게 올릴 때도 최신 블로커를 feedback 에 남겨야, 사람이
    '수정 요청'을 눌렀을 때 구현자가 그 지적을 받는다."""

    def test_exhaustion_records_the_blockers_for_the_next_round(self):
        db.merge_payload(self.c, self.card_id,
                         {"impl_rounds": impl_verifier.MAX_IMPL_ROUNDS,
                          "feedback": "낡은 지적"})
        engines.run_json = lambda *_a, **_k: {
            "approved": False,
            "blocking": [{"file": "x.ts", "line": "10", "problem": "터진다", "fix": "가드"}]}
        impl_verifier.process(self.c, self._card())
        fb = self._payload()["feedback"]
        self.assertIn("x.ts:10", fb)
        self.assertIn("[검증 미해결]", fb)
        self.assertNotIn("낡은 지적", fb)

    def test_rework_path_uses_the_same_wording(self):
        engines.run_json = lambda *_a, **_k: {
            "approved": False,
            "blocking": [{"file": "y.ts", "line": "2", "problem": "p", "fix": "f"}]}
        impl_verifier.process(self.c, self._card())
        self.assertIn("y.ts:2 — p / 고치는 방향: f", self._payload()["feedback"])
