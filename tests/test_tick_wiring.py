import ast
import inspect
import textwrap
import unittest

from src import tick

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

    def test_no_stage_picks_up_issue_cards(self):
        """이슈 작업 기능은 걷어냈다. 라이브 DB 에 남은 옛 kind='issue' 카드를
        어떤 스테이지도 다시 집어가면 안 된다."""
        for node in self._stage_calls():
            self.assertNotIn("kind='issue'", ast.unparse(node))
