import ast
import inspect
import textwrap
import unittest

from src import dashboard, impl_verifier, impl_worker, pr_opener, tick

# 스테이지 호출부는 kind를 반드시 넘겨야 한다 — db.cards_in은 status만 보므로
# kind가 빠지면 다른 kind의 카드가 그 스테이지로 들어간다.
REVIEW_LANES = {"intake", "reviewing", "verifying", "commenting", "commented",
                "lgtm", "triage", "failed"}
APPROVE_LANES = {"approving", "approve_blocked"}


class TickWiringTest(unittest.TestCase):
    """tick을 import하는 테스트가 없어서 import가 깨진 채로 전 스위트가 통과한 적이
    있다. 배선 자체를 테스트로 고정한다."""

    def setUp(self):
        # 모듈 전체를 훑는다. run_once만 보면 _fast_stages 안의 스테이지를 놓친다.
        self.src = inspect.getsource(tick)
        self.tree = ast.parse(textwrap.dedent(self.src))

    def _stage_calls(self):
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("_stage", "_wave")):
                yield node

    def test_every_stage_call_passes_a_kind(self):
        calls = list(self._stage_calls())
        self.assertTrue(calls)
        for node in calls:
            passes_kind = ("kind" in [k.arg for k in node.keywords]
                           or len(node.args) >= 4)   # _drain 은 위치인자로 넘긴다
            self.assertTrue(passes_kind,
                            f"kind 없는 스테이지 호출: {ast.unparse(node)[:80]}")

    def test_impl_worker_runs_only_on_issue_cards(self):
        hit = [n for n in self._stage_calls()
               if "impl_worker.process" in ast.unparse(n)]
        self.assertEqual(len(hit), 1, "impl_worker 스테이지가 없거나 중복됐다")
        call = ast.unparse(hit[0])
        self.assertIn("'implementing'", call)
        self.assertIn("kind='issue'", call)

    def test_every_work_lane_has_exactly_one_owner(self):
        """레인마다 집어가는 스테이지가 정확히 하나여야 한다. 없으면 카드가 영원히
        서고, 둘이면 같은 카드를 두 번 처리한다."""
        owners = {}
        for node in self._stage_calls():
            call = ast.unparse(node)
            for lane in ("spec", "implementing", "impl_verify", "pr_opening"):
                if f"'{lane}'" in call:
                    owners.setdefault(lane, []).append(call)
        for lane, calls in owners.items():
            self.assertEqual(len(calls), 1, f"{lane} 소유자가 {len(calls)}개")
            self.assertIn("kind='issue'", calls[0])
        self.assertEqual(set(owners), {"spec", "implementing", "impl_verify", "pr_opening"})

    def test_dashboard_can_only_start_stages_that_have_a_worker(self):
        """대시보드가 여는 시작 스테이지에 소유 워커가 없으면 카드가 그 레인에
        조용히 선다. spec(토론)이 정확히 이렇게 열려 있었다."""
        owned = set()
        for node in self._stage_calls():
            call = ast.unparse(node)
            if "kind='issue'" not in call:
                continue
            for lane, _ in dashboard.WORK_LANES:
                if f"'{lane}'" in call:
                    owned.add(lane)
        for action, status in dashboard.WORK_START.items():
            self.assertIn(status, owned, f"{action} → {status}: 집어가는 워커가 없다")
        # 막아둔 것은 반대로 소유 워커가 없어야 한다(있으면 열어야 한다)
        for action, (status, _why) in dashboard.WORK_START_PENDING.items():
            self.assertNotIn(status, owned,
                             f"{action} → {status}: 워커가 붙었으니 WORK_START 로 옮길 것")

    def test_verify_and_pr_stages_are_retryable(self):
        for stage in ("impl_verifier", "pr_opener", "debate_worker"):
            self.assertIn(stage, tick.RETRYABLE_STAGES)

    def test_impl_worker_is_retryable_so_failures_land_in_failed_lane(self):
        # RETRYABLE_STAGES에 없으면 실패가 stage_error만 남기고 카드는 그 레인에
        # 영원히 서 있는다 (조용히 멈춤).
        self.assertIn("impl_worker", tick.RETRYABLE_STAGES)

    def test_review_and_work_lanes_never_share_a_status(self):
        work = {"spec", "implementing", "impl_verify", "pr_blocked"}
        self.assertFalse(work & REVIEW_LANES)
        self.assertFalse(work & APPROVE_LANES)

    def test_human_gates_have_no_worker(self):
        """spec_blocked·pr_blocked 는 사람이 눌러야 넘어가는 상태다. 워커가 붙으면
        사람 게이트가 무력화된다."""
        src = inspect.getsource(tick)
        for gate in ("spec_blocked", "pr_blocked"):
            self.assertNotIn(f"'{gate}'", src)
            self.assertNotIn(f'"{gate}"', src)

    def test_verifier_is_read_only(self):
        """검증자는 run_json(read-only)을 쓴다. run_impl을 쓰면 양쪽이 편집 권한을
        갖고, 같은 제약을 서로 다르게 구현해 도달 불가 코드를 만든다."""
        src = inspect.getsource(impl_verifier.process)
        self.assertIn("engines.run_json", src)
        self.assertNotIn("run_impl", src)

    def test_pr_opener_defaults_to_dry_run(self):
        src = inspect.getsource(pr_opener.process)
        self.assertIn('CFG.get("dry_run_pr", True)', src)

    def test_target_unknown_is_not_retried(self):
        # 같은 입력이면 결과가 같으므로 예외로 올려 3번 태우지 않고 바로 failed로 보낸다
        src = inspect.getsource(impl_worker.process)
        self.assertIn("except TargetUnknown", src)
        self.assertIn('"failed"', src)


if __name__ == "__main__":
    unittest.main()
