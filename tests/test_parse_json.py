import json
import unittest

from src import claude_runner


class ParseJsonTest(unittest.TestCase):
    """엔진 응답에서 완결된 JSON 을 뽑아야 한다.

    첫 ``` 와 다음 ``` 사이를 그냥 자르면 본문에 코드펜스가 들어간 순간 JSON 이
    중간에서 끊긴다. PH-1816 토론에서 제안자 응답 3개가 전부 이 경로로 실패했고,
    폴백이 300자로 잘라 다음 턴이 반쪽 입력으로 논쟁했다.
    """

    def test_code_fence_inside_a_value_survives(self):
        inner = {"claim": "A",
                 "proposal": "이렇게 고친다:\n```ts\nconst x = 1\n```\n끝",
                 "verdict": "CONTINUE"}
        raw = "```json\n" + json.dumps(inner, ensure_ascii=False) + "\n```"
        out = claude_runner.parse_json(raw)
        self.assertEqual(out["claim"], "A")
        self.assertIn("```ts", out["proposal"])       # 본문의 펜스가 보존된다
        self.assertEqual(out["verdict"], "CONTINUE")

    def test_plain_object(self):
        self.assertEqual(claude_runner.parse_json('{"a": 1}'), {"a": 1})

    def test_prose_around_the_object(self):
        self.assertEqual(claude_runner.parse_json('설명.\n{"a": 2}\n뒷말'), {"a": 2})

    def test_array_reply(self):
        self.assertEqual(claude_runner.parse_json("[1,2,3]"), [1, 2, 3])

    def test_braces_inside_strings_do_not_confuse_the_scan(self):
        raw = '{"t": "중괄호 { 와 } 가 값에 있다", "n": {"deep": 1}}'
        self.assertEqual(claude_runner.parse_json(raw)["n"]["deep"], 1)

    def test_escaped_quote_inside_a_string(self):
        raw = r'{"t": "그는 \"안 된다\" 라고 했다", "k": 1}'
        self.assertEqual(claude_runner.parse_json(raw)["k"], 1)

    def test_first_object_wins_when_two_are_present(self):
        self.assertEqual(claude_runner.parse_json('{"a":1}\n{"b":2}'), {"a": 1})

    def test_unparsable_still_raises(self):
        with self.assertRaises(claude_runner.ClaudeError):
            claude_runner.parse_json("JSON 이 아니다")

    def test_truncated_object_raises_instead_of_returning_garbage(self):
        with self.assertRaises(claude_runner.ClaudeError):
            claude_runner.parse_json('{"a": 1, "b": ')


if __name__ == "__main__":
    unittest.main()
