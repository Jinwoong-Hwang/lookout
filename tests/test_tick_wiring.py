import ast
import inspect
import textwrap
import unittest

from src import impl_worker, tick

# 스테이지 호출부는 kind를 반드시 넘겨야 한다 — db.cards_in은 status만 보므로
# kind가 빠지면 다른 kind의 카드가 그 스테이지로 들어간다.
REVIEW_LANES = {"intake", "reviewing", "verifying", "commenting", "commented",
                "lgtm", "triage", "failed"}
APPROVE_LANES = {"approving", "approve_blocked"}


class TickWiringTest(unittest.TestCase):
    """tick을 import하는 테스트가 없어서 import가 깨진 채로 전 스위트가 통과한 적이
    있다. 배선 자체를 테스트로 고정한다."""

    def setUp(self):
        self.src = inspect.getsource(tick.run_once)
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
            kinds = [k.arg for k in node.keywords]
            self.assertIn("kind", kinds,
                          f"kind 없는 스테이지 호출: {ast.unparse(node)[:80]}")

    def test_impl_worker_runs_only_on_issue_cards(self):
        hit = [n for n in self._stage_calls()
               if "impl_worker.process" in ast.unparse(n)]
        self.assertEqual(len(hit), 1, "impl_worker 스테이지가 없거나 중복됐다")
        call = ast.unparse(hit[0])
        self.assertIn("'implementing'", call)
        self.assertIn("kind='issue'", call)

    def test_impl_worker_is_retryable_so_failures_land_in_failed_lane(self):
        # RETRYABLE_STAGES에 없으면 실패가 stage_error만 남기고 카드는 그 레인에
        # 영원히 서 있는다 (조용히 멈춤).
        self.assertIn("impl_worker", tick.RETRYABLE_STAGES)

    def test_review_and_work_lanes_never_share_a_status(self):
        work = {"spec", "implementing", "impl_verify", "pr_blocked"}
        self.assertFalse(work & REVIEW_LANES)
        self.assertFalse(work & APPROVE_LANES)

    def test_target_unknown_is_not_retried(self):
        # 같은 입력이면 결과가 같으므로 예외로 올려 3번 태우지 않고 바로 failed로 보낸다
        src = inspect.getsource(impl_worker.process)
        self.assertIn("except TargetUnknown", src)
        self.assertIn('"failed"', src)


if __name__ == "__main__":
    unittest.main()
